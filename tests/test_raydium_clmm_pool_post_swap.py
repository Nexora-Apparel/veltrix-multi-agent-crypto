"""Source-vector and adversarial tests for Raydium PoolState post-swap persistence."""
import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_pool_state import (
    RaydiumClmmDynamicFeeInfo,
    RaydiumClmmPoolState,
)
from veltrix.raydium_clmm_pool_post_swap import (
    RaydiumPoolPostSwapError,
    apply_raydium_pool_after_swap,
)

U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1


def pool(**overrides):
    values = dict(
        pool_address=str(Pubkey.new_unique()), bump=1,
        amm_config=str(Pubkey.new_unique()), pool_creator=str(Pubkey.new_unique()),
        token_mint_0=str(Pubkey.new_unique()), token_mint_1=str(Pubkey.new_unique()),
        token_vault_0=str(Pubkey.new_unique()), token_vault_1=str(Pubkey.new_unique()),
        observation_key=str(Pubkey.new_unique()), mint_decimals_0=6,
        mint_decimals_1=9, tick_spacing=10, liquidity=1000,
        sqrt_price_x64=1 << 64, tick_current=0, status=0,
        fee_on=0, seed_index=0, account_data_sha256="a" * 64,
    )
    values.update(overrides)
    return RaydiumClmmPoolState(**values)


def apply(p, **overrides):
    values = dict(
        tick_current=5, sqrt_price_x64=(1 << 64) + 10,
        liquidity=2000, lp_fee=100, protocol_fee=20,
        fund_fee=10, fee_growth_global_x64=777,
        zero_for_one=True, dynamic_fee_info=None,
    )
    values.update(overrides)
    return apply_raydium_pool_after_swap(p, **values)


def test_fee_on_input_mode_zero_routes_zero_for_one_fees_to_token0():
    result = apply(pool(fee_on=0))
    assert result.fee_growth_global_0_x64 == 777
    assert result.fee_growth_global_1_x64 == 0
    assert result.protocol_fees_token_0 == 20
    assert result.fund_fees_token_0 == 10
    assert result.protocol_fees_token_1 == 0
    assert result.fund_fees_token_1 == 0


def test_fee_on_input_mode_zero_routes_one_for_zero_fees_to_token1():
    result = apply(pool(fee_on=0), zero_for_one=False)
    assert result.fee_growth_global_0_x64 == 0
    assert result.fee_growth_global_1_x64 == 777
    assert result.protocol_fees_token_1 == 20
    assert result.fund_fees_token_1 == 10


def test_token0_only_and_token1_only_modes_ignore_swap_direction():
    token0 = apply(pool(fee_on=1), zero_for_one=False)
    token1 = apply(pool(fee_on=2), zero_for_one=True)
    assert token0.protocol_fees_token_0 == 20
    assert token0.fund_fees_token_0 == 10
    assert token0.fee_growth_global_0_x64 == 777
    assert token1.protocol_fees_token_1 == 20
    assert token1.fund_fees_token_1 == 10
    assert token1.fee_growth_global_1_x64 == 777


def test_persists_swap_tick_price_liquidity_and_only_updates_lp_growth_when_lp_fee_positive():
    original = pool(fee_growth_global_0_x64=123)
    result = apply(original, lp_fee=0, protocol_fee=0, fund_fee=0)
    assert result.tick_current == 5
    assert result.sqrt_price_x64 == (1 << 64) + 10
    assert result.liquidity == 2000
    assert result.fee_growth_global_0_x64 == 123


def test_dynamic_fee_state_updates_only_mutable_fields_when_pool_has_dynamic_fees():
    original = pool(dynamic_fee_info=RaydiumClmmDynamicFeeInfo(
        filter_period=60, decay_period=3600, reduction_factor=5000,
        dynamic_fee_control=1000, max_volatility_accumulator=1_000_000,
        tick_spacing_index_reference=1, volatility_reference=2,
        volatility_accumulator=3, last_update_timestamp=4,
    ))
    incoming = RaydiumClmmDynamicFeeInfo(
        filter_period=999, decay_period=9999, reduction_factor=4000,
        dynamic_fee_control=2000, max_volatility_accumulator=900_000,
        tick_spacing_index_reference=-10, volatility_reference=20,
        volatility_accumulator=30, last_update_timestamp=40,
    )
    result = apply(original, dynamic_fee_info=incoming)
    assert result.dynamic_fee_info.filter_period == 60
    assert result.dynamic_fee_info.decay_period == 3600
    assert result.dynamic_fee_info.reduction_factor == 5000
    assert result.dynamic_fee_info.dynamic_fee_control == 1000
    assert result.dynamic_fee_info.tick_spacing_index_reference == -10
    assert result.dynamic_fee_info.volatility_reference == 20
    assert result.dynamic_fee_info.volatility_accumulator == 30
    assert result.dynamic_fee_info.last_update_timestamp == 40


def test_disabled_dynamic_fee_pool_ignores_supplied_dynamic_state_like_upstream():
    original = pool(dynamic_fee_info=RaydiumClmmDynamicFeeInfo())
    incoming = RaydiumClmmDynamicFeeInfo(
        filter_period=60, decay_period=3600, reduction_factor=5000,
        dynamic_fee_control=1000, max_volatility_accumulator=100_000,
    )
    assert apply(original, dynamic_fee_info=incoming).dynamic_fee_info == original.dynamic_fee_info


@pytest.mark.parametrize("field", ["protocol_fees_token_0", "fund_fees_token_0"])
def test_rejects_fee_balance_overflow(field):
    with pytest.raises(RaydiumPoolPostSwapError, match="overflows u64"):
        apply(pool(**{field: U64_MAX}))


def test_rejects_invalid_fee_mode_and_dynamic_fee_state_bounds():
    with pytest.raises(RaydiumPoolPostSwapError, match="fee_on"):
        apply(pool(fee_on=3))
    original = pool(dynamic_fee_info=RaydiumClmmDynamicFeeInfo(
        filter_period=60, decay_period=3600, reduction_factor=5000,
        dynamic_fee_control=1000, max_volatility_accumulator=100,
    ))
    incoming = RaydiumClmmDynamicFeeInfo(
        filter_period=60, decay_period=3600, reduction_factor=5000,
        dynamic_fee_control=1000, max_volatility_accumulator=100,
        volatility_reference=101, volatility_accumulator=1,
    )
    with pytest.raises(RaydiumPoolPostSwapError, match="configured bounds"):
        apply(original, dynamic_fee_info=incoming)


def test_rejects_invalid_price_liquidity_and_integer_boolean_values():
    with pytest.raises(RaydiumPoolPostSwapError, match="sqrt_price_x64"):
        apply(pool(), sqrt_price_x64=True)
    with pytest.raises(RaydiumPoolPostSwapError, match="liquidity"):
        apply(pool(), liquidity=-1)
    with pytest.raises(RaydiumPoolPostSwapError, match="zero_for_one"):
        apply(pool(), zero_for_one=1)
