"""SOL-denominated, deterministic paper wallet for Solana token simulations.

All wallet balances, costs, fees and P&L are integer lamports. Token quantities
are decimal strings. This module has no key material, RPC signing or broadcast
capability and is intentionally separate from the legacy USD broker.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import time
from pathlib import Path

from ..database import connect_database, ensure_database_parent, is_remote_database, row_factory_for
from .candidates import canonical_mint

LAMPORTS_PER_SOL = 1_000_000_000
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_SQLITE_INTEGER:
        raise ValueError(f"{name} must be a positive SQLite integer")
    return value


def _quantity(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("quantity must be a finite positive decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("quantity must be a finite positive decimal") from None
    if not result.is_finite() or result <= 0:
        raise ValueError("quantity must be a finite positive decimal")
    return result


class SolanaPaperWallet:
    """Persistent SOL-only simulation ledger; never signs or broadcasts."""

    def __init__(self, database_path: str, starting_balance_lamports: int):
        _positive_int(starting_balance_lamports, "starting_balance_lamports")
        if not isinstance(database_path, (str, Path)) or not str(database_path).strip() or str(database_path) == ":memory:":
            raise ValueError("SolanaPaperWallet requires a persistent database path")
        self.database_path = str(database_path)
        ensure_database_parent(self.database_path)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS solana_paper_account (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    starting_balance_lamports INTEGER NOT NULL,
                    cash_lamports INTEGER NOT NULL,
                    created_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS solana_paper_positions (
                    mint TEXT PRIMARY KEY,
                    quantity TEXT NOT NULL,
                    entry_price_lamports INTEGER NOT NULL,
                    cost_basis_lamports INTEGER NOT NULL,
                    opened_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS solana_paper_fills (
                    fill_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    mint TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    quantity TEXT NOT NULL,
                    price_lamports INTEGER NOT NULL,
                    notional_lamports INTEGER NOT NULL,
                    fee_lamports INTEGER NOT NULL,
                    realized_pnl_lamports INTEGER NOT NULL,
                    created_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_solana_paper_fills_time
                    ON solana_paper_fills(created_at_ms DESC);
            """)
            db.execute(
                """INSERT OR IGNORE INTO solana_paper_account
                   (id,starting_balance_lamports,cash_lamports,created_at_ms)
                   VALUES(1,?,?,?)""",
                (starting_balance_lamports, starting_balance_lamports, int(time.time() * 1000)),
            )

    def _connect(self):
        db = connect_database(self.database_path, timeout=15)
        db.row_factory = row_factory_for(self.database_path)
        if not is_remote_database(self.database_path):
            db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _transaction(self):
        db = self._connect()
        try:
            if not is_remote_database(self.database_path):
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def open_position(
        self, *, idempotency_key: str, mint: str, quantity: object,
        price_lamports: int, fee_lamports: int, created_at_ms: int,
        context=None,
    ) -> dict:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        mint = canonical_mint(mint)
        qty = _quantity(quantity)
        _positive_int(price_lamports, "price_lamports")
        if isinstance(fee_lamports, bool) or not isinstance(fee_lamports, int) or fee_lamports < 0:
            raise ValueError("fee_lamports must be a non-negative integer")
        _positive_int(created_at_ms, "created_at_ms")
        notional = int((qty * price_lamports).to_integral_value(rounding=ROUND_DOWN))
        if notional <= 0 or notional > MAX_SQLITE_INTEGER:
            raise ValueError("position notional is outside the SQLite lamport range")
        total = notional + fee_lamports
        if total > MAX_SQLITE_INTEGER:
            raise ValueError("position debit exceeds the SQLite lamport range")
        if context is not None:
            context.checkpoint(items=1)
        with self._transaction() as db:
            if context is not None:
                context.assert_fence_in_transaction(db)
            existing = db.execute(
                "SELECT fill_id FROM solana_paper_fills WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                return {"status": "DUPLICATE", "fill_id": existing["fill_id"]}
            if db.execute("SELECT 1 FROM solana_paper_positions WHERE mint=?", (mint,)).fetchone():
                raise ValueError("an open paper position already exists for this mint")
            account = db.execute("SELECT cash_lamports FROM solana_paper_account WHERE id=1").fetchone()
            if account is None or account["cash_lamports"] < total:
                raise ValueError("insufficient simulated SOL balance")
            fill_id = "solpaper-" + idempotency_key
            db.execute(
                "UPDATE solana_paper_account SET cash_lamports=cash_lamports-? WHERE id=1",
                (total,),
            )
            db.execute(
                """INSERT INTO solana_paper_positions
                   (mint,quantity,entry_price_lamports,cost_basis_lamports,opened_at_ms)
                   VALUES(?,?,?,?,?)""",
                (mint, str(qty), price_lamports, total, created_at_ms),
            )
            db.execute(
                """INSERT INTO solana_paper_fills
                   (fill_id,idempotency_key,mint,side,quantity,price_lamports,
                    notional_lamports,fee_lamports,realized_pnl_lamports,created_at_ms)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (fill_id, idempotency_key, mint, "BUY", str(qty), price_lamports,
                 notional, fee_lamports, 0, created_at_ms),
            )
        return {"status": "OPENED", "fill_id": fill_id, "notional_lamports": notional,
                "fee_lamports": fee_lamports, "total_debit_lamports": total}

    def close_position(
        self, *, idempotency_key: str, mint: str, price_lamports: int,
        fee_lamports: int, created_at_ms: int, context=None,
    ) -> dict:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        mint = canonical_mint(mint)
        _positive_int(price_lamports, "price_lamports")
        if isinstance(fee_lamports, bool) or not isinstance(fee_lamports, int) or fee_lamports < 0:
            raise ValueError("fee_lamports must be a non-negative integer")
        _positive_int(created_at_ms, "created_at_ms")
        if context is not None:
            context.checkpoint(items=1)
        with self._transaction() as db:
            if context is not None:
                context.assert_fence_in_transaction(db)
            existing = db.execute(
                "SELECT fill_id FROM solana_paper_fills WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                return {"status": "DUPLICATE", "fill_id": existing["fill_id"]}
            position = db.execute(
                "SELECT * FROM solana_paper_positions WHERE mint=?", (mint,)
            ).fetchone()
            if position is None:
                raise ValueError("no open paper position exists for this mint")
            qty = Decimal(position["quantity"])
            notional = int((qty * price_lamports).to_integral_value(rounding=ROUND_DOWN))
            proceeds = notional - fee_lamports
            if proceeds < 0:
                raise ValueError("exit fee exceeds simulated proceeds")
            pnl = proceeds - int(position["cost_basis_lamports"])
            fill_id = "solpaper-" + idempotency_key
            db.execute(
                "UPDATE solana_paper_account SET cash_lamports=cash_lamports+? WHERE id=1",
                (proceeds,),
            )
            db.execute("DELETE FROM solana_paper_positions WHERE mint=?", (mint,))
            db.execute(
                """INSERT INTO solana_paper_fills
                   (fill_id,idempotency_key,mint,side,quantity,price_lamports,
                    notional_lamports,fee_lamports,realized_pnl_lamports,created_at_ms)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (fill_id, idempotency_key, mint, "SELL", str(qty), price_lamports,
                 notional, fee_lamports, pnl, created_at_ms),
            )
        return {"status": "CLOSED", "fill_id": fill_id, "realized_pnl_lamports": pnl,
                "proceeds_lamports": proceeds, "fee_lamports": fee_lamports}

    def snapshot(self, marks_lamports: dict[str, int] | None = None) -> dict:
        marks = marks_lamports or {}
        with self._connect() as db:
            account = db.execute("SELECT * FROM solana_paper_account WHERE id=1").fetchone()
            positions = db.execute(
                "SELECT * FROM solana_paper_positions ORDER BY opened_at_ms,mint"
            ).fetchall()
            fills = db.execute(
                "SELECT * FROM solana_paper_fills ORDER BY created_at_ms DESC LIMIT 100"
            ).fetchall()
        unrealized = 0
        position_rows = []
        for row in positions:
            mark = marks.get(row["mint"])
            qty = Decimal(row["quantity"])
            market_value = None
            pnl = None
            if mark is not None:
                _positive_int(mark, "mark_price_lamports")
                market_value = int((qty * mark).to_integral_value(rounding=ROUND_DOWN))
                pnl = market_value - int(row["cost_basis_lamports"])
                unrealized += pnl
            position_rows.append({
                **dict(row), "mark_price_lamports": mark,
                "market_value_lamports": market_value, "unrealized_pnl_lamports": pnl,
            })
        realized = sum(int(x["realized_pnl_lamports"]) for x in fills)
        return {
            "unit": "lamports", "lamports_per_sol": LAMPORTS_PER_SOL,
            "starting_balance_lamports": int(account["starting_balance_lamports"]),
            "cash_lamports": int(account["cash_lamports"]),
            "open_positions": position_rows,
            "realized_pnl_lamports": realized,
            "unrealized_pnl_lamports": unrealized,
            "equity_lamports": int(account["cash_lamports"]) + sum(
                int(x["market_value_lamports"] or 0) for x in position_rows
            ),
            "fills": [dict(x) for x in fills],
        }
