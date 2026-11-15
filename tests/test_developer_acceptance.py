from types import SimpleNamespace

from veltrix.developer_acceptance import run_developer_acceptance
from veltrix.developer_concentration import (
    FundingGraphSnapshot, FundingRelationship, HolderBalance, HolderSnapshot,
)
from veltrix.developer_evidence import DeveloperRootAttestation


NOW = 5_000_000
TOKEN = "0x" + "aa" * 20
WALLET = "0x" + "11" * 20
ROOT = "0x" + "22" * 20


class HolderProvider:
    provider_id = "base-rpc-holder-inventory"
    def __init__(self):
        self.deployment_block = None
    def fetch_snapshot(self, token, deployment_block):
        self.deployment_block = deployment_block
        return HolderSnapshot(
            self.provider_id, "base", token, NOW - 10, "holder-proof",
            "finalized-block:200", 18, 100,
            (HolderBalance(WALLET, 100),), True,
        )


class FundingProvider:
    provider_id = "alchemy-base-funding-graph"
    def fetch_snapshot(self, token, wallets):
        return FundingGraphSnapshot(
            self.provider_id, "base", token, NOW - 10, "funding-proof",
            "finalized-block:200", 0, NOW, (WALLET, ROOT),
            (
                FundingRelationship(WALLET, ROOT, "0xtx", NOW - 10, "edge-1"),
                FundingRelationship(ROOT, None, None, NOW - 10, "edge-2"),
            ), True,
        )


class RootProvider:
    def fetch_attestation(self, chain, token):
        return DeveloperRootAttestation(
            chain, token, "independent-root-service", "root-proof",
            NOW - 10, (ROOT,), True, "https://roots.example/proof",
            signature_verified=True, signing_key_fingerprint="a" * 64,
        )


class OriginResolver:
    def __init__(self):
        self.calls = []
    def resolve(self, token, launch_window_ms):
        self.calls.append((token, launch_window_ms))
        return SimpleNamespace(chain="base", complete=True, launch_block=123,
                               provider="base-rpc-launch-origin", evidence_id="origin-proof")


def cfg(**overrides):
    values = {
        "launch_window_ms": 120_000,
        "safety_evidence_max_age_ms": 300_000,
        "trusted_developer_root_providers": ("independent-root-service",),
        "trusted_holder_inventory_providers": ("base-rpc-holder-inventory",),
        "trusted_developer_funding_providers": ("alchemy-base-funding-graph",),
        "trusted_launch_origin_providers": ("base-rpc-launch-origin",),
        "trusted_developer_providers": ("developer-graph-provider",),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_live_acceptance_uses_resolved_evm_deployment_block_and_reports_structural_pass():
    holder = HolderProvider()
    origin = OriginResolver()
    result = run_developer_acceptance(
        chain="base", token_address=TOKEN, settings=cfg(),
        holder_providers={"base": holder},
        funding_providers={"base": FundingProvider()},
        root_provider=RootProvider(), origin_resolvers={"base": origin},
        now_ms=NOW,
    )
    assert holder.deployment_block == 123
    assert origin.calls == [(TOKEN, 120_000)]
    assert result["structural_pass"] is True
    assert result["trust_ready"] is True
    assert result["trust"]["launch_origin_provider_trusted"] is True
    assert result["capability_certified"] is False
    assert result["concentration"]["developer_hold_pct"] == 100.0
    assert result["live_execution_enabled"] is False


def test_structural_pass_does_not_imply_provider_trust_or_certification():
    result = run_developer_acceptance(
        chain="base", token_address=TOKEN,
        settings=cfg(trusted_developer_root_providers=()),
        holder_providers={"base": HolderProvider()},
        funding_providers={"base": FundingProvider()},
        root_provider=RootProvider(), origin_resolvers={"base": OriginResolver()},
        now_ms=NOW,
    )
    assert result["structural_pass"] is True
    assert result["trust_ready"] is False
    assert result["capability_certified"] is False



def test_developer_acceptance_rejects_unverified_root_attestation():
    class UnverifiedRootProvider:
        def fetch_attestation(self, chain, token):
            return DeveloperRootAttestation(
                chain, token, "independent-root-service", "root-proof",
                NOW - 10, (ROOT,), True, "https://roots.example/proof",
                signature_verified=False, signing_key_fingerprint="a" * 64,
            )
    result = None
    try:
        run_developer_acceptance(
            chain="base", token_address=TOKEN, settings=cfg(),
            holder_providers={"base": HolderProvider()},
            funding_providers={"base": FundingProvider()},
            root_provider=UnverifiedRootProvider(),
            origin_resolvers={"base": OriginResolver()}, now_ms=NOW,
        )
    except ValueError as exc:
        result = str(exc)
    assert result == "Developer-root attestation is not verified, complete and token-bound"



def test_evm_developer_acceptance_does_not_ignore_untrusted_launch_origin():
    result = run_developer_acceptance(
        chain="base", token_address=TOKEN,
        settings=cfg(trusted_launch_origin_providers=()),
        holder_providers={"base": HolderProvider()},
        funding_providers={"base": FundingProvider()},
        root_provider=RootProvider(), origin_resolvers={"base": OriginResolver()},
        now_ms=NOW,
    )
    assert result["structural_pass"] is True
    assert result["trust"]["launch_origin_provider_trusted"] is False
    assert result["trust_ready"] is False
    assert result["capability_certified"] is False



def test_developer_acceptance_requires_the_expected_composite_claim_provider_allowlist():
    result = run_developer_acceptance(
        chain="base", token_address=TOKEN,
        settings=cfg(trusted_developer_providers=("unrelated-provider",)),
        holder_providers={"base": HolderProvider()},
        funding_providers={"base": FundingProvider()},
        root_provider=RootProvider(), origin_resolvers={"base": OriginResolver()},
        now_ms=NOW,
    )
    assert result["structural_pass"] is True
    assert result["trust"]["developer_claim_provider_trusted"] is False
    assert result["trust_ready"] is False
    assert result["capability_certified"] is False
