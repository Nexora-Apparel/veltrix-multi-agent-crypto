"""End-to-end deterministic local swap replay over committed Raydium accounts."""
import hashlib
from dataclasses import replace

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_bitmap_traversal import RaydiumClmmBitmapTraversalResult
from veltrix.raydium_clmm_observation import (
    RAYDIUM_OBSERVATION_COUNT,
    RaydiumClmmObservation,
    RaydiumClmmObservationState,
    derive_raydium_clmm_observation_address,
    encode_raydium_clmm_observation_state,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_POOL_STATE_LENGTH,
    RaydiumClmmPoolState,
    derive_raydium_clmm_pool_address,
    encode_raydium_clmm_pool_state,
)
from veltrix.raydium_clmm_replay_bundle import (
    build_raydium_clmm_replay_account_bundle,
)
from veltrix.raydium_clmm_swap_replay import (
    RaydiumSwapReplayError,
    replay_raydium_clmm_swap,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    derive_raydium_clmm_tick_array_address,
    encode_raydium_clmm_tick_array,
)
from veltrix.raydium_clmm_tick_math import Q64
from veltrix.raydium_clmm_vaults import (
    CLASSIC_SPL_TOKEN_PROGRAM_ID,
    RaydiumClmmVaultAccount,
    derive_raydium_clmm_vault_address,
    encode_raydium_clmm_vault_token_account,
)


def make_replay_bundle():
    program = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
    config = Pubkey.new_unique()
    mint0, mint1 = sorted((Pubkey.new_unique(), Pubkey.new_unique()), key=bytes)
    creator = Pubkey.new_unique()
    pool_address, bump = Pubkey.find_program_address(
        [b"pool", bytes(config), bytes(mint0), bytes(mint1)], program
    )
    vault0 = derive_raydium_clmm_vault_address(
        pool_address=str(pool_address), mint=str(mint0)
    )
    vault1 = derive_raydium_clmm_vault_address(
        pool_address=str(pool_address), mint=str(mint1)
    )
    observation_address, _ = derive_raydium_clmm_observation_address(
        str(pool_address)
    )
    words = [0] * 16
    words[8] = 1  # default bitmap bit 512: tick-array start 0
    pool = RaydiumClmmPoolState(
        pool_address=str(pool_address),
        bump=bump,
        amm_config=str(config),
        pool_creator=str(creator),
        token_mint_0=str(mint0),
        token_mint_1=str(mint1),
        token_vault_0=vault0,
        token_vault_1=vault1,
        observation_key=observation_address,
        mint_decimals_0=6,
        mint_decimals_1=6,
        tick_spacing=1,
        liquidity=1000,
        sqrt_price_x64=Q64,
        tick_current=0,
        status=0,
        fee_on=0,
        seed_index=0,
        tick_array_bitmap=tuple(words),
        account_data=bytes(RAYDIUM_POOL_STATE_LENGTH),
        account_data_sha256=hashlib.sha256(bytes(RAYDIUM_POOL_STATE_LENGTH)).hexdigest(),
    )
    pool_bytes = encode_raydium_clmm_pool_state(pool)
    pool = pool.__class__(
        **{
            **{field: getattr(pool, field) for field in pool.__dataclass_fields__},
            "account_data": pool_bytes,
            "account_data_sha256": hashlib.sha256(pool_bytes).hexdigest(),
        }
    )

    ticks = []
    for index in range(RAYDIUM_TICK_ARRAY_SIZE):
        if index == 0:
            ticks.append(RaydiumClmmTick(
                tick_index=0,
                liquidity_net=25,
                liquidity_gross=100,
                orders_amount=10,
                part_filled_orders_remaining=0,
                initialized=True,
            ))
        else:
            ticks.append(RaydiumClmmTick(
                tick_index=index,
                liquidity_net=0,
                liquidity_gross=0,
                orders_amount=0,
                part_filled_orders_remaining=0,
                initialized=False,
            ))
    tick_array = RaydiumClmmTickArray(
        address=derive_raydium_clmm_tick_array_address(
            pool_address=pool.pool_address, start_tick_index=0
        ),
        pool_address=pool.pool_address,
        start_tick_index=0,
        tick_spacing=1,
        initialized_tick_count=1,
        recent_epoch=1,
        ticks=tuple(ticks),
        data_sha256="",
    )
    tick_bytes = encode_raydium_clmm_tick_array(tick_array)
    tick_array = tick_array.__class__(
        **{
            **{field: getattr(tick_array, field) for field in tick_array.__dataclass_fields__},
            "data_sha256": hashlib.sha256(tick_bytes).hexdigest(),
        }
    )

    observation = RaydiumClmmObservationState(
        address=observation_address,
        initialized=False,
        recent_epoch=1,
        observation_index=0,
        pool_id=pool.pool_address,
        observations=tuple(
            RaydiumClmmObservation(0, 0, bytes(32))
            for _ in range(RAYDIUM_OBSERVATION_COUNT)
        ),
        padding=bytes(32),
        data_sha256="",
    )
    observation_bytes = encode_raydium_clmm_observation_state(observation)
    observation = observation.__class__(
        **{
            **{field: getattr(observation, field) for field in observation.__dataclass_fields__},
            "data_sha256": hashlib.sha256(observation_bytes).hexdigest(),
        }
    )

    vaults = []
    for address, mint, amount in (
        (vault0, str(mint0), 1000),
        (vault1, str(mint1), 1000),
    ):
        raw = bytearray(165)
        raw[0:32] = bytes(Pubkey.from_string(mint))
        raw[32:64] = bytes(pool_address)
        raw[64:72] = amount.to_bytes(8, "little")
        raw[108] = 1
        raw = bytes(raw)
        vault = RaydiumClmmVaultAccount(
            address=address,
            pool_address=str(pool_address),
            mint=mint,
            token_program_id=CLASSIC_SPL_TOKEN_PROGRAM_ID,
            token_authority=str(pool_address),
            amount_raw=amount,
            state=1,
            lamports=1,
            data_sha256=hashlib.sha256(raw).hexdigest(),
            account_data=raw,
        )
        assert encode_raydium_clmm_vault_token_account(vault) == raw
        vaults.append(vault)

    bundle = build_raydium_clmm_replay_account_bundle(
        pool,
        tick_arrays=(tick_array,),
        observation=observation,
        vaults=tuple(vaults),
    )
    traversal = RaydiumClmmBitmapTraversalResult(
        passed=True,
        pool_address=pool.pool_address,
        zero_for_one=True,
        current_array_start_index=0,
        first_initialized_array_start_index=0,
        last_cpi_array_start_index=0,
        expected_initialized_array_starts=(0,),
        supplied_initialized_array_starts=(0,),
        supplied_array_starts=(0,),
        bitmap_extension_sha256=None,
        bounded_interval_complete=True,
        execution_ready=False,
    )
    return bundle, traversal


