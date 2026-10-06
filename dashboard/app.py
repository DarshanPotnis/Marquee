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
from marquee.windows import RANGE_DAYS

ACCENT = "#2563EB"  # the one accent colour; also set in .streamlit/config.toml
CACHE_SECONDS = 60
DEFAULT_DAYS = 14
UPCOMING_ROWS = 15  # beyond this many rows the upcoming table scrolls instead of growing
UPCOMING_HEIGHT = 560
NEON_FREE_LIMIT = 1024 ** 3  # used when the server doesn't report neon.max_cluster_size
_COLOUR = {"fresh": "blue", "late": "orange", "stale": "red", "partial": "orange",
           "failed": "red", "error": "red", "warning": "orange"}


def coloured(kind: str, word: str) -> str:
    """Markdown for a status word in its colour. The word itself always carries the meaning."""
    colour = _COLOUR.get(kind)
    return f":{colour}[**{word}**]" if colour else f"**{word}**"


def today(now: datetime) -> date:
    return now.astimezone(present.LA).date()


def last_day(first: date) -> date:
    return first + timedelta(days=RANGE_DAYS - 1)  # the range counts today as day 1


def status_cell(status: str | None) -> str:
    """Markdown for st.table: cancelled, postponed and rescheduled shout in amber."""
    word = present.event_status(status)
    if present.is_problem_status(status):
        return coloured("warning", word)
    return present.md_escape(word)  # the status comes from the API too


def result_cell(passed: bool, severity: str) -> str:
    word = present.result_word(passed, severity)
    return word if passed else coloured(severity, word)


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def overview() -> dict[str, Any]:
    settings = config.settings_from_environment()
    now = queries.utc_now()
    day = today(now)
    last = last_day(day)
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
            "weeks": queries.events_per_week(conn, day, weeks=present.weeks_spanned(day, last)),
            "runs": queries.recent_runs(conn, 24),
            "storage": queries.storage(conn),
            "options": queries.filter_options(conn, day, last),
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
    # st.table, not st.dataframe: its cells are Markdown, so a result can carry its colour.
    # The code name is grey because :small alone is the table's own size, and `code` is green.
    st.table([{"Result": result_cell(r.passed, r.severity),
               "Check": f"{present.check_title(r.name)}  \n"
                        f":gray[:small[{present.md_escape(r.name)}]]",
               "If it fails": present.if_it_fails(r.severity),
               "Detail": present.md_escape(r.detail or "")} for r in rows], hide_index=True)


def changes_panel(d: dict[str, Any]) -> None:
    st.subheader("Changes in the last 24 hours")
    day = today(d["now"])
    listed = [s for s in d["new_shows"] if not s.entered]
    st.markdown("**Newly listed**")
    if not listed:
        st.info("No newly listed shows in the last 24 hours.")
    else:
        st.dataframe([{"First seen": present.la_time(s.first_seen_at), "Show": s.show,
                       "Venue": s.venue or "", "Date": present.event_date(s.event_date, day),
                       "Time": present.event_time(s.event_time)} for s in listed],
                     hide_index=True, width="stretch")
    # Every LA midnight a new day joins the range; its shows are first seen, not newly listed.
    st.caption(present.entered_caption(
        [s.event_date for s in d["new_shows"] if s.entered and s.event_date], day))
    st.markdown("**Changes**")
    if not d["changes"]:
        st.info("No changes in the last 24 hours.")
    else:
        st.dataframe([{"When": present.la_time(c.detected_at), "Show": c.show,
                       "Venue": c.venue or "",
                       "Event date": present.event_date(c.event_date, day),
                       "What": present.field_label(c.field),
                       "Change": present.change_text(c.field, c.old, c.new)}
                      for c in d["changes"]], hide_index=True, width="stretch")


def onsales_panel(d: dict[str, Any]) -> None:
    st.subheader("Public onsales in the next 7 days")
    if not d["onsales"]:
        st.info("No public onsales in the next 7 days.")
        return
    day = today(d["now"])
    st.dataframe([{"Onsale": present.la_time(o.onsale_at, day), "Show": o.show,
                   "Venue": o.venue or "", "Event date": present.event_date(o.event_date, day)}
                  for o in d["onsales"]], hide_index=True, width="stretch")


def upcoming_panel(d: dict[str, Any]) -> None:
    st.subheader("Upcoming events")
    first = today(d["now"])
    venue_options, status_options = d["options"]
    dates_col, venue_col, status_col = st.columns([2, 3, 2])
    picked = dates_col.date_input("Dates", value=(first, first + timedelta(days=DEFAULT_DAYS)),
                                  min_value=first,
                                  max_value=last_day(first),
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
    # st.table so a problem status can be amber; every value from the API is escaped, because
    # real names carry Markdown ("Nice as F**k", "[THE X : NEXUS]").
    st.table([{"Date": present.event_date(e.event_date, first),
               "Time": present.event_time(e.event_time), "Show": present.md_escape(e.show),
               "Venue": present.md_escape(e.venue or ""), "City": present.md_escape(e.city or ""),
               "Status": status_cell(e.status), "Public onsale": present.onsale(e.onsale_at, first)}
              for e in rows], hide_index=True,
             height="content" if len(rows) <= UPCOMING_ROWS else UPCOMING_HEIGHT)


def weekly_panel(d: dict[str, Any]) -> None:
    st.subheader("Listed events per week")
    weeks = d["weeks"]
    if sum(n for _, n in weeks) == 0:
        st.info("No listed events to chart yet.")
        return
    first = today(d["now"])
    last = last_day(first)
    st.vega_lite_chart(weekly_spec([
        {"Week": present.week_label(w, first, last), "Events": n,
         "partial": present.partial_week(w, first, last)} for w, n in weeks]), width="stretch")
    st.caption(present.weekly_caption(first, last))


def weekly_spec(values: list[dict[str, Any]]) -> dict[str, Any]:
    """Weeks as labelled categories in week order (sort None), so bars get a band's width; a
    partial week is fainter, and its label says "(partial)" so the colour is never alone."""
    return {
        "data": {"values": values},
        "mark": {"type": "bar", "color": ACCENT, "cornerRadiusEnd": 4, "width": {"band": 0.6}},
        "encoding": {
            "x": {"field": "Week", "type": "nominal", "sort": None,
                  "title": "Week starting (Monday)", "axis": {"labelAngle": 0}},
            "y": {"field": "Events", "type": "quantitative", "title": "Listed events"},
            "opacity": {"condition": {"test": "datum.partial", "value": 0.45}, "value": 1},
            "tooltip": [{"field": "Week", "title": "Week starting"},
                        {"field": "Events", "title": "Listed events"}],
        },
    }


def runs_panel(d: dict[str, Any]) -> None:
    st.subheader("Last 24 runs")
    if not d["runs"]:
        st.info("No runs yet. Run `python -m marquee ingest`.")
        return
    st.dataframe([{"Run": r.run_id, "Started": present.la_time(r.started_at),
                   "Status": present.run_status_word(r.status), "Calls": r.api_calls,
                   "Reported": present.number(r.reported_total),
                   "Fetched": present.number(r.fetched_total),
                   "Unique (incl. undated)": present.number(r.unique_events),
                   "Undated": present.number(r.undated_events), "Changes": r.changes,
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
    st.header("Market")
    changes_panel(d)
    onsales_panel(d)
    upcoming_panel(d)
    weekly_panel(d)
    st.header("Pipeline health")
    checks_panel(d)
    runs_panel(d)
    storage_panel(d)
    st.caption("Read-only · refreshed at most every 60 s · times in Los Angeles (PT)")


main()
