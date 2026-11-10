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
