from dataclasses import replace
from decimal import Decimal

from veltrix.ledger import PaperBroker
from veltrix.whales import WalletBuyEvent
from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.consensus import ConsensusEngine, MarketEvidence, RiskEvidence
from veltrix.solana_meme.market_data import SolanaMarketSnapshot
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceReport

import pytest

from veltrix.ledger import PaperBroker
from veltrix.solana_meme.consensus_store import ConsensusStore
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.paper_trading import PaperTradingPolicy, PaperTradingWorker
from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore
from veltrix.solana_meme.sol_paper_trading import SolanaNativePaperTradingWorker
from veltrix.solana_meme.sol_paper_wallet import SolanaPaperWallet, LAMPORTS_PER_SOL
from veltrix.solana_meme.sol_paper_quote import SolanaPaperQuote


class QuoteAdapter:
    def __init__(self, store, quote):
        self.market_store = store
        self.quote_value = quote

    def quote(self, mint):
        return self.quote_value


def test_native_sol_worker_opens_only_validated_consensus_in_sol(tmp_path):
    path = str(tmp_path / "native-worker.sqlite3")
    consensus = ConsensusStore(path)
    markets = MarketSnapshotStore(path)
    risks = SolanaRiskEvidenceStore(path)
    broker = PaperBroker(path, starting_balance=500, max_positions=10, max_position_usd=50)
    validator = PaperTradingWorker(
        consensus, markets, risks, broker,
        policy=PaperTradingPolicy(position_usd=25, fee_bps=100, slippage_bps=50),
        clock_ms=lambda: 1_800_000_000_000,
    )
    wallet = SolanaPaperWallet(path, 2 * LAMPORTS_PER_SOL)
    quote = SolanaPaperQuote(
        mint="So11111111111111111111111111111111111111112",
        market_snapshot_id=1, token_pair_address="token-pool",
        price_lamports=5_000_000, token_price_usd=Decimal("0.5"),
        sol_price_usd=Decimal("100"), token_observed_at_ms=1_800_000_000_000,
        sol_observed_at_ms=1_800_000_000_000, token_source="dexscreener",
        sol_source="dexscreener", sol_usd_pair_address="sol-usdc-pool",
        sol_usd_method="wsol_stable_pool",
    )
    worker = SolanaNativePaperTradingWorker(validator, wallet, QuoteAdapter(markets, quote))
    assert worker.status()["unit"] == "SOL"
    assert wallet.snapshot()["cash_lamports"] == 2 * LAMPORTS_PER_SOL


MINT = "So11111111111111111111111111111111111111112"
WALLET_A = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLET_B = "Vote111111111111111111111111111111111111111"
NOW = 1_800_000_000_000


def _market(observed):
    return SolanaMarketSnapshot(
        mint=MINT, selected_pair_address="token-pool", symbol="MEME", name="Meme",
        price_usd=0.001, selected_pool_liquidity_usd=100_000,
        selected_pool_volume_m5_usd=1_000, selected_pool_volume_h1_usd=50_000,
        selected_pool_volume_h24_usd=200_000, price_change_m5_pct=5.0,
        price_change_h1_pct=10.0, price_change_h24_pct=20.0,
        txns_m5_buys=100, txns_m5_sells=40, txns_h1_buys=500,
        txns_h1_sells=200, txns_h24_buys=2_000, txns_h24_sells=1_000,
        fdv_usd=1_000_000, market_cap_usd=900_000, dex_id="raydium",
        quote_token_symbol="SOL", pool_token_side="base", pair_count=1,
        retrieved_at_ms=observed, response_received_at_ms=observed,
        pair_created_at_ms=NOW - 3_600_000, quality_flags=(), source="dexscreener",
    )


def _risk(observed):
    return SolanaRiskEvidenceReport(
        mint=MINT, observed_at_ms=observed, goplus_provider=None,
        goplus_evidence_id=None, goplus_observed_at_ms=None,
        liquidity_locked=None, goplus_dev_hold_pct=None,
        goplus_bundled_supply_pct=None, goplus_creator_hold_pct=None,
        holder_provider=None, holder_evidence_id=None, holder_block_ref=None,
        holder_count=None, total_supply_raw=None, observed_holder_balance_raw=None,
        holder_coverage_complete=False, top_holder_pct=None, top5_holder_pct=None,
        top10_holder_pct=None, missing_claims=(), errors=(),
        mint_evidence_id="finalized-mint", mint_finalized_slot=123,
        token_program_id="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        mint_authority=None, freeze_authority=None, token_2022_extension_types=(),
        token_2022_extension_review_complete=True, mint_supply_raw=1_000_000,
        mint_decimals=9, mint_supply_reconciled=True, risk_flags=(),
    )


