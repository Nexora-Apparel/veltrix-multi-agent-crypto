from __future__ import annotations

import pytest

from veltrix.jupiter_v6_instruction_decoder import (
    JUPITER_V6_PROGRAM_ID,
    JupiterInstructionDecodeError,
    decode_jupiter_v6_route_instruction,
)


def route_data(
    *,
    discriminator: str = "e517cb977ae3ad2a",
    route_id: int | None = None,
    variant: int = 7,
    variant_payload: bytes = b"",
    percent: int = 100,
    input_index: int = 0,
    output_index: int = 1,
    amount: int = 123,
    quoted: int = 456,
    slippage: int = 50,
    fee: int = 0,
    trailing: bytes = b"",
) -> bytes:
    body = bytes.fromhex(discriminator)
    if route_id is not None:
        body += bytes([route_id])
    body += (1).to_bytes(4, "little")
    body += bytes([variant]) + variant_payload
    body += bytes([percent, input_index, output_index])
    body += amount.to_bytes(8, "little")
    body += quoted.to_bytes(8, "little")
    body += slippage.to_bytes(2, "little")
    body += bytes([fee])
    return body + trailing


def test_decodes_jupiter_v6_route_and_keeps_execution_unapproved():
    decoded = decode_jupiter_v6_route_instruction(
        JUPITER_V6_PROGRAM_ID, route_data()
    )
    assert decoded.instruction_name == "route"
    assert decoded.exact_out is False
    assert decoded.amount == 123
    assert decoded.quoted_amount == 456
    assert decoded.slippage_bps == 50
    assert decoded.platform_fee_bps == 0
    assert decoded.route_plan[0].swap_variant == "Raydium"
    assert decoded.route_plan[0].percent == 100
    assert decoded.semantic_coverage == "jupiter_outer_instruction_only"
    assert decoded.execution_approved is False


def test_decodes_shared_route_id_and_variant_payload():
    decoded = decode_jupiter_v6_route_instruction(
        JUPITER_V6_PROGRAM_ID,
        route_data(
            discriminator="c1209b3341d69c81",
            route_id=3,
            variant=17,
            variant_payload=b"\x01",
        ),
    )
    assert decoded.instruction_name == "shared_accounts_route"
    assert decoded.route_id == 3
    assert decoded.route_plan[0].swap_variant == "Whirlpool"
    assert decoded.route_plan[0].variant_args == (True,)


def test_decodes_variant_with_multiple_serialized_fields():
    decoded = decode_jupiter_v6_route_instruction(
        JUPITER_V6_PROGRAM_ID,
        route_data(variant=42, variant_payload=b"\x02\x01\x00"),
    )
    assert decoded.route_plan[0].swap_variant == "Clone"
    assert decoded.route_plan[0].variant_args == (2, True, False)


@pytest.mark.parametrize(
    "program_id,data,reason",
    [
        ("not-jupiter", route_data(), "unexpected_jupiter_program_id"),
        (JUPITER_V6_PROGRAM_ID, b"", "instruction_data_too_short"),
        (JUPITER_V6_PROGRAM_ID, bytes.fromhex("ffffffffffffffff"), "unsupported_jupiter_discriminator"),
        (JUPITER_V6_PROGRAM_ID, route_data(variant=255), "unknown_jupiter_swap_variant"),
        (JUPITER_V6_PROGRAM_ID, route_data(variant=17, variant_payload=b"\x02"), "noncanonical_boolean"),
        (JUPITER_V6_PROGRAM_ID, route_data(percent=101), "route_percent_out_of_range"),
        (JUPITER_V6_PROGRAM_ID, route_data(input_index=1, output_index=1), "route_step_input_output_alias"),
        (JUPITER_V6_PROGRAM_ID, route_data(amount=0), "route_amount_must_be_positive"),
        (JUPITER_V6_PROGRAM_ID, route_data(slippage=10001), "route_fee_or_slippage_out_of_range"),
        (JUPITER_V6_PROGRAM_ID, route_data(trailing=b"\x00"), "trailing_instruction_data"),
    ],
)
def test_rejects_malformed_or_unsupported_jupiter_route(program_id, data, reason):
    with pytest.raises(JupiterInstructionDecodeError) as exc:
        decode_jupiter_v6_route_instruction(program_id, data)
    assert exc.value.reason_code == reason


def test_rejects_truncated_variant_payload():
    with pytest.raises(JupiterInstructionDecodeError, match="truncated_instruction_data"):
        decode_jupiter_v6_route_instruction(
            JUPITER_V6_PROGRAM_ID, route_data(variant=17, variant_payload=b"")
        )


def test_rejects_out_of_range_route_count_and_invalid_step_limit():
    data = bytes.fromhex("e517cb977ae3ad2a") + (13).to_bytes(4, "little")
    with pytest.raises(JupiterInstructionDecodeError, match="route_step_count_out_of_range"):
        decode_jupiter_v6_route_instruction(JUPITER_V6_PROGRAM_ID, data)
    with pytest.raises(JupiterInstructionDecodeError, match="invalid_route_step_limit"):
        decode_jupiter_v6_route_instruction(JUPITER_V6_PROGRAM_ID, route_data(), max_route_steps=0)

def test_decodes_whirlpool_swap_v2_option_none_and_some():
    absent = decode_jupiter_v6_route_instruction(
        JUPITER_V6_PROGRAM_ID,
        route_data(variant=47, variant_payload=b"\x01\x00"),
    )
    assert absent.route_plan[0].variant_args == (True, False, 0, 0)

    present = decode_jupiter_v6_route_instruction(
        JUPITER_V6_PROGRAM_ID,
        route_data(
            variant=47,
            variant_payload=b"\x00\x01" + (1).to_bytes(4, "little") + b"\x00\x02",
        ),
    )
    assert present.route_plan[0].variant_args == (False, True, 1, 2)


def test_rejects_noncanonical_whirlpool_option_tag():
    with pytest.raises(JupiterInstructionDecodeError, match="noncanonical_option_tag"):
        decode_jupiter_v6_route_instruction(
            JUPITER_V6_PROGRAM_ID,
            route_data(variant=47, variant_payload=b"\x01\x02"),
        )
