"""Adversarial tests for Raydium CLMM pool/config/observation/tick/vault verification."""
import dataclasses
import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_swap_effects import CLASSIC_TOKEN_PROGRAM_ID, derive_associated_token_address
from veltrix.raydium_clmm_cpi import (
    RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RaydiumClmmCpiResult,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_POOL_STATE_LEN,
    RAYDIUM_AMM_CONFIG_LEN,
    RAYDIUM_OBSERVATION_STATE_LEN,
    RAYDIUM_TICK_ARRAY_STATE_LEN,
    RaydiumPoolStateError,
    RaydiumPoolStatePolicy,
    evaluate_raydium_pool_state,
)
from veltrix.simulation_gate import (
    SimulatedTokenBalance,
    SimulationAccountSnapshot,
    SimulationEvidence,
)


def disc(name):
    return hashlib.sha256(("account:" + name).encode("ascii")).digest()[:8]


def token_data(mint, authority, amount):
    raw = bytearray(165)
    raw[0:32] = bytes(Pubkey.from_string(mint))
    raw[32:64] = bytes(Pubkey.from_string(authority))
    raw[64:72] = int(amount).to_bytes(8, "little")
    return bytes(raw)


def fixture():
    taker_key = Pubkey.new_unique()
    mint_keys = sorted((Pubkey.new_unique(), Pubkey.new_unique()), key=bytes)
    mint0, mint1 = mint_keys
    taker = str(taker_key)
    mint0_s, mint1_s = str(mint0), str(mint1)
    amm_config = Pubkey.new_unique()
    vault0, vault1 = Pubkey.new_unique(), Pubkey.new_unique()
    observation = Pubkey.new_unique()
    pool, bump = Pubkey.find_program_address(
        [b"pool", bytes(amm_config), bytes(mint0), bytes(mint1), b""],
        Pubkey.from_string(RAYDIUM_CLMM_MAINNET_PROGRAM_ID),
    )
    tick_start = 0
    tick_array, _ = Pubkey.find_program_address(
        [b"tick_array", bytes(pool), tick_start.to_bytes(4, "big", signed=True)],
        Pubkey.from_string(RAYDIUM_CLMM_MAINNET_PROGRAM_ID),
    )
    input_ata = derive_associated_token_address(taker, mint0_s)
    output_ata = derive_associated_token_address(taker, mint1_s)

    pool_data = bytearray(RAYDIUM_POOL_STATE_LEN)
    pool_data[0:8] = disc("PoolState")
    pool_data[8] = bump
    pool_data[9:41] = bytes(amm_config)
    pool_data[73:105] = bytes(mint0)
    pool_data[105:137] = bytes(mint1)
    pool_data[137:169] = bytes(vault0)
    pool_data[169:201] = bytes(vault1)
    pool_data[201:233] = bytes(observation)
    pool_data[233] = 6
    pool_data[234] = 9
    pool_data[235:237] = (10).to_bytes(2, "little")
    pool_data[389] = 0
    pool_data[391:393] = bytes(2)
    pool_data = bytes(pool_data)

    config_data = bytearray(RAYDIUM_AMM_CONFIG_LEN)
    config_data[0:8] = disc("AmmConfig")
    config_data[51:53] = (10).to_bytes(2, "little")
    config_data = bytes(config_data)

    observation_data = bytearray(RAYDIUM_OBSERVATION_STATE_LEN)
    observation_data[0:8] = disc("ObservationState")
    observation_data[8] = 1
    observation_data[19:51] = bytes(pool)
    observation_data = bytes(observation_data)

    tick_data = bytearray(RAYDIUM_TICK_ARRAY_STATE_LEN)
    tick_data[0:8] = disc("TickArrayState")
    tick_data[8:40] = bytes(pool)
    tick_data[40:44] = tick_start.to_bytes(4, "little", signed=True)
    tick_data = bytes(tick_data)

    input_vault_pre = token_data(mint0_s, str(pool), 100_000)
    input_vault_post = token_data(mint0_s, str(pool), 101_000)
    output_vault_pre = token_data(mint1_s, str(pool), 200_000)
    output_vault_post = token_data(mint1_s, str(pool), 198_000)
    user_input_pre = token_data(mint0_s, taker, 5_000)
    user_input_post = token_data(mint0_s, taker, 4_000)
    user_output_pre = token_data(mint1_s, taker, 100)
    user_output_post = token_data(mint1_s, taker, 2_100)

    addresses = {
        "pool": str(pool),
        "config": str(amm_config),
        "observation": str(observation),
        "tick": str(tick_array),
        "vault0": str(vault0),
        "vault1": str(vault1),
        "input_ata": input_ata,
        "output_ata": output_ata,
    }
    snapshots = (
        SimulationAccountSnapshot(addresses["pool"], True, RAYDIUM_CLMM_MAINNET_PROGRAM_ID, False, 1, pool_data, True, RAYDIUM_CLMM_MAINNET_PROGRAM_ID, False, 1, pool_data),
        SimulationAccountSnapshot(addresses["config"], True, RAYDIUM_CLMM_MAINNET_PROGRAM_ID, False, 1, config_data, True, RAYDIUM_CLMM_MAINNET_PROGRAM_ID, False, 1, config_data),
        SimulationAccountSnapshot(addresses["observation"], True, RAYDIUM_CLMM_MAINNET_PROGRAM_ID, False, 1, observation_data, True, RAYDIUM_CLMM_MAINNET_PROGRAM_ID, False, 1, observation_data),
        SimulationAccountSnapshot(addresses["tick"], True, RAYDIUM_CLMM_MAINNET_PROGRAM_ID, False, 1, tick_data, True, RAYDIUM_CLMM_MAINNET_PROGRAM_ID, False, 1, tick_data),
        SimulationAccountSnapshot(addresses["vault0"], True, CLASSIC_TOKEN_PROGRAM_ID, False, 1, input_vault_pre, True, CLASSIC_TOKEN_PROGRAM_ID, False, 1, input_vault_post),
        SimulationAccountSnapshot(addresses["vault1"], True, CLASSIC_TOKEN_PROGRAM_ID, False, 1, output_vault_pre, True, CLASSIC_TOKEN_PROGRAM_ID, False, 1, output_vault_post),
        SimulationAccountSnapshot(addresses["input_ata"], True, CLASSIC_TOKEN_PROGRAM_ID, False, 1, user_input_pre, True, CLASSIC_TOKEN_PROGRAM_ID, False, 1, user_input_post),
        SimulationAccountSnapshot(addresses["output_ata"], True, CLASSIC_TOKEN_PROGRAM_ID, False, 1, user_output_pre, True, CLASSIC_TOKEN_PROGRAM_ID, False, 1, user_output_post),
    )
    message = b"jupiter-raydium-pool-state-message"
    digest = hashlib.sha256(message).hexdigest()
    order = JupiterOrderTransaction(
        provider_id="jupiter-swap-v2",
        request_id="order-1",
        quote_id="quote-1",
        observed_at_epoch_ms=1_000_000,
        taker=taker,
        fee_payer=taker,
        input_mint=mint0_s,
        output_mint=mint1_s,
        input_amount_raw=1_000,
        output_amount_raw=2_000,
        minimum_output_amount_raw=1_950,
        price_impact_pct=0.1,
        fee_bps=0.0,
        router="metis",
        transaction_version=0,
        last_valid_block_height=950,
        recent_blockhash="blockhash",
        message_bytes=message,
        serialized_transaction_bytes=b"unsigned",
        route_plan=(
            JupiterRouteStep(
                amm_key=addresses["pool"],
                label="Raydium CLMM",
                input_mint=mint0_s,
                output_mint=mint1_s,
                input_amount_raw=1_000,
                output_amount_raw=2_000,
                percent=100.0,
                bps=10_000,
            ),
        ),
        signature_fee_lamports=5_000,
        prioritization_fee_lamports=0,
        rent_fee_lamports=0,
    )
    cpi = RaydiumClmmCpiResult(
        passed=True,
        message_sha256=digest,
        program_id=RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
        outer_instruction_index=0,
        cpi_instruction_index=0,
        payer=taker,
        amm_config=addresses["config"],
        input_token_account=input_ata,
        output_token_account=output_ata,
        input_vault=addresses["vault0"],
        output_vault=addresses["vault1"],
        observation_state=addresses["observation"],
        tick_array=addresses["tick"],
        pool_state=addresses["pool"],
        amount_raw=1_000,
        minimum_output_raw=1_980,
        sqrt_price_limit_x64=0,
        is_base_input=True,
        stack_height=2,
    )
    simulation = SimulationEvidence(
        message_sha256=digest,
        recent_blockhash="blockhash",
        cluster="mainnet-beta",
        genesis_hash="genesis",
        provider_id="rpc-primary",
        response_id="simulation-1",
        observed_at_epoch_ms=1_001_000,
        context_slot=500,
        current_block_height=900,
        last_valid_block_height=950,
        simulation_error=None,
        units_consumed=100_000,
        fee_lamports=5_000,
        token_balances=(
            SimulatedTokenBalance(input_ata, mint0_s, taker, 5_000, 4_000, CLASSIC_TOKEN_PROGRAM_ID),
            SimulatedTokenBalance(output_ata, mint1_s, taker, 100, 2_100, CLASSIC_TOKEN_PROGRAM_ID),
        ),
        account_snapshots=snapshots,
    )
    return order, cpi, simulation, addresses


