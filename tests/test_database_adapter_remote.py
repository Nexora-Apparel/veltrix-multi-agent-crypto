from __future__ import annotations

import sys
import types

from veltrix.database import connect_database, row_factory_for


def test_remote_connection_uses_explicit_token_and_driver(monkeypatch):
    calls = []
    sentinel = object()
    row_type = type("RemoteRow", (), {})

    def connect(url, *, auth_token, isolation_level):
        calls.append((url, auth_token, isolation_level))
        return sentinel

    fake_driver = types.ModuleType("turso_serverless")
    fake_driver.connect = connect
    fake_driver.Row = row_type
    monkeypatch.setitem(sys.modules, "turso_serverless", fake_driver)
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "test-only-token")

    url = "libsql://veltrix-h0-conformance.turso.io"
    assert connect_database(url) is sentinel
    assert calls == [(url, "test-only-token", "DEFERRED")]
    assert row_factory_for(url) is row_type


def test_remote_database_requires_exact_explicit_turso_endpoint(monkeypatch):
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "test-only-token")
    from veltrix.database import connect_database

    import pytest

    with pytest.raises(ValueError, match="approved Turso endpoint"):
        connect_database("https://example.invalid/database")
