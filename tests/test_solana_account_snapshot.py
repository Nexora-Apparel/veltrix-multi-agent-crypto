from __future__ import annotations

import base64

import pytest

from veltrix.solana_account_snapshot import (
    SolanaAccountSnapshotError,
    SolanaAccountSnapshotProvider,
)


ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(raw: bytes) -> str:
    zeros = len(raw) - len(raw.lstrip(b"\x00"))
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = ALPHABET[remainder] + encoded
    return "1" * zeros + (encoded or ("" if zeros else "1"))


def key(byte: int) -> str:
    return b58(bytes([byte]) * 32)


class Response:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def json(self):
        return self.body


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def account(owner: str, data: bytes = b"mint-account"):
    return {
        "lamports": 1_000,
        "owner": owner,
        "executable": False,
        "rentEpoch": 9,
        "space": len(data),
        "data": [base64.b64encode(data).decode("ascii"), "base64"],
    }


def provider(session, **kwargs):
    return SolanaAccountSnapshotProvider(
        "https://rpc.example.test/?api-key=must-not-be-recorded",
        expected_genesis_hash=key(9),
        session=session,
        clock_ms=lambda: 123456,
        **kwargs,
    )


def test_fetches_ordered_accounts_from_one_context_and_hashes_evidence():
    session = Session(
        Response({"jsonrpc": "2.0", "id": 1, "result": key(9)}),
        Response({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "context": {"slot": 500},
                "value": [account(key(8), b"first"), account(key(7), b"second")],
            },
        }),
    )
    snapshot = provider(session).fetch_snapshot([key(1), key(2)], min_context_slot=490)

    assert snapshot.context_slot == 500
    assert snapshot.commitment == "finalized"
    assert snapshot.requested_addresses == (key(1), key(2))
    assert snapshot.account(key(1)).data == b"first"
    assert snapshot.account(key(2)).data == b"second"
    assert snapshot.endpoint_origin == "https://rpc.example.test"
    assert "api-key" not in snapshot.endpoint_origin
    assert len(snapshot.account_state_hash) == 64
    request = session.calls[1][1]["json"]["params"]
    assert request[1]["minContextSlot"] == 490
    assert request[1]["commitment"] == "finalized"


def test_rejects_rpc_genesis_mismatch_before_account_query():
    session = Session(Response({"result": key(8)}))
    with pytest.raises(SolanaAccountSnapshotError, match="rpc_genesis_hash_mismatch"):
        provider(session).fetch_snapshot([key(1)])
    assert len(session.calls) == 1


def test_rejects_context_slot_below_requested_minimum():
    session = Session(
        Response({"result": key(9)}),
        Response({"result": {"context": {"slot": 99}, "value": [account(key(8))]}}),
    )
    with pytest.raises(SolanaAccountSnapshotError, match="multiple_accounts_slot_below_minimum"):
        provider(session).fetch_snapshot([key(1)], min_context_slot=100)


def test_rejects_missing_required_account_but_can_record_optional_missing():
    session = Session(
        Response({"result": key(9)}),
        Response({"result": {"context": {"slot": 10}, "value": [None]}}),
    )
    with pytest.raises(SolanaAccountSnapshotError, match="required_account_missing"):
        provider(session).fetch_snapshot([key(1)])

    session = Session(
        Response({"result": key(9)}),
        Response({"result": {"context": {"slot": 10}, "value": [None]}}),
    )
    snapshot = provider(session).fetch_snapshot([key(1)], require_all_accounts=False)
    assert snapshot.accounts == (None,)


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda value: {**value, "space": 999}, "account_space_mismatch"),
        (lambda value: {**value, "owner": "not-a-public-key"}, "account_owner_invalid"),
        (lambda value: {**value, "data": ["%%%bad", "base64"]}, "account_data_base64_invalid"),
        (lambda value: {**value, "executable": 1}, "account_executable_invalid"),
        (lambda value: {**value, "lamports": True}, "account_lamports_invalid"),
    ],
)
def test_rejects_malformed_account_state(mutate, reason):
    session = Session(
        Response({"result": key(9)}),
        Response({"result": {"context": {"slot": 10}, "value": [mutate(account(key(8)))]}}),
    )
    with pytest.raises(SolanaAccountSnapshotError, match=reason):
        provider(session).fetch_snapshot([key(1)])


def test_rejects_duplicate_addresses_and_more_than_one_hundred_accounts():
    session = Session()
    with pytest.raises(SolanaAccountSnapshotError, match="duplicate_account_address"):
        provider(session).fetch_snapshot([key(1), key(1)])
    with pytest.raises(SolanaAccountSnapshotError, match="account_count_out_of_range"):
        provider(session).fetch_snapshot([key(1)] * 101)


def test_rejects_http_rpc_url_and_redirects():
    with pytest.raises(SolanaAccountSnapshotError, match="rpc_url_must_use_https"):
        SolanaAccountSnapshotProvider("http://rpc.example.test", expected_genesis_hash=key(9))
    session = Session(Response({}, status_code=302))
    with pytest.raises(SolanaAccountSnapshotError, match="rpc_redirect_not_allowed"):
        provider(session).fetch_snapshot([key(1)])


def test_rejects_account_lookup_outside_snapshot():
    session = Session(
        Response({"result": key(9)}),
        Response({"result": {"context": {"slot": 10}, "value": [account(key(8))]}}),
    )
    snapshot = provider(session).fetch_snapshot([key(1)])
    with pytest.raises(SolanaAccountSnapshotError, match="account_not_in_snapshot"):
        snapshot.account(key(2))
