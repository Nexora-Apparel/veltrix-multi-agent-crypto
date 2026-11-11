"""Adversarial tests for Jupiter V6 route payload and fixed-account inspection."""
import dataclasses
import hashlib
import struct

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_swap_effects import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    CLASSIC_TOKEN_PROGRAM_ID,
    derive_associated_token_address,
)
from veltrix.jupiter_v6_route import (
    JUPITER_V6_PROGRAM_ID,
    ROUTE_DISCRIMINATOR,
    SHARED_ACCOUNTS_ROUTE_DISCRIMINATOR,
    JupiterV6RouteError,
    JupiterV6RoutePolicy,
    inspect_jupiter_v6_route,
)
from veltrix.transaction_validator import TransactionValidationResult


def _pda(program, seeds):
    address, _ = Pubkey.find_program_address(list(seeds), program)
    return address


def fixture(
    *,
    shared=False,
    variant=26,
    variant_payload=b"",
    input_amount=1_000,
    quoted_output=2_000,
    minimum_output=1_950,
    order_input_amount=None,
    order_quoted_output=None,
    slippage_bps=250,
    platform_fee_bps=0,
    route_label="Raydium CLMM",
    route_percent=100.0,
    route_bps=10_000,
    mutate_account=None,
    trailing_amm=True,
    extra_jupiter_instruction=False,
    extra_data=b"",
):
    taker = Pubkey.new_unique()
    input_mint = Pubkey.new_unique()
    output_mint = Pubkey.new_unique()
    amm_key = Pubkey.new_unique()
    dex_program = Pubkey.new_unique()
    jupiter = Pubkey.from_string(JUPITER_V6_PROGRAM_ID)
    event_authority = _pda(jupiter, (b"__event_authority",))
    input_ata = Pubkey.from_string(derive_associated_token_address(str(taker), str(input_mint)))
    output_ata = Pubkey.from_string(derive_associated_token_address(str(taker), str(output_mint)))
    token_program = Pubkey.from_string(CLASSIC_TOKEN_PROGRAM_ID)
    accounts = []

    if shared:
        authority_id = 3
        program_authority = _pda(jupiter, (b"authority", bytes([authority_id])))
        program_source = Pubkey.from_string(
            derive_associated_token_address(str(program_authority), str(input_mint))
        )
        program_destination = Pubkey.from_string(
            derive_associated_token_address(str(program_authority), str(output_mint))
        )
        accounts = [
            AccountMeta(token_program, False, False),
            AccountMeta(program_authority, False, False),
            AccountMeta(taker, True, True),
            AccountMeta(input_ata, False, True),
            AccountMeta(program_source, False, True),
            AccountMeta(program_destination, False, True),
            AccountMeta(output_ata, False, True),
            AccountMeta(input_mint, False, False),
            AccountMeta(output_mint, False, False),
            AccountMeta(jupiter, False, False),
            AccountMeta(jupiter, False, False),
            AccountMeta(event_authority, False, False),
            AccountMeta(jupiter, False, False),
        ]
        discriminator = SHARED_ACCOUNTS_ROUTE_DISCRIMINATOR
        prefix = bytes([authority_id])
        fixed_count = 13
    else:
        accounts = [
            AccountMeta(token_program, False, False),
            AccountMeta(taker, True, True),
            AccountMeta(input_ata, False, True),
            AccountMeta(output_ata, False, True),
            AccountMeta(jupiter, False, False),
            AccountMeta(output_mint, False, False),
            AccountMeta(jupiter, False, False),
            AccountMeta(event_authority, False, False),
            AccountMeta(jupiter, False, False),
        ]
        discriminator = ROUTE_DISCRIMINATOR
        prefix = b""
        fixed_count = 9

    if trailing_amm:
        accounts.extend([
            AccountMeta(dex_program, False, False),
            AccountMeta(amm_key, False, True),
        ])
    if mutate_account is not None:
        accounts[mutate_account] = AccountMeta(Pubkey.new_unique(), False, True)

    route_data = (
        discriminator
        + prefix
        + struct.pack("<I", 1)
        + bytes([variant])
        + variant_payload
        + bytes([int(route_percent), 0, 1])
        + struct.pack("<QQHB", input_amount, quoted_output, slippage_bps, platform_fee_bps)
        + extra_data
    )
    instruction = Instruction(jupiter, route_data, accounts)
    instructions = [instruction]
    if extra_jupiter_instruction:
        instructions.append(instruction)
    blockhash = Hash.new_unique()
    message = MessageV0.try_compile(taker, instructions, [], blockhash)
    message_bytes = to_bytes_versioned(message)
    account_keys = tuple(str(key) for key in message.account_keys)
    program_ids = tuple(
        account_keys[int(ix.program_id_index)]
        for ix in message.instructions
    )
    digest = hashlib.sha256(message_bytes).hexdigest()
    step = JupiterRouteStep(
        amm_key=str(amm_key),
        label=route_label,
        input_mint=str(input_mint),
        output_mint=str(output_mint),
        input_amount_raw=input_amount,
        output_amount_raw=quoted_output,
        percent=route_percent,
        bps=route_bps,
    )
    order = JupiterOrderTransaction(
        provider_id="jupiter-swap-v2",
        request_id="order-1",
        quote_id=None,
        observed_at_epoch_ms=1_000_000,
        taker=str(taker),
        fee_payer=str(taker),
        input_mint=str(input_mint),
        output_mint=str(output_mint),
        input_amount_raw=input_amount if order_input_amount is None else order_input_amount,
        output_amount_raw=quoted_output if order_quoted_output is None else order_quoted_output,
        minimum_output_amount_raw=minimum_output,
        price_impact_pct=0.2,
        fee_bps=50.0,
        router="metis",
        transaction_version=0,
        last_valid_block_height=950,
        recent_blockhash=str(blockhash),
        message_bytes=message_bytes,
        serialized_transaction_bytes=b"serialized",
        route_plan=(step,),
        signature_fee_lamports=5000,
        prioritization_fee_lamports=1000,
        rent_fee_lamports=0,
    )
    validation = TransactionValidationResult(
        message_sha256=digest,
        message_size=len(message_bytes),
        fee_payer=str(taker),
        required_signers=(str(taker),),
        program_ids=program_ids,
        instruction_count=len(message.instructions),
        account_keys=account_keys,
    )
    policy = JupiterV6RoutePolicy(
        allowed_jupiter_program_ids=(JUPITER_V6_PROGRAM_ID,),
        allowed_dex_labels=(route_label,),
    )
    return order, validation, policy, fixed_count


