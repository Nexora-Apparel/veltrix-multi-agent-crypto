"""Source-vector and adversarial tests for Raydium CLMM dynamic-fee arithmetic."""
from dataclasses import replace

import pytest

from veltrix.raydium_clmm_pool_state import RaydiumClmmDynamicFeeInfo
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick
from veltrix.raydium_clmm_dynamic_fee import (
    DYNAMIC_FEE_CONTROL_DENOMINATOR,
    MAX_FEE_RATE_NUMERATOR,
    RaydiumDynamicFeeError,
    RaydiumDynamicFeeSwapState,
    advance_dynamic_fee_index,
    compute_dynamic_fee_rate,
    compute_total_fee_rate,
    get_spacing_bounded_price,
    initialize_dynamic_fee_swap_state,
    tick_spacing_index_from_tick,
    update_dynamic_fee_reference,
    update_volatility_accumulator,
)


def info(**overrides):
    values = dict(
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
    values.update(overrides)
    return RaydiumClmmDynamicFeeInfo(**values)


@pytest.mark.parametrize("tick,spacing,expected", [
    (0, 10, 0), (1, 10, 0), (9, 10, 0), (10, 10, 1),
    (-1, 10, -1), (-9, 10, -1), (-10, 10, -1),
    (-11, 10, -2), (-20, 10, -2), (-21, 10, -3),
    (121, 60, 2), (-121, 60, -3),
])
def test_tick_spacing_index_matches_rust_truncation_and_negative_floor(tick, spacing, expected):
    assert tick_spacing_index_from_tick(tick, spacing) == expected


def test_dynamic_fee_matches_pinned_raydium_source_vector_36():
    state = info(volatility_accumulator=10_000)
    assert compute_dynamic_fee_rate(state, tick_spacing=60) == 36
    assert compute_total_fee_rate(
        base_fee_rate=1000, info=state, tick_spacing=60
    ) == 1036


def test_reference_filter_window_does_not_change_state():
    state = info(volatility_accumulator=80_000, last_update_timestamp=1000)
    result = update_dynamic_fee_reference(
        state, tick_spacing_index=7, current_timestamp=1059
    )
    assert result == state


def test_reference_decay_window_updates_reference_and_timestamp():
    state = info(volatility_accumulator=80_001, last_update_timestamp=1000)
    result = update_dynamic_fee_reference(
        state, tick_spacing_index=-4, current_timestamp=1060
    )
    assert result.tick_spacing_index_reference == -4
    assert result.volatility_reference == 40_000
    assert result.volatility_accumulator == 80_001
    assert result.last_update_timestamp == 1060


def test_reference_reset_window_clears_reference_but_preserves_accumulator():
    state = info(
        volatility_reference=50_000,
        volatility_accumulator=90_000,
        last_update_timestamp=1000,
    )
    result = update_dynamic_fee_reference(
        state, tick_spacing_index=12, current_timestamp=4600
    )
    assert result.tick_spacing_index_reference == 12
    assert result.volatility_reference == 0
    assert result.volatility_accumulator == 90_000
    assert result.last_update_timestamp == 4600


def test_reference_uses_saturating_elapsed_when_clock_is_behind():
    state = info(last_update_timestamp=2000)
    assert update_dynamic_fee_reference(
        state, tick_spacing_index=1, current_timestamp=1000
    ) == state


def test_volatility_accumulator_scales_and_caps_exactly():
    state = info(
        tick_spacing_index_reference=-2,
        volatility_reference=10_000,
        max_volatility_accumulator=25_000,
    )
    result = update_volatility_accumulator(state, tick_spacing_index=1)
    assert result.volatility_accumulator == 25_000


def test_fee_rate_uses_ceiling_and_caps_at_ten_percent():
    state = info(
        dynamic_fee_control=99_999,
        volatility_accumulator=100_000,
        max_volatility_accumulator=100_000,
    )
    assert compute_dynamic_fee_rate(state, tick_spacing=1) == MAX_FEE_RATE_NUMERATOR


def test_total_fee_caps_base_plus_dynamic_and_accepts_large_base_fee():
    state = info(volatility_accumulator=10_000)
    assert compute_total_fee_rate(
        base_fee_rate=999_999, info=state, tick_spacing=60
    ) == MAX_FEE_RATE_NUMERATOR


def test_disabled_dynamic_fee_returns_static_base_fee():
    assert compute_total_fee_rate(
        base_fee_rate=2500,
        info=RaydiumClmmDynamicFeeInfo(),
        tick_spacing=60,
    ) == 2500


def test_swap_state_initialization_applies_time_reference_update():
    state = info(
        volatility_accumulator=80_000,
        last_update_timestamp=1000,
    )
    result = initialize_dynamic_fee_swap_state(
        info=state,
        tick=-11,
        tick_spacing=10,
        base_fee_rate=1000,
        block_timestamp=1060,
    )
    assert result.tick_spacing_index == -2
    assert result.info.tick_spacing_index_reference == -2
    assert result.info.volatility_reference == 40_000


def test_advance_skipped_tick_spacing_updates_volatility_then_direction_index():
    state = RaydiumDynamicFeeSwapState(info(), 60, 1000, 0)
    result = advance_dynamic_fee_index(
        state,
        tick=0,
        tick_next=60,
        sqrt_price_x64=123,
        sqrt_price_next_x64=123,
        zero_for_one=True,
        is_skipped_tick_spacing=True,
    )
    assert result.info.volatility_accumulator == 10_000
    assert result.tick_spacing_index == 0


def test_advance_without_skipped_spacing_only_moves_direction_index():
    state = RaydiumDynamicFeeSwapState(info(), 60, 1000, 5)
    down = advance_dynamic_fee_index(
        state, tick=0, tick_next=60, sqrt_price_x64=1,
        sqrt_price_next_x64=2, zero_for_one=True,
        is_skipped_tick_spacing=False,
    )
    up = advance_dynamic_fee_index(
        state, tick=0, tick_next=60, sqrt_price_x64=1,
        sqrt_price_next_x64=2, zero_for_one=False,
        is_skipped_tick_spacing=False,
    )
    assert down.tick_spacing_index == 4
    assert up.tick_spacing_index == 6


def test_spacing_bound_returns_exact_tick_math_boundary():
    state = RaydiumDynamicFeeSwapState(info(), 60, 1000, 0)
    boundary = get_sqrt_price_at_tick(60)
    result = get_spacing_bounded_price(
        state, target_price_x64=get_sqrt_price_at_tick(120),
        zero_for_one=False, liquidity=1_000_000,
    )
    assert result.reached_target is False
    assert result.bounded_tick == 60
    assert result.sqrt_price_x64 == boundary


@pytest.mark.parametrize("bad_tick,bad_spacing", [
    (True, 10), (1, True), (1, 0), (1, 65536),
])
def test_rejects_invalid_tick_spacing_index_inputs(bad_tick, bad_spacing):
    with pytest.raises(RaydiumDynamicFeeError):
        tick_spacing_index_from_tick(bad_tick, bad_spacing)


def test_rejects_dynamic_fee_accumulator_product_outside_source_u32_bound():
    state = info(volatility_accumulator=100_000, max_volatility_accumulator=100_000)
    with pytest.raises(RaydiumDynamicFeeError, match="u32 bound"):
        compute_dynamic_fee_rate(state, tick_spacing=65535)
