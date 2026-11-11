"""End-to-end tests for Raydium CLMM reward/oracle and account serialization."""
import hashlib
import struct
from dataclasses import replace

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
from veltrix.raydium_clmm_swap_parity import (
    RaydiumSwapParityError,
    reconcile_raydium_swap_simulation,
)
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
from veltrix.raydium_clmm_vaults import (
    CLASSIC_SPL_TOKEN_PROGRAM_ID,
    RaydiumClmmVaultAccount,
    RaydiumClmmVaultSnapshot,
)
from veltrix.simulation_gate import (
    SimulatedAccountState,
    SimulatedTokenBalance,
    SimulationEvidence,
)


def fixture(*, zero_for_one=False):
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
    current_tick = 10 if zero_for_one else 0
    pool_data[253:269] = get_sqrt_price_at_tick(current_tick).to_bytes(16, "little")
    struct.pack_into("<i", pool_data, 269, current_tick)
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
    next_tick = 0 if zero_for_one else 10
    tick_offset = 44 + next_tick * 168
    struct.pack_into("<i", tick_data, tick_offset, next_tick)
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
        passed=True, pool_address=pool_address, zero_for_one=zero_for_one,
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
        zero_for_one=zero_for_one, is_base_input=True, block_timestamp=timestamp,
    )
    execution = execute_raydium_swap_loop(
        state, tick_arrays=(tick_array,), traversal=traversal,
        sqrt_price_limit_x64=get_sqrt_price_at_tick(next_tick),
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


def parity_fixture(*, zero_for_one=False):
    (
        pool, pool_raw, tick_array, tick_raw, observation,
        observation_raw, execution, timestamp,
    ) = fixture(zero_for_one=zero_for_one)
    finalized = finalize_raydium_swap_state(
        pool, pool_raw, execution,
        initial_observation=observation,
        observation_account_data=observation_raw,
        initial_tick_arrays=(tick_array,),
        tick_array_account_data=(tick_raw,),
        block_timestamp=timestamp,
    )
    message_sha = "c" * 64
    account_pairs = (
        (pool.pool_address, pool_raw, finalized.pool_account.data),
        (observation.address, observation_raw, finalized.observation_account.data),
        (tick_array.address, tick_raw, finalized.tick_array_accounts[0].data),
    )
    account_states = tuple(
        SimulatedAccountState(
            address=address,
            pre_owner=RAYDIUM_CLMM_PROGRAM_ID,
            post_owner=RAYDIUM_CLMM_PROGRAM_ID,
            pre_lamports=1,
            post_lamports=1,
            pre_executable=False,
            post_executable=False,
            pre_data=pre_data,
            post_data=post_data,
            pre_data_sha256=hashlib.sha256(pre_data).hexdigest(),
            post_data_sha256=hashlib.sha256(post_data).hexdigest(),
        )
        for address, pre_data, post_data in account_pairs
    )
    pre0 = pre1 = 10_000
    if execution.state.zero_for_one:
        delta0 = finalized.settlement.amount_0
        delta1 = -finalized.settlement.amount_1
    else:
        delta0 = -finalized.settlement.amount_0
        delta1 = finalized.settlement.amount_1
    balances = (
        SimulatedTokenBalance(
            token_account=pool.token_vault_0,
            mint=pool.token_mint_0,
            owner=pool.pool_address,
            pre_amount_raw=pre0,
            post_amount_raw=pre0 + delta0,
            token_program_id=CLASSIC_SPL_TOKEN_PROGRAM_ID,
        ),
        SimulatedTokenBalance(
            token_account=pool.token_vault_1,
            mint=pool.token_mint_1,
            owner=pool.pool_address,
            pre_amount_raw=pre1,
            post_amount_raw=pre1 + delta1,
            token_program_id=CLASSIC_SPL_TOKEN_PROGRAM_ID,
        ),
    )
    vaults = (
        RaydiumClmmVaultAccount(
            address=pool.token_vault_0, pool_address=pool.pool_address,
            mint=pool.token_mint_0, token_program_id=CLASSIC_SPL_TOKEN_PROGRAM_ID,
            token_authority=pool.pool_address, amount_raw=pre0, state=1,
            lamports=1, data_sha256="d" * 64,
        ),
        RaydiumClmmVaultAccount(
            address=pool.token_vault_1, pool_address=pool.pool_address,
            mint=pool.token_mint_1, token_program_id=CLASSIC_SPL_TOKEN_PROGRAM_ID,
            token_authority=pool.pool_address, amount_raw=pre1, state=1,
            lamports=1, data_sha256="e" * 64,
        ),
    )
    simulation = SimulationEvidence(
        message_sha256=message_sha,
        recent_blockhash="recent-blockhash",
        cluster="mainnet-beta",
        genesis_hash="genesis",
        provider_id="rpc-primary",
        response_id="sim-response-1",
        observed_at_epoch_ms=1_700_000_000_000,
        context_slot=500,
        current_block_height=900,
        last_valid_block_height=950,
        simulation_error=None,
        units_consumed=100_000,
        fee_lamports=5_000,
        token_balances=balances,
        account_states=account_states,
    )
    vault_snapshot = RaydiumClmmVaultSnapshot(
        route_message_sha256=message_sha,
        simulation_response_id=simulation.response_id,
        simulation_context_slot=simulation.context_slot,
        provider_id=simulation.provider_id,
        cluster=simulation.cluster,
        genesis_hash=simulation.genesis_hash,
        context_slot=simulation.context_slot,
        vaults=vaults,
        execution_ready=False,
    )
    return (
        pool, pool_raw, tick_array, tick_raw, observation, observation_raw, execution, finalized,
        simulation, vault_snapshot, message_sha,
    )


def test_simulation_reconciles_exact_account_bytes_and_both_pool_vault_deltas():
    (
        pool, pool_raw, tick_array, tick_raw, observation, observation_raw, execution, finalized,
        simulation, vault_snapshot, message_sha,
    ) = parity_fixture()
    report = reconcile_raydium_swap_simulation(
        pool, execution, finalized, simulation, vault_snapshot,
        expected_message_sha256=message_sha,
        initial_pool_account_data=pool_raw,
        initial_observation=observation,
        initial_observation_account_data=observation_raw,
        initial_tick_arrays=(tick_array,),
        initial_tick_array_account_data=(tick_raw,),
    )
    assert report.passed is True
    assert report.context_slot == 500
    assert len(report.account_comparisons) == 3
    assert all(item.exact_post_state_match for item in report.account_comparisons)
    assert len(report.vault_reconciliations) == 2
    assert all(item.reconciled for item in report.vault_reconciliations)


def test_simulation_parity_fails_closed_on_account_bytes_or_vault_delta_mismatch():
    (
        pool, pool_raw, tick_array, tick_raw, observation, observation_raw, execution, finalized,
        simulation, vault_snapshot, message_sha,
    ) = parity_fixture()
    first = simulation.account_states[0]
    corrupted_data = first.post_data[:-1] + bytes([first.post_data[-1] ^ 1])
    corrupted = replace(
        first,
        post_data=corrupted_data,
        post_data_sha256=hashlib.sha256(corrupted_data).hexdigest(),
    )
    bad_account_simulation = replace(
        simulation, account_states=(corrupted,) + simulation.account_states[1:]
    )
    with pytest.raises(RaydiumSwapParityError, match="post-state bytes"):
        reconcile_raydium_swap_simulation(
            pool, execution, finalized, bad_account_simulation, vault_snapshot,
            expected_message_sha256=message_sha,
            initial_pool_account_data=pool_raw,
            initial_observation=observation,
            initial_observation_account_data=observation_raw,
            initial_tick_arrays=(tick_array,),
            initial_tick_array_account_data=(tick_raw,),
        )

    first_balance = simulation.token_balances[0]
    bad_balance = replace(first_balance, post_amount_raw=first_balance.post_amount_raw + 1)
    bad_vault_simulation = replace(
        simulation, token_balances=(bad_balance, simulation.token_balances[1])
    )
    with pytest.raises(RaydiumSwapParityError, match="post balance differs"):
        reconcile_raydium_swap_simulation(
            pool, execution, finalized, bad_vault_simulation, vault_snapshot,
            expected_message_sha256=message_sha,
            initial_pool_account_data=pool_raw,
            initial_observation=observation,
            initial_observation_account_data=observation_raw,
            initial_tick_arrays=(tick_array,),
            initial_tick_array_account_data=(tick_raw,),
        )


def test_zero_for_one_simulation_reconciles_opposite_pool_vault_directions():
    (
        pool, pool_raw, tick_array, tick_raw, observation, observation_raw,
        execution, finalized, simulation, vault_snapshot, message_sha,
    ) = parity_fixture(zero_for_one=True)
    assert execution.state.zero_for_one is True
    report = reconcile_raydium_swap_simulation(
        pool, execution, finalized, simulation, vault_snapshot,
        expected_message_sha256=message_sha,
        initial_pool_account_data=pool_raw,
        initial_observation=observation,
        initial_observation_account_data=observation_raw,
        initial_tick_arrays=(tick_array,),
        initial_tick_array_account_data=(tick_raw,),
    )
    assert report.passed is True
    assert report.vault_reconciliations[0].expected_delta_raw > 0
    assert report.vault_reconciliations[1].expected_delta_raw < 0
