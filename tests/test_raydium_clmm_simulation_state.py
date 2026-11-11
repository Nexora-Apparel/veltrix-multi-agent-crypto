"""Adversarial tests for Raydium CLMM simulation pre/post state binding."""
import dataclasses
import hashlib

import pytest
from solders.pubkey import Pubkey

from test_raydium_clmm_pool_state import fixture as pool_fixture
from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET,
    RaydiumClmmPoolRouteCheck,
    decode_raydium_clmm_pool_state,
)
from veltrix.raydium_clmm_simulation_state import (
    RaydiumClmmSimulationStateError,
    verify_raydium_clmm_simulation_state_transition,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_DATA_LENGTH,
    RAYDIUM_TICK_ARRAY_DISCRIMINATOR,
    RaydiumClmmTickArraySnapshot,
    decode_raydium_clmm_tick_array,
    derive_raydium_clmm_tick_array_address,
)
from veltrix.simulation_gate import SimulatedAccountState, SimulationEvidence


def tick_array_bytes(pool_address, start=0):
    data = bytearray(RAYDIUM_TICK_ARRAY_DATA_LENGTH)
    data[:8] = RAYDIUM_TICK_ARRAY_DISCRIMINATOR
    data[8:40] = bytes(Pubkey.from_string(pool_address))
    data[40:44] = start.to_bytes(4, "little", signed=True)
    data[44:48] = start.to_bytes(4, "little", signed=True)
    data[64:80] = (1).to_bytes(16, "little")
    data[10_124] = 1
    data[10_125:10_133] = (1).to_bytes(8, "little")
    return bytes(data)


def account_state(address, pre_data, post_data, *, owner=RAYDIUM_CLMM_PROGRAM_ID):
    return SimulatedAccountState(
        address=address,
        pre_owner=owner,
        post_owner=owner,
        pre_lamports=1_000_000,
        post_lamports=1_000_000,
        pre_executable=False,
        post_executable=False,
        pre_data=pre_data,
        post_data=post_data,
        pre_data_sha256=hashlib.sha256(pre_data).hexdigest(),
        post_data_sha256=hashlib.sha256(post_data).hexdigest(),
    )


def fixture(*, post_pool_mutator=None, post_tick_mutator=None, include_pool=True, include_tick=True):
    raw, evidence, pool_evidence, _, order, pool_address, mint_a, mint_b = pool_fixture()
    pool_raw = bytearray(raw)
    bitmap = [0] * 16
    bitmap[8] = 1
    for index, word in enumerate(bitmap):
        offset = RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET + index * 8
        pool_raw[offset:offset + 8] = word.to_bytes(8, "little")
    pool_raw = bytes(pool_raw)
    pool_hash = hashlib.sha256(pool_raw).hexdigest()
    evidence = dataclasses.replace(
        evidence, data_bytes=pool_raw, data_length=len(pool_raw), data_sha256=pool_hash
    )
    pool_evidence = dataclasses.replace(
        pool_evidence, accounts=(evidence,), context_slot=500
    )
    pool = decode_raydium_clmm_pool_state(evidence, expected_pool_address=pool_address)
    pool_check = RaydiumClmmPoolRouteCheck(True, pool, mint_a, mint_b)

    tick_address = derive_raydium_clmm_tick_array_address(
        pool_address=pool_address, start_tick_index=0
    )
    tick_pre = tick_array_bytes(pool_address)
    tick_decoded = decode_raydium_clmm_tick_array(
        address=tick_address, owner=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
        executable=False, data=tick_pre, expected_pool_address=pool_address,
        expected_start_tick_index=0, tick_spacing=10, current_tick=0,
    )
    tick_snapshot = RaydiumClmmTickArraySnapshot(
        route_message_sha256=pool_evidence.route_message_sha256,
        simulation_response_id="sim-1", simulation_context_slot=500,
        provider_id="rpc-primary", cluster="mainnet-beta", genesis_hash="genesis",
        context_slot=500, tick_arrays=(tick_decoded,),
        cpi_account_order=(tick_address,),
    )

    pool_post = bytearray(pool_raw)
    pool_post[253:269] = ((1 << 64) + 1).to_bytes(16, "little")
    pool_post[269:273] = (1).to_bytes(4, "little", signed=True)
    if post_pool_mutator:
        post_pool_mutator(pool_post)
    pool_post = bytes(pool_post)

    tick_post = bytearray(tick_pre)
    tick_post[80:96] = (1).to_bytes(16, "little")
    if post_tick_mutator:
        post_tick_mutator(tick_post)
    tick_post = bytes(tick_post)

    states = []
    if include_pool:
        states.append(account_state(pool_address, pool_raw, pool_post))
    if include_tick:
        states.append(account_state(tick_address, tick_pre, tick_post))
    simulation = SimulationEvidence(
        message_sha256=pool_evidence.route_message_sha256,
        recent_blockhash=order.recent_blockhash,
        cluster="mainnet-beta", genesis_hash="genesis", provider_id="rpc-primary",
        response_id="sim-1", observed_at_epoch_ms=1000, context_slot=500,
        current_block_height=900, last_valid_block_height=950,
        simulation_error=None, units_consumed=100_000, fee_lamports=5000,
        token_balances=(), account_states=tuple(states),
    )
    return order, pool_check, pool_evidence, tick_snapshot, simulation


