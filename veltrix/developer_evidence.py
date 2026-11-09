"""Unified developer evidence orchestration and composite-gate submission.

This service coordinates independently sourced holder, funding and root
attestation evidence. It never promotes an untrusted root list or incomplete
provider response into a developer-holdings claim.
"""
from dataclasses import dataclass
from typing import Iterable

from .composite_safety import SafetyEvidenceBundle
from .developer_concentration import DeveloperConcentrationAnalyzer


@dataclass(frozen=True)
class DeveloperRootAttestation:
    chain: str
    token_address: str
    provider: str
    evidence_id: str
    observed_at_ms: int
    roots: tuple[str, ...]
    coverage_complete: bool
    source_ref: str
    signature_verified: bool = False
    signing_key_fingerprint: str | None = None


@dataclass(frozen=True)
class DeveloperEvidenceResult:
    report: object
    safety_report: object
    audit: dict


class DeveloperEvidenceOrchestrator:
    """Collect, analyze, gate and persist developer-concentration evidence."""

    def __init__(self, *, holder_providers, funding_providers, analyzer,
                 intelligence_adapters, trusted_root_providers=(),
                 trusted_holder_providers=(), trusted_funding_providers=(),
                 root_attestation_provider=None, certified_provider_id=None, independence_group=None,
                 audit_sink=None):
        self.holder_providers = dict(holder_providers)
        self.funding_providers = dict(funding_providers)
        self.analyzer = analyzer or DeveloperConcentrationAnalyzer()
        self.adapters = intelligence_adapters
        self.trusted_roots = frozenset(trusted_root_providers)
        self.trusted_holders = frozenset(trusted_holder_providers)
        self.trusted_funding = frozenset(trusted_funding_providers)
        self.root_attestation_provider = root_attestation_provider
        self.certified_provider_id = certified_provider_id
        self.independence_group = independence_group
        self.audit_sink = audit_sink

    @staticmethod
    def _blocked(chain, token, now_ms, reason):
        from .developer_concentration import DeveloperConcentrationReport
        return DeveloperConcentrationReport(
            chain=chain, token_address=token, provider=None, linked_wallets=(),
            linked_supply_raw=None, total_supply_raw=None, dev_hold_pct=None,
            coverage_complete=False, observed_at_ms=now_ms, evidence_id=None,
            source_ref=None, method="blocked_before_analysis",
            independence_group=None, holder_provider=None, funding_provider=None,
            root_provider=None, reasons=(reason,),
        )

    def evaluate(self, *, chain, token_address, root_attestation,
                 liquidity_evidence, launch_bundle, now_ms):
        chain = str(chain).lower()
        holder = funding = None
        try:
            if root_attestation is None:
                if self.root_attestation_provider is None:
                    raise ValueError("no signed developer-root attestation provider is configured")
                root_attestation = self.root_attestation_provider.fetch_attestation(chain, token_address)
            holder_provider = self.holder_providers.get(chain)
            funding_provider = self.funding_providers.get(chain)
            if holder_provider is None or funding_provider is None:
                raise ValueError("independent holder and funding providers are required")
            holder = holder_provider.fetch_snapshot(token_address)
            if holder.provider not in self.trusted_holders:
                raise ValueError("holder inventory provider is not trusted")
            funding = funding_provider.fetch_snapshot(
                token_address, [row.wallet_address for row in holder.holders]
            )
            if funding.provider not in self.trusted_funding:
                raise ValueError("developer funding graph provider is not trusted")
            if not isinstance(root_attestation, DeveloperRootAttestation):
                raise ValueError("typed independently attested developer roots are required")
            root_chain = str(root_attestation.chain).lower()
            token_matches = (
                root_attestation.token_address.lower() == token_address.lower()
                if chain in {"base", "bsc"}
                else root_attestation.token_address == token_address
            )
            if root_chain != chain or not token_matches:
                raise ValueError("developer-root attestation chain or token mismatch")
            if root_attestation.provider not in self.trusted_roots:
                raise ValueError("developer-root attestation provider is not trusted")
            if not root_attestation.coverage_complete or not root_attestation.roots:
                raise ValueError("developer-root attestation is incomplete")
            if (root_attestation.signature_verified is not True
                    or not isinstance(root_attestation.signing_key_fingerprint, str)
                    or len(root_attestation.signing_key_fingerprint) != 64):
                raise ValueError("developer-root attestation lacks verified Ed25519 signer provenance")
            if (not root_attestation.evidence_id or not root_attestation.source_ref
                    or not isinstance(root_attestation.observed_at_ms, int)
                    or isinstance(root_attestation.observed_at_ms, bool)
                    or root_attestation.observed_at_ms > now_ms):
                raise ValueError("developer-root attestation provenance or timestamp is invalid")
            report = self.analyzer.analyze(
                chain, token_address, holder, funding, root_attestation.roots,
                root_provider=root_attestation.provider,
                root_evidence_id=root_attestation.evidence_id,
                now_ms=now_ms,
                certified_provider_id=self.certified_provider_id,
                independence_group=self.independence_group,
            )
            # The root attestation itself must be fresh; analyzer independently
            # checks holder, funding graph and each edge.
            if now_ms - root_attestation.observed_at_ms > self.analyzer.max_age_ms:
                report = self._blocked(chain, token_address, now_ms,
                                       "developer-root attestation is stale")
        except Exception as exc:
            # Do not persist exception text: provider exceptions can contain
            # request metadata. Record a stable rejection category only.
            report = self._blocked(chain, token_address, now_ms,
                                   f"evidence collection rejected: {type(exc).__name__}")

        developer_evidence = report.to_safety_evidence()
        bundle = None
        if developer_evidence is not None and liquidity_evidence is not None and launch_bundle is not None:
            bundle = SafetyEvidenceBundle(
                liquidity=liquidity_evidence,
                developer_holdings=developer_evidence,
                launch_bundle=launch_bundle,
            )
        safety = self.adapters.validate_composite_safety(
            token_address, chain, bundle, now_ms
        )
        audit = {
            "chain": chain,
            "token": token_address,
            "accepted": bool(safety.passed and report.coverage_complete),
            "developer_report": report.__dict__,
            "composite_safety": safety.__dict__,
            "holder_provider": getattr(holder, "provider", None),
            "holder_evidence_id": getattr(holder, "evidence_id", None),
            "holder_block_ref": getattr(holder, "block_ref", None),
            "funding_provider": getattr(funding, "provider", None),
            "funding_evidence_id": getattr(funding, "evidence_id", None),
            "funding_block_ref": getattr(funding, "block_ref", None),
            "root_provider": getattr(root_attestation, "provider", None),
            "root_evidence_id": getattr(root_attestation, "evidence_id", None),
            "root_source_ref": getattr(root_attestation, "source_ref", None),
            "root_signature_verified": getattr(root_attestation, "signature_verified", False),
            "root_signing_key_fingerprint": getattr(root_attestation, "signing_key_fingerprint", None),
            "reasons": list(report.reasons) + list(safety.reasons),
        }
        if self.audit_sink is not None:
            self.audit_sink.record_developer_evidence(chain, token_address, audit)
        return DeveloperEvidenceResult(report=report, safety_report=safety, audit=audit)
