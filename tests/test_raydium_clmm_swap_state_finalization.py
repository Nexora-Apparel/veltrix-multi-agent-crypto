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
from veltrix.raydium_clmm_swap_state_finalization import (
    RaydiumSwapStateFinalizationError,
    finalize_raydium_swap_state_bundle,
)


def key():
    return str(Pubkey.new_unique())


def make_pool(*, tick_current=10, fee_on=0):
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
        tick_current=tick_current,
        status=0,
        fee_on=fee_on,
        seed_index=0,
        account_data_sha256="a" * 64,
    )


def make_observation(pool, *, initialized=True, timestamp=1000, cumulative=500):
    address, _ = derive_raydium_clmm_observation_address(pool.pool_address)
    observations = [RaydiumClmmObservation(0, 0, bytes(32)) for _ in range(100)]
    if initialized:
        observations[0] = RaydiumClmmObservation(timestamp, cumulative, bytes(32))
    state = RaydiumClmmObservationState(
        address=address,
        initialized=initialized,
        recent_epoch=1,
        observation_index=0,
        pool_id=pool.pool_address,
        observations=tuple(observations),
        padding=bytes(32),
        data_sha256="",
    )
    encoded = encode_raydium_clmm_observation_state(state)
    return replace(state, data_sha256=hashlib.sha256(encoded).hexdigest())


def make_iteration(*, tick=11, should_break=True):
    return RaydiumClmmSwapInnerIterationResult(
        fee_state=RaydiumFeeAccountingState(
            amount_specified_remaining=0,
            amount_calculated=100,
            liquidity=450,
            fee_growth_global_x64=99,
            lp_fee=2,
            protocol_fee=3,
            fund_fee=4,
        ),
        tick_state=None,
        tick=tick,
        sqrt_price_x64=1 << 65,
        dynamic_fee_state=None,
        liquidity_next=450,
        price_step=None,
        limit_order_match=RaydiumLimitOrderMatch(),
        unfilled_amount_before=0,
        unfilled_amount_after=0,
        reached_initialized_tick=False,
        crossed_tick=False,
        should_break=should_break,
    )


def test_finalization_updates_observation_using_previous_pool_tick_before_pool_update():
    p = make_pool(tick_current=10)
    observation = make_observation(p)
    result = finalize_raydium_swap_state_bundle(
        p, observation, make_iteration(tick=11),
        block_timestamp=1015, zero_for_one=True,
    )
    assert result.observation_updated is True
    assert result.observation.observation_index == 1
    assert result.observation.observations[1].block_timestamp == 1015
    assert result.observation.observations[1].tick_cumulative == 650
    assert result.pool.tick_current == 11
    assert result.pool.sqrt_price_x64 == 1 << 65
    assert result.pool.liquidity == 450


def test_finalization_does_not_write_observation_when_tick_is_unchanged():
    p = make_pool(tick_current=10)
    observation = make_observation(p)
    result = finalize_raydium_swap_state_bundle(
        p, observation, make_iteration(tick=10),
        block_timestamp=1015, zero_for_one=True,
    )
    assert result.observation_updated is False
    assert result.observation is observation
    assert result.observation.observation_index == 0
    assert result.pool.tick_current == 10


def test_finalization_validates_observation_identity_hash_and_loop_completion():
    p = make_pool()
    observation = make_observation(p)
    other_pool = replace(p, observation_key=key())
    with pytest.raises(RaydiumSwapStateFinalizationError, match="identity"):
        finalize_raydium_swap_state_bundle(
            other_pool, observation, make_iteration(),
            block_timestamp=1015, zero_for_one=True,
        )
    bad_hash = replace(observation, data_sha256="0" * 64)
    with pytest.raises(RaydiumSwapStateFinalizationError, match="source hash"):
        finalize_raydium_swap_state_bundle(
            p, bad_hash, make_iteration(),
            block_timestamp=1015, zero_for_one=True,
        )
    with pytest.raises(RaydiumSwapStateFinalizationError, match="PoolState finalization"):
        finalize_raydium_swap_state_bundle(
            p, observation, make_iteration(should_break=False),
            block_timestamp=1015, zero_for_one=True,
        )
