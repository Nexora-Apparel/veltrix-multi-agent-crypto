"""Fetch and bind every Raydium CLMM tick-array account referenced by a simulated CPI.

This stage preserves CPI account order, decodes all referenced TickArrayState
accounts, and validates the optional bitmap-extension account. It is evidence
only and does not authorize signing or execution.
"""
from __future__ import annotations

import hashlib

from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolEvidenceSnapshot
from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.raydium_clmm_cpi import RaydiumClmmCpiResult
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RaydiumClmmPoolRouteCheck,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_DATA_LENGTH,
    RaydiumClmmTickArraySnapshot,
    RaydiumClmmTickArrayBitmapExtension,
    decode_raydium_clmm_tick_array,
)
from veltrix.raydium_clmm_vaults import RaydiumClmmVaultSnapshot
from veltrix.simulation_gate import SimulationEvidence
from veltrix.solana_rpc_simulation import SolanaRpcSimulationAdapter


TICK_ARRAY_BITMAP_EXTENSION_SEED = b"pool_tick_array_bitmap_extension"
TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR = hashlib.sha256(
    b"account:TickArrayBitmapExtension"
).digest()[:8]
TICK_ARRAY_BITMAP_EXTENSION_DATA_LENGTH = 8 + 32 + (64 * 14 * 2)


class RaydiumClmmCpiTickArrayError(ValueError):
    """Raised when CPI-referenced tick-array traversal evidence is incomplete."""


def derive_raydium_clmm_tick_array_bitmap_extension_address(*, pool_address: str) -> str:
    try:
        pool = Pubkey.from_string(pool_address)
        program = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
        address, _ = Pubkey.find_program_address(
            [TICK_ARRAY_BITMAP_EXTENSION_SEED, bytes(pool)],
            program,
        )
        return str(address)
    except Exception as exc:
        raise RaydiumClmmCpiTickArrayError("tick-array bitmap extension PDA derivation failed") from exc


def validate_raydium_clmm_tick_array_bitmap_extension(
    *,
    address: str,
    owner: str,
    lamports: int,
    executable: bool,
    data: bytes,
    expected_pool_address: str,
) -> str:
    if owner != RAYDIUM_CLMM_PROGRAM_ID:
        raise RaydiumClmmCpiTickArrayError("bitmap extension owner is not the pinned Raydium CLMM program")
    if executable is not False or isinstance(lamports, bool) or not isinstance(lamports, int) or lamports <= 0:
        raise RaydiumClmmCpiTickArrayError("bitmap extension must be non-executable and have positive lamports")
    if not isinstance(data, bytes) or len(data) != TICK_ARRAY_BITMAP_EXTENSION_DATA_LENGTH:
        raise RaydiumClmmCpiTickArrayError("bitmap extension length differs from the pinned 1832-byte layout")
    if data[:8] != TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR:
        raise RaydiumClmmCpiTickArrayError("bitmap extension Anchor discriminator is invalid")
    try:
        observed_address = str(Pubkey.from_string(address))
        expected_pool = str(Pubkey.from_string(expected_pool_address))
        observed_pool = str(Pubkey.from_bytes(data[8:40]))
    except Exception as exc:
        raise RaydiumClmmCpiTickArrayError("bitmap extension address or pool identity is invalid") from exc
    if observed_pool != expected_pool:
        raise RaydiumClmmCpiTickArrayError("bitmap extension belongs to a different pool")
    if observed_address != derive_raydium_clmm_tick_array_bitmap_extension_address(pool_address=expected_pool):
        raise RaydiumClmmCpiTickArrayError("bitmap extension address does not match the canonical pool PDA")
    return observed_address


