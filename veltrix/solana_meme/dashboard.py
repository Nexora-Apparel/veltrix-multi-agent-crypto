"""Read-only Solana paper-trading dashboard and analytics API.

The server exposes GET endpoints only. It opens the shared SQLite ledger in
read-only mode and never initializes, mutates, or executes trades.
"""
from __future__ import annotations

import hmac
from contextlib import closing
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .database import shared_database_path

LOGGER = logging.getLogger("veltrix.dashboard")


class PaperDashboardReader:
    def __init__(
        self,
        database_path: str | Path,
        *,
        clock_ms=None,
        max_market_age_ms: int = 120_000,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        if isinstance(max_market_age_ms, bool) or not isinstance(max_market_age_ms, int) or max_market_age_ms < 1:
            raise ValueError("max_market_age_ms must be a positive integer")
        self.clock_ms = clock_ms or (lambda: int(datetime.now(tz=timezone.utc).timestamp() * 1000))
        self.max_market_age_ms = max_market_age_ms

    def _connect(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise FileNotFoundError(f"shared VELTRIX SQLite database does not exist: {self.database_path}")
        uri = self.database_path.as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    def _require_tables(db: sqlite3.Connection, *tables: str) -> None:
        existing = {
            row["name"]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing = sorted(set(tables) - existing)
        if missing:
            raise RuntimeError("dashboard database is missing required tables: " + ", ".join(missing))

    @staticmethod
    def _number(value: Any, fallback: float = 0.0) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return fallback
        value = float(value)
        return value if value == value and abs(value) != float("inf") else fallback

    def _positions(self, db: sqlite3.Connection, now_ms: int) -> list[dict]:
        rows = db.execute("SELECT * FROM positions ORDER BY opened_at DESC").fetchall()
        positions = []
        for row in rows:
            position = dict(row)
            market = db.execute(
                """SELECT price_usd,retrieved_at_ms FROM solana_market_snapshots
                WHERE mint=? ORDER BY retrieved_at_ms DESC,id DESC LIMIT 1""",
                (position["token"],),
            ).fetchone()
            if market is None:
                mark = float(position["entry_price"])
                age = None
                mark_status = "missing"
            else:
                raw_mark = market["price_usd"]
                observed = market["retrieved_at_ms"]
                if (
                    isinstance(raw_mark, bool)
                    or not isinstance(raw_mark, (int, float))
                    or not (0 < float(raw_mark) < float("inf"))
                ):
                    mark = float(position["entry_price"])
                    mark_status = "invalid"
                else:
                    mark = float(raw_mark)
                    mark_status = "fresh"
                age = now_ms - int(observed) if isinstance(observed, int) else None
                if age is None or age < 0:
                    mark_status = "future_or_invalid_time"
                elif age > self.max_market_age_ms and mark_status == "fresh":
                    mark_status = "stale"
            position["mark_price"] = mark
            position["market_age_ms"] = age
            position["mark_status"] = mark_status
            position["market_value"] = float(position["quantity"]) * mark
            position["unrealized_pnl"] = position["market_value"] - float(position["cost_basis"])
            state = db.execute(
                "SELECT hit_2x,hit_3x,peak_price FROM position_state WHERE token=?",
                (position["token"],),
            ).fetchone()
            position["hit_2x"] = bool(state["hit_2x"]) if state else False
            position["hit_3x"] = bool(state["hit_3x"]) if state else False
            position["peak_price"] = float(state["peak_price"]) if state else float(position["entry_price"])
            positions.append(position)
        return positions

    def overview(self) -> dict:
        now = self.clock_ms()
        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise ValueError("dashboard clock must return a positive integer")
        with closing(self._connect()) as db:
            self._require_tables(
                db, "account", "positions", "position_state", "trades",
                "paper_execution_costs", "paper_consensus_decisions",
                "solana_market_snapshots", "solana_risk_evidence_reports",
                "solana_consensus_decisions",
            )
            account = db.execute("SELECT starting_balance,cash FROM account WHERE id=1").fetchone()
            if account is None:
                raise RuntimeError("paper account has not been initialized")
            positions = self._positions(db, now)
            realized = float(db.execute(
                "SELECT COALESCE(SUM(realized_pnl),0) FROM trades WHERE side='SELL'"
            ).fetchone()[0])
            closed = db.execute(
                "SELECT COUNT(*) AS fills, SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) AS wins, "
                "SUM(CASE WHEN realized_pnl<0 THEN 1 ELSE 0 END) AS losses FROM trades WHERE side='SELL'"
            ).fetchone()
            costs = db.execute(
                "SELECT COALESCE(SUM(fee_usd),0) AS fees, "
                "COALESCE(SUM(slippage_cost_usd),0) AS slippage FROM paper_execution_costs"
            ).fetchone()
            latest_market = db.execute(
                "SELECT MAX(retrieved_at_ms) FROM solana_market_snapshots"
            ).fetchone()[0]
            latest_risk = db.execute(
                "SELECT MAX(observed_at_ms) FROM solana_risk_evidence_reports"
            ).fetchone()[0]
            since = now - 86_400_000
            consensus_rows = db.execute(
                "SELECT payload_json FROM solana_consensus_decisions WHERE created_at_ms>=?",
                (since,),
            ).fetchall()
            funnel: dict[str, int] = {}
            for row in consensus_rows:
                try:
                    status = json.loads(row["payload_json"]).get("status", "UNKNOWN")
                except (TypeError, ValueError):
                    status = "MALFORMED"
                funnel[str(status)] = funnel.get(str(status), 0) + 1

        starting = float(account["starting_balance"])
        cash = float(account["cash"])
        exposure = sum(float(p["cost_basis"]) for p in positions)
        market_value = sum(float(p["market_value"]) for p in positions)
        unrealized = sum(float(p["unrealized_pnl"]) for p in positions)
        equity = cash + market_value
        wins = int(closed["wins"] or 0)
        losses = int(closed["losses"] or 0)
        fills = int(closed["fills"] or 0)
        return {
            "as_of_ms": now,
            "account": {
                "starting_balance_usd": starting,
                "cash_usd": cash,
                "equity_usd": equity,
                "realized_pnl_usd": realized,
                "unrealized_pnl_usd": unrealized,
                "total_pnl_usd": realized + unrealized,
                "portfolio_exposure_usd": exposure,
                "portfolio_exposure_pct": exposure / starting * 100 if starting else 0.0,
                "open_positions": len(positions),
            },
            "performance": {
                "closed_fills": fills,
                "wins": wins,
                "losses": losses,
                "win_rate_pct": wins / fills * 100 if fills else 0.0,
            },
            "costs": {
                "fees_usd": float(costs["fees"]),
                "slippage_cost_usd": float(costs["slippage"]),
            },
            "freshness": {
                "latest_market_observation_ms": latest_market,
                "latest_risk_observation_ms": latest_risk,
                "stale_open_positions": sum(p["mark_status"] != "fresh" for p in positions),
            },
            "consensus_24h": funnel,
        }

    def positions(self) -> list[dict]:
        now = self.clock_ms()
        with self._connect() as db:
            self._require_tables(db, "positions", "position_state", "solana_market_snapshots")
            return self._positions(db, now)

    def decisions(self, *, limit: int = 50) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 250:
            raise ValueError("limit must be between 1 and 250")
        with self._connect() as db:
            self._require_tables(db, "paper_consensus_decisions")
            rows = db.execute(
                """SELECT decision_id,token,consensus_status,execution_status,reason,
                created_at_ms,processed_at_ms,trade_id,notional_usd,mark_price,
                fill_price,fee_usd,slippage_cost_usd
                FROM paper_consensus_decisions
                ORDER BY processed_at_ms DESC,decision_id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def performance(self, *, days: int = 30) -> dict:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise ValueError("days must be between 1 and 365")
        now = self.clock_ms()
        since_seconds = now / 1000 - days * 86_400
        with self._connect() as db:
            self._require_tables(db, "trades", "paper_execution_costs")
            rows = db.execute(
                """SELECT date(created_at,'unixepoch') AS day,
                COUNT(*) AS sell_fills,
                SUM(realized_pnl) AS realized_pnl_usd,
                SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN realized_pnl<0 THEN 1 ELSE 0 END) AS losses
                FROM trades WHERE side='SELL' AND created_at>=?
                GROUP BY date(created_at,'unixepoch') ORDER BY day ASC""",
                (since_seconds,),
            ).fetchall()
            cost_rows = db.execute(
                """SELECT date(t.created_at,'unixepoch') AS day,
                SUM(c.fee_usd) AS fees_usd,SUM(c.slippage_cost_usd) AS slippage_cost_usd
                FROM paper_execution_costs c JOIN trades t ON t.id=c.trade_id
                WHERE t.created_at>=? GROUP BY date(t.created_at,'unixepoch')""",
                (since_seconds,),
            ).fetchall()
        costs_by_day = {row["day"]: row for row in cost_rows}
        cumulative = 0.0
        daily = []
        for row in rows:
            pnl = float(row["realized_pnl_usd"] or 0)
            cumulative += pnl
            costs = costs_by_day.get(row["day"])
            daily.append({
                "day": row["day"],
                "sell_fills": int(row["sell_fills"]),
                "realized_pnl_usd": pnl,
                "cumulative_realized_pnl_usd": cumulative,
                "wins": int(row["wins"] or 0),
                "losses": int(row["losses"] or 0),
                "fees_usd": float(costs["fees_usd"] or 0) if costs else 0.0,
                "slippage_cost_usd": float(costs["slippage_cost_usd"] or 0) if costs else 0.0,
            })
        return {"days": days, "daily": daily, "cumulative_realized_pnl_usd": cumulative}


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VELTRIX · Solana Paper Analytics</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#121a2d;--line:#24314b;--muted:#94a3b8;--text:#e5edf9;--green:#39d98a;--red:#ff6b75;--blue:#7aa8ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 Inter,system-ui,sans-serif}
main{max-width:1240px;margin:auto;padding:28px 18px 56px}header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:22px}
h1{font-size:22px;margin:0;letter-spacing:.02em}.sub{color:var(--muted);font-size:12px}.status{border:1px solid #32634c;color:var(--green);padding:5px 10px;border-radius:999px;font-size:11px;font-weight:700;letter-spacing:.06em}
.toolbar{display:flex;gap:8px;align-items:center;margin:14px 0 20px}.toolbar input{background:#0e1729;border:1px solid var(--line);border-radius:8px;color:var(--text);padding:9px 11px;min-width:210px}.toolbar button{background:#263c63;border:0;color:white;border-radius:8px;padding:9px 14px;cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:15px;min-width:0}.label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.value{font-size:22px;font-weight:700;margin-top:6px;overflow-wrap:anywhere}.positive{color:var(--green)}.negative{color:var(--red)}
.row{display:grid;grid-template-columns:1.4fr 1fr;gap:12px;margin-top:12px}.section-title{font-size:14px;margin:0 0 12px}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;white-space:nowrap;font-size:12px}th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:600}td.reason{white-space:normal;min-width:210px;max-width:340px}
.pill{display:inline-block;border-radius:999px;padding:3px 7px;font-size:10px;background:#1d2b44;color:#b9c9e2}.pill.good{color:var(--green);background:#123427}.pill.bad{color:var(--red);background:#3a1d2b}.muted{color:var(--muted)}.chart{display:flex;align-items:end;gap:6px;height:160px;padding:14px 2px 2px;border-bottom:1px solid var(--line)}.bar{flex:1;min-width:5px;border-radius:4px 4px 0 0;background:var(--blue);position:relative}.bar.neg{background:var(--red)}.chart-labels{display:flex;justify-content:space-between;color:var(--muted);font-size:10px;margin-top:6px}.foot{color:var(--muted);font-size:11px;margin-top:18px}
#error{color:var(--red);min-height:20px;font-size:12px}
@media(max-width:980px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}.row{grid-template-columns:1fr}}
@media(max-width:600px){main{padding:18px 12px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.value{font-size:18px}.toolbar{align-items:stretch;flex-wrap:wrap}.toolbar input{flex:1;min-width:160px}}
</style>
</head>
<body><main>
<header><div><h1>VELTRIX · Solana Paper Analytics</h1><div class="sub">Consensus-gated virtual portfolio · read-only dashboard</div></div><span class="status">SIMULATION ONLY · LIVE EXECUTION OFF</span></header>
<div class="toolbar"><input id="token" type="password" autocomplete="off" placeholder="Dashboard access token (if required)"><button id="refresh">Refresh</button><span id="updated" class="sub">Waiting for data</span></div><div id="error"></div>
<section class="grid">
<div class="card"><div class="label">Virtual equity</div><div class="value" id="equity">—</div></div>
<div class="card"><div class="label">Available cash</div><div class="value" id="cash">—</div></div>
<div class="card"><div class="label">Realized P&amp;L</div><div class="value" id="realized">—</div></div>
<div class="card"><div class="label">Unrealized P&amp;L</div><div class="value" id="unrealized">—</div></div>
<div class="card"><div class="label">Portfolio exposure</div><div class="value" id="exposure">—</div></div>
</section>
<div class="row">
<section class="card"><h2 class="section-title">Open positions</h2><div class="table-wrap"><table><thead><tr><th>Mint</th><th>Entry</th><th>Mark</th><th>Market value</th><th>Unrealized P&amp;L</th><th>Mark status</th></tr></thead><tbody id="positions"><tr><td colspan="6" class="muted">No positions loaded</td></tr></tbody></table></div></section>
<section class="card"><h2 class="section-title">Performance &amp; costs</h2><div class="grid" style="grid-template-columns:repeat(2,minmax(0,1fr))"><div><div class="label">Win rate</div><div class="value" id="winrate">—</div></div><div><div class="label">Closed fills</div><div class="value" id="fills">—</div></div><div><div class="label">Fees paid</div><div class="value" id="fees">—</div></div><div><div class="label">Slippage cost</div><div class="value" id="slippage">—</div></div></div><div class="label" style="margin-top:20px">Daily realized P&amp;L · last 30 days</div><div id="chart" class="chart"></div><div id="chartlabels" class="chart-labels"></div></section>
</div>
<section class="card" style="margin-top:12px"><h2 class="section-title">Consensus funnel · last 24 hours</h2><div id="funnel" class="muted">No consensus decisions loaded</div></section>
<section class="card" style="margin-top:12px"><h2 class="section-title">Recent paper decisions</h2><div class="table-wrap"><table><thead><tr><th>Processed</th><th>Mint</th><th>Consensus</th><th>Paper outcome</th><th>Notional</th><th>Reason</th></tr></thead><tbody id="decisions"><tr><td colspan="6" class="muted">No decisions loaded</td></tr></tbody></table></div></section>
<div class="foot" id="freshness">Market and risk freshness: — · Dashboard is read-only; all orders are virtual.</div>
</main>
<script>
const $=id=>document.getElementById(id), money=n=>new Intl.NumberFormat(undefined,{style:"currency",currency:"USD",maximumFractionDigits:2}).format(Number(n||0)), short=s=>s&&s.length>18?s.slice(0,7)+"…"+s.slice(-7):(s||"—"), date=n=>n?new Date(Number(n)).toLocaleString():"—";
function cell(row,value,cls){const td=document.createElement("td");td.textContent=value??"—";if(cls)td.className=cls;row.appendChild(td);return td}
async function get(path,token){const headers={};if(token)headers.Authorization="Bearer "+token;const r=await fetch(path,{headers,cache:"no-store"});if(!r.ok)throw new Error(r.status===401?"Access token required":("Request failed ("+r.status+")"));return r.json()}
async function refresh(){const token=$("token").value.trim();$("error").textContent="";try{
const [or,pr,dr,perfr]=await Promise.all([get("/api/overview",token),get("/api/positions",token),get("/api/decisions?limit=50",token),get("/api/performance?days=30",token)]);
const o=or.data,p=pr.data,d=dr.data,perf=perfr.data;
const a=o.account,stats=o.performance,cost=o.costs;
$("equity").textContent=money(a.equity_usd);$("cash").textContent=money(a.cash_usd);$("realized").textContent=money(a.realized_pnl_usd);$("unrealized").textContent=money(a.unrealized_pnl_usd);$("exposure").textContent=money(a.portfolio_exposure_usd)+" · "+Number(a.portfolio_exposure_pct).toFixed(1)+"%";
$("realized").className="value "+(a.realized_pnl_usd>=0?"positive":"negative");$("unrealized").className="value "+(a.unrealized_pnl_usd>=0?"positive":"negative");
$("winrate").textContent=Number(stats.win_rate_pct).toFixed(1)+"%";$("fills").textContent=stats.closed_fills;$("fees").textContent=money(cost.fees_usd);$("slippage").textContent=money(cost.slippage_cost_usd);
const pt=$("positions");pt.replaceChildren();if(!p.length){const tr=pt.insertRow();cell(tr,"No open positions");}for(const x of p){const tr=pt.insertRow();cell(tr,short(x.token));cell(tr,Number(x.entry_price).toPrecision(5));cell(tr,Number(x.mark_price).toPrecision(5));cell(tr,money(x.market_value));cell(tr,money(x.unrealized_pnl),x.unrealized_pnl>=0?"positive":"negative");const status=cell(tr,x.mark_status);status.className="pill "+(x.mark_status==="fresh"?"good":"bad");}
const dt=$("decisions");dt.replaceChildren();if(!d.length){const tr=dt.insertRow();cell(tr,"No paper decisions");}for(const x of d){const tr=dt.insertRow();cell(tr,date(x.processed_at_ms));cell(tr,short(x.token));cell(tr,x.consensus_status);const outcome=cell(tr,x.execution_status);outcome.className="pill "+(x.execution_status==="OPENED"?"good":x.execution_status==="BLOCKED"?"bad":"");cell(tr,x.notional_usd?money(x.notional_usd):"—");cell(tr,x.reason,"reason");}
const daily=perf.daily||[],chart=$("chart");chart.replaceChildren();const max=Math.max(0.01,...daily.map(x=>Math.abs(x.realized_pnl_usd)));for(const x of daily){const bar=document.createElement("div");bar.className="bar"+(x.realized_pnl_usd<0?" neg":"");bar.style.height=Math.max(3,Math.abs(x.realized_pnl_usd)/max*100)+"%";bar.title=x.day+" · "+money(x.realized_pnl_usd);chart.appendChild(bar);}const labels=$("chartlabels");labels.replaceChildren();if(daily.length){const first=document.createElement("span");first.textContent=daily[0].day;const last=document.createElement("span");last.textContent=daily[daily.length-1].day;labels.append(first,last);}
const funnel=$("funnel");funnel.replaceChildren();const funnelEntries=Object.entries(o.consensus_24h||{}).sort((x,y)=>y[1]-x[1]);if(!funnelEntries.length){funnel.textContent="No consensus decisions in the last 24 hours";}for(const [status,count] of funnelEntries){const row=document.createElement("div");row.className="funnel-row";const label=document.createElement("span");label.textContent=status;const value=document.createElement("strong");value.textContent=String(count);row.append(label,value);funnel.appendChild(row);}
$("freshness").textContent="Latest market: "+date(o.freshness.latest_market_observation_ms)+" · Latest risk: "+date(o.freshness.latest_risk_observation_ms)+" · Stale open positions: "+o.freshness.stale_open_positions+" · Read-only virtual ledger.";
$("updated").textContent="Updated "+new Date().toLocaleTimeString();
}catch(e){$("error").textContent=e.message;$("updated").textContent="Refresh failed";}}
$("refresh").addEventListener("click",refresh);refresh();setInterval(refresh,15000);
</script></body></html>"""


class PaperDashboardHandler(BaseHTTPRequestHandler):
    server: "PaperDashboardHTTPServer"

    def _headers(self, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()

    def _json(self, payload: dict, status: int = 200) -> None:
        self._headers("application/json; charset=utf-8", status)
        self.wfile.write(json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8"))

    def _authorized(self) -> bool:
        token = self.server.access_token
        if not token:
            return True
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + token
        return hmac.compare_digest(supplied, expected)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            self._json({"status": "ok", "mode": "read-only"})
            return
        if path == "/":
            self._headers("text/html; charset=utf-8")
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
            return
        if not path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return
        if not self._authorized():
            self._json({"error": "unauthorized"}, 401)
            return
        try:
            if path == "/api/overview":
                payload = self.server.reader.overview()
            elif path == "/api/positions":
                payload = self.server.reader.positions()
            elif path == "/api/decisions":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["50"])[0])
                payload = self.server.reader.decisions(limit=limit)
            elif path == "/api/performance":
                query = parse_qs(parsed.query)
                days = int(query.get("days", ["30"])[0])
                payload = self.server.reader.performance(days=days)
            else:
                self._json({"error": "not found"}, 404)
                return
            self._json({"data": payload})
        except (ValueError, TypeError) as exc:
            self._json({"error": str(exc)}, 400)
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            LOGGER.warning("dashboard read failed: %s", exc)
            self._json({"error": "dashboard data unavailable"}, 503)

    def do_POST(self) -> None:
        self._json({"error": "read-only dashboard; write methods are disabled"}, 405)

    def do_PUT(self) -> None:
        self.do_POST()

    def do_PATCH(self) -> None:
        self.do_POST()

    def do_DELETE(self) -> None:
        self.do_POST()

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)


class PaperDashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, reader: PaperDashboardReader, access_token: str = "") -> None:
        self.reader = reader
        self.access_token = access_token
        super().__init__(address, PaperDashboardHandler)


def build_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    database_path: str | Path | None = None,
    access_token: str | None = None,
) -> PaperDashboardHTTPServer:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("dashboard port must be between 0 and 65535")
    token = os.getenv("VELTRIX_DASHBOARD_TOKEN", "") if access_token is None else access_token
    token = token.strip()
    loopback_hosts = {"localhost", "127.0.0.1", "::1"}
    if host not in loopback_hosts and len(token) < 32:
        raise ValueError("remote dashboard binding requires VELTRIX_DASHBOARD_TOKEN of at least 32 characters")
    db_path = shared_database_path(database_path)
    max_age = int(os.getenv("VELTRIX_DASHBOARD_MARKET_MAX_AGE_MS", "120000"))
    reader = PaperDashboardReader(db_path, max_market_age_ms=max_age)
    return PaperDashboardHTTPServer((host, port), reader, token)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("VELTRIX_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    host = os.getenv("VELTRIX_DASHBOARD_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("VELTRIX_DASHBOARD_PORT", "8765"))
    except ValueError as exc:
        raise ValueError("VELTRIX_DASHBOARD_PORT must be an integer") from exc
    server = build_server(host=host, port=port)
    LOGGER.info("read-only paper analytics dashboard listening at http://%s:%s", host, server.server_address[1])
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOGGER.info("dashboard shutdown requested")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
