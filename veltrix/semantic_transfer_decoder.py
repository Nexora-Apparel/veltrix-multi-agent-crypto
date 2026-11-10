"""Narrow semantic decoder for direct System and classic SPL Token transfers.

This is intentionally not a DEX/swap decoder. Unknown programs, Token-2022,
multisig, authority changes, account creation, and unsupported instructions fail
closed until dedicated parsers and account-state policy exist.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict

from solders.message import MessageV0, from_bytes_versioned

from veltrix.transaction_validator import (
    TransactionIntent,
    TransactionValidationPolicy,
    TransactionValidationResult,
    validate_transaction_message,
)


SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"
SUPPORTED_PROGRAMS = frozenset({
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    COMPUTE_BUDGET_PROGRAM_ID,
})


class SemanticDecodeError(ValueError):
    """Raised when a transaction instruction is unknown or semantically unsafe."""


@dataclass(frozen=True, slots=True)
class ExpectedTokenTransfer:
    source: str
    mint: str
    destination: str
    authority: str
    amount_raw: int
    decimals: int


@dataclass(frozen=True, slots=True)
class ExpectedNativeTransfer:
    source: str
    destination: str
    lamports: int


@dataclass(frozen=True, slots=True)
class SemanticDecodePolicy:
    max_native_lamports_per_transaction: int
    max_token_amount_raw_per_mint: int
    max_compute_units: int
    max_compute_unit_price_micro_lamports: int

    def __post_init__(self) -> None:
        for field in (
            "max_native_lamports_per_transaction",
            "max_token_amount_raw_per_mint",
            "max_compute_units",
            "max_compute_unit_price_micro_lamports",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SemanticDecodeError(f"{field} must be a non-negative integer")
        if self.max_native_lamports_per_transaction == 0 or self.max_token_amount_raw_per_mint == 0:
            raise SemanticDecodeError("native and token transfer ceilings must be positive")
        if self.max_compute_units == 0 or self.max_compute_units > 1_400_000:
            raise SemanticDecodeError("max_compute_units must be between 1 and 1,400,000")


@dataclass(frozen=True, slots=True)
class DecodedTokenTransfer:
    source: str
    mint: str
    destination: str
    authority: str
    amount_raw: int
    decimals: int


@dataclass(frozen=True, slots=True)
class DecodedNativeTransfer:
    source: str
    destination: str
    lamports: int


@dataclass(frozen=True, slots=True)
class SemanticDecodeResult:
    message_sha256: str
    token_transfers: tuple[DecodedTokenTransfer, ...]
    native_transfers: tuple[DecodedNativeTransfer, ...]
    compute_unit_limit: int | None
    compute_unit_price_micro_lamports: int | None
    total_native_lamports: int
    token_amounts_by_mint: tuple[tuple[str, int], ...]


def _is_writable(message: object, index: int, account_count: int) -> bool:
    header = message.header
    required = int(header.num_required_signatures)
    readonly_signed = int(header.num_readonly_signed_accounts)
    readonly_unsigned = int(header.num_readonly_unsigned_accounts)
    if (
        required < 1
        or required > account_count
        or readonly_signed > required
        or readonly_unsigned > account_count - required
    ):
        raise SemanticDecodeError("message header contains impossible account privilege counts")
    if index < 0 or index >= account_count:
        raise SemanticDecodeError("instruction account index is out of range")
    if index < required:
        return index < required - readonly_signed
    return index < account_count - readonly_unsigned


def decode_transaction_semantics(
    message_bytes: bytes,
    *,
    intent: TransactionIntent,
    validation: TransactionValidationResult,
    transaction_policy: TransactionValidationPolicy,
    policy: SemanticDecodePolicy,
    expected_token_transfers: tuple[ExpectedTokenTransfer, ...],
    expected_native_transfers: tuple[ExpectedNativeTransfer, ...],
) -> SemanticDecodeResult:
    """Decode only explicit direct-transfer primitives and compare approved effects."""
    if not isinstance(policy, SemanticDecodePolicy):
        raise SemanticDecodeError("semantic decode policy is required")
    if not isinstance(expected_token_transfers, tuple) or not isinstance(expected_native_transfers, tuple):
        raise SemanticDecodeError("expected effects must be immutable tuples")
    if any(not isinstance(x, ExpectedTokenTransfer) for x in expected_token_transfers):
        raise SemanticDecodeError("expected token effects contain invalid records")
    if any(not isinstance(x, ExpectedNativeTransfer) for x in expected_native_transfers):
        raise SemanticDecodeError("expected native effects contain invalid records")

    try:
        actual_validation = validate_transaction_message(
            message_bytes,
            intent=intent,
            policy=transaction_policy,
        )
    except Exception as exc:
        raise SemanticDecodeError("structural transaction validation failed") from exc
    if actual_validation != validation:
        raise SemanticDecodeError("supplied validation result does not match the exact message")

    try:
        message = from_bytes_versioned(message_bytes)
        if isinstance(message, MessageV0):
            message.sanitize()
        account_keys = tuple(str(key) for key in message.account_keys)
        instructions = tuple(message.instructions)
    except Exception as exc:
        raise SemanticDecodeError("message could not be decoded for semantic inspection") from exc

    if isinstance(message, MessageV0) and message.address_table_lookups:
        raise SemanticDecodeError("address lookup tables are unsupported")
    if not instructions:
        raise SemanticDecodeError("transaction contains no instructions")

    decoded_tokens: list[DecodedTokenTransfer] = []
    decoded_native: list[DecodedNativeTransfer] = []
    compute_unit_limit: int | None = None
    compute_unit_price: int | None = None

    for ix_number, instruction in enumerate(instructions):
        try:
            program_index = int(instruction.program_id_index)
            account_indices = tuple(int(value) for value in bytes(instruction.accounts))
            data = bytes(instruction.data)
        except Exception as exc:
            raise SemanticDecodeError(f"instruction {ix_number} is malformed") from exc
        if program_index < 0 or program_index >= len(account_keys):
            raise SemanticDecodeError(f"instruction {ix_number} program index is out of range")
        if any(index < 0 or index >= len(account_keys) for index in account_indices):
            raise SemanticDecodeError(f"instruction {ix_number} account index is out of range")

        program_id = account_keys[program_index]
        if program_id == TOKEN_2022_PROGRAM_ID:
            raise SemanticDecodeError("Token-2022 instructions are not approved until extension semantics are verified")
        if program_id not in SUPPORTED_PROGRAMS:
            raise SemanticDecodeError(f"instruction {ix_number} invokes an unsupported program")
        if not data:
            raise SemanticDecodeError(f"instruction {ix_number} has empty instruction data")

        if program_id == SYSTEM_PROGRAM_ID:
            if len(data) != 12 or int.from_bytes(data[0:4], "little") != 2:
                raise SemanticDecodeError("only the exact System Program Transfer instruction is supported")
            if len(account_indices) != 2:
                raise SemanticDecodeError("System Program Transfer must have exactly two accounts")
            source_index, destination_index = account_indices
            amount = int.from_bytes(data[4:12], "little")
            if amount <= 0:
                raise SemanticDecodeError("native transfer amount must be positive")
            if not message.is_signer(source_index) or not _is_writable(message, source_index, len(account_keys)):
                raise SemanticDecodeError("native transfer source must be a writable signer")
            if not _is_writable(message, destination_index, len(account_keys)):
                raise SemanticDecodeError("native transfer destination must be writable")
            decoded_native.append(DecodedNativeTransfer(
                source=account_keys[source_index],
                destination=account_keys[destination_index],
                lamports=amount,
            ))
            continue

        if program_id == TOKEN_PROGRAM_ID:
            if len(data) != 10 or data[0] != 12:
                raise SemanticDecodeError("only SPL Token TransferChecked with exact data length is supported")
            if len(account_indices) != 4:
                raise SemanticDecodeError("SPL Token TransferChecked must have exactly four accounts")
            source_index, mint_index, destination_index, authority_index = account_indices
            amount = int.from_bytes(data[1:9], "little")
            decimals = data[9]
            if amount <= 0:
                raise SemanticDecodeError("token transfer amount must be positive")
            if not _is_writable(message, source_index, len(account_keys)):
                raise SemanticDecodeError("token transfer source must be writable")
            if _is_writable(message, mint_index, len(account_keys)):
                raise SemanticDecodeError("token mint account must be read-only")
            if not _is_writable(message, destination_index, len(account_keys)):
                raise SemanticDecodeError("token transfer destination must be writable")
            if not message.is_signer(authority_index):
                raise SemanticDecodeError("token transfer authority must be a required signer")
            decoded_tokens.append(DecodedTokenTransfer(
                source=account_keys[source_index],
                mint=account_keys[mint_index],
                destination=account_keys[destination_index],
                authority=account_keys[authority_index],
                amount_raw=amount,
                decimals=decimals,
            ))
            continue

        if program_id == COMPUTE_BUDGET_PROGRAM_ID:
            if account_indices:
                raise SemanticDecodeError("Compute Budget instructions must not reference accounts")
            tag = data[0]
            if tag == 2 and len(data) == 5:
                if compute_unit_limit is not None:
                    raise SemanticDecodeError("duplicate compute-unit limit instruction")
                compute_unit_limit = int.from_bytes(data[1:5], "little")
                if compute_unit_limit <= 0 or compute_unit_limit > policy.max_compute_units:
                    raise SemanticDecodeError("compute-unit limit exceeds policy")
            elif tag == 3 and len(data) == 9:
                if compute_unit_price is not None:
                    raise SemanticDecodeError("duplicate compute-unit price instruction")
                compute_unit_price = int.from_bytes(data[1:9], "little")
                if compute_unit_price > policy.max_compute_unit_price_micro_lamports:
                    raise SemanticDecodeError("compute-unit price exceeds policy")
            else:
                raise SemanticDecodeError("unsupported Compute Budget instruction")
            continue

    native_total = sum(item.lamports for item in decoded_native)
    if native_total > policy.max_native_lamports_per_transaction:
        raise SemanticDecodeError("aggregate native transfer amount exceeds policy")

    token_totals: dict[str, int] = defaultdict(int)
    for item in decoded_tokens:
        token_totals[item.mint] += item.amount_raw
    if any(total > policy.max_token_amount_raw_per_mint for total in token_totals.values()):
        raise SemanticDecodeError("aggregate token transfer amount for a mint exceeds policy")

    expected_tokens = tuple(
        DecodedTokenTransfer(
            source=x.source,
            mint=x.mint,
            destination=x.destination,
            authority=x.authority,
            amount_raw=x.amount_raw,
            decimals=x.decimals,
        )
        for x in expected_token_transfers
    )
    expected_native = tuple(
        DecodedNativeTransfer(x.source, x.destination, x.lamports)
        for x in expected_native_transfers
    )
    if tuple(decoded_tokens) != expected_tokens:
        raise SemanticDecodeError("decoded token transfers do not exactly match approved effects")
    if tuple(decoded_native) != expected_native:
        raise SemanticDecodeError("decoded native transfers do not exactly match approved effects")

    return SemanticDecodeResult(
        message_sha256=validation.message_sha256,
        token_transfers=tuple(decoded_tokens),
        native_transfers=tuple(decoded_native),
        compute_unit_limit=compute_unit_limit,
        compute_unit_price_micro_lamports=compute_unit_price,
        total_native_lamports=native_total,
        token_amounts_by_mint=tuple(sorted(token_totals.items())),
    )
