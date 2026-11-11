"""Adversarial tests for Raydium CLMM vault PDA and classic SPL account verification."""
import base64
import dataclasses
import hashlib
import struct

import pytest
from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolEvidenceSnapshot
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolRouteCheck, RaydiumClmmPoolState
from veltrix.raydium_clmm_vaults import (
    CLASSIC_SPL_TOKEN_PROGRAM_ID,
    RaydiumClmmVaultError,
    apply_raydium_clmm_swap_vault_settlement,
    apply_raydium_clmm_vault_delta,
    encode_raydium_clmm_vault_account,
    validate_raydium_exact_internal_settlement,
    derive_raydium_clmm_vault_address,
    fetch_raydium_clmm_vault_snapshot,
)
from veltrix.simulation_gate import SimulationEvidence
from veltrix.solana_rpc_simulation import SolanaRpcSimulationAdapter


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, accounts, *, slot=501, genesis="genesis"):
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


def token_account(mint, authority, *, amount=1_000_000, state=1, delegate=0, native=0, close_authority=0):
    data = bytearray(165)
    data[0:32] = bytes(Pubkey.from_string(mint))
    data[32:64] = bytes(Pubkey.from_string(authority))
    data[64:72] = amount.to_bytes(8, "little")
    data[72:76] = delegate.to_bytes(4, "little")
    data[108] = state
    data[109:113] = native.to_bytes(4, "little")
    data[129:133] = close_authority.to_bytes(4, "little")
    return bytes(data)


def fixture(*, slot=501, owner=CLASSIC_SPL_TOKEN_PROGRAM_ID, state0=1, state1=1,
            delegate0=0, native0=0, close0=0, authority0=None, mint0=None,
            wrong_genesis="genesis", account_size=165):
    program = Pubkey.from_string("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK")
    pool_address = str(Pubkey.new_unique())
    mint_a, mint_b = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    vault_a = derive_raydium_clmm_vault_address(pool_address=pool_address, mint=mint_a)
    vault_b = derive_raydium_clmm_vault_address(pool_address=pool_address, mint=mint_b)
    pool = RaydiumClmmPoolState(
        pool_address=pool_address,
        bump=1,
        amm_config=str(Pubkey.new_unique()),
        pool_creator=str(Pubkey.new_unique()),
        token_mint_0=mint_a,
        token_mint_1=mint_b,
        token_vault_0=vault_a,
        token_vault_1=vault_b,
        observation_key=str(Pubkey.new_unique()),
        mint_decimals_0=6,
        mint_decimals_1=9,
        tick_spacing=10,
        liquidity=1_000_000,
        sqrt_price_x64=1 << 64,
        tick_current=0,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data_sha256="a" * 64,
    )
    checks = (RaydiumClmmPoolRouteCheck(
        passed=True,
        pool=pool,
        route_input_mint=mint_a,
        route_output_mint=mint_b,
    ),)
    digest = hashlib.sha256(b"unsigned-message").hexdigest()
    pool_evidence = DexPoolEvidenceSnapshot(
        route_message_sha256=digest,
        registry_sha256="b" * 64,
        simulation_response_id="simulation-1",
        simulation_context_slot=500,
        provider_id="rpc-primary",
        cluster="mainnet-beta",
        genesis_hash="genesis",
        context_slot=500,
        accounts=(),
        execution_ready=False,
    )
    simulation = SimulationEvidence(
        message_sha256=digest,
        recent_blockhash="blockhash",
        cluster="mainnet-beta",
        genesis_hash="genesis",
        provider_id="rpc-primary",
        response_id="simulation-1",
        observed_at_epoch_ms=1_000,
        context_slot=500,
        current_block_height=900,
        last_valid_block_height=950,
        simulation_error=None,
        units_consumed=100_000,
        fee_lamports=5_000,
        token_balances=(),
    )
    data0 = token_account(
        mint0 or mint_a,
        authority0 or pool_address,
        state=state0,
        delegate=delegate0,
        native=native0,
        close_authority=close0,
    )
    data1 = token_account(mint_b, pool_address, state=state1)
    if account_size != 165:
        data0 = data0[:account_size] if account_size < 165 else data0 + b"x" * (account_size - 165)
    accounts = {
        vault_a: {
            "owner": owner,
            "lamports": 2_000_000,
            "executable": False,
            "data": [base64.b64encode(data0).decode(), "base64"],
            "space": len(data0),
        },
        vault_b: {
            "owner": CLASSIC_SPL_TOKEN_PROGRAM_ID,
            "lamports": 2_000_000,
            "executable": False,
            "data": [base64.b64encode(data1).decode(), "base64"],
            "space": len(data1),
        },
    }
    session = FakeSession(accounts, slot=slot, genesis=wrong_genesis)
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
    return pool_evidence, checks, simulation, adapter, session, vault_a, vault_b, pool_address, mint_a, mint_b


