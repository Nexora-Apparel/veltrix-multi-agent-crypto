"""End-to-end tests for Raydium CLMM reward/oracle and account serialization."""
import hashlib
import struct

import pytest
from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolAccountEvidence
from veltrix.raydium_clmm_bitmap_traversal import RaydiumClmmBitmapTraversalResult
from veltrix.raydium_clmm_observation import (
    RAYDIUM_OBSERVATION_STATE_DISCRIMINATOR,
    RAYDIUM_OBSERVATION_STATE_LENGTH,
    decode_raydium_clmm_observation_state,
    derive_raydium_clmm_observation_address,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_POOL_STATE_DISCRIMINATOR,
    RAYDIUM_POOL_STATE_LENGTH,
    RaydiumClmmPoolStateError,
    decode_raydium_clmm_pool_state,
    derive_raydium_clmm_pool_address,
)
from veltrix.raydium_clmm_rewards import update_raydium_reward_infos
from veltrix.raydium_clmm_swap_finalization import (
    RaydiumSwapFinalizationError,
    finalize_raydium_swap_state,
)
from veltrix.raydium_clmm_swap_loop_execution import execute_raydium_swap_loop
from veltrix.raydium_clmm_swap_loop_state import initialize_raydium_swap_loop_state
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_DATA_LENGTH,
    RAYDIUM_TICK_ARRAY_DISCRIMINATOR,
    RaydiumClmmTickArrayError,
    decode_raydium_clmm_tick_array,
    derive_raydium_clmm_tick_array_address,
)
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick


def fixture():
    program = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
    config = Pubkey.new_unique()
    mint0, mint1 = sorted((Pubkey.new_unique(), Pubkey.new_unique()), key=bytes)
    creator, vault0, vault1 = Pubkey.new_unique(), Pubkey.new_unique(), Pubkey.new_unique()
    observation, _ = derive_raydium_clmm_observation_address(
        str(Pubkey.new_unique())
    )
    pool_address, bump = Pubkey.find_program_address(
        [b"pool", bytes(config), bytes(mint0), bytes(mint1), b""], program
    )
    pool_address = str(pool_address)
    observation, _ = derive_raydium_clmm_observation_address(pool_address)

    pool_data = bytearray(RAYDIUM_POOL_STATE_LENGTH)
    pool_data[:8] = RAYDIUM_POOL_STATE_DISCRIMINATOR
    pool_data[8] = bump
    for offset, key in (
        (9, config), (41, creator), (73, mint0), (105, mint1),
        (137, vault0), (169, vault1), (201, Pubkey.from_string(observation)),
    ):
        pool_data[offset:offset + 32] = bytes(key)
    pool_data[233] = 6
    pool_data[234] = 6
    struct.pack_into("<H", pool_data, 235, 1)
    pool_data[237:253] = (10_000).to_bytes(16, "little")
    pool_data[253:269] = get_sqrt_price_at_tick(0).to_bytes(16, "little")
    struct.pack_into("<i", pool_data, 269, 0)
    pool_data[389] = 0
    pool_data[390] = 0
    pool_data[391:393] = (0).to_bytes(2, "little")
    pool_data[904 + 8 * 8:904 + 8 * 8 + 8] = (1).to_bytes(8, "little")
    pool_raw = bytes(pool_data)
    pool_evidence = DexPoolAccountEvidence(
        amm_key=pool_address, dex_label="Raydium CLMM",
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
        executable=False, data_length=len(pool_raw),
        data_sha256=hashlib.sha256(pool_raw).hexdigest(), data_bytes=pool_raw,
    )
    pool = decode_raydium_clmm_pool_state(
        pool_evidence, expected_pool_address=pool_address
    )

    tick_data = bytearray(RAYDIUM_TICK_ARRAY_DATA_LENGTH)
    tick_data[:8] = RAYDIUM_TICK_ARRAY_DISCRIMINATOR
    tick_data[8:40] = bytes(Pubkey.from_string(pool_address))
    struct.pack_into("<i", tick_data, 40, 0)
    tick_offset = 44 + 10 * 168
    struct.pack_into("<i", tick_data, tick_offset, 10)
    tick_data[tick_offset + 20:tick_offset + 36] = (1_000).to_bytes(16, "little")
    tick_data[10_124] = 1
    tick_data[10_125:10_133] = (1).to_bytes(8, "little")
    tick_raw = bytes(tick_data)
    tick_array = decode_raydium_clmm_tick_array(
        address=derive_raydium_clmm_tick_array_address(
            pool_address=pool_address, start_tick_index=0,
        ),
        owner=RAYDIUM_CLMM_PROGRAM_ID, lamports=1, executable=False,
        data=tick_raw, expected_pool_address=pool_address,
        expected_start_tick_index=0, tick_spacing=1,
    )

    observation_data = bytearray(RAYDIUM_OBSERVATION_STATE_LENGTH)
    observation_data[:8] = RAYDIUM_OBSERVATION_STATE_DISCRIMINATOR
    observation_data[9:17] = (7).to_bytes(8, "little")
    observation_data[17:19] = (0).to_bytes(2, "little")
    observation_data[19:51] = bytes(Pubkey.from_string(pool_address))
    observation_raw = bytes(observation_data)
    observation_evidence = DexPoolAccountEvidence(
        amm_key=observation, dex_label="Raydium CLMM Observation",
        expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
        executable=False, data_length=len(observation_raw),
        data_sha256=hashlib.sha256(observation_raw).hexdigest(),
        data_bytes=observation_raw,
    )
    observation_state = decode_raydium_clmm_observation_state(
        observation_evidence,
        expected_address=observation,
        expected_pool_address=pool_address,
    )

    traversal = RaydiumClmmBitmapTraversalResult(
        passed=True, pool_address=pool_address, zero_for_one=False,
        current_array_start_index=0, first_initialized_array_start_index=0,
        last_cpi_array_start_index=0, expected_initialized_array_starts=(0,),
        supplied_initialized_array_starts=(0,), supplied_array_starts=(0,),
        bitmap_extension_sha256=None, bounded_interval_complete=True,
        execution_ready=False,
    )
    timestamp = 1_700_000_000
    rewards = update_raydium_reward_infos(
        pool.reward_infos, curr_timestamp=timestamp, liquidity=pool.liquidity
    )
    state = initialize_raydium_swap_loop_state(
        pool, amount_specified=100_000, trade_fee_rate=0,
        zero_for_one=False, is_base_input=True, block_timestamp=timestamp,
    )
    execution = execute_raydium_swap_loop(
        state, tick_arrays=(tick_array,), traversal=traversal,
        sqrt_price_limit_x64=get_sqrt_price_at_tick(10),
        first_tick_array_contains_pool_tick=True,
        updated_reward_infos=rewards, recent_epoch=2,
        protocol_fee_rate=0, fund_fee_rate=0, max_iterations=10,
    )
    return (
        pool, pool_raw, tick_array, tick_raw, observation_state,
        observation_raw, execution, timestamp,
    )


