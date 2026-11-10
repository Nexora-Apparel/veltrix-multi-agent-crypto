from decimal import Decimal

import pytest

from veltrix.solana_meme.sol_paper_wallet import LAMPORTS_PER_SOL, SolanaPaperWallet

MINT = "So11111111111111111111111111111111111111112"


def test_sol_wallet_opens_marks_closes_and_persists_after_restart(tmp_path):
    path = str(tmp_path / "sol-wallet.sqlite3")
    wallet = SolanaPaperWallet(path, 2 * LAMPORTS_PER_SOL)
    opened = wallet.open_position(
        idempotency_key="decision-001", mint=MINT, quantity=Decimal("100"),
        price_lamports=1_000_000, fee_lamports=10_000_000, created_at_ms=1_800_000_000_000,
    )
    assert opened["status"] == "OPENED"
    assert opened["total_debit_lamports"] == 110_000_000
    assert wallet.open_position(
        idempotency_key="decision-001", mint=MINT, quantity=100,
        price_lamports=1_000_000, fee_lamports=10_000_000, created_at_ms=1_800_000_000_001,
    )["status"] == "DUPLICATE"

    restarted = SolanaPaperWallet(path, 99 * LAMPORTS_PER_SOL)
    unmarked = restarted.snapshot()
    assert unmarked["equity_lamports"] is None
    assert unmarked["equity_complete"] is False
    assert unmarked["unmarked_positions"] == [MINT]
    marked = restarted.snapshot({MINT: 1_500_000})
    assert marked["equity_complete"] is True
    assert marked["cash_lamports"] == 2 * LAMPORTS_PER_SOL - 110_000_000
    assert marked["unrealized_pnl_lamports"] == 40_000_000
    assert marked["equity_lamports"] == 2 * LAMPORTS_PER_SOL + 40_000_000

    closed = restarted.close_position(
        idempotency_key="exit-001", mint=MINT, price_lamports=1_500_000,
        fee_lamports=15_000_000, created_at_ms=1_800_000_000_100,
    )
    assert closed["realized_pnl_lamports"] == 25_000_000
    again = SolanaPaperWallet(path, 1)
    assert again.snapshot()["cash_lamports"] == 2 * LAMPORTS_PER_SOL - 110_000_000 + 135_000_000
    assert again.snapshot()["realized_pnl_lamports"] == 25_000_000
    assert again.snapshot()["open_positions"] == []


def test_sol_wallet_rejects_insufficient_balance_and_duplicate_mint(tmp_path):
    wallet = SolanaPaperWallet(str(tmp_path / "sol-wallet.sqlite3"), 100_000_000)
    with pytest.raises(ValueError, match="insufficient"):
        wallet.open_position(
            idempotency_key="too-large", mint=MINT, quantity=100,
            price_lamports=2_000_000, fee_lamports=1, created_at_ms=1000,
        )
    wallet.open_position(
        idempotency_key="first", mint=MINT, quantity=10,
        price_lamports=1_000_000, fee_lamports=1, created_at_ms=1000,
    )
    with pytest.raises(ValueError, match="already exists"):
        wallet.open_position(
            idempotency_key="second", mint=MINT, quantity=10,
            price_lamports=1_000_000, fee_lamports=1, created_at_ms=1001,
        )


def test_sol_wallet_validates_lamport_precision_and_mint(tmp_path):
    wallet = SolanaPaperWallet(str(tmp_path / "sol-wallet.sqlite3"), 100_000_000)
    with pytest.raises(ValueError):
        wallet.open_position(
            idempotency_key="bad-price", mint=MINT, quantity=1,
            price_lamports=0, fee_lamports=0, created_at_ms=1000,
        )
    with pytest.raises(ValueError):
        wallet.open_position(
            idempotency_key="bad-mint", mint="not-a-mint", quantity=1,
            price_lamports=1, fee_lamports=0, created_at_ms=1000,
        )


class LostFence:
    def checkpoint(self, *, items=1):
        return None

    def assert_fence_in_transaction(self, db):
        raise RuntimeError("fence lost")


def test_sol_wallet_rolls_back_fill_when_orchestration_fence_is_lost(tmp_path):
    wallet = SolanaPaperWallet(str(tmp_path / "fenced-sol.sqlite3"), 1_000_000_000)
    with pytest.raises(RuntimeError, match="fence lost"):
        wallet.open_position(
            idempotency_key="fenced-entry", mint=MINT, quantity=10,
            price_lamports=1_000_000, fee_lamports=1_000,
            created_at_ms=1_800_000_000_000, context=LostFence(),
        )
    snapshot = wallet.snapshot()
    assert snapshot["cash_lamports"] == 1_000_000_000
    assert snapshot["open_positions"] == []
    assert snapshot["fills"] == []


