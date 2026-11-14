from __future__ import annotations

import pytest

from veltrix.spl_token_decoder import (
    SPL_TOKEN_PROGRAM_ID,
    SPL_TOKEN_2022_PROGRAM_ID,
    SplTokenDecodeError,
    decode_classic_token_account,
    decode_classic_token_mint,
    decode_classic_token_transfer,
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


def token_account_data(
    *, amount=1000, state=1, delegate_tag=0, delegate_key=bytes(32),
    native_tag=0, native_reserve=0, delegated_amount=0, close_tag=0,
):
    data = bytearray(165)
    data[0:32] = bytes([3]) * 32
    data[32:64] = bytes([4]) * 32
    data[64:72] = amount.to_bytes(8, "little")
    data[72:76] = delegate_tag.to_bytes(4, "little")
    data[76:108] = delegate_key
    data[108] = state
    data[109:113] = native_tag.to_bytes(4, "little")
    data[113:121] = native_reserve.to_bytes(8, "little")
    data[121:129] = delegated_amount.to_bytes(8, "little")
    data[129:133] = close_tag.to_bytes(4, "little")
    return bytes(data)


def mint_data(*, decimals=6, supply=5000, initialized=1):
    data = bytearray(82)
    data[0:4] = (0).to_bytes(4, "little")
    data[36:44] = supply.to_bytes(8, "little")
    data[44] = decimals
    data[45] = initialized
    data[46:50] = (0).to_bytes(4, "little")
    return bytes(data)


def test_decodes_classic_token_account_fields_and_hash():
    raw = token_account_data(amount=12345, native_tag=1, native_reserve=890)
    account = decode_classic_token_account(key(1), SPL_TOKEN_PROGRAM_ID, raw)
    assert account.address == key(1)
    assert account.mint == key(3)
    assert account.token_authority == key(4)
    assert account.amount == 12345
    assert account.native_reserve_lamports == 890
    assert account.state == "initialized"
    assert len(account.data_sha256) == 64


def test_decodes_classic_token_mint():
    mint = decode_classic_token_mint(key(3), SPL_TOKEN_PROGRAM_ID, mint_data())
    assert mint.address == key(3)
    assert mint.supply == 5000
    assert mint.decimals == 6
    assert mint.mint_authority is None
    assert mint.freeze_authority is None


def test_decodes_transfer_and_transfer_checked_wire_formats():
    accounts = (key(1), key(2), key(4))
    transfer = decode_classic_token_transfer(
        SPL_TOKEN_PROGRAM_ID, accounts, b"\x03" + (77).to_bytes(8, "little")
    )
    assert transfer.instruction_name == "Transfer"
    assert transfer.source == key(1)
    assert transfer.destination == key(2)
    assert transfer.authority == key(4)
    assert transfer.amount == 77
    assert transfer.mint is None
    assert transfer.execution_approved is False

    checked = decode_classic_token_transfer(
        SPL_TOKEN_PROGRAM_ID,
        (key(1), key(3), key(2), key(4)),
        b"\x0c" + (88).to_bytes(8, "little") + b"\x06",
    )
    assert checked.instruction_name == "TransferChecked"
    assert checked.mint == key(3)
    assert checked.amount == 88
    assert checked.decimals == 6


@pytest.mark.parametrize(
    "owner,data,reason",
    [
        (SPL_TOKEN_2022_PROGRAM_ID, token_account_data(), "token_2022_account_not_supported"),
        ("not-a-token-program", token_account_data(), "token_account_program_owner_unsupported"),
        (SPL_TOKEN_PROGRAM_ID, token_account_data() + b"\x00", "classic_token_account_length_invalid"),
        (SPL_TOKEN_PROGRAM_ID, token_account_data(state=2), "token_account_not_transferable"),
        (SPL_TOKEN_PROGRAM_ID, token_account_data(state=0), "token_account_not_transferable"),
    ],
)
def test_rejects_unsupported_or_nontransferable_token_accounts(owner, data, reason):
    with pytest.raises(SplTokenDecodeError, match=reason):
        decode_classic_token_account(key(1), owner, data)


def test_rejects_invalid_token_account_option_tag():
    raw = bytearray(token_account_data())
    raw[72:76] = (2).to_bytes(4, "little")
    with pytest.raises(SplTokenDecodeError, match="token_option_tag_invalid"):
        decode_classic_token_account(key(1), SPL_TOKEN_PROGRAM_ID, bytes(raw))


def test_rejects_invalid_or_uninitialized_mint():
    with pytest.raises(SplTokenDecodeError, match="token_mint_not_initialized"):
        decode_classic_token_mint(key(3), SPL_TOKEN_PROGRAM_ID, mint_data(initialized=0))
    with pytest.raises(SplTokenDecodeError, match="token_2022_mint_not_supported"):
        decode_classic_token_mint(key(3), SPL_TOKEN_2022_PROGRAM_ID, mint_data())


@pytest.mark.parametrize(
    "program,accounts,data,reason",
    [
        (SPL_TOKEN_2022_PROGRAM_ID, (key(1), key(2), key(4)), b"\x03" + bytes(8), "token_2022_instruction_not_supported"),
        ("not-token", (key(1), key(2), key(4)), b"\x03" + bytes(8), "token_instruction_program_unsupported"),
        (SPL_TOKEN_PROGRAM_ID, (key(1), key(2), key(4)), b"\x03", "token_transfer_layout_invalid"),
        (SPL_TOKEN_PROGRAM_ID, (key(1), key(2), key(4)), b"\xff", "token_instruction_not_supported"),
        (SPL_TOKEN_PROGRAM_ID, (key(1), key(2), key(4), key(5)), b"\x03" + bytes(8), "token_transfer_layout_invalid"),
    ],
)
def test_rejects_unsupported_token_transfer_shapes(program, accounts, data, reason):
    with pytest.raises(SplTokenDecodeError, match=reason):
        decode_classic_token_transfer(program, accounts, data)


def test_parser_preserves_wire_valid_zero_amount_and_u8_decimals():
    transfer = decode_classic_token_transfer(
        SPL_TOKEN_PROGRAM_ID,
        (key(1), key(1), key(4)),
        b"\x03" + bytes(8),
    )
    assert transfer.amount == 0
    assert transfer.source == transfer.destination

    mint = decode_classic_token_mint(
        key(3), SPL_TOKEN_PROGRAM_ID, mint_data(decimals=255)
    )
    assert mint.decimals == 255
