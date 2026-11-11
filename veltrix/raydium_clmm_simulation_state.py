"""Decode Raydium CLMM pre/post simulation state without authorizing execution.

This stage binds provider-returned simulated account bytes to the already
verified PoolState and CPI tick-array evidence, then enforces structural
invariants that a swap must not mutate. It is not yet an independent swap
math replay.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from veltrix.dex_pool_evidence import DexPoolAccountEvidence, DexPoolEvidenceSnapshot
from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RaydiumClmmPoolRouteCheck,
    RaydiumClmmPoolState,
    decode_raydium_clmm_pool_state,
)
from veltrix.raydium_clmm_tick_arrays import (
    RaydiumClmmTickArray,
    RaydiumClmmTickArraySnapshot,
    decode_raydium_clmm_tick_array,
    raydium_current_tick_array_start,
)
from veltrix.simulation_gate import SimulatedAccountState, SimulationEvidence


class RaydiumClmmSimulationStateError(ValueError):
    """Raised when simulated Raydium pre/post account state is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmSimulationStateTransition:
    passed: bool
    message_sha256: str
    pool_address: str
    pre_pool: RaydiumClmmPoolState
    post_pool: RaydiumClmmPoolState
    pre_tick_arrays: tuple[RaydiumClmmTickArray, ...]
    post_tick_arrays: tuple[RaydiumClmmTickArray, ...]
    changed_tick_array_addresses: tuple[str, ...]
    changed_order_tick_indexes: tuple[tuple[str, int], ...]
    pre_pool_sha256: str
    post_pool_sha256: str
    execution_ready: bool = False


def _pool_account(
    *,
    address: str,
    owner: str,
    lamports: int,
    executable: bool,
    data: bytes,
    digest: str,
) -> DexPoolAccountEvidence:
    return DexPoolAccountEvidence(
        amm_key=address,
        dex_label="Raydium CLMM",
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=owner,
        lamports=lamports,
        executable=executable,
        data_length=len(data),
        data_sha256=digest,
        data_bytes=data,
    )


def _decode_pool_state_pair(
    state: SimulatedAccountState,
    *,
    address: str,
) -> tuple[RaydiumClmmPoolState, RaydiumClmmPoolState]:
    if (
        state.pre_owner != RAYDIUM_CLMM_PROGRAM_ID
        or state.post_owner != RAYDIUM_CLMM_PROGRAM_ID
        or state.pre_executable is not False
        or state.post_executable is not False
        or state.pre_lamports <= 0
        or state.post_lamports <= 0
    ):
        raise RaydiumClmmSimulationStateError("simulated PoolState owner, executable flag or lamports are invalid")
    pre = decode_raydium_clmm_pool_state(
        _pool_account(
            address=address, owner=state.pre_owner, lamports=state.pre_lamports,
            executable=state.pre_executable, data=state.pre_data,
            digest=state.pre_data_sha256,
        ),
        expected_pool_address=address,
    )
    post = decode_raydium_clmm_pool_state(
        _pool_account(
            address=address, owner=state.post_owner, lamports=state.post_lamports,
            executable=state.post_executable, data=state.post_data,
            digest=state.post_data_sha256,
        ),
        expected_pool_address=address,
    )
    immutable_fields = (
        "pool_address", "bump", "amm_config", "pool_creator",
        "token_mint_0", "token_mint_1", "token_vault_0", "token_vault_1",
        "observation_key", "mint_decimals_0", "mint_decimals_1",
        "tick_spacing", "status", "fee_on", "seed_index", "tick_array_bitmap",
    )
    changed = tuple(name for name in immutable_fields if getattr(pre, name) != getattr(post, name))
    if changed:
        raise RaydiumClmmSimulationStateError(
            "swap simulation changed immutable PoolState identity/configuration fields: "
            + ",".join(changed)
        )
    if pre.account_data_sha256 == post.account_data_sha256:
        raise RaydiumClmmSimulationStateError("successful Raydium swap simulation produced no PoolState mutation")
    return pre, post


