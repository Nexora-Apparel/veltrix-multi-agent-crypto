"""Final Raydium CLMM swap-state persistence and account-byte serialization.

All account encoders preserve the original verified account layout and patch only
fields changed by the pinned swap path. No transaction is constructed or submitted.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import struct

from solders.pubkey import Pubkey

from veltrix.raydium_clmm_fee_accounting import (
    RaydiumSwapSettlement,
    settle_raydium_swap_amounts,
)
from veltrix.raydium_clmm_observation import (
    RAYDIUM_OBSERVATION_COUNT,
    RAYDIUM_OBSERVATION_RECORD_LENGTH,
    RAYDIUM_OBSERVATION_STATE_DISCRIMINATOR,
    RAYDIUM_OBSERVATION_STATE_LENGTH,
    RaydiumClmmObservationState,
    apply_raydium_observation_update,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RAYDIUM_DYNAMIC_FEE_CONTROL_DENOMINATOR,
    RAYDIUM_DYNAMIC_FEE_REDUCTION_DENOMINATOR,
    RAYDIUM_POOL_DYNAMIC_FEE_INFO_LENGTH,
    RAYDIUM_POOL_DYNAMIC_FEE_INFO_OFFSET,
    RAYDIUM_POOL_REWARD_COUNT,
    RAYDIUM_POOL_REWARD_INFO_LENGTH,
    RAYDIUM_POOL_REWARD_INFO_OFFSET,
    RAYDIUM_POOL_STATE_DISCRIMINATOR,
    RAYDIUM_POOL_STATE_LENGTH,
    RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET,
    RAYDIUM_POOL_TICK_ARRAY_BITMAP_WORDS,
    RaydiumClmmDynamicFeeInfo,
    RaydiumClmmPoolState,
    RaydiumClmmRewardInfo,
)
from veltrix.raydium_clmm_swap_loop_engine import RaydiumSwapLoopResult
from veltrix.raydium_clmm_tick_array_mutation import (
    TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR,
    TICK_ARRAY_BITMAP_EXTENSION_LENGTH,
    RaydiumClmmTickArrayBitmapExtension,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_DATA_LENGTH,
    RAYDIUM_TICK_ARRAY_DISCRIMINATOR,
    RAYDIUM_TICK_ARRAY_INITIALIZED_COUNT_OFFSET,
    RAYDIUM_TICK_ARRAY_SIZE,
    RAYDIUM_TICK_ARRAY_TICKS_OFFSET,
    RAYDIUM_TICK_STATE_SIZE,
    RaydiumClmmTickArray,
)


U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1
I128_MIN = -(1 << 127)
I128_MAX = (1 << 127) - 1


class RaydiumSwapFinalizationError(ValueError):
    """Raised when final account state cannot be serialized without losing provenance."""


@dataclass(frozen=True, slots=True)
class RaydiumSwapFinalizationResult:
    pool: RaydiumClmmPoolState
    pool_data: bytes
    tick_arrays: tuple[RaydiumClmmTickArray, ...]
    tick_array_data: tuple[bytes, ...]
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None
    bitmap_extension_data: bytes | None
    observation: RaydiumClmmObservationState
    observation_data: bytes
    settlement: RaydiumSwapSettlement
    zero_for_one: bool
    source_pool_sha256: str
    source_tick_array_sha256: tuple[str, ...]
    source_bitmap_extension_sha256: str | None
    source_observation_sha256: str


def _uint(value: int, bits: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << bits):
        raise RaydiumSwapFinalizationError(f"{field} must fit u{bits}")


def _sint(value: int, bits: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not -(1 << (bits - 1)) <= value < (1 << (bits - 1)):
        raise RaydiumSwapFinalizationError(f"{field} must fit i{bits}")


def _put_uint(data: bytearray, offset: int, length: int, value: int, field: str) -> None:
    _uint(value, length * 8, field)
    data[offset:offset + length] = value.to_bytes(length, "little")


def _put_sint(data: bytearray, offset: int, length: int, value: int, field: str) -> None:
    _sint(value, length * 8, field)
    data[offset:offset + length] = value.to_bytes(length, "little", signed=True)


def _pubkey_bytes(value: str, field: str) -> bytes:
    try:
        return bytes(Pubkey.from_string(value))
    except Exception as exc:
        raise RaydiumSwapFinalizationError(f"{field} is not a valid public key") from exc


def _verify_source_bytes(raw: bytes, expected_hash: str, length: int, field: str) -> bytearray:
    if not isinstance(raw, bytes) or len(raw) != length:
        raise RaydiumSwapFinalizationError(f"{field} raw account bytes are unavailable or have the wrong length")
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or hashlib.sha256(raw).hexdigest() != expected_hash
    ):
        raise RaydiumSwapFinalizationError(f"{field} raw bytes do not match the original verified account commitment")
    return bytearray(raw)


def _encode_reward(data: bytearray, offset: int, reward: RaydiumClmmRewardInfo) -> None:
    if not isinstance(reward, RaydiumClmmRewardInfo):
        raise RaydiumSwapFinalizationError("PoolState reward slot is not decoded")
    _uint(reward.reward_state, 8, "reward_state")
    if reward.reward_state not in (0, 1, 2, 3):
        raise RaydiumSwapFinalizationError("reward_state enum is invalid")
    for name, value in (
        ("open_time", reward.open_time),
        ("end_time", reward.end_time),
        ("last_update_time", reward.last_update_time),
        ("reward_total_emitted", reward.reward_total_emitted),
        ("reward_claimed", reward.reward_claimed),
    ):
        _uint(value, 64, name)
    _uint(reward.emissions_per_second_x64, 128, "emissions_per_second_x64")
    _uint(reward.reward_growth_global_x64, 128, "reward_growth_global_x64")
    if reward.reward_claimed > reward.reward_total_emitted:
        raise RaydiumSwapFinalizationError("reward claimed amount exceeds total emitted")
    data[offset] = reward.reward_state
    _put_uint(data, offset + 1, 8, reward.open_time, "reward open_time")
    _put_uint(data, offset + 9, 8, reward.end_time, "reward end_time")
    _put_uint(data, offset + 17, 8, reward.last_update_time, "reward last_update_time")
    _put_uint(data, offset + 25, 16, reward.emissions_per_second_x64, "reward emissions")
    _put_uint(data, offset + 41, 8, reward.reward_total_emitted, "reward total emitted")
    _put_uint(data, offset + 49, 8, reward.reward_claimed, "reward claimed")
    data[offset + 57:offset + 89] = _pubkey_bytes(reward.token_mint, "reward token mint")
    data[offset + 89:offset + 121] = _pubkey_bytes(reward.token_vault, "reward token vault")
    data[offset + 121:offset + 153] = _pubkey_bytes(reward.authority, "reward authority")
    _put_uint(data, offset + 153, 16, reward.reward_growth_global_x64, "reward global growth")


def _encode_dynamic_fee(data: bytearray, info: RaydiumClmmDynamicFeeInfo) -> None:
    if not isinstance(info, RaydiumClmmDynamicFeeInfo) or len(info.padding) != 46:
        raise RaydiumSwapFinalizationError("dynamic-fee state or its reserved padding is invalid")
    _put_uint(data, 0, 2, info.filter_period, "filter_period")
    _put_uint(data, 2, 2, info.decay_period, "decay_period")
    _put_uint(data, 4, 2, info.reduction_factor, "reduction_factor")
    _put_uint(data, 6, 4, info.dynamic_fee_control, "dynamic_fee_control")
    _put_uint(data, 10, 4, info.max_volatility_accumulator, "max_volatility_accumulator")
    _put_sint(data, 14, 4, info.tick_spacing_index_reference, "tick_spacing_index_reference")
    _put_uint(data, 18, 4, info.volatility_reference, "volatility_reference")
    _put_uint(data, 22, 4, info.volatility_accumulator, "volatility_accumulator")
    _put_uint(data, 26, 8, info.last_update_timestamp, "last_update_timestamp")
    data[34:80] = info.padding
    if not info.enabled:
        values = (
            info.filter_period, info.decay_period, info.reduction_factor,
            info.dynamic_fee_control, info.max_volatility_accumulator,
            info.tick_spacing_index_reference, info.volatility_reference,
            info.volatility_accumulator, info.last_update_timestamp,
        )
        if any(values) or any(info.padding):
            raise RaydiumSwapFinalizationError("disabled dynamic-fee state contains non-zero fields")
    elif (
        info.filter_period == 0
        or info.decay_period <= info.filter_period
        or not 0 < info.reduction_factor < RAYDIUM_DYNAMIC_FEE_REDUCTION_DENOMINATOR
        or info.dynamic_fee_control >= RAYDIUM_DYNAMIC_FEE_CONTROL_DENOMINATOR
        or any(info.padding)
    ):
        raise RaydiumSwapFinalizationError("dynamic-fee state violates pinned protocol constraints")


def _serialize_pool(
    pool: RaydiumClmmPoolState,
    *,
    source_sha256: str,
) -> tuple[RaydiumClmmPoolState, bytes]:
    data = _verify_source_bytes(pool.raw_data, source_sha256, RAYDIUM_POOL_STATE_LENGTH, "PoolState")
    if data[:8] != RAYDIUM_POOL_STATE_DISCRIMINATOR:
        raise RaydiumSwapFinalizationError("PoolState discriminator differs from the pinned layout")
    if pool.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT:
        raise RaydiumSwapFinalizationError("PoolState source revision is not pinned")

    _put_uint(data, 237, 16, pool.liquidity, "PoolState liquidity")
    _put_uint(data, 253, 16, pool.sqrt_price_x64, "PoolState sqrt_price_x64")
    _put_sint(data, 269, 4, pool.tick_current, "PoolState tick_current")
    _put_uint(data, 277, 16, pool.fee_growth_global_0_x64, "fee_growth_global_0_x64")
    _put_uint(data, 293, 16, pool.fee_growth_global_1_x64, "fee_growth_global_1_x64")
    _put_uint(data, 309, 8, pool.protocol_fees_token_0, "protocol_fees_token_0")
    _put_uint(data, 317, 8, pool.protocol_fees_token_1, "protocol_fees_token_1")
    if len(pool.tick_array_bitmap) != RAYDIUM_POOL_TICK_ARRAY_BITMAP_WORDS:
        raise RaydiumSwapFinalizationError("PoolState bitmap must contain exactly 16 words")
    for index, word in enumerate(pool.tick_array_bitmap):
        _put_uint(data, RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET + index * 8, 8, word, "tick-array bitmap word")
    _put_uint(data, 1064, 8, pool.fund_fees_token_0, "fund_fees_token_0")
    _put_uint(data, 1072, 8, pool.fund_fees_token_1, "fund_fees_token_1")

    if len(pool.reward_infos) != RAYDIUM_POOL_REWARD_COUNT:
        raise RaydiumSwapFinalizationError("PoolState must contain exactly three reward records")
    for index, reward in enumerate(pool.reward_infos):
        _encode_reward(
            data,
            RAYDIUM_POOL_REWARD_INFO_OFFSET + index * RAYDIUM_POOL_REWARD_INFO_LENGTH,
            reward,
        )
    dynamic = bytearray(RAYDIUM_POOL_DYNAMIC_FEE_INFO_LENGTH)
    _encode_dynamic_fee(dynamic, pool.dynamic_fee_info)
    start = RAYDIUM_POOL_DYNAMIC_FEE_INFO_OFFSET
    data[start:start + RAYDIUM_POOL_DYNAMIC_FEE_INFO_LENGTH] = dynamic

    encoded = bytes(data)
    digest = hashlib.sha256(encoded).hexdigest()
    return replace(pool, account_data_sha256=digest, raw_data=encoded), encoded


def _serialize_tick_array(
    array: RaydiumClmmTickArray,
    *,
    source_sha256: str,
) -> tuple[RaydiumClmmTickArray, bytes]:
    data = _verify_source_bytes(array.raw_data, source_sha256, RAYDIUM_TICK_ARRAY_DATA_LENGTH, "TickArrayState")
    if data[:8] != RAYDIUM_TICK_ARRAY_DISCRIMINATOR:
        raise RaydiumSwapFinalizationError("TickArrayState discriminator differs from the pinned layout")
    if data[8:40] != _pubkey_bytes(array.pool_address, "tick-array pool ID"):
        raise RaydiumSwapFinalizationError("TickArrayState source pool identity differs")
    if struct.unpack_from("<i", data, 40)[0] != array.start_tick_index:
        raise RaydiumSwapFinalizationError("TickArrayState source start index differs")
    if len(array.ticks) != RAYDIUM_TICK_ARRAY_SIZE:
        raise RaydiumSwapFinalizationError("TickArrayState must contain exactly 60 ticks")
    initialized_count = 0
    for index, tick in enumerate(array.ticks):
        if tick.initialized is not (
            tick.liquidity_gross > 0
            or tick.orders_amount > 0
            or tick.part_filled_orders_remaining > 0
        ):
            raise RaydiumSwapFinalizationError("tick initialized flag disagrees with its liquidity/order fields")
        if tick.initialized:
            initialized_count += 1
            if tick.tick_index != array.start_tick_index + index * array.tick_spacing:
                raise RaydiumSwapFinalizationError("initialized tick index differs from its canonical slot")
        offset = RAYDIUM_TICK_ARRAY_TICKS_OFFSET + index * RAYDIUM_TICK_STATE_SIZE
        _put_sint(data, offset, 4, tick.tick_index, "tick index")
        _put_sint(data, offset + 4, 16, tick.liquidity_net, "tick liquidity_net")
        _put_uint(data, offset + 20, 16, tick.liquidity_gross, "tick liquidity_gross")
        _put_uint(data, offset + 36, 16, tick.fee_growth_outside_0_x64, "tick fee growth outside token0")
        _put_uint(data, offset + 52, 16, tick.fee_growth_outside_1_x64, "tick fee growth outside token1")
        if len(tick.reward_growths_outside_x64) != 3:
            raise RaydiumSwapFinalizationError("tick must contain three reward-growth records")
        for reward_index, growth in enumerate(tick.reward_growths_outside_x64):
            _put_uint(data, offset + 68 + reward_index * 16, 16, growth, "tick reward growth outside")
        _put_uint(data, offset + 116, 8, tick.order_phase, "tick order_phase")
        _put_uint(data, offset + 124, 8, tick.orders_amount, "tick orders_amount")
        _put_uint(data, offset + 132, 8, tick.part_filled_orders_remaining, "tick part-filled amount")
        _put_uint(data, offset + 140, 16, tick.unfilled_ratio_x64, "tick unfilled ratio")
        if len(tick.padding) != 3 or any(tick.padding):
            raise RaydiumSwapFinalizationError("tick reserved padding must remain three zero u32 values")
        for pad_index, value in enumerate(tick.padding):
            _put_uint(data, offset + 156 + pad_index * 4, 4, value, "tick padding")
    if array.initialized_tick_count != initialized_count:
        raise RaydiumSwapFinalizationError("TickArrayState initialized count differs from its final tick records")
    _uint(array.recent_epoch, 64, "TickArrayState recent_epoch")
    data[RAYDIUM_TICK_ARRAY_INITIALIZED_COUNT_OFFSET] = initialized_count
    _put_uint(data, 10_125, 8, array.recent_epoch, "TickArrayState recent_epoch")

    encoded = bytes(data)
    digest = hashlib.sha256(encoded).hexdigest()
    return replace(array, data_sha256=digest, raw_data=encoded), encoded


def _encode_bitmap_extension(
    extension: RaydiumClmmTickArrayBitmapExtension,
) -> bytes:
    if not isinstance(extension, RaydiumClmmTickArrayBitmapExtension):
        raise RaydiumSwapFinalizationError("decoded bitmap-extension state is required")
    if len(extension.positive_bitmaps) != 14 or len(extension.negative_bitmaps) != 14:
        raise RaydiumSwapFinalizationError("bitmap extension must contain 14 positive and 14 negative rows")
    data = bytearray(
        TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR
        + _pubkey_bytes(extension.pool_address, "bitmap-extension pool ID")
    )
    for side in (extension.positive_bitmaps, extension.negative_bitmaps):
        for row in side:
            if len(row) != 8:
                raise RaydiumSwapFinalizationError("bitmap extension row must contain eight u64 words")
            for word in row:
                data.extend(int(word).to_bytes(8, "little"))
    if len(data) != TICK_ARRAY_BITMAP_EXTENSION_LENGTH:
        raise RaydiumSwapFinalizationError("serialized bitmap extension has an invalid length")
    return bytes(data)


def _serialize_observation(
    state: RaydiumClmmObservationState,
) -> tuple[RaydiumClmmObservationState, bytes]:
    data = _verify_source_bytes(
        state.raw_data,
        state.data_sha256,
        RAYDIUM_OBSERVATION_STATE_LENGTH,
        "ObservationState",
    )
    if data[:8] != RAYDIUM_OBSERVATION_STATE_DISCRIMINATOR:
        raise RaydiumSwapFinalizationError("ObservationState discriminator differs from the pinned layout")
    if len(state.observations) != RAYDIUM_OBSERVATION_COUNT:
        raise RaydiumSwapFinalizationError("ObservationState must contain exactly 100 observations")
    if not isinstance(state.initialized, bool):
        raise RaydiumSwapFinalizationError("ObservationState initialized flag must be boolean")
    data[8] = int(state.initialized)
    _put_uint(data, 9, 8, state.recent_epoch, "ObservationState recent_epoch")
    _put_uint(data, 17, 2, state.observation_index, "ObservationState ring index")
    if data[19:51] != _pubkey_bytes(state.pool_id, "ObservationState pool ID"):
        raise RaydiumSwapFinalizationError("ObservationState pool ID differs from its original account bytes")
    offset = 51
    for observation in state.observations:
        _put_uint(data, offset, 4, observation.block_timestamp, "observation timestamp")
        _put_sint(data, offset + 4, 8, observation.tick_cumulative, "observation tick cumulative")
        if len(observation.padding) != 32 or any(observation.padding):
            raise RaydiumSwapFinalizationError("observation reserved padding must remain 32 zero bytes")
        data[offset + 12:offset + 44] = observation.padding
        offset += RAYDIUM_OBSERVATION_RECORD_LENGTH
    if len(state.padding) != 32 or any(state.padding):
        raise RaydiumSwapFinalizationError("ObservationState trailing padding must remain 32 zero bytes")
    data[offset:offset + 32] = state.padding
    encoded = bytes(data)
    digest = hashlib.sha256(encoded).hexdigest()
    return replace(state, data_sha256=digest, raw_data=encoded), encoded


def finalize_raydium_swap_state(
    loop_result: RaydiumSwapLoopResult,
    observation: RaydiumClmmObservationState,
) -> RaydiumSwapFinalizationResult:
    """Settle amounts and serialize final PoolState, TickArrays and ObservationState."""
    if not isinstance(loop_result, RaydiumSwapLoopResult):
        raise RaydiumSwapFinalizationError("completed Raydium swap-loop result is required")
    if not isinstance(observation, RaydiumClmmObservationState):
        raise RaydiumSwapFinalizationError("decoded Raydium ObservationState is required")
    state = loop_result.state
    session = loop_result.mutation_session
    source_pool = session.pool
    if source_pool.pool_address != observation.pool_id or source_pool.observation_key != observation.address:
        raise RaydiumSwapFinalizationError("ObservationState is not the canonical account for the swap pool")
    if state.pool.pool_address != source_pool.pool_address:
        raise RaydiumSwapFinalizationError("swap-loop PoolState identity differs from its mutation session")

    fee_on_token0 = (
        state.zero_for_one if source_pool.fee_on == 0
        else True if source_pool.fee_on == 1
        else False if source_pool.fee_on == 2
        else None
    )
    if fee_on_token0 is None:
        raise RaydiumSwapFinalizationError("PoolState fee_on mode is invalid")
    settlement = settle_raydium_swap_amounts(
        state.fee_state,
        amount_specified=state.amount_specified,
        zero_for_one=state.zero_for_one,
        is_base_input=state.is_base_input,
        fee_on_token0=fee_on_token0,
    )

    fee_growth_0 = source_pool.fee_growth_global_0_x64
    fee_growth_1 = source_pool.fee_growth_global_1_x64
    protocol_0 = source_pool.protocol_fees_token_0
    protocol_1 = source_pool.protocol_fees_token_1
    fund_0 = source_pool.fund_fees_token_0
    fund_1 = source_pool.fund_fees_token_1
    if fee_on_token0:
        if state.fee_state.lp_fee > 0:
            fee_growth_0 = state.fee_state.fee_growth_global_x64
        protocol_0 += state.fee_state.protocol_fee
        fund_0 += state.fee_state.fund_fee
    else:
        if state.fee_state.lp_fee > 0:
            fee_growth_1 = state.fee_state.fee_growth_global_x64
        protocol_1 += state.fee_state.protocol_fee
        fund_1 += state.fee_state.fund_fee
    _uint(protocol_0, 64, "final protocol token0 fees")
    _uint(protocol_1, 64, "final protocol token1 fees")
    _uint(fund_0, 64, "final fund token0 fees")
    _uint(fund_1, 64, "final fund token1 fees")

    dynamic_fee_info = (
        state.dynamic_fee_state.info
        if state.dynamic_fee_state is not None
        else source_pool.dynamic_fee_info
    )
    pool_after = replace(
        source_pool,
        liquidity=state.fee_state.liquidity,
        sqrt_price_x64=state.sqrt_price_x64,
        tick_current=state.tick,
        fee_growth_global_0_x64=fee_growth_0,
        fee_growth_global_1_x64=fee_growth_1,
        protocol_fees_token_0=protocol_0,
        protocol_fees_token_1=protocol_1,
        fund_fees_token_0=fund_0,
        fund_fees_token_1=fund_1,
        dynamic_fee_info=dynamic_fee_info,
        reward_infos=loop_result.updated_reward_infos,
        account_data_sha256="",
    )
    pool_after, pool_data = _serialize_pool(
        pool_after,
        source_sha256=session.source_pool_sha256,
    )

    arrays = []
    array_data = []
    if len(session.tick_arrays) != len(session.source_tick_array_sha256):
        raise RaydiumSwapFinalizationError("tick-array session lost its source commitment index")
    for index, array in enumerate(session.tick_arrays):
        serialized, raw = _serialize_tick_array(
            array,
            source_sha256=session.source_tick_array_sha256[index],
        )
        arrays.append(serialized)
        array_data.append(raw)

    extension_after = session.bitmap_extension
    extension_data = None
    if extension_after is not None:
        original_extension = session.source_bitmap_extension
        if original_extension is None or session.source_bitmap_extension_sha256 is None:
            raise RaydiumSwapFinalizationError("bitmap-extension session lost its original account commitment")
        original_bytes = _encode_bitmap_extension(original_extension)
        if hashlib.sha256(original_bytes).hexdigest() != session.source_bitmap_extension_sha256:
            raise RaydiumSwapFinalizationError("original bitmap-extension words differ from their account commitment")
        extension_data = _encode_bitmap_extension(extension_after)
        extension_after = replace(
            extension_after,
            data_sha256=hashlib.sha256(extension_data).hexdigest(),
        )
    elif session.source_bitmap_extension is not None:
        raise RaydiumSwapFinalizationError("bitmap-extension account disappeared from the working set")

    source_observation_sha256 = observation.data_sha256
    observation_after = observation
    if state.tick != source_pool.tick_current:
        observation_after = apply_raydium_observation_update(
            observation,
            block_timestamp=state.block_timestamp,
            tick=source_pool.tick_current,
        )
    observation_after, observation_data = _serialize_observation(observation_after)

    return RaydiumSwapFinalizationResult(
        pool=pool_after,
        pool_data=pool_data,
        tick_arrays=tuple(arrays),
        tick_array_data=tuple(array_data),
        bitmap_extension=extension_after,
        bitmap_extension_data=extension_data,
        observation=observation_after,
        observation_data=observation_data,
        settlement=settlement,
        zero_for_one=state.zero_for_one,
        source_pool_sha256=session.source_pool_sha256,
        source_tick_array_sha256=session.source_tick_array_sha256,
        source_bitmap_extension_sha256=session.source_bitmap_extension_sha256,
        source_observation_sha256=source_observation_sha256,
    )
