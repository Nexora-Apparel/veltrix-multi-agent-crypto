"""Fail-closed token safety evidence contract.

No provider is currently trusted by default. A boolean in a market-data payload
is not evidence that liquidity is locked or supply concentration is acceptable.
"""
from dataclasses import dataclass
from typing import Iterable

@dataclass(frozen=True)
class SafetyEvidence:
    token_address: str
    provider: str
    evidence_id: str
    observed_at_ms: int
    liquidity_locked: bool | None
    dev_hold_pct: float | None
    bundled_supply_pct: float | None
    creator_hold_pct: float | None = None

@dataclass(frozen=True)
class SafetyReport:
    token_address: str
    passed: bool
    checks: tuple[str, ...]
    reasons: tuple[str, ...]
    observed_at_ms: int
    creator_hold_pct: float | None = None

class TokenSafetyValidator:
    def __init__(self, trusted_providers: Iterable[str] = (), max_age_ms: int = 300_000):
        self.trusted_providers = frozenset(trusted_providers)
        self.max_age_ms = max_age_ms
        if max_age_ms <= 0:
            raise ValueError("max_age_ms must be positive")

    def validate(self, token_address: str, evidence: SafetyEvidence | None, now_ms: int) -> SafetyReport:
        reasons, checks = [], []
        if not token_address:
            reasons.append("missing token address")
        if evidence is None:
            reasons.append("no independent safety evidence")
            return SafetyReport(token_address, False, (), tuple(reasons), now_ms)
        if evidence.token_address != token_address:
            reasons.append("evidence token address mismatch")
        if evidence.provider not in self.trusted_providers:
            reasons.append("evidence provider is not trusted")
        if not evidence.evidence_id:
            reasons.append("missing evidence identifier")
        age = now_ms - evidence.observed_at_ms
        if age < 0 or age > self.max_age_ms:
            reasons.append("safety evidence is stale or timestamp is invalid")
        if evidence.liquidity_locked is not True:
            reasons.append("liquidity lock not positively verified")
        else:
            checks.append("liquidity_locked")
        dev = evidence.dev_hold_pct
        if dev is None or not 0 <= dev < 5:
            reasons.append("developer holdings are missing or not below 5%")
        else:
            checks.append("dev_holdings_below_5_percent")
        bundled = evidence.bundled_supply_pct
        if bundled is None or bundled != 0:
            reasons.append("bundled supply is missing or non-zero")
        else:
            checks.append("bundled_supply_zero")
        passed = not reasons and len(checks) == 3
        return SafetyReport(
            token_address, passed, tuple(checks), tuple(reasons),
            evidence.observed_at_ms, creator_hold_pct=evidence.creator_hold_pct,
        )
