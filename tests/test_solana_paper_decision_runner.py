import time

from veltrix.core import Core
from veltrix.ledger import PaperBroker
from veltrix.safety import SafetyReport
from veltrix.developer_concentration import DeveloperConcentrationReport
from veltrix.launch_bundles import LaunchBundleReport
from veltrix.solana_meme.bundle_store import LaunchBundleEvidenceStore
from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.developer_store import DeveloperConcentrationStore
from veltrix.solana_meme.decision_runner import (
    SolanaAgentContext, SolanaPaperDecisionRunner,
)
from veltrix.solana_meme.launch_stream import LaunchStore
from veltrix.solana_meme.market_data import SolanaMarketSnapshot
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceReport
from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore
from veltrix.solana_meme.safety_store import SolanaSafetyReportStore
from veltrix.solana_meme.sellability import SolanaSellabilityEvidence
from veltrix.solana_meme.sellability_store import SolanaSellabilityEvidenceStore

MINT = "So11111111111111111111111111111111111111112"


def stores(path):
    return (
        LaunchStore(path),
        MarketSnapshotStore(path),
        SolanaRiskEvidenceStore(path),
        DeveloperConcentrationStore(path),
        LaunchBundleEvidenceStore(path),
        SolanaSellabilityEvidenceStore(path),
        SolanaSafetyReportStore(path),
    )


def seed_complete_packet(path):
    now = int(time.time() * 1000)
    launches, markets, risks, developers, bundles, routes, safety = stores(path)
    launches.record_candidate(normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="pumpportal-new-token", observed_at_ms=now,
    ))
    markets.record(SolanaMarketSnapshot(
        mint=MINT, selected_pair_address="pair-1", symbol="TEST",
        price_usd=1.0, selected_pool_liquidity_usd=100_000,
        selected_pool_volume_h1_usd=20_000, price_change_m5_pct=2.0,
        pair_count=1, retrieved_at_ms=now, provider_observed_at_ms=now,
        pair_created_at_ms=now - 60_000, quality_flags=(),
    ))
    risks.record(SolanaRiskEvidenceReport(
        mint=MINT, observed_at_ms=now, goplus_provider="goplus",
        goplus_evidence_id="goplus:1", goplus_observed_at_ms=now,
        liquidity_locked=True, holder_provider="holder-rpc",
        holder_evidence_id="holders:1", holder_block_ref="finalized:1",
        holder_count=2, total_supply_raw=1_000_000_000,
        observed_holder_balance_raw=1_000_000_000, holder_coverage_complete=True,
        top_holder_pct=2.0, top5_holder_pct=5.0, top10_holder_pct=10.0,
        missing_claims=("executable_sellability",), errors=(), mint_evidence_id="mint:1",
        mint_finalized_slot=1,
        token_program_id="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        mint_authority=None, freeze_authority=None,
        token_2022_extension_types=(), token_2022_extension_review_complete=True,
        mint_supply_raw=1_000_000_000, mint_decimals=6,
        mint_supply_reconciled=True,
    ))
    developers.record(DeveloperConcentrationReport(
        chain="solana", token_address=MINT, provider="certified-dev-provider",
        linked_wallets=(), linked_supply_raw=0, total_supply_raw=1_000_000_000,
        dev_hold_pct=0.0, coverage_complete=True, observed_at_ms=now,
        evidence_id="developer:1", source_ref="https://evidence.example/dev",
        method="complete-root-ancestry", independence_group="developer",
        holder_provider="holder-rpc", funding_provider="funding-graph",
        root_provider="signed-root", reasons=(),
    ))
    bundles.record(LaunchBundleReport(
        chain="solana", token_address=MINT, provider="helius-solana-launch-index",
        launch_at_ms=now - 60_000, window_end_ms=now + 60_000,
        coverage_complete=True, bundled_supply_pct=0.0,
        shared_funder_clusters=0, participating_wallets=0,
        accepted_transactions=10, rejected=(), notes=(),
        evidence_id="bundle:1", observed_at_ms=now,
        scanned_through_ms=now, supply_source="finalized-mint",
        supply_block_ref="slot:1", supply_observed_at_ms=now,
        pages_scanned=1, funder_providers=("helius-solana-funding-graph",),
        provenance=("signature:1",),
    ))
    routes.record(SolanaSellabilityEvidence(
        mint=MINT, observed_at_ms=now, provider="jupiter-v2",
        evidence_id="route:1", input_amount_raw=50_000_000,
        input_supply_fraction=0.05, quoted_output_raw=50_000_000,
        minimum_output_raw=49_000_000, route_plan_count=1,
        price_impact_bps=50.0, quote_expires_at_ms=now + 10_000,
        state="ROUTE_OBSERVED_NOT_EXECUTION_PROVEN", errors=(),
        execution_proven=False,
    ))
    safety.record(SafetyReport(
        token_address=MINT, passed=True, checks=("liquidity", "developer", "bundle"),
        reasons=(), observed_at_ms=now, creator_hold_pct=0.0,
        composite_verified=True,
    ))
    return launches, markets, risks, developers, bundles, routes, safety


