import hashlib
from dataclasses import replace

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_fee_accounting import RaydiumFeeAccountingState
from veltrix.raydium_clmm_limit_orders import RaydiumLimitOrderMatch
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_POOL_STATE_LENGTH,
    RaydiumClmmPoolState,
    encode_raydium_clmm_pool_state,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    derive_raydium_clmm_tick_array_address,
    encode_raydium_clmm_tick_array,
)
from veltrix.raydium_clmm_swap_inner_iteration import RaydiumClmmSwapInnerIterationResult
from veltrix.raydium_clmm_tick_array_commit import (
    RaydiumClmmTickArrayCommitError,
    commit_raydium_clmm_inner_iteration,
)


def key():
    return str(Pubkey.new_unique())


def build_pool_with_serialized_bytes():
    program = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
    config = Pubkey.new_unique()
    mint0, mint1 = sorted((Pubkey.new_unique(), Pubkey.new_unique()), key=bytes)
    pool_address, bump = Pubkey.find_program_address(
        [b"pool", bytes(config), bytes(mint0), bytes(mint1)], program
    )
    words = [0] * 16
    words[8] = 1
    original = bytes(RAYDIUM_POOL_STATE_LENGTH)
    pool = RaydiumClmmPoolState(
        pool_address=str(pool_address),
        bump=bump,
        amm_config=str(config),
        pool_creator=key(),
        token_mint_0=str(mint0),
        token_mint_1=str(mint1),
        token_vault_0=key(),
        token_vault_1=key(),
        observation_key=key(),
        mint_decimals_0=6,
        mint_decimals_1=6,
        tick_spacing=1,
        liquidity=1000,
        sqrt_price_x64=1 << 64,
        tick_current=0,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data_sha256=hashlib.sha256(original).hexdigest(),
        tick_array_bitmap=tuple(words),
        account_data=original,
    )
    encoded = encode_raydium_clmm_pool_state(pool)
    return replace(
        pool,
        account_data=encoded,
        account_data_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def build_tick_array(pool):
    ticks = []
    for index in range(RAYDIUM_TICK_ARRAY_SIZE):
        ticks.append(
            RaydiumClmmTick(
                tick_index=index,
                liquidity_net=0,
                liquidity_gross=1 if index == 0 else 0,
                orders_amount=0,
                part_filled_orders_remaining=0,
                initialized=index == 0,
                fee_growth_outside_0_x64=0,
                fee_growth_outside_1_x64=0,
                reward_growths_outside_x64=(0, 0, 0),
                order_phase=0,
                unfilled_ratio_x64=0,
                padding=(0, 0, 0),
            )
        )
    return RaydiumClmmTickArray(
        address=derive_raydium_clmm_tick_array_address(
            pool_address=pool.pool_address, start_tick_index=0
        ),
        pool_address=pool.pool_address,
        start_tick_index=0,
        tick_spacing=1,
        initialized_tick_count=1,
        recent_epoch=1,
        ticks=tuple(ticks),
        data_sha256="b" * 64,
    )


def iteration(updated_tick, *, reached=True):
    return RaydiumClmmSwapInnerIterationResult(
        fee_state=RaydiumFeeAccountingState(100, 0, 1000),
        tick_state=updated_tick,
        tick=-1,
        sqrt_price_x64=1 << 64,
        dynamic_fee_state=None,
        liquidity_next=1000,
        price_step=None,
        limit_order_match=RaydiumLimitOrderMatch(),
        unfilled_amount_before=0,
        unfilled_amount_after=0,
        reached_initialized_tick=reached,
        crossed_tick=False,
        should_break=True,
    )


def test_completed_inner_iteration_commits_exact_pool_and_tick_array_bytes():
    pool = build_pool_with_serialized_bytes()
    array = build_tick_array(pool)
    updated_tick = RaydiumClmmTick(
        tick_index=0,
        liquidity_net=0,
        liquidity_gross=0,
        orders_amount=0,
        part_filled_orders_remaining=0,
        initialized=False,
        fee_growth_outside_0_x64=0,
        fee_growth_outside_1_x64=0,
        reward_growths_outside_x64=(0, 0, 0),
        order_phase=0,
        unfilled_ratio_x64=0,
        padding=(0, 0, 0),
    )
    result = commit_raydium_clmm_inner_iteration(
        pool, array, iteration(updated_tick), recent_epoch=2
    )
    mutation = result.mutation
    assert mutation.initialized_before is True
    assert mutation.initialized_after is False
    assert mutation.bitmap_changed is True
    assert mutation.tick_array.initialized_tick_count == 0
    assert mutation.tick_array.recent_epoch == 2
    assert mutation.pool.account_data_sha256 == hashlib.sha256(mutation.pool.account_data).hexdigest()
    assert result.pool_data_sha256 == mutation.pool.account_data_sha256
    assert result.tick_array_data_sha256 == hashlib.sha256(
        encode_raydium_clmm_tick_array(mutation.tick_array)
    ).hexdigest()
    assert mutation.pool.tick_array_bitmap[8] & 1 == 0


def test_tick_array_commit_rejects_stale_pool_hash_and_non_boundary_iteration():
    pool = build_pool_with_serialized_bytes()
    array = build_tick_array(pool)
    updated_tick = replace(array.ticks[0], liquidity_gross=0, initialized=False)
    with pytest.raises(RaydiumClmmTickArrayCommitError, match="PoolState original bytes"):
        commit_raydium_clmm_inner_iteration(
            replace(pool, account_data_sha256="0" * 64),
            array,
            iteration(updated_tick),
            recent_epoch=2,
        )
    with pytest.raises(RaydiumClmmTickArrayCommitError, match="only be committed"):
        commit_raydium_clmm_inner_iteration(
            pool,
            array,
            iteration(updated_tick, reached=False),
            recent_epoch=2,
        )
