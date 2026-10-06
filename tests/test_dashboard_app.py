"""The Streamlit app, run headless with Streamlit's own AppTest, against a throwaway schema.

The app reads its clock through queries.utc_now, so these tests pin "now" to the day their data
describes and don't depend on the date they run.
"""

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import streamlit as st
from conftest import Schema
from fake_discovery import FakeDiscovery, FakeEvent, client_for, spread, undated
from streamlit.dataframe_util import convert_arrow_bytes_to_pandas_df
from streamlit.testing.v1 import AppTest

from marquee import config, queries
from marquee.config import load_settings
from marquee.db import migrate
from marquee.ingest import ingest
from marquee.present import SCOPE_LABEL
from marquee.windows import plan_range

APP = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
NOW = datetime(2026, 10, 6, 15, 22, 49, tzinfo=UTC)
WHOLE = plan_range(NOW)


@pytest.fixture
def migrated(schema: Schema) -> Schema:
    with schema.connect() as conn:
        migrate(conn)
    return schema


@pytest.fixture
def render(monkeypatch: pytest.MonkeyPatch) -> Callable[[Schema], AppTest]:
    def run_app(schema: Schema) -> AppTest:
        settings = load_settings({"MARQUEE_DATABASE_URL": schema.url})
        monkeypatch.setattr(config, "settings_from_environment", lambda: settings)
        monkeypatch.setattr(queries, "connect_readonly",
                            lambda url: queries.make_readonly(schema.connect()))
        monkeypatch.setattr(queries, "utc_now", lambda: NOW)
        st.cache_data.clear()
        at = AppTest.from_file(str(APP), default_timeout=60)
        at.run()
        assert not at.exception, at.exception
        return at
    return run_app


def text(at: AppTest) -> str:
    parts = [e.value for kind in ("title", "header", "subheader", "markdown", "caption", "info",
                                  "warning", "error") for e in getattr(at, kind)]
    return "\n".join(str(p) for p in parts)


def table_text(at: AppTest) -> str:
    return "\n".join(t.value.to_string() for t in [*at.dataframe, *at.table])


def table_with(at: AppTest, column: str) -> Any:
    """The one table (st.table or st.dataframe) that has this column, as a DataFrame."""
    found = [t.value for t in [*at.dataframe, *at.table] if column in t.value.columns]
    assert len(found) == 1, f"{len(found)} tables have a {column!r} column"
    return found[0]


def outline(at: AppTest) -> list[str]:
    """Headers and subheaders in page order, as '# Header' and '## Subheader'."""
    marks = {"header": "#", "subheader": "##"}
    return [f"{marks[n.type]} {n.value}" for n in at.main if getattr(n, "type", "") in marks]


def ingest_with(schema: Schema, api: FakeDiscovery) -> None:
    with schema.connect() as conn:
        ingest(conn, client_for(api), now=NOW)


# --- Empty database: calm empty states, never a crash or a blank panel ------------------------

def test_an_empty_database_renders_every_empty_state(migrated: Schema,
                                                     render: Callable[[Schema], AppTest]) -> None:
    at = render(migrated)
    t = text(at)
    assert at.title[0].value == "Marquee"
    assert SCOPE_LABEL in t
    assert "NO RUNS YET" in t
    assert "No runs yet. Run `python -m marquee ingest`." in t
    assert "No checks yet." in t
    assert "No newly listed shows in the last 24 hours." in t
    assert "Entered the 90-day window: none in the last 24 hours." in t
    assert "No changes in the last 24 hours." in t
    assert "No public onsales in the next 7 days." in t
    assert "No events match these filters." in t
    assert "No listed events to chart yet." in t


# --- With data ------------------------------------------------------------------------------------

def test_the_headline_shows_freshness_and_the_completeness_proof(
    migrated: Schema, render: Callable[[Schema], AppTest]
) -> None:
    ingest_with(migrated, FakeDiscovery(spread(60, WHOLE.start, WHOLE.end) + undated(2)))
    t = text(render(migrated))
    assert SCOPE_LABEL in t
    assert "60 reported · 60 received · 0 missing" in t
    assert "+2 undated (TBA/TBD)" in t
    assert "Fresh" in t
    assert "0 failed · 0 warnings" in t


