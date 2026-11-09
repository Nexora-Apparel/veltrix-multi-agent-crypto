"""Composite fail-closed safety gate for independently sourced evidence."""
from dataclasses import dataclass
import math
from typing import Iterable

from .safety import SafetyEvidence, SafetyReport
from .launch_bundles import LaunchBundleReport


@dataclass(frozen=True)
class SafetyEvidenceBundle:
    """Evidence claims remain separate so one provider cannot fill every gap."""
    liquidity: SafetyEvidence
    developer_holdings: SafetyEvidence
    launch_bundle: LaunchBundleReport


class CompositeTokenSafetyValidator:
    """Combine fresh, trusted, claim-specific evidence into the Sniper contract.

    Each evidence stream must come from a separately allowlisted provider.
    This is a provenance and consistency gate, not a guarantee that providers
    are truthful or that their underlying chain indexing is complete.
    """

    def __init__(
        self,
        trusted_liquidity_providers: Iterable[str] = (),
        trusted_developer_providers: Iterable[str] = (),
        trusted_bundle_providers: Iterable[str] = (),
        max_age_ms: int = 300_000,
    ):
        self.trusted_liquidity = frozenset(trusted_liquidity_providers)
        self.trusted_developer = frozenset(trusted_developer_providers)
        self.trusted_bundle = frozenset(trusted_bundle_providers)
        if max_age_ms <= 0:
            raise ValueError("max_age_ms must be positive")
        self.max_age_ms = max_age_ms

    def validate(
        self, token_address: str, bundle: SafetyEvidenceBundle | None, now_ms: int,
        chain: str,
    ) -> SafetyReport:
        reasons: list[str] = []
        checks: list[str] = []
        if not isinstance(token_address, str) or not token_address.strip():
            reasons.append("missing token address")
        chain_key = chain.lower() if isinstance(chain, str) else ""
        if chain_key not in {"solana", "base", "bsc"}:
            reasons.append("unsupported or missing token chain")
        if bundle is None or not isinstance(bundle, SafetyEvidenceBundle):
            return SafetyReport(token_address or "", False, (), ("composite safety evidence is missing",), now_ms)

        claims = (
            ("liquidity", bundle.liquidity, self.trusted_liquidity, "liquidity_locked"),
            ("developer holdings", bundle.developer_holdings, self.trusted_developer, "dev_hold_pct"),
        )
        provider_ids: list[str] = []
        for label, evidence, trusted, field in claims:
            if not isinstance(evidence, SafetyEvidence):
                reasons.append(f"{label}: typed evidence is missing")
                continue
            if evidence.token_address != token_address:
                reasons.append(f"{label}: token address mismatch")
            if evidence.provider not in trusted:
                reasons.append(f"{label}: provider is not trusted")
            if not evidence.evidence_id:
                reasons.append(f"{label}: evidence identifier is missing")
            age = now_ms - evidence.observed_at_ms
            if age < 0 or age > self.max_age_ms:
                reasons.append(f"{label}: evidence is stale or timestamp is invalid")
            if isinstance(evidence.provider, str) and evidence.provider:
                provider_ids.append(evidence.provider)
            if field == "liquidity_locked":
                if evidence.liquidity_locked is not True:
                    reasons.append("liquidity lock is not positively verified")
                else:
                    checks.append("liquidity_locked")
            else:
                try:
                    dev_pct = float(evidence.dev_hold_pct)
                except (TypeError, ValueError):
                    dev_pct = float("nan")
                if not math.isfinite(dev_pct) or not 0 <= dev_pct < 5:
                    reasons.append("developer holdings are missing or not below 5%")
                else:
                    checks.append("dev_holdings_below_5_percent")

        report = bundle.launch_bundle
        if not isinstance(report, LaunchBundleReport):
            reasons.append("launch bundle report is missing or malformed")
        else:
            if report.token_address != token_address:
                reasons.append("launch bundle token address mismatch")
            if report.chain.lower() != chain_key:
                reasons.append("launch bundle chain mismatch")
            if report.provider not in self.trusted_bundle:
                reasons.append("launch bundle provider is not trusted")
            age = now_ms - report.window_end_ms
            if age < 0 or age > self.max_age_ms:
                reasons.append("launch bundle evidence is stale or timestamp is invalid")
            if report.coverage_complete is not True or report.rejected:
                reasons.append("launch bundle coverage or evidence validation is incomplete")
            if report.bundled_supply_pct != 0:
                reasons.append("bundled supply is missing or non-zero")
            else:
                checks.append("bundled_supply_zero")
            if report.shared_funder_clusters != 0:
                reasons.append("launch bundle report contains shared-funder clusters")

        # Require three distinct provider identities for the three independent claims.
        if len(provider_ids) != 2 or len(set(provider_ids)) != 2:
            reasons.append("liquidity and developer claims must use distinct providers")
        if isinstance(report, LaunchBundleReport) and report.provider in provider_ids:
            reasons.append("launch bundle claim must use a provider distinct from other safety claims")

        passed = not reasons and len(checks) == 3
        timestamps = [
            item.observed_at_ms for item in (bundle.liquidity, bundle.developer_holdings)
            if isinstance(item, SafetyEvidence)
            and isinstance(item.observed_at_ms, int) and not isinstance(item.observed_at_ms, bool)
        ]
        if isinstance(report, LaunchBundleReport) and isinstance(report.window_end_ms, int):
            timestamps.append(report.window_end_ms)
        creator = (
            bundle.developer_holdings.creator_hold_pct
            if isinstance(bundle.developer_holdings, SafetyEvidence) else None
        )
        return SafetyReport(
            token_address, passed, tuple(checks), tuple(reasons),
            min(timestamps) if timestamps else now_ms,
            creator_hold_pct=creator, composite_verified=passed,
        )



def composite_validator_from_settings(settings) -> CompositeTokenSafetyValidator:
    """Construct claim-specific trust only from explicit environment settings."""
    return CompositeTokenSafetyValidator(
        trusted_liquidity_providers=settings.trusted_liquidity_providers,
        trusted_developer_providers=settings.trusted_developer_providers,
        trusted_bundle_providers=settings.trusted_bundle_providers,
        max_age_ms=settings.safety_evidence_max_age_ms,
    )
