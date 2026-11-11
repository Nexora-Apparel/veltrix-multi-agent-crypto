from solders.pubkey import Pubkey

from veltrix.raydium_clmm_dynamic_fee import RaydiumDynamicFeeSwapState
from veltrix.raydium_clmm_fee_accounting import RaydiumFeeAccountingState
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState, RaydiumClmmDynamicFeeInfo
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick
from veltrix.raydium_clmm_swap_inner_iteration import run_raydium_clmm_swap_inner_iteration


def key():
    return str(Pubkey.new_unique())


def pool(*, fee_on=0, dynamic=None):
    return RaydiumClmmPoolState(
        pool_address=key(),
        bump=1,
        amm_config=key(),
        pool_creator=key(),
        token_mint_0=key(),
        token_mint_1=key(),
        token_vault_0=key(),
        token_vault_1=key(),
        observation_key=key(),
        mint_decimals_0=6,
        mint_decimals_1=6,
        tick_spacing=10,
        liquidity=1000,
        sqrt_price_x64=1 << 64,
        tick_current=0,
        status=0,
        fee_on=fee_on,
        seed_index=0,
        account_data_sha256="0" * 64,
        fee_growth_global_0_x64=10,
        fee_growth_global_1_x64=20,
        dynamic_fee_info=dynamic if dynamic is not None else RaydiumClmmDynamicFeeInfo(),
    )


def tick(**overrides):
    values = dict(
        tick_index=10,
        liquidity_net=100,
        liquidity_gross=200,
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


def fee_state(amount, liquidity=1000):
    return RaydiumFeeAccountingState(
        amount_specified_remaining=amount,
        amount_calculated=0,
        liquidity=liquidity,
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


def test_inner_iteration_composes_price_fee_crossing_and_tick_finalization():
    p = pool()
    result = run_raydium_clmm_swap_inner_iteration(
        p,
        fee_state(1_000_000),
        current_tick=0,
        next_tick=10,
        next_tick_state=tick(),
        sqrt_price_current_x64=get_sqrt_price_at_tick(0),
        sqrt_price_next_x64=get_sqrt_price_at_tick(10),
        target_price_x64=get_sqrt_price_at_tick(10),
        base_fee_rate=0,
        protocol_fee_rate=0,
        fund_fee_rate=0,
        zero_for_one=False,
        is_base_input=True,
    )
    assert result.reached_initialized_tick is True
    assert result.crossed_tick is True
    assert result.liquidity_next == 1100
    assert result.fee_state.liquidity == 1100
    assert result.tick == 10
    assert result.should_break is True


def test_inner_iteration_matches_fifo_orders_before_crossing_and_obeys_directional_tick_rule():
    p = pool(fee_on=0)
    order_tick = tick(
        liquidity_gross=0,
        liquidity_net=0,
        orders_amount=1000,
    )
    result = run_raydium_clmm_swap_inner_iteration(
        p,
        fee_state(100),
        current_tick=10,
        next_tick=10,
        next_tick_state=order_tick,
        sqrt_price_current_x64=get_sqrt_price_at_tick(10),
        sqrt_price_next_x64=get_sqrt_price_at_tick(10),
        target_price_x64=get_sqrt_price_at_tick(10),
        base_fee_rate=3000,
        protocol_fee_rate=0,
        fund_fee_rate=0,
        zero_for_one=True,
        is_base_input=True,
    )
    assert result.reached_initialized_tick is True
    assert result.crossed_tick is False
    assert result.limit_order_match.amount_out == 99
    assert result.unfilled_amount_before == 1000
    assert result.unfilled_amount_after == 901
    assert result.tick_state.part_filled_orders_remaining == 901
    assert result.tick == 10
    assert result.fee_state.amount_specified_remaining == 0
    assert result.should_break is True


def test_inner_iteration_continues_after_spacing_bound_and_advances_dynamic_index():
    p = pool(dynamic=dynamic_state().info)
    result = run_raydium_clmm_swap_inner_iteration(
        p,
        fee_state(1_000_000),
        current_tick=0,
        next_tick=20,
        next_tick_state=tick(tick_index=20),
        sqrt_price_current_x64=get_sqrt_price_at_tick(0),
        sqrt_price_next_x64=get_sqrt_price_at_tick(20),
        target_price_x64=get_sqrt_price_at_tick(20),
        base_fee_rate=1000,
        protocol_fee_rate=0,
        fund_fee_rate=0,
        zero_for_one=False,
        is_base_input=True,
        dynamic_fee_state=dynamic_state(),
    )
    assert result.reached_initialized_tick is False
    assert result.crossed_tick is False
    assert result.tick == 10
    assert result.sqrt_price_x64 == get_sqrt_price_at_tick(10)
    assert result.dynamic_fee_state.tick_spacing_index == 1
    assert result.should_break is False


def test_inner_iteration_breaks_on_no_price_change_at_price_limit():
    p = pool()
    result = run_raydium_clmm_swap_inner_iteration(
        p,
        fee_state(100),
        current_tick=5,
        next_tick=10,
        next_tick_state=tick(),
        sqrt_price_current_x64=get_sqrt_price_at_tick(5),
        sqrt_price_next_x64=get_sqrt_price_at_tick(10),
        target_price_x64=get_sqrt_price_at_tick(5),
        base_fee_rate=0,
        protocol_fee_rate=0,
        fund_fee_rate=0,
        zero_for_one=False,
        is_base_input=True,
    )
    assert result.reached_initialized_tick is False
    assert result.tick == 5
    assert result.sqrt_price_x64 == get_sqrt_price_at_tick(5)
    assert result.should_break is True
