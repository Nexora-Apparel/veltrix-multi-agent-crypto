"""Adversarial tests for the exact-intent Solana transaction validator."""
import pytest

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message, MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.address_lookup_table_account import AddressLookupTableAccount

from veltrix.transaction_validator import (
    AccountExpectation,
    InstructionExpectation,
    TransactionIntent,
    TransactionValidationError,
    TransactionValidationPolicy,
    validate_transaction_message,
)


def fixture_message():
    payer = Pubkey.new_unique()
    source = Pubkey.new_unique()
    destination = Pubkey.new_unique()
    program = Pubkey.new_unique()
    blockhash = Hash.new_unique()
    data = b"approved-instruction-v1"
    instruction = Instruction(
        program,
        data,
        [
            AccountMeta(payer, True, True),
            AccountMeta(source, False, True),
            AccountMeta(destination, False, False),
        ],
    )
    message = Message.new_with_blockhash([instruction], payer, blockhash)
    intent = TransactionIntent(
        fee_payer=str(payer),
        required_signers=(str(payer),),
        recent_blockhash=str(blockhash),
        instructions=(
            InstructionExpectation(
                program_id=str(program),
                accounts=(
                    AccountExpectation(str(payer), True, True),
                    AccountExpectation(str(source), False, True),
                    AccountExpectation(str(destination), False, False),
                ),
                data=data,
            ),
        ),
    )
    policy = TransactionValidationPolicy(allowed_program_ids=(str(program),))
    return message, intent, policy


def test_accepts_exact_legacy_message_and_returns_audit_digest():
    message, intent, policy = fixture_message()
    raw = to_bytes_versioned(message)
    result = validate_transaction_message(raw, intent=intent, policy=policy)
    assert result.message_sha256
    assert result.message_size == len(raw)
    assert result.fee_payer == intent.fee_payer
    assert result.instruction_count == 1
    assert result.program_ids == (intent.instructions[0].program_id,)


def test_rejects_malformed_bytes():
    _, intent, policy = fixture_message()
    with pytest.raises(TransactionValidationError):
        validate_transaction_message(b"not a solana message", intent=intent, policy=policy)


@pytest.mark.parametrize("raw", [b"", bytearray(b"mutable")])
def test_rejects_empty_or_mutable_message(raw):
    _, intent, policy = fixture_message()
    with pytest.raises(TransactionValidationError):
        validate_transaction_message(raw, intent=intent, policy=policy)


def test_rejects_fee_payer_mismatch():
    message, intent, policy = fixture_message()
    wrong = TransactionIntent(
        fee_payer=str(Pubkey.new_unique()),
        required_signers=intent.required_signers,
        recent_blockhash=intent.recent_blockhash,
        instructions=intent.instructions,
    )
    with pytest.raises(TransactionValidationError, match="fee payer"):
        validate_transaction_message(to_bytes_versioned(message), intent=wrong, policy=policy)


def test_rejects_blockhash_mismatch():
    message, intent, policy = fixture_message()
    wrong = TransactionIntent(
        fee_payer=intent.fee_payer,
        required_signers=intent.required_signers,
        recent_blockhash=str(Hash.new_unique()),
        instructions=intent.instructions,
    )
    with pytest.raises(TransactionValidationError, match="blockhash"):
        validate_transaction_message(to_bytes_versioned(message), intent=wrong, policy=policy)


def test_rejects_instruction_data_mutation():
    message, intent, policy = fixture_message()
    wrong_ix = InstructionExpectation(
        program_id=intent.instructions[0].program_id,
        accounts=intent.instructions[0].accounts,
        data=b"attacker-changed-data",
    )
    wrong = TransactionIntent(intent.fee_payer, intent.required_signers, intent.recent_blockhash, (wrong_ix,))
    with pytest.raises(TransactionValidationError, match="data"):
        validate_transaction_message(to_bytes_versioned(message), intent=wrong, policy=policy)


def test_rejects_program_not_on_allowlist():
    message, intent, _ = fixture_message()
    policy = TransactionValidationPolicy(allowed_program_ids=())
    with pytest.raises(TransactionValidationError, match="non-allowlisted"):
        validate_transaction_message(to_bytes_versioned(message), intent=intent, policy=policy)


