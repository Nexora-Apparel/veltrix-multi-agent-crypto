"""Source-exact tests for Raydium CLMM tick crossing and liquidity transitions."""
import pytest

from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick
from veltrix.raydium_clmm_tick_crossing import (
    I128_MIN,
    U128_MAX,
    RaydiumRewardGrowth,
    RaydiumTickCrossingError,
    add_liquidity_delta,
    apply_crossed_tick_liquidity,
    cross_tick_and_apply_liquidity,
    cross_tick_state,
)


def tick(**overrides):
    values = dict(
        tick_index=120,
        liquidity_net=25,
        liquidity_gross=100,
        orders_amount=0,
        part_filled_orders_remaining=0,
        initialized=True,
        fee_growth_outside_0_x64=5,
        fee_growth_outside_1_x64=7,
        reward_growths_outside_x64=(15, 123, 20),
        order_phase=0,
        unfilled_ratio_x64=0,
        padding=(0, 0, 0),
    )
    values.update(overrides)
    return RaydiumClmmTick(**values)


def rewards():
    return (
        RaydiumRewardGrowth(True, 10),
        RaydiumRewardGrowth(False, 999),
        RaydiumRewardGrowth(True, 20),
    )


def test_cross_matches_wrapping_fee_and_initialized_reward_growth_inversion():
    crossed, liquidity_net = cross_tick_state(
        tick(),
        fee_growth_global_0_x64=3,
        fee_growth_global_1_x64=9,
        reward_infos=rewards(),
    )
    assert crossed.fee_growth_outside_0_x64 == U128_MAX - 1
    assert crossed.fee_growth_outside_1_x64 == 2
    assert crossed.reward_growths_outside_x64 == (U128_MAX - 4, 123, 0)
    assert liquidity_net == 25
    assert crossed.liquidity_net == tick().liquidity_net


def test_crossing_twice_with_same_global_growth_restores_outside_values():
    original = tick()
    first, _ = cross_tick_state(
        original,
        fee_growth_global_0_x64=3,
        fee_growth_global_1_x64=9,
        reward_infos=rewards(),
    )
    second, _ = cross_tick_state(
        first,
        fee_growth_global_0_x64=3,
        fee_growth_global_1_x64=9,
        reward_infos=rewards(),
    )
    assert second.fee_growth_outside_0_x64 == original.fee_growth_outside_0_x64
    assert second.fee_growth_outside_1_x64 == original.fee_growth_outside_1_x64
    assert second.reward_growths_outside_x64 == original.reward_growths_outside_x64


@pytest.mark.parametrize("zero_for_one,expected", [(False, 125), (True, 75)])
def test_liquidity_net_sign_is_directional(zero_for_one, expected):
    assert apply_crossed_tick_liquidity(
        100, 25, zero_for_one=zero_for_one
    ) == expected


@pytest.mark.parametrize("liquidity_net,zero_for_one,expected", [
    (-25, False, 75),
    (-25, True, 125),
    (0, False, 100),
])
def test_negative_and_zero_liquidity_net_transitions(liquidity_net, zero_for_one, expected):
    assert apply_crossed_tick_liquidity(
        100, liquidity_net, zero_for_one=zero_for_one
    ) == expected


def test_cross_tick_and_apply_liquidity_returns_atomic_composed_result():
    result = cross_tick_and_apply_liquidity(
        tick(),
        current_liquidity=100,
        fee_growth_global_0_x64=3,
        fee_growth_global_1_x64=9,
        reward_infos=rewards(),
        zero_for_one=True,
    )
    assert result.liquidity_after == 75
    assert result.liquidity_net == 25
    assert result.tick.fee_growth_outside_0_x64 == U128_MAX - 1


def test_liquidity_delta_allows_exact_subtraction_to_zero():
    assert add_liquidity_delta(50, -50) == 0


@pytest.mark.parametrize("liquidity,delta", [
    (0, -1),
    (U128_MAX, 1),
])
def test_liquidity_delta_rejects_underflow_and_overflow(liquidity, delta):
    with pytest.raises(RaydiumTickCrossingError):
        add_liquidity_delta(liquidity, delta)


def test_rejects_i128_minimum_when_direction_requires_negation():
    with pytest.raises(RaydiumTickCrossingError, match="negating"):
        apply_crossed_tick_liquidity(1, I128_MIN, zero_for_one=True)


@pytest.mark.parametrize("orders,part_filled", [(1, 0), (0, 1), (1, 1)])
def test_rejects_crossing_tick_with_outstanding_limit_orders(orders, part_filled):
    with pytest.raises(RaydiumTickCrossingError, match="no outstanding limit orders"):
        cross_tick_state(
            tick(orders_amount=orders, part_filled_orders_remaining=part_filled),
            fee_growth_global_0_x64=1,
            fee_growth_global_1_x64=1,
            reward_infos=rewards(),
        )


def test_rejects_tick_without_active_liquidity():
    with pytest.raises(RaydiumTickCrossingError, match="requires liquidity"):
        cross_tick_state(
            tick(liquidity_gross=0),
            fee_growth_global_0_x64=1,
            fee_growth_global_1_x64=1,
            reward_infos=rewards(),
        )


def test_rejects_wrong_reward_slot_count_and_invalid_reward_state():
    with pytest.raises(RaydiumTickCrossingError, match="exactly three"):
        cross_tick_state(
            tick(),
            fee_growth_global_0_x64=1,
            fee_growth_global_1_x64=1,
            reward_infos=(),
        )
    with pytest.raises(RaydiumTickCrossingError, match="reward slot 1"):
        cross_tick_state(
            tick(),
            fee_growth_global_0_x64=1,
            fee_growth_global_1_x64=1,
            reward_infos=(rewards()[0], object(), rewards()[2]),
        )


def test_rejects_growth_values_outside_u128():
    with pytest.raises(RaydiumTickCrossingError, match="u128"):
        cross_tick_state(
            tick(),
            fee_growth_global_0_x64=U128_MAX + 1,
            fee_growth_global_1_x64=1,
            reward_infos=rewards(),
        )


def test_rejects_tick_with_inconsistent_uninitialized_flag():
    with pytest.raises(RaydiumTickCrossingError, match="initialized tick"):
        cross_tick_state(
            tick(initialized=False),
            fee_growth_global_0_x64=1,
            fee_growth_global_1_x64=1,
            reward_infos=rewards(),
        )
