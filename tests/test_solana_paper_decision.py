from dataclasses import replace

import pytest

from veltrix.agents import Signal
from veltrix.core import Core
from veltrix.ledger import PaperBroker
from veltrix.safety import SafetyReport
from veltrix.developer_concentration import DeveloperConcentrationReport
from veltrix.launch_bundles import LaunchBundleReport
from veltrix.providers.solana_mint import TOKEN_PROGRAM_ID
from veltrix.solana_meme.bundle_store import LaunchBundleEvidenceStore
from veltrix.solana_meme.developer_store import DeveloperConcentrationStore
from veltrix.solana_meme.launch_stream import LaunchStore
from veltrix.solana_meme.market_data import SolanaMarketSnapshot
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.paper_decision import SolanaPaperDecisionRunner
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceReport
from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore
from veltrix.solana_meme.sellability import SolanaSellabilityEvidence
from veltrix.solana_meme.sellability_store import SolanaSellabilityEvidenceStore
from veltrix.solana_meme.candidates import normalize_candidate

MINT = "So11111111111111111111111111111111111111112"
NOW = 1_000_000


class FixedAgent:
    def __init__(self, name):
        self.name = name

    def evaluate(self, token):
        return Signal(self.name, token["address"], "BUY", "deterministic test signal")


def make_runner(tmp_path, *, clock=lambda: NOW, risk_missing=("executable_sellability",),
                safety=None, agent_context_loader=None):
    path = str(tmp_path / "runner.sqlite3")
    launches = LaunchStore(path)
    markets = MarketSnapshotStore(path)
    risks = SolanaRiskEvidenceStore(path)
    developers = DeveloperConcentrationStore(path)
    bundles = LaunchBundleEvidenceStore(path)
    routes = SolanaSellabilityEvidenceStore(path)

    launches.record_candidate(normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="pumpportal-new-token", observed_at_ms=NOW - 10_000,
    ))
    markets.record(SolanaMarketSnapshot(
        mint=MINT, selected_pair_address="pair-1", symbol="TEST",
        price_usd=1.0, selected_pool_liquidity_usd=100_000,
        selected_pool_volume_h1_usd=10_000, price_change_m5_pct=3.0,
        pair_count=1, retrieved_at_ms=NOW - 1_000,
        provider_observed_at_ms=NOW - 1_000,
        pair_created_at_ms=NOW - 60_000, quality_flags=(),
    ))
    supply = 1_000_000_000
    risks.record(SolanaRiskEvidenceReport(
        mint=MINT, observed_at_ms=NOW - 1_000,
        goplus_provider="goplus", goplus_evidence_id="goplus:1",
        goplus_observed_at_ms=NOW - 1_000, liquidity_locked=True,
        holder_provider="holder-rpc", holder_evidence_id="holders:1",
        holder_block_ref="finalized:100", holder_count=2,
        total_supply_raw=supply, observed_holder_balance_raw=supply,
        holder_coverage_complete=True, top_holder_pct=60.0,
        top5_holder_pct=100.0, top10_holder_pct=100.0,
        missing_claims=tuple(risk_missing), errors=(),
        mint_evidence_id="mint:100", mint_finalized_slot=100,
        token_program_id=TOKEN_PROGRAM_ID, mint_authority=None,
        freeze_authority=None, token_2022_extension_types=(),
        token_2022_extension_review_complete=True,
        mint_supply_raw=supply, mint_decimals=6, mint_supply_reconciled=True,
    ))
    developers.record(DeveloperConcentrationReport(
        chain="solana", token_address=MINT, provider="certified-dev-index",
        linked_wallets=(), linked_supply_raw=0, total_supply_raw=supply,
        dev_hold_pct=0.0, coverage_complete=True, observed_at_ms=NOW - 1_000,
        evidence_id="dev:1", source_ref="https://evidence.example/dev/1",
        method="complete-holder-funding-ancestry", independence_group="dev-index",
        holder_provider="holder-rpc", funding_provider="funding-graph",
        root_provider="signed-roots", reasons=(),
    ))
    bundles.record(LaunchBundleReport(
        chain="solana", token_address=MINT, provider="certified-launch-index",
        launch_at_ms=NOW - 60_000, window_end_ms=NOW - 1_000,
        coverage_complete=True, bundled_supply_pct=0.0,
        shared_funder_clusters=0, participating_wallets=0,
        accepted_transactions=0, rejected=(), notes=(),
        evidence_id="bundle:1", observed_at_ms=NOW - 1_000,
        scanned_through_ms=NOW - 1_000, supply_source="finalized-mint",
        supply_block_ref="slot:100", supply_observed_at_ms=NOW - 1_000,
        pages_scanned=1, funder_providers=("funding-graph",), provenance=("scan:1",),
    ))
    routes.record(SolanaSellabilityEvidence(
        mint=MINT, observed_at_ms=NOW - 500, provider="jupiter-v2",
        evidence_id="route:1", input_amount_raw=50_000_000,
        input_supply_fraction=0.05, quoted_output_raw=50_000_000,
        minimum_output_raw=49_000_000, route_plan_count=1,
        price_impact_bps=20.0, quote_expires_at_ms=NOW + 10_000,
        state="ROUTE_OBSERVED_NOT_EXECUTION_PROVEN", errors=(),
        execution_proven=False,
    ))
    broker = PaperBroker(path, starting_balance=500, max_positions=10, max_position_usd=50)
    core = Core(broker, max_positions=10, max_position_usd=50,
                agents=[FixedAgent("scout"), FixedAgent("sniper")])
    safety_report = safety or SafetyReport(
        token_address=MINT, passed=True, checks=("liquidity", "developer", "bundle"),
        reasons=(), observed_at_ms=NOW - 1_000, composite_verified=True,
    )
    runner = SolanaPaperDecisionRunner(
        launches, markets, risks, developers, bundles, routes, core,
        safety_report_loader=lambda mint, now: safety_report,
        agent_context_loader=agent_context_loader,
        clock_ms=clock,
    )
    return runner, broker, risks, path


