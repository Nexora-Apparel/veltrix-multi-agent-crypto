"""Adversarial tests for source-pinned Raydium auxiliary account layouts."""
import base64
import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_aux_accounts import (
    AMM_CONFIG_DISCRIMINATOR,
    OBSERVATION_STATE_DISCRIMINATOR,
    AMM_CONFIG_DATA_LENGTH,
    OBSERVATION_STATE_DATA_LENGTH,
    RaydiumClmmAuxAccountError,
    RaydiumClmmAuxAccountPolicy,
    decode_raydium_clmm_amm_config,
    decode_raydium_clmm_observation_state,
    derive_raydium_clmm_amm_config_address,
    derive_raydium_clmm_observation_address,
    fetch_raydium_clmm_aux_account_snapshot,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RaydiumClmmPoolRouteCheck,
    RaydiumClmmPoolState,
)
from veltrix.dex_pool_evidence import DexPoolEvidenceSnapshot
from veltrix.raydium_clmm_vaults import RaydiumClmmVaultSnapshot
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTickArraySnapshot
from veltrix.simulation_gate import SimulationEvidence
from veltrix.solana_rpc_simulation import SolanaRpcSimulationAdapter


def config_fixture(index=7, tick_spacing=10):
    address, bump = derive_raydium_clmm_amm_config_address(index=index)
    owner = bytes(Pubkey.new_unique())
    fund_owner = bytes(Pubkey.new_unique())
    data = (
        AMM_CONFIG_DISCRIMINATOR
        + bytes((bump,))
        + index.to_bytes(2, "little")
        + owner
        + (1_000).to_bytes(4, "little")
        + (3_000).to_bytes(4, "little")
        + tick_spacing.to_bytes(2, "little")
        + (1_000).to_bytes(4, "little")
        + bytes(4)
        + fund_owner
        + bytes(24)
    )
    assert len(data) == AMM_CONFIG_DATA_LENGTH
    return address, data


