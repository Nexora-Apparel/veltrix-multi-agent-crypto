"""Adversarial tests for Raydium CLMM current tick-array validation."""
import base64
import dataclasses
import hashlib
import struct

import pytest
from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolEvidenceSnapshot
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolRouteCheck, RaydiumClmmPoolState
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_DATA_LENGTH,
    RAYDIUM_TICK_ARRAY_DISCRIMINATOR,
    RaydiumClmmTickArrayError,
    decode_raydium_clmm_tick_array,
    derive_raydium_clmm_tick_array_address,
    fetch_raydium_clmm_current_tick_arrays,
    raydium_current_tick_array_start,
)
from veltrix.raydium_clmm_vaults import RaydiumClmmVaultSnapshot
from veltrix.simulation_gate import SimulationEvidence
from veltrix.solana_rpc_simulation import SolanaRpcSimulationAdapter


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, accounts, *, slot=502, genesis="genesis"):
        self.accounts = accounts
        self.slot = slot
        self.genesis = genesis
        self.calls = []

    def post(self, endpoint, *, json, headers, timeout, allow_redirects):
        self.calls.append(json)
        if json["method"] == "getGenesisHash":
            result = self.genesis
        elif json["method"] == "getMultipleAccounts":
            addresses = json["params"][0]
            result = {
                "context": {"slot": self.slot},
                "value": [self.accounts.get(address) for address in addresses],
            }
        else:
            raise AssertionError(json["method"])
        return FakeResponse({"jsonrpc": "2.0", "id": json["id"], "result": result})


def fixture(*, slot=502, pool_id_override=None, start_override=None, count_override=None,
            owner=None, data_length=RAYDIUM_TICK_ARRAY_DATA_LENGTH, wrong_genesis="genesis"):
    program = Pubkey.from_string("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK")
    pool_address = str(Pubkey.new_unique())
    tick_spacing = 1
    current_tick = 5
    start = raydium_current_tick_array_start(current_tick, tick_spacing)
    address = derive_raydium_clmm_tick_array_address(pool_address=pool_address, start_tick_index=start)
    data = bytearray(RAYDIUM_TICK_ARRAY_DATA_LENGTH)
    data[:8] = RAYDIUM_TICK_ARRAY_DISCRIMINATOR
    data[8:40] = bytes(Pubkey.from_string(pool_id_override or pool_address))
    struct.pack_into("<i", data, 40, start if start_override is None else start_override)
    # One initialized tick at index 10: tick=10, liquidity_net=500, gross=1000.
    tick_offset = 44 + 10 * 168
    struct.pack_into("<i", data, tick_offset, 10)
    data[tick_offset + 4:tick_offset + 20] = (500).to_bytes(16, "little", signed=True)
    data[tick_offset + 20:tick_offset + 36] = (1_000).to_bytes(16, "little")
    data[10_124] = 1 if count_override is None else count_override
    data[10_125:10_133] = (123).to_bytes(8, "little")
    raw = bytes(data)
    if data_length != len(raw):
        raw = raw[:data_length] if data_length < len(raw) else raw + b"x" * (data_length - len(raw))
    account = {
        "owner": owner or str(program),
        "lamports": 2_000_000,
        "executable": False,
        "data": [base64.b64encode(raw).decode(), "base64"],
        "space": len(raw),
    }
    digest = hashlib.sha256(b"unsigned-message").hexdigest()
    pool = RaydiumClmmPoolState(
        pool_address=pool_address,
        bump=1,
        amm_config=str(Pubkey.new_unique()),
        pool_creator=str(Pubkey.new_unique()),
        token_mint_0=str(Pubkey.new_unique()),
        token_mint_1=str(Pubkey.new_unique()),
        token_vault_0=str(Pubkey.new_unique()),
        token_vault_1=str(Pubkey.new_unique()),
        observation_key=str(Pubkey.new_unique()),
        mint_decimals_0=6,
        mint_decimals_1=9,
        tick_spacing=tick_spacing,
        liquidity=1_000_000,
        sqrt_price_x64=1 << 64,
        tick_current=current_tick,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data_sha256="a" * 64,
    )
    check = RaydiumClmmPoolRouteCheck(True, pool, pool.token_mint_0, pool.token_mint_1)
    pool_evidence = DexPoolEvidenceSnapshot(
        route_message_sha256=digest,
        registry_sha256="b" * 64,
        simulation_response_id="sim-1",
        simulation_context_slot=500,
        provider_id="rpc-primary",
        cluster="mainnet-beta",
        genesis_hash="genesis",
        context_slot=500,
        accounts=(),
        execution_ready=False,
    )
    vault_snapshot = RaydiumClmmVaultSnapshot(
        route_message_sha256=digest,
        simulation_response_id="sim-1",
        simulation_context_slot=500,
        provider_id="rpc-primary",
        cluster="mainnet-beta",
        genesis_hash="genesis",
        context_slot=501,
        vaults=(),
        execution_ready=False,
    )
    simulation = SimulationEvidence(
        message_sha256=digest,
        recent_blockhash="blockhash",
        cluster="mainnet-beta",
        genesis_hash="genesis",
        provider_id="rpc-primary",
        response_id="sim-1",
        observed_at_epoch_ms=1_000,
        context_slot=500,
        current_block_height=900,
        last_valid_block_height=950,
        simulation_error=None,
        units_consumed=100_000,
        fee_lamports=5_000,
        token_balances=(),
    )
    session = FakeSession({address: account}, slot=slot, genesis=wrong_genesis)
    adapter = SolanaRpcSimulationAdapter(
        endpoint="https://rpc.example.test",
        provider_id="rpc-primary",
        cluster="mainnet-beta",
        expected_genesis_hash="genesis",
        allowed_hosts=("rpc.example.test",),
        timeout_seconds=4.0,
        session=session,
        clock_ms=lambda: 1_005,
    )
    return raw, address, pool, check, pool_evidence, vault_snapshot, simulation, adapter, session, start


