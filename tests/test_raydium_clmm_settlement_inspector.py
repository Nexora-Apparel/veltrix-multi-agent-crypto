from types import SimpleNamespace

import pytest

from veltrix.raydium_clmm_settlement_inspector import (
    RaydiumDevnetInspectorError,
    build_devnet_readonly_rpc_adapter_from_env,
    inspect_finalized_raydium_clmm_devnet_settlement,
)


def configured_env(**overrides):
    values = {
        "VELTRIX_CLMM_DEVNET_RPC_URL": "https://rpc.devnet.example/v1/private-token",
        "VELTRIX_CLMM_DEVNET_RPC_PROVIDER_ID": "operator-devnet-rpc",
        "VELTRIX_CLMM_DEVNET_GENESIS_HASH": "pinned-devnet-genesis",
        "VELTRIX_CLMM_DEVNET_RPC_ALLOWED_HOSTS": "rpc.devnet.example",
    }
    values.update(overrides)
    return values


def test_builds_only_explicitly_allowlisted_devnet_rpc_adapter():
    adapter = build_devnet_readonly_rpc_adapter_from_env(configured_env())
    assert adapter.cluster == "devnet"
    assert adapter.provider_id == "operator-devnet-rpc"
    assert adapter.expected_genesis_hash == "pinned-devnet-genesis"


def test_requires_all_explicit_devnet_rpc_settings():
    with pytest.raises(RaydiumDevnetInspectorError, match="missing explicit Devnet RPC settings"):
        build_devnet_readonly_rpc_adapter_from_env({})


def test_rejects_endpoint_host_not_in_operator_allowlist():
    with pytest.raises(RaydiumDevnetInspectorError, match="not explicitly allowlisted"):
        build_devnet_readonly_rpc_adapter_from_env(
            configured_env(VELTRIX_CLMM_DEVNET_RPC_URL="https://unlisted.example/rpc")
        )


def test_rejects_mainnet_adapter_before_any_transaction_inspection():
    adapter = build_devnet_readonly_rpc_adapter_from_env(configured_env())
    mainnet_adapter = type(adapter)(
        endpoint="https://rpc.devnet.example/v1/private-token",
        provider_id="operator-devnet-rpc",
        cluster="mainnet-beta",
        expected_genesis_hash="pinned-devnet-genesis",
        allowed_hosts=("rpc.devnet.example",),
    )
    with pytest.raises(RaydiumDevnetInspectorError, match="Devnet evidence only"):
        inspect_finalized_raydium_clmm_devnet_settlement(
            "signature",
            pool_evidence=SimpleNamespace(cluster="devnet"),
            pool_checks=(),
            simulation=SimpleNamespace(cluster="mainnet-beta"),
            rpc_adapter=mainnet_adapter,
            expected_deltas=(),
        )