def context_provider(mint, evidence):
    return SolanaAgentContext(
        mint=mint, provider="independent-agent-context",
        evidence_id="agent-context:1", observed_at_ms=evidence["market"]["retrieved_at_ms"],
        volume_spike=True, price_change_5m_pct=2.0,
        smart_wallet_buys=("wallet-a", "wallet-b"),
    )


def test_runner_blocks_missing_evidence_and_is_idempotent(tmp_path):
    path = str(tmp_path / "missing.sqlite3")
    launches, markets, risks, developers, bundles, routes, safety = stores(path)
    launches.record_candidate(normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="pumpportal-new-token", observed_at_ms=int(time.time() * 1000),
    ))
    broker = PaperBroker(path, 500, 10, 50)
    runner = SolanaPaperDecisionRunner(
        launch_store=launches, market_store=markets, risk_store=risks,
        developer_store=developers, bundle_store=bundles,
        sellability_store=routes, safety_store=safety, broker=broker,
        core=Core(broker), agent_context_provider=context_provider,
        trusted_agent_context_providers=("independent-agent-context",),
    )
    first = runner.run_once()
    second = runner.run_once()
    assert first.blocked == 1
    assert second.already_processed == 1
    assert broker.snapshot()["open_positions"] == []
    assert runner.audit_history(MINT)[0]["state"] == "BLOCKED"


def test_runner_opens_only_with_complete_typed_evidence_and_two_agents(tmp_path):
    path = str(tmp_path / "complete.sqlite3")
    stores_complete = seed_complete_packet(path)
    launches, markets, risks, developers, bundles, routes, safety = stores_complete
    broker = PaperBroker(path, 500, 10, 50)
    runner = SolanaPaperDecisionRunner(
        launch_store=launches, market_store=markets, risk_store=risks,
        developer_store=developers, bundle_store=bundles,
        sellability_store=routes, safety_store=safety, broker=broker,
        core=Core(broker), agent_context_provider=context_provider,
        trusted_agent_context_providers=("independent-agent-context",),
    )
    result = runner.run_once()
    assert result.approved == 1, runner.audit_history(MINT)[0]["reasons"]
    snapshot = broker.snapshot()
    assert len(snapshot["open_positions"]) == 1
    assert snapshot["cash"] == 450
    audit = runner.audit_history(MINT)[0]
    assert audit["state"] == "PAPER_ENTRY_OPENED"
    assert audit["result"]["sellability_state"] == "ROUTE_OBSERVED_NOT_EXECUTION_PROVEN"
    assert risks.latest(MINT)["report"]["missing_claims"] == ["executable_sellability"]


def test_runner_requires_explicit_agent_context_provider(tmp_path):
    path = str(tmp_path / "no-agent-context.sqlite3")
    launches, markets, risks, developers, bundles, routes, safety = seed_complete_packet(path)
    broker = PaperBroker(path, 500, 10, 50)
    runner = SolanaPaperDecisionRunner(
        launch_store=launches, market_store=markets, risk_store=risks,
        developer_store=developers, bundle_store=bundles,
        sellability_store=routes, safety_store=safety, broker=broker,
        core=Core(broker), agent_context_provider=None,
    )
    result = runner.run_once()
    assert result.blocked == 1
    assert broker.snapshot()["open_positions"] == []
    assert "independent agent-context provider is not configured" in runner.audit_history(MINT)[0]["reasons"]


