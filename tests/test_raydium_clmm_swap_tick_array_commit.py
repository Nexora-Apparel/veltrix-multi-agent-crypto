from dataclasses import replace

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_fee_accounting import RaydiumFeeAccountingState
from veltrix.raydium_clmm_limit_orders import RaydiumLimitOrderMatch
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    derive_raydium_clmm_tick_array_address,
    encode_raydium_clmm_tick_array,
)
from veltrix.raydium_clmm_swap_inner_iteration import RaydiumClmmSwapInnerIterationResult
from veltrix.raydium_clmm_swap_tick_array_commit import (
    RaydiumSwapTickArrayCommitError,
    commit_raydium_swap_inner_tick_array,
)


def key():
    return str(Pubkey.new_unique())


def pool():
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
        tick_spacing=1,
        liquidity=100,
        sqrt_price_x64=1 << 64,
        tick_current=0,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data_sha256="a" * 64,
        tick_array_bitmap=(0,) * 8 + (1,) + (0,) * 7,
    )


def tick(index, *, growth=0):
    return RaydiumClmmTick(
        tick_index=index,
        liquidity_net=0,
        liquidity_gross=1,
        orders_amount=0,
        part_filled_orders_remaining=0,
        initialized=True,
        fee_growth_outside_0_x64=growth,
    )


def tick_array(p):
    ticks = [RaydiumClmmTick(
        tick_index=i,
        liquidity_net=0,
        liquidity_gross=0,
        orders_amount=0,
        part_filled_orders_remaining=0,
        initialized=False,
    ) for i in range(RAYDIUM_TICK_ARRAY_SIZE)]
    ticks[0] = tick(0, growth=1)
    return RaydiumClmmTickArray(
        address=derive_raydium_clmm_tick_array_address(
            pool_address=p.pool_address, start_tick_index=0
        ),
        pool_address=p.pool_address,
        start_tick_index=0,
        tick_spacing=1,
        initialized_tick_count=1,
        recent_epoch=1,
        ticks=tuple(ticks),
        data_sha256="b" * 64,
    )


def iteration(*, reached, updated_tick):
    return RaydiumClmmSwapInnerIterationResult(
        fee_state=RaydiumFeeAccountingState(10, 0, 100),
        tick_state=updated_tick,
        tick=0,
        sqrt_price_x64=1 << 64,
        dynamic_fee_state=None,
        liquidity_next=100,
        price_step=None,
        limit_order_match=RaydiumLimitOrderMatch(),
        unfilled_amount_before=0,
        unfilled_amount_after=0,
        reached_initialized_tick=reached,
        crossed_tick=False,
        should_break=False,
    )


def test_non_boundary_iteration_does_not_mutate_any_account_state():
    p = pool()
    a = tick_array(p)
    result = commit_raydium_swap_inner_tick_array(
        p, a, iteration(reached=False, updated_tick=tick(0, growth=2)),
        recent_epoch=2,
    )
    assert result.committed is False
    assert result.mutation is None
    assert result.pool is p
    assert result.tick_array is a


def test_reached_boundary_commits_tick_array_and_verifies_serialized_hash():
    p = pool()
    a = tick_array(p)
    updated = replace(a.ticks[0], fee_growth_outside_0_x64=99)
    result = commit_raydium_swap_inner_tick_array(
        p, a, iteration(reached=True, updated_tick=updated), recent_epoch=7,
    )
    assert result.committed is True
    assert result.mutation is not None
    assert result.tick_array.ticks[0].fee_growth_outside_0_x64 == 99
    assert result.tick_array.recent_epoch == 7
    import hashlib
    assert result.tick_array.data_sha256 == hashlib.sha256(
        encode_raydium_clmm_tick_array(result.tick_array)
    ).hexdigest()
    assert result.mutation.bitmap_changed is False


def test_reached_boundary_rejects_tick_from_another_array_slot():
    p = pool()
    a = tick_array(p)
    with pytest.raises(RaydiumSwapTickArrayCommitError, match="mutation failed"):
        commit_raydium_swap_inner_tick_array(
            p, a, iteration(reached=True, updated_tick=tick(1, growth=99)),
            recent_epoch=7,
        )
