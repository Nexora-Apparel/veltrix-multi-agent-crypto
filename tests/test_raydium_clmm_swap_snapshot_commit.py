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
    RaydiumClmmTickArraySnapshot,
    derive_raydium_clmm_tick_array_address,
)
from veltrix.raydium_clmm_swap_inner_iteration import RaydiumClmmSwapInnerIterationResult
from veltrix.raydium_clmm_swap_snapshot_commit import (
    RaydiumClmmSnapshotCommitError,
    commit_raydium_clmm_inner_iteration_snapshot,
)


def key():
    return str(Pubkey.new_unique())


def pool_with_bytes():
    program = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
    config = Pubkey.new_unique()
    mint0, mint1 = sorted((Pubkey.new_unique(), Pubkey.new_unique()), key=bytes)
    pool_address, bump = Pubkey.find_program_address(
        [b"pool", bytes(config), bytes(mint0), bytes(mint1)], program
    )
    words = [0] * 16
    words[8] = 1
    raw = bytes(RAYDIUM_POOL_STATE_LENGTH)
    p = RaydiumClmmPoolState(
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
        account_data_sha256=hashlib.sha256(raw).hexdigest(),
        tick_array_bitmap=tuple(words),
        account_data=raw,
    )
    encoded = encode_raydium_clmm_pool_state(p)
    return replace(p, account_data=encoded, account_data_sha256=hashlib.sha256(encoded).hexdigest())


def build_array(pool):
    ticks = tuple(
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
        for index in range(RAYDIUM_TICK_ARRAY_SIZE)
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
        ticks=ticks,
        data_sha256="b" * 64,
    )


def snapshot(array):
    return RaydiumClmmTickArraySnapshot(
        route_message_sha256="a" * 64,
        simulation_response_id="sim",
        simulation_context_slot=100,
        provider_id="rpc",
        cluster="devnet",
        genesis_hash="c" * 64,
        context_slot=101,
        tick_arrays=(array,),
        cpi_account_order=(array.address,),
    )


def iteration():
    updated = RaydiumClmmTick(
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
    return RaydiumClmmSwapInnerIterationResult(
        fee_state=RaydiumFeeAccountingState(100, 0, 1000),
        tick_state=updated,
        tick=-1,
        sqrt_price_x64=1 << 64,
        dynamic_fee_state=None,
        liquidity_next=1000,
        price_step=None,
        limit_order_match=RaydiumLimitOrderMatch(),
        unfilled_amount_before=0,
        unfilled_amount_after=0,
        reached_initialized_tick=True,
        crossed_tick=False,
        should_break=True,
    )


def test_snapshot_commit_updates_tick_array_and_pool_hash_without_reordering_cpi_accounts():
    pool = pool_with_bytes()
    array = build_array(pool)
    original_snapshot = snapshot(array)
    result = commit_raydium_clmm_inner_iteration_snapshot(
        pool,
        original_snapshot,
        iteration(),
        tick_array_address=array.address,
        recent_epoch=2,
    )
    assert result.snapshot.cpi_account_order == original_snapshot.cpi_account_order
    assert result.snapshot.tick_arrays[0].initialized_tick_count == 0
    assert result.snapshot.tick_arrays[0].recent_epoch == 2
    assert result.commit.mutation.bitmap_changed is True
    assert result.commit.mutation.pool.account_data_sha256 == hashlib.sha256(
        result.commit.mutation.pool.account_data
    ).hexdigest()


def test_snapshot_commit_rejects_wrong_array_or_missing_boundary():
    pool = pool_with_bytes()
    array = build_array(pool)
    with pytest.raises(RaydiumClmmSnapshotCommitError, match="exactly once"):
        commit_raydium_clmm_inner_iteration_snapshot(
            pool,
            snapshot(array),
            iteration(),
            tick_array_address=key(),
            recent_epoch=2,
        )
    no_boundary = replace(iteration(), reached_initialized_tick=False)
    with pytest.raises(RaydiumClmmSnapshotCommitError, match="boundary"):
        commit_raydium_clmm_inner_iteration_snapshot(
            pool,
            snapshot(array),
            no_boundary,
            tick_array_address=array.address,
            recent_epoch=2,
        )
