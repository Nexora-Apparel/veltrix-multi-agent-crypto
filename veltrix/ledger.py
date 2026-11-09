"""Durable SQLite paper ledger. No exchange connectivity or real orders."""
import json
import sqlite3
import time
import uuid
from pathlib import Path

class PaperBroker:
    def __init__(self, db_path: str, starting_balance: float = 500.0, max_positions: int = 10, max_position_usd: float = 50.0, stop_loss_pct: float = 0.06, trailing_pct: float = 0.06):
        if starting_balance <= 0 or max_positions < 1 or max_position_usd <= 0 or not 0 < stop_loss_pct < 1 or not 0 < trailing_pct < 1:
            raise ValueError("Invalid paper account limits")
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
            """)
            db.execute("INSERT OR IGNORE INTO account(id,starting_balance,cash) VALUES(1,?,?)", (starting_balance, starting_balance))

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def snapshot(self, prices=None):
        prices = prices or {}
        with self._connect() as db:
            account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
            positions = [dict(p) for p in db.execute("SELECT * FROM positions ORDER BY opened_at").fetchall()]
            unrealized = 0.0
            for p in positions:
                mark = float(prices.get(p["token"], p["entry_price"]))
                p["mark_price"] = mark
                p["market_value"] = p["quantity"] * mark
                p["unrealized_pnl"] = p["market_value"] - p["cost_basis"]
                unrealized += p["unrealized_pnl"]
            return {"starting_balance": account["starting_balance"], "cash": account["cash"],
                "open_positions": positions, "unrealized_pnl": unrealized,
                "equity": account["cash"] + sum(p["market_value"] for p in positions)}

    def open_position(self, token: str, price: float, amount_usd: float, reason: str):
        if not token or price <= 0 or amount_usd <= 0 or amount_usd > self.max_position_usd:
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
    def _sell_in_transaction(db, pos, price, fraction, reason, now):
        qty = pos["quantity"] * fraction
        basis = pos["cost_basis"] * fraction
        proceeds = qty * price
        pnl = proceeds - basis
        remaining_qty, remaining_basis = pos["quantity"] - qty, pos["cost_basis"] - basis
        db.execute("UPDATE account SET cash=cash+? WHERE id=1", (proceeds,))
        if remaining_qty <= 1e-12:
            db.execute("DELETE FROM positions WHERE token=?", (pos["token"],))
            db.execute("DELETE FROM position_state WHERE token=?", (pos["token"],))
        else:
            db.execute("UPDATE positions SET quantity=?,cost_basis=? WHERE token=?", (remaining_qty, remaining_basis, pos["token"]))
        db.execute("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), pos["token"], "SELL", price, qty, proceeds, pnl, reason, now))
        db.execute("INSERT INTO events(event,payload,created_at) VALUES(?,?,?)", ("POSITION_PARTIALLY_CLOSED" if remaining_qty > 1e-12 else "POSITION_CLOSED", json.dumps({"token":pos["token"],"price":price,"quantity":qty,"realized_pnl":pnl,"reason":reason}), now))
        return {"token":pos["token"],"quantity_sold":qty,"proceeds":proceeds,"realized_pnl":pnl,"remaining_quantity":remaining_qty,"reason":reason}

    def close_fraction(self, token: str, price: float, fraction: float, reason: str):
        if price <= 0 or not 0 < fraction <= 1: raise ValueError("Invalid paper exit")
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pos = db.execute("SELECT * FROM positions WHERE token=?", (token,)).fetchone()
            if pos is None: raise ValueError("No open position for token")
            return self._sell_in_transaction(db, pos, price, fraction, reason, now)

    def process_market_price(self, token: str, price: float):
        """Apply one-time 2x/3x partial exits and a 6% initial/trailing stop."""
        if not token or price <= 0:
            raise ValueError("Token and positive mark price are required")
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
                actions.append(self._sell_in_transaction(db, pos, price, 1.0, "trailing_stop" if trailing_active else "stop_loss", now))
                return actions
            multiple = price / pos["entry_price"]
            if multiple >= 3 and not state["hit_3x"]:
                # If the feed jumps over 2x between observations, do not fabricate
                # a historical 2x fill. Mark that milestone as passed and execute
                # the currently observable 3x partial only.
                result = self._sell_in_transaction(db, pos, price, 0.5, "take_profit_3x", now)
                actions.append(result)
                db.execute("UPDATE position_state SET hit_2x=1, hit_3x=1 WHERE token=?", (token,))
            elif multiple >= 2 and not state["hit_2x"]:
                result = self._sell_in_transaction(db, pos, price, 0.5, "take_profit_2x", now)
                actions.append(result)
                db.execute("UPDATE position_state SET hit_2x=1 WHERE token=?", (token,))
        return actions

    def recent_market_observations(self, token, limit=20):
        """Return prior persisted market observations, newest first."""
        if not token or limit < 1:
            return []
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
