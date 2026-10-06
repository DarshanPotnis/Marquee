"""A failing test must never print a database password: pytest shows fixture values on failure."""

import inspect

import pytest
from conftest import Schema


def test_a_failing_test_does_not_print_the_database_url(pytester: pytest.Pytester) -> None:
    # The real Schema class from tests/conftest.py, so this follows any change to it. The fake
    # password is built from two halves, so it can only appear in the output through a repr.
    pytester.makeconftest(
        "from dataclasses import dataclass, field\nimport psycopg\nimport pytest\n"
        "from psycopg import sql\n\n"
        + inspect.getsource(Schema)
        + "\n@pytest.fixture\ndef schema():\n"
          "    return Schema('postgresql://u:' + 's3cret' + '-pw@h.example/db', 'marquee_test_x')\n"
    )
    pytester.makepyfile("def test_fails(schema):\n    assert schema.name == 'something else'\n")
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    assert "marquee_test_x" in result.stdout.str()  # the fixture really was printed...
    assert "s3cret" + "-pw" not in result.stdout.str()  # ...without its URL