def _decode_tick_array_pair(
    state: SimulatedAccountState,
    *,
    address: str,
    pool_address: str,
    tick_spacing: int,
    pre_current_tick: int,
    post_current_tick: int,
) -> tuple[RaydiumClmmTickArray, RaydiumClmmTickArray]:
    if (
        state.pre_owner != RAYDIUM_CLMM_PROGRAM_ID
        or state.post_owner != RAYDIUM_CLMM_PROGRAM_ID
        or state.pre_executable is not False
        or state.post_executable is not False
        or state.pre_lamports <= 0
        or state.post_lamports <= 0
    ):
        raise RaydiumClmmSimulationStateError("simulated TickArrayState owner, executable flag or lamports are invalid")
    pre_start = int.from_bytes(state.pre_data[40:44], "little", signed=True) if len(state.pre_data) >= 44 else None
    post_start = int.from_bytes(state.post_data[40:44], "little", signed=True) if len(state.post_data) >= 44 else None
    if pre_start is None or post_start is None or pre_start != post_start:
        raise RaydiumClmmSimulationStateError("simulated TickArrayState start index changed or is truncated")
    pre_current = pre_current_tick if raydium_current_tick_array_start(pre_current_tick, tick_spacing) == pre_start else None
    post_current = post_current_tick if raydium_current_tick_array_start(post_current_tick, tick_spacing) == post_start else None
    pre = decode_raydium_clmm_tick_array(
        address=address, owner=state.pre_owner, lamports=state.pre_lamports,
        executable=state.pre_executable, data=state.pre_data,
        expected_pool_address=pool_address, expected_start_tick_index=pre_start,
        tick_spacing=tick_spacing, current_tick=pre_current,
    )
    post = decode_raydium_clmm_tick_array(
        address=address, owner=state.post_owner, lamports=state.post_lamports,
        executable=state.post_executable, data=state.post_data,
        expected_pool_address=pool_address, expected_start_tick_index=post_start,
        tick_spacing=tick_spacing, current_tick=post_current,
    )
    if len(pre.ticks) != len(post.ticks):
        raise RaydiumClmmSimulationStateError("simulated TickArrayState tick record count changed")
    for before, after in zip(pre.ticks, post.ticks):
        if (
            before.tick_index != after.tick_index
            or before.liquidity_net != after.liquidity_net
            or before.liquidity_gross != after.liquidity_gross
        ):
            raise RaydiumClmmSimulationStateError(
                "swap simulation changed tick identity or position liquidity at tick "
                + str(before.tick_index)
            )
    return pre, post


