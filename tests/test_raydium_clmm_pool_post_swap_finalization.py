import pytest

from veltrix.raydium_clmm_dynamic_fee import RaydiumDynamicFeeSwapState
from veltrix.raydium_clmm_fee_accounting import RaydiumFeeAccountingState
from veltrix.raydium_clmm_limit_orders import RaydiumLimitOrderMatch
from veltrix.raydium_clmm_pool_state import (
    RaydiumClmmDynamicFeeInfo,
    RaydiumClmmPoolState,
)
from veltrix.raydium_clmm_swap_inner_iteration import RaydiumClmmSwapInnerIterationResult
from veltrix.raydium_clmm_pool_post_swap_finalization import (
    RaydiumPoolPostSwapFinalizationError,
    finalize_raydium_pool_after_swap,
)
from solders.pubkey import Pubkey


def key():
    return str(Pubkey.new_unique())


def pool(*, fee_on=0, dynamic=False, **overrides):
    info = RaydiumClmmDynamicFeeInfo(
        filter_period=60 if dynamic else 0,
        decay_period=3600 if dynamic else 0,
        reduction_factor=5000 if dynamic else 0,
        dynamic_fee_control=1000 if dynamic else 0,
        max_volatility_accumulator=100000 if dynamic else 0,
        tick_spacing_index_reference=0,
        volatility_reference=0,
        volatility_accumulator=0,
        last_update_timestamp=1000,
        padding=bytes(46),
    )
    values = dict(
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
        liquidity=500,
        sqrt_price_x64=1 << 64,
        tick_current=0,
        status=0,
        fee_on=fee_on,
        seed_index=0,
        account_data_sha256="a" * 64,
        fee_growth_global_0_x64=7,
        fee_growth_global_1_x64=9,
        protocol_fees_token_0=10,
        protocol_fees_token_1=20,
        fund_fees_token_0=30,
        fund_fees_token_1=40,
        dynamic_fee_info=info,
    )
    values.update(overrides)
    return RaydiumClmmPoolState(**values)


def dynamic_state():
    info = RaydiumClmmDynamicFeeInfo(
        filter_period=60,
        decay_period=3600,
        reduction_factor=5000,
        dynamic_fee_control=1000,
        max_volatility_accumulator=100000,
        tick_spacing_index_reference=3,
        volatility_reference=400,
        volatility_accumulator=700,
        last_update_timestamp=1200,
        padding=bytes(46),
    )
    return RaydiumDynamicFeeSwapState(info, 10, 1000, 3)


def iteration(*, fee_state=None, tick=12, price=1 << 65, dynamic=None, should_break=True):
    return RaydiumClmmSwapInnerIterationResult(
        fee_state=fee_state or RaydiumFeeAccountingState(
            amount_specified_remaining=0,
            amount_calculated=100,
            liquidity=900,
            fee_growth_global_x64=111,
            lp_fee=2,
            protocol_fee=3,
            fund_fee=4,
        ),
        tick_state=None,
        tick=tick,
        sqrt_price_x64=price,
        dynamic_fee_state=dynamic,
        liquidity_next=900,
        price_step=None,
        limit_order_match=RaydiumLimitOrderMatch(),
        unfilled_amount_before=0,
        unfilled_amount_after=0,
        reached_initialized_tick=False,
        crossed_tick=False,
        should_break=should_break,
    )


@pytest.mark.parametrize(
    "fee_on,zero_for_one,expected_token0",
    [
        (0, True, True),
        (0, False, False),
        (1, True, True),
        (1, False, True),
        (2, True, False),
        (2, False, False),
    ],
)
def test_post_swap_fee_updates_follow_exact_collect_fee_on_mode(fee_on, zero_for_one, expected_token0):
    p = pool(fee_on=fee_on)
    result = finalize_raydium_pool_after_swap(p, iteration(), zero_for_one=zero_for_one)
    updated = result.pool
    assert result.fee_token0 is expected_token0
    assert updated.tick_current == 12
    assert updated.sqrt_price_x64 == 1 << 65
    assert updated.liquidity == 900
    if expected_token0:
        assert updated.fee_growth_global_0_x64 == 111
        assert updated.fee_growth_global_1_x64 == 9
        assert updated.protocol_fees_token_0 == 13
        assert updated.protocol_fees_token_1 == 20
        assert updated.fund_fees_token_0 == 34
        assert updated.fund_fees_token_1 == 40
    else:
        assert updated.fee_growth_global_0_x64 == 7
        assert updated.fee_growth_global_1_x64 == 111
        assert updated.protocol_fees_token_0 == 10
        assert updated.protocol_fees_token_1 == 23
        assert updated.fund_fees_token_0 == 30
        assert updated.fund_fees_token_1 == 44


def test_zero_lp_fee_does_not_overwrite_pool_fee_growth():
    state = RaydiumFeeAccountingState(
        amount_specified_remaining=0,
        amount_calculated=100,
        liquidity=900,
        fee_growth_global_x64=999,
        lp_fee=0,
        protocol_fee=1,
        fund_fee=0,
    )
    result = finalize_raydium_pool_after_swap(
        pool(fee_on=0), iteration(fee_state=state), zero_for_one=True
    )
    assert result.pool.fee_growth_global_0_x64 == 7
    assert result.pool.protocol_fees_token_0 == 11


def test_dynamic_fee_variables_copy_only_mutable_values_and_preserve_configuration():
    p = pool(dynamic=True)
    result = finalize_raydium_pool_after_swap(
        p, iteration(dynamic=dynamic_state()), zero_for_one=True
    )
    updated = result.pool.dynamic_fee_info
    assert updated.filter_period == 60
    assert updated.decay_period == 3600
    assert updated.reduction_factor == 5000
    assert updated.dynamic_fee_control == 1000
    assert updated.max_volatility_accumulator == 100000
    assert updated.tick_spacing_index_reference == 3
    assert updated.volatility_reference == 400
    assert updated.volatility_accumulator == 700
    assert updated.last_update_timestamp == 1200


def test_finalization_rejects_unterminated_loop_invalid_fee_mode_and_overflow():
    with pytest.raises(RaydiumPoolPostSwapFinalizationError, match="terminated"):
        finalize_raydium_pool_after_swap(
            pool(), iteration(should_break=False), zero_for_one=True
        )
    with pytest.raises(RaydiumPoolPostSwapFinalizationError, match="fee_on"):
        finalize_raydium_pool_after_swap(
            pool(fee_on=3), iteration(), zero_for_one=True
        )
    overflowing = RaydiumFeeAccountingState(
        amount_specified_remaining=0,
        amount_calculated=0,
        liquidity=900,
        fee_growth_global_x64=1,
        lp_fee=1,
        protocol_fee=1,
        fund_fee=0,
    )
    with pytest.raises(RaydiumPoolPostSwapFinalizationError, match="overflows"):
        finalize_raydium_pool_after_swap(
            pool(protocol_fees_token_0=(1 << 64) - 1),
            iteration(fee_state=overflowing),
            zero_for_one=True,
        )


def test_dynamic_fee_state_cannot_be_applied_to_disabled_pool():
    with pytest.raises(RaydiumPoolPostSwapFinalizationError, match="disabled"):
        finalize_raydium_pool_after_swap(
            pool(), iteration(dynamic=dynamic_state()), zero_for_one=True
        )
