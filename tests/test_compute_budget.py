"""Adversarial tests for top-level Solana Compute Budget decoding."""
import dataclasses
import hashlib
import struct

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey

from veltrix.compute_budget import (
    COMPUTE_BUDGET_PROGRAM_ID,
    DEFAULT_COMPUTE_UNIT_LIMIT,
    ComputeBudgetError,
    ComputeBudgetPolicy,
    validate_compute_budget_instructions,
)
from veltrix.transaction_validator import TransactionValidationResult


JUPITER_PROGRAM_ID = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"


def fixture(compute_instructions=()):
    payer = Pubkey.new_unique()
    jupiter = Pubkey.from_string(JUPITER_PROGRAM_ID)
    instructions = [Instruction(jupiter, b"jupiter-route", [])]
    instructions.extend(compute_instructions)
    message = MessageV0.try_compile(payer, instructions, [], Hash.new_unique())
    message_bytes = to_bytes_versioned(message)
    program_ids = tuple(
        str(message.account_keys[int(ix.program_id_index)])
        for ix in message.instructions
    )
    validation = TransactionValidationResult(
        message_sha256=hashlib.sha256(message_bytes).hexdigest(),
        message_size=len(message_bytes),
        fee_payer=str(payer),
        required_signers=(str(payer),),
        program_ids=program_ids,
        instruction_count=len(message.instructions),
        account_keys=tuple(str(key) for key in message.account_keys),
    )
    return message_bytes, validation


def set_limit(value):
    return Instruction(
        Pubkey.from_string(COMPUTE_BUDGET_PROGRAM_ID),
        bytes([2]) + struct.pack("<I", value),
        [],
    )


def set_price(value):
    return Instruction(
        Pubkey.from_string(COMPUTE_BUDGET_PROGRAM_ID),
        bytes([3]) + struct.pack("<Q", value),
        [],
    )


def test_accepts_absent_compute_budget_instructions_with_protocol_default():
    message, validation = fixture()
    result = validate_compute_budget_instructions(message, validation=validation)
    assert result.instruction_count == 0
    assert result.compute_unit_limit is None
    assert result.compute_unit_price_micro_lamports is None
    assert result.effective_compute_unit_limit == DEFAULT_COMPUTE_UNIT_LIMIT
    assert result.estimated_priority_fee_lamports == 0


def test_decodes_compute_unit_limit_and_price_and_estimates_fee():
    message, validation = fixture((set_limit(250_000), set_price(1_000)))
    result = validate_compute_budget_instructions(message, validation=validation)
    assert result.instruction_count == 2
    assert result.compute_unit_limit == 250_000
    assert result.compute_unit_price_micro_lamports == 1_000
    assert result.effective_compute_unit_limit == 250_000
    assert result.estimated_priority_fee_lamports == 250


def test_rounds_priority_fee_up_to_whole_lamports():
    message, validation = fixture((set_limit(200_001), set_price(1)))
    result = validate_compute_budget_instructions(message, validation=validation)
    assert result.estimated_priority_fee_lamports == 1


def test_accepts_zero_compute_unit_price():
    message, validation = fixture((set_price(0),))
    result = validate_compute_budget_instructions(message, validation=validation)
    assert result.compute_unit_price_micro_lamports == 0
    assert result.estimated_priority_fee_lamports == 0


def test_rejects_duplicate_compute_budget_instructions():
    message, validation = fixture((set_limit(200_000), set_limit(250_000)))
    with pytest.raises(ComputeBudgetError, match="duplicate SetComputeUnitLimit"):
        validate_compute_budget_instructions(message, validation=validation)

    message, validation = fixture((set_price(1), set_price(2)))
    with pytest.raises(ComputeBudgetError, match="duplicate SetComputeUnitPrice"):
        validate_compute_budget_instructions(message, validation=validation)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00" + b"\x00" * 8,
        b"\x01" + b"\x00" * 4,
        b"\x04" + b"\x00" * 4,
        b"\xff",
        b"\x02",
        b"\x02" + b"\x00" * 5,
        b"\x03" + b"\x00" * 9,
    ],
)
def test_rejects_unknown_deprecated_and_malformed_instruction_data(data):
    instruction = Instruction(Pubkey.from_string(COMPUTE_BUDGET_PROGRAM_ID), data, [])
    message, validation = fixture((instruction,))
    with pytest.raises(ComputeBudgetError):
        validate_compute_budget_instructions(message, validation=validation)


def test_rejects_zero_or_over_policy_compute_unit_limit():
    message, validation = fixture((set_limit(0),))
    with pytest.raises(ComputeBudgetError, match="zero or exceeds policy"):
        validate_compute_budget_instructions(message, validation=validation)

    message, validation = fixture((set_limit(300_001),))
    with pytest.raises(ComputeBudgetError, match="zero or exceeds policy"):
        validate_compute_budget_instructions(message, validation=validation)


def test_rejects_compute_unit_price_over_policy():
    message, validation = fixture((set_price(10_000_001),))
    with pytest.raises(ComputeBudgetError, match="price exceeds policy"):
        validate_compute_budget_instructions(message, validation=validation)


def test_rejects_priority_fee_over_lamport_budget():
    message, validation = fixture((set_limit(300_000), set_price(10_000_000)))
    policy = ComputeBudgetPolicy(max_priority_fee_lamports=1_000_000)
    with pytest.raises(ComputeBudgetError, match="priority fee exceeds"):
        validate_compute_budget_instructions(message, validation=validation, policy=policy)


def test_can_require_explicit_compute_unit_limit():
    message, validation = fixture()
    policy = ComputeBudgetPolicy(require_compute_unit_limit=True)
    with pytest.raises(ComputeBudgetError, match="explicit SetComputeUnitLimit"):
        validate_compute_budget_instructions(message, validation=validation, policy=policy)


def test_rejects_compute_budget_instruction_with_account_metas():
    account = Pubkey.new_unique()
    instruction = Instruction(
        Pubkey.from_string(COMPUTE_BUDGET_PROGRAM_ID),
        bytes([2]) + struct.pack("<I", 200_000),
        [AccountMeta(account, False, False)],
    )
    message, validation = fixture((instruction,))
    with pytest.raises(ComputeBudgetError, match="must not contain account metas"):
        validate_compute_budget_instructions(message, validation=validation)


def test_rejects_message_digest_or_program_trace_mismatch():
    message, validation = fixture((set_limit(200_000),))
    with pytest.raises(ComputeBudgetError, match="digest"):
        validate_compute_budget_instructions(
            message,
            validation=dataclasses.replace(validation, message_sha256="0" * 64),
        )
    with pytest.raises(ComputeBudgetError, match="program IDs"):
        validate_compute_budget_instructions(
            message,
            validation=dataclasses.replace(validation, program_ids=("not-the-program",)),
        )


def test_rejects_incomplete_policy_configuration():
    with pytest.raises(ComputeBudgetError, match="between 1"):
        ComputeBudgetPolicy(max_compute_unit_limit=0)
    with pytest.raises(ComputeBudgetError, match="boolean"):
        ComputeBudgetPolicy(require_compute_unit_limit=1)
