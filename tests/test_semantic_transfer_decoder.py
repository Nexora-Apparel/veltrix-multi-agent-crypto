"""Adversarial tests for the narrow Solana transfer semantic decoder."""
import dataclasses
import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message, to_bytes_versioned
from solders.pubkey import Pubkey

from veltrix.semantic_transfer_decoder import (
    COMPUTE_BUDGET_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    ExpectedNativeTransfer,
    ExpectedTokenTransfer,
    SemanticDecodeError,
    SemanticDecodePolicy,
    decode_transaction_semantics,
)
from veltrix.transaction_validator import (
    AccountExpectation,
    InstructionExpectation,
    TransactionIntent,
    TransactionValidationPolicy,
    validate_transaction_message,
)


def make_case(instructions):
    payer = Pubkey.new_unique()
    blockhash = Hash.new_unique()
    message = Message.new_with_blockhash(instructions(payer), payer, blockhash)
    keys = tuple(message.account_keys)
    compiled_instructions = tuple(message.instructions)

    def writable(index):
        header = message.header
        required = int(header.num_required_signatures)
        if index < required:
            return index < required - int(header.num_readonly_signed_accounts)
        return index < len(keys) - int(header.num_readonly_unsigned_accounts)

    expected_instructions = []
    allowed_programs = []
    for compiled in compiled_instructions:
        program_id = str(keys[int(compiled.program_id_index)])
        allowed_programs.append(program_id)
        accounts = tuple(
            AccountExpectation(
                str(keys[int(index)]),
                message.is_signer(int(index)),
                writable(int(index)),
            )
            for index in bytes(compiled.accounts)
        )
        expected_instructions.append(
            InstructionExpectation(program_id, accounts, bytes(compiled.data))
        )
    signers = tuple(str(key) for i, key in enumerate(keys) if message.is_signer(i))
    intent = TransactionIntent(
        fee_payer=str(payer),
        required_signers=signers,
        recent_blockhash=str(blockhash),
        instructions=tuple(expected_instructions),
    )
    tx_policy = TransactionValidationPolicy(allowed_program_ids=tuple(dict.fromkeys(allowed_programs)))
    raw = to_bytes_versioned(message)
    validation = validate_transaction_message(raw, intent=intent, policy=tx_policy)
    semantic_policy = SemanticDecodePolicy(
        max_native_lamports_per_transaction=1_000_000_000,
        max_token_amount_raw_per_mint=10_000_000,
        max_compute_units=1_000_000,
        max_compute_unit_price_micro_lamports=10_000,
    )
    return payer, raw, intent, tx_policy, validation, semantic_policy


def transfer_checked_instructions(payer, *, amount=250, decimals=6, mint=None, token_program=TOKEN_PROGRAM_ID):
    source = Pubkey.new_unique()
    destination = Pubkey.new_unique()
    mint = mint or Pubkey.new_unique()
    program = Pubkey.from_string(token_program)
    data = bytes([12]) + amount.to_bytes(8, "little") + bytes([decimals])
    instruction = Instruction(
        program,
        data,
        [
            AccountMeta(source, False, True),
            AccountMeta(mint, False, False),
            AccountMeta(destination, False, True),
            AccountMeta(payer, True, True),
        ],
    )
    return instruction, source, mint, destination


def decode(case, token_expected=(), native_expected=()):
    _, raw, intent, tx_policy, validation, semantic_policy = case
    return decode_transaction_semantics(
        raw,
        intent=intent,
        validation=validation,
        transaction_policy=tx_policy,
        policy=semantic_policy,
        expected_token_transfers=tuple(token_expected),
        expected_native_transfers=tuple(native_expected),
    )


def test_decodes_exact_spl_transfer_checked_even_when_authority_is_fee_payer():
    def build(payer):
        instruction, _, _, _ = transfer_checked_instructions(payer)
        return [instruction]

    payer, raw, intent, tx_policy, validation, semantic_policy = make_case(build)
    program = Pubkey.from_string(TOKEN_PROGRAM_ID)
    instruction, source, mint, destination = transfer_checked_instructions(payer)
    # Rebuild with the exact instruction set used in the message for effect assertions.
    expected = ExpectedTokenTransfer(
        source=str(source),
        mint=str(mint),
        destination=str(destination),
        authority=str(payer),
        amount_raw=250,
        decimals=6,
    )
    result = decode_transaction_semantics(
        raw,
        intent=intent,
        validation=validation,
        transaction_policy=tx_policy,
        policy=semantic_policy,
        expected_token_transfers=(expected,),
        expected_native_transfers=(),
    )
    assert result.token_transfers[0].amount_raw == 250
    assert result.token_transfers[0].mint == str(mint)
    assert result.total_native_lamports == 0


def test_decodes_bounded_system_transfer():
    amount = 50_000

    def build(payer):
        destination = Pubkey.new_unique()
        data = (2).to_bytes(4, "little") + amount.to_bytes(8, "little")
        return [Instruction(
            Pubkey.from_string(SYSTEM_PROGRAM_ID),
            data,
            [AccountMeta(payer, True, True), AccountMeta(destination, False, True)],
        )]

    payer, raw, intent, tx_policy, validation, semantic_policy = make_case(build)
    message = Message.new_with_blockhash(build(payer), payer, Hash.new_unique())
    destination = str(message.account_keys[1])
    expected = ExpectedNativeTransfer(str(payer), destination, amount)
    # Use the message's exact blockhash and derived intent from the helper.
    result = decode_transaction_semantics(
        raw,
        intent=intent,
        validation=validation,
        transaction_policy=tx_policy,
        policy=semantic_policy,
        expected_token_transfers=(),
        expected_native_transfers=(ExpectedNativeTransfer(str(payer), destination, amount),),
    )
    assert result.total_native_lamports == amount