def inspect(values):
    order, validation, policy, _ = values
    return inspect_jupiter_v6_route(order, validation=validation, policy=policy)


def test_inspects_metis_route_instruction_and_fixed_accounts():
    result = inspect(fixture())
    assert result.instruction_name == "route"
    assert result.program_id == JUPITER_V6_PROGRAM_ID
    assert result.route_steps[0].dex_label == "Raydium CLMM"
    assert result.input_amount_raw == 1_000
    assert result.quoted_output_amount_raw == 2_000
    assert result.slippage_bps == 250
    assert result.platform_fee_bps == 0
    assert result.remaining_account_count == 2
    assert result.fully_decoded is False
    assert result.blocking_reasons == ("DEX-specific remaining account roles are not yet decoded",)


def test_inspects_shared_accounts_route_and_authority_pda():
    result = inspect(fixture(shared=True))
    assert result.instruction_name == "sharedAccountsRoute"
    assert result.shared_authority_id == 3
    assert result.remaining_account_count == 2
    assert result.fully_decoded is False


@pytest.mark.parametrize(
    ("variant", "payload", "label"),
    [
        (17, b"\x01", "Orca Whirlpool"),
        (18, b"\x00", "Invariant"),
        (38, b"", "Meteora DLMM"),
        (40, b"", "Raydium CLMM"),
        (46, b"", "Raydium CPMM"),
    ],
)
def test_decodes_reviewed_swap_enum_variants(variant, payload, label):
    result = inspect(fixture(variant=variant, variant_payload=payload, route_label=label))
    assert result.route_steps[0].swap_variant == variant
    assert result.route_steps[0].dex_label == label


def test_rejects_unknown_swap_variant():
    with pytest.raises(JupiterV6RouteError, match="no reviewed decoder"):
        inspect(fixture(variant=250))


def test_rejects_invalid_boolean_swap_variant_payload():
    with pytest.raises(JupiterV6RouteError, match="boolean payload"):
        inspect(fixture(variant=17, variant_payload=b"\x02", route_label="Orca Whirlpool"))


def test_rejects_route_instruction_amount_or_slippage_mismatch():
    with pytest.raises(JupiterV6RouteError, match="route amounts differ"):
        inspect(fixture(input_amount=999, order_input_amount=1_000))
    with pytest.raises(JupiterV6RouteError, match="minimum output"):
        inspect(fixture(minimum_output=1_949))


def test_rejects_nonzero_platform_fee_until_fee_account_is_supported():
    with pytest.raises(JupiterV6RouteError, match="platform fee is not approved"):
        inspect(fixture(platform_fee_bps=1))


def test_rejects_quoted_route_label_or_split_mismatch():
    with pytest.raises(JupiterV6RouteError, match="quoted route label"):
        inspect(fixture(route_label="Meteora"))
    with pytest.raises(JupiterV6RouteError, match="percent/bps"):
        inspect(fixture(route_bps=9_999))


def test_rejects_quoted_route_mint_graph_mismatch():
    order, validation, policy, _ = fixture()
    wrong_step = dataclasses.replace(order.route_plan[0], input_mint=str(Pubkey.new_unique()))
    order = dataclasses.replace(order, route_plan=(wrong_step,))
    with pytest.raises(JupiterV6RouteError, match="mint graph"):
        inspect_jupiter_v6_route(order, validation=validation, policy=policy)


def test_rejects_missing_amm_key_from_remaining_accounts():
    with pytest.raises(JupiterV6RouteError, match="AMM keys are absent"):
        inspect(fixture(trailing_amm=False))


def test_rejects_fixed_account_role_mismatch():
    with pytest.raises(JupiterV6RouteError, match="fixed account roles"):
        inspect(fixture(mutate_account=2))


def test_rejects_multiple_jupiter_route_instructions():
    with pytest.raises(JupiterV6RouteError, match="exactly one"):
        inspect(fixture(extra_jupiter_instruction=True))


def test_rejects_unparsed_trailing_instruction_data():
    with pytest.raises(JupiterV6RouteError, match="trailing or unparsed"):
        inspect(fixture(extra_data=b"\x00"))


def test_rejects_digest_mismatch_and_unsupported_router():
    order, validation, policy, _ = fixture()
    with pytest.raises(JupiterV6RouteError, match="digest"):
        inspect_jupiter_v6_route(
            order,
            validation=dataclasses.replace(validation, message_sha256="0" * 64),
            policy=policy,
        )
    with pytest.raises(JupiterV6RouteError, match="only Metis"):
        inspect_jupiter_v6_route(
            dataclasses.replace(order, router="dflow"),
            validation=validation,
            policy=policy,
        )
