"""The Streamlit app, run headless with Streamlit's own AppTest, against a throwaway schema.

The app reads its clock through queries.utc_now, so these tests pin "now" to the day their data
describes and don't depend on the date they run.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import streamlit as st
from conftest import Schema
from fake_discovery import FakeDiscovery, FakeEvent, client_for, spread, undated
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
    return "\n".join(df.value.to_string() for df in at.dataframe)


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
    assert "No new shows in the last 24 hours." in t
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
    assert "WARNING" in text(at)
    assert "0 failed · 1 warning" in text(at)
    assert "WARNING" in table_text(at)  # the checks table carries the word too


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
