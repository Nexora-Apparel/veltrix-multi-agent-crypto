"""Consensus-gated paper execution over the existing durable paper ledger.

This module is simulation-only. It consumes persisted Phase 06 decisions and
Phase 04/05 evidence; it has no exchange, wallet-signing, or RPC execution path.
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

from ..ledger import PaperBroker
from .consensus import RiskEvidence
from .consensus_store import ConsensusStore
from .market_store import MarketSnapshotStore
from .candidates import canonical_mint
from .risk_store import SolanaRiskEvidenceStore
from ..orchestration import LeaseBusy, RunContext


@dataclass(frozen=True)
class PaperTradingPolicy:
    position_usd: float = 50.0
    fee_bps: float = 100.0
    slippage_bps: float = 50.0
    max_decision_age_ms: int = 120_000
    max_market_age_ms: int = 120_000
    max_risk_age_ms: int = 300_000
    max_portfolio_exposure_pct: float = 0.5

    def __post_init__(self) -> None:
        for name in ("position_usd", "fee_bps", "slippage_bps", "max_portfolio_exposure_pct"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if self.position_usd <= 0:
            raise ValueError("position_usd must be positive")
        if not 0 <= self.fee_bps <= 2_000 or not 0 <= self.slippage_bps <= 2_000:
            raise ValueError("fee_bps and slippage_bps must be between 0 and 2000")
        if not 0 < self.max_portfolio_exposure_pct <= 1:
            raise ValueError("max_portfolio_exposure_pct must be greater than 0 and at most 1")
        for name in ("max_decision_age_ms", "max_market_age_ms", "max_risk_age_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class PaperTradingBatchResult:
    scanned_decisions: int
    opened_positions: int
    skipped_decisions: int
    blocked_entries: int
    duplicate_decisions: int
    exit_fills: int
    errors: int


class PaperTradingWorker:
    """Open paper positions only from fresh, fully revalidated Phase 06 approvals."""

    def __init__(
        self,
        consensus_store: ConsensusStore,
        market_store: MarketSnapshotStore,
        risk_store: SolanaRiskEvidenceStore,
        broker: PaperBroker,
        *,
        policy: PaperTradingPolicy | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        paths = {
            str(Path(path).expanduser().resolve())
            for path in (
                consensus_store.database_path,
                market_store.database_path,
                risk_store.database_path,
                broker.db_path,
            )
        }
        if len(paths) != 1 or broker.db_path == ":memory:":
            raise ValueError(
                "paper broker and Phase 04–06 stores must share one persistent SQLite database: "
                + ", ".join(sorted(paths))
            )
        self.consensus_store = consensus_store
        self.market_store = market_store
        self.risk_store = risk_store
        self.broker = broker
        self.policy = policy or PaperTradingPolicy()
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        if self.policy.position_usd > broker.max_position_usd:
            raise ValueError("paper policy position_usd exceeds the broker per-position limit")

    @staticmethod
    def _fresh(timestamp: object, now_ms: int, max_age_ms: int) -> bool:
        return (
            isinstance(timestamp, int)
            and not isinstance(timestamp, bool)
            and timestamp > 0
            and 0 <= now_ms - timestamp <= max_age_ms
        )

    def _record_skip(
        self, decision: dict, now_ms: int, reason: str, *,
        blocked: bool = False, not_eligible: bool = False,
        context: RunContext | None = None,
    ) -> str:
        decision_id = decision.get("decision_id")
        mint = decision.get("mint")
        created = decision.get("created_at_ms")
        if not isinstance(decision_id, str) or not decision_id.strip():
            return "ERROR"
        if not isinstance(mint, str) or not mint.strip():
            mint = "unknown"
        if isinstance(created, bool) or not isinstance(created, int) or created <= 0:
            created = now_ms
        if context is None:
            result = self.broker.record_consensus_outcome(
                decision_id,
                mint,
                str(decision.get("status") or "UNKNOWN"),
                "BLOCKED" if blocked else ("NOT_ELIGIBLE" if not_eligible else "SKIPPED"),
                reason,
                created_at_ms=created,
                processed_at_ms=now_ms,
            )
        else:
            context.checkpoint(items=1)
            with self.broker._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                context.assert_fence_in_transaction(db)
                result = self.broker.record_consensus_outcome(
                    decision_id,
                    mint,
                    str(decision.get("status") or "UNKNOWN"),
                    "BLOCKED" if blocked else ("NOT_ELIGIBLE" if not_eligible else "SKIPPED"),
                    reason,
                    created_at_ms=created,
                    processed_at_ms=now_ms,
                    db=db,
                )
        return result["status"]

    def _validate_approval(self, decision: dict, now_ms: int) -> tuple[dict | None, str | None]:
        decision_id = decision.get("decision_id")
        mint = decision.get("mint")
        created = decision.get("created_at_ms")
        if not isinstance(decision_id, str) or not decision_id.strip():
            return None, "consensus decision ID is missing"
        if not isinstance(mint, str) or not mint.strip():
            return None, "consensus mint is missing"
        try:
            if canonical_mint(mint) != mint:
                return None, "consensus mint is not canonical Solana base58"
        except (TypeError, ValueError):
            return None, "consensus mint is invalid"
        if not self._fresh(created, now_ms, self.policy.max_decision_age_ms):
            return None, "consensus decision is stale or future-dated"
        if decision.get("status") != "CONSENSUS_APPROVED":
            return None, "consensus disposition is not approved"
        if decision.get("paper_review_eligible") is not True:
            return None, "decision is not eligible for paper review"
        if decision.get("execution_authorized") is not False:
            return None, "decision violates the non-executable Phase 06 boundary"

        votes = decision.get("votes")
        if not isinstance(votes, list):
            return None, "agent vote evidence is missing"
        shield = [
            vote for vote in votes
            if isinstance(vote, dict) and vote.get("agent") == "shield"
        ]
        supporters = {
            vote.get("agent")
            for vote in votes
            if isinstance(vote, dict)
            and isinstance(vote.get("agent"), str)
            and vote.get("agent") in {"scout", "whale", "sniper"}
            and vote.get("stance") == "SUPPORT"
        }
        if len(shield) != 1 or shield[0].get("stance") != "APPROVE":
            return None, "Shield approval is missing or ambiguous"
        if len(supporters) < 2 or decision.get("support_count") != len(supporters):
            return None, "two distinct specialist-agent support votes are required"
        supporting_summary = decision.get("supporting_agents")
        if (
            not isinstance(supporting_summary, list)
            or any(not isinstance(agent, str) for agent in supporting_summary)
            or set(supporting_summary) != supporters
        ):
            return None, "persisted supporting-agent summary does not match its votes"

        if decision.get("risk_state") != "EVIDENCE_COLLECTED_REVIEW_REQUIRED":
            return None, "risk evidence is not in the review-ready state"
        missing = decision.get("missing_claims")
        if not isinstance(missing, list) or missing:
            return None, "risk evidence contains missing claims"
        flags = decision.get("risk_flags")
        if not isinstance(flags, list):
            return None, "risk flag evidence is malformed"
        if any(
            not isinstance(flag, dict)
            or flag.get("severity") in {"critical", "high", "medium"}
            for flag in flags
        ):
            return None, "risk flags block paper entry"

        market = self.market_store.latest(mint)
        if market is None:
            return None, "latest market snapshot is unavailable"
        snapshot_id = market.get("id")
        if (
            isinstance(snapshot_id, bool)
            or not isinstance(snapshot_id, int)
            or snapshot_id != decision.get("market_snapshot_id")
        ):
            return None, "consensus market snapshot is no longer the latest observation"
        if not self._fresh(market.get("retrieved_at_ms"), now_ms, self.policy.max_market_age_ms):
            return None, "market snapshot is stale or future-dated"
        quality = market.get("quality_flags")
        if not isinstance(quality, list) or quality:
            return None, "market snapshot has unresolved data-quality flags"
        price = market.get("price_usd")
        if (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(float(price))
            or price <= 0
        ):
            return None, "market snapshot price is invalid"

        risk_payload = self.risk_store.latest(mint)
        if risk_payload is None:
            return None, "latest security evidence is unavailable"
        try:
            risk = RiskEvidence.from_store_payload(risk_payload)
        except (TypeError, ValueError):
            return None, "latest security evidence is malformed"
        if not self._fresh(risk.observed_at_ms, now_ms, self.policy.max_risk_age_ms):
            return None, "security evidence is stale or future-dated"
        if risk.mint != mint or risk.evidence_hash != decision.get("risk_evidence_hash"):
            return None, "security evidence changed or does not match the consensus mint"
        if risk.risk_state != decision.get("risk_state"):
            return None, "security evidence state differs from the consensus decision"
        if list(risk.missing_claims) != decision.get("missing_claims"):
            return None, "security missing-claim evidence differs from the consensus decision"
        expected_flags = [
            {
                "code": flag.code,
                "severity": flag.severity,
                "description": flag.description,
                "evidence_source": flag.evidence_source,
            }
            for flag in risk.risk_flags
        ]
        if expected_flags != decision.get("risk_flags"):
            return None, "security risk flags differ from the consensus decision"
        if risk.missing_claims or risk.errors:
            return None, "latest security evidence has unresolved claims or provider errors"
        if any(flag.severity not in {"low", "info"} for flag in risk.risk_flags):
            return None, "latest security evidence contains a blocking or unreviewed risk flag"
        return market, None

    def _process_exits(self, now_ms: int, context: RunContext | None = None) -> tuple[int, int]:
        actions = errors = 0
        open_positions = self.broker.snapshot().get("open_positions", [])
        for position in open_positions:
            mint = position.get("token")
            if not isinstance(mint, str) or not mint:
                errors += 1
                continue
            market = self.market_store.latest(mint)
            if market is None or not self._fresh(
                market.get("retrieved_at_ms"), now_ms, self.policy.max_market_age_ms
            ):
                continue
            price = market.get("price_usd")
            if (
                isinstance(price, bool)
                or not isinstance(price, (int, float))
                or not math.isfinite(float(price))
                or price <= 0
            ):
                errors += 1
                continue
            try:
                if context is None:
                    fills = self.broker.process_market_price(
                        mint,
                        float(price),
                        fee_bps=self.policy.fee_bps,
                        slippage_bps=self.policy.slippage_bps,
                    )
                else:
                    context.checkpoint(items=1)
                    with self.broker._connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        context.assert_fence_in_transaction(db)
                        fills = self.broker.process_market_price(
                            mint,
                            float(price),
                            fee_bps=self.policy.fee_bps,
                            slippage_bps=self.policy.slippage_bps,
                            db=db,
                        )
                actions += len(fills)
            except LeaseBusy:
                raise
            except (ValueError, RuntimeError):
                errors += 1
        return actions, errors

    def run_once(self, *, limit: int = 25, context: RunContext | None = None) -> PaperTradingBatchResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 250:
            raise ValueError("limit must be between 1 and 250")
        now = self.clock_ms()
        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise ValueError("clock_ms must return a positive integer")

        exit_fills, errors = self._process_exits(now, context)
        # Only the latest decision per mint is actionable. Older approvals must
        # never be executed after a newer rejection or changed market snapshot.
        decisions = self.consensus_store.latest_all(limit=limit)
        opened = skipped = blocked = duplicates = 0

        for decision in decisions:
            decision_id = decision.get("decision_id")
            if not isinstance(decision_id, str) or not decision_id.strip():
                errors += 1
                continue
            if self.broker.has_consensus_decision(decision_id) is not None:
                duplicates += 1
                continue
            if decision.get("status") != "CONSENSUS_APPROVED":
                outcome = self._record_skip(
                    decision,
                    now,
                    f"consensus disposition: {decision.get('status') or 'UNKNOWN'}",
                    not_eligible=True,
                    context=context,
                )
                if outcome == "DUPLICATE":
                    duplicates += 1
                else:
                    skipped += 1
                continue

            market, reason = self._validate_approval(decision, now)
            if reason is not None or market is None:
                outcome = self._record_skip(
                    decision, now, reason or "paper approval validation failed", blocked=True,
                    context=context,
                )
                if outcome == "DUPLICATE":
                    duplicates += 1
                else:
                    blocked += 1
                continue

            agent_reasons = [
                vote.get("reason", "")
                for vote in decision.get("votes", [])
                if isinstance(vote, dict)
                and vote.get("agent") in {"scout", "whale", "sniper"}
                and vote.get("stance") == "SUPPORT"
            ]
            open_args = {
                "created_at_ms": decision["created_at_ms"],
                "paper_review_eligible": decision["paper_review_eligible"],
                "execution_authorized": decision["execution_authorized"],
                "fee_bps": self.policy.fee_bps,
                "slippage_bps": self.policy.slippage_bps,
                "processed_at_ms": now,
                "expected_market_snapshot_id": market["id"],
                "expected_risk_evidence_hash": decision["risk_evidence_hash"],
                "max_portfolio_exposure_pct": self.policy.max_portfolio_exposure_pct,
            }
            if context is None:
                result = self.broker.open_position_from_consensus(
                    decision_id, decision["mint"], float(market["price_usd"]),
                    self.policy.position_usd,
                    "; ".join(reason for reason in agent_reasons if isinstance(reason, str)) or "Phase 06 consensus approval",
                    **open_args,
                )
            else:
                context.checkpoint(items=1)
                with self.broker._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    context.assert_fence_in_transaction(db)
                    result = self.broker.open_position_from_consensus(
                        decision_id, decision["mint"], float(market["price_usd"]),
                        self.policy.position_usd,
                        "; ".join(reason for reason in agent_reasons if isinstance(reason, str)) or "Phase 06 consensus approval",
                        db=db,
                        **open_args,
                    )
            if result["status"] == "OPENED":
                opened += 1
            elif result["status"] == "DUPLICATE":
                duplicates += 1
            else:
                blocked += 1

        return PaperTradingBatchResult(
            scanned_decisions=len(decisions),
            opened_positions=opened,
            skipped_decisions=skipped,
            blocked_entries=blocked,
            duplicate_decisions=duplicates,
            exit_fills=exit_fills,
            errors=errors,
        )

    def status(self) -> dict:
        now = self.clock_ms()
        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise ValueError("clock_ms must return a positive integer")
        unmarked = []
        prices = {}
        for position in self.broker.snapshot().get("open_positions", []):
            mint = position.get("token")
            market = self.market_store.latest(mint) if isinstance(mint, str) else None
            if market is None or not self._fresh(
                market.get("retrieved_at_ms"), now, self.policy.max_market_age_ms
            ):
                unmarked.append(mint)
                continue
            price = market.get("price_usd")
            if (
                isinstance(price, bool)
                or not isinstance(price, (int, float))
                or not math.isfinite(float(price))
                or price <= 0
            ):
                unmarked.append(mint)
                continue
            prices[mint] = float(price)
        account = self.broker.snapshot(prices)
        return {
            "account": account,
            "paper_performance": self.broker.session_report(),
            "execution_costs": self.broker.execution_cost_totals(),
            "unmarked_positions": unmarked,
            "recent_decisions": self.broker.paper_decisions(limit=50),
        }
