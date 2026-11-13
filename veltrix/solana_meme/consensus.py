"""Evidence-gated multi-agent consensus for Solana candidates.

The engine emits analytical dispositions only. It does not submit orders,
create positions, or authorize live execution.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields
from typing import Iterable, Mapping

from ..agents import Scout
from ..signal_intelligence import SignalContextBuilder
from ..whales import WalletBuyEvent
from .solana_whales import SolanaWhaleFlowAggregator
from .candidates import canonical_mint
from .risk_evidence import RiskFlag


@dataclass(frozen=True)
class MarketEvidence:
    mint: str
    snapshot_id: int
    pair_address: str
    price_usd: float
    liquidity_usd: float
    volume_h1_usd: float
    price_change_m5_pct: float | None
    retrieved_at_ms: int
    pair_created_at_ms: int | None
    pair_count: int
    quality_flags: tuple[str, ...]
    source: str = "dexscreener"
    chain: str = "solana"

    def __post_init__(self) -> None:
        if canonical_mint(self.mint) != self.mint:
            raise ValueError("market evidence mint must be canonical")
        if isinstance(self.snapshot_id, bool) or not isinstance(self.snapshot_id, int) or self.snapshot_id <= 0:
            raise ValueError("market evidence snapshot id must be positive")
        if not isinstance(self.pair_address, str) or not self.pair_address.strip():
            raise ValueError("market evidence pair address is required")
        object.__setattr__(self, "pair_address", self.pair_address.strip())
        for name in ("price_usd", "liquidity_usd", "volume_h1_usd"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"market evidence {name} must be finite")
        if self.price_usd <= 0 or self.liquidity_usd < 0 or self.volume_h1_usd < 0:
            raise ValueError("market evidence values are outside valid bounds")
        if self.price_change_m5_pct is not None and (
            isinstance(self.price_change_m5_pct, bool)
            or not isinstance(self.price_change_m5_pct, (int, float))
            or not math.isfinite(float(self.price_change_m5_pct))
        ):
            raise ValueError("market evidence price change must be finite or None")
        for name in ("retrieved_at_ms", "pair_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"market evidence {name} must be positive")
        if self.pair_created_at_ms is not None and (
            isinstance(self.pair_created_at_ms, bool)
            or not isinstance(self.pair_created_at_ms, int)
            or self.pair_created_at_ms <= 0
        ):
            raise ValueError("market evidence pair creation time is invalid")
        if self.source != "dexscreener" or self.chain != "solana":
            raise ValueError("market evidence must be canonical Solana DEX Screener evidence")
        if not isinstance(self.quality_flags, tuple) or any(
            not isinstance(flag, str) or not flag.strip() for flag in self.quality_flags
        ):
            raise ValueError("market evidence quality flags are malformed")
        object.__setattr__(self, "quality_flags", tuple(sorted(set(flag.strip() for flag in self.quality_flags))))

    @classmethod
    def from_store_row(cls, row: Mapping[str, object]) -> "MarketEvidence":
        mint = canonical_mint(row.get("mint"))
        numeric = {}
        for key in ("price_usd", "liquidity_usd", "volume_h1_usd"):
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"market snapshot {key} is invalid")
            numeric[key] = float(value)
        if numeric["price_usd"] <= 0 or numeric["liquidity_usd"] < 0 or numeric["volume_h1_usd"] < 0:
            raise ValueError("market snapshot core market values are outside valid bounds")
        change = row.get("price_change_m5_pct")
        if change is not None and (
            isinstance(change, bool)
            or not isinstance(change, (int, float))
            or not math.isfinite(float(change))
        ):
            raise ValueError("market snapshot price_change_m5_pct is invalid")
        retrieved = row.get("retrieved_at_ms")
        snapshot_id = row.get("id")
        pair_count = row.get("pair_count")
        if isinstance(retrieved, bool) or not isinstance(retrieved, int) or retrieved <= 0:
            raise ValueError("market snapshot retrieval timestamp is invalid")
        if isinstance(snapshot_id, bool) or not isinstance(snapshot_id, int) or snapshot_id <= 0:
            raise ValueError("market snapshot id is invalid")
        if isinstance(pair_count, bool) or not isinstance(pair_count, int) or pair_count < 1:
            raise ValueError("market snapshot pair count is invalid")
        pair_created = row.get("pair_created_at_ms")
        if pair_created is not None and (
            isinstance(pair_created, bool) or not isinstance(pair_created, int) or pair_created <= 0
        ):
            raise ValueError("market snapshot pair creation timestamp is invalid")
        raw_quality_flags = row.get("quality_flags", row.get("quality_flags_json", "[]"))
        if isinstance(raw_quality_flags, str):
            try:
                quality_flags = json.loads(raw_quality_flags or "[]")
            except json.JSONDecodeError as exc:
                raise ValueError("market snapshot quality flags are malformed") from exc
        else:
            quality_flags = raw_quality_flags
        if not isinstance(quality_flags, list) or any(not isinstance(item, str) for item in quality_flags):
            raise ValueError("market snapshot quality flags are malformed")
        pair = row.get("pair_address")
        if not isinstance(pair, str) or not pair.strip():
            raise ValueError("market snapshot pair address is missing")
        return cls(
            mint=mint,
            snapshot_id=snapshot_id,
            pair_address=pair,
            price_usd=numeric["price_usd"],
            liquidity_usd=numeric["liquidity_usd"],
            volume_h1_usd=numeric["volume_h1_usd"],
            price_change_m5_pct=float(change) if change is not None else None,
            retrieved_at_ms=retrieved,
            pair_created_at_ms=pair_created,
            pair_count=pair_count,
            quality_flags=tuple(quality_flags),
        )

    def is_fresh(self, now_ms: int, max_age_ms: int) -> bool:
        age = now_ms - self.retrieved_at_ms
        return 0 <= age <= max_age_ms


@dataclass(frozen=True)
class RiskEvidence:
    mint: str
    observed_at_ms: int
    risk_state: str
    missing_claims: tuple[str, ...]
    errors: tuple[str, ...]
    risk_flags: tuple[RiskFlag, ...]
    evidence_hash: str
    mint_evidence_id: str | None = None
    holder_evidence_id: str | None = None

    def __post_init__(self) -> None:
        if canonical_mint(self.mint) != self.mint:
            raise ValueError("risk evidence mint must be canonical")
        if isinstance(self.observed_at_ms, bool) or not isinstance(self.observed_at_ms, int) or self.observed_at_ms <= 0:
            raise ValueError("risk evidence timestamp must be positive")
        if self.risk_state not in {"REQUIRES_MORE_EVIDENCE", "EVIDENCE_COLLECTED_REVIEW_REQUIRED"}:
            raise ValueError("risk evidence state is unsupported")
        for name in ("missing_claims", "errors", "risk_flags"):
            if not isinstance(getattr(self, name), tuple):
                raise ValueError(f"risk evidence {name} must be a tuple")
        if any(not isinstance(item, str) or not item.strip() for item in self.missing_claims + self.errors):
            raise ValueError("risk evidence claims and errors must be non-empty strings")
        if self.risk_state == "EVIDENCE_COLLECTED_REVIEW_REQUIRED" and (
            self.missing_claims or self.errors
        ):
            raise ValueError("review-ready risk state cannot contain missing claims or errors")
        checked_flags = []
        for flag in self.risk_flags:
            if type(flag) is not RiskFlag:
                raise ValueError("risk evidence flags must be RiskFlag values")
            checked_flags.append(RiskFlag(flag.code, flag.severity, flag.description, flag.evidence_source))
        if len({flag.code for flag in checked_flags}) != len(checked_flags):
            raise ValueError("risk evidence flag codes must be unique")
        if not isinstance(self.evidence_hash, str) or not self.evidence_hash.strip():
            raise ValueError("risk evidence hash is required")
        if self.risk_state == "EVIDENCE_COLLECTED_REVIEW_REQUIRED" and (
            self.missing_claims or self.errors
        ):
            raise ValueError("review-ready risk state cannot contain missing claims or errors")
        for name in ("mint_evidence_id", "holder_evidence_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or None")
        object.__setattr__(self, "missing_claims", tuple(sorted(set(self.missing_claims))))
        object.__setattr__(self, "risk_flags", tuple(sorted(checked_flags, key=lambda flag: flag.code)))

    @classmethod
    def from_store_payload(cls, payload: Mapping[str, object]) -> "RiskEvidence":
        mint = canonical_mint(payload.get("mint"))
        observed = payload.get("observed_at_ms")
        if isinstance(observed, bool) or not isinstance(observed, int) or observed <= 0:
            raise ValueError("risk evidence timestamp is invalid")
        risk_state = payload.get("risk_state")
        if risk_state not in {"REQUIRES_MORE_EVIDENCE", "EVIDENCE_COLLECTED_REVIEW_REQUIRED"}:
            raise ValueError("risk evidence state is missing or unsupported")
        missing = payload.get("missing_claims")
        errors = payload.get("errors")
        raw_flags = payload.get("risk_flags")
        if not isinstance(missing, (list, tuple)) or any(not isinstance(x, str) for x in missing):
            raise ValueError("risk missing-claims list is malformed")
        if not isinstance(errors, (list, tuple)) or any(not isinstance(x, str) for x in errors):
            raise ValueError("risk errors list is malformed")
        if not isinstance(raw_flags, (list, tuple)):
            raise ValueError("risk flags list is malformed")
        flags = []
        for raw in raw_flags:
            if not isinstance(raw, Mapping):
                raise ValueError("risk flag row is malformed")
            try:
                values = {key: raw[key] for key in ("code", "severity", "description", "evidence_source")}
                if any(not isinstance(value, str) or not value.strip() for value in values.values()):
                    raise ValueError("risk flag text fields must be non-empty strings")
                flag = RiskFlag(**values)
            except (KeyError, TypeError) as exc:
                raise ValueError("risk flag row is incomplete") from exc
            if flag.severity not in {"critical", "high", "medium", "low", "info"}:
                raise ValueError("risk flag severity is invalid")
            flags.append(flag)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return cls(
            mint=mint,
            observed_at_ms=observed,
            risk_state=risk_state,
            missing_claims=tuple(sorted(set(missing))),
            errors=tuple(errors),
            risk_flags=tuple(sorted(flags, key=lambda flag: flag.code)),
            evidence_hash=digest,
            mint_evidence_id=payload.get("mint_evidence_id") if isinstance(payload.get("mint_evidence_id"), str) else None,
            holder_evidence_id=payload.get("holder_evidence_id") if isinstance(payload.get("holder_evidence_id"), str) else None,
        )

    def is_fresh(self, now_ms: int, max_age_ms: int) -> bool:
        age = now_ms - self.observed_at_ms
        return 0 <= age <= max_age_ms


@dataclass(frozen=True)
class AgentVote:
    agent: str
    stance: str
    reason: str
    evidence_refs: tuple[str, ...]
    observed_at_ms: int


@dataclass(frozen=True)
class ConsensusDecision:
    decision_id: str
    mint: str
    status: str
    created_at_ms: int
    votes: tuple[AgentVote, ...]
    support_count: int
    supporting_agents: tuple[str, ...]
    risk_state: str
    missing_claims: tuple[str, ...]
    risk_flags: tuple[RiskFlag, ...]
    reasons: tuple[str, ...]
    market_snapshot_id: int
    risk_evidence_hash: str
    paper_review_eligible: bool
    execution_authorized: bool = False
    rule_version: str = "phase6-consensus-v1"


def validate_market_evidence(evidence: object) -> MarketEvidence:
    if type(evidence) is not MarketEvidence:
        raise TypeError("market evidence must be a MarketEvidence value")
    try:
        values = {field.name: getattr(evidence, field.name) for field in fields(MarketEvidence)}
        return MarketEvidence(**values)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("market evidence failed canonical validation") from None


def validate_risk_evidence(evidence: object) -> RiskEvidence:
    if type(evidence) is not RiskEvidence:
        raise TypeError("risk evidence must be a RiskEvidence value")
    try:
        values = {field.name: getattr(evidence, field.name) for field in fields(RiskEvidence)}
        return RiskEvidence(**values)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("risk evidence failed canonical validation") from None


def validate_consensus_decision(decision: object) -> ConsensusDecision:
    """Reconstruct all decision fields and recompute its deterministic ID."""
    if type(decision) is not ConsensusDecision:
        raise TypeError("decision must be a ConsensusDecision value")
    try:
        values = {field.name: getattr(decision, field.name) for field in fields(ConsensusDecision)}
        checked = ConsensusDecision(**values)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("consensus decision failed canonical validation") from None

    if canonical_mint(checked.mint) != checked.mint:
        raise ValueError("consensus decision mint must be canonical")
    if checked.status not in {"REJECTED", "WATCH", "ABSTAIN", "CONSENSUS_APPROVED"}:
        raise ValueError("unsupported consensus disposition")
    if isinstance(checked.created_at_ms, bool) or not isinstance(checked.created_at_ms, int) or checked.created_at_ms <= 0:
        raise ValueError("consensus decision timestamp must be positive")
    if not isinstance(checked.votes, tuple) or len(checked.votes) != 4:
        raise ValueError("consensus decision must contain four specialist votes")
    expected_agents = ("scout", "whale", "sniper", "shield")
    checked_votes = []
    for vote, expected_agent in zip(checked.votes, expected_agents):
        if type(vote) is not AgentVote or vote.agent != expected_agent:
            raise ValueError("consensus votes are missing or out of order")
        allowed = {"APPROVE", "ABSTAIN", "REJECT"} if expected_agent == "shield" else {"SUPPORT", "ABSTAIN"}
        if vote.stance not in allowed:
            raise ValueError("consensus vote stance is invalid for its agent")
        if not isinstance(vote.reason, str) or not vote.reason.strip():
            raise ValueError("consensus vote reason is required")
        if not isinstance(vote.evidence_refs, tuple) or any(
            not isinstance(ref, str) or not ref.strip() for ref in vote.evidence_refs
        ):
            raise ValueError("consensus vote evidence references are malformed")
        if isinstance(vote.observed_at_ms, bool) or not isinstance(vote.observed_at_ms, int) or vote.observed_at_ms <= 0:
            raise ValueError("consensus vote timestamp must be positive")
        checked_votes.append(AgentVote(
            vote.agent, vote.stance, vote.reason, tuple(vote.evidence_refs), vote.observed_at_ms
        ))
    support_agents = tuple(
        vote.agent for vote in checked_votes
        if vote.agent in {"scout", "whale", "sniper"} and vote.stance == "SUPPORT"
    )
    if isinstance(checked.support_count, bool) or not isinstance(checked.support_count, int) or checked.support_count != len(support_agents):
        raise ValueError("consensus support count does not match votes")
    if not isinstance(checked.supporting_agents, tuple) or checked.supporting_agents != support_agents:
        raise ValueError("consensus supporting agents do not match votes")
    if checked.status == "REJECTED" and checked_votes[3].stance != "REJECT":
        raise ValueError("rejected consensus requires a Shield veto")
    if checked_votes[3].stance == "APPROVE" and (
        checked.risk_state != "EVIDENCE_COLLECTED_REVIEW_REQUIRED"
        or checked.missing_claims
        or any(flag.severity in {"medium", "high", "critical"} for flag in checked.risk_flags)
    ):
        raise ValueError("Shield approval requires complete risk evidence and no medium-or-higher flags")
    if checked.status == "CONSENSUS_APPROVED" and (
        checked_votes[3].stance != "APPROVE" or checked.support_count < 2
    ):
        raise ValueError("consensus approval requires Shield approval and two specialist supports")
    if checked.status in {"WATCH", "ABSTAIN"} and checked_votes[3].stance == "REJECT":
        raise ValueError("a Shield veto must be represented as REJECTED")
    if checked.risk_state not in {"REQUIRES_MORE_EVIDENCE", "EVIDENCE_COLLECTED_REVIEW_REQUIRED"}:
        raise ValueError("consensus risk state is unsupported")
    for name in ("missing_claims", "risk_flags", "reasons"):
        if not isinstance(getattr(checked, name), tuple):
            raise ValueError(f"consensus {name} must be a tuple")
    if any(not isinstance(item, str) or not item.strip() for item in checked.missing_claims + checked.reasons):
        raise ValueError("consensus claims and reasons must be non-empty strings")
    flags = []
    for flag in checked.risk_flags:
        if type(flag) is not RiskFlag:
            raise ValueError("consensus risk flags must be RiskFlag values")
        flags.append(RiskFlag(flag.code, flag.severity, flag.description, flag.evidence_source))
    if len({flag.code for flag in flags}) != len(flags):
        raise ValueError("consensus risk flag codes must be unique")
    if isinstance(checked.market_snapshot_id, bool) or not isinstance(checked.market_snapshot_id, int) or checked.market_snapshot_id <= 0:
        raise ValueError("consensus market snapshot id must be positive")
    if not isinstance(checked.risk_evidence_hash, str) or not checked.risk_evidence_hash.strip():
        raise ValueError("consensus risk evidence hash is required")
    if not isinstance(checked.paper_review_eligible, bool) or checked.paper_review_eligible != (checked.status == "CONSENSUS_APPROVED"):
        raise ValueError("paper-review eligibility must match the consensus disposition")
    if checked.execution_authorized is not False:
        raise ValueError("Phase 06 consensus decisions cannot authorize execution")
    if not isinstance(checked.rule_version, str) or not checked.rule_version.strip():
        raise ValueError("consensus rule version is required")
    payload = {
        "mint": checked.mint,
        "status": checked.status,
        "votes": [
            {
                "agent": vote.agent,
                "stance": vote.stance,
                "reason": vote.reason,
                "evidence_refs": vote.evidence_refs,
            }
            for vote in checked_votes
        ],
        "market_snapshot_id": checked.market_snapshot_id,
        "risk_evidence_hash": checked.risk_evidence_hash,
        "rule_version": checked.rule_version,
    }
    expected_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if checked.decision_id != expected_id:
        raise ValueError("consensus decision ID does not match its evidence and votes")
    return ConsensusDecision(
        decision_id=checked.decision_id,
        mint=checked.mint,
        status=checked.status,
        created_at_ms=checked.created_at_ms,
        votes=tuple(checked_votes),
        support_count=checked.support_count,
        supporting_agents=support_agents,
        risk_state=checked.risk_state,
        missing_claims=tuple(sorted(set(checked.missing_claims))),
        risk_flags=tuple(sorted(flags, key=lambda flag: flag.code)),
        reasons=tuple(checked.reasons),
        market_snapshot_id=checked.market_snapshot_id,
        risk_evidence_hash=checked.risk_evidence_hash,
        paper_review_eligible=checked.paper_review_eligible,
        execution_authorized=False,
        rule_version=checked.rule_version,
    )




@dataclass(frozen=True)
class _SignalEvent:
    token_address: str
    source: str
    chain: str
    price_usd: float
    liquidity_usd: float
    volume_h1_usd: float
    price_change_m5_pct: float | None
    observed_at_ms: int

    def is_fresh(self, now_ms: int, max_age_ms: int) -> bool:
        age = now_ms - self.observed_at_ms
        return 0 <= age <= max_age_ms


class ConsensusEngine:
    """Deterministic Scout/Whale/Sniper/Shield consensus with fail-closed gating."""

    def __init__(
        self,
        *,
        trusted_wallets: Iterable[str] = (),
        min_liquidity_usd: float = 10_000.0,
        min_volume_h1_usd: float = 10_000.0,
        min_pair_age_ms: int = 900_000,
        max_market_age_ms: int = 120_000,
        max_risk_age_ms: int = 300_000,
        min_history: int = 5,
        volume_multiple: float = 2.0,
        whale_window_ms: int = 300_000,
        whale_min_buy_usd: float = 100.0,
    ) -> None:
        for name, value in (
            ("min_liquidity_usd", min_liquidity_usd),
            ("min_volume_h1_usd", min_volume_h1_usd),
            ("volume_multiple", volume_multiple),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if min_liquidity_usd < 0 or min_volume_h1_usd < 0 or volume_multiple <= 1:
            raise ValueError("consensus market thresholds are invalid")
        for name, value in (
            ("min_pair_age_ms", min_pair_age_ms),
            ("max_market_age_ms", max_market_age_ms),
            ("max_risk_age_ms", max_risk_age_ms),
            ("min_history", min_history),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if min_history < 3:
            raise ValueError("min_history must be at least 3")
        self.min_liquidity_usd = float(min_liquidity_usd)
        self.min_volume_h1_usd = float(min_volume_h1_usd)
        self.min_pair_age_ms = min_pair_age_ms
        self.max_market_age_ms = max_market_age_ms
        self.max_risk_age_ms = max_risk_age_ms
        self.signal_context = SignalContextBuilder(
            min_history=min_history,
            volume_multiple=float(volume_multiple),
            max_age_ms=max_market_age_ms,
        )
        self.whale_aggregator = SolanaWhaleFlowAggregator(
            trusted_wallets=trusted_wallets,
            window_ms=whale_window_ms,
            min_buy_usd=whale_min_buy_usd,
        )

    @staticmethod
    def _vote(agent: str, stance: str, reason: str, refs: Iterable[str], now: int) -> AgentVote:
        return AgentVote(agent, stance, reason, tuple(refs), now)

    def evaluate(
        self,
        market: MarketEvidence,
        risk: RiskEvidence,
        *,
        history: Iterable[Mapping[str, object]],
        wallet_events: Iterable[WalletBuyEvent] = (),
        now_ms: int,
    ) -> ConsensusDecision:
        market = validate_market_evidence(market)
        risk = validate_risk_evidence(risk)
        now = now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise ValueError("now_ms must be a positive integer")
        if canonical_mint(market.mint) != market.mint or market.chain != "solana" or market.source != "dexscreener":
            raise ValueError("consensus accepts only canonical Solana DEX Screener market evidence")
        if market.mint != risk.mint:
            raise ValueError("market and risk evidence mint mismatch")
        if any(flag.severity not in {"critical", "high", "medium", "low", "info"} for flag in risk.risk_flags):
            raise ValueError("risk evidence contains an unsupported severity")

        market_fresh = market.is_fresh(now, self.max_market_age_ms)
        risk_fresh = risk.is_fresh(now, self.max_risk_age_ms)
        market_ref = f"market_snapshot:{market.snapshot_id}"
        risk_ref = f"risk_evidence:{risk.evidence_hash}"

        history_rows = []
        for row in history:
            if not isinstance(row, Mapping):
                continue
            history_rows.append({
                "source": "dexscreener",
                "chain": "solana",
                "token": market.mint,
                "volume_h1_usd": row.get("volume_h1_usd"),
                "observed_at_ms": row.get("retrieved_at_ms"),
            })

        signal_event = _SignalEvent(
            token_address=market.mint,
            source="dexscreener",
            chain="solana",
            price_usd=market.price_usd,
            liquidity_usd=market.liquidity_usd,
            volume_h1_usd=market.volume_h1_usd,
            price_change_m5_pct=market.price_change_m5_pct,
            observed_at_ms=market.retrieved_at_ms,
        )
        context = self.signal_context.build(signal_event, history_rows, now) if market_fresh else None
        if context is None:
            scout_vote = self._vote(
                "scout", "ABSTAIN",
                "Insufficient fresh market history for a comparative volume signal",
                (market_ref,), now,
            )
        else:
            context["price_change_5m"] = context.get("price_change_5m") or 0.0
            signal = Scout().evaluate(context)
            scout_vote = self._vote(
                "scout",
                "SUPPORT" if signal is not None else "ABSTAIN",
                signal.reason if signal is not None else "No qualifying volume-and-momentum anomaly",
                (market_ref,), now,
            )

        clusters = self.whale_aggregator.aggregate(wallet_events, now)
        matching = next(
            (cluster for cluster in clusters if cluster.chain == "solana" and cluster.token_address == market.mint),
            None,
        )
        whale_vote = self._vote(
            "whale",
            "SUPPORT" if matching is not None else "ABSTAIN",
            (
                f"{len(matching.wallet_addresses)} allowlisted wallets accumulated "
                f"{matching.total_notional_usd:.2f} USD within the configured window"
                if matching is not None
                else "No qualifying cluster from explicitly allowlisted wallets"
            ),
            (tuple(f"wallet:{wallet}" for wallet in matching.wallet_addresses) +
             (f"wallet_cluster:{matching.first_seen_ms}:{matching.last_seen_ms}:{matching.total_notional_usd:.8f}",)) if matching else (),
            now,
        )

        severe_flags = [flag for flag in risk.risk_flags if flag.severity in {"critical", "high"}]
        medium_flags = [flag for flag in risk.risk_flags if flag.severity == "medium"]
        if not market_fresh or not risk_fresh:
            sniper_vote = self._vote(
                "sniper", "ABSTAIN", "Market or risk evidence is stale", (market_ref, risk_ref), now
            )
        elif risk.missing_claims or risk.errors or severe_flags or medium_flags:
            sniper_vote = self._vote(
                "sniper", "ABSTAIN",
                "Execution-quality review is blocked by unresolved risk evidence or risk flags",
                (market_ref, risk_ref), now,
            )
        elif market.quality_flags:
            sniper_vote = self._vote(
                "sniper", "ABSTAIN", "Market snapshot has unresolved data-quality flags",
                (market_ref,), now,
            )
        elif (
            market.price_usd <= 0
            or market.liquidity_usd < self.min_liquidity_usd
            or market.volume_h1_usd < self.min_volume_h1_usd
            or market.pair_created_at_ms is None
            or now - market.pair_created_at_ms < self.min_pair_age_ms
        ):
            sniper_vote = self._vote(
                "sniper", "ABSTAIN",
                "Pool age, liquidity, volume or price does not meet the configured observation thresholds",
                (market_ref,), now,
            )
        else:
            sniper_vote = self._vote(
                "sniper", "SUPPORT",
                "Fresh market snapshot meets configured pool-age, liquidity and volume thresholds",
                (market_ref, risk_ref), now,
            )

        if not risk_fresh:
            shield_vote = self._vote(
                "shield", "ABSTAIN", "Risk evidence is stale", (risk_ref,), now
            )
        elif severe_flags:
            shield_vote = self._vote(
                "shield", "REJECT",
                "Critical/high deterministic security flags require rejection or manual resolution",
                tuple(f"risk_flag:{flag.code}" for flag in severe_flags), now,
            )
        elif risk.errors or risk.missing_claims or risk.risk_state != "EVIDENCE_COLLECTED_REVIEW_REQUIRED":
            shield_vote = self._vote(
                "shield", "ABSTAIN",
                "Required security claims remain missing or provider evidence contains errors",
                (risk_ref,), now,
            )
        elif medium_flags:
            shield_vote = self._vote(
                "shield", "ABSTAIN",
                "Medium-severity risk flags require independent review",
                tuple(f"risk_flag:{flag.code}" for flag in medium_flags), now,
            )
        else:
            shield_vote = self._vote(
                "shield", "APPROVE",
                "Configured security evidence is complete with no high-severity flags; human review still required",
                (risk_ref,), now,
            )

        votes = (scout_vote, whale_vote, sniper_vote, shield_vote)
        supporting = tuple(
            vote.agent for vote in votes
            if vote.agent in {"scout", "whale", "sniper"} and vote.stance == "SUPPORT"
        )
        support_count = len(supporting)
        if shield_vote.stance == "REJECT":
            status = "REJECTED"
            reasons = (shield_vote.reason,)
        elif shield_vote.stance == "ABSTAIN":
            status = "WATCH" if support_count else "ABSTAIN"
            reasons = (shield_vote.reason,) + tuple(
                vote.reason for vote in votes if vote.stance == "SUPPORT"
            )
        elif not market_fresh:
            status = "ABSTAIN"
            reasons = ("Market snapshot is stale; consensus approval is prohibited",)
        elif market.quality_flags:
            status = "WATCH" if support_count else "ABSTAIN"
            reasons = ("Market snapshot contains unresolved data-quality flags",)
        elif support_count >= 2:
            status = "CONSENSUS_APPROVED"
            reasons = (
                "At least two specialist analysis agents support the candidate",
                "Security evidence gate passed; this is not a trade authorization",
            )
        else:
            status = "ABSTAIN"
            reasons = ("Fewer than two analysis agents support the candidate",)

        decision_payload = {
            "mint": market.mint,
            "status": status,
            "votes": [
                {
                    "agent": vote.agent,
                    "stance": vote.stance,
                    "reason": vote.reason,
                    "evidence_refs": vote.evidence_refs,
                }
                for vote in votes
            ],
            "market_snapshot_id": market.snapshot_id,
            "risk_evidence_hash": risk.evidence_hash,
            "rule_version": "phase6-consensus-v1",
        }
        decision_id = hashlib.sha256(
            json.dumps(decision_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return validate_consensus_decision(ConsensusDecision(
            decision_id=decision_id,
            mint=market.mint,
            status=status,
            created_at_ms=now,
            votes=votes,
            support_count=support_count,
            supporting_agents=supporting,
            risk_state=risk.risk_state,
            missing_claims=risk.missing_claims,
            risk_flags=risk.risk_flags,
            reasons=reasons,
            market_snapshot_id=market.snapshot_id,
            risk_evidence_hash=risk.evidence_hash,
            paper_review_eligible=status == "CONSENSUS_APPROVED",
            execution_authorized=False,
        ))
