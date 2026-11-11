"""Ordered Raydium CLMM dynamic-fee, price-bound and amount-application step.

Composes the pinned swap.rs inner-loop operations at lines 633-667.
Limit-order matching, tick crossing, tick-array mutation and price/tick
advancement are intentionally handled by the following boundary stage.
"""
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
    RaydiumFeeAccountingError,
    RaydiumFeeAccountingState,
    apply_raydium_swap_amounts,
)
from veltrix.raydium_clmm_swap_step_math import (
    RaydiumSwapMathError,
    RaydiumSwapStepResult,
    compute_swap_step,
)


class RaydiumSwapPriceStepError(ValueError):
    """Raised when a source-ordered Raydium price/fee step is invalid."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmSwapPriceStep:
    fee_rate: int
    spacing_bound: RaydiumSpacingBound
    swap_result: RaydiumSwapStepResult
    fee_state: RaydiumFeeAccountingState
    dynamic_fee_state: RaydiumDynamicFeeSwapState | None
    price_changed: bool


def compute_raydium_clmm_swap_price_step(
    fee_state: RaydiumFeeAccountingState,
    *,
    sqrt_price_current_x64: int,
    target_price_x64: int,
    base_fee_rate: int,
    protocol_fee_rate: int,
    fund_fee_rate: int,
    zero_for_one: bool,
    is_base_input: bool,
    is_fee_on_input: bool,
    dynamic_fee_state: RaydiumDynamicFeeSwapState | None = None,
) -> RaydiumClmmSwapPriceStep:
    """Apply volatility update -> total fee -> spacing bound -> swap math -> fee accounting."""
    if not isinstance(fee_state, RaydiumFeeAccountingState):
        raise RaydiumSwapPriceStepError("Raydium fee-accounting state is required")
    if dynamic_fee_state is not None and not isinstance(dynamic_fee_state, RaydiumDynamicFeeSwapState):
        raise RaydiumSwapPriceStepError("dynamic-fee state must be decoded or None")
    for value, name in (
        (sqrt_price_current_x64, "current sqrt price"),
        (target_price_x64, "target sqrt price"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < (1 << 128):
            raise RaydiumSwapPriceStepError(f"{name} must be a positive u128")
    if isinstance(base_fee_rate, bool) or not isinstance(base_fee_rate, int) or not 0 <= base_fee_rate < 1_000_000:
        raise RaydiumSwapPriceStepError("base fee rate must be below the Raydium fee denominator")
    for flag, name in (
        (zero_for_one, "zero_for_one"),
        (is_base_input, "is_base_input"),
        (is_fee_on_input, "is_fee_on_input"),
    ):
        if not isinstance(flag, bool):
            raise RaydiumSwapPriceStepError(f"{name} must be boolean")

    updated_dynamic = dynamic_fee_state
    if dynamic_fee_state is not None:
        updated_info = update_volatility_accumulator(
            dynamic_fee_state.info,
            tick_spacing_index=dynamic_fee_state.tick_spacing_index,
        )
        updated_dynamic = replace(dynamic_fee_state, info=updated_info)
        fee_rate = compute_total_fee_rate(
            base_fee_rate=base_fee_rate,
            info=updated_info,
            tick_spacing=dynamic_fee_state.tick_spacing,
        )
        spacing_bound = get_spacing_bounded_price(
            updated_dynamic,
            target_price_x64=target_price_x64,
            zero_for_one=zero_for_one,
            liquidity=fee_state.liquidity,
        )
    else:
        fee_rate = base_fee_rate
        spacing_bound = RaydiumSpacingBound(
            reached_target=True,
            sqrt_price_x64=target_price_x64,
            bounded_tick=None,
        )

    price_changed = sqrt_price_current_x64 != spacing_bound.sqrt_price_x64
    if price_changed:
        try:
            swap_result = compute_swap_step(
                sqrt_price_current_x64,
                spacing_bound.sqrt_price_x64,
                fee_state.liquidity,
                fee_state.amount_specified_remaining,
                fee_rate,
                is_base_input=is_base_input,
                zero_for_one=zero_for_one,
                is_fee_on_input=is_fee_on_input,
            )
            next_fee_state = apply_raydium_swap_amounts(
                fee_state,
                amount_in=swap_result.amount_in,
                amount_out=swap_result.amount_out,
                fee_amount=swap_result.fee_amount,
                is_base_input=is_base_input,
                is_fee_on_input=is_fee_on_input,
                protocol_fee_rate=protocol_fee_rate,
                fund_fee_rate=fund_fee_rate,
            )
        except (RaydiumSwapMathError, RaydiumFeeAccountingError) as exc:
            raise RaydiumSwapPriceStepError(f"Raydium price/fee step failed: {exc}") from exc
    else:
        swap_result = RaydiumSwapStepResult(
            sqrt_price_next_x64=spacing_bound.sqrt_price_x64,
            amount_in=0,
            amount_out=0,
            fee_amount=0,
            reached_target=True,
        )
        next_fee_state = fee_state

    return RaydiumClmmSwapPriceStep(
        fee_rate=fee_rate,
        spacing_bound=spacing_bound,
        swap_result=swap_result,
        fee_state=next_fee_state,
        dynamic_fee_state=updated_dynamic,
        price_changed=price_changed,
    )
