"""Source-ordered PoolState::update_after_swap model for Raydium CLMM."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from veltrix.raydium_clmm_pool_state import (
    RaydiumClmmPoolState,
    RaydiumClmmPoolStateError,
    encode_raydium_clmm_pool_state,
)
from veltrix.raydium_clmm_swap_inner_iteration import RaydiumClmmSwapInnerIterationResult


class RaydiumPoolPostSwapFinalizationError(ValueError):
    """Raised when the final swap state cannot be applied to PoolState."""


@dataclass(frozen=True, slots=True)
class RaydiumPoolPostSwapFinalizationResult:
    pool: RaydiumClmmPoolState
    fee_token0: bool
    serialized: bool


def _u64(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << 64):
        raise RaydiumPoolPostSwapFinalizationError(f"{field} must fit u64")


def _u128(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << 128):
        raise RaydiumPoolPostSwapFinalizationError(f"{field} must fit u128")


def _fee_on_token0(pool: RaydiumClmmPoolState, zero_for_one: bool) -> bool:
    if pool.fee_on == 0:
        return zero_for_one
    if pool.fee_on == 1:
        return True
    if pool.fee_on == 2:
        return False
    raise RaydiumPoolPostSwapFinalizationError("PoolState fee_on mode must be 0, 1 or 2")


def _checked_add_u64(left: int, right: int, field: str) -> int:
    _u64(left, f"existing {field}")
    _u64(right, f"swap {field}")
    total = left + right
    if total >= (1 << 64):
        raise RaydiumPoolPostSwapFinalizationError(f"{field} overflows u64")
    return total


def finalize_raydium_pool_after_swap(
    pool: RaydiumClmmPoolState,
    iteration: RaydiumClmmSwapInnerIterationResult,
    *,
    zero_for_one: bool,
) -> RaydiumPoolPostSwapFinalizationResult:
    """Apply final tick/price/liquidity/fee/dynamic-fee values exactly once after the swap loop."""
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumPoolPostSwapFinalizationError("decoded Raydium PoolState is required")
    if not isinstance(iteration, RaydiumClmmSwapInnerIterationResult):
        raise RaydiumPoolPostSwapFinalizationError("completed Raydium swap iteration is required")
    if iteration.swap_complete is not True:
        raise RaydiumPoolPostSwapFinalizationError("PoolState finalization requires the entire swap to be complete")
    if not isinstance(zero_for_one, bool):
        raise RaydiumPoolPostSwapFinalizationError("swap direction must be boolean")

    state = iteration.fee_state
    if state is None:
        raise RaydiumPoolPostSwapFinalizationError("final fee-accounting state is required")
    _u128(state.liquidity, "final liquidity")
    _u128(iteration.sqrt_price_x64, "final sqrt price")
    if isinstance(iteration.tick, bool) or not isinstance(iteration.tick, int) or not -(1 << 31) <= iteration.tick < (1 << 31):
        raise RaydiumPoolPostSwapFinalizationError("final tick must fit i32")
    for value, field in (
        (state.lp_fee, "LP fee"),
        (state.protocol_fee, "protocol fee"),
        (state.fund_fee, "fund fee"),
    ):
        _u64(value, field)
    _u128(state.fee_growth_global_x64, "final fee growth global")

    fee_token0 = _fee_on_token0(pool, zero_for_one)
    next_pool = replace(
        pool,
        tick_current=iteration.tick,
        sqrt_price_x64=iteration.sqrt_price_x64,
        liquidity=state.liquidity,
    )

    if fee_token0:
        if state.lp_fee > 0:
            next_pool = replace(next_pool, fee_growth_global_0_x64=state.fee_growth_global_x64)
        if state.protocol_fee > 0:
            next_pool = replace(
                next_pool,
                protocol_fees_token_0=_checked_add_u64(
                    pool.protocol_fees_token_0, state.protocol_fee, "protocol fees token 0"
                ),
            )
        if state.fund_fee > 0:
            next_pool = replace(
                next_pool,
                fund_fees_token_0=_checked_add_u64(
                    pool.fund_fees_token_0, state.fund_fee, "fund fees token 0"
                ),
            )
    else:
        if state.lp_fee > 0:
            next_pool = replace(next_pool, fee_growth_global_1_x64=state.fee_growth_global_x64)
        if state.protocol_fee > 0:
            next_pool = replace(
                next_pool,
                protocol_fees_token_1=_checked_add_u64(
                    pool.protocol_fees_token_1, state.protocol_fee, "protocol fees token 1"
                ),
            )
        if state.fund_fee > 0:
            next_pool = replace(
                next_pool,
                fund_fees_token_1=_checked_add_u64(
                    pool.fund_fees_token_1, state.fund_fee, "fund fees token 1"
                ),
            )

    dynamic = iteration.dynamic_fee_state
    if dynamic is not None:
        info = dynamic.info
        current = pool.dynamic_fee_info
        if not current.enabled:
            raise RaydiumPoolPostSwapFinalizationError(
                "dynamic-fee state was returned for a PoolState with dynamic fees disabled"
            )
        if (
            info.filter_period != current.filter_period
            or info.decay_period != current.decay_period
            or info.reduction_factor != current.reduction_factor
            or info.dynamic_fee_control != current.dynamic_fee_control
            or info.max_volatility_accumulator != current.max_volatility_accumulator
        ):
            raise RaydiumPoolPostSwapFinalizationError(
                "dynamic-fee configuration changed during swap finalization"
            )
        next_pool = replace(
            next_pool,
            dynamic_fee_info=replace(
                current,
                last_update_timestamp=info.last_update_timestamp,
                volatility_reference=info.volatility_reference,
                tick_spacing_index_reference=info.tick_spacing_index_reference,
                volatility_accumulator=info.volatility_accumulator,
            ),
        )

    if pool.account_data:
        try:
            encoded = encode_raydium_clmm_pool_state(next_pool)
        except RaydiumClmmPoolStateError as exc:
            raise RaydiumPoolPostSwapFinalizationError(f"PoolState serialization failed: {exc}") from exc
        next_pool = replace(
            next_pool,
            account_data=encoded,
            account_data_sha256=hashlib.sha256(encoded).hexdigest(),
        )

    return RaydiumPoolPostSwapFinalizationResult(
        pool=next_pool,
        fee_token0=fee_token0,
        serialized=bool(pool.account_data),
    )
