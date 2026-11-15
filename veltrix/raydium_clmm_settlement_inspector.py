"""Read-only Devnet entry point for finalized Raydium CLMM settlement inspection.

This module accepts an already-finalized transaction signature and previously
captured, typed pool/simulation evidence. It has no signing, wallet, sendTransaction,
or broadcast path. All results remain execution_ready=False.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from veltrix.dex_pool_evidence import DexPoolEvidenceSnapshot
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolRouteCheck
from veltrix.raydium_clmm_vaults import (
    ExpectedRaydiumVaultDelta,
    RaydiumFinalizedVaultSettlement,
    RaydiumClmmVaultError,
    verify_finalized_raydium_clmm_vault_settlement_from_rpc,
)
from veltrix.simulation_gate import SimulationEvidence
from veltrix.solana_rpc_simulation import SolanaRpcSimulationAdapter


class RaydiumDevnetInspectorError(ValueError):
    """Raised when read-only Devnet inspection is not fully configured."""


def build_devnet_readonly_rpc_adapter_from_env(
    environ: dict[str, str] | None = None,
) -> SolanaRpcSimulationAdapter:
    """Build a provider-pinned Devnet adapter from explicit, non-default settings."""
    values = os.environ if environ is None else environ
    required = (
        "VELTRIX_CLMM_DEVNET_RPC_URL",
        "VELTRIX_CLMM_DEVNET_RPC_PROVIDER_ID",
        "VELTRIX_CLMM_DEVNET_GENESIS_HASH",
        "VELTRIX_CLMM_DEVNET_RPC_ALLOWED_HOSTS",
    )
    missing = [key for key in required if not isinstance(values.get(key), str) or not values[key].strip()]
    if missing:
        raise RaydiumDevnetInspectorError(
            "missing explicit Devnet RPC settings: " + ", ".join(missing)
        )

    endpoint = values["VELTRIX_CLMM_DEVNET_RPC_URL"].strip()
    provider_id = values["VELTRIX_CLMM_DEVNET_RPC_PROVIDER_ID"].strip()
    genesis_hash = values["VELTRIX_CLMM_DEVNET_GENESIS_HASH"].strip()
    allowed_hosts = tuple(
        host.strip().lower()
        for host in values["VELTRIX_CLMM_DEVNET_RPC_ALLOWED_HOSTS"].split(",")
        if host.strip()
    )
    if not allowed_hosts or len(allowed_hosts) != len(set(allowed_hosts)):
        raise RaydiumDevnetInspectorError("Devnet RPC allowed-host list must be non-empty and unique")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in allowed_hosts:
        raise RaydiumDevnetInspectorError("Devnet RPC endpoint host is not explicitly allowlisted")

    try:
        return SolanaRpcSimulationAdapter(
            endpoint=endpoint,
            provider_id=provider_id,
            cluster="devnet",
            expected_genesis_hash=genesis_hash,
            allowed_hosts=allowed_hosts,
        )
    except Exception as exc:
        raise RaydiumDevnetInspectorError("could not construct the pinned read-only Devnet RPC adapter") from exc


def inspect_finalized_raydium_clmm_devnet_settlement(
    signature: str,
    *,
    pool_evidence: DexPoolEvidenceSnapshot,
    pool_checks: tuple[RaydiumClmmPoolRouteCheck, ...],
    simulation: SimulationEvidence,
    rpc_adapter: SolanaRpcSimulationAdapter,
    expected_deltas: tuple[ExpectedRaydiumVaultDelta, ...],
) -> RaydiumFinalizedVaultSettlement:
    """Inspect an existing Devnet signature; never signs, submits, or broadcasts."""
    if not isinstance(rpc_adapter, SolanaRpcSimulationAdapter):
        raise RaydiumDevnetInspectorError("provider-pinned read-only RPC adapter is required")
    if rpc_adapter.cluster != "devnet" or simulation.cluster != "devnet":
        raise RaydiumDevnetInspectorError("Phase 09.74 inspector accepts Devnet evidence only")
    if pool_evidence.cluster != "devnet":
        raise RaydiumDevnetInspectorError("pool evidence is not from Solana Devnet")

    try:
        result = verify_finalized_raydium_clmm_vault_settlement_from_rpc(
            signature,
            pool_evidence=pool_evidence,
            pool_checks=pool_checks,
            simulation=simulation,
            rpc_adapter=rpc_adapter,
            expected_deltas=expected_deltas,
        )
    except Exception:
        # Do not echo RPC endpoints, credential-bearing URLs, or raw provider errors.
        raise
    if result.execution_ready is not False:
        raise RaydiumDevnetInspectorError("settlement inspection must never authorize execution")
    return result


def inspect_finalized_raydium_clmm_devnet_settlement_from_env(
    signature: str,
    *,
    pool_evidence: DexPoolEvidenceSnapshot,
    pool_checks: tuple[RaydiumClmmPoolRouteCheck, ...],
    simulation: SimulationEvidence,
    expected_deltas: tuple[ExpectedRaydiumVaultDelta, ...],
    environ: dict[str, str] | None = None,
) -> RaydiumFinalizedVaultSettlement:
    """Environment-configured read-only convenience entry point."""
    adapter = build_devnet_readonly_rpc_adapter_from_env(environ)
    return inspect_finalized_raydium_clmm_devnet_settlement(
        signature,
        pool_evidence=pool_evidence,
        pool_checks=pool_checks,
        simulation=simulation,
        rpc_adapter=adapter,
        expected_deltas=expected_deltas,
    )
