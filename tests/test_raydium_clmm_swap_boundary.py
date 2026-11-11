"""Source-order tests for the Raydium initialized-tick boundary transition."""
from dataclasses import replace

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_pool_state import (
    RaydiumClmmPoolState,
    RaydiumClmmRewardInfo,
)
from veltrix.raydium_clmm_swap_boundary import (
    RaydiumSwapBoundaryError,
    apply_raydium_swap_boundary,
)
from veltrix.raydium_clmm_swap_loop_state import (
    RaydiumPreparedSwapStep,
    initialize_raydium_swap_loop_state,
    set_raydium_swap_loop_target,
)
from veltrix.raydium_clmm_swap_step_math import RaydiumSwapStepResult
from veltrix.raydium_clmm_tick_array_mutation import apply_raydium_tick_array_mutation
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    derive_raydium_clmm_tick_array_address,
)
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick


def pool(*, zero_for_one=False):
    return RaydiumClmmPoolState(
        pool_address=str(Pubkey.new_unique()), bump=1,
        amm_config=str(Pubkey.new_unique()), pool_creator=str(Pubkey.new_unique()),
        token_mint_0=str(Pubkey.new_unique()), token_mint_1=str(Pubkey.new_unique()),
        token_vault_0=str(Pubkey.new_unique()), token_vault_1=str(Pubkey.new_unique()),
        observation_key=str(Pubkey.new_unique()), mint_decimals_0=6,
        mint_decimals_1=6, tick_spacing=10, liquidity=10_000,
        sqrt_price_x64=get_sqrt_price_at_tick(10 if zero_for_one else 0),
        tick_current=10 if zero_for_one else 0, status=0, fee_on=0,
        seed_index=0, account_data_sha256="a" * 64,
        fee_growth_global_0_x64=7, fee_growth_global_1_x64=11,
        tick_array_bitmap=(0,) * 8 + (1,) + (0,) * 7,
    )


def setup(*, zero_for_one=False, orders=0):
    p = pool(zero_for_one=zero_for_one)
    tick_index = 0 if zero_for_one else 10
    slot = tick_index // p.tick_spacing
    ticks = [
        RaydiumClmmTick(
            tick_index=i * p.tick_spacing, liquidity_net=0, liquidity_gross=0,
            orders_amount=0, part_filled_orders_remaining=0, initialized=False,
        )
        for i in range(RAYDIUM_TICK_ARRAY_SIZE)
    ]
    ticks[slot] = RaydiumClmmTick(
        tick_index=tick_index, liquidity_net=100, liquidity_gross=1000,
        orders_amount=orders, part_filled_orders_remaining=0, initialized=True,
        fee_growth_outside_0_x64=2, fee_growth_outside_1_x64=3,
        reward_growths_outside_x64=(4, 5, 6),
    )
    array = RaydiumClmmTickArray(
        address=derive_raydium_clmm_tick_array_address(
            pool_address=p.pool_address, start_tick_index=0,
        ),
        pool_address=p.pool_address, start_tick_index=0, tick_spacing=10,
        initialized_tick_count=1, recent_epoch=1, ticks=tuple(ticks),
        data_sha256="b" * 64,
    )
    state = initialize_raydium_swap_loop_state(
        p, amount_specified=100, trade_fee_rate=3000,
        zero_for_one=zero_for_one, is_base_input=True, block_timestamp=100,
    )
    next_tick = ticks[slot]
    target = set_raydium_swap_loop_target(
        state,
        next_tick=tick_index,
        sqrt_price_limit_x64=get_sqrt_price_at_tick(-10 if zero_for_one else 20),
    )
    prepared = RaydiumPreparedSwapStep(
        state=target,
        target_price_x64=target.target_price_x64,
        bounded_price_x64=target.sqrt_price_next_x64,
        bounded_tick=None,
        is_skipped_tick_spacing=True,
        total_fee_rate=3000,
        result=RaydiumSwapStepResult(
            sqrt_price_next_x64=target.sqrt_price_next_x64,
            amount_in=90, amount_out=80, fee_amount=1, reached_target=True,
        ),
    )
    return p, array, next_tick, prepared


def transition(prepared, array, tick):
    return apply_raydium_swap_boundary(
        prepared, tick_array=array, next_tick=tick, recent_epoch=2,
        updated_reward_infos=(RaydiumClmmRewardInfo(),) * 3,
        protocol_fee_rate=0, fund_fee_rate=0,
    )


@pytest.mark.parametrize("zero_for_one,expected_liquidity", [(False, 10_100), (True, 9_900)])
def test_boundary_crosses_liquidity_after_fifo_and_uses_directional_delta(
    zero_for_one, expected_liquidity,
):
    _, array, tick, prepared = setup(zero_for_one=zero_for_one)
    result = transition(prepared, array, tick)
    assert result.crossed_liquidity is True
    assert result.limit_order_unfilled_before == 0
    assert result.limit_order_unfilled_after == 0
    assert result.state.fee_state.liquidity == expected_liquidity
    assert result.mutation.tick_array.ticks[tick.tick_index // 10].fee_growth_outside_0_x64 != 2
    assert result.state.tick == (tick.tick_index - 1 if zero_for_one else tick.tick_index)
    assert result.mutation.tick_array.initialized_tick_count == 1


def test_outstanding_fifo_orders_prevent_liquidity_crossing_and_set_tick_side():
    _, array, tick, prepared = setup(orders=1000)
    result = transition(prepared, array, tick)
    assert result.limit_order_unfilled_before == 1000
    assert 0 < result.limit_order_unfilled_after < 1000
    assert result.crossed_liquidity is False
    assert result.state.fee_state.liquidity == 10_000
    assert result.state.tick == tick.tick_index - 1
    updated_tick = result.mutation.tick_array.ticks[1]
    assert updated_tick.orders_amount == 0
    assert updated_tick.part_filled_orders_remaining > 0


def test_exhausting_fifo_orders_allows_liquidity_crossing_in_same_boundary():
    _, array, tick, prepared = setup(orders=1)
    result = transition(prepared, array, tick)
    assert result.limit_order_unfilled_before == 1
    assert result.limit_order_unfilled_after == 0
    assert result.crossed_liquidity is True
    assert result.state.fee_state.liquidity == 10_100


def test_boundary_rejects_step_that_did_not_reach_selected_tick():
    _, array, tick, prepared = setup()
    wrong = replace(
        prepared,
        result=replace(
            prepared.result,
            sqrt_price_next_x64=prepared.state.sqrt_price_x64,
        ),
    )
    with pytest.raises(RaydiumSwapBoundaryError, match="did not reach"):
        transition(wrong, array, tick)


def test_boundary_rejects_tick_that_does_not_match_prepared_target():
    _, array, tick, prepared = setup()
    wrong_tick = replace(tick, tick_index=tick.tick_index + 10)
    with pytest.raises(RaydiumSwapBoundaryError, match="differs from"):
        transition(prepared, array, wrong_tick)
