"""Fail-closed structural validator for exact, pre-approved Solana messages.

This validator deliberately supports legacy and v0 messages without address
lookup tables. It does not infer arbitrary program semantics: each instruction's
program, ordered account privileges, and complete data bytes must match an
independently approved intent. Unknown instruction forms are rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from solders.message import MessageV0, from_bytes_versioned


class TransactionValidationError(ValueError):
    """Raised when a Solana message is malformed or outside its approved intent."""


@dataclass(frozen=True, slots=True)
class AccountExpectation:
    public_key: str
    is_signer: bool
    is_writable: bool

    def __post_init__(self) -> None:
        if not isinstance(self.public_key, str) or not self.public_key or self.public_key.strip() != self.public_key:
            raise TransactionValidationError("account public_key must be canonical")
        if not isinstance(self.is_signer, bool) or not isinstance(self.is_writable, bool):
            raise TransactionValidationError("account privileges must be booleans")


@dataclass(frozen=True, slots=True)
class InstructionExpectation:
    program_id: str
    accounts: tuple[AccountExpectation, ...]
    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.program_id, str) or not self.program_id or self.program_id.strip() != self.program_id:
            raise TransactionValidationError("program_id must be canonical")
        if not isinstance(self.accounts, tuple) or any(not isinstance(a, AccountExpectation) for a in self.accounts):
            raise TransactionValidationError("accounts must be an immutable tuple of AccountExpectation")
        if not isinstance(self.data, bytes):
            raise TransactionValidationError("instruction data must be immutable bytes")


@dataclass(frozen=True, slots=True)
class TransactionIntent:
    fee_payer: str
    required_signers: tuple[str, ...]
    recent_blockhash: str
    instructions: tuple[InstructionExpectation, ...]

    def __post_init__(self) -> None:
        for name in ("fee_payer", "recent_blockhash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value.strip() != value:
                raise TransactionValidationError(f"{name} must be canonical")
        if not isinstance(self.required_signers, tuple) or not self.required_signers:
            raise TransactionValidationError("required_signers must be a non-empty immutable tuple")
        if any(not isinstance(k, str) or not k or k.strip() != k for k in self.required_signers):
            raise TransactionValidationError("required signer keys must be canonical")
        if len(set(self.required_signers)) != len(self.required_signers):
            raise TransactionValidationError("required_signers must not contain duplicates")
        if self.fee_payer not in self.required_signers:
            raise TransactionValidationError("fee payer must be a required signer")
        if not isinstance(self.instructions, tuple) or not self.instructions:
            raise TransactionValidationError("instructions must be a non-empty immutable tuple")
        if any(not isinstance(ix, InstructionExpectation) for ix in self.instructions):
            raise TransactionValidationError("instructions contain an invalid expectation")


@dataclass(frozen=True, slots=True)
class TransactionValidationPolicy:
    allowed_program_ids: tuple[str, ...]
    max_message_bytes: int = 1232
    allow_address_lookup_tables: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_program_ids, tuple):
            raise TransactionValidationError("allowed_program_ids must be an immutable tuple")
        if any(not isinstance(p, str) or not p or p.strip() != p for p in self.allowed_program_ids):
            raise TransactionValidationError("allowed program IDs must be canonical")
        if len(set(self.allowed_program_ids)) != len(self.allowed_program_ids):
            raise TransactionValidationError("allowed_program_ids must not contain duplicates")
        if isinstance(self.max_message_bytes, bool) or not isinstance(self.max_message_bytes, int) or not 1 <= self.max_message_bytes <= 1232:
            raise TransactionValidationError("max_message_bytes must be between 1 and 1232")
        if self.allow_address_lookup_tables is not False:
            raise TransactionValidationError("address lookup tables are disabled in this release")


@dataclass(frozen=True, slots=True)
class TransactionValidationResult:
    message_sha256: str
    message_size: int
    fee_payer: str
    required_signers: tuple[str, ...]
    program_ids: tuple[str, ...]
    instruction_count: int


def _is_writable(message: object, index: int, account_count: int) -> bool:
    """Derive static-key writability from Solana's canonical message header."""
    header = message.header
    required = int(header.num_required_signatures)
    readonly_signed = int(header.num_readonly_signed_accounts)
    readonly_unsigned = int(header.num_readonly_unsigned_accounts)
    if index < 0 or index >= account_count:
        raise TransactionValidationError("account index is out of range")
    if index < required:
        return index < required - readonly_signed
    return index < account_count - readonly_unsigned


