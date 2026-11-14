"""Narrow classic SPL Token account and transfer-instruction decoder.

Only the classic SPL Token program's base account layouts and Transfer /
TransferChecked wire formats are decoded here. Token-2022 extensions, multisig
authority layouts, native-wSOL effects, and every other token instruction are
deliberately unsupported and must fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from solders.pubkey import Pubkey


SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SPL_TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
_U64_MAX = (1 << 64) - 1


class SplTokenDecodeError(ValueError):
    """Raised when a token account or instruction is outside supported scope."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _canonical_key(value: str, reason: str) -> str:
    try:
        return str(Pubkey.from_string(value))
    except Exception:
        raise SplTokenDecodeError(reason) from None


def _option_pubkey(data: bytes, offset: int, reason: str) -> str | None:
    if offset < 0 or offset + 36 > len(data):
        raise SplTokenDecodeError("token_option_field_truncated")
    tag = int.from_bytes(data[offset:offset + 4], "little")
    raw = data[offset + 4:offset + 36]
    if tag == 0:
        return None
    if tag == 1:
        try:
            return str(Pubkey.from_bytes(raw))
        except Exception:
            raise SplTokenDecodeError(reason) from None
    raise SplTokenDecodeError("token_option_tag_invalid")


@dataclass(frozen=True, slots=True)
class ClassicSplTokenAccount:
    address: str
    token_authority: str
    mint: str
    amount: int
    delegate: str | None
    delegated_amount: int
    close_authority: str | None
    native_reserve_lamports: int | None
    state: str
    data_sha256: str


@dataclass(frozen=True, slots=True)
class ClassicSplTokenMint:
    address: str
    supply: int
    decimals: int
    mint_authority: str | None
    freeze_authority: str | None
    data_sha256: str


@dataclass(frozen=True, slots=True)
class ClassicSplTokenTransfer:
    instruction_name: str
    source: str
    destination: str
    authority: str
    mint: str | None
    amount: int
    decimals: int | None
    execution_approved: bool = False


def decode_classic_token_account(
    address: str, program_owner: str, account_data: bytes
) -> ClassicSplTokenAccount:
    """Decode the exact 165-byte classic SPL Token Account layout."""
    address = _canonical_key(address, "token_account_address_invalid")
    if program_owner != SPL_TOKEN_PROGRAM_ID:
        if program_owner == SPL_TOKEN_2022_PROGRAM_ID:
            raise SplTokenDecodeError("token_2022_account_not_supported")
        raise SplTokenDecodeError("token_account_program_owner_unsupported")
    if not isinstance(account_data, bytes) or len(account_data) != 165:
        raise SplTokenDecodeError("classic_token_account_length_invalid")

    mint = str(Pubkey.from_bytes(account_data[0:32]))
    token_authority = str(Pubkey.from_bytes(account_data[32:64]))
    amount = int.from_bytes(account_data[64:72], "little")
    delegate = _option_pubkey(account_data, 72, "token_delegate_invalid")
    state_value = account_data[108]
    states = {0: "uninitialized", 1: "initialized", 2: "frozen"}
    state = states.get(state_value)
    if state is None:
        raise SplTokenDecodeError("token_account_state_invalid")
    if state != "initialized":
        raise SplTokenDecodeError("token_account_not_transferable")
    native_tag = int.from_bytes(account_data[109:113], "little")
    if native_tag == 0:
        native_reserve = None
    elif native_tag == 1:
        native_reserve = int.from_bytes(account_data[113:121], "little")
    else:
        raise SplTokenDecodeError("token_native_option_tag_invalid")
    delegated_amount = int.from_bytes(account_data[121:129], "little")
    close_authority = _option_pubkey(account_data, 129, "token_close_authority_invalid")
    return ClassicSplTokenAccount(
        address=address,
        token_authority=token_authority,
        mint=mint,
        amount=amount,
        delegate=delegate,
        delegated_amount=delegated_amount,
        close_authority=close_authority,
        native_reserve_lamports=native_reserve,
        state=state,
        data_sha256=hashlib.sha256(account_data).hexdigest(),
    )


