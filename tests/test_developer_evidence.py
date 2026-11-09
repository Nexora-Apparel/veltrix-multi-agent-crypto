from types import SimpleNamespace

from veltrix.developer_concentration import (
    FundingGraphSnapshot, FundingRelationship, HolderBalance, HolderSnapshot,
)
from veltrix.developer_evidence import (
    DeveloperEvidenceOrchestrator, DeveloperRootAttestation,
)
from veltrix.ledger import PaperBroker


NOW = 5_000_000


class HolderProvider:
    def fetch_snapshot(self, token):
        return HolderSnapshot(
            "holder-index", "base", token, NOW - 100, "holders-1", "finalized:100",
            18, 1000, (HolderBalance("0xwallet", 1000),), True,
        )


class FundingProvider:
    def fetch_snapshot(self, token, wallets):
        return FundingGraphSnapshot(
            "funding-index", "base", token, NOW - 100, "funding-1", "finalized:100",
            0, NOW - 50, ("0xwallet", "0xdev"),
            (
                FundingRelationship("0xwallet", "0xdev", "0xtx", NOW - 100, "edge-1"),
                FundingRelationship("0xdev", None, None, NOW - 100, "edge-2"),
            ),
            True,
        )


class Adapter:
    def __init__(self):
        self.bundle = "not-called"
    def validate_composite_safety(self, token, chain, bundle, now):
        self.bundle = bundle
        return SimpleNamespace(passed=False, reasons=("developer provider not certified",))


def test_orchestrator_analyzes_and_persists_rejected_evidence(tmp_path):
    broker = PaperBroker(str(tmp_path / "audit.db"))
    adapter = Adapter()
    service = DeveloperEvidenceOrchestrator(
        holder_providers={"base": HolderProvider()},
        funding_providers={"base": FundingProvider()},
        analyzer=None,
        intelligence_adapters=adapter,
        trusted_root_providers=("independent-roots",),
        trusted_holder_providers=("holder-index",),
        trusted_funding_providers=("funding-index",),
        certified_provider_id=None,
        audit_sink=broker,
    )
    roots = DeveloperRootAttestation(
        "base", "0xtoken", "independent-roots", "root-proof-1",
        NOW - 100, ("0xdev",), True, "https://roots.example/root-proof-1",
        signature_verified=True, signing_key_fingerprint="a" * 64,
    )
    result = service.evaluate(
        chain="base", token_address="0xtoken", root_attestation=roots,
        liquidity_evidence=None, launch_bundle=None, now_ms=NOW,
    )
    assert result.report.coverage_complete
    assert result.report.dev_hold_pct == 100.0
    assert result.safety_report.passed is False
    assert adapter.bundle is None
    with broker._connect() as db:
        row = db.execute(
            "SELECT payload FROM events WHERE event='DEVELOPER_EVIDENCE_AUDIT'"
        ).fetchone()
    assert row is not None
    assert '"accepted": false' in row["payload"]
    assert "holders-1" in row["payload"]
    assert "funding-1" in row["payload"]


def test_untrusted_root_provider_is_rejected_and_audited(tmp_path):
    broker = PaperBroker(str(tmp_path / "audit.db"))
    service = DeveloperEvidenceOrchestrator(
        holder_providers={"base": HolderProvider()},
        funding_providers={"base": FundingProvider()},
        analyzer=None,
        intelligence_adapters=Adapter(),
        trusted_root_providers=(),
        trusted_holder_providers=("holder-index",),
        trusted_funding_providers=("funding-index",),
        audit_sink=broker,
    )
    roots = DeveloperRootAttestation(
        "base", "0xtoken", "untrusted", "proof", NOW - 10,
        ("0xdev",), True, "attestation://proof",
        signature_verified=True, signing_key_fingerprint="a" * 64,
    )
    result = service.evaluate(
        chain="base", token_address="0xtoken", root_attestation=roots,
        liquidity_evidence=None, launch_bundle=None, now_ms=NOW,
    )
    assert result.report.coverage_complete is False
    assert any("collection rejected" in x for x in result.report.reasons)
    assert result.safety_report.passed is False


class RootProvider:
    def __init__(self):
        self.calls = []
    def fetch_attestation(self, chain, token):
        self.calls.append((chain, token))
        return DeveloperRootAttestation(
            chain, token, "signed-root-registry", "signed-proof",
            NOW - 100, ("0xdev",), True, "https://roots.example/proof",
            signature_verified=True, signing_key_fingerprint="a" * 64,
        )


def test_orchestrator_fetches_signed_root_attestation_when_not_supplied(tmp_path):
    broker = PaperBroker(str(tmp_path / "signed-roots.db"))
    roots = RootProvider()
    service = DeveloperEvidenceOrchestrator(
        holder_providers={"base": HolderProvider()},
        funding_providers={"base": FundingProvider()},
        analyzer=None,
        intelligence_adapters=Adapter(),
        trusted_root_providers=("signed-root-registry",),
        trusted_holder_providers=("holder-index",),
        trusted_funding_providers=("funding-index",),
        root_attestation_provider=roots,
        audit_sink=broker,
    )
    result = service.evaluate(
        chain="base", token_address="0xtoken", root_attestation=None,
        liquidity_evidence=None, launch_bundle=None, now_ms=NOW,
    )
    assert roots.calls == [("base", "0xtoken")]
    assert result.report.coverage_complete
    assert result.report.root_provider == "signed-root-registry"


def test_unverified_root_object_cannot_enter_concentration_analysis(tmp_path):
    service = DeveloperEvidenceOrchestrator(
        holder_providers={"base": HolderProvider()},
        funding_providers={"base": FundingProvider()},
        analyzer=None,
        intelligence_adapters=Adapter(),
        trusted_root_providers=("independent-roots",),
        trusted_holder_providers=("holder-index",),
        trusted_funding_providers=("funding-index",),
    )
    roots = DeveloperRootAttestation(
        "base", "0xtoken", "independent-roots", "root-proof",
        NOW - 100, ("0xdev",), True, "https://roots.example/proof",
    )
    result = service.evaluate(
        chain="base", token_address="0xtoken", root_attestation=roots,
        liquidity_evidence=None, launch_bundle=None, now_ms=NOW,
    )
    assert result.report.coverage_complete is False
    assert any("verified Ed25519 signer provenance" in x for x in result.report.reasons)
