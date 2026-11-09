import base64
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from veltrix.providers.factory import build_solana_developer_evidence_worker
from veltrix.providers.helius_funding import HeliusSolanaFundingGraphProvider
from veltrix.providers.holder_inventory import SolanaHolderInventoryProvider
from veltrix.providers.signed_roots import SignedDeveloperRootProvider
from veltrix.solana_meme.developer_store import DeveloperConcentrationStore
from veltrix.solana_meme.developer_worker import SolanaDeveloperEvidenceWorker
from veltrix.solana_meme.launch_stream import LaunchStore


def settings(**overrides):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    values = dict(
        solana_rpc_url="https://rpc.example",
        helius_api_key="existing-helius-key",
        developer_root_attestation_url="https://roots.example/attest",
        developer_root_attestation_public_key=base64.b64encode(public).decode(),
        developer_root_attestation_provider_id="signed-roots",
        trusted_developer_root_providers=("signed-roots",),
        trusted_holder_inventory_providers=("solana-rpc-holder-inventory",),
        trusted_developer_funding_providers=("helius-solana-funding-graph",),
        holder_inventory_max_accounts=100,
        funding_graph_max_pages=2,
        funding_graph_max_wallets=10,
        funding_graph_max_total_pages=20,
        safety_evidence_max_age_ms=300_000,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_factory_builds_worker_only_when_all_three_provider_trusts_are_explicit(tmp_path):
    path = tmp_path / "factory.sqlite3"
    launches = LaunchStore(path)
    store = DeveloperConcentrationStore(path)
    worker = build_solana_developer_evidence_worker(settings(), launches, store)
    assert isinstance(worker, SolanaDeveloperEvidenceWorker)
    assert isinstance(worker.holder_provider, SolanaHolderInventoryProvider)
    assert isinstance(worker.funding_provider, HeliusSolanaFundingGraphProvider)
    assert isinstance(worker.root_provider, SignedDeveloperRootProvider)
    assert worker.holder_provider.provider_id == "solana-rpc-holder-inventory"
    assert worker.funding_provider.provider_id == "helius-solana-funding-graph"
    assert worker.root_provider.provider_id == "signed-roots"


def test_factory_stays_disabled_when_any_provider_trust_is_missing(tmp_path):
    path = tmp_path / "disabled.sqlite3"
    launches = LaunchStore(path)
    store = DeveloperConcentrationStore(path)
    for overrides in (
        {"trusted_developer_root_providers": ()},
        {"trusted_holder_inventory_providers": ()},
        {"trusted_developer_funding_providers": ()},
        {"developer_root_attestation_url": ""},
        {"helius_api_key": ""},
        {"solana_rpc_url": ""},
    ):
        assert build_solana_developer_evidence_worker(
            settings(**overrides), launches, store,
        ) is None


def test_bundle_factory_requires_explicit_launch_bundle_and_funding_trust(tmp_path):
    from veltrix.providers.factory import build_solana_launch_bundle_worker
    from veltrix.providers.helius_launch_index import HeliusSolanaLaunchIndexProvider
    from veltrix.solana_meme.bundle_store import LaunchBundleEvidenceStore
    from veltrix.solana_meme.bundle_worker import SolanaLaunchBundleWorker

    path = tmp_path / "bundle-factory.sqlite3"
    launches = LaunchStore(path)
    store = LaunchBundleEvidenceStore(path)
    config = settings(
        trusted_launch_origin_providers=("helius-launch-origin",),
        trusted_bundle_providers=("helius-solana-launch-index",),
        trusted_funding_providers=("helius-solana-funding-graph",),
        launch_window_ms=120_000,
        launch_index_max_pages=10,
        funding_graph_max_wallets=20,
        funding_graph_max_pages=2,
        funding_graph_max_total_pages=40,
    )
    worker = build_solana_launch_bundle_worker(config, launches, store)
    assert isinstance(worker, SolanaLaunchBundleWorker)
    assert isinstance(worker.provider, HeliusSolanaLaunchIndexProvider)
    assert worker.analyzer.trusted_providers == frozenset({"helius-solana-launch-index"})

    assert build_solana_launch_bundle_worker(
        settings(**{
            "trusted_launch_origin_providers": ("helius-launch-origin",),
            "trusted_bundle_providers": (),
            "trusted_funding_providers": ("helius-solana-funding-graph",),
        }), launches, store,
    ) is None
