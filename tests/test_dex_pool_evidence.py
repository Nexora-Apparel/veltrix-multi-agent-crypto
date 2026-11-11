"""Adversarial tests for pinned DEX AMM pool owner evidence."""
import base64
import dataclasses
import hashlib
import json

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import (
    DexPoolEvidenceError,
    DexPoolEvidencePolicy,
    fetch_dex_pool_owner_evidence,
)
from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_program_trace import (
    JupiterProgramRegistrySnapshot,
    JupiterRouteProgramPolicy,
)
from veltrix.jupiter_swap_effects import CLASSIC_TOKEN_PROGRAM_ID, derive_associated_token_address
from veltrix.jupiter_v6_route import (
    JUPITER_V6_PROGRAM_ID,
    ROUTE_DISCRIMINATOR,
    JupiterV6RoutePolicy,
    inspect_jupiter_v6_route,
)
from veltrix.solana_rpc_simulation import (
    SolanaRpcAdapterError,
    SolanaRpcSimulationAdapter,
)
from veltrix.simulation_gate import SimulationEvidence
from veltrix.transaction_validator import TransactionValidationResult


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *, owner, data=b"pool-state-data-contents", lamports=2_000_000, executable=False, slot=500, genesis="test-genesis", missing=False):
        self.owner = owner
        self.data = data
        self.lamports = lamports
        self.executable = executable
        self.slot = slot
        self.genesis = genesis
        self.missing = missing
        self.calls = []

    def post(self, endpoint, *, json, headers, timeout, allow_redirects):
        self.calls.append(json)
        assert endpoint == "https://rpc.example.test"
        assert allow_redirects is False
        assert timeout == 4.0
        if json["method"] == "getGenesisHash":
            result = self.genesis
        elif json["method"] == "getMultipleAccounts":
            address = json["params"][0][0]
            info = None if self.missing else {
                "owner": self.owner,
                "lamports": self.lamports,
                "executable": self.executable,
                "data": [base64.b64encode(self.data).decode("ascii"), "base64"],
                "space": len(self.data),
            }
            result = {"context": {"slot": self.slot}, "value": [info]}
        else:
            raise AssertionError(f"unexpected RPC method {json['method']}")
        return FakeResponse({"jsonrpc": "2.0", "id": json["id"], "result": result})


