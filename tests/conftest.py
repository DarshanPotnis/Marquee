"""Shared fixtures. Integration tests use MARQUEE_TEST_DATABASE_URL, one throwaway schema each."""

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from marquee.config import load_test_database_url, read_environment
from marquee.db import connect as direct_connect

ROOT = Path(__file__).resolve().parents[1]


def _test_database_url() -> str | None:
    # Same rules as the app: a shell/.env clash, a pooled host or the production DB all fail.
    # Also reads the production URL, only to refuse a test URL that points at the same database.
    names = ["MARQUEE_TEST_DATABASE_URL", "MARQUEE_DATABASE_URL"]
    env = read_environment(ROOT / ".env", names=names)
    return load_test_database_url(env)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = _test_database_url()
    if url is None:
        # CI sets this so a missing database fails the build instead of skipping silently.
        if os.environ.get("MARQUEE_REQUIRE_DB_TESTS"):
            pytest.fail("MARQUEE_TEST_DATABASE_URL is not set, but MARQUEE_REQUIRE_DB_TESTS is")
        pytest.skip("MARQUEE_TEST_DATABASE_URL is not set")
    return url


@dataclass(frozen=True)
class Schema:
    # repr=False: pytest prints fixture values when a test fails, and the URL holds a password.
    url: str = field(repr=False)
    name: str = ""

    def connect(self) -> psycopg.Connection:
        conn = direct_connect(self.url)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.name)))
        return conn


@pytest.fixture
def schema(test_database_url: str) -> Iterator[Schema]:
    """A fresh, empty schema per test, dropped afterwards, so the test database stays clean."""
    s = Schema(test_database_url, f"marquee_test_{uuid.uuid4().hex[:12]}")
    with direct_connect(test_database_url) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(s.name)))
        try:
            yield s
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(s.name)))
