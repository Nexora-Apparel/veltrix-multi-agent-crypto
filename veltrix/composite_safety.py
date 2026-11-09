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
        trusted_funding_providers: Iterable[str] = (),
        max_age_ms: int = 300_000,
    ):
        self.trusted_liquidity = frozenset(trusted_liquidity_providers)
        self.trusted_developer = frozenset(trusted_developer_providers)
        self.trusted_bundle = frozenset(trusted_bundle_providers)
        self.trusted_funding = frozenset(trusted_funding_providers)
        if max_age_ms <= 0:
            raise ValueError("max_age_ms must be positive")
        self.max_age_ms = max_age_ms

    @staticmethod
    def _same_token(chain: str, left: str, right: str) -> bool:
        if not isinstance(left, str) or not isinstance(right, str):
            return False
        return left.lower() == right.lower() if chain in {"base", "bsc"} else left == right

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
            if not self._same_token(chain_key, evidence.token_address, token_address):
                reasons.append(f"{label}: token address mismatch")
            if not isinstance(evidence.provider, str) or evidence.provider not in trusted:
                reasons.append(f"{label}: provider is not trusted")
            if not isinstance(evidence.evidence_id, str) or not evidence.evidence_id.strip():
                reasons.append(f"{label}: evidence identifier is missing")
            expected_claim = "liquidity_lock" if label == "liquidity" else "developer_holdings"
            if evidence.claim != expected_claim:
                reasons.append(f"{label}: unsupported or mismatched evidence claim")
            if not isinstance(evidence.chain, str) or evidence.chain.lower() != chain_key:
                reasons.append(f"{label}: evidence chain is missing or mismatched")
            if evidence.coverage_complete is not True:
                reasons.append(f"{label}: evidence coverage is not certified complete")
            if not isinstance(evidence.source_ref, str) or not evidence.source_ref.strip():
                reasons.append(f"{label}: independently auditable source reference is missing")
            if not isinstance(evidence.method, str) or not evidence.method.strip():
                reasons.append(f"{label}: evidence method is not declared")
            if not isinstance(evidence.independence_group, str) or not evidence.independence_group.strip():
                reasons.append(f"{label}: provider independence group is missing")
            if (not isinstance(evidence.observed_at_ms, int)
                    or isinstance(evidence.observed_at_ms, bool)):
                reasons.append(f"{label}: evidence timestamp is invalid")
            else:
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
                    if isinstance(evidence.dev_hold_pct, bool):
                        dev_pct = float("nan")
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
            if not self._same_token(chain_key, report.token_address, token_address):
                reasons.append("launch bundle token address mismatch")
            if not isinstance(report.chain, str) or report.chain.lower() != chain_key:
                reasons.append("launch bundle chain mismatch")
            if not isinstance(report.provider, str) or report.provider not in self.trusted_bundle:
                reasons.append("launch bundle provider is not trusted")
            valid_window = (
                isinstance(report.launch_at_ms, int) and not isinstance(report.launch_at_ms, bool)
                and isinstance(report.window_end_ms, int) and not isinstance(report.window_end_ms, bool)
                and report.launch_at_ms <= report.window_end_ms
            )
            if not valid_window:
                reasons.append("launch bundle time range is invalid")
            elif report.window_end_ms > now_ms:
                reasons.append("launch bundle evidence window is in the future")
            if report.coverage_complete is not True or report.rejected:
                reasons.append("launch bundle coverage or evidence validation is incomplete")
            if not isinstance(report.evidence_id, str) or not report.evidence_id.strip():
                reasons.append("launch bundle evidence identifier is missing")
            if (not isinstance(report.observed_at_ms, int)
                    or isinstance(report.observed_at_ms, bool)
                    or report.observed_at_ms > now_ms
                    or now_ms - report.observed_at_ms > self.max_age_ms):
                reasons.append("launch bundle report provenance is stale or invalid")
            if (not valid_window or not isinstance(report.scanned_through_ms, int)
                    or isinstance(report.scanned_through_ms, bool)
                    or report.scanned_through_ms < report.window_end_ms):
                reasons.append("launch indexer did not prove coverage through window end")
            if report.supply_source != report.provider or not report.supply_block_ref:
                reasons.append("historical launch-time supply provenance is missing")
            if (not isinstance(report.pages_scanned, int)
                    or isinstance(report.pages_scanned, bool) or report.pages_scanned < 1):
                reasons.append("launch indexing page coverage is missing")
            if (not isinstance(report.provenance, (tuple, list)) or not report.provenance
                    or any(not isinstance(item, str) or not item for item in report.provenance)):
                reasons.append("launch indexing provenance records are missing")
            if (not isinstance(report.supply_observed_at_ms, int)
                    or isinstance(report.supply_observed_at_ms, bool)
                    or report.supply_observed_at_ms > now_ms
                    or now_ms - report.supply_observed_at_ms > self.max_age_ms):
                reasons.append("launch-time supply evidence is stale or invalid")
            accepted_count = report.accepted_transactions
            if (not isinstance(accepted_count, int) or isinstance(accepted_count, bool)
                    or accepted_count < 0):
                reasons.append("launch transaction count is invalid")
                accepted_count = 0
            funder_providers = report.funder_providers
            if not isinstance(funder_providers, (tuple, list)) or any(
                not isinstance(provider, str) or not provider for provider in funder_providers
            ):
                reasons.append("funding provider provenance is malformed")
                funder_providers = ()
            if accepted_count > 0 and (
                not funder_providers or any(
                    provider not in self.trusted_funding for provider in funder_providers
                )
            ):
                reasons.append("shared-funder evidence provider is missing or untrusted")
            if accepted_count > 0 and (
                set(funder_providers) & (set(provider_ids) | {report.provider})
            ):
                reasons.append("funding trace provider must be independent from other safety claims")
            try:
                bundled_pct = float(report.bundled_supply_pct)
                if isinstance(report.bundled_supply_pct, bool):
                    bundled_pct = float("nan")
            except (TypeError, ValueError):
                bundled_pct = float("nan")
            if not math.isfinite(bundled_pct) or bundled_pct != 0:
                reasons.append("bundled supply is missing or non-zero")
            else:
                checks.append("bundled_supply_zero")
            if (not isinstance(report.shared_funder_clusters, int)
                    or isinstance(report.shared_funder_clusters, bool)
                    or report.shared_funder_clusters != 0):
                reasons.append("launch bundle report contains shared-funder clusters")

        # Require three distinct provider identities for the three independent claims.
        if len(provider_ids) != 2 or len(set(provider_ids)) != 2:
            reasons.append("liquidity and developer claims must use distinct providers")
        groups = [
            item.independence_group for item in (bundle.liquidity, bundle.developer_holdings)
            if isinstance(item, SafetyEvidence) and isinstance(item.independence_group, str)
            and item.independence_group.strip()
        ]
        if len(groups) != 2 or len(set(groups)) != 2:
            reasons.append("liquidity and developer evidence must have independent provider ownership")
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
        trusted_liquidity_providers=getattr(settings, "trusted_liquidity_providers", ()),
        trusted_developer_providers=getattr(settings, "trusted_developer_providers", ()),
        trusted_bundle_providers=getattr(settings, "trusted_bundle_providers", ()),
        trusted_funding_providers=getattr(settings, "trusted_funding_providers", ()),
        max_age_ms=getattr(settings, "safety_evidence_max_age_ms", 300_000),
    )
