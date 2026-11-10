"""Adversarial tests for the exact-intent Solana transaction validator."""
import dataclasses
import pytest

from solders.hash import Hash
from solders.instruction import AccountMeta, CompiledInstruction, Instruction
from solders.message import Message, MessageV0, to_bytes_versioned
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.pubkey import Pubkey

from veltrix.transaction_validator import (
    AccountExpectation,
    InstructionExpectation,
    ADDRESS_LOOKUP_TABLE_PROGRAM_ID,
    ResolvedLookupTable,
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
    wrong_payer = str(Pubkey.new_unique())
    wrong = TransactionIntent(
        fee_payer=wrong_payer,
        required_signers=(wrong_payer,),
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
    wrong_policy = TransactionValidationPolicy(allowed_program_ids=(intent.instructions[0].program_id, wrong_ix.program_id))
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
    original = intent.instructions[0]
    program = Pubkey.from_string(original.program_id)
    metas = [
        AccountMeta(Pubkey.from_string(a.public_key), a.is_signer, a.is_writable)
        for a in original.accounts
    ]
    blockhash = Hash.new_unique()
    duplicate_instruction = Instruction(program, original.data, metas)
    two_instruction_message = Message.new_with_blockhash(
        [duplicate_instruction, duplicate_instruction],
        Pubkey.from_string(intent.fee_payer),
        blockhash,
    )
    updated_intent = TransactionIntent(
        intent.fee_payer,
        intent.required_signers,
        str(blockhash),
        (original,),
    )
    with pytest.raises(TransactionValidationError, match="instruction count"):
        validate_transaction_message(
            to_bytes_versioned(two_instruction_message),
            intent=updated_intent,
            policy=policy,
        )


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


def test_policy_requires_explicit_boolean_for_lookup_table_opt_in():
    policy = TransactionValidationPolicy(
        allowed_program_ids=("program-id",),
        allow_address_lookup_tables=True,
    )
    assert policy.allow_address_lookup_tables is True
    with pytest.raises(TransactionValidationError, match="must be a boolean"):
        TransactionValidationPolicy(
            allowed_program_ids=("program-id",),
            allow_address_lookup_tables="true",
        )


def test_intent_requires_fee_payer_to_be_signer():
    _, intent, _ = fixture_message()
    with pytest.raises(TransactionValidationError, match="fee payer"):
        TransactionIntent(
            fee_payer=intent.fee_payer,
            required_signers=(str(Pubkey.new_unique()),),
            recent_blockhash=intent.recent_blockhash,
            instructions=intent.instructions,
        )


def test_rejects_impossible_message_header_privilege_counts():
    payer = Pubkey.new_unique()
    program = Pubkey.new_unique()
    blockhash = Hash.new_unique()
    malformed = Message.new_with_compiled_instructions(
        1, 2, 0, [payer, program], blockhash,
        [CompiledInstruction(1, b"", b"")],
    )
    intent = TransactionIntent(
        str(payer), (str(payer),), str(blockhash),
        (InstructionExpectation(str(program), (), b""),),
    )
    policy = TransactionValidationPolicy(allowed_program_ids=(str(program),))
    with pytest.raises(TransactionValidationError, match="impossible signer"):
        validate_transaction_message(to_bytes_versioned(malformed), intent=intent, policy=policy)


def test_rejects_program_account_marked_writable_by_message_header():
    payer = Pubkey.new_unique()
    program = Pubkey.new_unique()
    blockhash = Hash.new_unique()
    unsafe = Message.new_with_compiled_instructions(
        1, 0, 0, [payer, program], blockhash,
        [CompiledInstruction(1, b"", b"")],
    )
    intent = TransactionIntent(
        str(payer), (str(payer),), str(blockhash),
        (InstructionExpectation(str(program), (), b""),),
    )
    policy = TransactionValidationPolicy(allowed_program_ids=(str(program),))
    with pytest.raises(TransactionValidationError, match="program account"):
        validate_transaction_message(to_bytes_versioned(unsafe), intent=intent, policy=policy)


def test_accepts_fee_payer_as_implicit_account_without_instruction_accounts():
    payer = Pubkey.new_unique()
    program = Pubkey.new_unique()
    blockhash = Hash.new_unique()
    instruction = Instruction(program, bytes([2]) + (200_000).to_bytes(4, "little"), [])
    message = Message.new_with_blockhash([instruction], payer, blockhash)
    intent = TransactionIntent(
        fee_payer=str(payer),
        required_signers=(str(payer),),
        recent_blockhash=str(blockhash),
        instructions=(
            InstructionExpectation(str(program), (), bytes([2]) + (200_000).to_bytes(4, "little")),
        ),
    )
    policy = TransactionValidationPolicy(allowed_program_ids=(str(program),))
    result = validate_transaction_message(to_bytes_versioned(message), intent=intent, policy=policy)
    assert result.fee_payer == str(payer)


def _v0_lookup_fixture():
    payer = Pubkey.new_unique()
    source = Pubkey.new_unique()
    destination = Pubkey.new_unique()
    program = Pubkey.new_unique()
    table_key = Pubkey.new_unique()
    blockhash = Hash.new_unique()
    table_account = AddressLookupTableAccount(key=table_key, addresses=[source, destination])
    instruction = Instruction(
        program,
        b"lookup-table-test",
        [
            AccountMeta(source, False, True),
            AccountMeta(destination, False, False),
        ],
    )
    message = MessageV0.try_compile(payer, [instruction], [table_account], blockhash)
    raw = to_bytes_versioned(message)
    static_keys = tuple(str(key) for key in message.account_keys)
    loaded_writable = []
    loaded_readonly = []
    for lookup in message.address_table_lookups:
        loaded_writable.extend(str(table_account.addresses[i]) for i in bytes(lookup.writable_indexes))
        loaded_readonly.extend(str(table_account.addresses[i]) for i in bytes(lookup.readonly_indexes))
    account_keys = static_keys + tuple(loaded_writable) + tuple(loaded_readonly)
    static_count = len(static_keys)

    def writable(index):
        if index < static_count:
            header = message.header
            required = int(header.num_required_signatures)
            readonly_signed = int(header.num_readonly_signed_accounts)
            readonly_unsigned = int(header.num_readonly_unsigned_accounts)
            if index < required:
                return index < required - readonly_signed
            return index < static_count - readonly_unsigned
        return index < static_count + len(loaded_writable)

    compiled = message.instructions[0]
    program_id = str(account_keys[int(compiled.program_id_index)])
    expected_accounts = tuple(
        AccountExpectation(
            account_keys[int(i)],
            message.is_signer(int(i)) if int(i) < static_count else False,
            writable(int(i)),
        )
        for i in bytes(compiled.accounts)
    )
    intent = TransactionIntent(
        fee_payer=str(payer),
        required_signers=tuple(
            key for i, key in enumerate(static_keys) if message.is_signer(i)
        ),
        recent_blockhash=str(blockhash),
        instructions=(
            InstructionExpectation(program_id, expected_accounts, bytes(compiled.data)),
        ),
    )
    policy = TransactionValidationPolicy(
        allowed_program_ids=(program_id,),
        allow_address_lookup_tables=True,
    )
    resolved = ResolvedLookupTable(
        table_address=str(table_key),
        owner_program_id=ADDRESS_LOOKUP_TABLE_PROGRAM_ID,
        addresses=tuple(str(key) for key in table_account.addresses),
        deactivation_slot=(1 << 64) - 1,
        last_extended_slot=50,
        context_slot=100,
    )
    return raw, intent, policy, resolved, message


def test_accepts_v0_message_with_exact_active_resolved_lookup_table():
    raw, intent, policy, resolved, _ = _v0_lookup_fixture()
    result = validate_transaction_message(
        raw,
        intent=intent,
        policy=policy,
        resolved_lookup_tables=(resolved,),
        current_slot=100,
        minimum_context_slot=90,
    )
    assert result.fee_payer == intent.fee_payer
    assert result.instruction_count == 1


def test_rejects_lookup_table_when_policy_disallows_it():
    raw, intent, _, resolved, _ = _v0_lookup_fixture()
    policy = TransactionValidationPolicy(allowed_program_ids=(intent.instructions[0].program_id,))
    with pytest.raises(TransactionValidationError, match="disabled by transaction policy"):
        validate_transaction_message(
            raw, intent=intent, policy=policy,
            resolved_lookup_tables=(resolved,), current_slot=100, minimum_context_slot=90,
        )


def test_rejects_v0_lookup_without_resolution_evidence():
    raw, intent, policy, _, _ = _v0_lookup_fixture()
    with pytest.raises(TransactionValidationError, match="evidence is required"):
        validate_transaction_message(raw, intent=intent, policy=policy, current_slot=100)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("owner_program_id", str(Pubkey.new_unique()), "not owned"),
        ("deactivation_slot", 99, "deactivated"),
        ("last_extended_slot", 100, "extended in the current slot"),
        ("context_slot", 89, "context slot"),
    ],
)
def test_rejects_untrusted_or_inactive_lookup_table_metadata(field, value, message):
    raw, intent, policy, resolved, _ = _v0_lookup_fixture()
    changed = dataclasses.replace(resolved, **{field: value})
    with pytest.raises(TransactionValidationError, match=message):
        validate_transaction_message(
            raw, intent=intent, policy=policy,
            resolved_lookup_tables=(changed,), current_slot=100, minimum_context_slot=90,
        )


