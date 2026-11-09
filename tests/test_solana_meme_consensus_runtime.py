from types import SimpleNamespace

import pytest

from veltrix.solana_meme.consensus_runtime import build_runtime

WALLET_A = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def test_consensus_runtime_uses_shared_database_and_allowlist(tmp_path):
    runtime = build_runtime(
        database_path=tmp_path / "runtime.sqlite3",
        poll_interval_seconds=10,
        refresh_interval_ms=120_000,
        batch_size=3,
        trusted_wallets=(WALLET_A,),
    )
    assert runtime.poll_interval_seconds == 10
    assert runtime.batch_size == 3
    assert runtime.worker.refresh_interval_ms == 120_000
    assert runtime.worker.engine.whale_aggregator.trusted_wallets == frozenset({WALLET_A})


def test_consensus_runtime_reads_allowlist_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("VELTRIX_TRUSTED_SOLANA_WALLETS", WALLET_A)
    runtime = build_runtime(database_path=tmp_path / "env.sqlite3")
    assert runtime.worker.engine.whale_aggregator.trusted_wallets == frozenset({WALLET_A})


def test_consensus_runtime_defaults_to_no_trusted_wallets(tmp_path, monkeypatch):
    monkeypatch.delenv("VELTRIX_TRUSTED_SOLANA_WALLETS", raising=False)
    runtime = build_runtime(database_path=tmp_path / "empty.sqlite3")
    assert runtime.worker.engine.whale_aggregator.trusted_wallets == frozenset()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"poll_interval_seconds": 4},
        {"poll_interval_seconds": 3601},
        {"batch_size": 0},
        {"batch_size": 26},
        {"refresh_interval_ms": 0},
        {"refresh_interval_ms": 1.5},
        {"batch_size": 1.5},
        {"trusted_wallets": ("not-a-wallet",)},
    ],
)
def test_consensus_runtime_rejects_invalid_configuration(tmp_path, kwargs):
    with pytest.raises(ValueError):
        build_runtime(
            database_path=tmp_path / "invalid.sqlite3",
            **kwargs,
        )

def test_consensus_runtime_wires_helius_only_with_explicit_trust_and_wallet_allowlist(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HELIUS_API_KEY", "test-helius-key")
    monkeypatch.setenv("TRUSTED_WALLET_PROVIDERS", "helius")
    monkeypatch.setenv("VELTRIX_HELIUS_WALLET_SCAN_LIMIT", "5")
    monkeypatch.setenv("WALLET_EVIDENCE_MAX_AGE_MS", "180000")

    runtime = build_runtime(
        database_path=tmp_path / "helius.sqlite3",
        trusted_wallets=(WALLET_A, "Vote111111111111111111111111111111111111111"),
    )

    source = runtime.worker.wallet_event_provider
    assert source is not None
    assert source.provider.provider_id == "helius"
    assert source.provider.tracked_wallets == (
        WALLET_A, "Vote111111111111111111111111111111111111111"
    )
    assert source.max_age_ms == 180_000


def test_consensus_runtime_does_not_enable_helius_without_provider_trust(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HELIUS_API_KEY", "test-helius-key")
    monkeypatch.setenv("TRUSTED_WALLET_PROVIDERS", "")
    runtime = build_runtime(
        database_path=tmp_path / "untrusted-helius.sqlite3",
        trusted_wallets=(WALLET_A,),
    )
    assert runtime.worker.wallet_event_provider is None


def test_consensus_runtime_rejects_allowlist_above_helius_scan_limit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HELIUS_API_KEY", "test-helius-key")
    monkeypatch.setenv("TRUSTED_WALLET_PROVIDERS", "helius")
    monkeypatch.setenv("VELTRIX_HELIUS_WALLET_SCAN_LIMIT", "2")
    with pytest.raises(ValueError, match="exceeds the configured Helius scan limit"):
        build_runtime(
            database_path=tmp_path / "too-many-wallets.sqlite3",
            trusted_wallets=(
                WALLET_A,
                "Vote111111111111111111111111111111111111111",
                "11111111111111111111111111111111",
            ),
        )
