"""Tests for the provider-pinned, read-only Solana simulation adapter."""
import base64
import dataclasses

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message, MessageV0, from_bytes_versioned, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from veltrix.solana_rpc_simulation import (
    SolanaRpcAdapterError,
    SolanaRpcSimulationAdapter,
)
from veltrix.simulation_gate import ExpectedTokenDelta, SimulationGatePolicy, evaluate_simulation
from veltrix.transaction_validator import (
    ADDRESS_LOOKUP_TABLE_PROGRAM_ID,
    AccountExpectation,
    InstructionExpectation,
    TransactionIntent,
    TransactionValidationPolicy,
)


TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def token_info(mint, owner, amount, program=TOKEN_PROGRAM, length=165):
    raw = bytearray(length)
    raw[0:32] = bytes(Pubkey.from_string(mint))
    raw[32:64] = bytes(Pubkey.from_string(owner))
    raw[64:72] = int(amount).to_bytes(8, "little")
    return {
        "owner": program,
        "lamports": 2_039_280,
        "data": [base64.b64encode(bytes(raw)).decode("ascii"), "base64"],
        "executable": False,
        "rentEpoch": 0,
        "space": len(raw),
    }


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, fixture, *, overrides=None):
        self.fixture = fixture
        self.overrides = overrides or {}
        self.calls = []

    def post(self, endpoint, *, json, headers, timeout, allow_redirects):
        self.calls.append(json)
        assert endpoint == "https://rpc.example.test"
        assert allow_redirects is False
        assert timeout == 4.0
        method = json["method"]
        params = json["params"]
        if method == "simulateTransaction":
            wire = base64.b64decode(params[0], validate=True)
            tx = VersionedTransaction.from_bytes(wire)
            assert all(sig == Signature.default() for sig in tx.signatures)
            assert params[1]["sigVerify"] is False
            assert params[1]["replaceRecentBlockhash"] is False
            assert params[1]["encoding"] == "base64"
            assert params[1]["accounts"]["addresses"] == self.fixture["addresses"]
            result = {
                "context": {"slot": self.fixture["slot"]},
                "value": {
                    "err": None,
                    "unitsConsumed": 120_000,
                    "accounts": self.fixture["post_accounts"],
                    "innerInstructions": self.fixture.get("inner_instructions", []),
                },
            }
        elif method == "getGenesisHash":
            result = "test-genesis"
        elif method == "isBlockhashValid":
            result = {"context": {"slot": self.fixture["slot"]}, "value": True}
        elif method == "getBlockHeight":
            result = 900
        elif method == "getMultipleAccounts":
            requested = params[0]
            if self.fixture.get("lookup_table_address") and requested == [self.fixture["lookup_table_address"]]:
                result = {
                    "context": {"slot": self.fixture.get("lookup_slot", self.fixture["slot"])},
                    "value": [self.fixture["lookup_table_info"]],
                }
            else:
                result = {
                    "context": {"slot": self.fixture["slot"]},
                    "value": self.fixture["pre_accounts"],
                }
        elif method == "getFeeForMessage":
            result = {"context": {"slot": self.fixture["slot"]}, "value": 5_000}
        else:
            raise AssertionError(f"unexpected RPC method: {method}")
        if method in self.overrides:
            result = self.overrides[method]
        return FakeResponse({
            "jsonrpc": "2.0",
            "id": json["id"],
            "result": result,
        })