def _approved_worker_fixture(tmp_path):
    path = str(tmp_path / "native-worker.sqlite3")
    consensus, markets, risks = ConsensusStore(path), MarketSnapshotStore(path), SolanaRiskEvidenceStore(path)
    for offset in range(5):
        markets.record(_market(NOW - (5 - offset) * 60_000))
    markets.record(_market(NOW))
    risks.record(_risk(NOW))
    latest = markets.latest(MINT)
    decision = ConsensusEngine(
        trusted_wallets=(WALLET_A, WALLET_B), min_liquidity_usd=10_000,
        min_volume_h1_usd=10_000, min_pair_age_ms=60_000, whale_min_buy_usd=100,
    ).evaluate(
        MarketEvidence.from_store_row(latest), RiskEvidence.from_store_payload(risks.latest(MINT)),
        history=[x for x in markets.history(MINT, limit=20) if x["id"] != latest["id"]],
        wallet_events=(
            WalletBuyEvent("solana", MINT, WALLET_A, NOW - 100, 150, "helius"),
            WalletBuyEvent("solana", MINT, WALLET_B, NOW - 50, 175, "helius"),
        ), now_ms=NOW,
    )
    consensus.record(decision)
    broker = PaperBroker(path, starting_balance=500, max_positions=10, max_position_usd=50)
    validator = PaperTradingWorker(
        consensus, markets, risks, broker,
        policy=PaperTradingPolicy(position_usd=25, fee_bps=100, slippage_bps=50),
        clock_ms=lambda: NOW,
    )
    wallet = SolanaPaperWallet(path, 2 * LAMPORTS_PER_SOL)
    quote = SolanaPaperQuote(
        mint=MINT, market_snapshot_id=latest["id"], token_pair_address="token-pool",
        price_lamports=10_000, token_price_usd=Decimal("0.001"),
        sol_price_usd=Decimal("100"), token_observed_at_ms=NOW,
        sol_observed_at_ms=NOW, token_source="dexscreener",
        sol_source="dexscreener", sol_usd_pair_address="sol-usdc-pool",
    )
    return markets, wallet, validator, decision, QuoteAdapter(markets, quote)


def test_native_sol_worker_opens_approved_decision_and_persists_quote_provenance(tmp_path):
    markets, wallet, validator, decision, adapter = _approved_worker_fixture(tmp_path)
    worker = SolanaNativePaperTradingWorker(validator, wallet, adapter)
    result = worker.run_once()
    assert decision.status == "CONSENSUS_APPROVED"
    assert result.opened_positions == 1
    assert result.errors == 0
    snap = wallet.snapshot()
    assert snap["cash_lamports"] < 2 * LAMPORTS_PER_SOL
    assert len(snap["open_positions"]) == 1
    fill = snap["fills"][0]
    assert fill["side"] == "BUY"
    assert fill["quote_provenance"]["sol_usd_pair_address"] == "sol-usdc-pool"
    assert fill["quote_provenance"]["market_snapshot_id"] == markets.latest(MINT)["id"]

    second = worker.run_once()
    assert second.opened_positions == 0
    assert second.duplicate_decisions == 1


def test_native_sol_worker_propagates_pipeline_deadline_without_mutation(tmp_path):
    from veltrix.orchestration import JobDeadlineExceeded

    markets, wallet, validator, decision, adapter = _approved_worker_fixture(tmp_path)
    worker = SolanaNativePaperTradingWorker(validator, wallet, adapter)

    class ExpiredContext:
        def checkpoint(self, *, items=1):
            raise JobDeadlineExceeded("job deadline exceeded")

    with pytest.raises(JobDeadlineExceeded, match="deadline"):
        worker.run_once(context=ExpiredContext())
    assert wallet.snapshot()["open_positions"] == []
    assert wallet.snapshot()["cash_lamports"] == 2 * LAMPORTS_PER_SOL
