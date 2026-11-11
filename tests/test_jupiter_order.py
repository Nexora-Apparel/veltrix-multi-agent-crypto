"""Adversarial tests for Jupiter v0 order intake without signing."""
import base64
import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0, to_bytes_versioned
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from veltrix.jupiter_order import (
    JUPITER_ORDER_URL,
    JupiterOrderIntakeError,
    JupiterOrderPolicy,
    JupiterSwapV2OrderAdapter,
)


class FakeResponse:
    status_code = 200

    def __init__(self, body, status_code=200, headers=None):
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, body, status_code=200, headers=None):
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}
        self.calls = []

    def get(self, url, *, params, headers, timeout, allow_redirects):
        self.calls.append((url, params, headers, timeout, allow_redirects))
        assert url == JUPITER_ORDER_URL
        assert params["maxSupportedTransactionVersion"] == "0"
        assert params["swapMode"] == "ExactIn"
        assert allow_redirects is False
        return FakeResponse(self.body, self.status_code, self.headers)


def order_fixture(*, use_lookup_table=False):
    taker = Pubkey.new_unique()
    input_mint = Pubkey.new_unique()
    output_mint = Pubkey.new_unique()
    amm = Pubkey.new_unique()
    program = Pubkey.new_unique()
    blockhash = Hash.new_unique()
    if use_lookup_table:
        lookup_key = Pubkey.new_unique()
        looked_up_account = Pubkey.new_unique()
        table = AddressLookupTableAccount(key=lookup_key, addresses=[looked_up_account])
        instruction = Instruction(
            program,
            b"jupiter-route-instruction",
            [
                AccountMeta(taker, True, True),
                AccountMeta(looked_up_account, False, True),
            ],
        )
        message = MessageV0.try_compile(taker, [instruction], [table], blockhash)
    else:
        instruction = Instruction(program, b"jupiter-route-instruction", [])
        message = MessageV0.try_compile(taker, [instruction], [], blockhash)
    transaction = VersionedTransaction.populate(
        message,
        [Signature.default() for _ in range(int(message.header.num_required_signatures))],
    )
    body = {
        "requestId": "order-123",
        "quoteId": "quote-456",
        "taker": str(taker),
        "inputMint": str(input_mint),
        "outputMint": str(output_mint),
        "inAmount": "1000000",
        "outAmount": "2500000",
        "otherAmountThreshold": "2475000",
        "priceImpact": 0.35,
        "feeBps": 50,
        "signatureFeeLamports": 5000,
        "prioritizationFeeLamports": 10000,
        "rentFeeLamports": 2039280,
        "signatureFeePayer": str(taker),
        "prioritizationFeePayer": str(taker),
        "rentFeePayer": str(taker),
        "gasless": False,
        "router": "metis",
        "transactionVersion": 0,
        "lastValidBlockHeight": "950",
        "transaction": base64.b64encode(bytes(transaction)).decode("ascii"),
        "routePlan": [
            {
                "swapInfo": {
                    "ammKey": str(amm),
                    "label": "Raydium CLMM",
                    "inputMint": str(input_mint),
                    "outputMint": str(output_mint),
                    "inAmount": "1000000",
                    "outAmount": "2500000",
                },
                "percent": 100,
                "bps": 10000,
            }
        ],
    }
    return body, message, transaction


def client(body):
    return JupiterSwapV2OrderAdapter(
        api_key="jupiter-test-key",
        timeout_seconds=5.0,
        session=FakeSession(body),
        clock_ms=lambda: 1_005_000,
    )


def request(body, policy=None):
    return client(body).order_exact_in(
        body["inputMint"],
        body["outputMint"],
        1_000_000,
        taker=body["taker"],
        policy=policy or JupiterOrderPolicy(),
    )



def test_order_policy_rejects_routers_without_semantic_decoder():
    assert JupiterOrderPolicy().allowed_routers == ("metis",)
    for router in ("dflow", "okx", "jupiterz"):
        with pytest.raises(JupiterOrderIntakeError, match="only Metis"):
            JupiterOrderPolicy(allowed_routers=(router,))


def test_accepts_unsigned_v0_order_and_extracts_exact_message():
    body, message, transaction = order_fixture()
    result = request(body)
    assert result.provider_id == "jupiter-swap-v2"
    assert result.request_id == "order-123"
    assert result.taker == body["taker"]
    assert result.fee_payer == body["taker"]
    assert result.transaction_version == 0
    assert result.last_valid_block_height == 950
    assert result.message_bytes == to_bytes_versioned(message)
    assert result.serialized_transaction_bytes == bytes(transaction)
    assert result.router == "metis"
    assert result.route_plan[0].label == "Raydium CLMM"