def verify_raydium_clmm_simulation_state_transition(
    order: JupiterOrderTransaction,
    *,
    pool_check: RaydiumClmmPoolRouteCheck,
    pool_evidence: DexPoolEvidenceSnapshot,
    tick_array_snapshot: RaydiumClmmTickArraySnapshot,
    simulation: SimulationEvidence,
) -> RaydiumClmmSimulationStateTransition:
    """Bind exact simulated PoolState/TickArrayState pre/post bytes to verified evidence."""
    if not isinstance(order, JupiterOrderTransaction):
        raise RaydiumClmmSimulationStateError("Jupiter order is required")
    if not isinstance(pool_check, RaydiumClmmPoolRouteCheck) or pool_check.passed is not True:
        raise RaydiumClmmSimulationStateError("successful Raydium PoolState route check is required")
    if not isinstance(pool_evidence, DexPoolEvidenceSnapshot):
        raise RaydiumClmmSimulationStateError("owner-verified PoolState account evidence is required")
    if not isinstance(tick_array_snapshot, RaydiumClmmTickArraySnapshot):
        raise RaydiumClmmSimulationStateError("decoded CPI tick-array snapshot is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumClmmSimulationStateError("simulation evidence is required")

    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if (
        pool_check.route_input_mint != order.input_mint
        or pool_check.route_output_mint != order.output_mint
        or simulation.message_sha256 != digest
        or pool_evidence.route_message_sha256 != digest
        or tick_array_snapshot.route_message_sha256 != digest
        or simulation.simulation_error is not None
        or pool_evidence.simulation_response_id != simulation.response_id
        or tick_array_snapshot.simulation_response_id != simulation.response_id
        or pool_evidence.simulation_context_slot != simulation.context_slot
        or tick_array_snapshot.simulation_context_slot != simulation.context_slot
        or pool_evidence.provider_id != simulation.provider_id
        or tick_array_snapshot.provider_id != simulation.provider_id
        or pool_evidence.cluster != simulation.cluster
        or tick_array_snapshot.cluster != simulation.cluster
        or pool_evidence.genesis_hash != simulation.genesis_hash
        or tick_array_snapshot.genesis_hash != simulation.genesis_hash
        or pool_evidence.context_slot > simulation.context_slot
        or tick_array_snapshot.context_slot < simulation.context_slot
    ):
        raise RaydiumClmmSimulationStateError("simulation, pool and tick-array evidence provenance is inconsistent")

    pool_address = pool_check.pool.pool_address
    account_states = {item.address: item for item in simulation.account_states}
    if len(account_states) != len(simulation.account_states):
        raise RaydiumClmmSimulationStateError("simulation contains duplicate tracked account states")
    required = {pool_address, *(item.address for item in tick_array_snapshot.tick_arrays)}
    if tick_array_snapshot.bitmap_extension_address is not None:
        required.add(tick_array_snapshot.bitmap_extension_address)
    missing = required - set(account_states)
    if missing:
        raise RaydiumClmmSimulationStateError(
            "simulation omitted required PoolState or CPI tick-array pre/post accounts: "
            + ",".join(sorted(missing))
        )

    expected_tick_order = tuple(
        address for address in tick_array_snapshot.cpi_account_order
        if address != tick_array_snapshot.bitmap_extension_address
    )
    observed_tick_order = tuple(item.address for item in tick_array_snapshot.tick_arrays)
    if (
        not tick_array_snapshot.cpi_account_order
        or expected_tick_order != observed_tick_order
        or len(set(tick_array_snapshot.cpi_account_order)) != len(tick_array_snapshot.cpi_account_order)
        or (tick_array_snapshot.bitmap_extension_address is not None
            and tick_array_snapshot.bitmap_extension_address not in tick_array_snapshot.cpi_account_order)
    ):
        raise RaydiumClmmSimulationStateError("tick-array snapshot does not preserve the exact CPI remaining-account order")

    pool_accounts = [item for item in pool_evidence.accounts if item.amm_key == pool_address]
    if len(pool_accounts) != 1:
        raise RaydiumClmmSimulationStateError("owner-verified pool snapshot must contain exactly one PoolState")
    pool_account = pool_accounts[0]
    if (
        pool_account.owner_program_id != RAYDIUM_CLMM_PROGRAM_ID
        or pool_account.expected_program_id != RAYDIUM_CLMM_PROGRAM_ID
        or pool_account.executable is not False
        or pool_account.lamports <= 0
        or hashlib.sha256(pool_account.data_bytes).hexdigest() != pool_account.data_sha256
    ):
        raise RaydiumClmmSimulationStateError("owner-verified PoolState account metadata or hash is invalid")
    pool_state = account_states[pool_address]
    if (
        pool_state.pre_data_sha256 != pool_account.data_sha256
        or pool_state.pre_data != pool_account.data_bytes
    ):
        raise RaydiumClmmSimulationStateError("simulation PoolState pre-state differs from owner-verified PoolState bytes")
    pre_pool, post_pool = _decode_pool_state_pair(pool_state, address=pool_address)
    if pre_pool != pool_check.pool:
        raise RaydiumClmmSimulationStateError("simulation PoolState pre-state differs from the decoded route PoolState")

    pre_arrays = []
    post_arrays = []
    changed_arrays = []
    changed_order_ticks = []
    snapshot_by_address = {item.address: item for item in tick_array_snapshot.tick_arrays}
    for address, verified in snapshot_by_address.items():
        state = account_states[address]
        if state.pre_data_sha256 != verified.data_sha256:
            raise RaydiumClmmSimulationStateError("simulation tick-array pre-state hash differs from verified CPI tick-array bytes")
        before, after = _decode_tick_array_pair(
            state,
            address=address,
            pool_address=pool_address,
            tick_spacing=pre_pool.tick_spacing,
            pre_current_tick=pre_pool.tick_current,
            post_current_tick=post_pool.tick_current,
        )
        if before.data_sha256 != verified.data_sha256:
            raise RaydiumClmmSimulationStateError("decoded simulation tick-array pre-state differs from the verified snapshot")
        pre_arrays.append(before)
        post_arrays.append(after)
        if state.pre_data_sha256 != state.post_data_sha256:
            changed_arrays.append(address)
        for old_tick, new_tick in zip(before.ticks, after.ticks):
            if (
                old_tick.orders_amount != new_tick.orders_amount
                or old_tick.part_filled_orders_remaining != new_tick.part_filled_orders_remaining
            ):
                changed_order_ticks.append((address, old_tick.tick_index))

    if tick_array_snapshot.bitmap_extension_address is not None:
        extension_state = account_states[tick_array_snapshot.bitmap_extension_address]
        if (
            extension_state.pre_data_sha256 != tick_array_snapshot.bitmap_extension_sha256
            or extension_state.post_data_sha256 != extension_state.pre_data_sha256
            or extension_state.pre_owner != RAYDIUM_CLMM_PROGRAM_ID
            or extension_state.post_owner != RAYDIUM_CLMM_PROGRAM_ID
            or extension_state.pre_executable is not False
            or extension_state.post_executable is not False
            or extension_state.pre_lamports <= 0
            or extension_state.post_lamports <= 0
        ):
            raise RaydiumClmmSimulationStateError("simulation changed or failed to bind the immutable tick-array bitmap extension")

    return RaydiumClmmSimulationStateTransition(
        passed=True,
        message_sha256=digest,
        pool_address=pool_address,
        pre_pool=pre_pool,
        post_pool=post_pool,
        pre_tick_arrays=tuple(pre_arrays),
        post_tick_arrays=tuple(post_arrays),
        changed_tick_array_addresses=tuple(changed_arrays),
        changed_order_tick_indexes=tuple(changed_order_ticks),
        pre_pool_sha256=pool_state.pre_data_sha256,
        post_pool_sha256=pool_state.post_data_sha256,
        execution_ready=False,
    )