def test_runner_opens_one_paper_position_and_persists_provenance(tmp_path):
    runner, broker, _, _ = make_runner(tmp_path)
    result = runner.run_once()
    assert result.opened == 1
    assert result.blocked == 0
    assert len(broker.snapshot()["open_positions"]) == 1
    audit = runner.audit_history(MINT)
    assert audit[0]["state"] == "OPENED"
    assert audit[0]["risk_evidence_id"].startswith("solana-risk:")
    assert audit[0]["sellability_evidence_id"].startswith("solana-sellability:")
    assert audit[0]["result"]["sellability_state"] == "ROUTE_OBSERVED_NOT_EXECUTION_PROVEN"


def test_runner_is_idempotent_for_the_same_evidence_cycle(tmp_path):
    runner, broker, _, _ = make_runner(tmp_path)
    first = runner.run_once()
    second = runner.run_once()
    assert first.opened == 1
    assert second.duplicate_cycles == 1
    assert len(broker.snapshot()["open_positions"]) == 1
    assert len(runner.audit_history(MINT)) == 1


def test_unknown_mandatory_token_risk_claim_blocks_without_opening(tmp_path):
    runner, broker, _, _ = make_runner(
        tmp_path, risk_missing=("bundled_supply", "executable_sellability")
    )
    result = runner.run_once()
    assert result.blocked == 1
    assert result.opened == 0
    assert broker.snapshot()["open_positions"] == []
    assert runner.audit_history(MINT)[0]["state"] == "BLOCKED"


def test_missing_or_untyped_composite_safety_report_blocks(tmp_path):
    runner, broker, _, _ = make_runner(tmp_path, safety=None)
    runner.safety_report_loader = lambda mint, now: {"passed": True}
    result = runner.run_once()
    assert result.blocked == 1
    assert broker.snapshot()["open_positions"] == []


def test_stale_market_snapshot_blocks(tmp_path):
    runner, broker, _, _ = make_runner(
        tmp_path, clock=lambda: NOW + 200_000
    )
    result = runner.run_once()
    assert result.blocked == 1
    assert broker.snapshot()["open_positions"] == []


def test_agent_context_cannot_override_market_or_evidence_fields(tmp_path):
    runner, broker, _, _ = make_runner(
        tmp_path, agent_context_loader=lambda mint, now: {"price": 0.000001}
    )
    result = runner.run_once()
    assert result.blocked == 1
    assert broker.snapshot()["open_positions"] == []


def test_runner_requires_approved_paper_account_profile(tmp_path):
    path = str(tmp_path / "wrong-profile.sqlite3")
    launches = LaunchStore(path)
    markets = MarketSnapshotStore(path)
    risks = SolanaRiskEvidenceStore(path)
    developers = DeveloperConcentrationStore(path)
    bundles = LaunchBundleEvidenceStore(path)
    routes = SolanaSellabilityEvidenceStore(path)
    broker = PaperBroker(path, starting_balance=100, max_positions=10, max_position_usd=50)
    core = Core(broker, agents=[FixedAgent("scout"), FixedAgent("sniper")])
    with pytest.raises(ValueError, match="approved"):
        SolanaPaperDecisionRunner(
            launches, markets, risks, developers, bundles, routes, core,
            safety_report_loader=lambda mint, now: None,
        )