def test_changes_read_old_to_new_with_show_venue_and_date(
    migrated: Schema, render: Callable[[Schema], AppTest]
) -> None:
    api = FakeDiscovery(spread(60, WHOLE.start, WHOLE.end))
    ingest_with(migrated, api)
    api.update("evt00001", status="postponed")
    api.update("evt00002", starts_at=datetime(2026, 12, 3, 4, tzinfo=UTC))
    api.events.append(FakeEvent("evtNEW", datetime(2026, 10, 9, 3, tzinfo=UTC)))
    ingest_with(migrated, api)
    tables = table_text(render(migrated))
    assert "Synthetic evt00001" in tables and "onsale → postponed" in tables
    assert "→ Dec 2" in tables
    assert "Venue KovFAKE001" in tables
    assert "Synthetic evtNEW" in tables  # in the new shows table


def test_a_warning_says_warning_in_words(migrated: Schema,
                                         render: Callable[[Schema], AppTest]) -> None:
    events = spread(20, WHOLE.start, WHOLE.end)
    events[0] = FakeEvent(events[0].id, events[0].starts_at, onsale="2027-06-01T17:00:00Z")
    ingest_with(migrated, FakeDiscovery(events))
    at = render(migrated)
    assert "0 failed · 1 warning" in text(at)
    assert "WARNING" in table_text(at)  # the checks table carries the word


@pytest.mark.parametrize(("problem", "word"), [("partial", "PARTIAL"), ("failed", "FAILED")])
def test_a_bad_latest_run_says_so_in_words(
    migrated: Schema, render: Callable[[Schema], AppTest], problem: str, word: str
) -> None:
    api = FakeDiscovery(spread(300, WHOLE.start, WHOLE.end))
    ingest_with(migrated, api)  # one good run first
    if problem == "partial":
        api.failures[len(api.requests) + 3] = lambda: httpx.Response(429, json={"fault": {
            "faultstring": "Rate limit quota violation.",
            "detail": {"errorcode": "policies.ratelimit.QuotaViolation"}}})
    else:
        for n in range(len(api.requests) + 3, len(api.requests) + 8):
            api.failures[n] = lambda: httpx.Response(500)
    ingest_with(migrated, api)
    at = render(migrated)
    assert f"{word}" in text(at)  # the headline names the bad run in words
    assert word in table_text(at)  # and so does the runs table


# --- Dashboard review: sections, plain-English checks, statuses, weeks ---------------------------

def test_market_comes_first_then_pipeline_health_under_the_headline(
    migrated: Schema, render: Callable[[Schema], AppTest]
) -> None:
    at = render(migrated)
    assert outline(at) == [
        "# Market", "## Changes in the last 24 hours", "## Public onsales in the next 7 days",
        "## Upcoming events", "## Listed events per week",
        "# Pipeline health", "## Checks", "## Last 24 runs", "## Database size",
    ]
    order = [getattr(n, "value", None) for n in at.main]
    assert order.index("**Freshness**") < order.index("Market")  # the headline tiles stay on top


def test_checks_read_in_plain_english_with_what_happens_if_they_fail(
    migrated: Schema, render: Callable[[Schema], AppTest]
) -> None:
    events = spread(20, WHOLE.start, WHOLE.end)
    events[0] = FakeEvent(events[0].id, events[0].starts_at, onsale="2027-06-01T17:00:00Z")
    ingest_with(migrated, FakeDiscovery(events))
    at = render(migrated)
    checks = table_with(at, "If it fails")
    assert list(checks.columns) == ["Result", "Check", "If it fails", "Detail"]
    first = checks.iloc[0]  # problems first
    assert first["Result"] == ":orange[**WARNING**]"
    assert first["Check"].startswith("Onsale dates plausible")
    assert ":small[" in first["Check"] and "implausible\\_onsales" in first["Check"]
    assert first["If it fails"] == "Warning only"
    assert len(checks) == 7 and set(checks["If it fails"]) == {"Run fails", "Warning only"}
    assert "Every page fully fetched" in table_text(at)
    # No separate warning line above the table: the table already puts problems first.
    assert not any("implausible" in m.value for m in at.markdown)


