import hashlib
from dataclasses import replace

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_fee_accounting import RaydiumFeeAccountingState
from veltrix.raydium_clmm_limit_orders import RaydiumLimitOrderMatch
from veltrix.raydium_clmm_observation import (
    RaydiumClmmObservation,
    RaydiumClmmObservationState,
    derive_raydium_clmm_observation_address,
    encode_raydium_clmm_observation_state,
)
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState
from veltrix.raydium_clmm_swap_inner_iteration import RaydiumClmmSwapInnerIterationResult
from veltrix.raydium_clmm_swap_transaction_settlement import (
    RaydiumSwapSettlementError,
    reconcile_raydium_swap_vault_balances,
    settle_raydium_clmm_swap_transaction,
)


def key():
    return str(Pubkey.new_unique())


def pool(*, fee_on=0):
    address = key()
    observation, _ = derive_raydium_clmm_observation_address(address)
    return RaydiumClmmPoolState(
        pool_address=address,
        bump=1,
        amm_config=key(),
        pool_creator=key(),
        token_mint_0=key(),
        token_mint_1=key(),
        token_vault_0=key(),
        token_vault_1=key(),
        observation_key=observation,
        mint_decimals_0=6,
        mint_decimals_1=6,
        tick_spacing=1,
        liquidity=500,
        sqrt_price_x64=1 << 64,
        tick_current=0,
        status=0,
        fee_on=fee_on,
        seed_index=0,
        account_data_sha256="a" * 64,
    )


def observation(p):
    address, _ = derive_raydium_clmm_observation_address(p.pool_address)
    state = RaydiumClmmObservationState(
        address=address,
        initialized=False,
        recent_epoch=1,
        observation_index=0,
        pool_id=p.pool_address,
        observations=tuple(
            RaydiumClmmObservation(0, 0, bytes(32)) for _ in range(100)
        ),
        padding=bytes(32),
        data_sha256="",
    )
    raw = encode_raydium_clmm_observation_state(state)
    return replace(state, data_sha256=hashlib.sha256(raw).hexdigest())


def iteration(*, zero_for_one, is_base_input, remaining=0, price_delta=10):
    amount0, amount1 = (
        (1000, 900) if is_base_input and zero_for_one else
        (900, 1000) if is_base_input else
        (1100, 1000) if zero_for_one else
        (1000, 1100)
    )
    state = RaydiumFeeAccountingState(
        amount_specified_remaining=remaining,
        amount_calculated=900 if is_base_input else 1100,
        liquidity=450,
        fee_growth_global_x64=99,
        lp_fee=2,
        protocol_fee=3,
        fund_fee=4,
    )
    price = (1 << 64) - price_delta if zero_for_one else (1 << 64) + price_delta
    return RaydiumClmmSwapInnerIterationResult(
        fee_state=state,
        tick_state=None,
        tick=0,
        sqrt_price_x64=price,
        dynamic_fee_state=None,
        liquidity_next=450,
        price_step=None,
        limit_order_match=RaydiumLimitOrderMatch(),
        unfilled_amount_before=0,
        unfilled_amount_after=0,
        reached_initialized_tick=False,
        crossed_tick=False,
        should_break=True,
    )


def test_exact_input_zero_for_one_settles_amounts_checks_threshold_and_finalizes_state():
    p = pool()
    result = settle_raydium_clmm_swap_transaction(
        p, observation(p), iteration(zero_for_one=True, is_base_input=True),
        amount_specified=1000,
        zero_for_one=True,
        is_base_input=True,
        sqrt_price_limit_x64=0,
        other_amount_threshold=850,
        block_timestamp=1700000000,
    )
    assert result.amounts.amount_0 == 1000
    assert result.amounts.amount_1 == 900
    assert result.amount_returned == 900
    assert result.transfer_fee_0 == 0
    assert result.transfer_fee_1 == 0
    assert result.state.pool.sqrt_price_x64 == (1 << 64) - 10


