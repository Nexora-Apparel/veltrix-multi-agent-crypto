"""Read-only Phase 08 dashboard and analytics server.

Uses only Python's standard library. All database connections use SQLite
read-only/query-only mode. The HTTP surface exposes GET endpoints only.
"""
from __future__ import annotations

import json
import logging
import math
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .database import connect_database, is_remote_database, row_factory_for
from .solana_meme.database import shared_database_path

LOGGER = logging.getLogger("veltrix.dashboard")
ASSET_DIR = Path(__file__).with_name("dashboard_assets")


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    result = float(value)
    return result if math.isfinite(result) else default


def _iso_from_seconds(value: object) -> str | None:
    stamp = _number(value, -1)
    if stamp < 0:
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()


def _iso_from_ms(value: object) -> str | None:
    stamp = _number(value, -1)
    if stamp < 0:
        return None
    return datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).isoformat()


class _ReadOnlyRemoteConnection:
    """Application-enforced SELECT-only guard for Turso's HTTP DB-API.

    The Turso serverless driver rejects SQLite PRAGMA query_only. This wrapper
    therefore permits only a single SELECT statement and exposes no commit or
    transaction methods to dashboard code. Use a separate Turso read-only token
    as an additional deployment boundary where available.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @staticmethod
    def _validate(sql: str) -> None:
        if not isinstance(sql, str):
            raise sqlite3.OperationalError("dashboard database is read-only")
        statement = sql.lstrip()
        while statement.startswith("--") or statement.startswith("/*"):
            if statement.startswith("--"):
                newline = statement.find("\n")
                statement = "" if newline < 0 else statement[newline + 1:].lstrip()
            else:
                end = statement.find("*/", 2)
                statement = "" if end < 0 else statement[end + 2:].lstrip()
        if not statement[:6].upper() == "SELECT" or (
            len(statement) > 6 and statement[6].isalnum()
        ):
            raise sqlite3.OperationalError("dashboard database is read-only")
        without_trailing = statement.rstrip()
        if ";" in without_trailing.rstrip(";"):
            raise sqlite3.OperationalError("multiple SQL statements are not allowed")

    def execute(self, sql: str, parameters: Any = ()):
        self._validate(sql)
        return self._connection.execute(sql, parameters)

    def executemany(self, *_args, **_kwargs):
        raise sqlite3.OperationalError("dashboard database is read-only")

    def executescript(self, *_args, **_kwargs):
        raise sqlite3.OperationalError("dashboard database is read-only")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


class DashboardRepository:
    """Small read-only query layer over the shared Phase 03–07 SQLite database."""

    def __init__(self, database_path: str | Path, *, max_market_age_ms: int = 120_000):
        raw_database = str(database_path).strip()
        if not raw_database:
            raise ValueError("dashboard database path or URL is required")
        self._remote_database = is_remote_database(raw_database)
        self.database_path = raw_database if self._remote_database else Path(raw_database).expanduser().resolve()
        if isinstance(max_market_age_ms, bool) or not isinstance(max_market_age_ms, int) or max_market_age_ms < 1:
            raise ValueError("max_market_age_ms must be a positive integer")
        self.max_market_age_ms = max_market_age_ms

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._remote_database:
            db = connect_database(
                str(self.database_path), timeout=3,
                auth_token=os.getenv("TURSO_DASHBOARD_AUTH_TOKEN") or None,
            )
            db.row_factory = row_factory_for(str(self.database_path))
            guarded = _ReadOnlyRemoteConnection(db)
            try:
                yield guarded
            finally:
                guarded.close()
            return
        if not self.database_path.is_file():
            raise FileNotFoundError("shared VELTRIX SQLite database does not exist")
        uri = self.database_path.as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _has_table(db: sqlite3.Connection, table: str) -> bool:
        return db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None

    @staticmethod
    def _rows(db: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict]:
        return [dict(row) for row in db.execute(query, params).fetchall()]

    def health(self) -> dict:
        with self._connect() as db:
            tables = {
                row["name"]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
        required = {"account", "positions", "trades", "paper_consensus_decisions"}
        worker = None
        if "solana_pipeline_health" in tables:
            with self._connect() as db:
                row = db.execute("""
                    SELECT status,last_started_at_ms,last_finished_at_ms,
                           last_success_at_ms,last_duration_ms,last_job_id,
                           last_error,consecutive_failures,updated_at_ms
                    FROM solana_pipeline_health WHERE id=1
                """).fetchone()
                if row is not None:
                    worker = dict(row)
                    reported_status = worker["status"]
                    updated_at_ms = int(worker["updated_at_ms"])
                    age_seconds = max(0.0, (int(time.time() * 1000) - updated_at_ms) / 1000)
                    try:
                        stale_after_seconds = float(os.getenv("VELTRIX_PIPELINE_STALE_AFTER_SECONDS", "300"))
                    except ValueError:
                        stale_after_seconds = 300.0
                    if not math.isfinite(stale_after_seconds) or stale_after_seconds < 30:
                        stale_after_seconds = 300.0
                    stale = age_seconds > stale_after_seconds
                    worker["reported_status"] = reported_status
                    worker["health_age_seconds"] = round(age_seconds, 1)
                    worker["stale_after_seconds"] = stale_after_seconds
                    worker["stale"] = stale
                    if stale:
                        worker["status"] = "stale"
                    worker["healthy"] = reported_status == "healthy" and not stale
                    worker["last_success_at"] = _iso_from_ms(worker.pop("last_success_at_ms"))
                    worker["last_started_at"] = _iso_from_ms(worker.pop("last_started_at_ms"))
                    worker["last_finished_at"] = _iso_from_ms(worker.pop("last_finished_at_ms"))
        candle_refresh: dict = {"status": "not_configured", "total_jobs": 0}
        if "solana_candle_refresh_jobs" in tables:
            with self._connect() as db:
                row = db.execute("""
                    SELECT COUNT(*) AS total_jobs,
                           SUM(CASE WHEN status='retry' THEN 1 ELSE 0 END) AS retry_jobs,
                           SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END) AS processing_jobs,
                           COALESCE(SUM(consecutive_failures),0) AS consecutive_failures,
                           MAX(last_success_at_ms) AS last_success_at_ms,
                           MAX(updated_at_ms) AS updated_at_ms
                    FROM solana_candle_refresh_jobs
                """).fetchone()
            candle_refresh = dict(row)
            candle_refresh["status"] = (
                "idle" if int(candle_refresh["total_jobs"] or 0) == 0
                else "degraded" if int(candle_refresh["retry_jobs"] or 0) > 0
                else "active"
            )
            candle_refresh["last_success_at"] = _iso_from_ms(candle_refresh.pop("last_success_at_ms"))
            candle_refresh["last_updated_at"] = _iso_from_ms(candle_refresh.get("updated_at_ms"))
            candle_refresh["last_success_age_seconds"] = (
                None if candle_refresh.get("last_success_at") is None
                else max(0.0, (int(time.time() * 1000) - int(row["last_success_at_ms"])) / 1000)
            )

        return {
            "ok": True,
            "database": "connected",
            "database_read_only": True,
            "paper_ledger_initialized": "account" in tables,
            "available_tables": sorted(tables),
            "missing_paper_tables": sorted(required - tables),
            "worker": worker or {"status": "not_configured", "healthy": False},
            "candle_refresh": candle_refresh,
            "live_execution": False,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    def _latest_market(self, db: sqlite3.Connection, mint: str) -> dict | None:
        if not self._has_table(db, "solana_market_snapshots"):
            return None
        row = db.execute(
            """SELECT * FROM solana_market_snapshots
               WHERE mint=? ORDER BY retrieved_at_ms DESC, id DESC LIMIT 1""",
            (mint,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["quality_flags"] = json.loads(result.pop("quality_flags_json") or "[]")
        except (TypeError, ValueError):
            result["quality_flags"] = ["malformed_quality_flags"]
        return result

    def sol_paper_wallet(self) -> dict:
        """Return read-only lamport-native wallet state without inventing marks."""
        from decimal import Decimal

        with self._connect() as db:
            required = {
                "solana_paper_account", "solana_paper_positions",
                "solana_paper_fills", "solana_paper_position_state",
            }
            tables = {
                row["name"] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not required.issubset(tables):
                return {
                    "configured": False, "unit": "SOL",
                    "reason": "native SOL paper ledger is not initialized",
                    "live_execution": False,
                }
            account = db.execute(
                "SELECT starting_balance_lamports,cash_lamports,created_at_ms "
                "FROM solana_paper_account WHERE id=1"
            ).fetchone()
            positions = self._rows(
                db, "SELECT * FROM solana_paper_positions ORDER BY opened_at_ms,mint"
            )
            fills = self._rows(
                db, "SELECT * FROM solana_paper_fills ORDER BY created_at_ms DESC LIMIT 50"
            )
            realized = db.execute(
                "SELECT COALESCE(SUM(realized_pnl_lamports),0) AS total "
                "FROM solana_paper_fills"
            ).fetchone()
            totals = db.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN side='BUY' THEN 1 ELSE 0 END) AS entries,
                          SUM(CASE WHEN side='SELL' THEN 1 ELSE 0 END) AS exits
                   FROM solana_paper_fills"""
            ).fetchone()
        if account is None:
            return {"configured": False, "unit": "SOL", "reason": "wallet account row is missing", "live_execution": False}
        lamports_per_sol = Decimal(1_000_000_000)
        to_sol = lambda value: format(Decimal(int(value)) / lamports_per_sol, "f")
        for fill in fills:
            try:
                fill["quote_provenance"] = json.loads(fill.pop("quote_provenance_json") or "{}")
            except (TypeError, ValueError):
                fill["quote_provenance"] = {"error": "malformed quote provenance"}
        realized_lamports = int(realized["total"] or 0)
        cash = int(account["cash_lamports"])
        return {
            "configured": True,
            "unit": "SOL",
            "lamports_per_sol": 1_000_000_000,
            "starting_balance_lamports": int(account["starting_balance_lamports"]),
            "starting_balance_sol": to_sol(account["starting_balance_lamports"]),
            "cash_lamports": cash,
            "cash_sol": to_sol(cash),
            "realized_pnl_lamports": realized_lamports,
            "realized_pnl_sol": to_sol(realized_lamports),
            "open_positions": positions,
            "open_position_count": len(positions),
            "unmarked_positions": [position["mint"] for position in positions],
            "equity_lamports": None if positions else cash,
            "equity_sol": None if positions else to_sol(cash),
            "equity_complete": not positions,
            "fill_count": int(totals["total"] or 0),
            "entry_fills": int(totals["entries"] or 0),
            "exit_fills": int(totals["exits"] or 0),
            "recent_fills": fills,
            "created_at_ms": int(account["created_at_ms"]),
            "live_execution": False,
        }

    def candles(self, *, mint: str, pair_address: str, interval: str, limit: int = 500) -> dict:
        """Return stored provider candles; never synthesize missing OHLCV data."""
        from .solana_meme.candle_data import INTERVAL_SECONDS
        from .solana_meme.candidates import canonical_mint

        canonical_mint(mint)
        if not isinstance(pair_address, str) or not pair_address.strip() or len(pair_address) > 128:
            raise ValueError("pair_address is required")
        if interval not in INTERVAL_SECONDS:
            raise ValueError("unsupported candle interval")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
            raise ValueError("limit must be between 1 and 5000")
        with self._connect() as db:
            if not self._has_table(db, "solana_ohlcv_candles"):
                rows = []
            else:
                rows = self._rows(db, """
                    SELECT mint,pair_address,interval,open_time_ms,open,high,low,
                           close,volume,source,observed_at_ms,is_closed
                    FROM solana_ohlcv_candles
                    WHERE mint=? AND pair_address=? AND interval=?
                    ORDER BY open_time_ms DESC LIMIT ?
                """, (mint,pair_address,interval,limit))
        rows.reverse()
        now_ms = int(time.time() * 1000)
        latest = rows[-1] if rows else None
        age_ms = now_ms - int(latest["observed_at_ms"]) if latest else None
        return {
            "mint": mint, "pair_address": pair_address, "interval": interval,
            "candles": rows, "count": len(rows),
            "source": latest["source"] if latest else None,
            "observed_at_ms": latest["observed_at_ms"] if latest else None,
            "age_ms": age_ms,
            "freshness": "fresh" if age_ms is not None and 0 <= age_ms <= 120_000 else
                         "stale" if age_ms is not None else "unavailable",
            "synthetic": False,
        }

    def overview(self) -> dict:
        now_ms = int(time.time() * 1000)
        with self._connect() as db:
            account = db.execute("SELECT * FROM account WHERE id=1").fetchone() if self._has_table(db, "account") else None
            positions = self._rows(db, "SELECT * FROM positions ORDER BY opened_at") if self._has_table(db, "positions") else []
            sells = self._rows(
                db, "SELECT realized_pnl, created_at FROM trades WHERE side='SELL' ORDER BY created_at"
            ) if self._has_table(db, "trades") else []
            buys = db.execute("SELECT COUNT(*) FROM trades WHERE side='BUY'").fetchone()[0] if self._has_table(db, "trades") else 0
            costs = db.execute(
                "SELECT COALESCE(SUM(fee_usd),0) AS fees, COALESCE(SUM(slippage_cost_usd),0) AS slippage FROM paper_execution_costs"
            ).fetchone() if self._has_table(db, "paper_execution_costs") else {"fees": 0, "slippage": 0}

            starting_balance = _number(account["starting_balance"]) if account else 0.0
            cash = _number(account["cash"]) if account else 0.0
            market_value = 0.0
            unrealized_pnl = 0.0
            exposure = 0.0
            stale_positions = 0
            for position in positions:
                mint = position["token"]
                market = self._latest_market(db, mint)
                entry = _number(position["entry_price"])
                mark = entry
                observed = market.get("retrieved_at_ms") if market else None
                if (
                    market
                    and isinstance(observed, int)
                    and not isinstance(observed, bool)
                    and 0 <= now_ms - observed <= self.max_market_age_ms
                    and _number(market.get("price_usd")) > 0
                ):
                    mark = _number(market["price_usd"])
                else:
                    stale_positions += 1
                value = _number(position["quantity"]) * mark
                market_value += value
                basis = _number(position["cost_basis"])
                unrealized_pnl += value - basis
                exposure += basis

            realized_pnl = sum(_number(row["realized_pnl"]) for row in sells)
            wins = sum(1 for row in sells if _number(row["realized_pnl"]) > 0)
            losses = sum(1 for row in sells if _number(row["realized_pnl"]) < 0)
            closed_fills = len(sells)
            equity = cash + market_value
            fees = _number(costs["fees"])
            slippage = _number(costs["slippage"])

            counts = {}
            count_tables = {
                "candidates": "solana_launch_candidates",
                "market_snapshots": "solana_market_snapshots",
                "risk_reports": "solana_risk_evidence_reports",
                "consensus_decisions": "solana_consensus_decisions",
                "paper_decisions": "paper_consensus_decisions",
            }
            for key, table in count_tables.items():
                counts[key] = int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) if self._has_table(db, table) else 0

            latest = {}
            for key, table, column in (
                ("market", "solana_market_snapshots", "retrieved_at_ms"),
                ("risk", "solana_risk_evidence_reports", "observed_at_ms"),
                ("consensus", "solana_consensus_decisions", "created_at_ms"),
                ("paper", "paper_consensus_decisions", "processed_at_ms"),
            ):
                if self._has_table(db, table):
                    value = db.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
                    latest[key] = _iso_from_ms(value)
                else:
                    latest[key] = None

        return {
            "account": {
                "starting_balance_usd": starting_balance,
                "cash_usd": cash,
                "equity_usd": equity,
                "realized_pnl_usd": realized_pnl,
                "unrealized_pnl_usd": unrealized_pnl,
                "total_return_pct": ((equity - starting_balance) / starting_balance * 100) if starting_balance else 0.0,
                "open_positions": len(positions),
                "portfolio_exposure_usd": exposure,
                "portfolio_exposure_pct": (exposure / starting_balance * 100) if starting_balance else 0.0,
                "stale_or_unmarked_positions": stale_positions,
            },
            "performance": {
                "exit_fills": closed_fills,
                "winning_exit_fills": wins,
                "losing_exit_fills": losses,
                "win_rate_pct": (wins / closed_fills * 100) if closed_fills else 0.0,
                "realized_pnl_usd": realized_pnl,
                "fees_usd": fees,
                "slippage_cost_usd": slippage,
                "buy_fills": int(buys),
            },
            "pipeline": counts,
            "latest_updates": latest,
            "read_only": True,
            "live_execution": False,
        }

    def positions(self) -> list[dict]:
        now_ms = int(time.time() * 1000)
        with self._connect() as db:
            if not self._has_table(db, "positions"):
                return []
            rows = self._rows(db, "SELECT * FROM positions ORDER BY opened_at")
            output = []
            for row in rows:
                market = self._latest_market(db, row["token"])
                entry = _number(row["entry_price"])
                observed = market.get("retrieved_at_ms") if market else None
                fresh = (
                    isinstance(observed, int)
                    and not isinstance(observed, bool)
                    and 0 <= now_ms - observed <= self.max_market_age_ms
                    and _number(market.get("price_usd")) > 0
                )
                mark = _number(market["price_usd"]) if fresh else entry
                market_value = _number(row["quantity"]) * mark
                pnl = market_value - _number(row["cost_basis"])
                output.append({
                    "token": row["token"],
                    "entry_price": entry,
                    "mark_price": mark,
                    "quantity": _number(row["quantity"]),
                    "cost_basis_usd": _number(row["cost_basis"]),
                    "market_value_usd": market_value,
                    "unrealized_pnl_usd": pnl,
                    "unrealized_pnl_pct": pnl / _number(row["cost_basis"]) * 100 if _number(row["cost_basis"]) else 0.0,
                    "opened_at": _iso_from_seconds(row["opened_at"]),
                    "mark_status": "FRESH" if fresh else ("STALE" if market else "MISSING"),
                    "market_observed_at": _iso_from_ms(observed),
                    "symbol": market.get("symbol") if market else None,
                    "name": market.get("name") if market else None,
                })
        return output

    def trades(self, *, limit: int = 100) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as db:
            if not self._has_table(db, "trades"):
                return []
            if self._has_table(db, "paper_execution_costs"):
                rows = self._rows(db, """
                    SELECT t.*, c.mark_price, c.fee_usd, c.slippage_cost_usd
                    FROM trades t LEFT JOIN paper_execution_costs c ON c.trade_id=t.id
                    ORDER BY t.created_at DESC LIMIT ?
                """, (limit,))
            else:
                rows = self._rows(db, """
                    SELECT t.*, NULL AS mark_price, 0 AS fee_usd, 0 AS slippage_cost_usd
                    FROM trades t ORDER BY t.created_at DESC LIMIT ?
                """, (limit,))
        return [{
            **row,
            "created_at_iso": _iso_from_seconds(row["created_at"]),
            "price": _number(row["price"]),
            "quantity": _number(row["quantity"]),
            "notional": _number(row["notional"]),
            "realized_pnl": _number(row["realized_pnl"]),
            "fee_usd": _number(row.get("fee_usd")),
            "slippage_cost_usd": _number(row.get("slippage_cost_usd")),
        } for row in rows]

    def decisions(self, *, limit: int = 100) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as db:
            if not self._has_table(db, "paper_consensus_decisions"):
                return []
            rows = self._rows(db, """
                SELECT * FROM paper_consensus_decisions
                ORDER BY processed_at_ms DESC, decision_id DESC LIMIT ?
            """, (limit,))
        return [{
            **row,
            "created_at_iso": _iso_from_ms(row["created_at_ms"]),
            "processed_at_iso": _iso_from_ms(row["processed_at_ms"]),
        } for row in rows]

    def consensus(self, *, limit: int = 50) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 250:
            raise ValueError("limit must be between 1 and 250")
        with self._connect() as db:
            if not self._has_table(db, "solana_consensus_decisions"):
                return []
            rows = self._rows(db, """
                SELECT mint, created_at_ms, decision_id, payload_json
                FROM solana_consensus_decisions
                ORDER BY created_at_ms DESC, id DESC LIMIT ?
            """, (limit,))
        output = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                payload = {}
            output.append({
                "mint": row["mint"],
                "decision_id": row["decision_id"],
                "created_at": _iso_from_ms(row["created_at_ms"]),
                "status": payload.get("status"),
                "support_count": payload.get("support_count"),
                "supporting_agents": payload.get("supporting_agents", []),
                "paper_review_eligible": payload.get("paper_review_eligible") is True,
                "execution_authorized": payload.get("execution_authorized") is True,
                "reasons": payload.get("reasons", []),
            })
        return output

    def markets(self, *, limit: int = 50) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 250:
            raise ValueError("limit must be between 1 and 250")
        with self._connect() as db:
            if not self._has_table(db, "solana_market_snapshots"):
                return []
            rows = self._rows(db, """
                SELECT m.* FROM solana_market_snapshots m
                JOIN (
                    SELECT mint, MAX(id) AS latest_id
                    FROM solana_market_snapshots GROUP BY mint
                ) latest ON latest.latest_id=m.id
                ORDER BY m.retrieved_at_ms DESC LIMIT ?
            """, (limit,))
        output = []
        for row in rows:
            try:
                flags = json.loads(row.pop("quality_flags_json") or "[]")
            except (TypeError, ValueError):
                flags = ["malformed_quality_flags"]
            output.append({
                **row,
                "quality_flags": flags,
                "retrieved_at": _iso_from_ms(row["retrieved_at_ms"]),
            })
        return output

    def equity_curve(self, *, limit: int = 250) -> dict:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as db:
            account = db.execute("SELECT starting_balance FROM account WHERE id=1").fetchone() if self._has_table(db, "account") else None
            starting = _number(account["starting_balance"]) if account else 0.0
            if not self._has_table(db, "trades"):
                return {"basis": "realized exit fills only", "points": [{"time": None, "equity_usd": starting, "realized_pnl_usd": 0.0}]}
            rows = self._rows(db, """
                SELECT created_at, realized_pnl FROM trades
                WHERE side='SELL' ORDER BY created_at ASC LIMIT ?
            """, (limit,))
        cumulative = 0.0
        points = [{"time": None, "equity_usd": starting, "realized_pnl_usd": 0.0}]
        for row in rows:
            cumulative += _number(row["realized_pnl"])
            points.append({
                "time": _iso_from_seconds(row["created_at"]),
                "equity_usd": starting + cumulative,
                "realized_pnl_usd": cumulative,
            })
        return {"basis": "realized exit fills only; excludes open-position mark-to-market", "points": points}

    def costs(self) -> dict:
        with self._connect() as db:
            if not self._has_table(db, "paper_execution_costs"):
                return {"fees_usd": 0.0, "slippage_cost_usd": 0.0, "by_side": []}
            totals = db.execute("""
                SELECT COALESCE(SUM(fee_usd),0) AS fees,
                       COALESCE(SUM(slippage_cost_usd),0) AS slippage
                FROM paper_execution_costs
            """).fetchone()
            sides = self._rows(db, """
                SELECT side, COALESCE(SUM(fee_usd),0) AS fees,
                       COALESCE(SUM(slippage_cost_usd),0) AS slippage,
                       COUNT(*) AS fills
                FROM paper_execution_costs GROUP BY side ORDER BY side
            """)
        return {
            "fees_usd": _number(totals["fees"]),
            "slippage_cost_usd": _number(totals["slippage"]),
            "by_side": sides,
        }