def test_preserves_v0_message_with_address_lookup_table_for_next_validation_layer():
    body, message, _ = order_fixture(use_lookup_table=True)
    result = request(body)
    assert message.address_table_lookups
    parsed = VersionedTransaction.from_bytes(result.serialized_transaction_bytes)
    assert parsed.message.address_table_lookups
    assert result.message_bytes == to_bytes_versioned(parsed.message)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("taker", str(Pubkey.new_unique()), "taker does not match"),
        ("gasless", True, "gasless"),
        ("router", "jupiterz", "outside local policy"),
        ("transactionVersion", 1, "version 0"),
        ("lastValidBlockHeight", "bad", "lastValidBlockHeight"),
        ("inAmount", "999", "input amount differs"),
        ("otherAmountThreshold", "2600000", "threshold exceeds"),
        ("priceImpact", 6.0, "price impact exceeds"),
        ("feeBps", 301, "fee basis points exceed"),
        ("signatureFeePayer", str(Pubkey.new_unique()), "signatureFeePayer"),
        ("prioritizationFeePayer", str(Pubkey.new_unique()), "prioritizationFeePayer"),
        ("rentFeePayer", str(Pubkey.new_unique()), "rentFeePayer"),
    ],
)
def test_rejects_unsafe_order_metadata(field, value, message):
    body, _, _ = order_fixture()
    requested_taker = body["taker"]
    body[field] = value
    with pytest.raises(JupiterOrderIntakeError, match=message):
        client(body).order_exact_in(
            body["inputMint"], body["outputMint"], 1_000_000,
            taker=requested_taker, policy=JupiterOrderPolicy(),
        )


def test_rejects_invalid_base64_transaction():
    body, _, _ = order_fixture()
    body["transaction"] = "not-base64!"
    with pytest.raises(JupiterOrderIntakeError, match="valid serialized v0"):
        request(body)


def test_rejects_transaction_with_existing_signature():
    body, message, _ = order_fixture()
    signed_like = VersionedTransaction.populate(message, [Signature.new_unique()])
    body["transaction"] = base64.b64encode(bytes(signed_like)).decode("ascii")
    with pytest.raises(JupiterOrderIntakeError, match="partially signed"):
        request(body)


def test_rejects_fee_payer_not_equal_to_taker():
    body, message, _ = order_fixture()
    other_payer = Pubkey.new_unique()
    # Build a different v0 transaction whose first static key is not the requested taker.
    altered = MessageV0.try_compile(
        other_payer,
        [Instruction(Pubkey.new_unique(), b"data", [])],
        [],
        Hash.new_unique(),
    )
    tx = VersionedTransaction.populate(altered, [Signature.default()])
    body["transaction"] = base64.b64encode(bytes(tx)).decode("ascii")
    with pytest.raises(JupiterOrderIntakeError, match="fee payer"):
        request(body)


def test_rejects_missing_transaction_and_invalid_route():
    body, _, _ = order_fixture()
    body["transaction"] = None
    with pytest.raises(JupiterOrderIntakeError, match="omitted its transaction"):
        request(body)

    body, _, _ = order_fixture()
    body["routePlan"] = []
    with pytest.raises(JupiterOrderIntakeError, match="routePlan"):
        request(body)


def test_requests_only_allowed_routers_and_no_legacy_transaction_versions():
    body, _, _ = order_fixture()
    session = FakeSession(body)
    api = JupiterSwapV2OrderAdapter(
        api_key="jupiter-test-key",
        session=session,
        clock_ms=lambda: 1_005_000,
    )
    api.order_exact_in(
        body["inputMint"], body["outputMint"], 1_000_000,
        taker=body["taker"],
        policy=JupiterOrderPolicy(),
    )
    params = session.calls[0][1]
    assert "jupiterz" in params["excludeRouters"]
    assert params["maxSupportedTransactionVersion"] == "0"
    assert "taker" in params


def test_provider_rejection_preserves_safe_code_message_and_request_id():
    body, _, _ = order_fixture()
    body = {
        "errorCode": "INSUFFICIENT_BALANCE",
        "error": {"message": "Taker account has insufficient balance"},
        "requestId": "jup-req-789",
    }
    api = JupiterSwapV2OrderAdapter(session=FakeSession(body))
    with pytest.raises(JupiterOrderIntakeError) as caught:
        api.order_exact_in(
            str(Pubkey.new_unique()), str(Pubkey.new_unique()), 1_000_000,
            taker=str(Pubkey.new_unique()), policy=JupiterOrderPolicy(),
        )
    assert caught.value.provider_error_code == "INSUFFICIENT_BALANCE"
    assert caught.value.provider_error_message == "Taker account has insufficient balance"
    assert caught.value.provider_request_id == "jup-req-789"
    assert caught.value.http_status == 200


def test_http_error_preserves_provider_diagnostics_without_raw_payload():
    body = {"errorCode": "RATE_LIMITED", "error": "Too many requests"}
    api = JupiterSwapV2OrderAdapter(
        session=FakeSession(body, status_code=429, headers={"x-request-id": "req-429"})
    )
    with pytest.raises(JupiterOrderIntakeError) as caught:
        api.order_exact_in(
            str(Pubkey.new_unique()), str(Pubkey.new_unique()), 1_000_000,
            taker=str(Pubkey.new_unique()), policy=JupiterOrderPolicy(),
        )
    assert caught.value.http_status == 429
    assert caught.value.provider_error_code == "RATE_LIMITED"
    assert caught.value.provider_error_message == "Too many requests"
    assert caught.value.provider_request_id == "req-429"
