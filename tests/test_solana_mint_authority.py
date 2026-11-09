import base64
import struct

import pytest

from veltrix.providers.base import ProviderError
from veltrix.providers.solana_mint import (
    ACCOUNT_TYPE_MINT,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    SolanaMintAuthorityProvider,
)

MINT = "So11111111111111111111111111111111111111112"
AUTHORITY = bytes(range(1, 33))


def mint_data(*, mint_authority=AUTHORITY, freeze_authority=None, supply=123456, decimals=6):
    data = bytearray(82)
    if mint_authority is None:
        struct.pack_into("<I", data, 0, 0)
    else:
        struct.pack_into("<I", data, 0, 1)
        data[4:36] = mint_authority
    struct.pack_into("<Q", data, 36, supply)
    data[44] = decimals
    data[45] = 1
    if freeze_authority is None:
        struct.pack_into("<I", data, 46, 0)
    else:
        struct.pack_into("<I", data, 46, 1)
        data[50:82] = freeze_authority
    return bytes(data)


def token_2022_data(extension_entries=()):
    data = bytearray(mint_data(mint_authority=None, freeze_authority=None))
    data.extend(bytes(83))
    data.append(ACCOUNT_TYPE_MINT)
    for extension_type, payload in extension_entries:
        data.extend(struct.pack("<HH", extension_type, len(payload)))
        data.extend(payload)
    data.extend(bytes(4))
    return bytes(data)


def test_classic_mint_decodes_authorities_supply_and_provenance():
    snapshot = SolanaMintAuthorityProvider.parse_account(
        MINT, TOKEN_PROGRAM_ID, mint_data(), finalized_slot=99, observed_at_ms=10_000,
    )
    assert snapshot.mint == MINT
    assert snapshot.mint_authority is not None
    assert snapshot.freeze_authority is None
    assert snapshot.supply_raw == 123456
    assert snapshot.decimals == 6
    assert snapshot.extension_parse_complete is True
    assert snapshot.finalized_slot == 99
    assert len(snapshot.evidence_id) == 64


def test_revoked_authorities_are_explicit_none():
    snapshot = SolanaMintAuthorityProvider.parse_account(
        MINT, TOKEN_PROGRAM_ID,
        mint_data(mint_authority=None, freeze_authority=None),
        finalized_slot=99, observed_at_ms=10_000,
    )
    assert snapshot.mint_authority is None
    assert snapshot.freeze_authority is None


def test_token_2022_parses_known_and_unknown_extensions_without_dropping_unknown():
    snapshot = SolanaMintAuthorityProvider.parse_account(
        MINT, TOKEN_2022_PROGRAM_ID,
        token_2022_data([(12, bytes(32)), (14, bytes(64)), (65000, b"x")]),
        finalized_slot=100, observed_at_ms=10_000,
    )
    assert snapshot.extension_types == (
        "permanent_delegate", "transfer_hook", "unknown_65000",
    )
    assert snapshot.unknown_extension_ids == (65000,)
    assert snapshot.extension_parse_complete is False


@pytest.mark.parametrize("data", [
    bytes(81),
    mint_data() + b"x",
    token_2022_data()[:165],
    token_2022_data()[:165] + b"\x02" + bytes(4),
    token_2022_data([(12, b"abc")])[:-2],
])
def test_malformed_mint_layouts_fail_closed(data):
    owner = TOKEN_2022_PROGRAM_ID if len(data) != 83 else TOKEN_PROGRAM_ID
    with pytest.raises(ProviderError):
        SolanaMintAuthorityProvider.parse_account(
            MINT, owner, data, finalized_slot=1, observed_at_ms=1,
        )


def test_classic_program_rejects_non_82_byte_account():
    with pytest.raises(ProviderError, match="exactly 82"):
        SolanaMintAuthorityProvider.parse_account(
            MINT, TOKEN_PROGRAM_ID, mint_data() + b"x",
            finalized_slot=1, observed_at_ms=1,
        )


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def post(self, url, *, json, timeout):
        self.calls.append((url, json, timeout))
        return FakeResponse(self.body)


def test_rpc_fetch_requests_finalized_base64_and_never_exposes_endpoint():
    encoded = base64.b64encode(mint_data()).decode()
    body = {
        "jsonrpc": "2.0", "id": 1,
        "result": {
            "context": {"slot": 1234},
            "value": {
                "owner": TOKEN_PROGRAM_ID, "executable": False,
                "data": [encoded, "base64"],
            },
        },
    }
    session = FakeSession(body)
    provider = SolanaMintAuthorityProvider(
        "https://rpc.example/?api-key=private", session=session,
        clock_ms=lambda: 55_000,
    )
    result = provider.fetch_snapshot(MINT)
    assert result.finalized_slot == 1234
    assert result.observed_at_ms == 55_000
    assert session.calls[0][1]["params"][1]["commitment"] == "finalized"
    assert session.calls[0][1]["params"][1]["encoding"] == "base64"


def test_rpc_fetch_rejects_missing_account_and_malformed_response():
    provider = SolanaMintAuthorityProvider(
        "https://rpc.example", session=FakeSession({
            "jsonrpc": "2.0", "id": 1,
            "result": {"context": {"slot": 1}, "value": None},
        }),
    )
    with pytest.raises(ProviderError, match="does not exist"):
        provider.fetch_snapshot(MINT)
