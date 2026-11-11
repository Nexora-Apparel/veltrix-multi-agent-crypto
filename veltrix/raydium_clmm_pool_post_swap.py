"""Pinned Raydium PoolState::update_after_swap and update_fee transitions."""
from __future__ import annotations

from dataclasses import replace
import hashlib

from veltrix.raydium_clmm_pool_state import (
    RaydiumClmmDynamicFeeInfo,
    RaydiumClmmPoolState,
    encode_raydium_clmm_pool_state,
)

U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1
I32_MIN = -(1 << 31)
I32_MAX = (1 << 31) - 1


class RaydiumPoolPostSwapError(ValueError):
    """Raised when post-swap PoolState mutation exceeds source invariants."""


def _unsigned(value: int, maximum: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise RaydiumPoolPostSwapError(f"{field} is outside its unsigned integer range")


def _checked_add_u64(left: int, right: int, field: str) -> int:
    result = left + right
    if result > U64_MAX:
        raise RaydiumPoolPostSwapError(f"{field} overflows u64")
    return result


def _fee_on_token0(fee_on: int, zero_for_one: bool) -> bool:
    if fee_on == 0:
        return zero_for_one
    if fee_on == 1:
        return True
    if fee_on == 2:
        return False
    raise RaydiumPoolPostSwapError("pool fee_on mode is invalid")


def apply_raydium_pool_after_swap(
    pool: RaydiumClmmPoolState,
    *,
    tick_current: int,
    sqrt_price_x64: int,
    liquidity: int,
    lp_fee: int,
    protocol_fee: int,
    fund_fee: int,
    fee_growth_global_x64: int,
    zero_for_one: bool,
    dynamic_fee_info: RaydiumClmmDynamicFeeInfo | None,
) -> RaydiumClmmPoolState:
    """Port PoolState::update_after_swap/update_fee with immutable state output."""
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumPoolPostSwapError("decoded Raydium PoolState is required")
    if isinstance(tick_current, bool) or not isinstance(tick_current, int) or not I32_MIN <= tick_current <= I32_MAX:
        raise RaydiumPoolPostSwapError("tick_current must fit i32")
    _unsigned(sqrt_price_x64, U128_MAX, "sqrt_price_x64")
    _unsigned(liquidity, U128_MAX, "liquidity")
    _unsigned(lp_fee, U64_MAX, "lp_fee")
    _unsigned(protocol_fee, U64_MAX, "protocol_fee")
    _unsigned(fund_fee, U64_MAX, "fund_fee")
    _unsigned(fee_growth_global_x64, U128_MAX, "fee_growth_global_x64")
    if not isinstance(zero_for_one, bool):
        raise RaydiumPoolPostSwapError("zero_for_one must be boolean")

    fee_token0 = _fee_on_token0(pool.fee_on, zero_for_one)
    if fee_token0:
        growth0 = fee_growth_global_x64 if lp_fee > 0 else pool.fee_growth_global_0_x64
        growth1 = pool.fee_growth_global_1_x64
        protocol0 = _checked_add_u64(pool.protocol_fees_token_0, protocol_fee, "protocol_fees_token_0")
        protocol1 = pool.protocol_fees_token_1
        fund0 = _checked_add_u64(pool.fund_fees_token_0, fund_fee, "fund_fees_token_0")
        fund1 = pool.fund_fees_token_1
    else:
        growth0 = pool.fee_growth_global_0_x64
        growth1 = fee_growth_global_x64 if lp_fee > 0 else pool.fee_growth_global_1_x64
        protocol0 = pool.protocol_fees_token_0
        protocol1 = _checked_add_u64(pool.protocol_fees_token_1, protocol_fee, "protocol_fees_token_1")
        fund0 = pool.fund_fees_token_0
        fund1 = _checked_add_u64(pool.fund_fees_token_1, fund_fee, "fund_fees_token_1")

    next_dynamic = pool.dynamic_fee_info
    if next_dynamic.enabled and dynamic_fee_info is not None:
        if not isinstance(dynamic_fee_info, RaydiumClmmDynamicFeeInfo) or not dynamic_fee_info.enabled:
            raise RaydiumPoolPostSwapError("updated dynamic fee state is invalid")
        _unsigned(dynamic_fee_info.volatility_reference, U32_MAX, "volatility_reference")
        _unsigned(dynamic_fee_info.volatility_accumulator, U32_MAX, "volatility_accumulator")
        _unsigned(dynamic_fee_info.last_update_timestamp, U64_MAX, "last_update_timestamp")
        if (
            isinstance(dynamic_fee_info.tick_spacing_index_reference, bool)
            or not isinstance(dynamic_fee_info.tick_spacing_index_reference, int)
            or not I32_MIN <= dynamic_fee_info.tick_spacing_index_reference <= I32_MAX
            or dynamic_fee_info.volatility_reference > next_dynamic.max_volatility_accumulator
            or dynamic_fee_info.volatility_accumulator > next_dynamic.max_volatility_accumulator
        ):
            raise RaydiumPoolPostSwapError("updated dynamic fee state exceeds configured bounds")
        next_dynamic = replace(
            next_dynamic,
            last_update_timestamp=dynamic_fee_info.last_update_timestamp,
            volatility_reference=dynamic_fee_info.volatility_reference,
            tick_spacing_index_reference=dynamic_fee_info.tick_spacing_index_reference,
            volatility_accumulator=dynamic_fee_info.volatility_accumulator,
        )

    next_pool = replace(
        pool,
        tick_current=tick_current,
        sqrt_price_x64=sqrt_price_x64,
        liquidity=liquidity,
        fee_growth_global_0_x64=growth0,
        fee_growth_global_1_x64=growth1,
        protocol_fees_token_0=protocol0,
        protocol_fees_token_1=protocol1,
        fund_fees_token_0=fund0,
        fund_fees_token_1=fund1,
        dynamic_fee_info=next_dynamic,
    )
    if next_pool.account_data:
        encoded = encode_raydium_clmm_pool_state(next_pool)
        next_pool = replace(
            next_pool,
            account_data=encoded,
            account_data_sha256=hashlib.sha256(encoded).hexdigest(),
        )
    return next_pool


U32_MAX = (1 << 32) - 1
