"""Fill a local database with SYNTHETIC data, for the README's dashboard screenshots.

Everything comes from the test suite's fake Discovery API (tests/fake_discovery.py): invented show
names ("Synthetic evt00042"), invented venues, invented dates. No real Ticketmaster data, in line
with "never commit real API data". The script refuses any database that isn't on this machine, so
it can't write into production.

    uv run python scripts/demo_seed.py postgresql://marquee:marquee@localhost:55432/marquee_demo

It runs six ingests through the real pipeline: a baseline a day ago (so tomorrow's day of shows
"enters the window"), then a day of changes and new listings, one run cut short by a quota error
(PARTIAL), and three good runs.
"""

from __future__ import annotations

import random
import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from fake_discovery import FakeDiscovery, FakeEvent, client_for, undated  # noqa: E402

from marquee.db import connect, migrate  # noqa: E402
from marquee.ingest import ingest  # noqa: E402
from marquee.windows import LA, plan_range  # noqa: E402

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
VENUES = [f"KovFAKE{i:03d}" for i in range(1, 9)]
QUOTA = {"fault": {"faultstring": "Rate limit quota violation.",
                   "detail": {"errorcode": "policies.ratelimit.QuotaViolation"}}}


def api_time(instant: datetime) -> str:
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def evening(day: datetime | None, rng: random.Random, after: datetime) -> datetime:
    assert day is not None
    show = datetime.combine(day.date(), time(rng.choice([19, 19, 20, 20, 21]),
                                             rng.choice([0, 30])), tzinfo=LA)
    return max(show, after + timedelta(hours=1)).astimezone(UTC).replace(microsecond=0)


def synthetic_events(now: datetime, n: int = 360) -> list[FakeEvent]:
    """Evening shows over the 90 days, thinning out further ahead as the real market does."""
    rng = random.Random(7)
    whole = plan_range(now)
    first = datetime.combine(whole.first_day, time(), tzinfo=LA)
    events = []
    for i in range(n):
        offset = int(whole.days * (1 - (1 - rng.random()) ** 0.5))  # denser near today
        starts = evening(first + timedelta(days=offset), rng, whole.start)
        onsale_day = rng.randint(10, 120)
        onsale = (None if i % 41 == 0 else
                  api_time(datetime.combine((now - timedelta(days=onsale_day)).date(),
                                            time(10), tzinfo=LA)))
        events.append(FakeEvent(f"evt{i:05d}", starts, venue=rng.choice(VENUES), onsale=onsale,
                                status="offsale" if i % 53 == 0 else "onsale"))
    # A few shows on the range's last day, so the moving window visibly brings some in.
    last = datetime.combine(whole.first_day + timedelta(days=whole.days - 1), time(), tzinfo=LA)
    events += [FakeEvent(f"evtEND{i}", evening(last, rng, whole.start), venue=VENUES[i])
               for i in range(3)]
    # Public onsales in the next 7 days.
    for i in range(0, 60, 9):
        e = events[i]
        soon = datetime.combine((now + timedelta(days=1 + i % 6)).date(), time(10), tzinfo=LA)
        events[i] = FakeEvent(e.id, e.starts_at, venue=e.venue, onsale=api_time(soon))
    return events + undated(3)


def seed(url: str) -> None:
    if urlsplit(url).hostname not in LOCAL_HOSTS:
        raise SystemExit("demo_seed: refusing a database that isn't on this machine")
    now = datetime.now(UTC)
    api = FakeDiscovery(synthetic_events(now))
    with connect(url) as conn:
        migrate(conn)
        ingest(conn, client_for(api), now=now - timedelta(days=1))  # the baseline, a day ago
        ids = [e.id for e in api.events if e.starts_at is not None]
        api.update(ids[4], status="cancelled")
        api.update(ids[11], status="postponed")
        moved = next(e for e in api.events if e.id == ids[17])
        assert moved.starts_at is not None
        api.update(ids[17], status="rescheduled", starts_at=moved.starts_at + timedelta(days=9))
        api.update(ids[23], venue=VENUES[0] if moved.venue != VENUES[0] else VENUES[1])
        api.update(ids[30], onsale="2023-01-10T18:00:00Z")  # 3+ years early: a WARNING
        whole = plan_range(now)
        rng = random.Random(11)
        api.events += [FakeEvent(f"evtNEW{i}", evening(whole.start + timedelta(days=5 + 11 * i),
                                                       rng, whole.start), venue=VENUES[i + 2])
                       for i in range(4)]
        ingest(conn, client_for(api), now=now)
        api.failures[len(api.requests) + 4] = lambda: httpx.Response(429, json=QUOTA)
        ingest(conn, client_for(api), now=now)  # PARTIAL: the quota ran out mid-run
        for _ in range(3):
            ingest(conn, client_for(api), now=now)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    seed(sys.argv[1])