def decode_raydium_clmm_tick_array_bitmap_extension(
    *,
    address: str,
    owner: str,
    lamports: int,
    executable: bool,
    data: bytes,
    expected_pool_address: str,
) -> RaydiumClmmTickArrayBitmapExtension:
    """Decode the pinned positive/negative 14x512-bit extension bitmap."""
    validated_address = validate_raydium_clmm_tick_array_bitmap_extension(
        address=address,
        owner=owner,
        lamports=lamports,
        executable=executable,
        data=data,
        expected_pool_address=expected_pool_address,
    )
    positive_start = 40
    words_per_side = 14 * 8
    positive = tuple(
        int.from_bytes(data[positive_start + index * 8:positive_start + (index + 1) * 8], "little")
        for index in range(words_per_side)
    )
    negative_start = positive_start + words_per_side * 8
    negative = tuple(
        int.from_bytes(data[negative_start + index * 8:negative_start + (index + 1) * 8], "little")
        for index in range(words_per_side)
    )
    return RaydiumClmmTickArrayBitmapExtension(
        address=validated_address,
        pool_address=str(Pubkey.from_string(expected_pool_address)),
        positive_bitmaps=positive,
        negative_bitmaps=negative,
        data_sha256=hashlib.sha256(data).hexdigest(),
        data_bytes=data,
    )


