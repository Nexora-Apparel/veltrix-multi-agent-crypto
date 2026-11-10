"""Tests for the pinned Jupiter v6 Borsh route instruction decoder."""
import dataclasses
import hashlib
import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0, from_bytes_versioned, to_bytes_versioned
from solders.pubkey import Pubkey

from veltrix.jupiter_instruction_decoder import (
    JUPITER_V6_PROGRAM_ID,
    ROUTE_DISCRIMINATOR,
    SHARED_ACCOUNTS_ROUTE_DISCRIMINATOR,
    JupiterInstructionDecodeError,
    JupiterSwapInstructionPolicy,
    decode_jupiter_swap_instruction,
)
from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_swap_effects import (
    CLASSIC_TOKEN_PROGRAM_ID,
    derive_associated_token_address,
)
from veltrix.transaction_validator import (
    AccountExpectation,
    InstructionExpectation,
    TransactionIntent,
    TransactionValidationPolicy,
    validate_transaction_message,
)


def encode_route_data(
    *,
    swap_tag=26,
    percent=100,
    input_index=0,
    output_index=1,
    in_amount=1_000,
    out_amount=2_000,
    slippage_bps=100,
    platform_fee_bps=0,
    shared=False,
    trailing=b"",
):
    prefix = SHARED_ACCOUNTS_ROUTE_DISCRIMINATOR + bytes([1]) if shared else ROUTE_DISCRIMINATOR
    return (
        prefix
        + (1).to_bytes(4, "little")
        + bytes([swap_tag, percent, input_index, output_index])
        + in_amount.to_bytes(8, "little")
        + out_amount.to_bytes(8, "little")
        + slippage_bps.to_bytes(2, "little")
        + bytes([platform_fee_bps])
        + trailing
    )


def build_case(*, shared=False, data=None, source_override=None, output_override=None, authority_override=None, source_mint_override=None, source_writable=True):
    taker = Pubkey.new_unique()
    input_mint = Pubkey.new_unique()
    output_mint = Pubkey.new_unique()
    input_ata = Pubkey.from_string(derive_associated_token_address(str(taker), str(input_mint)))
    output_ata = Pubkey.from_string(derive_associated_token_address(str(taker), str(output_mint)))
    token_program = Pubkey.from_string(CLASSIC_TOKEN_PROGRAM_ID)
    jupiter_program = Pubkey.from_string(JUPITER_V6_PROGRAM_ID)
    system_program = Pubkey.default()
    blockhash = Hash.new_unique()
    authority = authority_override or taker
    source_account = source_override or input_ata
    destination_account = output_override or output_ata

    if shared:
        program_authority = Pubkey.new_unique()
        program_source = Pubkey.new_unique()
        program_destination = Pubkey.new_unique()
        metas = [
            AccountMeta(token_program, False, False),
            AccountMeta(program_authority, False, False),
            AccountMeta(authority, True, False),
            AccountMeta(source_account, False, source_writable),
            AccountMeta(program_source, False, True),
            AccountMeta(program_destination, False, True),
            AccountMeta(destination_account, False, True),
            AccountMeta(source_mint_override or input_mint, False, False),
            AccountMeta(output_mint, False, False),
            AccountMeta(system_program, False, False),
            AccountMeta(system_program, False, False),
        ]
        instruction_name = "sharedAccountsRoute"
        raw_data = data or encode_route_data(shared=True)
    else:
        metas = [
            AccountMeta(token_program, False, False),
            AccountMeta(authority, True, False),
            AccountMeta(source_account, False, source_writable),
            AccountMeta(destination_account, False, True),
            AccountMeta(system_program, False, False),
            AccountMeta(output_mint, False, False),
            AccountMeta(system_program, False, False),
        ]
        instruction_name = "route"
        raw_data = data or encode_route_data()
    instruction = Instruction(jupiter_program, raw_data, metas)
    message = MessageV0.try_compile(taker, [instruction], [], blockhash)
    message_bytes = to_bytes_versioned(message)
    static_keys = tuple(str(key) for key in message.account_keys)
    compiled = message.instructions[0]

    def writable(index):
        header = message.header
        required = int(header.num_required_signatures)
        readonly_signed = int(header.num_readonly_signed_accounts)
        readonly_unsigned = int(header.num_readonly_unsigned_accounts)
        if index < required:
            return index < required - readonly_signed
        return index < len(static_keys) - readonly_unsigned

    expected_accounts = tuple(
        AccountExpectation(
            static_keys[int(index)],
            message.is_signer(int(index)),
            writable(int(index)),
        )
        for index in bytes(compiled.accounts)
    )
    intent = TransactionIntent(
        fee_payer=str(taker),
        required_signers=tuple(
            key for index, key in enumerate(static_keys) if message.is_signer(index)
        ),
        recent_blockhash=str(blockhash),
        instructions=(
            InstructionExpectation(
                str(jupiter_program),
                expected_accounts,
                bytes(compiled.data),
            ),
        ),
    )
    transaction_policy = TransactionValidationPolicy(
        allowed_program_ids=(str(jupiter_program),),
    )
    validation = validate_transaction_message(
        message_bytes,
        intent=intent,
        policy=transaction_policy,
    )
    order = JupiterOrderTransaction(
        provider_id="jupiter-swap-v2",
        request_id="order-1",
        quote_id="quote-1",
        observed_at_epoch_ms=1_000_000,
        taker=str(taker),
        fee_payer=str(taker),
        input_mint=str(input_mint),
        output_mint=str(output_mint),
        input_amount_raw=1_000,
        output_amount_raw=2_000,
        minimum_output_amount_raw=1_950,
        price_impact_pct=0.2,
        fee_bps=0.0,
        router="metis",
        transaction_version=0,
        last_valid_block_height=950,
        recent_blockhash=str(blockhash),
        message_bytes=message_bytes,
        serialized_transaction_bytes=b"unsigned-v0-transaction",
        route_plan=(
            JupiterRouteStep(
                amm_key=str(Pubkey.new_unique()),
                label="Raydium CLMM",
                input_mint=str(input_mint),
                output_mint=str(output_mint),
                input_amount_raw=1_000,
                output_amount_raw=2_000,
                percent=100.0,
                bps=10_000,
            ),
        ),
        signature_fee_lamports=5_000,
        prioritization_fee_lamports=1_000,
        rent_fee_lamports=0,
        slippage_bps=100,
    )
    return order, validation, instruction_name, message_bytes