def verify(values):
    order, pool_check, pool_evidence, ticks, simulation = values
    return verify_raydium_clmm_simulation_state_transition(
        order, pool_check=pool_check, pool_evidence=pool_evidence,
        tick_array_snapshot=ticks, simulation=simulation,
    )


def test_accepts_same_order_pre_post_pool_and_tick_array_state():
    result = verify(fixture())
    assert result.passed is True
    assert result.pre_pool.tick_current == 0
    assert result.post_pool.tick_current == 1
    assert result.pre_pool_sha256 != result.post_pool_sha256
    assert result.changed_tick_array_addresses
    assert result.execution_ready is False


def test_rejects_missing_pool_or_tick_array_simulation_state():
    with pytest.raises(RaydiumClmmSimulationStateError, match="omitted required"):
        verify(fixture(include_pool=False))
    with pytest.raises(RaydiumClmmSimulationStateError, match="omitted required"):
        verify(fixture(include_tick=False))


def test_rejects_pre_state_that_differs_from_owner_verified_pool_snapshot():
    values = list(fixture())
    order, pool_check, pool_evidence, ticks, simulation = values
    pool_state = simulation.account_states[0]
    changed_pre = bytearray(pool_state.pre_data)
    changed_pre[253:269] = ((1 << 64) + 99).to_bytes(16, "little")
    changed_pre = bytes(changed_pre)
    changed_state = dataclasses.replace(
        pool_state, pre_data=changed_pre,
        pre_data_sha256=hashlib.sha256(changed_pre).hexdigest(),
    )
    simulation = dataclasses.replace(
        simulation, account_states=(changed_state, simulation.account_states[1])
    )
    values[4] = simulation
    with pytest.raises(RaydiumClmmSimulationStateError, match="pre-state differs"):
        verify(tuple(values))


def test_rejects_pool_configuration_mutation_during_simulated_swap():
    def change_spacing(raw):
        raw[235:237] = (20).to_bytes(2, "little")
    with pytest.raises(RaydiumClmmSimulationStateError, match="immutable PoolState"):
        verify(fixture(post_pool_mutator=change_spacing))


def test_rejects_tick_position_liquidity_mutation_during_swap():
    def change_liquidity(raw):
        raw[64:80] = (2).to_bytes(16, "little")
    with pytest.raises(RaydiumClmmSimulationStateError, match="position liquidity"):
        verify(fixture(post_tick_mutator=change_liquidity))


def test_rejects_simulation_that_did_not_mutate_pool_state():
    values = list(fixture())
    order, pool_check, pool_evidence, ticks, simulation = values
    pool_state = simulation.account_states[0]
    unchanged = dataclasses.replace(
        pool_state,
        post_data=pool_state.pre_data,
        post_data_sha256=pool_state.pre_data_sha256,
    )
    values[4] = dataclasses.replace(
        simulation, account_states=(unchanged, simulation.account_states[1])
    )
    with pytest.raises(RaydiumClmmSimulationStateError, match="no PoolState mutation"):
        verify(tuple(values))