def fixture():
    payer = Pubkey.new_unique()
    source = Pubkey.new_unique()
    destination = Pubkey.new_unique()
    source_owner = Pubkey.new_unique()
    destination_owner = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    program = Pubkey.new_unique()
    blockhash = Hash.new_unique()
    instruction = Instruction(
        program,
        b"approved-transfer",
        [
            AccountMeta(payer, True, True),
            AccountMeta(source, False, True),
            AccountMeta(destination, False, True),
        ],
    )
    message = Message.new_with_blockhash([instruction], payer, blockhash)
    raw_message = to_bytes_versioned(message)
    intent = TransactionIntent(
        str(payer),
        (str(payer),),
        str(blockhash),
        (
            InstructionExpectation(
                str(program),
                (
                    AccountExpectation(str(payer), True, True),
                    AccountExpectation(str(source), False, True),
                    AccountExpectation(str(destination), False, True),
                ),
                b"approved-transfer",
            ),
        ),
    )
    policy = TransactionValidationPolicy(allowed_program_ids=(str(program),))
    expected = (
        ExpectedTokenDelta(str(source), str(mint), str(source_owner), -250),
        ExpectedTokenDelta(str(destination), str(mint), str(destination_owner), 240),
    )
    fixture_data = {
        "slot": 500,
        "addresses": [str(source), str(destination)],
        "pre_accounts": [
            token_info(str(mint), str(source_owner), 1_000),
            token_info(str(mint), str(destination_owner), 10),
        ],
        "post_accounts": [
            token_info(str(mint), str(source_owner), 750),
            token_info(str(mint), str(destination_owner), 250),
        ],
    }
    return raw_message, intent, policy, expected, fixture_data


def adapter(session):
    return SolanaRpcSimulationAdapter(
        endpoint="https://rpc.example.test",
        provider_id="rpc-primary",
        cluster="mainnet-beta",
        expected_genesis_hash="test-genesis",
        allowed_hosts=("rpc.example.test",),
        timeout_seconds=4.0,
        session=session,
        clock_ms=lambda: 1_005_000,
    )


def test_builds_unsigned_simulation_and_returns_gate_compatible_evidence():
    raw, intent, tx_policy, expected, data = fixture()
    session = FakeSession(data)
    result = adapter(session).simulate(
        raw,
        intent=intent,
        transaction_policy=tx_policy,
        expected_token_deltas=expected,
        minimum_context_slot=490,
        last_valid_block_height=950,
    )
    assert result.validation.message_sha256
    assert result.evidence.context_slot == 500
    assert result.evidence.units_consumed == 120_000
    assert result.evidence.fee_lamports == 5_000
    assert len(session.calls) == 6

    gate = evaluate_simulation(
        result.evidence,
        intent=intent,
        validation=result.validation,
        expected_token_deltas=expected,
        policy=SimulationGatePolicy(
            expected_cluster="mainnet-beta",
            expected_genesis_hash="test-genesis",
            allowed_provider_ids=("rpc-primary",),
        ),
        now_epoch_ms=1_005_000,
        minimum_context_slot=490,
    )
    assert gate.approved is True


def test_rejects_non_https_or_non_allowlisted_endpoint():
    with pytest.raises(SolanaRpcAdapterError, match="HTTPS"):
        SolanaRpcSimulationAdapter(
            endpoint="http://rpc.example.test",
            provider_id="rpc-primary",
            cluster="mainnet-beta",
            expected_genesis_hash="test-genesis",
            allowed_hosts=("rpc.example.test",),
        )
    with pytest.raises(SolanaRpcAdapterError, match="allowlisted"):
        SolanaRpcSimulationAdapter(
            endpoint="https://attacker.example",
            provider_id="rpc-primary",
            cluster="mainnet-beta",
            expected_genesis_hash="test-genesis",
            allowed_hosts=("rpc.example.test",),
        )


@pytest.mark.parametrize(
    ("method", "result", "message"),
    [
        ("getGenesisHash", "wrong-genesis", "genesis hash"),
        ("isBlockhashValid", {"context": {"slot": 500}, "value": False}, "not valid"),
        ("getMultipleAccounts", {"context": {"slot": 500}, "value": [None, None]}, "missing"),
        ("simulateTransaction", {"context": {"slot": 501}, "value": {"err": None, "unitsConsumed": 1, "accounts": []}}, "same context slot"),
        ("simulateTransaction", {"context": {"slot": 500}, "value": {"err": None, "unitsConsumed": 1, "accounts": [None, None]}}, "missing"),
        ("getFeeForMessage", {"context": {"slot": 501}, "value": 5000}, "different context slots"),
        ("getFeeForMessage", {"context": {"slot": 500}, "value": None}, "invalid fee"),
    ],
)
def test_rejects_incomplete_or_inconsistent_rpc_evidence(method, result, message):
    raw, intent, tx_policy, expected, data = fixture()
    session = FakeSession(data, overrides={method: result})
    with pytest.raises(SolanaRpcAdapterError, match=message):
        adapter(session).simulate(
            raw,
            intent=intent,
            transaction_policy=tx_policy,
            expected_token_deltas=expected,
            minimum_context_slot=490,
            last_valid_block_height=950,
        )