def test_untrusted_agent_context_provider_blocks_entry(tmp_path):
    path = str(tmp_path / "untrusted-agent-context.sqlite3")
    launches, markets, risks, developers, bundles, routes, safety = seed_complete_packet(path)
    broker = PaperBroker(path, 500, 10, 50)
    runner = SolanaPaperDecisionRunner(
        launch_store=launches, market_store=markets, risk_store=risks,
        developer_store=developers, bundle_store=bundles,
        sellability_store=routes, safety_store=safety, broker=broker,
        core=Core(broker), agent_context_provider=context_provider,
        trusted_agent_context_providers=("another-provider",),
    )
    result = runner.run_once()
    assert result.blocked == 1
    assert broker.snapshot()["open_positions"] == []
    assert "agent context provenance, mint, or provider trust is invalid" in runner.audit_history(MINT)[0]["reasons"]


def test_stale_claim_without_new_broker_position_is_not_replayed(tmp_path):
    path = str(tmp_path / "stale-claim-no-position.sqlite3")
    launches, markets, risks, developers, bundles, routes, safety = seed_complete_packet(path)
    broker = PaperBroker(path, 500, 10, 50)
    runner = SolanaPaperDecisionRunner(
        launch_store=launches, market_store=markets, risk_store=risks,
        developer_store=developers, bundle_store=bundles,
        sellability_store=routes, safety_store=safety, broker=broker,
        core=Core(broker), agent_context_provider=None,
    )
    old = int(time.time() * 1000) - 600_000
    key = runner._claim(MINT, {"simulated_crash": True}, old)
    assert key is not None
    result = runner.reconcile_stale_claims(max_claim_age_ms=1_000)
    assert result == {
        "examined": 1, "reconciled_open_positions": 0,
        "indeterminate_no_replay": 1,
    }
    audit = runner.audit_history(MINT)[0]
    assert audit["state"] == "INDETERMINATE_NO_REPLAY"
    assert audit["result"]["automatic_replay"] is False


def test_stale_claim_with_matching_new_broker_position_is_reconciled(tmp_path):
    path = str(tmp_path / "stale-claim-with-position.sqlite3")
    launches, markets, risks, developers, bundles, routes, safety = seed_complete_packet(path)
    broker = PaperBroker(path, 500, 10, 50)
    runner = SolanaPaperDecisionRunner(
        launch_store=launches, market_store=markets, risk_store=risks,
        developer_store=developers, bundle_store=bundles,
        sellability_store=routes, safety_store=safety, broker=broker,
        core=Core(broker), agent_context_provider=None,
    )
    old = int(time.time() * 1000) - 600_000
    key = runner._claim(MINT, {"simulated_crash": True}, old)
    assert key is not None
    broker.open_position(MINT, 1.0, 50.0, "recovery test")
    result = runner.reconcile_stale_claims(max_claim_age_ms=1_000)
    assert result == {
        "examined": 1, "reconciled_open_positions": 1,
        "indeterminate_no_replay": 0,
    }
    audit = runner.audit_history(MINT)[0]
    assert audit["state"] == "PAPER_ENTRY_OPENED_RECONCILED"
    assert audit["result"]["automatic_replay"] is False


def test_indeterminate_claim_blocks_new_cycles_until_operator_ack(tmp_path):
    path = str(tmp_path / "indeterminate-block.sqlite3")
    launches, markets, risks, developers, bundles, routes, safety = seed_complete_packet(path)
    broker = PaperBroker(path, 500, 10, 50)
    runner = SolanaPaperDecisionRunner(
        launch_store=launches, market_store=markets, risk_store=risks,
        developer_store=developers, bundle_store=bundles,
        sellability_store=routes, safety_store=safety, broker=broker,
        core=Core(broker), agent_context_provider=None,
    )
    old = int(time.time() * 1000) - 600_000
    key = runner._claim(MINT, {"simulated_crash": True}, old)
    assert key is not None
    first = runner.run_once()
    assert first.blocked == 1
    assert len(runner.audit_history(MINT)) == 1
    assert runner.audit_history(MINT)[0]["state"] == "INDETERMINATE_NO_REPLAY"
    assert runner.acknowledge_indeterminate_claim(
        key, operator_note="Reviewed broker trades and confirmed no entry occurred",
    ) is True
    second = runner.run_once()
    assert second.blocked == 1
    states = [item["state"] for item in runner.audit_history(MINT)]
    assert "MANUALLY_RECONCILED_ACKNOWLEDGED" in states
    assert "BLOCKED" in states
    assert broker.snapshot()["open_positions"] == []
