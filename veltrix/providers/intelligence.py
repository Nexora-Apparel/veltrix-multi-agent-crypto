"""Fail-closed adapters for external wallet-flow and token-safety evidence.

These adapters normalize provider observations. They do not discover smart wallets,
prove that a provider is honest, or independently audit contracts. Provider trust
must be configured by the operator and backed by an external due-diligence process.
"""
from dataclasses import dataclass
import math
from typing import Iterable, Protocol

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
    creator_hold_pct: float | None = None
    claim: str | None = None
    chain: str | None = None
    coverage_complete: bool = False
    source_ref: str | None = None
    method: str | None = None
    independence_group: str | None = None


class SmartWalletProvider(Protocol):
    """Contract for an external indexed transaction source."""

    @property
    def provider_id(self) -> str: ...

    def fetch_wallet_observations(
        self, chain: str, token_addresses: tuple[str, ...], since_ms: int
    ) -> Iterable[SmartWalletObservation]: ...


class TokenSafetyProvider(Protocol):
    """Contract for an external liquidity/holder evidence source."""

    @property
    def provider_id(self) -> str: ...

    def fetch_safety_observation(
        self, chain: str, token_address: str
    ) -> TokenSafetyObservation | None: ...


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

    @classmethod
    def from_settings(cls, settings):
        """Build the trust boundary from validated application settings."""
        adapter = cls(
            trusted_wallet_providers=settings.trusted_wallet_providers,
            trusted_safety_providers=settings.trusted_safety_providers,
            max_wallet_age_ms=settings.wallet_evidence_max_age_ms,
            max_safety_age_ms=settings.safety_evidence_max_age_ms,
        )
        # Composite trust roles are deliberately configured independently.
        # Empty or overlapping allowlists remain fail-closed at validation time.
        from ..composite_safety import composite_validator_from_settings
        adapter.set_composite_safety_validator(composite_validator_from_settings(settings))
        return adapter

    def collect_wallet_observations(
        self, provider: SmartWalletProvider, chain: str,
        token_addresses: Iterable[str], since_ms: int, now_ms: int,
    ) -> AdapterBatch:
        provider_id = getattr(provider, "provider_id", None)
        if not isinstance(provider_id, str) or provider_id not in self.trusted_wallet_providers:
            return AdapterBatch((), ("whale: provider identity is not trusted",))
        try:
            observations = provider.fetch_wallet_observations(
                chain, tuple(token_addresses), since_ms
            )
            normalized = tuple(observations)
        except Exception:
            return AdapterBatch((), ("whale: trusted provider request failed",))
        mismatched = [
            f"whale: provider identity mismatch on observation {index}"
            for index, item in enumerate(normalized)
            if not isinstance(item, SmartWalletObservation) or item.provider != provider_id
        ]
        matching = [item for item in normalized
                    if isinstance(item, SmartWalletObservation) and item.provider == provider_id]
        batch = self.adapt_wallet_observations(matching, now_ms)
        return AdapterBatch(batch.accepted, tuple(mismatched) + batch.rejected)

    def collect_safety_observation(
        self, provider: TokenSafetyProvider, chain: str,
        token_address: str, now_ms: int,
    ) -> SafetyReport:
        provider_id = getattr(provider, "provider_id", None)
        if not isinstance(provider_id, str) or provider_id not in self.trusted_safety_providers:
            return self.safety_validator.validate(token_address, None, now_ms)
        try:
            observation = provider.fetch_safety_observation(chain, token_address)
        except Exception:
            observation = None
        if observation is not None and (
            not isinstance(observation, TokenSafetyObservation) or observation.provider != provider_id
        ):
            observation = TokenSafetyObservation(
                provider="provider-identity-mismatch",
                token_address=getattr(observation, "token_address", ""),
                evidence_id=getattr(observation, "evidence_id", ""),
                observed_at_ms=getattr(observation, "observed_at_ms", now_ms),
                liquidity_locked=getattr(observation, "liquidity_locked", None),
                dev_hold_pct=getattr(observation, "dev_hold_pct", None),
                bundled_supply_pct=getattr(observation, "bundled_supply_pct", None),
                creator_hold_pct=getattr(observation, "creator_hold_pct", None),
                claim=getattr(observation, "claim", None),
                chain=getattr(observation, "chain", None),
                coverage_complete=getattr(observation, "coverage_complete", False),
                source_ref=getattr(observation, "source_ref", None),
                method=getattr(observation, "method", None),
                independence_group=getattr(observation, "independence_group", None),
            )
        return self.validate_safety_observation(token_address, observation, now_ms)

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
            # Namespace transaction IDs by chain, wallet and token. This prevents
            # false collisions across chains and preserves distinct token legs.
            if chain in {"base", "bsc"}:
                tx_key = (
                    chain, item.wallet_address.lower(),
                    item.transaction_id.lower(), item.token_address.lower(),
                )
            else:
                # Solana public keys and signatures are base58 and case-sensitive.
                tx_key = (
                    chain, item.wallet_address, item.transaction_id,
                    item.token_address,
                )
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

    def validate_composite_safety(self, token_address, chain, evidence_bundle, now_ms):
        """Validate claim-separated liquidity, developer and launch-bundle evidence."""
        from ..composite_safety import CompositeTokenSafetyValidator

        validator = getattr(self, "composite_safety_validator", None)
        if not isinstance(validator, CompositeTokenSafetyValidator):
            return SafetyReport(
                token_address, False, (),
                ("claim-specific composite safety provider allowlists are not configured",),
                now_ms,
            )
        return validator.validate(token_address, evidence_bundle, now_ms, chain=chain)

    def set_composite_safety_validator(self, validator):
        """Install an explicitly configured claim-specific composite validator."""
        from ..composite_safety import CompositeTokenSafetyValidator

        if not isinstance(validator, CompositeTokenSafetyValidator):
            raise TypeError("validator must be a CompositeTokenSafetyValidator")
        self.composite_safety_validator = validator

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
            creator_hold_pct=observation.creator_hold_pct,
            claim=observation.claim, chain=observation.chain,
            coverage_complete=observation.coverage_complete,
            source_ref=observation.source_ref, method=observation.method,
            independence_group=observation.independence_group,
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
        return AgentEvidenceDecision("whale", accepted, tuple(reasons))

    @staticmethod
    def sniper_decision(report: SafetyReport) -> AgentEvidenceDecision:
        return AgentEvidenceDecision(
            "sniper", report.passed,
            () if report.passed else tuple(f"sniper: {reason}" for reason in report.reasons),
        )
