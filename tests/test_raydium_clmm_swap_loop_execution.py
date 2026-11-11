"""Integration tests for the bounded Raydium CLMM outer swap loop."""
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_bitmap_traversal import RaydiumClmmBitmapTraversalResult
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState, RaydiumClmmRewardInfo
from veltrix.raydium_clmm_swap_loop_execution import execute_raydium_swap_loop
from veltrix.raydium_clmm_swap_loop_state import initialize_raydium_swap_loop_state
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    derive_raydium_clmm_tick_array_address,
)
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick


def make_fixture(initialized_ticks=(10, 20), amount=1_000_000):
    pool = RaydiumClmmPoolState(
        pool_address=str(Pubkey.new_unique()), bump=1,
        amm_config=str(Pubkey.new_unique()), pool_creator=str(Pubkey.new_unique()),
        token_mint_0=str(Pubkey.new_unique()), token_mint_1=str(Pubkey.new_unique()),
        token_vault_0=str(Pubkey.new_unique()), token_vault_1=str(Pubkey.new_unique()),
        observation_key=str(Pubkey.new_unique()), mint_decimals_0=6,
        mint_decimals_1=6, tick_spacing=1, liquidity=10_000,
        sqrt_price_x64=get_sqrt_price_at_tick(0), tick_current=0,
        status=0, fee_on=0, seed_index=0, account_data_sha256="a" * 64,
        tick_array_bitmap=(0,) * 8 + (1,) + (0,) * 7,
    )
    initialized = set(initialized_ticks)
    ticks = tuple(
        RaydiumClmmTick(
            tick_index=index,
            liquidity_net=0,
            liquidity_gross=1_000 if index in initialized else 0,
            orders_amount=0,
            part_filled_orders_remaining=0,
            initialized=index in initialized,
        )
        for index in range(RAYDIUM_TICK_ARRAY_SIZE)
    )
    array = RaydiumClmmTickArray(
        address=derive_raydium_clmm_tick_array_address(
            pool_address=pool.pool_address, start_tick_index=0,
        ),
        pool_address=pool.pool_address,
        start_tick_index=0,
        tick_spacing=1,
        initialized_tick_count=len(initialized),
        recent_epoch=1,
        ticks=ticks,
        data_sha256="b" * 64,
    )
    traversal = RaydiumClmmBitmapTraversalResult(
        passed=True,
        pool_address=pool.pool_address,
        zero_for_one=False,
        current_array_start_index=0,
        first_initialized_array_start_index=0,
        last_cpi_array_start_index=0,
        expected_initialized_array_starts=(0,),
        supplied_initialized_array_starts=(0,),
        supplied_array_starts=(0,),
        bitmap_extension_sha256=None,
        bounded_interval_complete=True,
        execution_ready=False,
    )
    state = initialize_raydium_swap_loop_state(
        pool,
        amount_specified=amount,
        trade_fee_rate=0,
        zero_for_one=False,
        is_base_input=True,
        block_timestamp=100,
    )
    return pool, array, traversal, state


def execute(pool, array, traversal, state, *, limit_tick, max_iterations=20):
    return execute_raydium_swap_loop(
        state,
        tick_arrays=(array,),
        traversal=traversal,
        sqrt_price_limit_x64=get_sqrt_price_at_tick(limit_tick),
        first_tick_array_contains_pool_tick=True,
        updated_reward_infos=(RaydiumClmmRewardInfo(),) * 3,
        recent_epoch=2,
        protocol_fee_rate=0,
        fund_fee_rate=0,
        max_iterations=max_iterations,
    )


def test_outer_loop_crosses_multiple_ticks_in_one_array_with_mutation_lineage():
    pool, array, traversal, state = make_fixture()
    result = execute(pool, array, traversal, state, limit_tick=20)
    assert result.complete is True
    assert result.termination_reason == "price_limit_reached"
    assert result.iterations == 2
    assert result.boundary_transitions == 2
    assert result.state.tick == 20
    assert result.tick_arrays[0].initialized_tick_count == 2
    assert result.tick_arrays[0].data_sha256 == ""
    assert result.mutation is not None
    assert len(result.array_mutations) == 1


def test_outer_loop_stops_when_exact_input_is_consumed_before_price_limit():
    pool, array, traversal, state = make_fixture(amount=1)
    result = execute(pool, array, traversal, state, limit_tick=20)
    assert result.complete is True
    assert result.termination_reason == "amount_fully_consumed"
    assert result.iterations == 1
    assert result.boundary_transitions == 0


def test_outer_loop_iteration_cap_is_fail_closed_and_reports_incomplete_state():
    pool, array, traversal, state = make_fixture(amount=1_000_000)
    result = execute(pool, array, traversal, state, limit_tick=20, max_iterations=1)
    assert result.complete is False
    assert result.termination_reason == "iteration_limit"
    assert result.iterations == 1
