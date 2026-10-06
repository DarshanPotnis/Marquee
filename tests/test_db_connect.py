"""Connecting must never put the database password into an error message. No network needed."""

import pytest

from marquee.db import DatabaseConnectError, connect


def test_connection_errors_never_show_the_database_password() -> None:
    # libpq quotes the connection string in parse errors, password included. This URL fails while
    # being parsed, before any network I/O.
    with pytest.raises(DatabaseConnectError) as err:
        connect("postgresql://marquee:s3cret-pw@[::1/marquee")
    assert "s3cret-pw" not in str(err.value)
    # The original psycopg error (which holds the URL) must not ride along as the cause.
    assert err.value.__cause__ is None
    assert err.value.__suppress_context__