def evaluate(values=None, policy=None):
    order, cpi, simulation, _ = values or fixture()
    return evaluate_raydium_pool_state(
        order,
        cpi=cpi,
        simulation=simulation,
        policy=policy or RaydiumPoolStatePolicy(),
    )


def test_accepts_pool_config_observation_tick_and_vaults_matching_cpi():
    result = evaluate()
    assert result.passed is True
    assert result.tick_spacing == 10
    assert result.input_vault_delta_raw == 1_000
    assert result.output_vault_delta_raw == -2_000
    assert result.source_commit == RAYDIUM_CLMM_SOURCE_COMMIT


def test_rejects_pool_state_with_wrong_discriminator_or_owner():
    values = list(fixture())
    order, cpi, simulation, addresses = values
    snapshots = list(simulation.account_snapshots)
    index = next(i for i, x in enumerate(snapshots) if x.address == addresses["pool"])
    snapshots[index] = dataclasses.replace(snapshots[index], pre_data=b"bad" + snapshots[index].pre_data[3:])
    values[2] = dataclasses.replace(simulation, account_snapshots=tuple(snapshots))
    with pytest.raises(RaydiumPoolStateError, match="discriminator"):
        evaluate(tuple(values))

    values = list(fixture())
    order, cpi, simulation, addresses = values
    snapshots = list(simulation.account_snapshots)
    index = next(i for i, x in enumerate(snapshots) if x.address == addresses["pool"])
    snapshots[index] = dataclasses.replace(snapshots[index], pre_owner=CLASSIC_TOKEN_PROGRAM_ID)
    values[2] = dataclasses.replace(simulation, account_snapshots=tuple(snapshots))
    with pytest.raises(RaydiumPoolStateError, match="owner or executable"):
        evaluate(tuple(values))