def test_decodes_bounded_compute_budget_limit_and_price():
    def build(payer):
        compute = Pubkey.from_string(COMPUTE_BUDGET_PROGRAM_ID)
        return [
            Instruction(compute, bytes([2]) + (300_000).to_bytes(4, "little"), []),
            Instruction(compute, bytes([3]) + (5_000).to_bytes(8, "little"), []),
        ]

    case = make_case(build)
    result = decode(case)
    assert result.compute_unit_limit == 300_000
    assert result.compute_unit_price_micro_lamports == 5_000


def test_rejects_unknown_program_even_if_structurally_allowlisted():
    def build(payer):
        return [Instruction(Pubkey.new_unique(), b"unknown", [])]

    case = make_case(build)
    with pytest.raises(SemanticDecodeError, match="unsupported program"):
        decode(case)


def test_rejects_token_2022_until_extensions_are_reviewed():
    def build(payer):
        instruction, _, _, _ = transfer_checked_instructions(
            payer, token_program=TOKEN_2022_PROGRAM_ID
        )
        return [instruction]

    case = make_case(build)
    with pytest.raises(SemanticDecodeError, match="Token-2022"):
        decode(case)


def test_rejects_unchecked_spl_transfer_without_mint_binding():
    def build(payer):
        source, destination = Pubkey.new_unique(), Pubkey.new_unique()
        data = bytes([3]) + (250).to_bytes(8, "little")
        return [Instruction(
            Pubkey.from_string(TOKEN_PROGRAM_ID),
            data,
            [
                AccountMeta(source, False, True),
                AccountMeta(destination, False, True),
                AccountMeta(payer, True, True),
            ],
        )]

    case = make_case(build)
    with pytest.raises(SemanticDecodeError, match="TransferChecked"):
        decode(case)


def test_rejects_transfer_amount_different_from_approved_effect():
    def build(payer):
        instruction, _, _, _ = transfer_checked_instructions(payer, amount=250)
        return [instruction]

    case = make_case(build)
    wrong = ExpectedTokenTransfer("source", "mint", "destination", "authority", 251, 6)
    with pytest.raises(SemanticDecodeError, match="exactly match"):
        decode(case, token_expected=(wrong,))


def test_rejects_token_amount_over_policy():
    def build(payer):
        instruction, _, _, _ = transfer_checked_instructions(payer, amount=10_000_001)
        return [instruction]

    case = make_case(build)
    payer, raw, intent, tx_policy, validation, policy = case
    policy = SemanticDecodePolicy(1_000_000, 10_000_000, 1_000_000, 10_000)
    _, source, mint, destination = transfer_checked_instructions(payer, amount=10_000_001)
    expected = ExpectedTokenTransfer(str(source), str(mint), str(destination), str(payer), 10_000_001, 6)
    with pytest.raises(SemanticDecodeError, match="aggregate token"):
        decode_transaction_semantics(
            raw, intent=intent, validation=validation, transaction_policy=tx_policy,
            policy=policy, expected_token_transfers=(expected,), expected_native_transfers=(),
        )


def test_rejects_compute_budget_price_over_policy():
    def build(payer):
        compute = Pubkey.from_string(COMPUTE_BUDGET_PROGRAM_ID)
        return [Instruction(compute, bytes([3]) + (10_001).to_bytes(8, "little"), [])]

    case = make_case(build)
    with pytest.raises(SemanticDecodeError, match="price exceeds"):
        decode(case)


def test_rejects_duplicate_compute_budget_limit():
    def build(payer):
        compute = Pubkey.from_string(COMPUTE_BUDGET_PROGRAM_ID)
        data = bytes([2]) + (200_000).to_bytes(4, "little")
        return [Instruction(compute, data, []), Instruction(compute, data, [])]

    case = make_case(build)
    with pytest.raises(SemanticDecodeError, match="duplicate"):
        decode(case)


def test_rejects_system_create_account_and_other_system_instructions():
    def build(payer):
        destination = Pubkey.new_unique()
        return [Instruction(
            Pubkey.from_string(SYSTEM_PROGRAM_ID),
            bytes([0, 0, 0, 0]) + (100).to_bytes(8, "little"),
            [AccountMeta(payer, True, True), AccountMeta(destination, False, True)],
        )]

    case = make_case(build)
    with pytest.raises(SemanticDecodeError, match="only the exact System Program Transfer"):
        decode(case)


def test_rejects_mismatched_structural_validation_result():
    case = make_case(lambda payer: [
        Instruction(Pubkey.from_string(COMPUTE_BUDGET_PROGRAM_ID), bytes([2]) + (200_000).to_bytes(4, "little"), [])
    ])
    payer, raw, intent, tx_policy, validation, policy = case
    wrong = dataclasses.replace(validation, message_sha256="b" * 64)
    with pytest.raises(SemanticDecodeError, match="does not match"):
        decode_transaction_semantics(
            raw, intent=intent, validation=wrong, transaction_policy=tx_policy,
            policy=policy, expected_token_transfers=(), expected_native_transfers=(),
        )