def test_rejects_lookup_index_outside_resolved_address_table():
    raw, intent, policy, resolved, _ = _v0_lookup_fixture()
    changed = dataclasses.replace(resolved, addresses=resolved.addresses[:1])
    with pytest.raises(TransactionValidationError, match="outside the resolved table"):
        validate_transaction_message(
            raw, intent=intent, policy=policy,
            resolved_lookup_tables=(changed,), current_slot=100, minimum_context_slot=90,
        )


def test_rejects_duplicate_loaded_addresses():
    raw, intent, policy, resolved, _ = _v0_lookup_fixture()
    changed = dataclasses.replace(resolved, addresses=(resolved.addresses[0], resolved.addresses[0]))
    with pytest.raises(TransactionValidationError, match="duplicate account keys"):
        validate_transaction_message(
            raw, intent=intent, policy=policy,
            resolved_lookup_tables=(changed,), current_slot=100, minimum_context_slot=90,
        )


def test_rejects_lookup_resolution_set_with_extra_table():
    raw, intent, policy, resolved, _ = _v0_lookup_fixture()
    extra = dataclasses.replace(resolved, table_address=str(Pubkey.new_unique()))
    with pytest.raises(TransactionValidationError, match="set does not exactly match"):
        validate_transaction_message(
            raw, intent=intent, policy=policy,
            resolved_lookup_tables=(resolved, extra), current_slot=100, minimum_context_slot=90,
        )