def test_rejects_swaps_disabled_and_noncanonical_pool_identity():
    values = list(fixture())
    simulation = values[2]
    snapshots = list(simulation.account_snapshots)
    index = next(i for i, x in enumerate(snapshots) if x.address == values[3]["pool"])
    data = bytearray(snapshots[index].pre_data)
    data[389] = 1 << 4
    snapshots[index] = dataclasses.replace(snapshots[index], pre_data=bytes(data), post_data=bytes(data))
    values[2] = dataclasses.replace(simulation, account_snapshots=tuple(snapshots))
    with pytest.raises(RaydiumPoolStateError, match="swaps disabled"):
        evaluate(tuple(values))


def test_rejects_wrong_config_observation_or_tick_pool_relationship():
    for target, offset in (("config", 51), ("observation", 19), ("tick", 8)):
        values = list(fixture())
        simulation = values[2]
        snapshots = list(simulation.account_snapshots)
        index = next(i for i, x in enumerate(snapshots) if x.address == values[3][target])
        data = bytearray(snapshots[index].pre_data)
        data[offset:offset + 2] = b"\x01\x00" if target == "config" else data[offset:offset + 2]
        if target != "config":
            data[offset:offset + 32] = bytes(Pubkey.new_unique())
        snapshots[index] = dataclasses.replace(snapshots[index], pre_data=bytes(data), post_data=bytes(data))
        values[2] = dataclasses.replace(simulation, account_snapshots=tuple(snapshots))
        with pytest.raises(RaydiumPoolStateError):
            evaluate(tuple(values))


