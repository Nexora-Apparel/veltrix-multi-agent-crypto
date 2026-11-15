import hashlib
from dataclasses import replace

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_observation import (
    RaydiumClmmObservation,
    RaydiumClmmObservationState,
    derive_raydium_clmm_observation_address,
    encode_raydium_clmm_observation_state,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_POOL_STATE_LENGTH,
    RaydiumClmmPoolState,
    encode_raydium_clmm_pool_state,
)
from veltrix.raydium_clmm_swap_loop import (
    RaydiumClmmSwapLoopError,
    run_raydium_clmm_swap_loop,
)
from veltrix.raydium_clmm_swap_loop_settlement import (
    RaydiumSwapLoopSettlementError,
    settle_raydium_clmm_swap_loop,
)
from veltrix.raydium_clmm_tick_array_cursor import RaydiumClmmTickArrayQueue
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    RaydiumClmmTickArraySnapshot,
    derive_raydium_clmm_tick_array_address,
)
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick
from veltrix.raydium_clmm_swap_vault_settlement import (
    apply_raydium_clmm_swap_vault_settlement,
    reconcile_raydium_clmm_swap_vault_snapshots,
)
from veltrix.raydium_clmm_vaults import (
    CLASSIC_SPL_TOKEN_PROGRAM_ID,
    RaydiumClmmVaultAccount,
    RaydiumClmmVaultSnapshot,
    derive_raydium_clmm_vault_address,
)


def key():
    return Pubkey.new_unique()


