"""Durable SQLite paper ledger. No exchange connectivity or real orders."""
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

class PaperBroker:
    def __init__(self, db_path: str, starting_balance: float = 500.0, max_positions: int = 10, max_position_usd: float = 50.0, stop_loss_pct: float = 0.06, trailing_pct: float = 0.06):
        for name, value in (
            ("starting_balance", starting_balance),
            ("max_position_usd", max_position_usd),
            ("stop_loss_pct", stop_loss_pct),
            ("trailing_pct", trailing_pct),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if (
            starting_balance <= 0
            or isinstance(max_positions, bool)
            or not isinstance(max_positions, int)
            or max_positions < 1
            or max_position_usd <= 0
            or not 0 < stop_loss_pct < 1
            or not 0 < trailing_pct < 1
        ):
            raise ValueError("Invalid paper account limits")
        if not isinstance(db_path, (str, Path)) or not str(db_path).strip() or str(db_path) == ":memory:":
            raise ValueError("PaperBroker requires a persistent SQLite database path")
        self.db_path = str(db_path)
        self.max_positions, self.max_position_usd = max_positions, max_position_usd
        self.stop_loss_pct, self.trailing_pct = stop_loss_pct, trailing_pct
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True) if Path(self.db_path).parent != Path(".") else None
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK(id=1), starting_balance REAL NOT NULL, cash REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS positions (token TEXT PRIMARY KEY, entry_price REAL NOT NULL, quantity REAL NOT NULL, cost_basis REAL NOT NULL, opened_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, token TEXT NOT NULL, side TEXT NOT NULL, price REAL NOT NULL, quantity REAL NOT NULL, notional REAL NOT NULL, realized_pnl REAL NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS position_state (
                    token TEXT PRIMARY KEY, hit_2x INTEGER NOT NULL DEFAULT 0,
                    hit_3x INTEGER NOT NULL DEFAULT 0, peak_price REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_consensus_decisions (
                    decision_id TEXT PRIMARY KEY,
                    token TEXT NOT NULL,
                    consensus_status TEXT NOT NULL,
                    execution_status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    processed_at_ms INTEGER NOT NULL,
                    trade_id TEXT,
                    notional_usd REAL,
                    mark_price REAL,
                    fill_price REAL,
                    fee_usd REAL NOT NULL DEFAULT 0,
                    slippage_cost_usd REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_paper_decisions_token_time
                    ON paper_consensus_decisions(token, processed_at_ms DESC);
                CREATE TABLE IF NOT EXISTS paper_execution_costs (
                    trade_id TEXT PRIMARY KEY,
                    decision_id TEXT,
                    side TEXT NOT NULL,
                    mark_price REAL NOT NULL,
                    fill_price REAL NOT NULL,
                    fee_usd REAL NOT NULL,
                    slippage_cost_usd REAL NOT NULL
                );
            """)
            db.execute("INSERT OR IGNORE INTO account(id,starting_balance,cash) VALUES(1,?,?)", (starting_balance, starting_balance))

    @contextmanager
    def _connect(self):
        """Open, commit/rollback, and always close one SQLite connection."""
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _finite_number(value, name, *, positive=False):
        import math
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"{name} must be a finite number")
        if positive and value <= 0:
            raise ValueError(f"{name} must be positive")
        return float(value)

    @classmethod
    def _validate_execution_costs(cls, fee_bps, slippage_bps):
        fee = cls._finite_number(fee_bps, "fee_bps")
        slippage = cls._finite_number(slippage_bps, "slippage_bps")
        if not 0 <= fee <= 2_000 or not 0 <= slippage <= 2_000:
            raise ValueError("fee_bps and slippage_bps must be between 0 and 2000")
        return fee, slippage

    def has_consensus_decision(self, decision_id):
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise ValueError("decision_id is required")
        with self._connect() as db:
            row = db.execute(
                "SELECT execution_status FROM paper_consensus_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        return row["execution_status"] if row else None

    def record_consensus_outcome(
        self, decision_id, token, consensus_status, execution_status, reason,
        *, created_at_ms, processed_at_ms=None,
    ):
        """Record a non-entry consensus outcome once; decision IDs are idempotency keys."""
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise ValueError("decision_id is required")
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token is required")
        if not isinstance(consensus_status, str) or not consensus_status.strip():
            raise ValueError("consensus_status is required")
        if execution_status not in {"NOT_ELIGIBLE", "SKIPPED", "BLOCKED"}:
            raise ValueError("unsupported paper execution outcome")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required")
        if isinstance(created_at_ms, bool) or not isinstance(created_at_ms, int) or created_at_ms <= 0:
            raise ValueError("created_at_ms must be a positive integer")
        processed = int(time.time() * 1000) if processed_at_ms is None else processed_at_ms
        if isinstance(processed, bool) or not isinstance(processed, int) or processed <= 0:
            raise ValueError("processed_at_ms must be a positive integer")
        with self._connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO paper_consensus_decisions
                (decision_id,token,consensus_status,execution_status,reason,
                 created_at_ms,processed_at_ms)
                VALUES(?,?,?,?,?,?,?)""",
                (decision_id, token, consensus_status, execution_status,
                 reason[:500], created_at_ms, processed),
            )
            if cursor.rowcount != 1:
                return {"status": "DUPLICATE", "decision_id": decision_id}
            db.execute(
                "INSERT INTO events(event,payload,created_at) VALUES(?,?,?)",
                ("PAPER_DECISION_SKIPPED", json.dumps({
                    "decision_id": decision_id, "token": token,
                    "consensus_status": consensus_status,
                    "execution_status": execution_status, "reason": reason[:500],
                }), processed / 1000),
            )
        return {"status": execution_status, "decision_id": decision_id, "reason": reason}

    def open_position_from_consensus(
        self, decision_id, token, mark_price, amount_usd, reason, *,
        consensus_status="CONSENSUS_APPROVED", paper_review_eligible=None,
        execution_authorized=None, created_at_ms,
        fee_bps=100, slippage_bps=50, processed_at_ms=None,
        expected_market_snapshot_id=None, expected_risk_evidence_hash=None,
        max_portfolio_exposure_pct=1.0,
    ):
        """Atomically open a paper position and consume one approved consensus ID."""
        import math
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise ValueError("decision_id is required")
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token is required")
        if consensus_status != "CONSENSUS_APPROVED":
            raise ValueError("only CONSENSUS_APPROVED decisions may open paper positions")
        if paper_review_eligible is not True:
            raise ValueError("paper_review_eligible must be explicitly true")
        if execution_authorized is not False:
            raise ValueError("execution_authorized must remain false")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required")
        if isinstance(created_at_ms, bool) or not isinstance(created_at_ms, int) or created_at_ms <= 0:
            raise ValueError("created_at_ms must be a positive integer")
        mark = self._finite_number(mark_price, "mark_price", positive=True)
        amount = self._finite_number(amount_usd, "amount_usd", positive=True)
        fee_rate, slippage_rate = self._validate_execution_costs(fee_bps, slippage_bps)
        exposure_limit_pct = self._finite_number(
            max_portfolio_exposure_pct, "max_portfolio_exposure_pct", positive=True
        )
        if exposure_limit_pct > 1:
            raise ValueError("max_portfolio_exposure_pct must not exceed 1.0")
        processed = int(time.time() * 1000) if processed_at_ms is None else processed_at_ms
        if isinstance(processed, bool) or not isinstance(processed, int) or processed <= 0:
            raise ValueError("processed_at_ms must be a positive integer")
        if amount > self.max_position_usd:
            raise ValueError("paper position exceeds the configured per-position limit")
        fill = mark * (1 + slippage_rate / 10_000)
        fee = amount * fee_rate / 10_000
        quantity = amount / fill
        slippage_cost = quantity * (fill - mark)
        total_debit = amount + fee
        trade_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT execution_status FROM paper_consensus_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if existing is not None:
                return {"status": "DUPLICATE", "decision_id": decision_id}
            count = int(db.execute("SELECT COUNT(*) FROM positions").fetchone()[0])
            account = db.execute("SELECT starting_balance,cash FROM account WHERE id=1").fetchone()
            cash = float(account["cash"])
            starting_balance = float(account["starting_balance"])
            current_exposure = float(db.execute(
                "SELECT COALESCE(SUM(cost_basis),0) FROM positions"
            ).fetchone()[0])
            blocked_reason = None
            if expected_market_snapshot_id is not None or expected_risk_evidence_hash is not None:
                if (
                    isinstance(expected_market_snapshot_id, bool)
                    or not isinstance(expected_market_snapshot_id, int)
                    or expected_market_snapshot_id < 1
                    or not isinstance(expected_risk_evidence_hash, str)
                    or not expected_risk_evidence_hash
                ):
                    raise ValueError("expected market snapshot ID and risk evidence hash are required together")
                latest_decision = db.execute(
                    """SELECT decision_id FROM solana_consensus_decisions
                    WHERE mint=? ORDER BY created_at_ms DESC, id DESC LIMIT 1""",
                    (token,),
                ).fetchone()
                latest_market = db.execute(
                    """SELECT id FROM solana_market_snapshots
                    WHERE mint=? ORDER BY retrieved_at_ms DESC, id DESC LIMIT 1""",
                    (token,),
                ).fetchone()
                latest_risk = db.execute(
                    """SELECT evidence_hash FROM solana_risk_evidence_reports
                    WHERE mint=? ORDER BY observed_at_ms DESC, id DESC LIMIT 1""",
                    (token,),
                ).fetchone()
                if latest_decision is None or latest_decision["decision_id"] != decision_id:
                    blocked_reason = "a newer consensus decision superseded this approval"
                elif latest_market is None or latest_market["id"] != expected_market_snapshot_id:
                    blocked_reason = "market snapshot changed during paper-entry validation"
                elif latest_risk is None or latest_risk["evidence_hash"] != expected_risk_evidence_hash:
                    blocked_reason = "security evidence changed during paper-entry validation"
            if blocked_reason is None and current_exposure + total_debit > starting_balance * exposure_limit_pct + 1e-9:
                blocked_reason = "paper portfolio exposure cap would be exceeded"
            elif blocked_reason is None and count >= self.max_positions:
                blocked_reason = "maximum open paper positions reached"
            elif blocked_reason is None and db.execute("SELECT 1 FROM positions WHERE token=?", (token,)).fetchone():
                blocked_reason = "token already has an open paper position"
            elif blocked_reason is None and cash < total_debit:
                blocked_reason = "insufficient paper cash after entry fee"
            if blocked_reason:
                db.execute(
                    """INSERT INTO paper_consensus_decisions
                    (decision_id,token,consensus_status,execution_status,reason,
                     created_at_ms,processed_at_ms,notional_usd,mark_price,fill_price,fee_usd,slippage_cost_usd)
                    VALUES(?,?,?,'BLOCKED',?,?,?,?,?,?,?,?)""",
                    (decision_id, token, consensus_status, blocked_reason,
                     created_at_ms, processed, amount, mark, fill, fee, slippage_cost),
                )
                db.execute(
                    "INSERT INTO events(event,payload,created_at) VALUES(?,?,?)",
                    ("PAPER_ENTRY_BLOCKED", json.dumps({
                        "decision_id": decision_id, "token": token, "reason": blocked_reason,
                    }), processed / 1000),
                )
                return {"status": "BLOCKED", "decision_id": decision_id, "reason": blocked_reason}
            db.execute("UPDATE account SET cash=cash-? WHERE id=1", (total_debit,))
            db.execute(
                "INSERT INTO positions(token,entry_price,quantity,cost_basis,opened_at) VALUES(?,?,?,?,?)",
                (token, fill, quantity, total_debit, processed / 1000),
            )
            db.execute("INSERT INTO position_state(token,peak_price) VALUES(?,?)", (token, fill))
            db.execute(
                "INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)",
                (trade_id, token, "BUY", fill, quantity, amount, 0.0, reason[:500], processed / 1000),
            )
            db.execute(
                """INSERT INTO paper_execution_costs
                (trade_id,decision_id,side,mark_price,fill_price,fee_usd,slippage_cost_usd)
                VALUES(?,?,?,?,?,?,?)""",
                (trade_id, decision_id, "BUY", mark, fill, fee, slippage_cost),
            )
            db.execute(
                """INSERT INTO paper_consensus_decisions
                (decision_id,token,consensus_status,execution_status,reason,
                 created_at_ms,processed_at_ms,trade_id,notional_usd,mark_price,fill_price,fee_usd,slippage_cost_usd)
                VALUES(?,?,?,'OPENED',?,?,?,?,?,?,?,?,?)""",
                (decision_id, token, consensus_status, reason[:500],
                 created_at_ms, processed, trade_id, amount, mark, fill, fee, slippage_cost),
            )
            db.execute(
                "INSERT INTO events(event,payload,created_at) VALUES(?,?,?)",
                ("PAPER_CONSENSUS_EXECUTED", json.dumps({
                    "decision_id": decision_id, "token": token,
                    "mark_price": mark, "fill_price": fill,
                    "notional_usd": amount, "quantity": quantity,
                    "fee_usd": fee, "slippage_cost_usd": slippage_cost,
                }), processed / 1000),
            )
        return {
            "status": "OPENED", "decision_id": decision_id, "trade_id": trade_id,
            "token": token, "quantity": quantity, "mark_price": mark,
            "fill_price": fill, "notional_usd": amount, "fee_usd": fee,
            "slippage_cost_usd": slippage_cost, "cost_basis": total_debit,
        }

    def paper_decisions(self, *, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM paper_consensus_decisions ORDER BY processed_at_ms DESC, decision_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def execution_cost_totals(self):
        with self._connect() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(fee_usd),0) AS fees, COALESCE(SUM(slippage_cost_usd),0) AS slippage FROM paper_execution_costs"
            ).fetchone()
        return {"fees_usd": float(row["fees"]), "slippage_cost_usd": float(row["slippage"])}

    def snapshot(self, prices=None):
        prices = {} if prices is None else prices
        if not isinstance(prices, dict):
            raise TypeError("prices must be a mapping of token to positive mark price")
        with self._connect() as db:
            account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
            positions = [dict(p) for p in db.execute("SELECT * FROM positions ORDER BY opened_at").fetchall()]
            unrealized = 0.0
            for p in positions:
                raw_mark = prices.get(p["token"], p["entry_price"])
                if (
                    isinstance(raw_mark, bool)
                    or not isinstance(raw_mark, (int, float))
                    or not math.isfinite(float(raw_mark))
                    or raw_mark <= 0
                ):
                    raise ValueError("mark prices must be finite and positive")
                mark = float(raw_mark)
                p["mark_price"] = mark
                p["market_value"] = p["quantity"] * mark
                p["unrealized_pnl"] = p["market_value"] - p["cost_basis"]
                unrealized += p["unrealized_pnl"]
            exposure = sum(float(p["cost_basis"]) for p in positions)
            exposure_pct = exposure / float(account["starting_balance"]) * 100 if account["starting_balance"] else 0.0
            return {"starting_balance": account["starting_balance"], "cash": account["cash"],
                "open_positions": positions, "unrealized_pnl": unrealized,
                "portfolio_exposure_usd": exposure,
                "portfolio_exposure_pct": exposure_pct,
                "equity": account["cash"] + sum(p["market_value"] for p in positions)}

    def open_position(self, token: str, price: float, amount_usd: float, reason: str):
        if (
            not isinstance(token, str) or not token.strip()
            or isinstance(price, bool) or not isinstance(price, (int, float))
            or not math.isfinite(float(price)) or price <= 0
            or isinstance(amount_usd, bool) or not isinstance(amount_usd, (int, float))
            or not math.isfinite(float(amount_usd)) or amount_usd <= 0
            or amount_usd > self.max_position_usd
            or not isinstance(reason, str) or not reason.strip()
        ):
            raise ValueError("Invalid or over-limit paper entry")
        now, qty = time.time(), amount_usd / price
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
            cash = db.execute("SELECT cash FROM account WHERE id=1").fetchone()[0]
            if count >= self.max_positions: raise ValueError("Maximum open positions reached")
            if db.execute("SELECT 1 FROM positions WHERE token=?", (token,)).fetchone(): raise ValueError("Token already has an open position")
            if cash < amount_usd: raise ValueError("Insufficient paper cash")
            db.execute("UPDATE account SET cash=cash-? WHERE id=1", (amount_usd,))
            db.execute("INSERT INTO positions VALUES(?,?,?,?,?)", (token, price, qty, amount_usd, now))
            db.execute("INSERT INTO position_state(token,peak_price) VALUES(?,?)", (token, price))
            db.execute("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), token, "BUY", price, qty, amount_usd, 0.0, reason, now))
            db.execute("INSERT INTO events(event,payload,created_at) VALUES(?,?,?)", ("POSITION_OPENED", json.dumps({"token":token,"price":price,"amount_usd":amount_usd,"reason":reason}), now))
        return {"token": token, "quantity": qty, "entry_price": price, "cost_basis": amount_usd}

    @staticmethod
    def _sell_in_transaction(
        db, pos, mark_price, fill_price, fraction, reason, now,
        fee_bps=0.0, decision_id=None,
    ):
        qty = pos["quantity"] * fraction
        basis = pos["cost_basis"] * fraction
        gross_proceeds = qty * fill_price
        fee = gross_proceeds * fee_bps / 10_000
        proceeds = gross_proceeds - fee
        slippage_cost = qty * abs(mark_price - fill_price)
        pnl = proceeds - basis
        remaining_qty, remaining_basis = pos["quantity"] - qty, pos["cost_basis"] - basis
        db.execute("UPDATE account SET cash=cash+? WHERE id=1", (proceeds,))
        if remaining_qty <= 1e-12:
            db.execute("DELETE FROM positions WHERE token=?", (pos["token"],))
            db.execute("DELETE FROM position_state WHERE token=?", (pos["token"],))
        else:
            db.execute("UPDATE positions SET quantity=?,cost_basis=? WHERE token=?", (remaining_qty, remaining_basis, pos["token"]))
        trade_id = str(uuid.uuid4())
        db.execute("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)", (trade_id, pos["token"], "SELL", fill_price, qty, gross_proceeds, pnl, reason, now))
        db.execute(
            """INSERT INTO paper_execution_costs
            (trade_id,decision_id,side,mark_price,fill_price,fee_usd,slippage_cost_usd)
            VALUES(?,?,?,?,?,?,?)""",
            (trade_id, decision_id, "SELL", mark_price, fill_price, fee, slippage_cost),
        )
        db.execute(
            "INSERT INTO events(event,payload,created_at) VALUES(?,?,?)",
            ("POSITION_PARTIALLY_CLOSED" if remaining_qty > 1e-12 else "POSITION_CLOSED",
             json.dumps({
                 "token":pos["token"], "mark_price":mark_price, "fill_price":fill_price,
                 "quantity":qty, "gross_proceeds":gross_proceeds, "fee_usd":fee,
                 "slippage_cost_usd":slippage_cost, "net_proceeds":proceeds,
                 "realized_pnl":pnl, "reason":reason,
             }), now),
        )
        return {
            "token":pos["token"], "quantity_sold":qty, "proceeds":proceeds,
            "gross_proceeds":gross_proceeds, "fee_usd":fee,
            "slippage_cost_usd":slippage_cost, "mark_price":mark_price,
            "fill_price":fill_price, "realized_pnl":pnl,
            "remaining_quantity":remaining_qty, "reason":reason,
        }

    def close_fraction(
        self, token: str, price: float, fraction: float, reason: str, *,
        fee_bps: float = 0.0, slippage_bps: float = 0.0, decision_id=None,
    ):
        if (
            not isinstance(token, str) or not token.strip()
            or isinstance(price, bool) or not isinstance(price, (int, float))
            or not math.isfinite(float(price)) or price <= 0
            or isinstance(fraction, bool) or not isinstance(fraction, (int, float))
            or not math.isfinite(float(fraction)) or not 0 < fraction <= 1
            or not isinstance(reason, str) or not reason.strip()
        ):
            raise ValueError("Invalid paper exit")
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pos = db.execute("SELECT * FROM positions WHERE token=?", (token,)).fetchone()
            if pos is None: raise ValueError("No open position for token")
            fee, slippage = self._validate_execution_costs(fee_bps, slippage_bps)
            fill = float(price) * (1 - slippage / 10_000)
            return self._sell_in_transaction(
                db, pos, float(price), fill, float(fraction), reason, now,
                fee_bps=fee, decision_id=decision_id,
            )

    def process_market_price(
        self, token: str, price: float, *, fee_bps: float = 0.0,
        slippage_bps: float = 0.0, decision_id=None,
    ):
        """Apply one-time 2x/3x partial exits and a 6% initial/trailing stop."""
        if (
            not isinstance(token, str) or not token.strip()
            or isinstance(price, bool) or not isinstance(price, (int, float))
            or not math.isfinite(float(price)) or price <= 0
        ):
            raise ValueError("Token and finite positive mark price are required")
        fee, slippage = self._validate_execution_costs(fee_bps, slippage_bps)
        fill_price = float(price) * (1 - slippage / 10_000)
        now = time.time()
        actions = []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pos = db.execute("SELECT * FROM positions WHERE token=?", (token,)).fetchone()
            if pos is None:
                return actions
            state = db.execute("SELECT * FROM position_state WHERE token=?", (token,)).fetchone()
            if state is None:
                db.execute("INSERT INTO position_state(token,peak_price) VALUES(?,?)", (token, pos["entry_price"]))
                state = {"hit_2x":0,"hit_3x":0,"peak_price":pos["entry_price"]}
            peak = max(float(state["peak_price"]), price)
            db.execute("UPDATE position_state SET peak_price=? WHERE token=?", (peak, token))
            initial_stop = pos["entry_price"] * (1 - self.stop_loss_pct)
            trailing_active = bool(state["hit_3x"])
            trailing_stop = peak * (1 - self.trailing_pct)
            if price <= initial_stop or (trailing_active and price <= trailing_stop):
                actions.append(self._sell_in_transaction(db, pos, float(price), fill_price, 1.0, "trailing_stop" if trailing_active else "stop_loss", now, fee_bps=fee, decision_id=decision_id))
                return actions
            multiple = price / pos["entry_price"]
            if multiple >= 3 and not state["hit_3x"]:
                # If the feed jumps over 2x between observations, do not fabricate
                # a historical 2x fill. Mark that milestone as passed and execute
                # the currently observable 3x partial only.
                result = self._sell_in_transaction(db, pos, float(price), fill_price, 0.5, "take_profit_3x", now, fee_bps=fee, decision_id=decision_id)
                actions.append(result)
                db.execute("UPDATE position_state SET hit_2x=1, hit_3x=1 WHERE token=?", (token,))
            elif multiple >= 2 and not state["hit_2x"]:
                result = self._sell_in_transaction(db, pos, float(price), fill_price, 0.5, "take_profit_2x", now, fee_bps=fee, decision_id=decision_id)
                actions.append(result)
                db.execute("UPDATE position_state SET hit_2x=1 WHERE token=?", (token,))
        return actions

    def recent_market_observations(self, token, limit=20):
        """Return prior persisted market observations, newest first."""
        if (
            not isinstance(token, str) or not token.strip()
            or isinstance(limit, bool) or not isinstance(limit, int)
            or limit < 1
        ):
            raise ValueError("token and positive integer limit are required")
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM events WHERE event='MARKET_OBSERVATION' ORDER BY id DESC LIMIT ?",
                (int(limit) * 20,)
            ).fetchall()
        observations = []
        for row in rows:
            try:
                item = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if item.get("token") == token:
                observations.append(item)
                if len(observations) >= limit:
                    break
        return observations

    def record_agent_decision(self, token, decision, signals, reason):
        payload = {"token": token, "decision": decision, "signals": signals, "reason": reason}
        with self._connect() as db:
            db.execute("INSERT INTO events(event,payload,created_at) VALUES(?,?,?)", ("AGENT_DECISION", json.dumps(payload), time.time()))

    def record_discovery_candidate(self, candidate):
        payload = {"chain": candidate.chain, "token": candidate.token_address, "source": candidate.source, "discovered_at_ms": candidate.discovered_at_ms}
        with self._connect() as db:
            db.execute("INSERT INTO events(event,payload,created_at) VALUES(?,?,?)", ("DISCOVERY_CANDIDATE", json.dumps(payload), time.time()))

    def record_launch_intelligence(self, chain, token_address, result):
        """Persist launch-origin and composite-gate outcome without credentials."""
        payload = {
            "chain": chain, "token": token_address,
            "result": result,
        }
        with self._connect() as db:
            db.execute(
                "INSERT INTO events(event,payload,created_at) VALUES(?,?,?)",
                ("LAUNCH_INTELLIGENCE", json.dumps(payload, default=str), time.time()),
            )

    def record_developer_evidence(self, chain, token_address, audit):
        """Persist accepted and rejected developer-evidence decisions."""
        payload = {
            "chain": chain,
            "token": token_address,
            "audit": audit,
        }
        with self._connect() as db:
            db.execute(
                "INSERT INTO events(event,payload,created_at) VALUES(?,?,?)",
                ("DEVELOPER_EVIDENCE_AUDIT", json.dumps(payload, default=str), time.time()),
            )

    def record_market_event(self, event):
        payload = {"chain": event.chain, "token": event.token_address, "pair": event.pair_address, "symbol": event.symbol, "price_usd": event.price_usd, "liquidity_usd": event.liquidity_usd, "volume_h1_usd": event.volume_h1_usd, "price_change_m5_pct": event.price_change_m5_pct, "observed_at_ms": event.observed_at_ms, "source": event.source}
        with self._connect() as db:
            db.execute("INSERT INTO events(event,payload,created_at) VALUES(?,?,?)", ("MARKET_OBSERVATION", json.dumps(payload), time.time()))

    def totals(self):
        with self._connect() as db:
            row = db.execute("SELECT COALESCE(SUM(realized_pnl),0) AS pnl, COUNT(*) AS n FROM trades WHERE side='SELL'").fetchone()
            return {"realized_pnl": row["pnl"], "closed_sell_fills": row["n"]}

    def session_report(self):
        with self._connect() as db:
            sells = db.execute("SELECT token,SUM(realized_pnl) AS pnl,COUNT(*) AS fills FROM trades WHERE side='SELL' GROUP BY token ORDER BY pnl").fetchall()
            closed = [dict(r) for r in sells]
            wins = [r for r in closed if r["pnl"] > 0]
            losses = [r for r in closed if r["pnl"] < 0]
            total = sum(r["pnl"] for r in closed)
            account = db.execute("SELECT starting_balance,cash FROM account WHERE id=1").fetchone()
            return {"starting_balance": account["starting_balance"], "cash": account["cash"],
                "realized_pnl": total, "closed_positions": len(closed),
                "win_rate_pct": (len(wins) / len(closed) * 100) if closed else 0.0,
                "best_trade": dict(max(closed, key=lambda r:r["pnl"])) if closed else None,
                "worst_trade": dict(min(closed, key=lambda r:r["pnl"])) if closed else None,
                "open_positions": db.execute("SELECT COUNT(*) FROM positions").fetchone()[0]}