def test_rejects_wrong_vault_mint_and_wrong_vault_deltas():
    values = list(fixture())
    simulation = values[2]
    snapshots = list(simulation.account_snapshots)
    index = next(i for i, x in enumerate(snapshots) if x.address == values[3]["vault0"])
    raw = bytearray(snapshots[index].pre_data)
    raw[0:32] = bytes(Pubkey.new_unique())
    snapshots[index] = dataclasses.replace(snapshots[index], pre_data=bytes(raw))
    values[2] = dataclasses.replace(simulation, account_snapshots=tuple(snapshots))
    with pytest.raises(RaydiumPoolStateError, match="vault mint"):
        evaluate(tuple(values))

    values = list(fixture())
    simulation = values[2]
    snapshots = list(simulation.account_snapshots)
    index = next(i for i, x in enumerate(snapshots) if x.address == values[3]["vault0"])
    raw = bytearray(snapshots[index].post_data)
    raw[64:72] = (100_500).to_bytes(8, "little")
    snapshots[index] = dataclasses.replace(snapshots[index], post_data=bytes(raw))
    values[2] = dataclasses.replace(simulation, account_snapshots=tuple(snapshots))
    with pytest.raises(RaydiumPoolStateError, match="vault balance deltas"):
        evaluate(tuple(values))


def test_rejects_user_token_account_authority_or_balance_mismatch():
    values = list(fixture())
    simulation = values[2]
    snapshots = list(simulation.account_snapshots)
    index = next(i for i, x in enumerate(snapshots) if x.address == values[3]["input_ata"])
    raw = bytearray(snapshots[index].pre_data)
    raw[32:64] = bytes(Pubkey.new_unique())
    snapshots[index] = dataclasses.replace(snapshots[index], pre_data=bytes(raw))
    values[2] = dataclasses.replace(simulation, account_snapshots=tuple(snapshots))
    with pytest.raises(RaydiumPoolStateError, match="authority"):
        evaluate(tuple(values))


def test_rejects_missing_account_snapshot_or_digest_mismatch():
    values = list(fixture())
    simulation = values[2]
    snapshots = tuple(x for x in simulation.account_snapshots if x.address != values[3]["pool"])
    values[2] = dataclasses.replace(simulation, account_snapshots=snapshots)
    with pytest.raises(RaydiumPoolStateError, match="omitted the pool state"):
        evaluate(tuple(values))

    values = list(fixture())
    values[2] = dataclasses.replace(values[2], message_sha256="b" * 64)
    with pytest.raises(RaydiumPoolStateError, match="digests differ"):
        evaluate(tuple(values))


def test_rejects_route_pool_key_mismatch_and_invalid_tick_spacing():
    values = list(fixture())
    order = dataclasses.replace(values[0], route_plan=(dataclasses.replace(values[0].route_plan[0], amm_key=str(Pubkey.new_unique())),))
    values[0] = order
    with pytest.raises(RaydiumPoolStateError, match="AMM key differs"):
        evaluate(tuple(values))