def decode(values=None, policy=None):
    order, validation, _, _ = values or build_case()
    return decode_jupiter_swap_instruction(
        order,
        validation=validation,
        policy=policy or JupiterSwapInstructionPolicy(),
    )


def test_decodes_jupiter_route_exact_in_instruction():
    order, validation, instruction_name, _ = build_case()
    result = decode((order, validation, instruction_name, None))
    assert result.instruction_name == "route"
    assert result.program_id == JUPITER_V6_PROGRAM_ID
    assert result.transfer_authority == order.taker
    assert result.input_amount_raw == 1_000
    assert result.quoted_output_amount_raw == 2_000
    assert result.slippage_bps == 100
    assert result.route_plan[0].swap_variant == "RaydiumClmm"
    assert result.route_plan[0].dex_label == "Raydium CLMM"


def test_decodes_shared_accounts_route_with_source_and_destination_mints():
    order, validation, _, _ = build_case(shared=True)
    result = decode((order, validation, "sharedAccountsRoute", None))
    assert result.instruction_name == "sharedAccountsRoute"
    assert result.shared_route_id == 1
    assert result.input_mint == order.input_mint
    assert result.output_mint == order.output_mint


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (encode_route_data(swap_tag=255), "enum tag is unknown"),
        (b"bad-data", "discriminator"),
        (encode_route_data(in_amount=999), "route amounts differ"),
        (encode_route_data(out_amount=1_999), "route amounts differ"),
        (encode_route_data(slippage_bps=501), "slippage exceeds"),
        (encode_route_data(platform_fee_bps=1), "platform fee"),
        (encode_route_data(trailing=b"unexpected"), "trailing or unknown data"),
        (encode_route_data(percent=99), "split percentages do not total"),
        (encode_route_data(input_index=1, output_index=1), "topologically sorted"),
    ],
)
def test_rejects_malformed_or_policy_violating_route_data(data, message):
    order, validation, _, _ = build_case(data=data)
    with pytest.raises(JupiterInstructionDecodeError, match=message):
        decode((order, validation, "route", None))


def test_rejects_api_route_label_mismatch():
    order, validation, _, _ = build_case()
    changed = dataclasses.replace(
        order,
        route_plan=(dataclasses.replace(order.route_plan[0], label="Meteora DLMM"),),
    )
    with pytest.raises(JupiterInstructionDecodeError, match="DEX variant differs"):
        decode((changed, validation, "route", None))