def fetch(values=None):
    pool_evidence, checks, simulation, adapter, *_ = values or fixture()
    return fetch_raydium_clmm_vault_snapshot(
        pool_evidence,
        pool_checks=checks,
        simulation=simulation,
        rpc_adapter=adapter,
    )


def test_fetches_both_vaults_at_or_after_the_pool_and_simulation_slot():
    values = fixture()
    result = fetch(values)
    assert result.execution_ready is False
    assert result.context_slot == 501
    assert result.simulation_context_slot == 500
    assert result.simulation_response_id == "simulation-1"
    assert len(result.vaults) == 2
    assert {item.address for item in result.vaults} == {values[5], values[6]}
    assert all(item.token_authority == values[7] for item in result.vaults)
    request = next(call for call in values[4].calls if call["method"] == "getMultipleAccounts")
    assert request["params"][1]["minContextSlot"] == 500


def test_rejects_nonclassic_token_program_vault():
    values = fixture(owner=str(Pubkey.new_unique()))
    with pytest.raises(RaydiumClmmVaultError, match="classic SPL Token"):
        fetch(values)


def test_rejects_wrong_vault_authority_or_mint():
    with pytest.raises(RaydiumClmmVaultError, match="authority"):
        fetch(fixture(authority0=str(Pubkey.new_unique())))
    with pytest.raises(RaydiumClmmVaultError, match="mint"):
        fetch(fixture(mint0=str(Pubkey.new_unique())))


@pytest.mark.parametrize("state", [0, 2, 3])
def test_rejects_uninitialized_or_frozen_vault(state):
    with pytest.raises(RaydiumClmmVaultError, match="not initialized or is frozen"):
        fetch(fixture(state0=state))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("delegate0", 1, "delegate authority"),
        ("native0", 1, "wrapped-native"),
        ("close0", 1, "close authority"),
    ],
)
def test_rejects_delegate_native_and_close_authority(field, value, message):
    kwargs = {field: value}
    with pytest.raises(RaydiumClmmVaultError, match=message):
        fetch(fixture(**kwargs))


def test_rejects_wrong_vault_account_size():
    with pytest.raises(RaydiumClmmVaultError, match="165-byte"):
        fetch(fixture(account_size=164))


def test_rejects_stale_or_wrong_provider_evidence():
    with pytest.raises(Exception, match="older than the required simulation slot"):
        fetch(fixture(slot=499))
    values = fixture()
    pool_evidence, checks, simulation, adapter, *_ = values
    wrong = dataclasses.replace(simulation, provider_id="rpc-secondary")
    with pytest.raises(RaydiumClmmVaultError, match="same provider and cluster"):
        fetch_raydium_clmm_vault_snapshot(
            pool_evidence,
            pool_checks=checks,
            simulation=wrong,
            rpc_adapter=adapter,
        )


def test_rejects_wrong_cluster_genesis():
    values = fixture(wrong_genesis="wrong-genesis")
    with pytest.raises(Exception, match="genesis hash"):
        fetch(values)


def test_rejects_pool_evidence_from_another_simulation():
    values = fixture()
    pool_evidence, checks, simulation, adapter, *_ = values
    wrong = dataclasses.replace(pool_evidence, simulation_response_id="other")
    with pytest.raises(RaydiumClmmVaultError, match="response/slot/digest"):
        fetch_raydium_clmm_vault_snapshot(
            wrong,
            pool_checks=checks,
            simulation=simulation,
            rpc_adapter=adapter,
        )


