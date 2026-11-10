"""Tests for the provider-pinned, read-only Solana simulation adapter."""
import base64
import dataclasses

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from veltrix.solana_rpc_simulation import (
    SolanaRpcAdapterError,
    SolanaRpcSimulationAdapter,
)
from veltrix.simulation_gate import ExpectedTokenDelta, SimulationGatePolicy, evaluate_simulation
from veltrix.transaction_validator import (
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
                },
            }
        elif method == "getGenesisHash":
            result = "test-genesis"
        elif method == "isBlockhashValid":
            result = {"context": {"slot": self.fixture["slot"]}, "value": True}
        elif method == "getBlockHeight":
            result = 900
        elif method == "getMultipleAccounts":
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