def test_full_local_replay_matches_boundary_fifo_crossing_and_staged_settlement():
    bundle, traversal = make_replay_bundle()
    result = replay_raydium_clmm_swap(
        bundle,
        traversal,
        amount_specified=10,
        sqrt_price_limit_x64=Q64 - 1,
        base_fee_rate=0,
        protocol_fee_rate=0,
        fund_fee_rate=0,
        block_timestamp=1000,
        recent_epoch=2,
        zero_for_one=True,
        is_base_input=True,
    )
    assert result.execution_ready is False
    assert result.amount_specified_remaining == 0
    assert result.outer_iterations == 1
    assert result.inner_iterations == 1
    assert result.boundary_crossings == 1
    assert result.settlement.amount_0 == 10
    assert result.settlement.amount_1 == 10
    assert result.final_bundle.pool.tick_current == -1
    assert result.final_bundle.pool.liquidity == 975
    assert result.final_bundle.observation.initialized is True
    vaults = {vault.address: vault for vault in result.final_bundle.vaults}
    assert vaults[result.final_bundle.pool.token_vault_0].amount_raw == 1010
    assert vaults[result.final_bundle.pool.token_vault_1].amount_raw == 990
    assert len(result.final_bundle.bundle_sha256) == 64


def test_replay_recomputes_and_rejects_forged_bitmap_traversal_proof():
    bundle, traversal = make_replay_bundle()
    forged = replace(
        traversal,
        first_initialized_array_start_index=60,
        expected_initialized_array_starts=(60,),
        supplied_initialized_array_starts=(60,),
        supplied_array_starts=(60,),
        last_cpi_array_start_index=60,
    )
    with pytest.raises(RaydiumSwapReplayError, match="not source-derived"):
        replay_raydium_clmm_swap(
            bundle,
            forged,
            amount_specified=10,
            sqrt_price_limit_x64=Q64 - 1,
            base_fee_rate=0,
            protocol_fee_rate=0,
            fund_fee_rate=0,
            block_timestamp=1000,
            recent_epoch=2,
            zero_for_one=True,
            is_base_input=True,
        )