def validate_transaction_message(
    message_bytes: bytes,
    *,
    intent: TransactionIntent,
    policy: TransactionValidationPolicy,
) -> TransactionValidationResult:
    """Validate exact message structure; never signs, simulates, or broadcasts.

    Address lookup tables are rejected because this implementation does not
    resolve their loaded addresses. Only legacy and v0 static-account messages
    are accepted. Each instruction's complete data and account privileges must
    match the immutable intent byte-for-byte.
    """
    if not isinstance(message_bytes, bytes) or not message_bytes:
        raise TransactionValidationError("message_bytes must be non-empty immutable bytes")
    if not isinstance(intent, TransactionIntent):
        raise TransactionValidationError("intent must be a TransactionIntent")
    if not isinstance(policy, TransactionValidationPolicy):
        raise TransactionValidationError("policy must be a TransactionValidationPolicy")
    if len(message_bytes) > policy.max_message_bytes:
        raise TransactionValidationError("message exceeds the configured size limit")

    try:
        message = from_bytes_versioned(message_bytes)
        # Legacy messages expose duplicate-key validation; v0 also exposes
        # sanitize for compiled indices and lookup-table descriptors.
        if isinstance(message, MessageV0):
            message.sanitize()
        elif message.has_duplicates():
            raise TransactionValidationError("legacy message contains duplicate account keys")
    except Exception as exc:
        raise TransactionValidationError("message could not be decoded and sanitized") from exc

    if isinstance(message, MessageV0) and message.address_table_lookups:
        raise TransactionValidationError("address lookup tables are unsupported and must be resolved before validation")

    try:
        account_keys = tuple(str(key) for key in message.account_keys)
        instructions = tuple(message.instructions)
        recent_blockhash = str(message.recent_blockhash)
    except Exception as exc:
        raise TransactionValidationError("message is missing required structural fields") from exc

    if not account_keys or account_keys[0] != intent.fee_payer:
        raise TransactionValidationError("fee payer does not match the approved intent")
    if recent_blockhash != intent.recent_blockhash:
        raise TransactionValidationError("recent blockhash does not match the approved intent")
    if len(instructions) != len(intent.instructions):
        raise TransactionValidationError("instruction count does not match the approved intent")

    try:
        actual_signers = tuple(
            key for index, key in enumerate(account_keys) if message.is_signer(index)
        )
    except Exception as exc:
        raise TransactionValidationError("message signer privileges could not be inspected") from exc
    if actual_signers != intent.required_signers:
        raise TransactionValidationError("required signer set or ordering does not match the approved intent")
    if not message.is_signer(0) or not _is_writable(message, 0, len(account_keys)):
        raise TransactionValidationError("fee payer must be a writable signer")

    referenced_indices: set[int] = set()
    actual_program_ids: list[str] = []
    for index, (compiled, expected) in enumerate(zip(instructions, intent.instructions)):
        try:
            program_index = int(compiled.program_id_index)
            account_indices = tuple(int(value) for value in bytes(compiled.accounts))
            data = bytes(compiled.data)
        except Exception as exc:
            raise TransactionValidationError(f"instruction {index} is malformed") from exc

        if program_index < 0 or program_index >= len(account_keys):
            raise TransactionValidationError(f"instruction {index} program index is out of range")
        if any(account_index < 0 or account_index >= len(account_keys) for account_index in account_indices):
            raise TransactionValidationError(f"instruction {index} account index is out of range")

        program_id = account_keys[program_index]
        if program_id not in policy.allowed_program_ids:
            raise TransactionValidationError(f"instruction {index} invokes a non-allowlisted program")
        if program_id != expected.program_id:
            raise TransactionValidationError(f"instruction {index} program ID differs from approved intent")
        if data != expected.data:
            raise TransactionValidationError(f"instruction {index} data differs from approved intent")
        if len(account_indices) != len(expected.accounts):
            raise TransactionValidationError(f"instruction {index} account count differs from approved intent")

        referenced_indices.add(program_index)
        for account_index, account_expectation in zip(account_indices, expected.accounts):
            actual_key = account_keys[account_index]
            if actual_key != account_expectation.public_key:
                raise TransactionValidationError(f"instruction {index} account key differs from approved intent")
            if message.is_signer(account_index) != account_expectation.is_signer:
                raise TransactionValidationError(f"instruction {index} signer privilege differs from approved intent")
            if _is_writable(message, account_index, len(account_keys)) != account_expectation.is_writable:
                raise TransactionValidationError(f"instruction {index} writable privilege differs from approved intent")
            referenced_indices.add(account_index)
        actual_program_ids.append(program_id)

    if referenced_indices != set(range(len(account_keys))):
        raise TransactionValidationError("message contains unexpected or unused account keys")

    return TransactionValidationResult(
        message_sha256=hashlib.sha256(message_bytes).hexdigest(),
        message_size=len(message_bytes),
        fee_payer=intent.fee_payer,
        required_signers=actual_signers,
        program_ids=tuple(actual_program_ids),
        instruction_count=len(instructions),
    )