def test_rejects_blockhash_expired_before_simulation():
    raw, intent, tx_policy, expected, data = fixture()
    session = FakeSession(data, overrides={"getBlockHeight": 951})
    with pytest.raises(SolanaRpcAdapterError, match="expired"):
        adapter(session).simulate(
            raw,
            intent=intent,
            transaction_policy=tx_policy,
            expected_token_deltas=expected,
            minimum_context_slot=490,
            last_valid_block_height=950,
        )


def test_rejects_prestate_slot_below_minimum_context():
    raw, intent, tx_policy, expected, data = fixture()
    data["slot"] = 489
    session = FakeSession(data)
    with pytest.raises(SolanaRpcAdapterError, match="minimum context"):
        adapter(session).simulate(
            raw,
            intent=intent,
            transaction_policy=tx_policy,
            expected_token_deltas=expected,
            minimum_context_slot=490,
            last_valid_block_height=950,
        )


def test_rejects_non_token_program_owned_account():
    raw, intent, tx_policy, expected, data = fixture()
    data["pre_accounts"][0]["owner"] = str(Pubkey.new_unique())
    session = FakeSession(data)
    with pytest.raises(SolanaRpcAdapterError, match="supported SPL token program"):
        adapter(session).simulate(
            raw,
            intent=intent,
            transaction_policy=tx_policy,
            expected_token_deltas=expected,
            minimum_context_slot=490,
            last_valid_block_height=950,
        )


def test_rejects_truncated_token_account_layout():
    raw, intent, tx_policy, expected, data = fixture()
    data["post_accounts"][0] = token_info(
        expected[0].mint, expected[0].owner, 750, length=64
    )
    session = FakeSession(data)
    with pytest.raises(SolanaRpcAdapterError, match="shorter than"):
        adapter(session).simulate(
            raw,
            intent=intent,
            transaction_policy=tx_policy,
            expected_token_deltas=expected,
            minimum_context_slot=490,
            last_valid_block_height=950,
        )


def test_rejects_rpc_redirect_response():
    raw, intent, tx_policy, expected, data = fixture()

    class RedirectSession(FakeSession):
        def post(self, *args, **kwargs):
            return type("Redirect", (), {"status_code": 302, "json": lambda self: {}})()

    with pytest.raises(SolanaRpcAdapterError, match="HTTP 302"):
        adapter(RedirectSession(data)).simulate(
            raw,
            intent=intent,
            transaction_policy=tx_policy,
            expected_token_deltas=expected,
            minimum_context_slot=490,
            last_valid_block_height=950,
        )


def lookup_table_raw(addresses, *, deactivation_slot=(1 << 64) - 1, last_extended_slot=50):
    raw = bytearray(56 + 32 * len(addresses))
    raw[0:4] = (1).to_bytes(4, "little")
    raw[4:12] = deactivation_slot.to_bytes(8, "little")
    raw[12:20] = last_extended_slot.to_bytes(8, "little")
    raw[20] = 0
    raw[21] = 0
    raw[22:54] = bytes(32)
    raw[54:56] = bytes(2)
    for index, address in enumerate(addresses):
        raw[56 + index * 32:56 + (index + 1) * 32] = bytes(address)
    return bytes(raw)


