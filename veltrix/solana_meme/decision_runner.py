"""Fail-closed bridge from persisted Solana evidence to the paper-only Core.

The runner cannot construct safety certificates or agent evidence. It consumes
typed, independently persisted reports and an explicitly supplied agent-context
provider. Its audit claim is written before Core is called, so a process crash
cannot cause the same evidence packet to be replayed into a second paper entry.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import sqlite3
import time
from typing import Callable

from ..core import Core
from ..safety import SafetyReport
from .candidates import canonical_mint
from .risk_evidence import SolanaRiskEvidenceReport
from .sellability import SolanaSellabilityEvidence


@dataclass(frozen=True)
class SolanaAgentContext:
    mint: str
    provider: str
    evidence_id: str
    observed_at_ms: int
    volume_spike: bool
    price_change_5m_pct: float
    smart_wallet_buys: tuple[str, ...]


@dataclass(frozen=True)
class PaperDecisionBatchResult:
    considered: int
    approved: int
    blocked: int
    already_processed: int
    failed: int


class SolanaPaperDecisionRunner:
    """Evaluate complete evidence packets once, using only the paper broker."""

    def __init__(
        self,
        *,
        launch_store,
        market_store,
        risk_store,
        developer_store,
        bundle_store,
        sellability_store,
        safety_store,
        broker,
        core: Core,
        agent_context_provider: Callable[[str, dict], SolanaAgentContext] | None = None,
        max_market_age_ms: int = 120_000,
        max_evidence_age_ms: int = 300_000,
        max_route_age_ms: int = 15_000,
        paper_notional_usd: float = 50.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        stores = (
            launch_store, market_store, risk_store, developer_store,
            bundle_store, sellability_store, safety_store,
        )
        paths = {getattr(store, "database_path", None) for store in stores}
        paths.add(getattr(broker, "db_path", None))
        if len(paths) != 1 or None in paths or ":memory:" in paths:
            raise ValueError("all evidence stores and the durable paper broker must share one file database")
        if not isinstance(core, Core) or core.broker is not broker:
            raise ValueError("Core must use the supplied durable paper broker")
        for value, label in (
            (max_market_age_ms, "max_market_age_ms"),
            (max_evidence_age_ms, "max_evidence_age_ms"),
            (max_route_age_ms, "max_route_age_ms"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if (isinstance(paper_notional_usd, bool)
                or not isinstance(paper_notional_usd, (int, float))
                or not math.isfinite(paper_notional_usd)
                or not 0 < paper_notional_usd <= broker.max_position_usd):
            raise ValueError("paper_notional_usd exceeds the broker's configured position limit")

        self.launch_store = launch_store
        self.market_store = market_store
        self.risk_store = risk_store
        self.developer_store = developer_store
        self.bundle_store = bundle_store
        self.sellability_store = sellability_store
        self.safety_store = safety_store
        self.broker = broker
        self.core = core
        self.agent_context_provider = agent_context_provider
        self.max_market_age_ms = max_market_age_ms
        self.max_evidence_age_ms = max_evidence_age_ms
        self.max_route_age_ms = max_route_age_ms
        self.paper_notional_usd = float(paper_notional_usd)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.database_path = next(iter(paths))
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_paper_decision_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_key TEXT NOT NULL UNIQUE,
                    mint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    reasons_json TEXT NOT NULL DEFAULT '[]',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_paper_audit_mint "
                "ON solana_paper_decision_audits(mint, created_at_ms DESC)"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _risk_report(payload: dict) -> SolanaRiskEvidenceReport:
        values = dict(payload)
        for key in ("missing_claims", "errors", "token_2022_extension_types"):
            values[key] = tuple(values.get(key) or ())
        return SolanaRiskEvidenceReport(**values)

    @staticmethod
    def _sellability_report(payload: dict) -> SolanaSellabilityEvidence:
        values = dict(payload)
        values["errors"] = tuple(values.get("errors") or ())
        return SolanaSellabilityEvidence(**values)

    @staticmethod
    def _safety_report(payload: dict) -> SafetyReport:
        values = dict(payload)
        values["checks"] = tuple(values.get("checks") or ())
        values["reasons"] = tuple(values.get("reasons") or ())
        return SafetyReport(**values)

    def _claim(self, mint: str, evidence: dict, now_ms: int) -> str | None:
        encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(f"{mint}|{encoded}".encode("utf-8")).hexdigest()
        with self._connect() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO solana_paper_decision_audits
                    (decision_key, mint, state, evidence_json, created_at_ms, updated_at_ms)
                VALUES (?, ?, 'CLAIMED', ?, ?, ?)
            """, (key, mint, encoded, now_ms, now_ms))
            return key if cursor.rowcount == 1 else None

    def _finish(self, key: str, state: str, reasons: list[str], result: dict, now_ms: int) -> None:
        with self._connect() as db:
            db.execute("""
                UPDATE solana_paper_decision_audits
                SET state=?, reasons_json=?, result_json=?, updated_at_ms=?
                WHERE decision_key=? AND state='CLAIMED'
            """, (
                state, json.dumps(reasons, sort_keys=True),
                json.dumps(result, sort_keys=True, default=str), now_ms, key,
            ))

    def run_once(self, *, limit: int = 25) -> PaperDecisionBatchResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        now = self.clock_ms()
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("clock must return a non-negative integer timestamp")

        considered = approved = blocked = already_processed = failed = 0
        for candidate in self.launch_store.list_candidates(limit=limit):
            considered += 1
            mint = canonical_mint(candidate["mint"])
            market = self.market_store.latest(mint)
            risk = self.risk_store.latest(mint)
            developer = self.developer_store.latest(mint)
            bundle = self.bundle_store.latest(mint)
            route = self.sellability_store.latest(mint)
            safety = self.safety_store.latest(mint)
            evidence_ids = {
                "candidate_first_seen_at_ms": candidate.get("first_seen_at_ms"),
                "market_id": market.get("id") if market else None,
                "risk_id": risk.get("evidence_id") if risk else None,
                "developer_id": developer.get("evidence_id") if developer else None,
                "bundle_id": bundle.get("evidence_id") if bundle else None,
                "sellability_id": route.get("evidence_id") if route else None,
                "safety_id": safety.get("evidence_id") if safety else None,
            }
            key = self._claim(mint, evidence_ids, now)
            if key is None:
                already_processed += 1
                continue

            reasons: list[str] = []
            token: dict = {"address": mint, "chain": "solana"}
            try:
                if not all((market, risk, developer, bundle, route, safety)):
                    reasons.append("one or more required persisted evidence reports are missing")

                if market:
                    observed = market.get("retrieved_at_ms")
                    if (isinstance(observed, bool) or not isinstance(observed, int)
                            or not 0 <= now - observed <= self.max_market_age_ms):
                        reasons.append("market snapshot is stale or future-dated")
                    price = market.get("price_usd")
                    if (isinstance(price, bool) or not isinstance(price, (int, float))
                            or not math.isfinite(price) or price <= 0):
                        reasons.append("market USD price is invalid")
                    else:
                        token["price"] = float(price)
                        token["price_change_5m"] = market.get("price_change_m5_pct")

                risk_report = self._risk_report(risk["report"]) if risk else None
                if risk_report:
                    age = now - risk_report.observed_at_ms
                    if age < 0 or age > self.max_evidence_age_ms:
                        reasons.append("risk report is stale or future-dated")
                    if risk_report.errors:
                        reasons.append("risk report contains validation/provider errors")
                    if set(risk_report.missing_claims) - {"executable_sellability"}:
                        reasons.append("mandatory token-risk claims remain unknown")
                    if risk_report.mint != mint:
                        reasons.append("risk report mint mismatch")

                if developer:
                    report = developer["report"]
                    age = now - developer["observed_at_ms"]
                    if age < 0 or age > self.max_evidence_age_ms:
                        reasons.append("developer evidence is stale or future-dated")
                    if report.get("coverage_complete") is not True or not report.get("provider") or not report.get("evidence_id"):
                        reasons.append("developer ancestry evidence is incomplete or uncertified")
                    dev_pct = report.get("dev_hold_pct")
                    if (isinstance(dev_pct, bool) or not isinstance(dev_pct, (int, float))
                            or not math.isfinite(dev_pct) or not 0 <= dev_pct < 5):
                        reasons.append("developer-linked holdings are unknown or at/above 5%")

                if bundle:
                    report = bundle["report"]
                    age = now - bundle["observed_at_ms"]
                    if age < 0 or age > self.max_evidence_age_ms:
                        reasons.append("launch-bundle evidence is stale or future-dated")
                    if report.get("coverage_complete") is not True or not report.get("evidence_id"):
                        reasons.append("launch-bundle coverage is incomplete")
                    if report.get("rejected"):
                        reasons.append("launch-bundle report contains rejected evidence")
                    bundled_pct = report.get("bundled_supply_pct")
                    if bundled_pct != 0:
                        reasons.append("bundled supply is unknown or non-zero")

                risk_report_typed = risk_report
                route_report = self._sellability_report(route["report"]) if route else None
                if route_report:
                    age = now - route_report.observed_at_ms
                    if age < 0 or age > self.max_route_age_ms:
                        reasons.append("sell-route observation is stale or future-dated")
                    if route_report.state != "ROUTE_OBSERVED_NOT_EXECUTION_PROVEN" or route_report.execution_proven is not False:
                        reasons.append("fresh paper-only sell route is unavailable")
                    if route_report.errors:
                        reasons.append("sell-route report contains validation errors")
                    if route_report.quote_expires_at_ms is None or route_report.quote_expires_at_ms <= now:
                        reasons.append("sell-route quote has expired")

                safety_report = self._safety_report(safety["report"]) if safety else None
                if safety_report:
                    age = now - safety_report.observed_at_ms
                    if age < 0 or age > self.max_evidence_age_ms:
                        reasons.append("composite safety certificate is stale or future-dated")
                    if safety_report.passed is not True or safety_report.composite_verified is not True:
                        reasons.append("composite safety certificate is not positively verified")
                    if safety_report.token_address != mint:
                        reasons.append("composite safety certificate mint mismatch")

                if self.agent_context_provider is None:
                    reasons.append("independent agent-context provider is not configured")
                elif market and risk_report and developer and bundle:
                    context = self.agent_context_provider(mint, {
                        "market": market, "risk": risk, "developer": developer,
                        "bundle": bundle,
                    })
                    if not isinstance(context, SolanaAgentContext):
                        reasons.append("agent context is missing or untyped")
                    else:
                        if context.mint != mint or not context.provider or not context.evidence_id:
                            reasons.append("agent context provenance or mint is invalid")
                        age = now - context.observed_at_ms
                        if age < 0 or age > self.max_evidence_age_ms:
                            reasons.append("agent context is stale or future-dated")
                        if not isinstance(context.volume_spike, bool):
                            reasons.append("volume-spike agent input is not boolean")
                        if (isinstance(context.price_change_5m_pct, bool)
                                or not isinstance(context.price_change_5m_pct, (int, float))
                                or not math.isfinite(context.price_change_5m_pct)):
                            reasons.append("agent momentum input is invalid")
                        if (not isinstance(context.smart_wallet_buys, tuple)
                                or any(not isinstance(x, str) or not x.strip() for x in context.smart_wallet_buys)):
                            reasons.append("smart-wallet BUY evidence is malformed")
                        if not reasons:
                            token.update({
                                "volume_spike": context.volume_spike,
                                "price_change_5m": context.price_change_5m_pct,
                                "smart_wallet_buys": list(context.smart_wallet_buys),
                                "risk_evidence_report": risk_report_typed,
                                "safety_report": safety_report,
                                "sellability_evidence": route_report,
                                "sell_amount_raw": route_report.input_amount_raw,
                            })

                if reasons:
                    self._finish(key, "BLOCKED", reasons, {"decision": "HOLD"}, now)
                    blocked += 1
                    continue

                result = self.core.evaluate(token, amount_usd=self.paper_notional_usd)
                if result is None:
                    reasons.append("Core or Shield rejected the candidate")
                    self._finish(key, "BLOCKED", reasons, {"decision": "REJECT"}, now)
                    blocked += 1
                else:
                    self._finish(key, "PAPER_ENTRY_OPENED", [], {
                        "decision": "BUY", "result": result,
                        "sellability_state": "ROUTE_OBSERVED_NOT_EXECUTION_PROVEN",
                        "evidence_ids": evidence_ids,
                    }, now)
                    approved += 1
            except Exception as exc:
                self._finish(key, "FAILED_CLOSED", [
                    f"{type(exc).__name__}: {str(exc)[:240]}"
                ], {"decision": "HOLD"}, now)
                failed += 1

        return PaperDecisionBatchResult(
            considered=considered, approved=approved, blocked=blocked,
            already_processed=already_processed, failed=failed,
        )

    def audit_history(self, mint: str, *, limit: int = 100) -> list[dict]:
        canonical = canonical_mint(mint)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._connect() as db:
            rows = db.execute("""
                SELECT * FROM solana_paper_decision_audits
                WHERE mint=? ORDER BY created_at_ms DESC, id DESC LIMIT ?
            """, (canonical, limit)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            item["reasons"] = json.loads(item.pop("reasons_json"))
            item["result"] = json.loads(item.pop("result_json"))
            output.append(item)
        return output