def test_current_tick_array_start_matches_negative_floor_semantics():
    assert raydium_current_tick_array_start(5, 10) == 0
    assert raydium_current_tick_array_start(-1, 10) == -600
    assert raydium_current_tick_array_start(-600, 10) == -600
    assert raydium_current_tick_array_start(-601, 10) == -1200


def test_decodes_current_tick_array_and_initialized_tick_records():
    raw, address, pool, _, *_ = fixture()
    result = decode_raydium_clmm_tick_array(
        address=address,
        owner="CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
        lamports=2_000_000,
        executable=False,
        data=raw,
        expected_pool_address=pool.pool_address,
        expected_start_tick_index=0,
        tick_spacing=1,
        current_tick=5,
    )
    assert result.initialized_tick_count == 1
    assert result.ticks[10].initialized is True
    assert result.ticks[10].tick_index == 10
    assert result.ticks[10].liquidity_net == 500
    assert result.ticks[10].liquidity_gross == 1_000
    assert result.recent_epoch == 123


def test_fetches_current_tick_array_at_or_after_pool_vault_and_simulation_slot():
    values = fixture()
    result = fetch_raydium_clmm_current_tick_arrays(
        values[4],
        pool_checks=(values[3],),
        vault_snapshot=values[5],
        simulation=values[6],
        rpc_adapter=values[7],
    )
    assert result.execution_ready is False
    assert result.context_slot == 502
    assert len(result.tick_arrays) == 1
    request = next(call for call in values[8].calls if call["method"] == "getMultipleAccounts")
    assert request["params"][1]["minContextSlot"] == 501


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"owner": str(Pubkey.new_unique())}, "owner"),
        ({"pool_id_override": str(Pubkey.new_unique())}, "different pool"),
        ({"start_override": 60}, "start index"),
        ({"count_override": 2}, "initialized tick count"),
        ({"data_length": 100}, "10240-byte"),
    ],
)
def test_rejects_wrong_owner_pool_id_start_count_or_layout(kwargs, message):
    values = fixture(**kwargs)
    with pytest.raises(RaydiumClmmTickArrayError, match=message):
        decode_raydium_clmm_tick_array(
            address=values[1],
            owner=values[0] and values[2] and (
                str(Pubkey.new_unique()) if "owner" in kwargs else "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
            ),
            lamports=2_000_000,
            executable=False,
            data=values[0],
            expected_pool_address=values[2].pool_address,
            expected_start_tick_index=0,
            tick_spacing=1,
            current_tick=5,
        )


def test_rejects_bad_discriminator_executable_and_zero_lamports():
    raw, address, pool, *_ = fixture()
    bad = b"badmagic" + raw[8:]
    with pytest.raises(RaydiumClmmTickArrayError, match="discriminator"):
        decode_raydium_clmm_tick_array(
            address=address, owner="CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
            lamports=1, executable=False, data=bad, expected_pool_address=pool.pool_address,
            expected_start_tick_index=0, tick_spacing=1, current_tick=5,
        )
    with pytest.raises(RaydiumClmmTickArrayError, match="executable"):
        decode_raydium_clmm_tick_array(
            address=address, owner="CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
            lamports=1, executable=True, data=raw, expected_pool_address=pool.pool_address,
            expected_start_tick_index=0, tick_spacing=1, current_tick=5,
        )
    with pytest.raises(RaydiumClmmTickArrayError, match="positive lamports"):
        decode_raydium_clmm_tick_array(
            address=address, owner="CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
            lamports=0, executable=False, data=raw, expected_pool_address=pool.pool_address,
            expected_start_tick_index=0, tick_spacing=1, current_tick=5,
        )


def test_rejects_tick_index_or_liquidity_inconsistency():
    raw, address, pool, *_ = fixture()
    bad = bytearray(raw)
    offset = 44 + 10 * 168
    struct.pack_into("<i", bad, offset, 11)
    with pytest.raises(RaydiumClmmTickArrayError, match="tick index"):
        decode_raydium_clmm_tick_array(
            address=address, owner="CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
            lamports=1, executable=False, data=bytes(bad), expected_pool_address=pool.pool_address,
            expected_start_tick_index=0, tick_spacing=1, current_tick=5,
        )
    bad = bytearray(raw)
    bad[offset + 4:offset + 20] = (2_000).to_bytes(16, "little", signed=True)
    with pytest.raises(RaydiumClmmTickArrayError, match="net liquidity"):
        decode_raydium_clmm_tick_array(
            address=address, owner="CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
            lamports=1, executable=False, data=bytes(bad), expected_pool_address=pool.pool_address,
            expected_start_tick_index=0, tick_spacing=1, current_tick=5,
        )


def test_rejects_pool_vault_or_simulation_evidence_mismatch():
    values = fixture()
    wrong = dataclasses.replace(values[5], simulation_response_id="other")
    with pytest.raises(RaydiumClmmTickArrayError, match="same order/response/slot"):
        fetch_raydium_clmm_current_tick_arrays(
            values[4], pool_checks=(values[3],), vault_snapshot=wrong,
            simulation=values[6], rpc_adapter=values[7],
        )