def v0_lookup_fixture():
    payer = Pubkey.new_unique()
    source = Pubkey.new_unique()
    destination = Pubkey.new_unique()
    source_owner = Pubkey.new_unique()
    destination_owner = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    program = Pubkey.new_unique()
    table_key = Pubkey.new_unique()
    blockhash = Hash.new_unique()
    table_account = AddressLookupTableAccount(key=table_key, addresses=[source, destination])
    instruction = Instruction(
        program,
        b"approved-transfer",
        [
            AccountMeta(payer, True, True),
            AccountMeta(source, False, True),
            AccountMeta(destination, False, True),
        ],
    )
    message = MessageV0.try_compile(payer, [instruction], [table_account], blockhash)
    raw_message = to_bytes_versioned(message)
    static_keys = tuple(str(key) for key in message.account_keys)
    loaded_writable = []
    loaded_readonly = []
    for lookup in message.address_table_lookups:
        loaded_writable.extend(str(table_account.addresses[i]) for i in bytes(lookup.writable_indexes))
        loaded_readonly.extend(str(table_account.addresses[i]) for i in bytes(lookup.readonly_indexes))
    account_keys = static_keys + tuple(loaded_writable) + tuple(loaded_readonly)
    static_count = len(static_keys)

    def writable(index):
        if index < static_count:
            header = message.header
            required = int(header.num_required_signatures)
            readonly_signed = int(header.num_readonly_signed_accounts)
            readonly_unsigned = int(header.num_readonly_unsigned_accounts)
            if index < required:
                return index < required - readonly_signed
            return index < static_count - readonly_unsigned
        return index < static_count + len(loaded_writable)

    compiled = message.instructions[0]
    program_id = str(account_keys[int(compiled.program_id_index)])
    intent = TransactionIntent(
        fee_payer=str(payer),
        required_signers=tuple(
            key for index, key in enumerate(static_keys) if message.is_signer(index)
        ),
        recent_blockhash=str(blockhash),
        instructions=(
            InstructionExpectation(
                program_id,
                tuple(
                    AccountExpectation(
                        account_keys[int(index)],
                        message.is_signer(int(index)) if int(index) < static_count else False,
                        writable(int(index)),
                    )
                    for index in bytes(compiled.accounts)
                ),
                bytes(compiled.data),
            ),
        ),
    )
    tx_policy = TransactionValidationPolicy(
        allowed_program_ids=(program_id,),
        allow_address_lookup_tables=True,
    )
    expected = (
        ExpectedTokenDelta(str(source), str(mint), str(source_owner), -250),
        ExpectedTokenDelta(str(destination), str(mint), str(destination_owner), 240),
    )
    data = {
        "slot": 500,
        "lookup_slot": 500,
        "lookup_table_address": str(table_key),
        "lookup_table_info": {
            "owner": ADDRESS_LOOKUP_TABLE_PROGRAM_ID,
            "lamports": 1_000_000,
            "data": [base64.b64encode(lookup_table_raw([source, destination])).decode("ascii"), "base64"],
            "executable": False,
            "rentEpoch": 0,
            "space": 120,
        },
        "addresses": [str(source), str(destination)],
        "pre_accounts": [
            token_info(str(mint), str(source_owner), 1_000),
            token_info(str(mint), str(destination_owner), 10),
        ],
        "post_accounts": [
            token_info(str(mint), str(source_owner), 750),
            token_info(str(mint), str(destination_owner), 250),
        ],
    }
    return raw_message, intent, tx_policy, expected, data


def test_resolves_active_v0_lookup_table_and_simulates_exact_message():
    raw, intent, tx_policy, expected, data = v0_lookup_fixture()
    result = adapter(FakeSession(data)).simulate(
        raw,
        intent=intent,
        transaction_policy=tx_policy,
        expected_token_deltas=expected,
        minimum_context_slot=490,
        last_valid_block_height=950,
    )
    assert result.validation.message_sha256
    assert result.evidence.context_slot == 500
    assert len(result.evidence.token_balances) == 2


def test_rejects_deactivated_lookup_table_before_simulation():
    raw, intent, tx_policy, expected, data = v0_lookup_fixture()
    table_info = dict(data["lookup_table_info"])
    table_info["data"] = [
        base64.b64encode(lookup_table_raw(
            [Pubkey.from_string(expected[0].token_account), Pubkey.from_string(expected[1].token_account)],
            deactivation_slot=499,
        )).decode("ascii"),
        "base64",
    ]
    data["lookup_table_info"] = table_info
    with pytest.raises(Exception, match="deactivated"):
        adapter(FakeSession(data)).simulate(
            raw,
            intent=intent,
            transaction_policy=tx_policy,
            expected_token_deltas=expected,
            minimum_context_slot=490,
            last_valid_block_height=950,
        )