def test_exact_input_one_for_zero_and_exact_output_amount_return_semantics():
    p = pool()
    exact_input = settle_raydium_clmm_swap_transaction(
        p, observation(p), iteration(zero_for_one=False, is_base_input=True),
        amount_specified=1000,
        zero_for_one=False,
        is_base_input=True,
        sqrt_price_limit_x64=0,
        other_amount_threshold=850,
        block_timestamp=1700000000,
    )
    assert (exact_input.amounts.amount_0, exact_input.amounts.amount_1) == (900, 1000)
    assert exact_input.amount_returned == 900

    exact_output = settle_raydium_clmm_swap_transaction(
        p, observation(p), iteration(zero_for_one=True, is_base_input=False),
        amount_specified=1000,
        zero_for_one=True,
        is_base_input=False,
        sqrt_price_limit_x64=0,
        other_amount_threshold=1200,
        block_timestamp=1700000000,
    )
    assert (exact_output.amounts.amount_0, exact_output.amounts.amount_1) == (1100, 1000)
    assert exact_output.amount_returned == 1100


def test_partial_fill_requires_explicit_price_limit_and_respects_limit_direction():
    p = pool()
    partial = iteration(zero_for_one=True, is_base_input=True, remaining=100)
    with pytest.raises(RaydiumSwapSettlementError, match="partial fill is forbidden"):
        settle_raydium_clmm_swap_transaction(
            p, observation(p), partial,
            amount_specified=1000,
            zero_for_one=True,
            is_base_input=True,
            sqrt_price_limit_x64=0,
            other_amount_threshold=0,
            block_timestamp=1700000000,
        )
    # An explicit limit permits a partial fill, provided final price stays above it.
    allowed = settle_raydium_clmm_swap_transaction(
        p, observation(p), partial,
        amount_specified=1000,
        zero_for_one=True,
        is_base_input=True,
        sqrt_price_limit_x64=(1 << 64) - 20,
        other_amount_threshold=0,
        block_timestamp=1700000000,
    )
    assert allowed.amount_returned == 900


def test_threshold_price_direction_and_zero_amount_guards():
    p = pool()
    with pytest.raises(RaydiumSwapSettlementError, match="below"):
        settle_raydium_clmm_swap_transaction(
            p, observation(p), iteration(zero_for_one=True, is_base_input=True),
            amount_specified=1000, zero_for_one=True, is_base_input=True,
            sqrt_price_limit_x64=0, other_amount_threshold=901,
            block_timestamp=1700000000,
        )
    with pytest.raises(RaydiumSwapSettlementError, match="increased"):
        settle_raydium_clmm_swap_transaction(
            p, observation(p), iteration(zero_for_one=False, is_base_input=True),
            amount_specified=1000, zero_for_one=True, is_base_input=True,
            sqrt_price_limit_x64=0, other_amount_threshold=0,
            block_timestamp=1700000000,
        )


@pytest.mark.parametrize(
    "zero_for_one,amount0,amount1,before0,after0,before1,after1",
    [
        (True, 1000, 900, 5000, 6000, 7000, 6100),
        (False, 900, 1000, 5000, 4100, 7000, 8000),
    ],
)
def test_vault_balance_deltas_reconcile_exact_swap_transfer_amounts(
    zero_for_one, amount0, amount1, before0, after0, before1, after1
):
    result = reconcile_raydium_swap_vault_balances(
        zero_for_one=zero_for_one,
        amount_0=amount0,
        amount_1=amount1,
        token0_vault_before=before0,
        token0_vault_after=after0,
        token1_vault_before=before1,
        token1_vault_after=after1,
    )
    assert result.input_vault_delta == 1000
    assert result.output_vault_delta == 900


def test_vault_balance_reconciliation_rejects_wrong_delta_and_direction():
    with pytest.raises(RaydiumSwapSettlementError, match="input vault balance delta"):
        reconcile_raydium_swap_vault_balances(
            zero_for_one=True,
            amount_0=1000,
            amount_1=900,
            token0_vault_before=5000,
            token0_vault_after=5999,
            token1_vault_before=7000,
            token1_vault_after=6100,
        )
    with pytest.raises(RaydiumSwapSettlementError, match="output token0 vault balance increased"):
        reconcile_raydium_swap_vault_balances(
            zero_for_one=False,
            amount_0=900,
            amount_1=1000,
            token0_vault_before=5000,
            token0_vault_after=5001,
            token1_vault_before=7000,
            token1_vault_after=8000,
        )