def make_handler(repository: DashboardRepository, *, api_token: str = ""):
    class Handler(BaseHTTPRequestHandler):
        server_version = "VELTRIXDashboard/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOGGER.info("dashboard %s - %s", self.address_string(), fmt % args)

        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:")
            self.end_headers()

        def _authorized(self) -> bool:
            if not api_token:
                return True
            header = self.headers.get("Authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else ""
            return secrets.compare_digest(supplied, api_token)

        def _json(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(raw))
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            path = urlsplit(self.path).path
            if path == "/api/pool":
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                try:
                    from .solana_meme.pool_resolver import (
                        PoolResolutionUnavailable, SolanaPoolResolver,
                    )
                    mint = query.get("mint", [""])[0]
                    resolved = SolanaPoolResolver().resolve(mint)
                    self._json(200, resolved.as_dict())
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                except PoolResolutionUnavailable as exc:
                    self._json(422, {"error": str(exc), "pool": None})
                except Exception:
                    LOGGER.exception("Solana pool resolution failed")
                    self._json(503, {"error": "pool resolution unavailable"})
                return
            if path == "/api/candles":
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                try:
                    mint = query.get("mint", [""])[0]
                    pair = query.get("pair", [""])[0]
                    interval = query.get("interval", ["1m"])[0]
                    raw_limit = query.get("limit", ["500"])[0]
                    if not raw_limit.isdigit():
                        raise ValueError("limit must be an integer")
                    self._json(200, repository.candles(
                        mint=mint, pair_address=pair, interval=interval,
                        limit=int(raw_limit),
                    ))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                except (sqlite3.Error, FileNotFoundError):
                    LOGGER.exception("candle read failed")
                    self._json(503, {"error": "candle data unavailable"})
                return
            if path == "/":
                self._asset("index.html", "text/html; charset=utf-8")
                return
            if path == "/assets/dashboard.css":
                self._asset("dashboard.css", "text/css; charset=utf-8")
                return
            if path == "/assets/dashboard.js":
                self._asset("dashboard.js", "application/javascript; charset=utf-8")
                return
            routes = {
                "/api/health": repository.health,
                "/api/sol-paper-wallet": repository.sol_paper_wallet,
                "/api/overview": repository.overview,
                "/api/positions": repository.positions,
                "/api/trades": repository.trades,
                "/api/decisions": repository.decisions,
                "/api/consensus": repository.consensus,
                "/api/markets": repository.markets,
                "/api/equity-curve": repository.equity_curve,
                "/api/costs": repository.costs,
            }
            route = routes.get(path)
            if route is None:
                self._json(404, {"error": "not found"})
                return
            try:
                self._json(200, route())
            except FileNotFoundError as exc:
                self._json(503, {"error": str(exc)})
            except (sqlite3.Error, ValueError) as exc:
                LOGGER.exception("dashboard read failed")
                self._json(503, {"error": "dashboard data unavailable", "detail": type(exc).__name__})

        def _asset(self, name: str, content_type: str) -> None:
            path = ASSET_DIR / name
            try:
                raw = path.read_bytes()
            except OSError:
                self._json(404, {"error": "dashboard asset not found"})
                return
            self._headers(200, content_type, len(raw))
            self.wfile.write(raw)

        def do_POST(self) -> None:
            self._json(405, {"error": "read-only dashboard; write methods are disabled"})

        def do_PUT(self) -> None:
            self._json(405, {"error": "read-only dashboard; write methods are disabled"})

        def do_DELETE(self) -> None:
            self._json(405, {"error": "read-only dashboard; write methods are disabled"})

    return Handler


def build_server(
    *,
    database_path: str | Path | None = None,
    host: str | None = None,
    port: int | None = None,
    api_token: str | None = None,
) -> ThreadingHTTPServer:
    resolved_host = host or os.getenv("VELTRIX_DASHBOARD_HOST", "127.0.0.1")
    resolved_port = int(port if port is not None else os.getenv("VELTRIX_DASHBOARD_PORT", "8765"))
    token = os.getenv("VELTRIX_DASHBOARD_TOKEN", "") if api_token is None else api_token
    if not isinstance(resolved_host, str) or not resolved_host.strip():
        raise ValueError("dashboard host is required")
    if isinstance(resolved_port, bool) or not isinstance(resolved_port, int) or not 0 <= resolved_port <= 65535:
        raise ValueError("dashboard port must be between 0 and 65535")
    if not isinstance(token, str):
        raise ValueError("dashboard API token must be a string")
    token = token.strip()
    if len(token) < 32:
        raise ValueError("dashboard binding requires VELTRIX_DASHBOARD_TOKEN of at least 32 characters")
    repository = DashboardRepository(shared_database_path(database_path))
    server = ThreadingHTTPServer(
        (resolved_host, resolved_port),
        make_handler(repository, api_token=token),
    )
    server.daemon_threads = True
    server.dashboard_repository = repository
    return server


def main() -> None:
    logging.basicConfig(
        level=os.getenv("VELTRIX_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    server = build_server()
    host, port = server.server_address[:2]
    LOGGER.info(
        "VELTRIX read-only dashboard listening at http://%s:%s; writes and live execution disabled",
        host,
        port,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOGGER.info("dashboard shutdown requested")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