def test_finalizes_reward_oracle_pool_and_tick_array_post_state_bytes():
    (
        pool, pool_raw, tick_array, tick_raw, observation,
        observation_raw, execution, timestamp,
    ) = fixture()
    assert execution.complete is True
    assert execution.state.tick == 10

    final = finalize_raydium_swap_state(
        pool, pool_raw, execution,
        initial_observation=observation,
        observation_account_data=observation_raw,
        initial_tick_arrays=(tick_array,),
        tick_array_account_data=(tick_raw,),
        block_timestamp=timestamp,
    )
    assert final.pool.tick_current == 10
    assert final.pool.sqrt_price_x64 == get_sqrt_price_at_tick(10)
    assert final.observation.initialized is True
    assert final.observation.observation_index == 0
    assert final.observation.observations[0].block_timestamp == timestamp
    assert final.pool_account.data_sha256 == hashlib.sha256(final.pool_account.data).hexdigest()
    assert final.observation_account.data_sha256 == hashlib.sha256(final.observation_account.data).hexdigest()
    assert final.tick_array_accounts[0].data_sha256 == hashlib.sha256(final.tick_array_accounts[0].data).hexdigest()

    decoded_pool = decode_raydium_clmm_pool_state(
        DexPoolAccountEvidence(
            amm_key=pool.pool_address, dex_label="Raydium CLMM",
            expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
            owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
            executable=False, data_length=len(final.pool_account.data),
            data_sha256=final.pool_account.data_sha256,
            data_bytes=final.pool_account.data,
        ),
        expected_pool_address=pool.pool_address,
    )
    assert decoded_pool == final.pool

    decoded_observation = decode_raydium_clmm_observation_state(
        DexPoolAccountEvidence(
            amm_key=observation.address, dex_label="Raydium CLMM Observation",
            expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
            owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
            executable=False, data_length=len(final.observation_account.data),
            data_sha256=final.observation_account.data_sha256,
            data_bytes=final.observation_account.data,
        ),
        expected_address=observation.address,
        expected_pool_address=pool.pool_address,
    )
    assert decoded_observation == final.observation

    decoded_array = decode_raydium_clmm_tick_array(
        address=tick_array.address, owner=RAYDIUM_CLMM_PROGRAM_ID,
        lamports=1, executable=False, data=final.tick_array_accounts[0].data,
        expected_pool_address=pool.pool_address, expected_start_tick_index=0,
        tick_spacing=1,
    )
    assert decoded_array == final.tick_arrays[0]


def test_finalization_rejects_incomplete_loop_and_wrong_original_account_bytes():
    (
        pool, pool_raw, tick_array, tick_raw, observation,
        observation_raw, execution, timestamp,
    ) = fixture()
    incomplete = execution.__class__(
        **{**execution.__dict__, "complete": False}
    ) if hasattr(execution, "__dict__") else None
    if incomplete is None:
        from dataclasses import replace
        incomplete = replace(execution, complete=False)
    with pytest.raises(RaydiumSwapFinalizationError, match="fully terminated"):
        finalize_raydium_swap_state(
            pool, pool_raw, incomplete,
            initial_observation=observation,
            observation_account_data=observation_raw,
            initial_tick_arrays=(tick_array,),
            tick_array_account_data=(tick_raw,),
            block_timestamp=timestamp,
        )

    with pytest.raises(RaydiumSwapFinalizationError, match="original PoolState bytes"):
        finalize_raydium_swap_state(
            pool, pool_raw[:-1] + bytes([pool_raw[-1] ^ 1]), execution,
            initial_observation=observation,
            observation_account_data=observation_raw,
            initial_tick_arrays=(tick_array,),
            tick_array_account_data=(tick_raw,),
            block_timestamp=timestamp,
        )
