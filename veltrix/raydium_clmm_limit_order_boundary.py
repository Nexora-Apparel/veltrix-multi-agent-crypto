"""Source-ordered limit-order boundary matching and amount application."""
from __future__ import annotations

from dataclasses import dataclass

from veltrix.raydium_clmm_fee_accounting import (
    RaydiumFeeAccountingError,
    RaydiumFeeAccountingState,
    apply_raydium_swap_amounts,
)
from veltrix.raydium_clmm_limit_orders import (
    RaydiumLimitOrderError,
    RaydiumLimitOrderMatch,
    match_raydium_limit_order,
)
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick


class RaydiumLimitOrderBoundaryError(ValueError):
    """Raised when a tick-boundary limit-order transition is inconsistent."""


@dataclass(frozen=True, slots=True)
class RaydiumLimitOrderBoundaryResult:
    tick: RaydiumClmmTick
    fee_state: RaydiumFeeAccountingState
    match: RaydiumLimitOrderMatch
    unfilled_amount_before: int
    unfilled_amount_after: int


def _unfilled_amount(tick: RaydiumClmmTick) -> int:
    for value, name in (
        (tick.orders_amount, "orders_amount"),
        (tick.part_filled_orders_remaining, "part_filled_orders_remaining"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << 64):
            raise RaydiumLimitOrderBoundaryError(f"{name} must fit u64")
    total = tick.orders_amount + tick.part_filled_orders_remaining
    if total >= (1 << 64):
        raise RaydiumLimitOrderBoundaryError("total unfilled limit-order amount overflows u64")
    return total


def apply_raydium_limit_order_boundary(
    tick: RaydiumClmmTick,
    fee_state: RaydiumFeeAccountingState,
    *,
    fee_rate: int,
    sqrt_price_x64: int,
    zero_for_one: bool,
    is_base_input: bool,
    is_fee_on_input: bool,
    protocol_fee_rate: int,
    fund_fee_rate: int,
) -> RaydiumLimitOrderBoundaryResult:
    """Match FIFO orders at the tick using the already-computed pre-advance fee rate."""
    if not isinstance(tick, RaydiumClmmTick):
        raise RaydiumLimitOrderBoundaryError("decoded initialized Raydium tick is required")
    if not isinstance(fee_state, RaydiumFeeAccountingState):
        raise RaydiumLimitOrderBoundaryError("Raydium fee-accounting state is required")
    before = _unfilled_amount(tick)
    try:
        updated_tick, match = match_raydium_limit_order(
            tick,
            swap_amount=fee_state.amount_specified_remaining,
            zero_for_one=zero_for_one,
            is_base_input=is_base_input,
            fee_rate=fee_rate,
            is_fee_on_input=is_fee_on_input,
            sqrt_price_x64=sqrt_price_x64,
        )
        if any((match.amount_in, match.amount_out, match.amm_fee_amount)):
            updated_fee_state = apply_raydium_swap_amounts(
                fee_state,
                amount_in=match.amount_in,
                amount_out=match.amount_out,
                fee_amount=match.amm_fee_amount,
                is_base_input=is_base_input,
                is_fee_on_input=is_fee_on_input,
                protocol_fee_rate=protocol_fee_rate,
                fund_fee_rate=fund_fee_rate,
            )
        else:
            updated_fee_state = fee_state
    except (RaydiumLimitOrderError, RaydiumFeeAccountingError) as exc:
        raise RaydiumLimitOrderBoundaryError(f"Raydium limit-order boundary failed: {exc}") from exc
    after = _unfilled_amount(updated_tick)
    return RaydiumLimitOrderBoundaryResult(
        tick=updated_tick,
        fee_state=updated_fee_state,
        match=match,
        unfilled_amount_before=before,
        unfilled_amount_after=after,
    )