def test_captures_inner_cpi_program_ids_from_simulation_trace():
    raw, intent, tx_policy, expected, data = fixture()
    program = intent.instructions[0].program_id
    # The compiled program account is present in the validated static account list.
    message = from_bytes_versioned(raw)
    program_index = tuple(str(key) for key in message.account_keys).index(program)
    data["inner_instructions"] = [
        {
            "index": 0,
            "instructions": [
                {"programIdIndex": program_index, "accounts": [], "data": "", "stackHeight": 1}
            ],
        }
    ]
    result = adapter(FakeSession(data)).simulate(
        raw,
        intent=intent,
        transaction_policy=tx_policy,
        expected_token_deltas=expected,
        minimum_context_slot=490,
        last_valid_block_height=950,
    )
    assert result.evidence.top_level_program_ids == result.validation.program_ids
    assert result.evidence.inner_program_ids == (program,)
    assert result.evidence.inner_instruction_count == 1


def test_rejects_inner_cpi_program_index_outside_validated_account_list():
    raw, intent, tx_policy, expected, data = fixture()
    data["inner_instructions"] = [
        {
            "index": 0,
            "instructions": [
                {"programIdIndex": 255, "accounts": [], "data": "", "stackHeight": 1}
            ],
        }
    ]
    with pytest.raises(SolanaRpcAdapterError, match="outside the resolved account list"):
        adapter(FakeSession(data)).simulate(
            raw,
            intent=intent,
            transaction_policy=tx_policy,
            expected_token_deltas=expected,
            minimum_context_slot=490,
            last_valid_block_height=950,
        )



def test_preserves_full_inner_cpi_instruction_evidence():
    raw, intent, tx_policy, expected, data = fixture()
    message = from_bytes_versioned(raw)
    compiled = message.instructions[0]
    data["inner_instructions"] = [{
        "index": 0,
        "instructions": [{
            "programIdIndex": int(compiled.program_id_index),
            "accounts": list(bytes(compiled.accounts)),
            "data": "2",
            "stackHeight": 2,
        }],
    }]
    result = adapter(FakeSession(data)).simulate(
        raw,
        intent=intent,
        transaction_policy=tx_policy,
        expected_token_deltas=expected,
        minimum_context_slot=490,
        last_valid_block_height=950,
    )
    evidence = result.evidence
    assert evidence.inner_instruction_count == 1
    assert len(evidence.inner_instructions) == 1
    inner = evidence.inner_instructions[0]
    assert inner.parent_instruction_index == 0
    assert inner.ordinal == 0
    assert inner.program_id == result.validation.program_ids[0]
    assert inner.account_keys == tuple(result.validation.account_keys[i] for i in bytes(compiled.accounts))
    assert inner.data == b"\x01"
    assert inner.stack_height == 2


@pytest.mark.parametrize(
    ("inner", "message"),
    [
        ({"programIdIndex": 999, "accounts": [], "data": "", "stackHeight": 2}, "program index"),
        ({"programIdIndex": 0, "accounts": [999], "data": "", "stackHeight": 2}, "account index"),
        ({"programIdIndex": 0, "accounts": [], "data": "0", "stackHeight": 2}, "base58 character"),
        ({"programIdIndex": 0, "accounts": [], "data": "2", "stackHeight": 0}, "stack height"),
    ],
)
def test_rejects_malformed_inner_cpi_instruction_records(inner, message):
    raw, intent, tx_policy, expected, data = fixture()
    data["inner_instructions"] = [{"index": 0, "instructions": [inner]}]
    with pytest.raises(SolanaRpcAdapterError, match=message):
        adapter(FakeSession(data)).simulate(
            raw,
            intent=intent,
            transaction_policy=tx_policy,
            expected_token_deltas=expected,
            minimum_context_slot=490,
            last_valid_block_height=950,
        )


def test_rejects_inner_instruction_group_with_invalid_parent_index():
    raw, intent, tx_policy, expected, data = fixture()
    data["inner_instructions"] = [{"index": 1, "instructions": []}]
    with pytest.raises(SolanaRpcAdapterError, match="parent index"):
        adapter(FakeSession(data)).simulate(
            raw,
            intent=intent,
            transaction_policy=tx_policy,
            expected_token_deltas=expected,
            minimum_context_slot=490,
            last_valid_block_height=950,
        )