def test_sol_wallet_partial_take_profits_and_trailing_stop_are_idempotent(tmp_path):
    wallet = SolanaPaperWallet(str(tmp_path / "sol-exits.sqlite3"), 2_000_000_000)
    wallet.open_position(
        idempotency_key="entry", mint=MINT, quantity=100,
        price_lamports=1_000_000, fee_lamports=10_000_000,
        created_at_ms=1_800_000_000_000,
    )

    first = wallet.process_market_price(
        mint=MINT, price_lamports=2_000_000, market_snapshot_id=10,
        fee_bps=100, slippage_bps=0, created_at_ms=1_800_000_001_000,
    )
    assert first[0]["status"] == "PARTIALLY_CLOSED"
    assert Decimal(first[0]["quantity_sold"]) == Decimal("50")
    assert wallet.process_market_price(
        mint=MINT, price_lamports=2_000_000, market_snapshot_id=10,
        fee_bps=100, slippage_bps=0, created_at_ms=1_800_000_001_000,
    ) == []

    second = wallet.process_market_price(
        mint=MINT, price_lamports=3_000_000, market_snapshot_id=11,
        fee_bps=100, slippage_bps=0, created_at_ms=1_800_000_002_000,
    )
    assert Decimal(second[0]["quantity_sold"]) == Decimal("25")
    assert Decimal(wallet.snapshot()["open_positions"][0]["quantity"]) == Decimal("25")

    final = wallet.process_market_price(
        mint=MINT, price_lamports=2_800_000, market_snapshot_id=12,
        fee_bps=100, slippage_bps=0, created_at_ms=1_800_000_003_000,
    )
    assert final[0]["status"] == "CLOSED"
    snap = SolanaPaperWallet(str(tmp_path / "sol-exits.sqlite3"), 1).snapshot()
    assert snap["open_positions"] == []
    assert snap["realized_pnl_lamports"] > 0
    assert len([fill for fill in snap["fills"] if fill["side"] == "SELL"]) == 3


def test_sol_wallet_initial_stop_closes_position_and_records_loss(tmp_path):
    wallet = SolanaPaperWallet(str(tmp_path / "sol-stop.sqlite3"), 1_000_000_000)
    wallet.open_position(
        idempotency_key="entry", mint=MINT, quantity=10,
        price_lamports=10_000_000, fee_lamports=1_000_000,
        created_at_ms=1_800_000_000_000,
    )
    exits = wallet.process_market_price(
        mint=MINT, price_lamports=9_300_000, market_snapshot_id=99,
        fee_bps=100, slippage_bps=50, created_at_ms=1_800_000_001_000,
    )
    assert len(exits) == 1
    assert exits[0]["status"] == "CLOSED"
    assert exits[0]["realized_pnl_lamports"] < 0
    assert wallet.snapshot()["open_positions"] == []


def test_sol_wallet_rejects_incomplete_or_invalid_quote_audit_provenance(tmp_path):
    wallet = SolanaPaperWallet(str(tmp_path / "sol-provenance.sqlite3"), 1_000_000_000)
    provenance = {
        "mint": MINT,
        "market_snapshot_id": 1,
        "token_pair_address": "token-pool",
        "sol_usd_pair_address": "sol-usdc-pool",
        "sol_usd_method": "wsol_stable_pool",
        "token_price_usd": "0.001",
        "sol_price_usd": "150.25",
        "token_observed_at_ms": 1_800_000_000_000,
        "sol_observed_at_ms": 1_800_000_000_000,
        "token_source": "dexscreener",
        "sol_source": "dexscreener",
    }
    for key, invalid in (
        ("token_pair_address", ""),
        ("sol_usd_pair_address", ""),
        ("token_price_usd", "0"),
        ("sol_price_usd", "NaN"),
        ("market_snapshot_id", 0),
    ):
        broken = {**provenance, key: invalid}
        with pytest.raises(ValueError, match="quote provenance|market_snapshot_id"):
            wallet.open_position(
                idempotency_key="bad-" + key, mint=MINT, quantity=1,
                price_lamports=1_000, fee_lamports=0,
                created_at_ms=1_800_000_000_001, quote_provenance=broken,
            )

    with pytest.raises(ValueError, match="audit record size"):
        wallet.open_position(
            idempotency_key="oversized", mint=MINT, quantity=1,
            price_lamports=1_000, fee_lamports=0,
            created_at_ms=1_800_000_000_001,
            quote_provenance={**provenance, "extra": "x" * 20_000},
        )
    assert wallet.snapshot()["fills"] == []
