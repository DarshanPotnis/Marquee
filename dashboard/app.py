"""Marquee dashboard: is the data fresh, complete and trustworthy right now?

Layout only. Every SQL statement lives in marquee.queries (read-only connections, 5 s timeout);
every display string comes from marquee.present. Results are cached for 60 s.
Never colour alone: red and amber always come with a word (FAILED, WARNING, PARTIAL, LATE, STALE).

Run: uv run streamlit run dashboard/app.py
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import psycopg
import streamlit as st

from marquee import config, present, queries
from marquee.db import DatabaseConnectError

ACCENT = "#2563EB"  # the one accent colour; also set in .streamlit/config.toml
CACHE_SECONDS = 60
DEFAULT_DAYS = 14
RANGE_DAYS = 90
NEON_FREE_LIMIT = 1024 ** 3  # used when the server doesn't report neon.max_cluster_size
_COLOUR = {"fresh": "blue", "late": "orange", "stale": "red", "partial": "orange",
           "failed": "red", "error": "red", "warning": "orange"}


def coloured(kind: str, word: str) -> str:
    """Markdown for a status word in its colour. The word itself always carries the meaning."""
    colour = _COLOUR.get(kind)
    return f":{colour}[**{word}**]" if colour else f"**{word}**"


def today(now: datetime) -> date:
    return now.astimezone(present.LA).date()


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def overview() -> dict[str, Any]:
    settings = config.settings_from_environment()
    now = queries.utc_now()
    day = today(now)
    with queries.connect_readonly(settings.database_url) as conn:
        return {
            "now": now,
            "interval": settings.schedule_interval_minutes,
            "latest": queries.latest_run(conn),
            "last_success": queries.last_success_at(conn),
            "completeness": queries.completeness(conn),
            "checks": queries.latest_checks(conn),
            "changes": queries.recent_changes(conn, now - timedelta(days=1)),
            "new_shows": queries.new_shows(conn, now - timedelta(days=1)),
            "onsales": queries.onsales_between(conn, now, now + timedelta(days=7)),
            "weeks": queries.events_per_week(conn, day, weeks=14),
            "runs": queries.recent_runs(conn, 24),
            "storage": queries.storage(conn),
            "options": queries.filter_options(conn, day, day + timedelta(days=RANGE_DAYS)),
        }


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def upcoming(start: date, end: date, venues: tuple[str, ...],
             statuses: tuple[str, ...]) -> tuple[list[queries.EventRow], int]:
    settings = config.settings_from_environment()
    with queries.connect_readonly(settings.database_url) as conn:
        return queries.upcoming_events(conn, start, end, venues, statuses)


def headline(d: dict[str, Any]) -> None:
    now, latest, last = d["now"], d["latest"], d["last_success"]
    fresh = present.freshness(last, now, interval_minutes=d["interval"])
    left, middle, right = st.columns(3)
    with left:
        st.markdown("**Freshness**")
        if last is None:
            st.markdown(coloured("none", fresh.word))
            st.info("No runs yet. Run `python -m marquee ingest`.")
        else:
            st.markdown(f"{coloured(fresh.level, fresh.word)} · last good run "
                        f"{present.ago(last, now)}")
            st.caption(present.la_time(last))
    with middle:
        st.markdown("**Completeness**")
        c = d["completeness"]
        if c is None:
            st.caption("No completed run with a completeness check yet.")
        else:
            st.markdown(f"**{present.completeness_sentence(c.reported, c.received)}**")
            st.caption(f"+{present.number(c.undated)} undated (TBA/TBD) · run {c.run_id} · "
                       f"{present.la_time(c.finished_at)}")
        if latest is not None and latest.status in ("partial", "failed"):
            st.markdown(f"{coloured(latest.status, present.run_status_word(latest.status))} "
                        f"latest run {latest.run_id}: {latest.error or 'no error recorded'}")
    with right:
        st.markdown("**Checks**")
        found = d["checks"]
        if found is None:
            st.caption("No checks yet.")
        else:
            run_id, rows = found
            failed = sum(1 for r in rows if not r.passed and r.severity == "error")
            warnings = sum(1 for r in rows if not r.passed and r.severity == "warning")
            kind = "error" if failed else ("warning" if warnings else "none")
            st.markdown(coloured(kind, present.checks_summary(failed, warnings)))
            st.caption(f"run {run_id}")


def checks_panel(d: dict[str, Any]) -> None:
    st.subheader("Checks")
    found = d["checks"]
    if found is None:
        st.info("No checks yet.")
        return
    run_id, rows = found
    st.caption(f"Run {run_id}, problems first")
    for r in rows:
        if not r.passed:
            st.markdown(f"{coloured(r.severity, present.result_word(r.passed, r.severity))} "
                        f"`{r.name}`: {r.detail or ''}")
    st.dataframe([{"Result": present.result_word(r.passed, r.severity), "Check": r.name,
                   "Severity": r.severity, "Detail": r.detail or ""} for r in rows],
                 hide_index=True, width="stretch")


def changes_panel(d: dict[str, Any]) -> None:
    st.subheader("Changes in the last 24 hours")
    st.markdown("**New shows**")
    if not d["new_shows"]:
        st.info("No new shows in the last 24 hours.")
    else:
        st.dataframe([{"First seen": present.la_time(s.first_seen_at), "Show": s.show,
                       "Venue": s.venue or "", "Date": present.la_date(s.event_date),
                       "Time": present.event_time(s.event_time)} for s in d["new_shows"]],
                     hide_index=True, width="stretch")
    st.markdown("**Changes**")
    if not d["changes"]:
        st.info("No changes in the last 24 hours.")
    else:
        st.dataframe([{"When": present.la_time(c.detected_at), "Show": c.show,
                       "Venue": c.venue or "", "Event date": present.la_date(c.event_date),
                       "What": present.field_label(c.field),
                       "Change": present.change_text(c.field, c.old, c.new)}
                      for c in d["changes"]], hide_index=True, width="stretch")


def onsales_panel(d: dict[str, Any]) -> None:
    st.subheader("Public onsales in the next 7 days")
    if not d["onsales"]:
        st.info("No public onsales in the next 7 days.")
        return
    st.dataframe([{"Onsale": present.la_time(o.onsale_at), "Show": o.show,
                   "Venue": o.venue or "", "Event date": present.la_date(o.event_date)}
                  for o in d["onsales"]], hide_index=True, width="stretch")


def upcoming_panel(d: dict[str, Any]) -> None:
    st.subheader("Upcoming events")
    first = today(d["now"])
    venue_options, status_options = d["options"]
    dates_col, venue_col, status_col = st.columns([2, 3, 2])
    picked = dates_col.date_input("Dates", value=(first, first + timedelta(days=DEFAULT_DAYS)),
                                  min_value=first,
                                  max_value=first + timedelta(days=RANGE_DAYS),
                                  format="YYYY-MM-DD")
    venues = venue_col.multiselect("Venue", venue_options)
    statuses = status_col.multiselect("Status", status_options)
    chosen = picked if isinstance(picked, tuple) else (picked,)
    start = chosen[0] if chosen else first
    end = chosen[-1] if chosen else start
    rows, total = upcoming(start, end, tuple(venues), tuple(statuses))
    if not rows:
        st.info("No events match these filters.")
        return
    st.caption(f"Showing {present.number(len(rows))} of {present.number(total)} events")
    st.dataframe([{"Date": present.la_date(e.event_date), "Time": present.event_time(e.event_time),
                   "Show": e.show, "Venue": e.venue or "", "City": e.city or "",
                   "Status": e.status or "",
                   "Public onsale": present.la_time(e.onsale_at) if e.onsale_at else ""}
                  for e in rows], hide_index=True, width="stretch")


def weekly_panel(d: dict[str, Any]) -> None:
    st.subheader("Listed events per week")
    weeks = d["weeks"]
    if sum(n for _, n in weeks) == 0:
        st.info("No listed events to chart yet.")
        return
    st.bar_chart({"Week starting": [w for w, _ in weeks], "Events": [n for _, n in weeks]},
                 x="Week starting", y="Events", color=ACCENT, width="stretch")


def runs_panel(d: dict[str, Any]) -> None:
    st.subheader("Last 24 runs")
    if not d["runs"]:
        st.info("No runs yet. Run `python -m marquee ingest`.")
        return
    st.dataframe([{"Run": r.run_id, "Started": present.la_time(r.started_at),
                   "Status": present.run_status_word(r.status), "Calls": r.api_calls,
                   "Reported": present.number(r.reported_total),
                   "Fetched": present.number(r.fetched_total),
                   "Unique": present.number(r.unique_events), "Changes": r.changes,
                   "Failed checks": present.number(r.checks_failed),
                   "Raw pruned": present.number(r.pruned_raw), "Error": r.error or ""}
                  for r in d["runs"]], hide_index=True, width="stretch")


def storage_panel(d: dict[str, Any]) -> None:
    st.subheader("Database size")
    s = d["storage"]
    limit = s.limit_bytes or NEON_FREE_LIMIT
    share = s.database_bytes / limit
    st.progress(min(share, 1.0))
    st.caption(f"{present.megabytes(s.database_bytes)} of {present.megabytes(limit)} "
               f"({share:.1%})"
               + ("" if s.limit_bytes else " · limit assumed: Neon free plan, 1 GB"))
    if share >= 0.8:
        st.markdown(f"{coloured('warning', 'WARNING')} the database is over 80% of its limit")
    st.dataframe([{"Table": name, "Size": present.megabytes(size)} for name, size in s.tables],
                 hide_index=True, width="stretch")


def main() -> None:
    st.set_page_config(page_title="Marquee", layout="wide")
    st.title("Marquee")
    st.caption(present.SCOPE_LABEL)
    try:
        d = overview()
    except (config.ConfigError, DatabaseConnectError, psycopg.Error) as exc:
        # One calm line, never a traceback. Connection errors are already free of the password.
        st.error(f"Can't read the database right now ({type(exc).__name__}). The dashboard is "
                 "read-only and changed nothing.")
        st.stop()
    headline(d)
    checks_panel(d)
    changes_panel(d)
    onsales_panel(d)
    upcoming_panel(d)
    weekly_panel(d)
    runs_panel(d)
    storage_panel(d)
    st.caption("Read-only · refreshed at most every 60 s · times in Los Angeles (PT)")


main()
