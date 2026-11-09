"""Chain-neutral launch indexing contract and fail-closed composite-gate orchestration.

Concrete RPC/indexer clients must normalize chain-specific transaction data into
these records. A provider cannot claim complete coverage merely because a page
returned no rows: it must attest the requested interval and pagination completion.
"""
from dataclasses import dataclass
from typing import Iterable, Protocol

from .composite_safety import (
    CompositeTokenSafetyValidator,
    SafetyEvidenceBundle,
)
from .launch_bundles import LaunchBundleAnalyzer, LaunchBundleReport, LaunchBuyEvidence
from .safety import SafetyEvidence, SafetyReport


@dataclass(frozen=True)
class LaunchCoverage:
    """Auditable completeness declaration for one launch-window scan."""
    provider: str
    chain: str
    token_address: str
    requested_start_ms: int
    requested_end_ms: int
    indexed_through_ms: int
    pages_expected: int
    pages_completed: int
    source_complete: bool
    evidence_id: str


@dataclass(frozen=True)
class LaunchIndexSnapshot:
    """Normalized token launch metadata and launch-window buy observations."""
    provider: str
    chain: str
    token_address: str
    launch_at_ms: int
    total_supply: float | None
    supply_decimals: int | None
    observations: tuple[LaunchBuyEvidence, ...]
    coverage: LaunchCoverage
    observed_at_ms: int


class LaunchIndexProvider(Protocol):
    provider_id: str
    supported_chains: frozenset[str]

    def fetch_launch_snapshot(
        self, chain: str, token_address: str, launch_window_ms: int
    ) -> LaunchIndexSnapshot: ...


@dataclass(frozen=True)
class LaunchGateResult:
    bundle_report: LaunchBundleReport | None
    safety_report: SafetyReport
    provider_error: str | None = None


class LaunchIntelligenceService:
    """Index launch activity, validate coverage/provenance, then submit to safety.

    The three claim providers remain independent: liquidity, developer holdings,
    and launch indexing. This service never manufactures a passing report.
    """

    def __init__(
        self,
        provider: LaunchIndexProvider,
        analyzer: LaunchBundleAnalyzer,
        composite_validator: CompositeTokenSafetyValidator,
        trusted_launch_providers: Iterable[str] = (),
        launch_window_ms: int = 120_000,
    ):
        if launch_window_ms <= 0:
            raise ValueError("launch_window_ms must be positive")
        self.provider = provider
        self.analyzer = analyzer
        self.validator = composite_validator
        self.trusted_launch_providers = frozenset(trusted_launch_providers)
        self.launch_window_ms = launch_window_ms

    def evaluate(
        self,
        *,
        chain: str,
        token_address: str,
        liquidity_evidence: SafetyEvidence,
        developer_evidence: SafetyEvidence,
        now_ms: int,
    ) -> LaunchGateResult:
        provider_id = getattr(self.provider, "provider_id", None)
        supported = getattr(self.provider, "supported_chains", frozenset())
        if (not isinstance(provider_id, str)
                or provider_id not in self.trusted_launch_providers
                or chain not in supported):
            report = self._withheld_report(chain, token_address, provider_id or "")
            safety = self.validator.validate(
                token_address,
                SafetyEvidenceBundle(liquidity_evidence, developer_evidence, report),
                now_ms,
                chain,
            )
            return LaunchGateResult(report, safety, "launch provider is untrusted or chain unsupported")

        try:
            snapshot = self.provider.fetch_launch_snapshot(
                chain, token_address, self.launch_window_ms
            )
        except Exception:
            report = self._withheld_report(chain, token_address, provider_id)
            safety = self.validator.validate(
                token_address,
                SafetyEvidenceBundle(liquidity_evidence, developer_evidence, report),
                now_ms,
                chain,
            )
            return LaunchGateResult(report, safety, "launch index provider request failed")

        coverage_ok = self._coverage_is_complete(
            snapshot, provider_id, chain, token_address, now_ms
        )
        observations = snapshot.observations if isinstance(snapshot, LaunchIndexSnapshot) else ()
        launch_at = snapshot.launch_at_ms if isinstance(snapshot, LaunchIndexSnapshot) else now_ms
        supply = snapshot.total_supply if isinstance(snapshot, LaunchIndexSnapshot) else None
        report = self.analyzer.analyze(
            provider=provider_id,
            chain=chain,
            token_address=token_address,
            launch_at_ms=launch_at,
            total_supply=supply,
            observations=observations,
            coverage_complete=coverage_ok,
            now_ms=now_ms,
        )
        safety = self.validator.validate(
            token_address,
            SafetyEvidenceBundle(liquidity_evidence, developer_evidence, report),
            now_ms,
            chain,
        )
        return LaunchGateResult(report, safety)

    @staticmethod
    def _coverage_is_complete(snapshot, provider_id, chain, token_address, now_ms):
        if not isinstance(snapshot, LaunchIndexSnapshot):
            return False
        coverage = snapshot.coverage
        if not isinstance(coverage, LaunchCoverage):
            return False
        return bool(
            snapshot.provider == provider_id
            and snapshot.chain == chain
            and snapshot.token_address == token_address
            and snapshot.launch_at_ms <= now_ms
            and snapshot.observed_at_ms <= now_ms
            and coverage.provider == provider_id
            and coverage.chain == chain
            and coverage.token_address == token_address
            and coverage.evidence_id
            and coverage.requested_start_ms == snapshot.launch_at_ms
            and coverage.requested_end_ms == snapshot.launch_at_ms + (
                coverage.requested_end_ms - coverage.requested_start_ms
            )
            and coverage.requested_end_ms >= coverage.requested_start_ms
            and coverage.indexed_through_ms >= coverage.requested_end_ms
            and coverage.pages_expected >= 1
            and coverage.pages_completed == coverage.pages_expected
            and coverage.source_complete is True
            and coverage.requested_end_ms <= now_ms
        )

    @staticmethod
    def _withheld_report(chain, token_address, provider):
        return LaunchBundleReport(
            chain=chain,
            token_address=token_address,
            provider=provider,
            launch_at_ms=0,
            window_end_ms=0,
            coverage_complete=False,
            bundled_supply_pct=None,
            shared_funder_clusters=0,
            participating_wallets=0,
            accepted_transactions=0,
            rejected=("launch index evidence unavailable or untrusted",),
            notes=("No bundle percentage is certified without complete indexed coverage.",),
        )
