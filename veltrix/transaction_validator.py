"""Fail-closed structural validator for exact, pre-approved Solana messages."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from solders.message import MessageV0, from_bytes_versioned
from solders.pubkey import Pubkey


ADDRESS_LOOKUP_TABLE_PROGRAM_ID = "AddressLookupTab1e1111111111111111111111111"
LOOKUP_TABLE_ACTIVE_DEACTIVATION_SLOT = (1 << 64) - 1
LOOKUP_TABLE_MAX_ADDRESSES = 256
MAX_TRANSACTION_ACCOUNT_KEYS = 128


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
class ResolvedLookupTable:
    table_address: str
    owner_program_id: str
    addresses: tuple[str, ...]
    deactivation_slot: int
    last_extended_slot: int
    context_slot: int

    def __post_init__(self) -> None:
        for field in ("table_address", "owner_program_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value or value.strip() != value:
                raise TransactionValidationError(f"{field} must be a canonical non-empty string")
            try:
                if str(Pubkey.from_string(value)) != value:
                    raise ValueError("non-canonical")
            except Exception as exc:
                raise TransactionValidationError(f"{field} must be a valid public key") from exc
        if not isinstance(self.addresses, tuple) or len(self.addresses) > LOOKUP_TABLE_MAX_ADDRESSES:
            raise TransactionValidationError("lookup table addresses must be an immutable tuple of at most 256 keys")
        for address in self.addresses:
            if not isinstance(address, str) or not address:
                raise TransactionValidationError("lookup table contains an invalid address")
            try:
                if str(Pubkey.from_string(address)) != address:
                    raise ValueError("non-canonical")
            except Exception as exc:
                raise TransactionValidationError("lookup table contains a non-canonical public key") from exc
        for field in ("deactivation_slot", "last_extended_slot", "context_slot"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TransactionValidationError(f"{field} must be a non-negative integer")


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
        if not isinstance(self.allow_address_lookup_tables, bool):
            raise TransactionValidationError("allow_address_lookup_tables must be a boolean")


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
    if (
        required < 1
        or required > account_count
        or readonly_signed > required
        or readonly_unsigned > account_count - required
    ):
        raise TransactionValidationError("message header contains impossible signer or writable counts")
    if index < 0 or index >= account_count:
        raise TransactionValidationError("account index is out of range")
    if index < required:
        return index < required - readonly_signed
    return index < account_count - readonly_unsigned


def _resolve_account_keys(
    message: object,
    *,
    policy: TransactionValidationPolicy,
    resolved_lookup_tables: tuple[ResolvedLookupTable, ...],
    current_slot: int | None,
    minimum_context_slot: int,
) -> tuple[tuple[str, ...], int, int]:
    """Return full runtime key order, static key count, and loaded-writable count."""
    static_keys = tuple(str(key) for key in message.account_keys)
    static_count = len(static_keys)
    if not isinstance(message, MessageV0) or not message.address_table_lookups:
        if resolved_lookup_tables:
            raise TransactionValidationError("lookup table evidence was supplied for a message without lookups")
        if current_slot is not None:
            raise TransactionValidationError("current_slot was supplied for a message without lookups")
        return static_keys, static_count, 0

    if not policy.allow_address_lookup_tables:
        raise TransactionValidationError("address lookup tables are disabled by transaction policy")
    if not isinstance(resolved_lookup_tables, tuple) or not resolved_lookup_tables:
        raise TransactionValidationError("resolved lookup table evidence is required")
    if isinstance(current_slot, bool) or not isinstance(current_slot, int) or current_slot < 0:
        raise TransactionValidationError("current_slot is required for address lookup table validation")
    if isinstance(minimum_context_slot, bool) or not isinstance(minimum_context_slot, int) or minimum_context_slot < 0:
        raise TransactionValidationError("minimum_context_slot must be a non-negative integer")

    supplied = {item.table_address: item for item in resolved_lookup_tables}
    if len(supplied) != len(resolved_lookup_tables):
        raise TransactionValidationError("duplicate resolved lookup table evidence")
    expected_table_addresses = tuple(str(item.account_key) for item in message.address_table_lookups)
    if len(expected_table_addresses) != len(supplied) or set(expected_table_addresses) != set(supplied):
        raise TransactionValidationError("resolved lookup table set does not exactly match message lookups")

    loaded_writable: list[str] = []
    loaded_readonly: list[str] = []
    for lookup in message.address_table_lookups:
        table_address = str(lookup.account_key)
        table = supplied[table_address]
        if table.owner_program_id != ADDRESS_LOOKUP_TABLE_PROGRAM_ID:
            raise TransactionValidationError("lookup table account is not owned by the address lookup table program")
        if table.deactivation_slot != LOOKUP_TABLE_ACTIVE_DEACTIVATION_SLOT:
            raise TransactionValidationError("deactivated or deactivating lookup tables are not supported")
        if table.last_extended_slot >= current_slot:
            raise TransactionValidationError("lookup table was extended in the current slot and is not yet eligible")
        if table.context_slot < minimum_context_slot or table.context_slot > current_slot:
            raise TransactionValidationError("lookup table evidence context slot is outside the required range")
        writable_indexes = tuple(bytes(lookup.writable_indexes))
        readonly_indexes = tuple(bytes(lookup.readonly_indexes))
        if len(set(writable_indexes + readonly_indexes)) != len(writable_indexes) + len(readonly_indexes):
            raise TransactionValidationError("lookup table indexes contain duplicates")
        for index in writable_indexes:
            if index >= len(table.addresses):
                raise TransactionValidationError("writable lookup index is outside the resolved table")
            loaded_writable.append(table.addresses[index])
        for index in readonly_indexes:
            if index >= len(table.addresses):
                raise TransactionValidationError("readonly lookup index is outside the resolved table")
            loaded_readonly.append(table.addresses[index])

    full_keys = static_keys + tuple(loaded_writable) + tuple(loaded_readonly)
    if len(full_keys) > MAX_TRANSACTION_ACCOUNT_KEYS:
        raise TransactionValidationError("resolved transaction account count exceeds the runtime account-lock limit")
    if len(full_keys) != len(set(full_keys)):
        raise TransactionValidationError("resolved transaction contains duplicate account keys")
    return full_keys, static_count, len(loaded_writable)


def validate_transaction_message(
    message_bytes: bytes,
    *,
    intent: TransactionIntent,
    policy: TransactionValidationPolicy,
    resolved_lookup_tables: tuple[ResolvedLookupTable, ...] = (),
    current_slot: int | None = None,
    minimum_context_slot: int = 0,
) -> TransactionValidationResult:
    """Validate exact message structure; never signs, simulates, or broadcasts.

    Legacy and v0 messages are supported. v0 address lookup tables require
    explicit, fresh, active, owner-verified resolution evidence. v1 messages
    and unresolved/ambiguous lookup tables are rejected.
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
        if isinstance(message, MessageV0):
            message.sanitize()
        elif message.has_duplicates():
            raise TransactionValidationError("legacy message contains duplicate account keys")
    except Exception as exc:
        raise TransactionValidationError("message could not be decoded and sanitized") from exc

    try:
        account_keys, static_count, loaded_writable_count = _resolve_account_keys(
            message,
            policy=policy,
            resolved_lookup_tables=resolved_lookup_tables,
            current_slot=current_slot,
            minimum_context_slot=minimum_context_slot,
        )
        instructions = tuple(message.instructions)
        recent_blockhash = str(message.recent_blockhash)
    except TransactionValidationError:
        raise
    except Exception as exc:
        raise TransactionValidationError("message or lookup table resolution is incomplete") from exc

    def is_signer(index: int) -> bool:
        if index < 0 or index >= len(account_keys):
            raise TransactionValidationError("account index is out of range")
        return message.is_signer(index) if index < static_count else False

    def is_writable(index: int) -> bool:
        if index < 0 or index >= len(account_keys):
            raise TransactionValidationError("account index is out of range")
        if index < static_count:
            return _is_writable(message, index, static_count)
        return index < static_count + loaded_writable_count

    if not account_keys or account_keys[0] != intent.fee_payer:
        raise TransactionValidationError("fee payer does not match the approved intent")
    if recent_blockhash != intent.recent_blockhash:
        raise TransactionValidationError("recent blockhash does not match the approved intent")
    if len(instructions) != len(intent.instructions):
        raise TransactionValidationError("instruction count does not match the approved intent")

    try:
        actual_signers = tuple(
            key for index, key in enumerate(account_keys[:static_count]) if is_signer(index)
        )
    except Exception as exc:
        raise TransactionValidationError("message signer privileges could not be inspected") from exc
    if actual_signers != intent.required_signers:
        raise TransactionValidationError("required signer set or ordering does not match the approved intent")
    if not is_signer(0) or not is_writable(0):
        raise TransactionValidationError("fee payer must be a writable signer")

    # The fee payer is an implicit message account even when no instruction references it.
    referenced_indices: set[int] = {0}
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
        if is_signer(program_index) or is_writable(program_index):
            raise TransactionValidationError(f"instruction {index} program account has unsafe signer or writable privileges")
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
            if is_signer(account_index) != account_expectation.is_signer:
                raise TransactionValidationError(f"instruction {index} signer privilege differs from approved intent")
            if is_writable(account_index) != account_expectation.is_writable:
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
