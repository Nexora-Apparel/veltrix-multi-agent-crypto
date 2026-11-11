"""Golden and adversarial tests for pinned Raydium dynamic-fee state transitions."""
import pytest

from veltrix.raydium_clmm_pool_state import RaydiumClmmDynamicFeeInfo
from veltrix.raydium_clmm_dynamic_fee import (
    RaydiumDynamicFeeError,
    compute_dynamic_fee_rate,
    get_spacing_bounded_price,
    get_total_fee_rate,
    tick_spacing_index_from_tick,
    update_dynamic_fee_index,
    update_dynamic_fee_reference,
    update_volatility_accumulator,
)
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick


def dynamic_info(**overrides):
    values = dict(
        filter_period=10,
        decay_period=30,
        reduction_factor=5000,
        dynamic_fee_control=1000,
        max_volatility_accumulator=1_000_000,
        tick_spacing_index_reference=0,
        volatility_reference=0,
        volatility_accumulator=0,
        last_update_timestamp=100,
        padding=bytes(46),
    )
    values.update(overrides)
    return RaydiumClmmDynamicFeeInfo(**values)


@pytest.mark.parametrize("tick,spacing,expected", [
    (0, 10, 0), (1, 10, 0), (9, 10, 0), (10, 10, 1),
    (-1, 10, -1), (-5, 10, -1), (-10, 10, -1),
    (-11, 10, -2), (-21, 10, -3), (30, 60, 0),
    (-30, 60, -1), (-90, 60, -2), (-121, 60, -3),
])
def test_tick_spacing_index_matches_pinned_rust_truncation(tick, spacing, expected):
    assert tick_spacing_index_from_tick(tick, spacing) == expected


def test_dynamic_fee_reference_filter_decay_and_reset_windows():
    info = dynamic_info(
        tick_spacing_index_reference=5,
        volatility_reference=20_000,
        volatility_accumulator=50_000,
        last_update_timestamp=100,
    )
    assert update_dynamic_fee_reference(
        info, tick_spacing_index=8, current_timestamp=109
    ) == info
    decayed = update_dynamic_fee_reference(
        info, tick_spacing_index=8, current_timestamp=110
    )
    assert decayed.tick_spacing_index_reference == 8
    assert decayed.volatility_reference == 25_000
    assert decayed.last_update_timestamp == 110
    reset = update_dynamic_fee_reference(
        info, tick_spacing_index=9, current_timestamp=130
    )
    assert reset.tick_spacing_index_reference == 9
    assert reset.volatility_reference == 0
    assert reset.last_update_timestamp == 130


def test_dynamic_fee_reference_uses_saturating_elapsed_time():
    info = dynamic_info(last_update_timestamp=200, volatility_accumulator=5000)
    assert update_dynamic_fee_reference(
        info, tick_spacing_index=10, current_timestamp=100
    ) == info


def test_volatility_accumulator_scales_tick_distance_and_caps_at_maximum():
    info = dynamic_info(
        tick_spacing_index_reference=-2,
        volatility_reference=20_000,
        max_volatility_accumulator=50_000,
    )
    result = update_volatility_accumulator(info, tick_spacing_index=3)
    assert result.volatility_accumulator == 50_000


def test_dynamic_fee_rate_matches_pinned_quadratic_ceiling_vector():
    info = dynamic_info(volatility_accumulator=600_000)
    # crossed=600,000*10; ceil(1000*(6,000,000)^2 / 10^13) = 3,600
    assert compute_dynamic_fee_rate(info, tick_spacing=10) == 3_600
    assert get_total_fee_rate(1_000, dynamic_fee_info=info, tick_spacing=10) == 4_600


def test_dynamic_fee_rate_and_total_fee_are_capped_at_ten_percent():
    info = dynamic_info(
        dynamic_fee_control=99_999,
        max_volatility_accumulator=0xFFFFFFFF // 10,
        volatility_accumulator=0xFFFFFFFF // 10,
    )
    assert compute_dynamic_fee_rate(info, tick_spacing=10) == 100_000
    assert get_total_fee_rate(90_000, dynamic_fee_info=info, tick_spacing=10) == 100_000


def test_static_pool_uses_base_fee_without_dynamic_component():
    assert get_total_fee_rate(
        3000, dynamic_fee_info=RaydiumClmmDynamicFeeInfo(), tick_spacing=10
    ) == 3000


def test_dynamic_fee_index_advances_and_recomputes_on_skipped_spacing():
    info = dynamic_info(tick_spacing_index_reference=0, volatility_reference=10_000)
    updated, index = update_dynamic_fee_index(
        info, current_index=0, tick=25, tick_next=30,
        sqrt_price_x64=100, sqrt_price_next_x64=200,
        tick_spacing=10, zero_for_one=False, is_skipped_tick_spacing=True,
    )
    assert index == 3
    assert updated.volatility_accumulator == 30_000
    unchanged, index = update_dynamic_fee_index(
        info, current_index=3, tick=30, tick_next=40,
        sqrt_price_x64=200, sqrt_price_next_x64=200,
        tick_spacing=10, zero_for_one=True, is_skipped_tick_spacing=False,
    )
    assert index == 2
    assert unchanged == info


def test_spacing_bound_clamps_dynamic_fee_swaps_to_tick_spacing_group():
    info = dynamic_info(tick_spacing_index_reference=0, volatility_accumulator=10_000)
    bounded = get_spacing_bounded_price(
        target_price_x64=get_sqrt_price_at_tick(-100),
        tick_spacing_index=10, tick_spacing=10, liquidity=1000,
        dynamic_fee_info=info, zero_for_one=True,
    )
    assert bounded.fully_reached_target is False
    assert bounded.bounded_tick == 100
    assert bounded.target_sqrt_price_x64 == get_sqrt_price_at_tick(100)

    unbounded = get_spacing_bounded_price(
        target_price_x64=get_sqrt_price_at_tick(200),
        tick_spacing_index=10, tick_spacing=10, liquidity=1000,
        dynamic_fee_info=info, zero_for_one=True,
    )
    assert unbounded.bounded_tick is None
    assert unbounded.target_sqrt_price_x64 == get_sqrt_price_at_tick(200)


def test_spacing_bound_is_disabled_at_max_volatility_or_zero_liquidity():
    info = dynamic_info(
        max_volatility_accumulator=1000, volatility_accumulator=1000
    )
    result = get_spacing_bounded_price(
        target_price_x64=get_sqrt_price_at_tick(50),
        tick_spacing_index=1, tick_spacing=10, liquidity=100,
        dynamic_fee_info=info, zero_for_one=False,
    )
    assert result.fully_reached_target is True
    assert result.bounded_tick is None


def test_rejects_invalid_dynamic_fee_arguments():
    with pytest.raises(RaydiumDynamicFeeError, match="tick_spacing"):
        tick_spacing_index_from_tick(1, 0)
    with pytest.raises(RaydiumDynamicFeeError, match="tick_index"):
        tick_spacing_index_from_tick(1 << 31, 10)
    with pytest.raises(RaydiumDynamicFeeError, match="fee denominator"):
        get_total_fee_rate(
            1_000_000, dynamic_fee_info=RaydiumClmmDynamicFeeInfo(), tick_spacing=10
        )
