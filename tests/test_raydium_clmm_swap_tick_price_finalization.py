import pytest

from veltrix.raydium_clmm_dynamic_fee import RaydiumDynamicFeeSwapState
from veltrix.raydium_clmm_pool_state import RaydiumClmmDynamicFeeInfo
from veltrix.raydium_clmm_swap_price_step import RaydiumClmmSwapPriceStep
from veltrix.raydium_clmm_swap_step_math import RaydiumSwapStepResult
from veltrix.raydium_clmm_dynamic_fee import RaydiumSpacingBound
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick
from veltrix.raydium_clmm_swap_tick_price_finalization import (
    RaydiumSwapTickPriceFinalizationError,
    finalize_raydium_swap_tick_price,
)


def tick(**overrides):
    values = dict(
        tick_index=10,
        liquidity_net=0,
        liquidity_gross=100,
        orders_amount=0,
        part_filled_orders_remaining=0,
        initialized=True,
        fee_growth_outside_0_x64=0,
        fee_growth_outside_1_x64=0,
        reward_growths_outside_x64=(0, 0, 0),
        order_phase=0,
        unfilled_ratio_x64=0,
        padding=(0, 0, 0),
    )
    values.update(overrides)
    return RaydiumClmmTick(**values)


def price_step(*, current_tick=0, next_tick=10, bounded_tick=None, price_changed=True, dynamic=None):
    current_price = get_sqrt_price_at_tick(current_tick)
    next_price = get_sqrt_price_at_tick(next_tick)
    price = get_sqrt_price_at_tick(bounded_tick) if bounded_tick is not None else next_price
    return RaydiumClmmSwapPriceStep(
        fee_rate=1000,
        spacing_bound=RaydiumSpacingBound(
            reached_target=bounded_tick is None,
            sqrt_price_x64=price,
            bounded_tick=bounded_tick,
        ),
        swap_result=RaydiumSwapStepResult(
            sqrt_price_next_x64=price if price_changed else current_price,
            amount_in=1,
            amount_out=1,
            fee_amount=0,
            reached_target=True,
        ),
        fee_state=None,
        dynamic_fee_state=dynamic,
        price_changed=price_changed,
    )


def dynamic_state():
    info = RaydiumClmmDynamicFeeInfo(
        filter_period=60,
        decay_period=3600,
        reduction_factor=5000,
        dynamic_fee_control=1000,
        max_volatility_accumulator=100_000,
        tick_spacing_index_reference=0,
        volatility_reference=0,
        volatility_accumulator=0,
        last_update_timestamp=1000,
        padding=bytes(46),
    )
    return RaydiumDynamicFeeSwapState(info, 10, 1000, 0)


@pytest.mark.parametrize(
    "zero_for_one,orders,expected",
    [
        (True, False, 9),
        (False, False, 10),
        (True, True, 10),
        (False, True, 9),
    ],
)
def test_initialized_tick_assignment_matches_direction_and_remaining_orders(zero_for_one, orders, expected):
    updated = tick(orders_amount=1 if orders else 0)
    result = finalize_raydium_swap_tick_price(
        price_step(),
        current_tick=5,
        next_tick=10,
        sqrt_price_next_x64=get_sqrt_price_at_tick(10),
        updated_tick_state=updated,
        reached_initialized_tick=True,
        zero_for_one=zero_for_one,
    )
    assert result.tick == expected
    assert result.sqrt_price_x64 == get_sqrt_price_at_tick(10)


def test_non_boundary_spacing_bound_uses_bounded_tick():
    result = finalize_raydium_swap_tick_price(
        price_step(next_tick=20, bounded_tick=10),
        current_tick=5,
        next_tick=20,
        sqrt_price_next_x64=get_sqrt_price_at_tick(20),
        updated_tick_state=tick(),
        reached_initialized_tick=False,
        zero_for_one=False,
    )
    assert result.tick == 10
    assert result.sqrt_price_x64 == get_sqrt_price_at_tick(10)


def test_non_boundary_unbounded_price_uses_tick_math_and_no_price_change_preserves_tick():
    unbounded = finalize_raydium_swap_tick_price(
        price_step(next_tick=20),
        current_tick=5,
        next_tick=20,
        sqrt_price_next_x64=get_sqrt_price_at_tick(20),
        updated_tick_state=tick(),
        reached_initialized_tick=False,
        zero_for_one=False,
    )
    assert unbounded.tick == 20

    unchanged = finalize_raydium_swap_tick_price(
        price_step(current_tick=5, next_tick=20, price_changed=False),
        current_tick=5,
        next_tick=20,
        sqrt_price_next_x64=get_sqrt_price_at_tick(20),
        updated_tick_state=tick(),
        reached_initialized_tick=False,
        zero_for_one=False,
    )
    assert unchanged.tick == 5
    assert unchanged.sqrt_price_x64 == get_sqrt_price_at_tick(5)


@pytest.mark.parametrize("zero_for_one,expected_index", [(True, 0), (False, 1)])
def test_dynamic_fee_index_advances_after_tick_assignment(zero_for_one, expected_index):
    result = finalize_raydium_swap_tick_price(
        price_step(dynamic=dynamic_state()),
        current_tick=5,
        next_tick=10,
        sqrt_price_next_x64=get_sqrt_price_at_tick(10),
        updated_tick_state=tick(),
        reached_initialized_tick=True,
        zero_for_one=zero_for_one,
    )
    assert result.dynamic_fee_state.tick_spacing_index == expected_index


def test_finalizer_rejects_mismatched_tick_price_and_boundary_flag():
    with pytest.raises(RaydiumSwapTickPriceFinalizationError, match="canonical tick math"):
        finalize_raydium_swap_tick_price(
            price_step(),
            current_tick=5,
            next_tick=10,
            sqrt_price_next_x64=get_sqrt_price_at_tick(11),
            updated_tick_state=tick(),
            reached_initialized_tick=True,
            zero_for_one=False,
        )
    with pytest.raises(RaydiumSwapTickPriceFinalizationError, match="different sqrt price"):
        finalize_raydium_swap_tick_price(
            price_step(next_tick=10, bounded_tick=5),
            current_tick=5,
            next_tick=10,
            sqrt_price_next_x64=get_sqrt_price_at_tick(10),
            updated_tick_state=tick(),
            reached_initialized_tick=True,
            zero_for_one=False,
        )
