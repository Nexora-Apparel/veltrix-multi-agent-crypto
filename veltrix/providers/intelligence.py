"""Fail-closed adapters for external wallet-flow and token-safety evidence.

These adapters normalize provider observations. They do not discover smart wallets,
prove that a provider is honest, or independently audit contracts. Provider trust
must be configured by the operator and backed by an external due-diligence process.
"""
from dataclasses import dataclass
import math
from typing import Iterable

from ..safety import SafetyEvidence, SafetyReport, TokenSafetyValidator
from ..whales import WalletBuyEvent


SUPPORTED_CHAINS = frozenset({"solana", "base", "bsc"})


@dataclass(frozen=True)
class SmartWalletObservation:
    provider: str
    chain: str
    token_address: str
    wallet_address: str
    side: str
    notional_usd: float
    transaction_id: str
    observed_at_ms: int


@dataclass(frozen=True)
class TokenSafetyObservation:
    provider: str
    token_address: str
    evidence_id: str
    observed_at_ms: int
    liquidity_locked: bool | None
    dev_hold_pct: float | None
    bundled_supply_pct: float | None


@dataclass(frozen=True)
class AdapterBatch:
    accepted: tuple[WalletBuyEvent, ...]
    rejected: tuple[str, ...]


@dataclass(frozen=True)
class AgentEvidenceDecision:
    agent: str
    accepted: bool
    reasons: tuple[str, ...]


class VerifiedIntelligenceAdapters:
    """Normalize observations only from explicitly trusted provider identities."""

    def __init__(
        self,
        trusted_wallet_providers: Iterable[str] = (),
        trusted_safety_providers: Iterable[str] = (),
        max_wallet_age_ms: int = 300_000,
        max_safety_age_ms: int = 300_000,
    ):
        if max_wallet_age_ms <= 0 or max_safety_age_ms <= 0:
            raise ValueError("Evidence freshness windows must be positive")
        self.trusted_wallet_providers = frozenset(trusted_wallet_providers)
        self.trusted_safety_providers = frozenset(trusted_safety_providers)
        self.max_wallet_age_ms = max_wallet_age_ms
        self.safety_validator = TokenSafetyValidator(
            self.trusted_safety_providers, max_age_ms=max_safety_age_ms
        )

    def adapt_wallet_observations(
        self, observations: Iterable[SmartWalletObservation], now_ms: int
    ) -> AdapterBatch:
        accepted, rejected, seen_transactions = [], [], set()
        for index, item in enumerate(observations):
            prefix = f"whale: observation {index}"
            if not isinstance(item.provider, str) or item.provider not in self.trusted_wallet_providers:
                rejected.append(f"{prefix}: provider is not trusted")
                continue
            chain = item.chain.lower() if isinstance(item.chain, str) else ""
            if chain not in SUPPORTED_CHAINS:
                rejected.append(f"{prefix}: unsupported or missing chain")
                continue
            if not isinstance(item.token_address, str) or not item.token_address or not isinstance(item.wallet_address, str) or not item.wallet_address:
                rejected.append(f"{prefix}: token or wallet address is missing")
                continue
            if not isinstance(item.side, str) or item.side.upper() != "BUY":
                rejected.append(f"{prefix}: observation is not a buy")
                continue
            if not isinstance(item.transaction_id, str) or not item.transaction_id:
                rejected.append(f"{prefix}: transaction identifier is missing")
                continue
            if not isinstance(item.observed_at_ms, int) or isinstance(item.observed_at_ms, bool):
                rejected.append(f"{prefix}: observation timestamp is invalid")
                continue
            age = now_ms - item.observed_at_ms
            if age < 0 or age > self.max_wallet_age_ms:
                rejected.append(f"{prefix}: observation is stale or timestamp is invalid")
                continue
            try:
                notional = float(item.notional_usd)
            except (TypeError, ValueError):
                rejected.append(f"{prefix}: notional is invalid")
                continue
            if not math.isfinite(notional) or notional <= 0:
                rejected.append(f"{prefix}: notional must be finite and positive")
                continue
            tx_key = (chain, item.transaction_id)
            if tx_key in seen_transactions:
                rejected.append(f"{prefix}: duplicate transaction")
                continue
            seen_transactions.add(tx_key)
            accepted.append(WalletBuyEvent(
                chain=chain,
                token_address=item.token_address,
                wallet_address=item.wallet_address,
                observed_at_ms=item.observed_at_ms,
                notional_usd=notional,
                source=item.provider,
            ))
        return AdapterBatch(tuple(accepted), tuple(rejected))

    def validate_safety_observation(
        self, token_address: str, observation: TokenSafetyObservation | None, now_ms: int
    ) -> SafetyReport:
        if observation is None:
            return self.safety_validator.validate(token_address, None, now_ms)
        evidence = SafetyEvidence(
            token_address=observation.token_address,
            provider=observation.provider,
            evidence_id=observation.evidence_id,
            observed_at_ms=observation.observed_at_ms,
            liquidity_locked=observation.liquidity_locked,
            dev_hold_pct=observation.dev_hold_pct,
            bundled_supply_pct=observation.bundled_supply_pct,
        )
        return self.safety_validator.validate(token_address, evidence, now_ms)

    @staticmethod
    def whale_decision(batch: AdapterBatch, cluster_count: int) -> AgentEvidenceDecision:
        reasons = list(batch.rejected)
        if not batch.accepted:
            reasons.append("whale: no trusted, fresh buy observations")
        if cluster_count < 1:
            reasons.append("whale: no qualifying multi-wallet cluster")
        accepted = bool(batch.accepted) and cluster_count >= 1
        if not accepted and cluster_count < 1:
            reasons.append("whale: no qualifying multi-wallet cluster")
        return AgentEvidenceDecision("whale", accepted, tuple(reasons))

    @staticmethod
    def sniper_decision(report: SafetyReport) -> AgentEvidenceDecision:
        return AgentEvidenceDecision(
            "sniper", report.passed,
            () if report.passed else tuple(f"sniper: {reason}" for reason in report.reasons),
        )