def make_pool():
    program = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
    config = key()
    mint0, mint1 = sorted((key(), key()), key=bytes)
    address, bump = Pubkey.find_program_address(
        [b"pool", bytes(config), bytes(mint0), bytes(mint1)], program
    )
    observation, _ = derive_raydium_clmm_observation_address(str(address))
    bitmap = [0] * 16
    bitmap[8] = 1
    raw = bytes(RAYDIUM_POOL_STATE_LENGTH)
    pool = RaydiumClmmPoolState(
        pool_address=str(address),
        bump=bump,
        amm_config=str(config),
        pool_creator=str(key()),
        token_mint_0=str(mint0),
        token_mint_1=str(mint1),
        token_vault_0=derive_raydium_clmm_vault_address(
            pool_address=str(address), mint=str(mint0)
        ),
        token_vault_1=derive_raydium_clmm_vault_address(
            pool_address=str(address), mint=str(mint1)
        ),
        observation_key=observation,
        mint_decimals_0=6,
        mint_decimals_1=6,
        tick_spacing=10,
        liquidity=1_000_000_000,
        sqrt_price_x64=get_sqrt_price_at_tick(0),
        tick_current=0,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data_sha256=hashlib.sha256(raw).hexdigest(),
        tick_array_bitmap=tuple(bitmap),
        account_data=raw,
    )
    encoded = encode_raydium_clmm_pool_state(pool)
    return replace(
        pool,
        account_data=encoded,
        account_data_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def make_tick_array(pool):
    ticks = [
        RaydiumClmmTick(
            tick_index=index * pool.tick_spacing,
            liquidity_net=0,
            liquidity_gross=0,
            orders_amount=0,
            part_filled_orders_remaining=0,
            initialized=False,
        )
        for index in range(RAYDIUM_TICK_ARRAY_SIZE)
    ]
    for index in (1, 2):
        ticks[index] = replace(
            ticks[index],
            liquidity_gross=100,
            initialized=True,
        )
    return RaydiumClmmTickArray(
        address=derive_raydium_clmm_tick_array_address(
            pool_address=pool.pool_address, start_tick_index=0
        ),
        pool_address=pool.pool_address,
        start_tick_index=0,
        tick_spacing=pool.tick_spacing,
        initialized_tick_count=2,
        recent_epoch=1,
        ticks=tuple(ticks),
        data_sha256="b" * 64,
    )


def make_snapshot(array):
    return RaydiumClmmTickArraySnapshot(
        route_message_sha256="c" * 64,
        simulation_response_id="sim-outer-loop",
        simulation_context_slot=100,
        provider_id="rpc-provider",
        cluster="devnet",
        genesis_hash="d" * 64,
        context_slot=101,
        tick_arrays=(array,),
        cpi_account_order=(array.address,),
        execution_ready=False,
    )


def make_queue(pool, snapshot):
    array = snapshot.tick_arrays[0]
    return RaydiumClmmTickArrayQueue(
        pool_address=pool.pool_address,
        tick_spacing=pool.tick_spacing,
        arrays=snapshot.tick_arrays,
        next_index=1,
        current=array,
        consumed_addresses=(array.address,),
        skipped_addresses=(),
        selected_addresses=(array.address,),
    )


def make_observation(pool):
    address, _ = derive_raydium_clmm_observation_address(pool.pool_address)
    state = RaydiumClmmObservationState(
        address=address,
        initialized=False,
        recent_epoch=1,
        observation_index=0,
        pool_id=pool.pool_address,
        observations=tuple(
            RaydiumClmmObservation(0, 0, bytes(32)) for _ in range(100)
        ),
        padding=bytes(32),
        data_sha256="",
    )
    raw = encode_raydium_clmm_observation_state(state)
    return replace(state, data_sha256=hashlib.sha256(raw).hexdigest())


def run(pool, snapshot, queue, **overrides):
    values = dict(
        tick_array_snapshot=snapshot,
        tick_array_queue=queue,
        first_tick_array_contains_pool_tick=True,
        amount_specified=750_000,
        sqrt_price_limit_x64=get_sqrt_price_at_tick(100),
        zero_for_one=False,
        is_base_input=True,
        base_fee_rate=0,
        protocol_fee_rate=0,
        fund_fee_rate=0,
        block_timestamp=1_700_000_000,
        recent_epoch=2,
        max_inner_iterations=20,
    )
    values.update(overrides)
    return run_raydium_clmm_swap_loop(pool, **values)


def test_outer_loop_crosses_intermediate_tick_then_continues_until_amount_exhaustion():
    p = make_pool()
    snapshot = make_snapshot(make_tick_array(p))
    queue = make_queue(p, snapshot)
    result = run(p, snapshot, queue)

    assert result.swap_complete is True
    assert result.inner_iterations == 2
    assert result.fee_state.amount_specified_remaining == 0
    assert result.final_iteration.swap_complete is True
    assert result.final_iteration.should_break is True
    assert result.final_iteration.reached_initialized_tick is False
    assert result.tick_array_snapshot.tick_arrays[0].ticks[1].initialized is True
    assert result.tick_array_snapshot.tick_arrays[0].ticks[2].initialized is True
    assert result.pool.account_data_sha256 == hashlib.sha256(result.pool.account_data).hexdigest()

    settled = settle_raydium_clmm_swap_loop(
        result,
        make_observation(result.pool),
        other_amount_threshold=0,
        block_timestamp=1_700_000_000,
    )
    assert settled.settlement.amounts.amount_1 == result.amount_specified
    assert settled.settlement.amount_returned > 0
    assert settled.settlement.state.pool.tick_current == result.final_iteration.tick
    with pytest.raises(RaydiumSwapLoopSettlementError, match="not reached"):
        settle_raydium_clmm_swap_loop(
            replace(result, swap_complete=False),
            make_observation(result.pool),
            other_amount_threshold=0,
            block_timestamp=1_700_000_000,
        )


def test_outer_loop_stops_at_user_price_limit_even_with_remaining_amount():
    p = make_pool()
    snapshot = make_snapshot(make_tick_array(p))
    queue = make_queue(p, snapshot)
    limit = get_sqrt_price_at_tick(10)
    result = run(
        p, snapshot, queue,
        amount_specified=2_000_000,
        sqrt_price_limit_x64=limit,
    )
    assert result.swap_complete is True
    assert result.final_iteration.swap_complete is True
    assert result.final_iteration.reached_initialized_tick is True
    assert result.fee_state.amount_specified_remaining > 0
    assert result.final_iteration.sqrt_price_x64 == limit


def test_outer_loop_rejects_missing_raw_pool_bytes_and_inconsistent_queue():
    p = make_pool()
    snapshot = make_snapshot(make_tick_array(p))
    queue = make_queue(p, snapshot)
    with pytest.raises(RaydiumClmmSwapLoopError, match="raw PoolState"):
        run(replace(p, account_data=b""), snapshot, queue)
    with pytest.raises(RaydiumClmmSwapLoopError, match="queue"):
        run(p, replace(snapshot, tick_arrays=()), queue)

def make_vault(pool, *, token0, amount=10_000_000_000):
    mint = pool.token_mint_0 if token0 else pool.token_mint_1
    address = pool.token_vault_0 if token0 else pool.token_vault_1
    raw = bytearray(165)
    raw[:32] = bytes(Pubkey.from_string(mint))
    raw[32:64] = bytes(Pubkey.from_string(pool.pool_address))
    raw[64:72] = amount.to_bytes(8, "little")
    raw[108] = 1
    encoded = bytes(raw)
    return RaydiumClmmVaultAccount(
        address=address,
        pool_address=pool.pool_address,
        mint=mint,
        token_program_id=CLASSIC_SPL_TOKEN_PROGRAM_ID,
        token_authority=pool.pool_address,
        amount_raw=amount,
        state=1,
        lamports=1,
        data_sha256=hashlib.sha256(encoded).hexdigest(),
        account_data=encoded,
    )


def test_completed_outer_loop_settlement_reconciles_real_directional_vault_snapshots():
    pool = make_pool()
    snapshot = make_snapshot(make_tick_array(pool))
    queue = make_queue(pool, snapshot)
    loop = run(pool, snapshot, queue)
    observation = make_observation(pool)
    settled = settle_raydium_clmm_swap_loop(
        loop,
        observation,
        other_amount_threshold=1,
        block_timestamp=1_700_000_000,
    )
    before = RaydiumClmmVaultSnapshot(
        route_message_sha256=snapshot.route_message_sha256,
        simulation_response_id=snapshot.simulation_response_id,
        simulation_context_slot=snapshot.simulation_context_slot,
        provider_id=snapshot.provider_id,
        cluster=snapshot.cluster,
        genesis_hash=snapshot.genesis_hash,
        context_slot=snapshot.context_slot,
        vaults=(
            make_vault(pool, token0=True),
            make_vault(pool, token0=False),
        ),
    )
    expected = apply_raydium_clmm_swap_vault_settlement(before, settled)
    after = replace(expected.expected_post_state, context_slot=snapshot.context_slot + 1)
    reconciled = reconcile_raydium_clmm_swap_vault_snapshots(
        before, after, settled
    )
    assert reconciled.reconciliation.input_vault_delta == settled.settlement.amounts.amount_1
    assert reconciled.reconciliation.output_vault_delta == settled.settlement.amounts.amount_0
    assert reconciled.reconciliation.input_amount_expected == settled.settlement.amounts.amount_1
    assert reconciled.reconciliation.output_amount_expected == settled.settlement.amounts.amount_0
    for vault in after.vaults:
        assert len(vault.account_data) == 165
        assert hashlib.sha256(vault.account_data).hexdigest() == vault.data_sha256