def test_replay_rejects_wrong_swap_direction_before_mutating_any_account():
    bundle, traversal = make_replay_bundle()
    with pytest.raises(RaydiumSwapReplayError, match="direction"):
        replay_raydium_clmm_swap(
            bundle,
            traversal,
            amount_specified=10,
            sqrt_price_limit_x64=Q64 - 1,
            base_fee_rate=0,
            protocol_fee_rate=0,
            fund_fee_rate=0,
            block_timestamp=1000,
            recent_epoch=2,
            zero_for_one=False,
            is_base_input=True,
        )


def test_replay_advances_across_two_initialized_tick_arrays_after_first_crossing():
    bundle, _ = make_replay_bundle()
    pool = bundle.pool
    first_array = bundle.tick_arrays[0]
    second_ticks = []
    for offset, original in enumerate(first_array.ticks):
        index = -60 + offset
        if offset == 50:
            second_ticks.append(replace(
                original,
                tick_index=index,
                liquidity_net=25,
                liquidity_gross=100,
                orders_amount=10,
                part_filled_orders_remaining=0,
                initialized=True,
            ))
        else:
            second_ticks.append(replace(original, tick_index=index))
    second_array = replace(
        first_array,
        address=derive_raydium_clmm_tick_array_address(
            pool_address=pool.pool_address, start_tick_index=-60
        ),
        start_tick_index=-60,
        ticks=tuple(second_ticks),
        initialized_tick_count=1,
        data_sha256="",
    )
    second_bytes = encode_raydium_clmm_tick_array(second_array)
    second_array = replace(
        second_array, data_sha256=hashlib.sha256(second_bytes).hexdigest()
    )
    words = list(pool.tick_array_bitmap)
    words[7] |= 1 << 63  # start -60 -> compressed bit 511
    updated_pool = replace(pool, tick_array_bitmap=tuple(words))
    pool_bytes = encode_raydium_clmm_pool_state(updated_pool)
    updated_pool = replace(
        updated_pool,
        account_data=pool_bytes,
        account_data_sha256=hashlib.sha256(pool_bytes).hexdigest(),
    )
    bundle = build_raydium_clmm_replay_account_bundle(
        updated_pool,
        tick_arrays=(first_array, second_array),
        observation=bundle.observation,
        vaults=bundle.vaults,
    )
    traversal = RaydiumClmmBitmapTraversalResult(
        passed=True,
        pool_address=updated_pool.pool_address,
        zero_for_one=True,
        current_array_start_index=0,
        first_initialized_array_start_index=0,
        last_cpi_array_start_index=-60,
        expected_initialized_array_starts=(0, -60),
        supplied_initialized_array_starts=(0, -60),
        supplied_array_starts=(0, -60),
        bitmap_extension_sha256=None,
        bounded_interval_complete=True,
        execution_ready=False,
    )
    result = replay_raydium_clmm_swap(
        bundle,
        traversal,
        amount_specified=20,
        sqrt_price_limit_x64=Q64 - 1,
        base_fee_rate=0,
        protocol_fee_rate=0,
        fund_fee_rate=0,
        block_timestamp=1000,
        recent_epoch=2,
        zero_for_one=True,
        is_base_input=True,
    )
    assert result.outer_iterations == 2
    assert result.inner_iterations == 2
    assert result.boundary_crossings == 1
    assert result.final_bundle.pool.sqrt_price_x64 < Q64
    assert len(result.final_bundle.tick_arrays) == 2
    assert result.amount_specified_remaining == 0