def test_vault_serializer_roundtrips_verified_classic_spl_account_bytes():
    values = fixture()
    snapshot = fetch(values)
    for vault in snapshot.vaults:
        assert encode_raydium_clmm_vault_account(vault) == vault.account_data
        assert vault.source_data_sha256 == vault.data_sha256


@pytest.mark.parametrize(
    "zero_for_one,expected_0,expected_1",
    [
        (True, 1_000_100, 999_800),
        (False, 999_900, 1_000_200),
    ],
)
def test_swap_vault_settlement_applies_directional_deltas_and_rehashes_accounts(
    zero_for_one, expected_0, expected_1
):
    values = fixture()
    snapshot = fetch(values)
    vault_0 = next(vault for vault in snapshot.vaults if vault.mint == values[8])
    vault_1 = next(vault for vault in snapshot.vaults if vault.mint == values[9])
    pool_state = values[1][0].pool
    updated_0, updated_1 = apply_raydium_clmm_swap_vault_settlement(
        pool_state, vault_0, vault_1,
        amount_0=100,
        amount_1=200,
        zero_for_one=zero_for_one,
    )
    assert (vault_0.amount_raw, vault_1.amount_raw) == (1_000_000, 1_000_000)
    assert (updated_0.amount_raw, updated_1.amount_raw) == (
        expected_0, expected_1
    )
    for updated in (updated_0, updated_1):
        digest = hashlib.sha256(updated.account_data).hexdigest()
        assert updated.data_sha256 == digest
        assert updated.source_data_sha256 == digest
        assert encode_raydium_clmm_vault_account(updated) == updated.account_data


@pytest.mark.parametrize(
    "amount_delta",
    [-(1_000_001), (1 << 64)],
)
def test_vault_delta_rejects_underflow_and_overflow(amount_delta):
    snapshot = fetch(fixture())
    vault = snapshot.vaults[0]
    with pytest.raises(RaydiumClmmVaultError, match="underflows or overflows"):
        apply_raydium_clmm_vault_delta(vault, amount_delta=amount_delta)


def test_swap_vault_settlement_is_atomic_when_output_vault_would_underflow():
    values = fixture()
    snapshot = fetch(values)
    vault_0 = next(vault for vault in snapshot.vaults if vault.mint == values[8])
    vault_1 = next(vault for vault in snapshot.vaults if vault.address != vault_0.address)
    before = (vault_0.amount_raw, vault_1.amount_raw)
    with pytest.raises(RaydiumClmmVaultError, match="underflows or overflows"):
        apply_raydium_clmm_swap_vault_settlement(
            values[1][0].pool, vault_0, vault_1,
            amount_0=10,
            amount_1=vault_1.amount_raw + 1,
            zero_for_one=True,
        )
    assert (vault_0.amount_raw, vault_1.amount_raw) == before


def test_swap_vault_settlement_rejects_zero_amounts_and_wrong_pool_identity():
    values = fixture()
    snapshot = fetch(values)
    vault_0 = next(vault for vault in snapshot.vaults if vault.mint == values[8])
    vault_1 = next(vault for vault in snapshot.vaults if vault.mint == values[9])
    pool_state = values[1][0].pool
    with pytest.raises(RaydiumClmmVaultError, match="positive u64"):
        apply_raydium_clmm_swap_vault_settlement(
            pool_state, vault_0, vault_1,
            amount_0=0, amount_1=1, zero_for_one=True,
        )
    wrong_pool = dataclasses.replace(
        pool_state, token_vault_0=str(Pubkey.new_unique())
    )
    with pytest.raises(RaydiumClmmVaultError, match="do not match PoolState"):
        apply_raydium_clmm_swap_vault_settlement(
            wrong_pool, vault_0, vault_1,
            amount_0=1, amount_1=1, zero_for_one=True,
        )


