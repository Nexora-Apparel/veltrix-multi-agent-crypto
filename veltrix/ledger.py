"""Durable SQLite paper ledger. No exchange connectivity or real orders."""
import json
import sqlite3
import time
import uuid
from pathlib import Path

class PaperBroker:
    def __init__(self, db_path: str, starting_balance: float = 500.0, max_positions: int = 10, max_position_usd: float = 50.0):
        if starting_balance <= 0 or max_positions < 1 or max_position_usd <= 0:
            raise ValueError("Invalid paper account limits")
        self.db_path = str(db_path)
        self.max_positions, self.max_position_usd = max_positions, max_position_usd
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True) if Path(self.db_path).parent != Path(".") else None
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK(id=1), starting_balance REAL NOT NULL, cash REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS positions (token TEXT PRIMARY KEY, entry_price REAL NOT NULL, quantity REAL NOT NULL, cost_basis REAL NOT NULL, opened_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, token TEXT NOT NULL, side TEXT NOT NULL, price REAL NOT NULL, quantity REAL NOT NULL, notional REAL NOT NULL, realized_pnl REAL NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO account(id,starting_balance,cash) VALUES(1,?,?)", (starting_balance, starting_balance))

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def snapshot(self):
        with self._connect() as db:
            account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
            positions = db.execute("SELECT * FROM positions ORDER BY opened_at").fetchall()
            return {"starting_balance": account["starting_balance"], "cash": account["cash"], "open_positions": [dict(p) for p in positions]}

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
            db.execute("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), token, "BUY", price, qty, amount_usd, 0.0, reason, now))
            db.execute("INSERT INTO events(event,payload,created_at) VALUES(?,?,?)", ("POSITION_OPENED", json.dumps({"token":token,"price":price,"amount_usd":amount_usd,"reason":reason}), now))
        return {"token": token, "quantity": qty, "entry_price": price, "cost_basis": amount_usd}

    def close_fraction(self, token: str, price: float, fraction: float, reason: str):
        if price <= 0 or not 0 < fraction <= 1: raise ValueError("Invalid paper exit")
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pos = db.execute("SELECT * FROM positions WHERE token=?", (token,)).fetchone()
            if pos is None: raise ValueError("No open position for token")
            qty = pos["quantity"] * fraction
            basis = pos["cost_basis"] * fraction
            proceeds = qty * price
            pnl = proceeds - basis
            remaining_qty, remaining_basis = pos["quantity"] - qty, pos["cost_basis"] - basis
            db.execute("UPDATE account SET cash=cash+? WHERE id=1", (proceeds,))
            if remaining_qty <= 1e-12:
                db.execute("DELETE FROM positions WHERE token=?", (token,))
            else:
                db.execute("UPDATE positions SET quantity=?,cost_basis=? WHERE token=?", (remaining_qty, remaining_basis, token))
            db.execute("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), token, "SELL", price, qty, proceeds, pnl, reason, now))
            db.execute("INSERT INTO events(event,payload,created_at) VALUES(?,?,?)", ("POSITION_PARTIALLY_CLOSED" if remaining_qty > 1e-12 else "POSITION_CLOSED", json.dumps({"token":token,"price":price,"quantity":qty,"realized_pnl":pnl,"reason":reason}), now))
        return {"token":token,"quantity_sold":qty,"proceeds":proceeds,"realized_pnl":pnl,"remaining_quantity":remaining_qty}

    def totals(self):
        with self._connect() as db:
            row = db.execute("SELECT COALESCE(SUM(realized_pnl),0) AS pnl, COUNT(*) AS n FROM trades WHERE side='SELL'").fetchone()
            return {"realized_pnl": row["pnl"], "closed_sell_fills": row["n"]}
