"""Read-only Raydium CLMM current TickArrayState validation."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct

from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolEvidenceSnapshot
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RaydiumClmmPoolRouteCheck,
)
from veltrix.raydium_clmm_vaults import RaydiumClmmVaultSnapshot
from veltrix.simulation_gate import SimulationEvidence
from veltrix.solana_rpc_simulation import SolanaRpcSimulationAdapter


RAYDIUM_TICK_ARRAY_SEED = b"tick_array"
RAYDIUM_TICK_ARRAY_SIZE = 60
RAYDIUM_TICK_STATE_SIZE = 168
RAYDIUM_TICK_ARRAY_DATA_LENGTH = 10_240
RAYDIUM_TICK_ARRAY_DISCRIMINATOR = hashlib.sha256(b"account:TickArrayState").digest()[:8]
RAYDIUM_TICK_ARRAY_TICKS_OFFSET = 44
RAYDIUM_TICK_ARRAY_INITIALIZED_COUNT_OFFSET = 10_124


class RaydiumClmmTickArrayError(ValueError):
    """Raised when the current Raydium CLMM tick array is missing or inconsistent."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmTick:
    tick_index: int
    liquidity_net: int
    liquidity_gross: int
    orders_amount: int
    part_filled_orders_remaining: int
    initialized: bool
    fee_growth_outside_0_x64: int = 0
    fee_growth_outside_1_x64: int = 0
    reward_growths_outside_x64: tuple[int, int, int] = (0, 0, 0)
    order_phase: int = 0
    unfilled_ratio_x64: int = 0
    padding: tuple[int, int, int] = (0, 0, 0)


@dataclass(frozen=True, slots=True)
class RaydiumClmmTickArray:
    address: str
    pool_address: str
    start_tick_index: int
    tick_spacing: int
    initialized_tick_count: int
    recent_epoch: int
    ticks: tuple[RaydiumClmmTick, ...]
    data_sha256: str


@dataclass(frozen=True, slots=True)
class RaydiumClmmTickArrayBitmapExtension:
    address: str
    pool_address: str
    positive_bitmaps: tuple[int, ...]
    negative_bitmaps: tuple[int, ...]
    data_sha256: str
    data_bytes: bytes = b""


@dataclass(frozen=True, slots=True)
class RaydiumClmmTickArraySnapshot:
    route_message_sha256: str
    simulation_response_id: str
    simulation_context_slot: int
    provider_id: str
    cluster: str
    genesis_hash: str
    context_slot: int
    tick_arrays: tuple[RaydiumClmmTickArray, ...]
    cpi_account_order: tuple[str, ...] = ()
    bitmap_extension_address: str | None = None
    bitmap_extension_sha256: str | None = None
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None = None
    execution_ready: bool = False


