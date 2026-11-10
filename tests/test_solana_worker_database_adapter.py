from __future__ import annotations

import importlib
import sqlite3

import pytest


@pytest.mark.parametrize(
    ("module_name", "worker_name"),
    [
        ("veltrix.solana_meme.enrichment", "CandidateEnrichmentWorker"),
        ("veltrix.solana_meme.market_refresh", "MarketRefreshWorker"),
        ("veltrix.solana_meme.risk_worker", "SolanaRiskEvidenceWorker"),
        ("veltrix.solana_meme.consensus_worker", "ConsensusWorker"),
    ],
)
def test_worker_connection_uses_shared_database_adapter(
    monkeypatch, module_name: str, worker_name: str
) -> None:
    module = importlib.import_module(module_name)
    calls: list[tuple[str, float]] = []
    database_url = "libsql://worker-adapter-test.turso.io"

    def connect_database(database: str, *, timeout: float):
        calls.append((database, timeout))
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(module, "connect_database", connect_database)
    monkeypatch.setattr(module, "row_factory_for", lambda database: sqlite3.Row)

    worker = object.__new__(getattr(module, worker_name))
    worker.database_path = database_url

    with worker._connect() as connection:
        row = connection.execute("SELECT 7 AS value").fetchone()
        assert row["value"] == 7

    assert calls == [(database_url, 10)]
