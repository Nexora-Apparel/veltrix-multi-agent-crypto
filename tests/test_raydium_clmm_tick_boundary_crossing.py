import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_pool_state import (
    RaydiumClmmPoolState,
    RaydiumClmmRewardInfo,
)
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTick
from veltrix.raydium_clmm_tick_boundary_crossing import (
    RaydiumTickBoundaryCrossingError,
    cross_raydium_clmm_tick_at_boundary,
)


def key():
    return str(Pubkey.new_unique())


def pool(*, fee_on=0, rewards=None):
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
        fee_growth_global_0_x64=30,
        fee_growth_global_1_x64=50,
        reward_infos=rewards if rewards is not None else (RaydiumClmmRewardInfo(),) * 3,
    )


def tick(**overrides):
    values = dict(
        tick_index=10,
        liquidity_net=100,
        liquidity_gross=200,
        orders_amount=0,
        part_filled_orders_remaining=0,
        initialized=True,
        fee_growth_outside_0_x64=10,
        fee_growth_outside_1_x64=20,
        reward_growths_outside_x64=(5, 11, 17),
        order_phase=0,
        unfilled_ratio_x64=0,
        padding=(0, 0, 0),
    )
    values.update(overrides)
    return RaydiumClmmTick(**values)


def rewards():
    return (
        RaydiumClmmRewardInfo(token_mint=key(), reward_growth_global_x64=80),
        RaydiumClmmRewardInfo(),
        RaydiumClmmRewardInfo(token_mint=key(), reward_growth_global_x64=120),
    )


def test_zero_for_one_crossing_inverts_fee_and_initialized_reward_growth_and_subtracts_liquidity():
    result = cross_raydium_clmm_tick_at_boundary(
        pool(rewards=rewards()),
        tick(),
        current_liquidity=1000,
        fee_growth_global_x64=100,
        zero_for_one=True,
    )
    assert result.crossed is True
    assert result.liquidity_net == 100
    assert result.liquidity_after == 900
    assert result.tick.fee_growth_outside_0_x64 == 90
    assert result.tick.fee_growth_outside_1_x64 == 30
    assert result.tick.reward_growths_outside_x64 == (75, 11, 103)


def test_one_for_zero_crossing_adds_liquidity_and_uses_token1_fee_growth_when_configured():
    result = cross_raydium_clmm_tick_at_boundary(
        pool(fee_on=2, rewards=rewards()),
        tick(liquidity_net=-100),
        current_liquidity=1000,
        fee_growth_global_x64=100,
        zero_for_one=False,
    )
    assert result.liquidity_after == 900
    assert result.tick.fee_growth_outside_0_x64 == 20
    assert result.tick.fee_growth_outside_1_x64 == 80


@pytest.mark.parametrize("fee_on,zero_for_one,expected0,expected1", [
    (0, True, 100, 50),
    (0, False, 30, 100),
    (1, True, 100, 50),
    (1, False, 100, 50),
    (2, True, 30, 100),
    (2, False, 30, 100),
])
def test_fee_growth_is_assigned_to_exact_fee_on_token_side(fee_on, zero_for_one, expected0, expected1):
    result = cross_raydium_clmm_tick_at_boundary(
        pool(fee_on=fee_on),
        tick(),
        current_liquidity=1000,
        fee_growth_global_x64=100,
        zero_for_one=zero_for_one,
    )
    assert result.tick.fee_growth_outside_0_x64 == expected0 - 10
    assert result.tick.fee_growth_outside_1_x64 == expected1 - 20


def test_tick_with_outstanding_limit_orders_is_not_crossed():
    original = tick(orders_amount=10)
    result = cross_raydium_clmm_tick_at_boundary(
        pool(),
        original,
        current_liquidity=1000,
        fee_growth_global_x64=100,
        zero_for_one=True,
    )
    assert result.crossed is False
    assert result.tick is original
    assert result.liquidity_after == 1000


def test_liquidity_crossing_underflow_fails_closed():
    with pytest.raises(RaydiumTickBoundaryCrossingError, match="failed"):
        cross_raydium_clmm_tick_at_boundary(
            pool(),
            tick(),
            current_liquidity=50,
            fee_growth_global_x64=100,
            zero_for_one=True,
        )
