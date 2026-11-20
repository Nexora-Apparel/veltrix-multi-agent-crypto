import json
import sqlite3
from types import SimpleNamespace

from veltrix.runtime_telemetry import RuntimeTelemetryStore


def test_runtime_telemetry_records_agent_votes_and_provider_probes(tmp_path):
    path = str(tmp_path / "runtime.db")
    store = RuntimeTelemetryStore(path)
    store.record_agent_votes(
        [SimpleNamespace(agent="scout", stance="SUPPORT"),
         SimpleNamespace(agent="whale", stance="ABSTAIN"),
         SimpleNamespace(agent="not-an-agent", stance="SUPPORT")],
        observed_at_ms=1_700_000_000_000,
    )
    store.record_provider_probe(
        "DexScreener", "base", succeeded=True,
        observed_at_ms=1_700_000_001_000, latency_ms=42,
    )
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT event,payload FROM events ORDER BY id").fetchall()
    assert len(rows) == 3
    agents = [json.loads(payload) for event, payload in rows if event == "AGENT_HEARTBEAT"]
    assert [row["agent"] for row in agents] == ["scout", "whale"]
    assert all(row["status"] == "SUCCESS" for row in agents)
    provider = json.loads(next(payload for event, payload in rows if event == "PROVIDER_PROBE"))
    assert provider["provider"] == "dexscreener"
    assert provider["chain"] == "base"
    assert provider["status"] == "SUCCESS"
    assert provider["latency_ms"] == 42


def test_runtime_telemetry_records_failures_without_exception_text(tmp_path):
    path = str(tmp_path / "runtime.db")
    store = RuntimeTelemetryStore(path)
    store.record_agent_failure(
        observed_at_ms=1_700_000_000_000,
        error_code="RuntimeError",
    )
    store.record_provider_probe(
        "helius", "solana", succeeded=False,
        observed_at_ms=1_700_000_001_000, latency_ms=100,
        error_code="ProviderError",
    )
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT event,payload FROM events ORDER BY id").fetchall()
    failures = [json.loads(payload) for event, payload in rows if event == "AGENT_HEARTBEAT"]
    assert len(failures) == 4
    assert all(row["status"] == "FAILED" for row in failures)
    assert all(row["error_code"] == "RuntimeError" for row in failures)
    provider = json.loads(rows[-1][1])
    assert provider["status"] == "FAILED"
    assert provider["error_code"] == "ProviderError"


def test_runtime_telemetry_records_worker_cycles_separately_from_agent_evaluations(tmp_path):
    path = str(tmp_path / "runtime.db")
    store = RuntimeTelemetryStore(path)
    store.record_worker_heartbeat(
        "solana-consensus", observed_at_ms=1_700_000_002_000,
        status="DEGRADED", error_code="CANDIDATE_EVALUATION_FAILURE",
        metrics={"processed": 4, "failed": 1},
    )
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT event,payload FROM events ORDER BY id").fetchall()
    assert len(rows) == 1
    event, payload = rows[0]
    record = json.loads(payload)
    assert event == "WORKER_HEARTBEAT"
    assert record["worker"] == "solana-consensus"
    assert record["status"] == "DEGRADED"
    assert record["metrics"] == {"failed": 1, "processed": 4}
    assert record["error_code"] == "CANDIDATE_EVALUATION_FAILURE"
    assert store.record_agent_votes([], observed_at_ms=1_700_000_003_000) is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE event='AGENT_HEARTBEAT'").fetchone()[0] == 0
