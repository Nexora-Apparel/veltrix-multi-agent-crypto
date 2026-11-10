from __future__ import annotations

import sqlite3

import pytest

from veltrix.database import (
    connect_database,
    database_backend,
    ensure_database_parent,
    is_remote_database,
    row_factory_for,
)


def test_local_sqlite_remains_the_default(tmp_path):
    path = tmp_path / "nested" / "paper.sqlite3"
    ensure_database_parent(path)
    connection = connect_database(path)
    try:
        connection.row_factory = row_factory_for(path)
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO sample(value) VALUES (?)", ("local",))
        connection.commit()
        row = connection.execute("SELECT value FROM sample").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["value"] == "local"
    finally:
        connection.close()
    assert database_backend(path) == "sqlite"


def test_remote_url_is_not_treated_as_a_filesystem_path():
    url = "libsql://veltrix-h0-conformance-eu.turso.io"
    assert is_remote_database(url)
    assert database_backend(url) == "turso"


def test_remote_connection_fails_closed_without_token(monkeypatch):
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TURSO_AUTH_TOKEN is missing"):
        connect_database("libsql://veltrix-h0-conformance.turso.io")


def test_remote_url_does_not_create_local_directories(tmp_path):
    url = "libsql://veltrix-h0-conformance.turso.io"
    ensure_database_parent(url)
    assert list(tmp_path.iterdir()) == []


def test_remote_sqlite_uri_mode_is_rejected():
    with pytest.raises(ValueError, match="URI mode"):
        connect_database("libsql://veltrix-h0-conformance.turso.io", uri=True)


def test_plain_http_remote_url_is_rejected():
    with pytest.raises(ValueError, match="approved Turso endpoint"):
        connect_database("http://example.invalid/database")
