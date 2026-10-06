"""Shared fixtures. Integration tests use TEST_DATABASE_URL, each in its own throwaway schema."""

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from dotenv import dotenv_values
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]


def _test_database_url() -> str | None:
    url = os.environ.get("TEST_DATABASE_URL") or dotenv_values(ROOT / ".env").get(
        "TEST_DATABASE_URL"
    )
    return (url or "").strip() or None


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = _test_database_url()
    if url is None:
        # CI sets this so a missing database fails the build instead of skipping silently.
        if os.environ.get("MARQUEE_REQUIRE_DB_TESTS"):
            pytest.fail("TEST_DATABASE_URL is not set, but MARQUEE_REQUIRE_DB_TESTS is")
        pytest.skip("TEST_DATABASE_URL is not set")
    return url


@dataclass(frozen=True)
class Schema:
    url: str
    name: str

    def connect(self) -> psycopg.Connection:
        conn = psycopg.connect(self.url, autocommit=True)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.name)))
        return conn


@pytest.fixture
def schema(test_database_url: str) -> Iterator[Schema]:
    """A fresh, empty schema per test, dropped afterwards, so the test database stays clean."""
    s = Schema(test_database_url, f"marquee_test_{uuid.uuid4().hex[:12]}")
    with psycopg.connect(test_database_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(s.name)))
        try:
            yield s
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(s.name)))