def test_rejects_wrong_user_source_associated_token_account():
    order, validation, _, _ = build_case(source_override=Pubkey.new_unique())
    with pytest.raises(JupiterInstructionDecodeError, match="user source token account"):
        decode((order, validation, "route", None))


def test_rejects_wrong_transfer_authority():
    order, validation, _, _ = build_case(authority_override=Pubkey.new_unique())
    with pytest.raises(JupiterInstructionDecodeError, match="transfer authority account key"):
        decode((order, validation, "route", None))


def test_rejects_source_account_privilege_escalation_or_downgrade():
    order, validation, _, _ = build_case(source_writable=False)
    with pytest.raises(JupiterInstructionDecodeError, match="user source token account signer/writable"):
        decode((order, validation, "route", None))


def test_rejects_shared_route_with_wrong_source_mint():
    order, validation, _, _ = build_case(shared=True, source_mint_override=Pubkey.new_unique())
    with pytest.raises(JupiterInstructionDecodeError, match="source mint account key"):
        decode((order, validation, "sharedAccountsRoute", None))


def test_rejects_transaction_with_multiple_jupiter_instructions():
    order, validation, _, _ = build_case()
    original_message = from_bytes_versioned(order.message_bytes)
    compiled = original_message.instructions[0]
    static_keys = tuple(original_message.account_keys)
    header = original_message.header
    required = int(header.num_required_signatures)
    readonly_signed = int(header.num_readonly_signed_accounts)
    readonly_unsigned = int(header.num_readonly_unsigned_accounts)
    def writable(index):
        if index < required:
            return index < required - readonly_signed
        return index < len(static_keys) - readonly_unsigned
    metas = [
        AccountMeta(
            Pubkey.from_string(static_keys[int(index)]),
            original_message.is_signer(int(index)),
            writable(int(index)),
        )
        for index in bytes(compiled.accounts)
    ]
    duplicate_message = MessageV0.try_compile(
        Pubkey.from_string(order.taker),
        [
            Instruction(Pubkey.from_string(JUPITER_V6_PROGRAM_ID), bytes(compiled.data), metas),
            Instruction(Pubkey.from_string(JUPITER_V6_PROGRAM_ID), bytes(compiled.data), metas),
        ],
        [],
        Hash.from_string(order.recent_blockhash),
    )
    changed_bytes = to_bytes_versioned(duplicate_message)
    changed_order = dataclasses.replace(order, message_bytes=changed_bytes)
    changed_keys = tuple(str(key) for key in duplicate_message.account_keys)
    changed_header = duplicate_message.header
    changed_required = int(changed_header.num_required_signatures)
    changed_readonly_signed = int(changed_header.num_readonly_signed_accounts)
    changed_readonly_unsigned = int(changed_header.num_readonly_unsigned_accounts)
    def changed_writable(index):
        if index < changed_required:
            return index < changed_required - changed_readonly_signed
        return index < len(changed_keys) - changed_readonly_unsigned
    changed_validation = dataclasses.replace(
        validation,
        message_sha256=hashlib.sha256(changed_bytes).hexdigest(),
        message_size=len(changed_bytes),
        program_ids=(JUPITER_V6_PROGRAM_ID, JUPITER_V6_PROGRAM_ID),
        instruction_count=2,
        account_keys=changed_keys,
        account_signers=tuple(duplicate_message.is_signer(i) for i in range(len(changed_keys))),
        account_writable=tuple(changed_writable(i) for i in range(len(changed_keys))),
    )
    with pytest.raises(JupiterInstructionDecodeError, match="exactly one top-level"):
        decode((changed_order, changed_validation, "route", changed_bytes))


def test_rejects_slippage_mismatch_with_api_order():
    order, validation, _, _ = build_case(data=encode_route_data(slippage_bps=200))
    with pytest.raises(JupiterInstructionDecodeError, match="slippage differs"):
        decode((order, validation, "route", None))


def test_policy_rejects_unknown_variant_or_missing_label_mapping():
    with pytest.raises(JupiterInstructionDecodeError, match="unknown or duplicate"):
        JupiterSwapInstructionPolicy(allowed_swap_variants=("NotARealSwap",))
    with pytest.raises(JupiterInstructionDecodeError, match="explicit DEX label mapping"):
        JupiterSwapInstructionPolicy(
            allowed_swap_variants=("RaydiumClmm",),
            variant_to_label=(),
        )