def test_rejects_expected_program_mismatch():
    message, intent, policy = fixture_message()
    wrong_ix = InstructionExpectation(
        program_id=str(Pubkey.new_unique()),
        accounts=intent.instructions[0].accounts,
        data=intent.instructions[0].data,
    )
    wrong_policy = TransactionValidationPolicy(allowed_program_ids=(wrong_ix.program_id,))
    wrong = TransactionIntent(intent.fee_payer, intent.required_signers, intent.recent_blockhash, (wrong_ix,))
    with pytest.raises(TransactionValidationError, match="program ID"):
        validate_transaction_message(to_bytes_versioned(message), intent=wrong, policy=wrong_policy)


def test_rejects_account_order_or_identity_change():
    message, intent, policy = fixture_message()
    original = intent.instructions[0]
    wrong_ix = InstructionExpectation(
        original.program_id,
        (original.accounts[1], original.accounts[0], original.accounts[2]),
        original.data,
    )
    wrong = TransactionIntent(intent.fee_payer, intent.required_signers, intent.recent_blockhash, (wrong_ix,))
    with pytest.raises(TransactionValidationError, match="account key"):
        validate_transaction_message(to_bytes_versioned(message), intent=wrong, policy=policy)


def test_rejects_privilege_escalation():
    message, intent, policy = fixture_message()
    original = intent.instructions[0]
    changed = list(original.accounts)
    changed[2] = AccountExpectation(changed[2].public_key, False, True)
    wrong = TransactionIntent(
        intent.fee_payer, intent.required_signers, intent.recent_blockhash,
        (InstructionExpectation(original.program_id, tuple(changed), original.data),),
    )
    with pytest.raises(TransactionValidationError, match="writable privilege"):
        validate_transaction_message(to_bytes_versioned(message), intent=wrong, policy=policy)


def test_rejects_extra_required_signer():
    message, intent, policy = fixture_message()
    wrong = TransactionIntent(
        intent.fee_payer,
        intent.required_signers + (str(Pubkey.new_unique()),),
        intent.recent_blockhash,
        intent.instructions,
    )
    with pytest.raises(TransactionValidationError, match="signer set"):
        validate_transaction_message(to_bytes_versioned(message), intent=wrong, policy=policy)


def test_rejects_instruction_count_change():
    message, intent, policy = fixture_message()
    wrong = TransactionIntent(intent.fee_payer, intent.required_signers, intent.recent_blockhash, ())
    with pytest.raises(TransactionValidationError, match="instructions"):
        validate_transaction_message(to_bytes_versioned(message), intent=wrong, policy=policy)


def test_rejects_message_over_size_limit():
    message, intent, _ = fixture_message()
    policy = TransactionValidationPolicy(
        allowed_program_ids=(intent.instructions[0].program_id,),
        max_message_bytes=1,
    )
    with pytest.raises(TransactionValidationError, match="size"):
        validate_transaction_message(to_bytes_versioned(message), intent=intent, policy=policy)


def test_rejects_v0_message_with_address_lookup_table():
    payer = Pubkey.new_unique()
    looked_up = Pubkey.new_unique()
    program = Pubkey.new_unique()
    blockhash = Hash.new_unique()
    lookup = AddressLookupTableAccount(key=Pubkey.new_unique(), addresses=[looked_up])
    ix = Instruction(program, b"data", [AccountMeta(looked_up, False, True)])
    message = MessageV0.try_compile(payer, [ix], [lookup], blockhash)
    intent = TransactionIntent(
        fee_payer=str(payer),
        required_signers=(str(payer),),
        recent_blockhash=str(blockhash),
        instructions=(
            InstructionExpectation(
                str(program),
                (AccountExpectation(str(looked_up), False, True),),
                b"data",
            ),
        ),
    )
    policy = TransactionValidationPolicy(allowed_program_ids=(str(program),))
    with pytest.raises(TransactionValidationError, match="address lookup"):
        validate_transaction_message(to_bytes_versioned(message), intent=intent, policy=policy)


def test_policy_rejects_attempt_to_enable_lookup_tables():
    with pytest.raises(TransactionValidationError, match="disabled"):
        TransactionValidationPolicy(allowed_program_ids=(), allow_address_lookup_tables=True)


def test_intent_requires_fee_payer_to_be_signer():
    _, intent, _ = fixture_message()
    with pytest.raises(TransactionValidationError, match="fee payer"):
        TransactionIntent(
            fee_payer=intent.fee_payer,
            required_signers=(),
            recent_blockhash=intent.recent_blockhash,
            instructions=intent.instructions,
        )