def decode_classic_token_mint(
    address: str, program_owner: str, account_data: bytes
) -> ClassicSplTokenMint:
    """Decode the exact 82-byte classic SPL Token Mint layout."""
    address = _canonical_key(address, "token_mint_address_invalid")
    if program_owner != SPL_TOKEN_PROGRAM_ID:
        if program_owner == SPL_TOKEN_2022_PROGRAM_ID:
            raise SplTokenDecodeError("token_2022_mint_not_supported")
        raise SplTokenDecodeError("token_mint_program_owner_unsupported")
    if not isinstance(account_data, bytes) or len(account_data) != 82:
        raise SplTokenDecodeError("classic_token_mint_length_invalid")
    mint_authority = _option_pubkey(account_data, 0, "token_mint_authority_invalid")
    supply = int.from_bytes(account_data[36:44], "little")
    decimals = account_data[44]
    initialized = account_data[45]
    if initialized != 1:
        raise SplTokenDecodeError("token_mint_not_initialized")
    if decimals > 38:
        raise SplTokenDecodeError("token_mint_decimals_out_of_range")
    freeze_authority = _option_pubkey(account_data, 46, "token_freeze_authority_invalid")
    return ClassicSplTokenMint(
        address=address,
        supply=supply,
        decimals=decimals,
        mint_authority=mint_authority,
        freeze_authority=freeze_authority,
        data_sha256=hashlib.sha256(account_data).hexdigest(),
    )


def decode_classic_token_transfer(
    program_id: str, accounts: tuple[str, ...], instruction_data: bytes
) -> ClassicSplTokenTransfer:
    """Decode only classic SPL Token Transfer (3) and TransferChecked (12)."""
    if program_id == SPL_TOKEN_2022_PROGRAM_ID:
        raise SplTokenDecodeError("token_2022_instruction_not_supported")
    if program_id != SPL_TOKEN_PROGRAM_ID:
        raise SplTokenDecodeError("token_instruction_program_unsupported")
    if not isinstance(accounts, tuple) or not isinstance(instruction_data, bytes):
        raise SplTokenDecodeError("token_instruction_input_invalid")
    if not instruction_data:
        raise SplTokenDecodeError("token_instruction_data_empty")

    tag = instruction_data[0]
    if tag == 3:
        if len(instruction_data) != 9 or len(accounts) != 3:
            raise SplTokenDecodeError("token_transfer_layout_invalid")
        source, destination, authority = accounts
        mint = None
        amount = int.from_bytes(instruction_data[1:9], "little")
        decimals = None
        name = "Transfer"
    elif tag == 12:
        if len(instruction_data) != 10 or len(accounts) != 4:
            raise SplTokenDecodeError("token_transfer_checked_layout_invalid")
        source, mint, destination, authority = accounts
        amount = int.from_bytes(instruction_data[1:9], "little")
        decimals = instruction_data[9]
        if decimals > 38:
            raise SplTokenDecodeError("token_transfer_decimals_out_of_range")
        name = "TransferChecked"
    else:
        raise SplTokenDecodeError("token_instruction_not_supported")

    source = _canonical_key(source, "token_transfer_source_invalid")
    destination = _canonical_key(destination, "token_transfer_destination_invalid")
    authority = _canonical_key(authority, "token_transfer_authority_invalid")
    if mint is not None:
        mint = _canonical_key(mint, "token_transfer_mint_invalid")
    if source == destination:
        raise SplTokenDecodeError("token_transfer_source_destination_alias")
    if amount <= 0 or amount > _U64_MAX:
        raise SplTokenDecodeError("token_transfer_amount_invalid")
    return ClassicSplTokenTransfer(
        instruction_name=name,
        source=source,
        destination=destination,
        authority=authority,
        mint=mint,
        amount=amount,
        decimals=decimals,
    )
