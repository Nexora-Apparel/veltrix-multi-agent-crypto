from types import SimpleNamespace

from veltrix.providers.factory import build_solana_risk_evidence_collector
from veltrix.providers.goplus import GoPlusTokenSafetyProvider
from veltrix.providers.holder_inventory import SolanaHolderInventoryProvider
from veltrix.providers.solana_mint import SolanaMintAuthorityProvider
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceCollector


def test_factory_reuses_existing_credentials_and_builds_all_read_only_sources():
    settings = SimpleNamespace(
        goplus_access_token="configured-goplus-token",
        solana_rpc_url="https://rpc.example",
        safety_evidence_max_age_ms=90_000,
        holder_inventory_max_accounts=1234,
    )
    collector = build_solana_risk_evidence_collector(settings)
    assert isinstance(collector, SolanaRiskEvidenceCollector)
    assert isinstance(collector.goplus_provider, GoPlusTokenSafetyProvider)
    assert isinstance(collector.holder_provider, SolanaHolderInventoryProvider)
    assert isinstance(collector.mint_provider, SolanaMintAuthorityProvider)
    assert collector.max_age_ms == 90_000
    assert collector.holder_provider.max_accounts == 1234


def test_factory_does_not_construct_partial_collector_without_required_credentials():
    assert build_solana_risk_evidence_collector(SimpleNamespace(
        goplus_access_token="", solana_rpc_url="https://rpc.example",
    )) is None
    assert build_solana_risk_evidence_collector(SimpleNamespace(
        goplus_access_token="token", solana_rpc_url="",
    )) is None
