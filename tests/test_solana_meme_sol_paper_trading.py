from decimal import Decimal

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
    )
    worker = SolanaNativePaperTradingWorker(validator, wallet, QuoteAdapter(markets, quote))
    assert worker.status()["unit"] == "SOL"
    assert wallet.snapshot()["cash_lamports"] == 2 * LAMPORTS_PER_SOL