def test_upcoming_events_shout_problem_statuses_with_weekdays_and_a_dash_for_no_onsale(
    migrated: Schema, render: Callable[[Schema], AppTest]
) -> None:
    events = spread(60, WHOLE.start, WHOLE.end)  # about one every 1.5 days
    events[0:5] = [
        FakeEvent(events[0].id, events[0].starts_at, status="cancelled"),
        FakeEvent(events[1].id, events[1].starts_at, status="postponed"),
        FakeEvent(events[2].id, events[2].starts_at, status="rescheduled"),
        FakeEvent(events[3].id, events[3].starts_at, onsale=None),
        FakeEvent(events[4].id, events[4].starts_at, onsale="2025-03-01T18:00:00Z"),
    ]
    ingest_with(migrated, FakeDiscovery(events))
    upcoming = table_with(render(migrated), "Public onsale")
    statuses = list(upcoming["Status"])
    assert statuses[:4] == [":orange[**CANCELLED**]", ":orange[**POSTPONED**]",
                            ":orange[**RESCHEDULED**]", "onsale"]
    assert list(upcoming["Public onsale"])[3:5] == ["—", "Mar 1, 2025, 10:00 AM PST"]
    assert all(re.fullmatch(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2} \d{1,2}", d)
               for d in upcoming["Date"]), list(upcoming["Date"])


def test_the_runs_table_says_unique_includes_undated_and_counts_them(
    migrated: Schema, render: Callable[[Schema], AppTest]
) -> None:
    ingest_with(migrated, FakeDiscovery(spread(60, WHOLE.start, WHOLE.end) + undated(2)))
    at = render(migrated)
    runs = table_with(at, "Undated")
    assert "Unique" not in runs.columns
    assert (runs.iloc[0]["Unique (incl. undated)"], runs.iloc[0]["Undated"]) == ("62", "2")
    assert "<0.1 MB" in list(table_with(at, "Table")["Size"])  # small tables, not "0.0 MB"


def test_weeks_are_labelled_categories_with_partial_weeks_marked(
    migrated: Schema, render: Callable[[Schema], AppTest]
) -> None:
    ingest_with(migrated, FakeDiscovery(spread(60, WHOLE.start, WHOLE.end)))
    at = render(migrated)
    charts = at.get("vega_lite_chart")
    assert len(charts) == 1
    spec = json.loads(charts[0].proto.spec)
    assert spec["encoding"]["x"]["type"] == "nominal"  # categories, not a time axis
    assert spec["encoding"]["x"]["sort"] is None  # in week order, not alphabetical
    # Streamlit moves the chart's inline values into an Arrow table beside the spec.
    weeks = convert_arrow_bytes_to_pandas_df(charts[0].proto.data.data).to_dict("records")
    # NOW is Tuesday Oct 6, so the 90 days run Oct 6 to Sunday Jan 3: 13 weeks, the first partial.
    assert [w["Week"] for w in weeks] == [
        "Oct 5 (partial)", "Oct 12", "Oct 19", "Oct 26", "Nov 2", "Nov 9", "Nov 16", "Nov 23",
        "Nov 30", "Dec 7", "Dec 14", "Dec 21", "Dec 28"]
    assert sum(w["Events"] for w in weeks) == 60
    assert ("Later weeks are naturally lower: shows further out are announced later, "
            "and the first week is partial.") in text(at)


def test_shows_that_only_entered_the_window_are_counted_apart_from_newly_listed_ones(
    migrated: Schema, render: Callable[[Schema], AppTest]
) -> None:
    api = FakeDiscovery(spread(30, WHOLE.start, WHOLE.end)
                        + [FakeEvent("evtJAN4", datetime(2027, 1, 5, 4, tzinfo=UTC))])
    ingest_with(migrated, api)
    api.events.append(FakeEvent("evtFRESH", datetime(2026, 10, 21, 3, tzinfo=UTC)))
    with migrated.connect() as conn:
        ingest(conn, client_for(api), now=NOW + timedelta(days=1))  # the window moves a day
    at = render(migrated)
    assert "**Newly listed**" in [m.value for m in at.markdown]
    assert list(table_with(at, "First seen")["Show"]) == ["Synthetic evtFRESH"]
    assert ("Entered the 90-day window: 1 show (Mon Jan 4, 2027), in range only because the "
            "window moved forward.") in text(at)
