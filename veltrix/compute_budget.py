"""Strict decoder for top-level Solana Compute Budget instructions.

Only SetComputeUnitLimit and SetComputeUnitPrice are supported. Unknown,
deprecated, duplicate, malformed, or over-policy instructions fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import struct

from solders.message import MessageV0, from_bytes_versioned

from veltrix.transaction_validator import TransactionValidationResult


COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"
DEFAULT_COMPUTE_UNIT_LIMIT = 200_000
SOLANA_MAX_COMPUTE_UNIT_LIMIT = 1_400_000


class ComputeBudgetError(ValueError):
    """Raised when compute-budget instructions are malformed or exceed policy."""


@dataclass(frozen=True, slots=True)
class ComputeBudgetPolicy:
    max_compute_unit_limit: int = 300_000
    max_compute_unit_price_micro_lamports: int = 10_000_000
    max_priority_fee_lamports: int = 5_000_000
    require_compute_unit_limit: bool = False

    def __post_init__(self) -> None:
        for field in (
            "max_compute_unit_limit",
            "max_compute_unit_price_micro_lamports",
            "max_priority_fee_lamports",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ComputeBudgetError(f"{field} must be a non-negative integer")
        if not 1 <= self.max_compute_unit_limit <= SOLANA_MAX_COMPUTE_UNIT_LIMIT:
            raise ComputeBudgetError("max_compute_unit_limit must be between 1 and the Solana transaction maximum")
        if not isinstance(self.require_compute_unit_limit, bool):
            raise ComputeBudgetError("require_compute_unit_limit must be a boolean")


@dataclass(frozen=True, slots=True)
class ComputeBudgetResult:
    compute_unit_limit: int | None
    compute_unit_price_micro_lamports: int | None
    effective_compute_unit_limit: int
    estimated_priority_fee_lamports: int
    instruction_count: int


def _decode_instruction(data: bytes, policy: ComputeBudgetPolicy) -> tuple[str, int]:
    if not isinstance(data, bytes) or not data:
        raise ComputeBudgetError("compute-budget instruction data is empty or malformed")
    tag = data[0]
    if tag == 2:
        if len(data) != 5:
            raise ComputeBudgetError("SetComputeUnitLimit must contain exactly one u32 value")
        units = struct.unpack("<I", data[1:])[0]
        if units <= 0 or units > policy.max_compute_unit_limit:
            raise ComputeBudgetError("requested compute-unit limit is zero or exceeds policy")
        return "limit", units
    if tag == 3:
        if len(data) != 9:
            raise ComputeBudgetError("SetComputeUnitPrice must contain exactly one u64 value")
        price = struct.unpack("<Q", data[1:])[0]
        if price > policy.max_compute_unit_price_micro_lamports:
            raise ComputeBudgetError("compute-unit price exceeds policy")
        return "price", price
    raise ComputeBudgetError(f"unsupported Compute Budget instruction discriminator {tag}")


def validate_compute_budget_instructions(
    message_bytes: bytes,
    *,
    validation: TransactionValidationResult,
    policy: ComputeBudgetPolicy | None = None,
) -> ComputeBudgetResult:
    """Decode every top-level Compute Budget instruction in a validated v0 message."""
    if not isinstance(message_bytes, bytes) or not message_bytes:
        raise ComputeBudgetError("serialized message bytes are required")
    if not isinstance(validation, TransactionValidationResult):
        raise ComputeBudgetError("successful structural transaction validation is required")
    policy = policy or ComputeBudgetPolicy()
    if not isinstance(policy, ComputeBudgetPolicy):
        raise ComputeBudgetError("a ComputeBudgetPolicy is required")

    digest = hashlib.sha256(message_bytes).hexdigest()
    if digest != validation.message_sha256:
        raise ComputeBudgetError("message digest differs from structural validation evidence")

    try:
        message = from_bytes_versioned(message_bytes)
        if not isinstance(message, MessageV0):
            raise ComputeBudgetError("only v0 messages are supported by this compute-budget decoder")
        message.sanitize()
        if len(message.instructions) != validation.instruction_count:
            raise ComputeBudgetError("instruction count differs from structural validation evidence")
        account_keys = tuple(str(key) for key in message.account_keys)
        if account_keys != validation.account_keys:
            raise ComputeBudgetError("static account keys differ from structural validation evidence")
        program_ids = tuple(
            account_keys[int(ix.program_id_index)]
            for ix in message.instructions
        )
        if program_ids != validation.program_ids:
            raise ComputeBudgetError("top-level program IDs differ from structural validation evidence")
    except ComputeBudgetError:
        raise
    except Exception as exc:
        raise ComputeBudgetError("v0 message could not be decoded and inspected") from exc

    unit_limit = None
    unit_price = None
    count = 0
    for ix_index, instruction in enumerate(message.instructions):
        program_id = account_keys[int(instruction.program_id_index)]
        if program_id != COMPUTE_BUDGET_PROGRAM_ID:
            continue
        count += 1
        if bytes(instruction.accounts):
            raise ComputeBudgetError("Compute Budget instructions must not contain account metas")
        kind, value = _decode_instruction(bytes(instruction.data), policy)
        if kind == "limit":
            if unit_limit is not None:
                raise ComputeBudgetError("duplicate SetComputeUnitLimit instructions are not allowed")
            unit_limit = value
        elif kind == "price":
            if unit_price is not None:
                raise ComputeBudgetError("duplicate SetComputeUnitPrice instructions are not allowed")
            unit_price = value
        else:
            raise ComputeBudgetError(f"unhandled Compute Budget instruction at index {ix_index}")

    if policy.require_compute_unit_limit and unit_limit is None:
        raise ComputeBudgetError("an explicit SetComputeUnitLimit instruction is required")
    effective_limit = unit_limit if unit_limit is not None else DEFAULT_COMPUTE_UNIT_LIMIT
    effective_price = unit_price if unit_price is not None else 0
    estimated_priority_fee = math.ceil(effective_limit * effective_price / 1_000_000)
    if estimated_priority_fee > policy.max_priority_fee_lamports:
        raise ComputeBudgetError("estimated priority fee exceeds policy")

    return ComputeBudgetResult(
        compute_unit_limit=unit_limit,
        compute_unit_price_micro_lamports=unit_price,
        effective_compute_unit_limit=effective_limit,
        estimated_priority_fee_lamports=estimated_priority_fee,
        instruction_count=count,
    )
