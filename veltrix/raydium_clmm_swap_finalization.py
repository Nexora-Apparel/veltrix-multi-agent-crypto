"""Pinned Raydium CLMM final-state update and account-byte serialization.

This module composes reward accrual, oracle observation update, swap settlement,
PoolState::update_after_swap and byte-exact account encoders. It produces local
post-state bytes only; it never writes accounts or submits a transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import struct

from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolAccountEvidence
from veltrix.raydium_clmm_fee_accounting import (
    RaydiumSwapSettlement,
    settle_raydium_swap_amounts,
)
from veltrix.raydium_clmm_observation import (
    RAYDIUM_OBSERVATION_COUNT,
    RAYDIUM_OBSERVATION_RECORD_LENGTH,
    RAYDIUM_OBSERVATION_STATE_LENGTH,
    RaydiumClmmObservationState,
    apply_raydium_observation_update,
    decode_raydium_clmm_observation_state,
)
from veltrix.raydium_clmm_pool_post_swap import apply_raydium_pool_after_swap
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_POOL_DYNAMIC_FEE_INFO_LENGTH,
    RAYDIUM_POOL_DYNAMIC_FEE_INFO_OFFSET,
    RAYDIUM_POOL_REWARD_COUNT,
    RAYDIUM_POOL_REWARD_INFO_LENGTH,
    RAYDIUM_POOL_REWARD_INFO_OFFSET,
    RAYDIUM_POOL_STATE_DISCRIMINATOR,
    RAYDIUM_POOL_STATE_LENGTH,
    RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET,
    RAYDIUM_POOL_TICK_ARRAY_BITMAP_WORDS,
    RaydiumClmmPoolState,
    decode_raydium_clmm_pool_state,
)
from veltrix.raydium_clmm_rewards import update_raydium_reward_infos
from veltrix.raydium_clmm_swap_loop_execution import RaydiumSwapLoopExecution
from veltrix.raydium_clmm_tick_array_mutation import (
    TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR,
    TICK_ARRAY_BITMAP_EXTENSION_LENGTH,
    RaydiumClmmTickArrayBitmapExtension,
    decode_raydium_tick_array_bitmap_extension,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_DATA_LENGTH,
    RAYDIUM_TICK_ARRAY_DISCRIMINATOR,
    RAYDIUM_TICK_ARRAY_INITIALIZED_COUNT_OFFSET,
    RAYDIUM_TICK_ARRAY_SIZE,
    RAYDIUM_TICK_ARRAY_TICKS_OFFSET,
    RAYDIUM_TICK_STATE_SIZE,
    RaydiumClmmTickArray,
    decode_raydium_clmm_tick_array,
)


class RaydiumSwapFinalizationError(ValueError):
    """Raised when source account evidence or final-state serialization is invalid."""


@dataclass(frozen=True, slots=True)
class RaydiumSerializedSwapAccount:
    address: str
    data: bytes
    data_sha256: str


@dataclass(frozen=True, slots=True)
class RaydiumSwapFinalizedState:
    pool: RaydiumClmmPoolState
    observation: RaydiumClmmObservationState
    tick_arrays: tuple[RaydiumClmmTickArray, ...]
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None
    settlement: RaydiumSwapSettlement
    pool_account: RaydiumSerializedSwapAccount
    observation_account: RaydiumSerializedSwapAccount
    tick_array_accounts: tuple[RaydiumSerializedSwapAccount, ...]
    bitmap_extension_account: RaydiumSerializedSwapAccount | None


def _uint(value: int, bits: int, field: str) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << bits):
        raise RaydiumSwapFinalizationError(f"{field} must fit u{bits}")
    return value.to_bytes(bits // 8, "little")


def _sint(value: int, bits: int, field: str) -> bytes:
    minimum, maximum = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RaydiumSwapFinalizationError(f"{field} must fit i{bits}")
    return value.to_bytes(bits // 8, "little", signed=True)


def _pubkey_bytes(value: str, field: str) -> bytes:
    try:
        return bytes(Pubkey.from_string(value))
    except Exception as exc:
        raise RaydiumSwapFinalizationError(f"{field} is not a valid Solana public key") from exc


def _account_evidence(address: str, label: str, raw: bytes) -> DexPoolAccountEvidence:
    return DexPoolAccountEvidence(
        amm_key=address,
        dex_label=label,
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        lamports=1,
        executable=False,
        data_length=len(raw),
        data_sha256=hashlib.sha256(raw).hexdigest(),
        data_bytes=raw,
    )


def _encode_pool_state(
    initial_pool: RaydiumClmmPoolState,
    final_pool: RaydiumClmmPoolState,
    raw: bytes,
) -> tuple[RaydiumClmmPoolState, RaydiumSerializedSwapAccount]:
    if not isinstance(raw, bytes) or len(raw) != RAYDIUM_POOL_STATE_LENGTH:
        raise RaydiumSwapFinalizationError("original PoolState must contain exactly 1544 bytes")
    if hashlib.sha256(raw).hexdigest() != initial_pool.account_data_sha256:
        raise RaydiumSwapFinalizationError("original PoolState bytes do not match the decoded source commitment")
    decoded = decode_raydium_clmm_pool_state(
        _account_evidence(initial_pool.pool_address, "Raydium CLMM", raw),
        expected_pool_address=initial_pool.pool_address,
    )
    if decoded != initial_pool:
        raise RaydiumSwapFinalizationError("original PoolState bytes differ from the supplied decoded PoolState")
    if final_pool.pool_address != initial_pool.pool_address:
        raise RaydiumSwapFinalizationError("final PoolState identity changed during swap replay")

    data = bytearray(raw)
    data[237:253] = _uint(final_pool.liquidity, 128, "PoolState liquidity")
    data[253:269] = _uint(final_pool.sqrt_price_x64, 128, "PoolState sqrt price")
    data[269:273] = _sint(final_pool.tick_current, 32, "PoolState current tick")
    data[277:293] = _uint(final_pool.fee_growth_global_0_x64, 128, "token-0 fee growth")
    data[293:309] = _uint(final_pool.fee_growth_global_1_x64, 128, "token-1 fee growth")
    data[309:317] = _uint(final_pool.protocol_fees_token_0, 64, "token-0 protocol fees")
    data[317:325] = _uint(final_pool.protocol_fees_token_1, 64, "token-1 protocol fees")
    if len(final_pool.tick_array_bitmap) != RAYDIUM_POOL_TICK_ARRAY_BITMAP_WORDS:
        raise RaydiumSwapFinalizationError("final PoolState bitmap must contain sixteen u64 words")
    for index, word in enumerate(final_pool.tick_array_bitmap):
        start = RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET + index * 8
        data[start:start + 8] = _uint(word, 64, f"PoolState bitmap word {index}")
    data[1064:1072] = _uint(final_pool.fund_fees_token_0, 64, "token-0 fund fees")
    data[1072:1080] = _uint(final_pool.fund_fees_token_1, 64, "token-1 fund fees")

    if len(final_pool.reward_infos) != RAYDIUM_POOL_REWARD_COUNT:
        raise RaydiumSwapFinalizationError("final PoolState must contain exactly three RewardInfo records")
    for index, reward in enumerate(final_pool.reward_infos):
        offset = RAYDIUM_POOL_REWARD_INFO_OFFSET + index * RAYDIUM_POOL_REWARD_INFO_LENGTH
        data[offset] = _uint(reward.reward_state, 8, f"reward {index} state")[0]
        data[offset + 1:offset + 9] = _uint(reward.open_time, 64, f"reward {index} open time")
        data[offset + 9:offset + 17] = _uint(reward.end_time, 64, f"reward {index} end time")
        data[offset + 17:offset + 25] = _uint(reward.last_update_time, 64, f"reward {index} update time")
        data[offset + 25:offset + 41] = _uint(reward.emissions_per_second_x64, 128, f"reward {index} emissions")
        data[offset + 41:offset + 49] = _uint(reward.reward_total_emitted, 64, f"reward {index} emitted total")
        data[offset + 49:offset + 57] = _uint(reward.reward_claimed, 64, f"reward {index} claimed total")
        data[offset + 57:offset + 89] = _pubkey_bytes(reward.token_mint, f"reward {index} mint")
        data[offset + 89:offset + 121] = _pubkey_bytes(reward.token_vault, f"reward {index} vault")
        data[offset + 121:offset + 153] = _pubkey_bytes(reward.authority, f"reward {index} authority")
        data[offset + 153:offset + 169] = _uint(reward.reward_growth_global_x64, 128, f"reward {index} global growth")

    dynamic = final_pool.dynamic_fee_info
    offset = RAYDIUM_POOL_DYNAMIC_FEE_INFO_OFFSET
    data[offset:offset + 2] = _uint(dynamic.filter_period, 16, "dynamic filter period")
    data[offset + 2:offset + 4] = _uint(dynamic.decay_period, 16, "dynamic decay period")
    data[offset + 4:offset + 6] = _uint(dynamic.reduction_factor, 16, "dynamic reduction factor")
    data[offset + 6:offset + 10] = _uint(dynamic.dynamic_fee_control, 32, "dynamic fee control")
    data[offset + 10:offset + 14] = _uint(dynamic.max_volatility_accumulator, 32, "maximum volatility accumulator")
    data[offset + 14:offset + 18] = _sint(dynamic.tick_spacing_index_reference, 32, "dynamic tick-spacing reference")
    data[offset + 18:offset + 22] = _uint(dynamic.volatility_reference, 32, "dynamic volatility reference")
    data[offset + 22:offset + 26] = _uint(dynamic.volatility_accumulator, 32, "dynamic volatility accumulator")
    data[offset + 26:offset + 34] = _uint(dynamic.last_update_timestamp, 64, "dynamic fee timestamp")
    if len(dynamic.padding) != RAYDIUM_POOL_DYNAMIC_FEE_INFO_LENGTH - 34:
        raise RaydiumSwapFinalizationError("dynamic-fee reserved padding has an invalid length")
    data[offset + 34:offset + 80] = dynamic.padding

    encoded = bytes(data)
    digest = hashlib.sha256(encoded).hexdigest()
    return replace(final_pool, account_data_sha256=digest), RaydiumSerializedSwapAccount(
        final_pool.pool_address, encoded, digest
    )


def _encode_tick_array(
    initial: RaydiumClmmTickArray,
    final: RaydiumClmmTickArray,
    raw: bytes,
) -> tuple[RaydiumClmmTickArray, RaydiumSerializedSwapAccount]:
    if not isinstance(raw, bytes) or len(raw) != RAYDIUM_TICK_ARRAY_DATA_LENGTH:
        raise RaydiumSwapFinalizationError("original TickArrayState must contain exactly 10240 bytes")
    decoded = decode_raydium_clmm_tick_array(
        address=initial.address,
        owner=RAYDIUM_CLMM_PROGRAM_ID,
        lamports=1,
        executable=False,
        data=raw,
        expected_pool_address=initial.pool_address,
        expected_start_tick_index=initial.start_tick_index,
        tick_spacing=initial.tick_spacing,
    )
    if decoded != initial:
        raise RaydiumSwapFinalizationError("original TickArray bytes differ from the supplied decoded state")
    if (
        final.address != initial.address
        or final.pool_address != initial.pool_address
        or final.start_tick_index != initial.start_tick_index
        or final.tick_spacing != initial.tick_spacing
        or len(final.ticks) != RAYDIUM_TICK_ARRAY_SIZE
    ):
        raise RaydiumSwapFinalizationError("final TickArray identity or layout changed")
    if final.initialized_tick_count != sum(1 for tick in final.ticks if tick.initialized):
        raise RaydiumSwapFinalizationError("final TickArray initialized count is inconsistent")

    data = bytearray(raw)
    for index, tick in enumerate(final.ticks):
        offset = RAYDIUM_TICK_ARRAY_TICKS_OFFSET + index * RAYDIUM_TICK_STATE_SIZE
        data[offset:offset + 4] = _sint(tick.tick_index, 32, f"tick {index} index")
        data[offset + 4:offset + 20] = _sint(tick.liquidity_net, 128, f"tick {index} liquidity net")
        data[offset + 20:offset + 36] = _uint(tick.liquidity_gross, 128, f"tick {index} liquidity gross")
        data[offset + 36:offset + 52] = _uint(tick.fee_growth_outside_0_x64, 128, f"tick {index} token-0 outside growth")
        data[offset + 52:offset + 68] = _uint(tick.fee_growth_outside_1_x64, 128, f"tick {index} token-1 outside growth")
        if len(tick.reward_growths_outside_x64) != 3:
            raise RaydiumSwapFinalizationError(f"tick {index} must contain three reward-growth values")
        for reward_index, value in enumerate(tick.reward_growths_outside_x64):
            start = offset + 68 + reward_index * 16
            data[start:start + 16] = _uint(value, 128, f"tick {index} reward {reward_index} outside growth")
        data[offset + 116:offset + 124] = _uint(tick.order_phase, 64, f"tick {index} order phase")
        data[offset + 124:offset + 132] = _uint(tick.orders_amount, 64, f"tick {index} orders amount")
        data[offset + 132:offset + 140] = _uint(tick.part_filled_orders_remaining, 64, f"tick {index} part-filled amount")
        data[offset + 140:offset + 156] = _uint(tick.unfilled_ratio_x64, 128, f"tick {index} unfilled ratio")
        if len(tick.padding) != 3:
            raise RaydiumSwapFinalizationError(f"tick {index} padding must contain three u32 values")
        for pad_index, value in enumerate(tick.padding):
            start = offset + 156 + pad_index * 4
            data[start:start + 4] = _uint(value, 32, f"tick {index} padding {pad_index}")
    data[RAYDIUM_TICK_ARRAY_INITIALIZED_COUNT_OFFSET] = _uint(
        final.initialized_tick_count, 8, "TickArray initialized count"
    )[0]
    data[10_125:10_133] = _uint(final.recent_epoch, 64, "TickArray recent epoch")
    encoded = bytes(data)
    digest = hashlib.sha256(encoded).hexdigest()
    return replace(final, data_sha256=digest), RaydiumSerializedSwapAccount(
        final.address, encoded, digest
    )


def _encode_bitmap_extension(
    initial: RaydiumClmmTickArrayBitmapExtension,
    final: RaydiumClmmTickArrayBitmapExtension,
    raw: bytes,
) -> tuple[RaydiumClmmTickArrayBitmapExtension, RaydiumSerializedSwapAccount]:
    decoded = decode_raydium_tick_array_bitmap_extension(
        address=initial.address,
        owner=RAYDIUM_CLMM_PROGRAM_ID,
        lamports=1,
        executable=False,
        data=raw,
        expected_pool_address=initial.pool_address,
    )
    if decoded != initial:
        raise RaydiumSwapFinalizationError("original bitmap-extension bytes differ from decoded source state")
    if final.address != initial.address or final.pool_address != initial.pool_address:
        raise RaydiumSwapFinalizationError("final bitmap-extension identity changed")
    if len(raw) != TICK_ARRAY_BITMAP_EXTENSION_LENGTH or raw[:8] != TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR:
        raise RaydiumSwapFinalizationError("bitmap-extension byte layout is invalid")
    data = bytearray(raw)
    offset = 40
    for rows in (final.positive_bitmaps, final.negative_bitmaps):
        if len(rows) != 14 or any(len(row) != 8 for row in rows):
            raise RaydiumSwapFinalizationError("bitmap extension must contain fourteen rows of eight words per side")
        for row in rows:
            for word in row:
                data[offset:offset + 8] = _uint(word, 64, "bitmap-extension word")
                offset += 8
    encoded = bytes(data)
    digest = hashlib.sha256(encoded).hexdigest()
    return replace(final, data_sha256=digest), RaydiumSerializedSwapAccount(
        final.address, encoded, digest
    )


def _encode_observation(
    initial: RaydiumClmmObservationState,
    final: RaydiumClmmObservationState,
    raw: bytes,
) -> tuple[RaydiumClmmObservationState, RaydiumSerializedSwapAccount]:
    if not isinstance(raw, bytes) or len(raw) != RAYDIUM_OBSERVATION_STATE_LENGTH:
        raise RaydiumSwapFinalizationError("original ObservationState has an invalid byte length")
    decoded = decode_raydium_clmm_observation_state(
        _account_evidence(initial.address, "Raydium CLMM Observation", raw),
        expected_address=initial.address,
        expected_pool_address=initial.pool_id,
    )
    if decoded != initial:
        raise RaydiumSwapFinalizationError("original ObservationState bytes differ from decoded source state")
    if final.address != initial.address or final.pool_id != initial.pool_id:
        raise RaydiumSwapFinalizationError("final ObservationState identity changed")
    if len(final.observations) != RAYDIUM_OBSERVATION_COUNT:
        raise RaydiumSwapFinalizationError("final ObservationState must retain the 100-entry ring")
    data = bytearray(raw)
    data[8] = _uint(int(final.initialized), 8, "ObservationState initialized flag")[0]
    data[17:19] = _uint(final.observation_index, 16, "ObservationState ring index")
    for index, observation in enumerate(final.observations):
        offset = 51 + index * RAYDIUM_OBSERVATION_RECORD_LENGTH
        data[offset:offset + 4] = _uint(observation.block_timestamp, 32, f"observation {index} timestamp")
        data[offset + 4:offset + 12] = _sint(observation.tick_cumulative, 64, f"observation {index} cumulative tick")
        if len(observation.padding) != 32 or any(observation.padding):
            raise RaydiumSwapFinalizationError(f"observation {index} reserved padding is invalid")
        data[offset + 12:offset + 44] = observation.padding
    if len(final.padding) != 32 or any(final.padding):
        raise RaydiumSwapFinalizationError("ObservationState account padding is invalid")
    data[-32:] = final.padding
    encoded = bytes(data)
    digest = hashlib.sha256(encoded).hexdigest()
    return replace(final, data_sha256=digest), RaydiumSerializedSwapAccount(
        final.address, encoded, digest
    )


def _fee_on_token0(fee_on: int, zero_for_one: bool) -> bool:
    if fee_on == 0:
        return zero_for_one
    if fee_on == 1:
        return True
    if fee_on == 2:
        return False
    raise RaydiumSwapFinalizationError("pool fee_on mode is invalid")


def finalize_raydium_swap_state(
    initial_pool: RaydiumClmmPoolState,
    pool_account_data: bytes,
    execution: RaydiumSwapLoopExecution,
    *,
    initial_observation: RaydiumClmmObservationState,
    observation_account_data: bytes,
    initial_tick_arrays: tuple[RaydiumClmmTickArray, ...],
    tick_array_account_data: tuple[bytes, ...],
    block_timestamp: int,
    initial_bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None = None,
    bitmap_extension_account_data: bytes | None = None,
) -> RaydiumSwapFinalizedState:
    """Finalize a complete replay and serialize every mutated Raydium account."""
    if not isinstance(initial_pool, RaydiumClmmPoolState):
        raise RaydiumSwapFinalizationError("decoded original PoolState is required")
    if not isinstance(execution, RaydiumSwapLoopExecution) or execution.complete is not True:
        raise RaydiumSwapFinalizationError("only a fully terminated swap-loop replay can be finalized")
    if isinstance(block_timestamp, bool) or not isinstance(block_timestamp, int) or not 0 <= block_timestamp <= (1 << 64) - 1:
        raise RaydiumSwapFinalizationError("block timestamp must fit u64")
    if len(initial_tick_arrays) != len(execution.tick_arrays) or len(tick_array_account_data) != len(initial_tick_arrays):
        raise RaydiumSwapFinalizationError("original, working and serialized TickArray sequences must align")
    if execution.state.pool.pool_address != initial_pool.pool_address:
        raise RaydiumSwapFinalizationError("swap replay belongs to a different PoolState")

    expected_rewards = update_raydium_reward_infos(
        initial_pool.reward_infos,
        curr_timestamp=block_timestamp,
        liquidity=initial_pool.liquidity,
    )
    if execution.updated_reward_infos != expected_rewards:
        raise RaydiumSwapFinalizationError(
            "swap-loop tick crossing used reward growths different from the pinned pre-swap reward update"
        )

    fee_on_token0 = _fee_on_token0(initial_pool.fee_on, execution.state.zero_for_one)
    settlement = settle_raydium_swap_amounts(
        execution.state.fee_state,
        amount_specified=execution.state.amount_specified,
        zero_for_one=execution.state.zero_for_one,
        is_base_input=execution.state.is_base_input,
        fee_on_token0=fee_on_token0,
    )
    dynamic_info = (
        execution.state.dynamic_fee_state.info
        if execution.state.dynamic_fee_state is not None else None
    )
    final_pool = apply_raydium_pool_after_swap(
        execution.state.pool,
        tick_current=execution.state.tick,
        sqrt_price_x64=execution.state.sqrt_price_x64,
        liquidity=execution.state.fee_state.liquidity,
        lp_fee=execution.state.fee_state.lp_fee,
        protocol_fee=execution.state.fee_state.protocol_fee,
        fund_fee=execution.state.fee_state.fund_fee,
        fee_growth_global_x64=execution.state.fee_state.fee_growth_global_x64,
        zero_for_one=execution.state.zero_for_one,
        dynamic_fee_info=dynamic_info,
    )
    final_pool = replace(final_pool, reward_infos=expected_rewards, account_data_sha256="")

    final_observation = initial_observation
    if execution.state.tick != initial_pool.tick_current:
        if block_timestamp > 0xFFFFFFFF:
            raise RaydiumSwapFinalizationError("ObservationState timestamp exceeds its pinned u32 field")
        final_observation = apply_raydium_observation_update(
            initial_observation,
            block_timestamp=block_timestamp,
            tick=initial_pool.tick_current,
        )

    serialized_pool, pool_account = _encode_pool_state(
        initial_pool, final_pool, pool_account_data
    )
    serialized_observation, observation_account = _encode_observation(
        initial_observation, final_observation, observation_account_data
    )

    serialized_arrays = []
    array_accounts = []
    for original, final, raw in zip(
        initial_tick_arrays, execution.tick_arrays, tick_array_account_data
    ):
        encoded_array, account = _encode_tick_array(original, final, raw)
        serialized_arrays.append(encoded_array)
        array_accounts.append(account)

    final_extension = execution.bitmap_extension
    extension_account = None
    if final_extension is not None:
        if initial_bitmap_extension is None or bitmap_extension_account_data is None:
            raise RaydiumSwapFinalizationError(
                "bitmap-extension post-state requires its original decoded state and account bytes"
            )
        final_extension, extension_account = _encode_bitmap_extension(
            initial_bitmap_extension, final_extension, bitmap_extension_account_data
        )
    elif initial_bitmap_extension is not None or bitmap_extension_account_data is not None:
        raise RaydiumSwapFinalizationError("original bitmap extension was omitted from final swap working state")

    return RaydiumSwapFinalizedState(
        pool=serialized_pool,
        observation=serialized_observation,
        tick_arrays=tuple(serialized_arrays),
        bitmap_extension=final_extension,
        settlement=settlement,
        pool_account=pool_account,
        observation_account=observation_account,
        tick_array_accounts=tuple(array_accounts),
        bitmap_extension_account=extension_account,
    )