def observation_fixture(*, now_ms=1_700_000_000_000, age=10, pool=None, index=4):
    pool = pool or str(Pubkey.new_unique())
    address = derive_raydium_clmm_observation_address(pool_address=pool)
    data = bytearray(OBSERVATION_STATE_DATA_LENGTH)
    data[:8] = OBSERVATION_STATE_DISCRIMINATOR
    data[8] = 1
    data[9:17] = (123).to_bytes(8, "little")
    data[17:19] = index.to_bytes(2, "little")
    data[19:51] = bytes(Pubkey.from_string(pool))
    timestamp = ((now_ms // 1000) - age) & 0xFFFF_FFFF
    offset = 51 + index * 44
    data[offset:offset + 4] = timestamp.to_bytes(4, "little")
    data[offset + 4:offset + 12] = (-1234).to_bytes(8, "little", signed=True)
    return address, pool, bytes(data)


def test_decodes_canonical_amm_config_and_fee_parameters():
    address, data = config_fixture()
    result = decode_raydium_clmm_amm_config(
        address=address, owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        lamports=1, executable=False, data=data,
    )
    assert result.address == address
    assert result.index == 7
    assert result.tick_spacing == 10
    assert result.trade_fee_rate == 3_000


@pytest.mark.parametrize("mutator,match", [
    (lambda data: bytes(8) + data[8:], "discriminator"),
    (lambda data: data[:-1], "117-byte layout"),
    (lambda data: data[:47] + (1_000_001).to_bytes(4, "little") + data[51:], "trade fee rate"),
    (lambda data: data[:53] + (1_000_001).to_bytes(4, "little") + data[57:], "fund plus protocol fee"),
])
def test_rejects_invalid_amm_config_layout_and_fee_policy(mutator, match):
    address, data = config_fixture()
    with pytest.raises(RaydiumClmmAuxAccountError, match=match):
        decode_raydium_clmm_amm_config(
            address=address, owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
            lamports=1, executable=False, data=mutator(data),
        )


def test_rejects_amm_config_wrong_owner_and_wrong_pda():
    address, data = config_fixture()
    with pytest.raises(RaydiumClmmAuxAccountError, match="owner"):
        decode_raydium_clmm_amm_config(
            address=address, owner_program_id=str(Pubkey.new_unique()),
            lamports=1, executable=False, data=data,
        )
    with pytest.raises(RaydiumClmmAuxAccountError, match="canonical index PDA"):
        decode_raydium_clmm_amm_config(
            address=str(Pubkey.new_unique()), owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
            lamports=1, executable=False, data=data,
        )


def test_decodes_pool_bound_fresh_observation_state():
    now_ms = 1_700_000_000_000
    address, pool, data = observation_fixture(now_ms=now_ms, age=15)
    result = decode_raydium_clmm_observation_state(
        address=address, expected_pool_address=pool,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
        executable=False, data=data, observed_at_epoch_ms=now_ms,
    )
    assert result.pool_address == pool
    assert result.observation_index == 4
    assert result.latest_tick_cumulative == -1234


def test_rejects_uninitialized_stale_future_or_wrong_pool_observation():
    now_ms = 1_700_000_000_000
    address, pool, data = observation_fixture(now_ms=now_ms, age=90_000)
    with pytest.raises(RaydiumClmmAuxAccountError, match="stale"):
        decode_raydium_clmm_observation_state(
            address=address, expected_pool_address=pool,
            owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
            executable=False, data=data, observed_at_epoch_ms=now_ms,
        )
    address, pool, data = observation_fixture(now_ms=now_ms, age=0)
    with pytest.raises(RaydiumClmmAuxAccountError, match="future"):
        decode_raydium_clmm_observation_state(
            address=address, expected_pool_address=pool,
            owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
            executable=False, data=data, observed_at_epoch_ms=now_ms - 10_000,
        )
    wrong_pool = str(Pubkey.new_unique())
    with pytest.raises(RaydiumClmmAuxAccountError, match="different pool"):
        decode_raydium_clmm_observation_state(
            address=address, expected_pool_address=wrong_pool,
            owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
            executable=False, data=data, observed_at_epoch_ms=now_ms,
        )
    uninitialized = bytearray(data)
    uninitialized[8] = 0
    with pytest.raises(RaydiumClmmAuxAccountError, match="not initialized"):
        decode_raydium_clmm_observation_state(
            address=address, expected_pool_address=pool,
            owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
            executable=False, data=bytes(uninitialized), observed_at_epoch_ms=now_ms,
        )


def test_rejects_invalid_observation_ring_index_and_wrong_owner():
    now_ms = 1_700_000_000_000
    address, pool, data = observation_fixture(now_ms=now_ms)
    invalid_index = bytearray(data)
    invalid_index[17:19] = (100).to_bytes(2, "little")
    with pytest.raises(RaydiumClmmAuxAccountError, match="ring index"):
        decode_raydium_clmm_observation_state(
            address=address, expected_pool_address=pool,
            owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
            executable=False, data=bytes(invalid_index), observed_at_epoch_ms=now_ms,
        )
    with pytest.raises(RaydiumClmmAuxAccountError, match="owner"):
        decode_raydium_clmm_observation_state(
            address=address, expected_pool_address=pool,
            owner_program_id=str(Pubkey.new_unique()), lamports=1,
            executable=False, data=data, observed_at_epoch_ms=now_ms,
        )


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, accounts, *, slot=504):
        self.accounts = accounts
        self.slot = slot
        self.calls = []

    def post(self, endpoint, *, json, headers, timeout, allow_redirects):
        self.calls.append(json)
        if json["method"] == "getGenesisHash":
            result = "genesis"
        elif json["method"] == "getMultipleAccounts":
            result = {
                "context": {"slot": self.slot},
                "value": [self.accounts.get(address) for address in json["params"][0]],
            }
        else:
            raise AssertionError(json["method"])
        return FakeResponse({"jsonrpc": "2.0", "id": json["id"], "result": result})


def fetch_fixture(*, slot=504, omit_observation=False):
    pool_address = str(Pubkey.new_unique())
    mint0, mint1 = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    config_address, config_bump = derive_raydium_clmm_amm_config_address(index=7)
    observation_address = derive_raydium_clmm_observation_address(pool_address=pool_address)
    pool = RaydiumClmmPoolState(
        pool_address=pool_address, bump=1, amm_config=config_address,
        pool_creator=str(Pubkey.new_unique()), token_mint_0=mint0,
        token_mint_1=mint1, token_vault_0=str(Pubkey.new_unique()),
        token_vault_1=str(Pubkey.new_unique()), observation_key=observation_address,
        mint_decimals_0=6, mint_decimals_1=6, tick_spacing=10,
        liquidity=1000, sqrt_price_x64=1 << 64, tick_current=0,
        status=0, fee_on=0, seed_index=0, account_data_sha256="a" * 64,
    )
    checks = (RaydiumClmmPoolRouteCheck(True, pool, mint0, mint1),)
    digest = hashlib.sha256(b"auxiliary-account-message").hexdigest()
    observed_at = 1_700_000_000_000
    simulation = SimulationEvidence(
        message_sha256=digest, recent_blockhash="blockhash",
        cluster="mainnet-beta", genesis_hash="genesis", provider_id="rpc-primary",
        response_id="sim-aux", observed_at_epoch_ms=observed_at, context_slot=500,
        current_block_height=900, last_valid_block_height=950, simulation_error=None,
        units_consumed=100_000, fee_lamports=5_000, token_balances=(),
    )
    pool_evidence = DexPoolEvidenceSnapshot(
        route_message_sha256=digest, registry_sha256="registry",
        simulation_response_id="sim-aux", simulation_context_slot=500,
        provider_id="rpc-primary", cluster="mainnet-beta", genesis_hash="genesis",
        context_slot=501, accounts=(),
    )
    vaults = RaydiumClmmVaultSnapshot(
        route_message_sha256=digest, simulation_response_id="sim-aux",
        simulation_context_slot=500, provider_id="rpc-primary",
        cluster="mainnet-beta", genesis_hash="genesis", context_slot=502, vaults=(),
    )
    ticks = RaydiumClmmTickArraySnapshot(
        route_message_sha256=digest, simulation_response_id="sim-aux",
        simulation_context_slot=500, provider_id="rpc-primary",
        cluster="mainnet-beta", genesis_hash="genesis", context_slot=503, tick_arrays=(),
    )
    config_data = (
        AMM_CONFIG_DISCRIMINATOR + bytes((config_bump,))
        + (7).to_bytes(2, "little") + bytes(Pubkey.new_unique())
        + (1000).to_bytes(4, "little") + (3000).to_bytes(4, "little")
        + (10).to_bytes(2, "little") + (1000).to_bytes(4, "little")
        + bytes(4) + bytes(Pubkey.new_unique()) + bytes(24)
    )
    obs_data = bytearray(OBSERVATION_STATE_DATA_LENGTH)
    obs_data[:8] = OBSERVATION_STATE_DISCRIMINATOR
    obs_data[8] = 1
    obs_data[9:17] = (123).to_bytes(8, "little")
    obs_data[17:19] = (0).to_bytes(2, "little")
    obs_data[19:51] = bytes(Pubkey.from_string(pool_address))
    latest_timestamp = (observed_at // 1000 - 15) & 0xFFFF_FFFF
    obs_data[51:55] = latest_timestamp.to_bytes(4, "little")
    obs_data = bytes(obs_data)
    def account(data):
        return {
            "owner": RAYDIUM_CLMM_PROGRAM_ID, "lamports": 1,
            "executable": False, "data": [base64.b64encode(data).decode(), "base64"],
            "space": len(data),
        }
    accounts = {config_address: account(config_data)}
    if not omit_observation:
        accounts[observation_address] = account(obs_data)
    session = FakeSession(accounts, slot=slot)
    adapter = SolanaRpcSimulationAdapter(
        endpoint="https://rpc.example.test", provider_id="rpc-primary",
        cluster="mainnet-beta", expected_genesis_hash="genesis",
        allowed_hosts=("rpc.example.test",), session=session, clock_ms=lambda: observed_at,
    )
    return pool_evidence, checks, vaults, ticks, simulation, adapter, session, pool


def test_fetches_config_and_observation_after_all_prior_evidence_slots():
    values = fetch_fixture()
    result = fetch_raydium_clmm_aux_account_snapshot(
        values[0], pool_checks=values[1], vault_snapshot=values[2],
        tick_array_snapshot=values[3], simulation=values[4], rpc_adapter=values[5],
    )
    assert result.execution_ready is False
    assert result.context_slot == 504
    assert result.amm_configs[0].tick_spacing == 10
    assert result.observations[0].pool_address == values[7].pool_address
    request = next(call for call in values[6].calls if call["method"] == "getMultipleAccounts")
    assert request["params"][1]["minContextSlot"] == 503


def test_rejects_missing_auxiliary_account_and_stale_snapshot_slot():
    values = fetch_fixture(omit_observation=True)
    with pytest.raises(Exception, match="missing from the requested snapshot"):
        fetch_raydium_clmm_aux_account_snapshot(
            values[0], pool_checks=values[1], vault_snapshot=values[2],
            tick_array_snapshot=values[3], simulation=values[4], rpc_adapter=values[5],
        )
    values = fetch_fixture(slot=502)
    with pytest.raises(Exception, match="older than the required simulation slot"):
        fetch_raydium_clmm_aux_account_snapshot(
            values[0], pool_checks=values[1], vault_snapshot=values[2],
            tick_array_snapshot=values[3], simulation=values[4], rpc_adapter=values[5],
        )
