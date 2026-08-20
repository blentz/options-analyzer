"""Tests for the Firefox/LibreWolf cookie reader.

The WAL test is the important one. Firefox journals cookies.sqlite in WAL
mode, so while the browser is running, recently-written cookies live in the
cookies.sqlite-wal sidecar and have not been checkpointed into the main
database file. Copying only cookies.sqlite therefore misses exactly the
cookies most likely to matter — the session cookie from a login the user
just performed. The failure is silent: extraction succeeds and returns
stale or empty results, which downstream looks identical to expired auth.
"""

import sqlite3

import pytest

from app.stocknear_cookies import extract_browser_cookies

_SCHEMA = """
CREATE TABLE moz_cookies (
    id INTEGER PRIMARY KEY,
    name TEXT,
    value TEXT,
    host TEXT,
    path TEXT,
    expiry INTEGER,
    isSecure INTEGER,
    isHttpOnly INTEGER,
    sameSite INTEGER
)
"""


def _insert(conn, name, host="stocknear.com"):
    conn.execute(
        "INSERT INTO moz_cookies (name, value, host, path, expiry, isSecure,"
        " isHttpOnly, sameSite) VALUES (?, ?, ?, '/', 4102444800, 1, 1, 1)",
        (name, f"{name}-value", host),
    )


@pytest.fixture
def wal_profile(tmp_path):
    """A profile whose newest cookie is committed but still only in the WAL.

    The connection is deliberately left open for the duration of the test.
    Closing it would checkpoint the WAL into the main file and destroy the
    very condition under test — which is also why this reproduces a running
    browser rather than a closed one.
    """
    conn = sqlite3.connect(tmp_path / "cookies.sqlite")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
    _insert(conn, "old_cookie")
    conn.commit()
    # Force the schema + first row into the main db file, so the only thing
    # the WAL holds is the cookie written below.
    conn.execute("PRAGMA wal_checkpoint(FULL)")

    _insert(conn, "pb_auth")
    conn.commit()

    yield tmp_path
    conn.close()


def test_reads_cookie_still_in_wal(wal_profile):
    names = {c["name"] for c in extract_browser_cookies(str(wal_profile), "stocknear.com")}
    assert "pb_auth" in names, (
        "cookie committed to the WAL was not seen — the -wal sidecar is not "
        "being copied alongside cookies.sqlite"
    )


def test_still_reads_checkpointed_cookies(wal_profile):
    names = {c["name"] for c in extract_browser_cookies(str(wal_profile), "stocknear.com")}
    assert "old_cookie" in names


def test_filters_by_domain(wal_profile):
    conn = sqlite3.connect(wal_profile / "cookies.sqlite")
    _insert(conn, "unrelated", host="example.com")
    conn.commit()
    conn.close()

    names = {c["name"] for c in extract_browser_cookies(str(wal_profile), "stocknear.com")}
    assert "unrelated" not in names


def test_missing_profile_returns_empty(tmp_path):
    assert extract_browser_cookies(str(tmp_path / "nope"), "stocknear.com") == []


def test_source_profile_is_not_modified(wal_profile):
    """The profile is mounted read-only; extraction must never write to it."""
    before = {p.name: p.stat().st_mtime_ns for p in wal_profile.iterdir()}
    extract_browser_cookies(str(wal_profile), "stocknear.com")
    after = {p.name: p.stat().st_mtime_ns for p in wal_profile.iterdir()}
    assert before == after