def fixture(*, owner=None, data=b"pool-state-data-contents", lamports=2_000_000, executable=False, slot=500, genesis="test-genesis", missing=False):
    taker = Pubkey.new_unique()
    input_mint = Pubkey.new_unique()
    output_mint = Pubkey.new_unique()
    amm_key = Pubkey.new_unique()
    dex_program = Pubkey.new_unique()
    jupiter = Pubkey.from_string(JUPITER_V6_PROGRAM_ID)
    token_program = Pubkey.from_string(CLASSIC_TOKEN_PROGRAM_ID)
    input_ata = Pubkey.from_string(derive_associated_token_address(str(taker), str(input_mint)))
    output_ata = Pubkey.from_string(derive_associated_token_address(str(taker), str(output_mint)))
    event_authority, _ = Pubkey.find_program_address([b"__event_authority"], jupiter)
    route_data = (
        ROUTE_DISCRIMINATOR
        + (1).to_bytes(4, "little")
        + bytes([26, 100, 0, 1])
        + (1_000).to_bytes(8, "little")
        + (2_000).to_bytes(8, "little")
        + (250).to_bytes(2, "little")
        + b"\x00"
    )
    ix = Instruction(
        jupiter,
        route_data,
        [
            AccountMeta(token_program, False, False),
            AccountMeta(taker, True, True),
            AccountMeta(input_ata, False, True),
            AccountMeta(output_ata, False, True),
            AccountMeta(jupiter, False, False),
            AccountMeta(output_mint, False, False),
            AccountMeta(jupiter, False, False),
            AccountMeta(event_authority, False, False),
            AccountMeta(jupiter, False, False),
            AccountMeta(dex_program, False, False),
            AccountMeta(amm_key, False, True),
        ],
    )
    blockhash = Hash.new_unique()
    message = MessageV0.try_compile(taker, [ix], [], blockhash)
    message_bytes = to_bytes_versioned(message)
    step = JupiterRouteStep(
        amm_key=str(amm_key),
        label="Raydium CLMM",
        input_mint=str(input_mint),
        output_mint=str(output_mint),
        input_amount_raw=1_000,
        output_amount_raw=2_000,
        percent=100.0,
        bps=10_000,
    )
    order = JupiterOrderTransaction(
        provider_id="jupiter-swap-v2",
        request_id="pool-evidence-test",
        quote_id=None,
        observed_at_epoch_ms=1_000_000,
        taker=str(taker),
        fee_payer=str(taker),
        input_mint=str(input_mint),
        output_mint=str(output_mint),
        input_amount_raw=1_000,
        output_amount_raw=2_000,
        minimum_output_amount_raw=1_950,
        price_impact_pct=0.2,
        fee_bps=0.0,
        router="metis",
        transaction_version=0,
        last_valid_block_height=950,
        recent_blockhash=str(blockhash),
        message_bytes=message_bytes,
        serialized_transaction_bytes=b"serialized",
        route_plan=(step,),
        signature_fee_lamports=5_000,
        prioritization_fee_lamports=0,
        rent_fee_lamports=0,
    )
    validation = TransactionValidationResult(
        message_sha256=hashlib.sha256(message_bytes).hexdigest(),
        message_size=len(message_bytes),
        fee_payer=str(taker),
        required_signers=(str(taker),),
        program_ids=(JUPITER_V6_PROGRAM_ID,),
        instruction_count=1,
        account_keys=tuple(str(key) for key in message.account_keys),
    )
    route_inspection = inspect_jupiter_v6_route(
        order,
        validation=validation,
        policy=JupiterV6RoutePolicy(
            allowed_jupiter_program_ids=(JUPITER_V6_PROGRAM_ID,),
            allowed_dex_labels=("Raydium CLMM",),
        ),
    )
    simulation = SimulationEvidence(
        message_sha256=route_inspection.message_sha256,
        recent_blockhash=order.recent_blockhash,
        cluster="mainnet-beta",
        genesis_hash="test-genesis",
        provider_id="rpc-primary",
        response_id="simulation-500",
        observed_at_epoch_ms=1_000_000,
        context_slot=500,
        current_block_height=900,
        last_valid_block_height=950,
        simulation_error=None,
        units_consumed=100_000,
        fee_lamports=5_000,
        token_balances=(),
        top_level_program_ids=(JUPITER_V6_PROGRAM_ID,),
        inner_program_ids=(str(dex_program),),
        inner_instruction_count=1,
    )
    pairs = tuple(sorted((
        (JUPITER_V6_PROGRAM_ID, "Jupiter V6"),
        (str(dex_program), "Raydium CLMM"),
    )))
    registry = JupiterProgramRegistrySnapshot(
        observed_at_epoch_ms=1_000_000,
        program_labels=pairs,
        sha256=hashlib.sha256(json.dumps(pairs, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest(),
    )
    route_policy = JupiterRouteProgramPolicy(
        allowed_dex_labels=("Raydium CLMM",),
        allowed_dex_program_ids=(str(dex_program),),
        allowed_jupiter_program_ids=(JUPITER_V6_PROGRAM_ID,),
    )
    session = FakeSession(
        owner=owner or str(dex_program),
        data=data,
        lamports=lamports,
        executable=executable,
        slot=slot,
        genesis=genesis,
        missing=missing,
    )
    rpc = SolanaRpcSimulationAdapter(
        endpoint="https://rpc.example.test",
        provider_id="rpc-primary",
        cluster="mainnet-beta",
        expected_genesis_hash="test-genesis",
        allowed_hosts=("rpc.example.test",),
        timeout_seconds=4.0,
        session=session,
        clock_ms=lambda: 1_005_000,
    )
    return order, route_inspection, registry, route_policy, simulation, rpc, session, str(dex_program), str(amm_key)


def fetch(values=None, *, policy=None):
    order, inspection, registry, route_policy, simulation, rpc, *_ = values or fixture()
    return fetch_dex_pool_owner_evidence(
        order,
        route_inspection=inspection,
        registry=registry,
        route_policy=route_policy,
        simulation=simulation,
        rpc_adapter=rpc,
        policy=policy,
    )


def test_accepts_pinned_pool_owner_and_records_account_data_hash():
    values = fixture()
    result = fetch(values)
    dex_program, amm_key = values[-2], values[-1]
    assert result.execution_ready is False
    assert result.context_slot == 500
    assert result.simulation_context_slot == 500
    assert result.simulation_response_id == "simulation-500"
    assert result.provider_id == "rpc-primary"
    assert len(result.accounts) == 1
    account = result.accounts[0]
    assert account.amm_key == amm_key
    assert account.expected_program_id == dex_program
    assert account.owner_program_id == dex_program
    assert account.executable is False
    assert account.data_length == len(account.data_bytes)
    assert account.data_sha256 == hashlib.sha256(account.data_bytes).hexdigest()


def test_requests_pool_accounts_at_or_after_simulation_slot():
    values = fixture()
    result = fetch(values)
    request = next(call for call in values[6].calls if call["method"] == "getMultipleAccounts")
    assert request["params"][1]["minContextSlot"] == 500
    assert result.context_slot >= result.simulation_context_slot


def test_rejects_pool_account_owned_by_another_program():
    wrong_owner = str(Pubkey.new_unique())
    values = fixture(owner=wrong_owner)
    with pytest.raises(DexPoolEvidenceError, match="owner differs"):
        fetch(values)


def test_rejects_executable_or_empty_pool_account():
    with pytest.raises(DexPoolEvidenceError, match="must not be executable"):
        fetch(fixture(executable=True))
    with pytest.raises(DexPoolEvidenceError, match="data length"):
        fetch(fixture(data=b""))


def test_rejects_missing_pool_account_or_zero_lamports():
    with pytest.raises(SolanaRpcAdapterError, match="is missing"):
        fetch(fixture(missing=True))
    with pytest.raises(DexPoolEvidenceError, match="positive lamports"):
        fetch(fixture(lamports=0))


def test_rejects_snapshot_older_than_simulation_slot():
    with pytest.raises(SolanaRpcAdapterError, match="older than the required simulation slot"):
        fetch(fixture(slot=499))


def test_rejects_wrong_cluster_genesis():
    with pytest.raises(SolanaRpcAdapterError, match="genesis hash"):
        fetch(fixture(genesis="wrong-genesis"))


def test_rejects_pool_data_outside_configured_size_bounds():
    with pytest.raises(DexPoolEvidenceError, match="data length"):
        fetch(fixture(data=b"12345678"), policy=DexPoolEvidencePolicy(min_account_data_bytes=16))
    with pytest.raises(DexPoolEvidenceError, match="data length"):
        fetch(fixture(data=b"x" * 64), policy=DexPoolEvidencePolicy(max_account_data_bytes=32))


def test_rejects_registry_label_without_exactly_one_pinned_program():
    values = fixture()
    order, inspection, registry, route_policy, simulation, rpc, *_ = values
    other = str(Pubkey.new_unique())
    pairs = tuple(sorted(registry.program_labels + ((other, "Raydium CLMM"),)))
    registry = JupiterProgramRegistrySnapshot(
        observed_at_epoch_ms=1_000_000,
        program_labels=pairs,
        sha256=hashlib.sha256(json.dumps(pairs, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest(),
    )
    route_policy = dataclasses.replace(route_policy, allowed_dex_program_ids=route_policy.allowed_dex_program_ids + (other,))
    with pytest.raises(DexPoolEvidenceError, match="exactly one"):
        fetch_dex_pool_owner_evidence(
            order,
            route_inspection=inspection,
            registry=registry,
            route_policy=route_policy,
            simulation=simulation,
            rpc_adapter=rpc,
        )


def test_rejects_route_inspection_not_bound_to_exact_order_message():
    values = fixture()
    order, inspection, registry, route_policy, simulation, rpc, *_ = values
    inspection = dataclasses.replace(inspection, message_sha256="0" * 64)
    with pytest.raises(DexPoolEvidenceError, match="exact order message"):
        fetch_dex_pool_owner_evidence(
            order,
            route_inspection=inspection,
            registry=registry,
            route_policy=route_policy,
            simulation=simulation,
            rpc_adapter=rpc,
        )
