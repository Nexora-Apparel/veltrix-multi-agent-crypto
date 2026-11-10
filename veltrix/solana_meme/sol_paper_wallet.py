"""SOL-denominated, deterministic paper wallet for Solana token simulations.

All wallet balances, costs, fees and P&L are integer lamports. Token quantities
are decimal strings. This module has no key material, RPC signing or broadcast
capability and is intentionally separate from the legacy USD broker.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
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

    def __init__(
        self, database_path: str, starting_balance_lamports: int,
        *, stop_loss_pct: float = 0.06, trailing_pct: float = 0.06,
        max_open_positions: int = 10, max_portfolio_exposure_pct: float = 0.5,
    ):
        _positive_int(starting_balance_lamports, "starting_balance_lamports")
        for name, value in (("stop_loss_pct", stop_loss_pct), ("trailing_pct", trailing_pct)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1:
                raise ValueError(f"{name} must be between zero and one")
        if isinstance(max_open_positions, bool) or not isinstance(max_open_positions, int) or max_open_positions < 1:
            raise ValueError("max_open_positions must be a positive integer")
        if isinstance(max_portfolio_exposure_pct, bool) or not isinstance(max_portfolio_exposure_pct, (int, float)) or not 0 < max_portfolio_exposure_pct <= 1:
            raise ValueError("max_portfolio_exposure_pct must be in (0, 1]")
        self.stop_loss_pct = Decimal(str(stop_loss_pct))
        self.trailing_pct = Decimal(str(trailing_pct))
        self.max_open_positions = max_open_positions
        self.max_portfolio_exposure_pct = Decimal(str(max_portfolio_exposure_pct))
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
                CREATE TABLE IF NOT EXISTS solana_paper_position_state (
                    mint TEXT PRIMARY KEY,
                    hit_2x INTEGER NOT NULL DEFAULT 0,
                    hit_3x INTEGER NOT NULL DEFAULT 0,
                    peak_price_lamports INTEGER NOT NULL
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
                    created_at_ms INTEGER NOT NULL,
                    quote_provenance_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_solana_paper_fills_time
                    ON solana_paper_fills(created_at_ms DESC);
            """)
            fill_columns = {row["name"] for row in db.execute("PRAGMA table_info(solana_paper_fills)").fetchall()}
            if "quote_provenance_json" not in fill_columns:
                db.execute("ALTER TABLE solana_paper_fills ADD COLUMN quote_provenance_json TEXT NOT NULL DEFAULT '{}'")
            db.execute(
                """INSERT OR IGNORE INTO solana_paper_account
                   (id,starting_balance_lamports,cash_lamports,created_at_ms)
                   VALUES(1,?,?,?)""",
                (starting_balance_lamports, starting_balance_lamports, int(time.time() * 1000)),
            )

    @staticmethod
    def _validate_quote_provenance(value: dict | None, mint: str) -> str:
        if value is None:
            return "{}"
        if not isinstance(value, dict) or value.get("mint") != mint:
            raise ValueError("quote provenance must match the canonical mint")
        for field in ("token_source", "sol_source", "sol_usd_pair_address"):
            if not isinstance(value.get(field), str) or not value[field].strip():
                raise ValueError(f"quote provenance requires {field}")
        for field in ("market_snapshot_id", "token_observed_at_ms", "sol_observed_at_ms"):
            _positive_int(value.get(field), field)
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def has_fill_key(self, idempotency_key: str) -> bool:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        with self._connect() as db:
            return db.execute(
                "SELECT 1 FROM solana_paper_fills WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone() is not None

    @staticmethod
    def _decimal(value: object, name: str) -> Decimal:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a finite non-negative decimal")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError(f"{name} must be a finite non-negative decimal") from None
        if not result.is_finite() or result < 0:
            raise ValueError(f"{name} must be a finite non-negative decimal")
        return result

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
        context=None, quote_provenance: dict | None = None,
    ) -> dict:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        mint = canonical_mint(mint)
        qty = _quantity(quantity)
        _positive_int(price_lamports, "price_lamports")
        if isinstance(fee_lamports, bool) or not isinstance(fee_lamports, int) or not 0 <= fee_lamports <= MAX_SQLITE_INTEGER:
            raise ValueError("fee_lamports must be a non-negative SQLite integer")
        _positive_int(created_at_ms, "created_at_ms")
        notional = int((qty * price_lamports).to_integral_value(rounding=ROUND_DOWN))
        if notional <= 0 or notional > MAX_SQLITE_INTEGER:
            raise ValueError("position notional is outside the SQLite lamport range")
        total = notional + fee_lamports
        if total > MAX_SQLITE_INTEGER:
            raise ValueError("position debit exceeds the SQLite lamport range")
        provenance_json = self._validate_quote_provenance(quote_provenance, mint)
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
            count = int(db.execute("SELECT COUNT(*) FROM solana_paper_positions").fetchone()[0])
            if count >= self.max_open_positions:
                raise ValueError("maximum SOL paper positions reached")
            account = db.execute("SELECT starting_balance_lamports,cash_lamports FROM solana_paper_account WHERE id=1").fetchone()
            exposure = int(db.execute("SELECT COALESCE(SUM(cost_basis_lamports),0) FROM solana_paper_positions").fetchone()[0])
            if exposure + total > int(Decimal(account["starting_balance_lamports"]) * self.max_portfolio_exposure_pct):
                raise ValueError("SOL paper portfolio exposure cap would be exceeded")
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
                "INSERT INTO solana_paper_position_state(mint,peak_price_lamports) VALUES(?,?)",
                (mint, price_lamports),
            )
            db.execute(
                """INSERT INTO solana_paper_fills
                   (fill_id,idempotency_key,mint,side,quantity,price_lamports,
                    notional_lamports,fee_lamports,realized_pnl_lamports,created_at_ms,quote_provenance_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (fill_id, idempotency_key, mint, "BUY", str(qty), price_lamports,
                 notional, fee_lamports, 0, created_at_ms, provenance_json),
            )
        return {"status": "OPENED", "fill_id": fill_id, "notional_lamports": notional,
                "fee_lamports": fee_lamports, "total_debit_lamports": total}

    def _sell_in_transaction(
        self, db, position, *, idempotency_key: str, fill_price: int,
        fee_bps: Decimal, fraction: Decimal, created_at_ms: int,
        quote_provenance_json: str = "{}",
    ) -> dict:
        quantity = Decimal(position["quantity"])
        sold_quantity = quantity if fraction == 1 else quantity * fraction
        notional = int((sold_quantity * fill_price).to_integral_value(rounding=ROUND_DOWN))
        if notional <= 0 or notional > MAX_SQLITE_INTEGER:
            raise ValueError("exit notional is outside the SQLite lamport range")
        fee = int((Decimal(notional) * fee_bps / Decimal(10_000)).to_integral_value(rounding=ROUND_DOWN))
        proceeds = notional - fee
        if proceeds < 0 or proceeds > MAX_SQLITE_INTEGER:
            raise ValueError("exit proceeds are outside the SQLite lamport range")
        current_basis = int(position["cost_basis_lamports"])
        allocated_basis = current_basis if fraction == 1 else int(
            (Decimal(current_basis) * sold_quantity / quantity).to_integral_value(rounding=ROUND_DOWN)
        )
        realized = proceeds - allocated_basis
        remaining_quantity = quantity - sold_quantity
        remaining_basis = current_basis - allocated_basis
        fill_id = "solpaper-" + idempotency_key
        db.execute(
            "UPDATE solana_paper_account SET cash_lamports=cash_lamports+? WHERE id=1",
            (proceeds,),
        )
        if remaining_quantity <= 0:
            db.execute("DELETE FROM solana_paper_positions WHERE mint=?", (position["mint"],))
            db.execute("DELETE FROM solana_paper_position_state WHERE mint=?", (position["mint"],))
        else:
            db.execute(
                """UPDATE solana_paper_positions
                   SET quantity=?,cost_basis_lamports=? WHERE mint=?""",
                (str(remaining_quantity), remaining_basis, position["mint"]),
            )
        db.execute(
            """INSERT INTO solana_paper_fills
               (fill_id,idempotency_key,mint,side,quantity,price_lamports,
                notional_lamports,fee_lamports,realized_pnl_lamports,created_at_ms,quote_provenance_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (fill_id, idempotency_key, position["mint"], "SELL", str(sold_quantity),
             fill_price, notional, fee, realized, created_at_ms, quote_provenance_json),
        )
        return {
            "status": "CLOSED" if remaining_quantity <= 0 else "PARTIALLY_CLOSED",
            "fill_id": fill_id, "realized_pnl_lamports": realized,
            "proceeds_lamports": proceeds, "fee_lamports": fee,
            "quantity_sold": str(sold_quantity),
        }

    def close_position(
        self, *, idempotency_key: str, mint: str, price_lamports: int,
        fee_lamports: int, created_at_ms: int, context=None,
        fraction: object = Decimal("1"),
    ) -> dict:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        mint = canonical_mint(mint)
        _positive_int(price_lamports, "price_lamports")
        if isinstance(fee_lamports, bool) or not isinstance(fee_lamports, int) or not 0 <= fee_lamports <= MAX_SQLITE_INTEGER:
            raise ValueError("fee_lamports must be a non-negative SQLite integer")
        _positive_int(created_at_ms, "created_at_ms")
        part = _quantity(fraction)
        if part > 1:
            raise ValueError("fraction must be at most one")
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
            sold_qty = qty if part == 1 else qty * part
            notional = int((sold_qty * price_lamports).to_integral_value(rounding=ROUND_DOWN))
            if notional <= 0 or notional > MAX_SQLITE_INTEGER:
                raise ValueError("exit notional is outside the SQLite lamport range")
            if fee_lamports > notional:
                raise ValueError("exit fee exceeds simulated proceeds")
            # The manual close API supplies an explicit fee in lamports; preserve it
            # exactly rather than recomputing a basis-point estimate.
            proceeds = notional - fee_lamports
            current_basis = int(position["cost_basis_lamports"])
            allocated_basis = current_basis if part == 1 else int(
                (Decimal(current_basis) * sold_qty / qty).to_integral_value(rounding=ROUND_DOWN)
            )
            pnl = proceeds - allocated_basis
            remaining_qty = qty - sold_qty
            remaining_basis = current_basis - allocated_basis
            fill_id = "solpaper-" + idempotency_key
            db.execute(
                "UPDATE solana_paper_account SET cash_lamports=cash_lamports+? WHERE id=1",
                (proceeds,),
            )
            if remaining_qty <= 0:
                db.execute("DELETE FROM solana_paper_positions WHERE mint=?", (mint,))
                db.execute("DELETE FROM solana_paper_position_state WHERE mint=?", (mint,))
            else:
                db.execute(
                    "UPDATE solana_paper_positions SET quantity=?,cost_basis_lamports=? WHERE mint=?",
                    (str(remaining_qty), remaining_basis, mint),
                )
            db.execute(
                """INSERT INTO solana_paper_fills
                   (fill_id,idempotency_key,mint,side,quantity,price_lamports,
                    notional_lamports,fee_lamports,realized_pnl_lamports,created_at_ms)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (fill_id, idempotency_key, mint, "SELL", str(sold_qty), price_lamports,
                 notional, fee_lamports, pnl, created_at_ms),
            )
        return {"status": "CLOSED" if remaining_qty <= 0 else "PARTIALLY_CLOSED",
                "fill_id": fill_id, "realized_pnl_lamports": pnl,
                "proceeds_lamports": proceeds, "fee_lamports": fee_lamports}

    def process_market_price(
        self, *, mint: str, price_lamports: int, market_snapshot_id: int,
        fee_bps: object = 100, slippage_bps: object = 50,
        created_at_ms: int, context=None, quote_provenance: dict | None = None,
    ) -> list[dict]:
        mint = canonical_mint(mint)
        _positive_int(price_lamports, "price_lamports")
        _positive_int(market_snapshot_id, "market_snapshot_id")
        _positive_int(created_at_ms, "created_at_ms")
        fee_rate = self._decimal(fee_bps, "fee_bps")
        slip_rate = self._decimal(slippage_bps, "slippage_bps")
        if not 0 <= fee_rate <= 2_000 or not 0 <= slip_rate <= 2_000:
            raise ValueError("fee_bps and slippage_bps must be between 0 and 2000")
        fill_price = int((Decimal(price_lamports) * (1 - slip_rate / 10_000)).to_integral_value(rounding=ROUND_DOWN))
        if fill_price <= 0:
            raise ValueError("slippage-adjusted exit price is below one lamport")
        provenance_json = self._validate_quote_provenance(quote_provenance, mint)
        if context is not None:
            context.checkpoint(items=1)
        with self._transaction() as db:
            if context is not None:
                context.assert_fence_in_transaction(db)
            position = db.execute(
                "SELECT * FROM solana_paper_positions WHERE mint=?", (mint,)
            ).fetchone()
            if position is None:
                return []
            state = db.execute(
                "SELECT * FROM solana_paper_position_state WHERE mint=?", (mint,)
            ).fetchone()
            if state is None:
                db.execute(
                    "INSERT INTO solana_paper_position_state(mint,peak_price_lamports) VALUES(?,?)",
                    (mint, int(position["entry_price_lamports"])),
                )
                state = {"hit_2x": 0, "hit_3x": 0, "peak_price_lamports": position["entry_price_lamports"]}
            peak = max(int(state["peak_price_lamports"]), price_lamports)
            db.execute(
                "UPDATE solana_paper_position_state SET peak_price_lamports=? WHERE mint=?",
                (peak, mint),
            )
            entry = int(position["entry_price_lamports"])
            initial_stop = int((Decimal(entry) * (1 - self.stop_loss_pct)).to_integral_value(rounding=ROUND_DOWN))
            trailing_active = bool(state["hit_3x"])
            trailing_stop = int((Decimal(peak) * (1 - self.trailing_pct)).to_integral_value(rounding=ROUND_DOWN))
            if price_lamports <= initial_stop or (trailing_active and price_lamports <= trailing_stop):
                key = f"exit:{mint}:{market_snapshot_id}:stop"
                if db.execute("SELECT 1 FROM solana_paper_fills WHERE idempotency_key=?", (key,)).fetchone():
                    return []
                return [self._sell_in_transaction(
                    db, position, idempotency_key=key, fill_price=fill_price,
                    fee_bps=fee_rate, fraction=Decimal("1"), created_at_ms=created_at_ms,
                    quote_provenance_json=provenance_json,
                )]
            multiple = Decimal(price_lamports) / Decimal(entry)
            if multiple >= 3 and not state["hit_3x"]:
                key = f"exit:{mint}:{market_snapshot_id}:3x"
                result = self._sell_in_transaction(
                    db, position, idempotency_key=key, fill_price=fill_price,
                    fee_bps=fee_rate, fraction=Decimal("0.5"), created_at_ms=created_at_ms,
                    quote_provenance_json=provenance_json,
                )
                db.execute(
                    "UPDATE solana_paper_position_state SET hit_2x=1,hit_3x=1 WHERE mint=?",
                    (mint,),
                )
                return [result]
            if multiple >= 2 and not state["hit_2x"]:
                key = f"exit:{mint}:{market_snapshot_id}:2x"
                result = self._sell_in_transaction(
                    db, position, idempotency_key=key, fill_price=fill_price,
                    fee_bps=fee_rate, fraction=Decimal("0.5"), created_at_ms=created_at_ms,
                    quote_provenance_json=provenance_json,
                )
                db.execute(
                    "UPDATE solana_paper_position_state SET hit_2x=1 WHERE mint=?",
                    (mint,),
                )
                return [result]
            return []

    def snapshot(self, marks_lamports: dict[str, int] | None = None) -> dict:
        marks = marks_lamports or {}
        with self._connect() as db:
            account = db.execute("SELECT * FROM solana_paper_account WHERE id=1").fetchone()
            positions = db.execute(
                "SELECT * FROM solana_paper_positions ORDER BY opened_at_ms,mint"
            ).fetchall()
            realized_row = db.execute(
                "SELECT COALESCE(SUM(realized_pnl_lamports),0) AS total FROM solana_paper_fills"
            ).fetchone()
            fills = [dict(row) for row in db.execute(
                "SELECT * FROM solana_paper_fills ORDER BY created_at_ms DESC LIMIT 100"
            ).fetchall()]
        unrealized = 0
        position_rows = []
        unmarked_positions = []
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
            else:
                unmarked_positions.append(row["mint"])
            position_rows.append({
                **dict(row), "mark_price_lamports": mark,
                "market_value_lamports": market_value, "unrealized_pnl_lamports": pnl,
            })
        realized = int(realized_row["total"])
        for fill in fills:
            try:
                fill["quote_provenance"] = json.loads(fill.pop("quote_provenance_json") or "{}")
            except (TypeError, ValueError):
                fill["quote_provenance"] = {"error": "malformed_quote_provenance"}
        return {
            "unit": "lamports", "lamports_per_sol": LAMPORTS_PER_SOL,
            "starting_balance_lamports": int(account["starting_balance_lamports"]),
            "cash_lamports": int(account["cash_lamports"]),
            "open_positions": position_rows,
            "realized_pnl_lamports": realized,
            "unrealized_pnl_lamports": unrealized if not unmarked_positions else None,
            "equity_lamports": (
                int(account["cash_lamports"]) + sum(
                    int(x["market_value_lamports"]) for x in position_rows
                )
                if not unmarked_positions else None
            ),
            "equity_complete": not unmarked_positions,
            "unmarked_positions": unmarked_positions,
            "fills": [dict(x) for x in fills],
        }