@pytest.mark.parametrize("zero_for_one", [True, False])
@pytest.mark.parametrize("is_base_input", [True, False])
def test_exact_internal_validator_matches_directional_vault_reload_and_return_value(
    zero_for_one, is_base_input
):
    values = fixture()
    snapshot = fetch(values)
    pool_state = values[1][0].pool
    before_0 = next(vault for vault in snapshot.vaults if vault.mint == values[8])
    before_1 = next(vault for vault in snapshot.vaults if vault.mint == values[9])
    after_0, after_1 = apply_raydium_clmm_swap_vault_settlement(
        pool_state, before_0, before_1,
        amount_0=100,
        amount_1=80,
        zero_for_one=zero_for_one,
    )
    specified = (
        (100 if zero_for_one else 80)
        if is_base_input
        else (80 if zero_for_one else 100)
    )
    result = validate_raydium_exact_internal_settlement(
        pool_state,
        before_0, before_1, after_0, after_1,
        amount_0=100,
        amount_1=80,
        zero_for_one=zero_for_one,
        is_base_input=is_base_input,
        amount_specified=specified,
        sqrt_price_before_x64=(1 << 64) + 100 if zero_for_one else (1 << 64),
        sqrt_price_after_x64=(1 << 64) if zero_for_one else (1 << 64) + 100,
        sqrt_price_limit_x64=0,
    )
    assert result == (80 if is_base_input else 100)


@pytest.mark.parametrize("zero_for_one", [True, False])
def test_exact_internal_validator_allows_partial_fill_only_with_price_limit(zero_for_one):
    values = fixture()
    snapshot = fetch(values)
    pool_state = values[1][0].pool
    before_0 = next(vault for vault in snapshot.vaults if vault.mint == values[8])
    before_1 = next(vault for vault in snapshot.vaults if vault.mint == values[9])
    after_0, after_1 = apply_raydium_clmm_swap_vault_settlement(
        pool_state, before_0, before_1,
        amount_0=100, amount_1=80, zero_for_one=zero_for_one,
    )
    result = validate_raydium_exact_internal_settlement(
        pool_state,
        before_0, before_1, after_0, after_1,
        amount_0=100, amount_1=80,
        zero_for_one=zero_for_one,
        is_base_input=True,
        amount_specified=150,
        sqrt_price_before_x64=(1 << 64) + 100 if zero_for_one else (1 << 64),
        sqrt_price_after_x64=(1 << 64) if zero_for_one else (1 << 64) + 100,
        sqrt_price_limit_x64=(1 << 64) - 1 if zero_for_one else (1 << 64) + 200,
    )
    assert result == 80


def test_exact_internal_validator_rejects_partial_fill_without_price_limit():
    values = fixture()
    snapshot = fetch(values)
    pool_state = values[1][0].pool
    before_0 = next(vault for vault in snapshot.vaults if vault.mint == values[8])
    before_1 = next(vault for vault in snapshot.vaults if vault.mint == values[9])
    after_0, after_1 = apply_raydium_clmm_swap_vault_settlement(
        pool_state, before_0, before_1,
        amount_0=100, amount_1=80, zero_for_one=True,
    )
    with pytest.raises(RaydiumClmmVaultError, match="did not fill"):
        validate_raydium_exact_internal_settlement(
            pool_state,
            before_0, before_1, after_0, after_1,
            amount_0=100, amount_1=80,
            zero_for_one=True,
            is_base_input=True,
            amount_specified=150,
            sqrt_price_before_x64=(1 << 64) + 100,
            sqrt_price_after_x64=(1 << 64),
            sqrt_price_limit_x64=0,
        )


def test_exact_internal_validator_rejects_wrong_price_direction():
    values = fixture()
    snapshot = fetch(values)
    pool_state = values[1][0].pool
    before_0 = next(vault for vault in snapshot.vaults if vault.mint == values[8])
    before_1 = next(vault for vault in snapshot.vaults if vault.mint == values[9])
    after_0, after_1 = apply_raydium_clmm_swap_vault_settlement(
        pool_state, before_0, before_1,
        amount_0=100, amount_1=80, zero_for_one=True,
    )
    with pytest.raises(RaydiumClmmVaultError, match="wrong direction"):
        validate_raydium_exact_internal_settlement(
            pool_state,
            before_0, before_1, after_0, after_1,
            amount_0=100, amount_1=80,
            zero_for_one=True,
            is_base_input=True,
            amount_specified=100,
            sqrt_price_before_x64=(1 << 64),
            sqrt_price_after_x64=(1 << 64) + 100,
            sqrt_price_limit_x64=0,
        )