def fetch_raydium_clmm_cpi_tick_array_snapshot(
    order: JupiterOrderTransaction,
    *,
    cpi: RaydiumClmmCpiResult,
    pool_evidence: DexPoolEvidenceSnapshot,
    pool_check: RaydiumClmmPoolRouteCheck,
    vault_snapshot: RaydiumClmmVaultSnapshot,
    simulation: SimulationEvidence,
    rpc_adapter: SolanaRpcSimulationAdapter,
) -> RaydiumClmmTickArraySnapshot:
    """Fetch every trailing CPI account and decode tick arrays in exact CPI order."""
    if not isinstance(order, JupiterOrderTransaction):
        raise RaydiumClmmCpiTickArrayError("Jupiter order is required")
    if not isinstance(cpi, RaydiumClmmCpiResult) or cpi.passed is not True:
        raise RaydiumClmmCpiTickArrayError("successful Raydium CLMM CPI evidence is required")
    if cpi.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT:
        raise RaydiumClmmCpiTickArrayError("Raydium CPI source revision is not pinned")
    if not isinstance(pool_evidence, DexPoolEvidenceSnapshot):
        raise RaydiumClmmCpiTickArrayError("provenance-bound pool evidence is required")
    if not isinstance(pool_check, RaydiumClmmPoolRouteCheck) or pool_check.passed is not True:
        raise RaydiumClmmCpiTickArrayError("successful Raydium PoolState route check is required")
    if not isinstance(vault_snapshot, RaydiumClmmVaultSnapshot):
        raise RaydiumClmmCpiTickArrayError("verified Raydium vault snapshot is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumClmmCpiTickArrayError("simulation evidence is required")
    if not isinstance(rpc_adapter, SolanaRpcSimulationAdapter):
        raise RaydiumClmmCpiTickArrayError("provider-pinned Solana RPC adapter is required")

    pool = pool_check.pool
    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if (
        cpi.message_sha256 != digest
        or pool_evidence.route_message_sha256 != digest
        or vault_snapshot.route_message_sha256 != digest
        or simulation.message_sha256 != digest
    ):
        raise RaydiumClmmCpiTickArrayError("CPI, pool, vault and simulation message digests differ")
    if cpi.pool_state != pool.pool_address:
        raise RaydiumClmmCpiTickArrayError("CPI pool account differs from the decoded PoolState")
    if (
        order.input_mint not in {pool.token_mint_0, pool.token_mint_1}
        or order.output_mint not in {pool.token_mint_0, pool.token_mint_1}
        or order.input_mint == order.output_mint
        or {order.input_mint, order.output_mint} != {pool.token_mint_0, pool.token_mint_1}
    ):
        raise RaydiumClmmCpiTickArrayError("Jupiter mint direction does not match the decoded pool pair")
    if not cpi.tick_array_accounts or len(cpi.tick_array_accounts) > 23:
        raise RaydiumClmmCpiTickArrayError("CPI must reference between one and 23 trailing accounts")
    if len(set(cpi.tick_array_accounts)) != len(cpi.tick_array_accounts):
        raise RaydiumClmmCpiTickArrayError("CPI trailing account list contains duplicate keys")
    if (
        pool_evidence.simulation_response_id != simulation.response_id
        or vault_snapshot.simulation_response_id != simulation.response_id
        or pool_evidence.simulation_context_slot != simulation.context_slot
        or vault_snapshot.simulation_context_slot != simulation.context_slot
        or pool_evidence.provider_id != simulation.provider_id
        or vault_snapshot.provider_id != simulation.provider_id
        or pool_evidence.cluster != simulation.cluster
        or vault_snapshot.cluster != simulation.cluster
        or pool_evidence.genesis_hash != simulation.genesis_hash
        or vault_snapshot.genesis_hash != simulation.genesis_hash
        or rpc_adapter.provider_id != simulation.provider_id
        or rpc_adapter.cluster != simulation.cluster
        or rpc_adapter.expected_genesis_hash != simulation.genesis_hash
    ):
        raise RaydiumClmmCpiTickArrayError("CPI tick-array evidence provider, cluster or simulation identity differs")

    minimum_slot = max(
        simulation.context_slot,
        pool_evidence.context_slot,
        vault_snapshot.context_slot,
    )
    snapshot = rpc_adapter.fetch_account_snapshot(
        tuple(cpi.tick_array_accounts),
        min_context_slot=minimum_slot,
    )
    if (
        snapshot.provider_id != simulation.provider_id
        or snapshot.cluster != simulation.cluster
        or snapshot.genesis_hash != simulation.genesis_hash
        or snapshot.context_slot < minimum_slot
        or len(snapshot.accounts) != len(cpi.tick_array_accounts)
    ):
        raise RaydiumClmmCpiTickArrayError("CPI tick-array RPC snapshot provenance, slot or account count is invalid")
    if tuple(account.address for account in snapshot.accounts) != tuple(cpi.tick_array_accounts):
        raise RaydiumClmmCpiTickArrayError("RPC account snapshot does not preserve the exact CPI trailing-account order")

    arrays = []
    bitmap_extension_address = None
    bitmap_extension_sha256 = None
    bitmap_extension = None
    seen = set()
    for account in snapshot.accounts:
        if account.address in seen:
            raise RaydiumClmmCpiTickArrayError("CPI tick-array RPC snapshot contains duplicate accounts")
        seen.add(account.address)
        if len(account.data) == RAYDIUM_TICK_ARRAY_DATA_LENGTH:
            arrays.append(decode_raydium_clmm_tick_array(
                address=account.address,
                owner=account.owner,
                lamports=account.lamports,
                executable=account.executable,
                data=account.data,
                expected_pool_address=pool.pool_address,
                tick_spacing=pool.tick_spacing,
            ))
        elif len(account.data) == TICK_ARRAY_BITMAP_EXTENSION_DATA_LENGTH:
            if bitmap_extension_address is not None:
                raise RaydiumClmmCpiTickArrayError("CPI contains more than one bitmap extension account")
            bitmap_extension = decode_raydium_clmm_tick_array_bitmap_extension(
                address=account.address,
                owner=account.owner,
                lamports=account.lamports,
                executable=account.executable,
                data=account.data,
                expected_pool_address=pool.pool_address,
            )
            bitmap_extension_address = bitmap_extension.address
            bitmap_extension_sha256 = bitmap_extension.data_sha256
        else:
            raise RaydiumClmmCpiTickArrayError("CPI trailing account is neither a pinned TickArrayState nor bitmap extension")

    if len(arrays) == 0:
        raise RaydiumClmmCpiTickArrayError("CPI tick-array account list contains no decoded TickArrayState")
    starts = tuple(item.start_tick_index for item in arrays)
    zero_for_one = order.input_mint == pool.token_mint_0
    if zero_for_one and any(right >= left for left, right in zip(starts, starts[1:])):
        raise RaydiumClmmCpiTickArrayError("CPI tick arrays are not strictly ordered in zero-for-one traversal direction")
    if not zero_for_one and any(right <= left for left, right in zip(starts, starts[1:])):
        raise RaydiumClmmCpiTickArrayError("CPI tick arrays are not strictly ordered in one-for-zero traversal direction")

    return RaydiumClmmTickArraySnapshot(
        route_message_sha256=digest,
        simulation_response_id=simulation.response_id,
        simulation_context_slot=simulation.context_slot,
        provider_id=snapshot.provider_id,
        cluster=snapshot.cluster,
        genesis_hash=snapshot.genesis_hash,
        context_slot=snapshot.context_slot,
        tick_arrays=tuple(arrays),
        cpi_account_order=tuple(cpi.tick_array_accounts),
        bitmap_extension_address=bitmap_extension_address,
        bitmap_extension_sha256=bitmap_extension_sha256,
        bitmap_extension=bitmap_extension,
        execution_ready=False,
    )
