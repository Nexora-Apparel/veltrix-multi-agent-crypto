"""Deterministic Raydium swap-loop pre-step transition, pinned to swap_internal."""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_dynamic_fee import (
    RaydiumDynamicFeeSwapState,
    RaydiumSpacingBound,
    compute_total_fee_rate,
    get_spacing_bounded_price,
    update_volatility_accumulator,
)
from veltrix.raydium_clmm_fee_accounting import (
    RaydiumFeeAccountingState,
    apply_raydium_swap_amounts,
)
from veltrix.raydium_clmm_swap_step_math import (
    RaydiumSwapStepResult,
    compute_swap_step,
)


class RaydiumSwapLoopError(ValueError):
    """Raised when a pinned Raydium swap-loop pre-step cannot be evaluated."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmSwapLoopPreStepResult:
    dynamic_fee_state: RaydiumDynamicFeeSwapState | None
    total_fee_rate: int
    spacing_bound: RaydiumSpacingBound
    swap_step: RaydiumSwapStepResult
    fee_accounting_state: RaydiumFeeAccountingState
    price_changed: bool


def compute_raydium_swap_loop_pre_step(
    fee_state: RaydiumFeeAccountingState,
    *,
    sqrt_price_current_x64: int,
    target_price_x64: int,
    base_fee_rate: int,
    zero_for_one: bool,
    is_base_input: bool,
    is_fee_on_input: bool,
    protocol_fee_rate: int,
    fund_fee_rate: int,
    dynamic_fee_state: RaydiumDynamicFeeSwapState | None = None,
) -> RaydiumClmmSwapLoopPreStepResult:
    """Execute source lines 635–668: volatility -> total fee -> spacing bound -> swap math -> amount accounting.

    FIFO matching is deliberately not performed here. The returned total fee
    rate is the pre-index-advance rate and must be reused for a boundary limit
    order match before the caller advances the dynamic-fee spacing index.
    """
    if not isinstance(fee_state, RaydiumFeeAccountingState):
        raise RaydiumSwapLoopError("Raydium fee-accounting state is required")
    for value, field in (
        (zero_for_one, "zero_for_one"),
        (is_base_input, "is_base_input"),
        (is_fee_on_input, "is_fee_on_input"),
    ):
        if not isinstance(value, bool):
            raise RaydiumSwapLoopError(f"{field} must be boolean")
    if isinstance(base_fee_rate, bool) or not isinstance(base_fee_rate, int) or not 0 <= base_fee_rate < 1_000_000:
        raise RaydiumSwapLoopError("base fee rate must be in [0, 1,000,000)")
    if dynamic_fee_state is None:
        total_fee_rate = base_fee_rate
        spacing_bound = RaydiumSpacingBound(
            reached_target=True,
            sqrt_price_x64=target_price_x64,
            bounded_tick=None,
        )
        updated_dynamic_state = None
    else:
        if not isinstance(dynamic_fee_state, RaydiumDynamicFeeSwapState):
            raise RaydiumSwapLoopError("dynamic-fee swap state is malformed")
        if dynamic_fee_state.base_fee_rate != base_fee_rate:
            raise RaydiumSwapLoopError("dynamic-fee state base rate differs from the requested pool base fee")
        if dynamic_fee_state.tick_spacing <= 0:
            raise RaydiumSwapLoopError("dynamic-fee tick spacing must be positive")
        if dynamic_fee_state.info.enabled is not True:
            raise RaydiumSwapLoopError("dynamic-fee swap state must contain enabled configuration")
        if fee_state.liquidity < 0:
            raise RaydiumSwapLoopError("fee-accounting liquidity is invalid")
        updated_info = update_volatility_accumulator(
            dynamic_fee_state.info,
            tick_spacing_index=dynamic_fee_state.tick_spacing_index,
        )
        updated_dynamic_state = replace(
            dynamic_fee_state,
            info=updated_info,
        )
        total_fee_rate = compute_total_fee_rate(
            base_fee_rate=base_fee_rate,
            info=updated_info,
            tick_spacing=dynamic_fee_state.tick_spacing,
        )
        spacing_bound = get_spacing_bounded_price(
            updated_dynamic_state,
            target_price_x64=target_price_x64,
            zero_for_one=zero_for_one,
            liquidity=fee_state.liquidity,
        )

    if not 0 <= spacing_bound.sqrt_price_x64 < (1 << 128):
        raise RaydiumSwapLoopError("bounded sqrt price is outside u128")
    price_changed = sqrt_price_current_x64 != spacing_bound.sqrt_price_x64
    if price_changed:
        step = compute_swap_step(
            sqrt_price_current_x64,
            spacing_bound.sqrt_price_x64,
            fee_state.liquidity,
            fee_state.amount_specified_remaining,
            total_fee_rate,
            is_base_input=is_base_input,
            zero_for_one=zero_for_one,
            is_fee_on_input=is_fee_on_input,
        )
        next_fee_state = apply_raydium_swap_amounts(
            fee_state,
            amount_in=step.amount_in,
            amount_out=step.amount_out,
            fee_amount=step.fee_amount,
            is_base_input=is_base_input,
            is_fee_on_input=is_fee_on_input,
            protocol_fee_rate=protocol_fee_rate,
            fund_fee_rate=fund_fee_rate,
        )
    else:
        step = RaydiumSwapStepResult(
            sqrt_price_next_x64=spacing_bound.sqrt_price_x64,
            amount_in=0,
            amount_out=0,
            fee_amount=0,
            reached_target=False,
        )
        next_fee_state = fee_state

    return RaydiumClmmSwapLoopPreStepResult(
        dynamic_fee_state=updated_dynamic_state,
        total_fee_rate=total_fee_rate,
        spacing_bound=spacing_bound,
        swap_step=step,
        fee_accounting_state=next_fee_state,
        price_changed=price_changed,
    )
