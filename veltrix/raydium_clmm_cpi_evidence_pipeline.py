"""Ordered, fail-closed assembly of live Raydium CLMM CPI account evidence.

This module connects existing source-pinned decoders and validators. It does not
request a quote, construct a transaction, sign, submit, or execute a swap.
The caller must already hold a validated unsigned Jupiter order, its successful
read-only simulation evidence, and the independently decoded PoolState/vault
evidence bound to that exact message and simulation response.
"""
from __future__ import annotations

from dataclasses import dataclass

from veltrix.dex_pool_evidence import DexPoolEvidenceSnapshot
from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.raydium_clmm_aux_accounts import (
    RaydiumClmmAuxAccountSnapshot,
    fetch_raydium_clmm_aux_account_snapshot,
)
from veltrix.raydium_clmm_bitmap_traversal import (
    RaydiumClmmBitmapTraversalResult,
    verify_raydium_clmm_bitmap_traversal,
)
from veltrix.raydium_clmm_cpi import (
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RaydiumClmmCpiResult,
)
from veltrix.raydium_clmm_cpi_tick_arrays import (
    fetch_raydium_clmm_cpi_tick_array_snapshot,
)
from veltrix.raydium_clmm_pool_binding import (
    RaydiumClmmPoolBindingResult,
    evaluate_raydium_clmm_cpi_pool_binding,
)
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolRouteCheck
from veltrix.raydium_clmm_simulation_state import (
    RaydiumClmmAccountCommitment,
    build_raydium_clmm_account_commitments,
)
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTickArraySnapshot
from veltrix.raydium_clmm_vaults import RaydiumClmmVaultSnapshot
from veltrix.simulation_gate import SimulationEvidence
from veltrix.solana_rpc_simulation import SolanaRpcSimulationAdapter


class RaydiumCpiEvidencePipelineError(ValueError):
    """Raised when ordered live CPI evidence cannot be assembled safely."""


@dataclass(frozen=True, slots=True)
class RaydiumCpiEvidencePipelineResult:
    message_sha256: str
    source_commit: str
    provider_id: str
    cluster: str
    genesis_hash: str
    simulation_context_slot: int
    tick_array_context_slot: int
    auxiliary_context_slot: int
    tick_array_snapshot: RaydiumClmmTickArraySnapshot
    auxiliary_snapshot: RaydiumClmmAuxAccountSnapshot
    bitmap_traversal: RaydiumClmmBitmapTraversalResult
    pool_binding: RaydiumClmmPoolBindingResult
    account_commitments: tuple[RaydiumClmmAccountCommitment, ...]
    execution_ready: bool = False


