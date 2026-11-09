"""Evidence-to-Core bridge for strictly paper-only Solana decisions.

The runner reads persisted evidence only. It has no provider, network, wallet,
signer, swap, RPC-write, or transaction-submission interface.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Callable

from ..safety import SafetyReport
from .risk_evidence import SolanaRiskEvidenceReport
from .sellability import SolanaSellabilityEvidence
from .candidates import canonical_mint


@dataclass(frozen=True)
class PaperDecisionBatchResult:
    discovered: int
    processed: int
    opened: int
    rejected: int
    blocked: int
    duplicate_cycles: int
    failed: int


class SolanaPaperDecisionRunner:
    """Join persisted Solana evidence and invoke Core's paper broker once per evidence cycle.

    A caller must supply a trusted composite-safety report loader. Missing or
    untyped reports are vetoes. The runner never synthesizes a SafetyReport
    from partial GoPlus, holder, developer, or bundle observations.
    """

    def __init__(
        self,
        launch_store,
        market_store,
        risk_store,
        developer_store,
        bundle_store,
        sellability_store,
        core,
        *,
        safety_report_loader: Callable[[str, int], SafetyReport | None],
        agent_context_loader: Callable[[str, int], dict] | None = None,
        max_market_age_ms: int = 120_000,
        max_evidence_age_ms: int = 300_000,
        volume_spike_multiple: float = 2.0,
        volume_baseline_observations: int = 5,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        stores = (
            launch_store, market_store, risk_store, developer_store,
            bundle_store, sellability_store,
        )
        paths = {getattr(store, "database_path", None) for store in stores}
        broker_path = getattr(getattr(core, "broker", None), "db_path", None)
        if len(paths) != 1 or None in paths or ":memory:" in paths or broker_path not in paths:
            raise ValueError("all evidence stores and the paper broker must share one persistent SQLite database")
        if not callable(safety_report_loader):
            raise ValueError("a trusted composite-safety report loader is required")
        if min(max_market_age_ms, max_evidence_age_ms, volume_baseline_observations) < 1:
            raise ValueError("freshness limits and volume baseline count must be positive")
        if (isinstance(volume_spike_multiple, bool)
                or not isinstance(volume_spike_multiple, (int, float))
                or not math.isfinite(volume_spike_multiple) or volume_spike_multiple <= 1):
            raise ValueError("volume_spike_multiple must be finite and greater than one")
        if (getattr(core, "max_positions", None) != 10
                or getattr(core, "max_position_usd", None) != 50
                or getattr(core.broker, "max_positions", None) != 10
                or getattr(core.broker, "max_position_usd", None) != 50
                or core.broker.snapshot().get("starting_balance") != 500):
            raise ValueError("paper runner requires the approved $500 / $50 / 10-position profile")
        self.launch_store = launch_store
        self.market_store = market_store
        self.risk_store = risk_store
        self.developer_store = developer_store
        self.bundle_store = bundle_store
        self.sellability_store = sellability_store
        self.core = core
        self.safety_report_loader = safety_report_loader
        self.agent_context_loader = agent_context_loader
        self.max_market_age_ms = max_market_age_ms
        self.max_evidence_age_ms = max_evidence_age_ms
        self.volume_spike_multiple = float(volume_spike_multiple)
        self.volume_baseline_observations = volume_baseline_observations
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.database_path = broker_path
        self._initialize()

    def _connect(self):
        db = sqlite3.connect(self.database_path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def _initialize(self):
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_paper_decision_audits (
                    decision_key TEXT PRIMARY KEY,
                    mint TEXT NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    market_row_id INTEGER,
                    risk_evidence_id TEXT,
                    developer_evidence_id TEXT,
                    bundle_evidence_id TEXT,
                    sellability_evidence_id TEXT,
                    safety_fingerprint TEXT,
                    reasons_json TEXT NOT NULL,
                    result_json TEXT,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_paper_audit_mint_time "
                "ON solana_paper_decision_audits(mint, observed_at_ms DESC)"
            )

    def _candidates(self, limit: int) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT mint FROM solana_launch_candidates ORDER BY first_seen_at_ms, mint LIMIT ?",
                (limit,),
            ).fetchall()
        return [row["mint"] for row in rows]

    @staticmethod
    def _tuple_fields(payload: dict, names: tuple[str, ...]) -> dict:
        clean = dict(payload)
        for name in names:
            if name in clean:
                clean[name] = tuple(clean[name] or ())
        return clean

    def _risk_report(self, mint: str) -> tuple[SolanaRiskEvidenceReport, dict]:
        stored = self.risk_store.latest(mint)
        payload = stored.get("report") if stored else None
        if not isinstance(payload, dict):
            raise ValueError("persisted typed risk evidence is missing")
        payload = self._tuple_fields(
            payload, ("missing_claims", "errors", "token_2022_extension_types")
        )
        report = SolanaRiskEvidenceReport(**payload)
        if report.mint != mint:
            raise ValueError("risk evidence mint mismatch")
        if report.errors:
            raise ValueError("risk evidence contains collection or validation errors")
        # The only tolerated unknown is the explicit live-execution claim.
        if set(report.missing_claims) - {"executable_sellability"}:
            raise ValueError("mandatory token-risk claims remain unknown")
        if report.missing_claims.count("executable_sellability") > 1:
            raise ValueError("duplicate executable-sellability claim")
        if report.mint_supply_reconciled is not True:
            raise ValueError("finalized mint supply is not reconciled")
        return report, stored

    def _sellability(self, mint: str) -> tuple[SolanaSellabilityEvidence, dict]:
        stored = self.sellability_store.latest(mint)
        payload = stored.get("report") if stored else None
        if not isinstance(payload, dict):
            raise ValueError("persisted sell-route evidence is missing")
        payload = self._tuple_fields(payload, ("errors",))
        report = SolanaSellabilityEvidence(**payload)
        if report.mint != mint:
            raise ValueError("sell-route evidence mint mismatch")
        if report.state != "ROUTE_OBSERVED_NOT_EXECUTION_PROVEN" or report.execution_proven is not False:
            raise ValueError("a fresh route-only sell observation is required")
        if report.errors:
            raise ValueError("sell-route evidence contains errors")
        return report, stored

    def _ancillary_evidence(self, mint: str, now: int) -> tuple[dict, dict]:
        developer = self.developer_store.latest(mint)
        dev = developer.get("report") if developer else None
        if not isinstance(dev, dict):
            raise ValueError("developer ancestry evidence is missing")
        if (dev.get("coverage_complete") is not True
                or not isinstance(dev.get("provider"), str) or not dev["provider"].strip()
                or not isinstance(dev.get("evidence_id"), str) or not dev["evidence_id"].strip()
                or not isinstance(dev.get("source_ref"), str) or not dev["source_ref"].startswith("https://")
                or not isinstance(dev.get("independence_group"), str) or not dev["independence_group"].strip()
                or dev.get("reasons")):
            raise ValueError("developer ancestry evidence is incomplete or uncertified")
        dev_pct = dev.get("dev_hold_pct")
        if (isinstance(dev_pct, bool) or not isinstance(dev_pct, (int, float))
                or not math.isfinite(dev_pct) or not 0 <= dev_pct < 5):
            raise ValueError("developer-linked holdings are unknown or exceed the paper threshold")
        dev_time = dev.get("observed_at_ms")
        if (isinstance(dev_time, bool) or not isinstance(dev_time, int)
                or not 0 <= now - dev_time <= self.max_evidence_age_ms):
            raise ValueError("developer ancestry evidence is stale or future-dated")

        bundle = self.bundle_store.latest(mint)
        bundled = bundle.get("report") if bundle else None
        if not isinstance(bundled, dict):
            raise ValueError("launch-window bundle evidence is missing")
        if (bundled.get("coverage_complete") is not True
                or not isinstance(bundled.get("evidence_id"), str) or not bundled["evidence_id"].strip()
                or not isinstance(bundled.get("provider"), str) or not bundled["provider"].strip()
                or bundled.get("rejected")):
            raise ValueError("launch-window bundle evidence is incomplete or uncertified")
        bundle_pct = bundled.get("bundled_supply_pct")
        if (isinstance(bundle_pct, bool) or not isinstance(bundle_pct, (int, float))
                or not math.isfinite(bundle_pct) or bundle_pct != 0):
            raise ValueError("launch-window bundled supply is unknown or non-zero")
        bundle_time = bundled.get("observed_at_ms")
        if (isinstance(bundle_time, bool) or not isinstance(bundle_time, int)
                or not 0 <= now - bundle_time <= self.max_evidence_age_ms):
            raise ValueError("launch-window bundle evidence is stale or future-dated")
        return developer, bundle

    def _market_context(self, mint: str, now: int) -> tuple[dict, bool]:
        market = self.market_store.latest(mint)
        if not isinstance(market, dict):
            raise ValueError("persisted market snapshot is missing")
        observed = market.get("retrieved_at_ms")
        price = market.get("price_usd")
        if (isinstance(observed, bool) or not isinstance(observed, int)
                or not 0 <= now - observed <= self.max_market_age_ms):
            raise ValueError("market snapshot is stale or future-dated")
        if (isinstance(price, bool) or not isinstance(price, (int, float))
                or not math.isfinite(price) or price <= 0):
            raise ValueError("market USD price is invalid")
        history = self.market_store.history(mint, limit=self.volume_baseline_observations + 1)
        baseline = history[1:self.volume_baseline_observations + 1]
        volumes = [
            float(row["volume_h1_usd"]) for row in baseline
            if isinstance(row.get("volume_h1_usd"), (int, float))
            and not isinstance(row.get("volume_h1_usd"), bool)
            and math.isfinite(row["volume_h1_usd"]) and row["volume_h1_usd"] > 0
        ]
        volume_spike = False
        if len(volumes) == self.volume_baseline_observations:
            ordered = sorted(volumes)
            middle = len(ordered) // 2
            median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
            current_volume = market.get("volume_h1_usd")
            volume_spike = (
                isinstance(current_volume, (int, float))
                and not isinstance(current_volume, bool)
                and math.isfinite(current_volume)
                and median > 0
                and current_volume >= median * self.volume_spike_multiple
            )
        token = {
            "address": mint,
            "chain": "solana",
            "price": float(price),
            "price_change_5m": float(market.get("price_change_m5_pct", 0)),
            "volume_spike": volume_spike,
        }
        return token, volume_spike

    def _safety_report(self, mint: str, now: int) -> tuple[SafetyReport, str]:
        report = self.safety_report_loader(mint, now)
        if not isinstance(report, SafetyReport):
            raise ValueError("trusted composite SafetyReport is missing or untyped")
        if report.token_address != mint or report.passed is not True or report.composite_verified is not True:
            raise ValueError("composite safety certificate is not positively verified for this mint")
        if (isinstance(report.observed_at_ms, bool) or not isinstance(report.observed_at_ms, int)
                or not 0 <= now - report.observed_at_ms <= self.max_evidence_age_ms):
            raise ValueError("composite safety certificate is stale or future-dated")
        fingerprint = hashlib.sha256(
            json.dumps(asdict(report), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return report, fingerprint

    def _decision_key(self, mint: str, market: dict, risk: dict, developer: dict,
                      bundle: dict, sellability: dict, safety_fingerprint: str) -> str:
        material = {
            "mint": mint,
            "market_row_id": market.get("id"),
            "risk_evidence_id": risk.get("evidence_id"),
            "developer_evidence_id": developer.get("evidence_id"),
            "bundle_evidence_id": bundle.get("evidence_id"),
            "sellability_evidence_id": sellability.get("evidence_id"),
            "safety_fingerprint": safety_fingerprint,
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _audit(self, key: str, mint: str, now: int, state: str, reasons: list[str],
               market: dict | None = None, risk: dict | None = None,
               developer: dict | None = None, bundle: dict | None = None,
               sellability: dict | None = None, safety_fingerprint: str | None = None,
               result: dict | None = None) -> bool:
        with self._connect() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO solana_paper_decision_audits
                    (decision_key, mint, observed_at_ms, state, market_row_id,
                     risk_evidence_id, developer_evidence_id, bundle_evidence_id,
                     sellability_evidence_id, safety_fingerprint, reasons_json,
                     result_json, created_at_ms, updated_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                key, mint, now, state,
                market.get("id") if market else None,
                risk.get("evidence_id") if risk else None,
                developer.get("evidence_id") if developer else None,
                bundle.get("evidence_id") if bundle else None,
                sellability.get("evidence_id") if sellability else None,
                safety_fingerprint, json.dumps(reasons), json.dumps(result, default=str) if result else None,
                now, now,
            ))
            return cursor.rowcount == 1

    def run_once(self, *, limit: int = 25) -> PaperDecisionBatchResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        now = self.clock_ms()
        mints = self._candidates(limit)
        opened = rejected = blocked = duplicate = failed = processed = 0
        for mint_value in mints:
            mint = canonical_mint(mint_value)
            market = risk = developer = bundle = sellability = None
            safety_fingerprint = None
            reasons: list[str] = []
            try:
                market = self.market_store.latest(mint)
                risk = self.risk_store.latest(mint)
                developer = self.developer_store.latest(mint)
                bundle = self.bundle_store.latest(mint)
                sellability = self.sellability_store.latest(mint)
                if not all((market, risk, developer, bundle, sellability)):
                    raise ValueError("one or more persisted evidence streams are missing")
                token, _ = self._market_context(mint, now)
                risk_report, risk = self._risk_report(mint)
                sell_report, sellability = self._sellability(mint)
                self._ancillary_evidence(mint, now)
                safety_report, safety_fingerprint = self._safety_report(mint, now)
                token.update({
                    "risk_evidence_report": risk_report,
                    "safety_report": safety_report,
                    "sellability_evidence": sell_report,
                    "sell_amount_raw": sell_report.input_amount_raw,
                })
                if self.agent_context_loader is not None:
                    extra = self.agent_context_loader(mint, now)
                    if not isinstance(extra, dict):
                        raise ValueError("agent context loader must return a dictionary")
                    forbidden = set(extra) - {"smart_wallet_buys"}
                    if forbidden:
                        raise ValueError("agent context attempted to override protected market/evidence fields")
                    token.update(extra)
                key = self._decision_key(mint, market, risk, developer, bundle, sellability, safety_fingerprint)
                if not self._audit(
                    key, mint, now, "PENDING", [], market, risk, developer, bundle,
                    sellability, safety_fingerprint,
                ):
                    duplicate += 1
                    continue
                processed += 1
                result = self.core.evaluate(token, amount_usd=50.0)
                if result is None:
                    state = "REJECTED"
                    reasons = ["Core/Shield rejected candidate or paper broker constraints blocked entry"]
                    rejected += 1
                else:
                    state = "OPENED"
                    reasons = ["paper-only entry opened by Core after evidence and agent gates"]
                    opened += 1
                self._update_audit(key, state, reasons, result)
            except ValueError as exc:
                blocked += 1
                key_material = f"{mint}:{type(exc).__name__}:{str(exc)}:{now}"
                key = hashlib.sha256(key_material.encode()).hexdigest()
                self._audit(
                    key, mint, now, "BLOCKED", [str(exc)], market, risk, developer,
                    bundle, sellability, safety_fingerprint,
                )
            except Exception as exc:
                failed += 1
                key_material = f"{mint}:{type(exc).__name__}:{now}"
                key = hashlib.sha256(key_material.encode()).hexdigest()
                self._audit(
                    key, mint, now, "FAILED", [f"{type(exc).__name__}: {str(exc)[:240]}"],
                    market, risk, developer, bundle, sellability, safety_fingerprint,
                )
        return PaperDecisionBatchResult(
            discovered=len(mints), processed=processed, opened=opened,
            rejected=rejected, blocked=blocked, duplicate_cycles=duplicate,
            failed=failed,
        )

    def _update_audit(self, key: str, state: str, reasons: list[str], result: dict | None):
        with self._connect() as db:
            db.execute("""
                UPDATE solana_paper_decision_audits
                SET state=?, reasons_json=?, result_json=?, updated_at_ms=?
                WHERE decision_key=?
            """, (
                state, json.dumps(reasons), json.dumps(result, default=str) if result else None,
                self.clock_ms(), key,
            ))

    def audit_history(self, mint: str, *, limit: int = 100) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._connect() as db:
            rows = db.execute("""
                SELECT * FROM solana_paper_decision_audits
                WHERE mint=? ORDER BY observed_at_ms DESC LIMIT ?
            """, (canonical_mint(mint), limit)).fetchall()
        return [
            {**dict(row), "reasons": json.loads(row["reasons_json"]),
             "result": json.loads(row["result_json"]) if row["result_json"] else None}
            for row in rows
        ]