def raydium_current_tick_array_start(tick_current: int, tick_spacing: int) -> int:
    if isinstance(tick_current, bool) or not isinstance(tick_current, int):
        raise RaydiumClmmTickArrayError("current tick must be an integer")
    if isinstance(tick_spacing, bool) or not isinstance(tick_spacing, int) or tick_spacing <= 0:
        raise RaydiumClmmTickArrayError("tick spacing must be a positive integer")
    ticks_per_array = RAYDIUM_TICK_ARRAY_SIZE * tick_spacing
    # Python // is floor division, matching the upstream Rust truncation plus
    # negative-remainder correction in TickArrayState::get_array_start_index.
    return (tick_current // ticks_per_array) * ticks_per_array


def derive_raydium_clmm_tick_array_address(*, pool_address: str, start_tick_index: int) -> str:
    if isinstance(start_tick_index, bool) or not isinstance(start_tick_index, int) or not -(2**31) <= start_tick_index < 2**31:
        raise RaydiumClmmTickArrayError("tick-array start index must fit i32")
    try:
        pool = Pubkey.from_string(pool_address)
        program = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
        address, _ = Pubkey.find_program_address(
            [RAYDIUM_TICK_ARRAY_SEED, bytes(pool), start_tick_index.to_bytes(4, "big", signed=True)],
            program,
        )
        return str(address)
    except Exception as exc:
        raise RaydiumClmmTickArrayError("Raydium tick-array PDA derivation failed") from exc


def decode_raydium_clmm_tick_array(
    *,
    address: str,
    owner: str,
    lamports: int,
    executable: bool,
    data: bytes,
    expected_pool_address: str,
    expected_start_tick_index: int | None = None,
    tick_spacing: int,
    current_tick: int | None = None,
) -> RaydiumClmmTickArray:
    if owner != RAYDIUM_CLMM_PROGRAM_ID:
        raise RaydiumClmmTickArrayError("tick-array account owner is not the pinned Raydium CLMM program")
    if executable is not False:
        raise RaydiumClmmTickArrayError("tick-array account must not be executable")
    if isinstance(lamports, bool) or not isinstance(lamports, int) or lamports <= 0:
        raise RaydiumClmmTickArrayError("tick-array account must have positive lamports")
    if not isinstance(data, bytes) or len(data) != RAYDIUM_TICK_ARRAY_DATA_LENGTH:
        raise RaydiumClmmTickArrayError("Raydium TickArrayState must have the exact 10240-byte layout")
    if data[:8] != RAYDIUM_TICK_ARRAY_DISCRIMINATOR:
        raise RaydiumClmmTickArrayError("Raydium TickArrayState discriminator is invalid")
    try:
        expected_pool = str(Pubkey.from_string(expected_pool_address))
        observed_pool = str(Pubkey.from_bytes(data[8:40]))
        observed_address = str(Pubkey.from_string(address))
    except Exception as exc:
        raise RaydiumClmmTickArrayError("tick-array address or pool ID is invalid") from exc
    if observed_pool != expected_pool:
        raise RaydiumClmmTickArrayError("tick array belongs to a different pool")
    if isinstance(tick_spacing, bool) or not isinstance(tick_spacing, int) or tick_spacing <= 0:
        raise RaydiumClmmTickArrayError("tick spacing must be a positive integer")
    start = struct.unpack_from("<i", data, 40)[0]
    if start % (RAYDIUM_TICK_ARRAY_SIZE * tick_spacing) != 0:
        raise RaydiumClmmTickArrayError("tick-array start index is not aligned to pool tick spacing")
    if expected_start_tick_index is not None and start != expected_start_tick_index:
        raise RaydiumClmmTickArrayError("tick-array start index differs from the expected array")
    if current_tick is not None:
        if start != raydium_current_tick_array_start(current_tick, tick_spacing):
            raise RaydiumClmmTickArrayError("tick-array start index is not derived from the current pool tick")
        if current_tick < start or current_tick >= start + RAYDIUM_TICK_ARRAY_SIZE * tick_spacing:
            raise RaydiumClmmTickArrayError("current pool tick is outside the decoded tick array")
    derived_address = derive_raydium_clmm_tick_array_address(
        pool_address=expected_pool,
        start_tick_index=start,
    )
    if observed_address != derived_address:
        raise RaydiumClmmTickArrayError("tick-array address does not match the canonical Raydium PDA")

    ticks = []
    initialized_count = 0
    for index in range(RAYDIUM_TICK_ARRAY_SIZE):
        offset = RAYDIUM_TICK_ARRAY_TICKS_OFFSET + index * RAYDIUM_TICK_STATE_SIZE
        tick_index = struct.unpack_from("<i", data, offset)[0]
        liquidity_net = int.from_bytes(data[offset + 4:offset + 20], "little", signed=True)
        liquidity_gross = int.from_bytes(data[offset + 20:offset + 36], "little")
        fee_growth_outside_0 = int.from_bytes(data[offset + 36:offset + 52], "little")
        fee_growth_outside_1 = int.from_bytes(data[offset + 52:offset + 68], "little")
        reward_growths = tuple(
            int.from_bytes(data[offset + 68 + reward_index * 16:offset + 84 + reward_index * 16], "little")
            for reward_index in range(3)
        )
        order_phase = int.from_bytes(data[offset + 116:offset + 124], "little")
        orders_amount = int.from_bytes(data[offset + 124:offset + 132], "little")
        part_filled = int.from_bytes(data[offset + 132:offset + 140], "little")
        unfilled_ratio = int.from_bytes(data[offset + 140:offset + 156], "little")
        padding = tuple(
            int.from_bytes(data[offset + 156 + pad_index * 4:offset + 160 + pad_index * 4], "little")
            for pad_index in range(3)
        )
        if unfilled_ratio > (1 << 64):
            raise RaydiumClmmTickArrayError("tick limit-order unfilled ratio exceeds Q64.64 unity")
        if any(padding):
            raise RaydiumClmmTickArrayError("tick state contains unsupported non-zero reserved padding")
        initialized = liquidity_gross > 0 or orders_amount > 0 or part_filled > 0
        if initialized:
            expected_tick = start + index * tick_spacing
            if tick_index != expected_tick:
                raise RaydiumClmmTickArrayError("initialized tick index does not match its array offset and spacing")
            if liquidity_gross > 0 and abs(liquidity_net) > liquidity_gross:
                raise RaydiumClmmTickArrayError("initialized tick net liquidity exceeds gross liquidity")
            initialized_count += 1
        ticks.append(RaydiumClmmTick(
            tick_index=tick_index,
            liquidity_net=liquidity_net,
            liquidity_gross=liquidity_gross,
            orders_amount=orders_amount,
            part_filled_orders_remaining=part_filled,
            initialized=initialized,
            fee_growth_outside_0_x64=fee_growth_outside_0,
            fee_growth_outside_1_x64=fee_growth_outside_1,
            reward_growths_outside_x64=reward_growths,
            order_phase=order_phase,
            unfilled_ratio_x64=unfilled_ratio,
            padding=padding,
        ))

    recorded_count = data[RAYDIUM_TICK_ARRAY_INITIALIZED_COUNT_OFFSET]
    if recorded_count != initialized_count:
        raise RaydiumClmmTickArrayError("initialized tick count differs from the decoded tick records")
    recent_epoch = int.from_bytes(data[10_125:10_133], "little")
    return RaydiumClmmTickArray(
        address=observed_address,
        pool_address=expected_pool,
        start_tick_index=start,
        tick_spacing=tick_spacing,
        initialized_tick_count=recorded_count,
        recent_epoch=recent_epoch,
        ticks=tuple(ticks),
        data_sha256=hashlib.sha256(data).hexdigest(),
    )


def fetch_raydium_clmm_current_tick_arrays(
    pool_evidence: DexPoolEvidenceSnapshot,
    *,
    pool_checks: tuple[RaydiumClmmPoolRouteCheck, ...],
    vault_snapshot: RaydiumClmmVaultSnapshot,
    simulation: SimulationEvidence,
    rpc_adapter: SolanaRpcSimulationAdapter,
) -> RaydiumClmmTickArraySnapshot:
    """Fetch the current tick array for each verified pool at or after all prior evidence slots."""
    if not isinstance(pool_evidence, DexPoolEvidenceSnapshot):
        raise RaydiumClmmTickArrayError("provenance-bound DEX pool evidence is required")
    if not isinstance(vault_snapshot, RaydiumClmmVaultSnapshot):
        raise RaydiumClmmTickArrayError("verified Raydium vault snapshot is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumClmmTickArrayError("Solana simulation evidence is required")
    if not isinstance(rpc_adapter, SolanaRpcSimulationAdapter):
        raise RaydiumClmmTickArrayError("provider-pinned Solana RPC adapter is required")
    if not isinstance(pool_checks, tuple) or not pool_checks or any(
        not isinstance(item, RaydiumClmmPoolRouteCheck) or item.passed is not True
        for item in pool_checks
    ):
        raise RaydiumClmmTickArrayError("successful Raydium pool-state checks are required")
    if (
        pool_evidence.route_message_sha256 != simulation.message_sha256
        or vault_snapshot.route_message_sha256 != simulation.message_sha256
        or pool_evidence.simulation_response_id != simulation.response_id
        or vault_snapshot.simulation_response_id != simulation.response_id
        or pool_evidence.simulation_context_slot != simulation.context_slot
        or vault_snapshot.simulation_context_slot != simulation.context_slot
    ):
        raise RaydiumClmmTickArrayError("pool, vault and simulation evidence do not share the same order/response/slot")
    for evidence in (pool_evidence, vault_snapshot):
        if (
            evidence.provider_id != simulation.provider_id
            or evidence.cluster != simulation.cluster
            or evidence.genesis_hash != simulation.genesis_hash
        ):
            raise RaydiumClmmTickArrayError("pool, vault and simulation evidence must share provider and cluster")
    if (
        rpc_adapter.provider_id != simulation.provider_id
        or rpc_adapter.cluster != simulation.cluster
        or rpc_adapter.expected_genesis_hash != simulation.genesis_hash
    ):
        raise RaydiumClmmTickArrayError("tick-array RPC provider or cluster differs from simulation")

    expected: dict[str, tuple[RaydiumClmmPoolRouteCheck, int]] = {}
    for check in pool_checks:
        pool = check.pool
        start = raydium_current_tick_array_start(pool.tick_current, pool.tick_spacing)
        address = derive_raydium_clmm_tick_array_address(
            pool_address=pool.pool_address,
            start_tick_index=start,
        )
        prior = expected.get(address)
        if prior is not None and prior[0].pool.pool_address != pool.pool_address:
            raise RaydiumClmmTickArrayError("tick-array address is assigned to conflicting pools")
        expected[address] = (check, start)

    minimum_slot = max(pool_evidence.context_slot, vault_snapshot.context_slot, simulation.context_slot)
    snapshot = rpc_adapter.fetch_account_snapshot(tuple(sorted(expected)), min_context_slot=minimum_slot)
    if (
        snapshot.provider_id != simulation.provider_id
        or snapshot.cluster != simulation.cluster
        or snapshot.genesis_hash != simulation.genesis_hash
        or snapshot.context_slot < minimum_slot
    ):
        raise RaydiumClmmTickArrayError("tick-array snapshot provider, cluster or context slot is inconsistent")
    if len(snapshot.accounts) != len(expected):
        raise RaydiumClmmTickArrayError("tick-array snapshot count differs from the expected current pool arrays")

    arrays = []
    seen: set[str] = set()
    for account in snapshot.accounts:
        if account.address in seen:
            raise RaydiumClmmTickArrayError("tick-array snapshot contains duplicate accounts")
        seen.add(account.address)
        item = expected.get(account.address)
        if item is None:
            raise RaydiumClmmTickArrayError("tick-array snapshot returned an unexpected account")
        check, start = item
        arrays.append(decode_raydium_clmm_tick_array(
            address=account.address,
            owner=account.owner,
            lamports=account.lamports,
            executable=account.executable,
            data=account.data,
            expected_pool_address=check.pool.pool_address,
            expected_start_tick_index=start,
            tick_spacing=check.pool.tick_spacing,
            current_tick=check.pool.tick_current,
        ))
    if seen != set(expected):
        raise RaydiumClmmTickArrayError("tick-array snapshot omitted a required current pool array")

    return RaydiumClmmTickArraySnapshot(
        route_message_sha256=simulation.message_sha256,
        simulation_response_id=simulation.response_id,
        simulation_context_slot=simulation.context_slot,
        provider_id=snapshot.provider_id,
        cluster=snapshot.cluster,
        genesis_hash=snapshot.genesis_hash,
        context_slot=snapshot.context_slot,
        tick_arrays=tuple(arrays),
        execution_ready=False,
    )