def assemble_raydium_clmm_cpi_evidence(
    order: JupiterOrderTransaction,
    *,
    cpi: RaydiumClmmCpiResult,
    pool_check: RaydiumClmmPoolRouteCheck,
    pool_evidence: DexPoolEvidenceSnapshot,
    vault_snapshot: RaydiumClmmVaultSnapshot,
    simulation: SimulationEvidence,
    rpc_adapter: SolanaRpcSimulationAdapter,
) -> RaydiumCpiEvidencePipelineResult:
    """Fetch and validate CPI trailing accounts, bitmap traversal and role commitments.

    Fetch order is deliberate: CPI-ordered tick arrays first, then AMM config and
    ObservationState at or after the tick-array snapshot slot. All evidence must
    remain bound to one exact unsigned message, simulation response, provider and
    mainnet genesis. No account state is inferred or filled in when absent.
    """
    if not isinstance(order, JupiterOrderTransaction):
        raise RaydiumCpiEvidencePipelineError("validated unsigned Jupiter order is required")
    if not isinstance(cpi, RaydiumClmmCpiResult) or cpi.passed is not True:
        raise RaydiumCpiEvidencePipelineError("successful Raydium CLMM CPI evidence is required")
    if cpi.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT:
        raise RaydiumCpiEvidencePipelineError("Raydium CPI source revision is not pinned")
    if not isinstance(pool_check, RaydiumClmmPoolRouteCheck) or pool_check.passed is not True:
        raise RaydiumCpiEvidencePipelineError("successful decoded PoolState route check is required")
    if not isinstance(pool_evidence, DexPoolEvidenceSnapshot):
        raise RaydiumCpiEvidencePipelineError("owner-verified PoolState account evidence is required")
    if not isinstance(vault_snapshot, RaydiumClmmVaultSnapshot):
        raise RaydiumCpiEvidencePipelineError("verified pool vault snapshot is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumCpiEvidencePipelineError("successful read-only simulation evidence is required")
    if not isinstance(rpc_adapter, SolanaRpcSimulationAdapter):
        raise RaydiumCpiEvidencePipelineError("provider-pinned read-only Solana RPC adapter is required")
    if simulation.simulation_error is not None:
        raise RaydiumCpiEvidencePipelineError("simulation contains an execution error")
    if (
        simulation.cluster != "mainnet-beta"
        or simulation.genesis_hash != rpc_adapter.expected_genesis_hash
        or simulation.provider_id != rpc_adapter.provider_id
        or simulation.cluster != rpc_adapter.cluster
    ):
        raise RaydiumCpiEvidencePipelineError("simulation and RPC adapter cluster/provider provenance differs")

    tick_arrays = fetch_raydium_clmm_cpi_tick_array_snapshot(
        order,
        cpi=cpi,
        pool_evidence=pool_evidence,
        pool_check=pool_check,
        vault_snapshot=vault_snapshot,
        simulation=simulation,
        rpc_adapter=rpc_adapter,
    )
    auxiliary = fetch_raydium_clmm_aux_account_snapshot(
        pool_evidence,
        pool_checks=(pool_check,),
        vault_snapshot=vault_snapshot,
        tick_array_snapshot=tick_arrays,
        simulation=simulation,
        rpc_adapter=rpc_adapter,
    )
    traversal = verify_raydium_clmm_bitmap_traversal(
        order,
        pool_check=pool_check,
        pool_evidence=pool_evidence,
        tick_array_snapshot=tick_arrays,
    )
    binding = evaluate_raydium_clmm_cpi_pool_binding(
        order,
        cpi=cpi,
        pool_check=pool_check,
        pool_evidence=pool_evidence,
        vault_snapshot=vault_snapshot,
        tick_array_snapshot=tick_arrays,
        aux_snapshot=auxiliary,
        simulation=simulation,
    )
    commitments = build_raydium_clmm_account_commitments(
        cpi,
        pool_evidence=pool_evidence,
        vault_snapshot=vault_snapshot,
        tick_array_snapshot=tick_arrays,
        aux_snapshot=auxiliary,
    )

    digest = simulation.message_sha256
    if (
        binding.passed is not True
        or traversal.passed is not True
        or not commitments
        or any(item.address == "" for item in commitments)
        or tick_arrays.route_message_sha256 != digest
        or auxiliary.route_message_sha256 != digest
    ):
        raise RaydiumCpiEvidencePipelineError("assembled CPI evidence failed its final message/role consistency gate")

    return RaydiumCpiEvidencePipelineResult(
        message_sha256=digest,
        source_commit=RAYDIUM_CLMM_SOURCE_COMMIT,
        provider_id=simulation.provider_id,
        cluster=simulation.cluster,
        genesis_hash=simulation.genesis_hash,
        simulation_context_slot=simulation.context_slot,
        tick_array_context_slot=tick_arrays.context_slot,
        auxiliary_context_slot=auxiliary.context_slot,
        tick_array_snapshot=tick_arrays,
        auxiliary_snapshot=auxiliary,
        bitmap_traversal=traversal,
        pool_binding=binding,
        account_commitments=commitments,
        execution_ready=False,
    )
