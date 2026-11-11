"""Adversarial tests for the pinned Meteora DLMM swap2 CPI semantic gate."""
import dataclasses
import hashlib
import struct

import pytest
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_route_semantics import DecodedJupiterRouteStep, JupiterRouteSemanticResult
from veltrix.jupiter_swap_effects import derive_associated_token_address
from veltrix.meteora_cpi_semantics import (
    CLASSIC_TOKEN_PROGRAM_ID,
    MEMO_PROGRAM_ID,
    METEORA_DLMM_PROGRAM_ID,
    METEORA_SWAP2_DISCRIMINATOR,
    MeteoraCpiPolicy,
    MeteoraCpiSemanticError,
    decode_meteora_swap2,
    evaluate_meteora_dlmm_cpis,
)
from veltrix.simulation_gate import SimulatedInnerInstruction, SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult


def instruction_data(*, amount=1_000, minimum_output=1_900, remaining_count=0):
    return (
        METEORA_SWAP2_DISCRIMINATOR
        + struct.pack("<QQI", amount, minimum_output, remaining_count)
    )


def fixture():
    taker = str(Pubkey.new_unique())
    input_mint = str(Pubkey.new_unique())
    output_mint = str(Pubkey.new_unique())
    lb_pair = str(Pubkey.new_unique())
    jupiter = str(Pubkey.new_unique())
    program = METEORA_DLMM_PROGRAM_ID
    event_authority, _ = Pubkey.find_program_address([b"__event_authority"], Pubkey.from_string(program))
    other = [str(Pubkey.new_unique()) for _ in range(5)]
    accounts = (
        lb_pair,                                                    # lb_pair
        program,                                                    # optional bitmap extension omitted
        other[0],                                                   # reserve_x
        other[1],                                                   # reserve_y
        derive_associated_token_address(taker, input_mint),          # user_token_in
        derive_associated_token_address(taker, output_mint),         # user_token_out
        input_mint,                                                 # token_x_mint
        output_mint,                                                # token_y_mint
        other[2],                                                   # oracle
        program,                                                    # optional host_fee_in omitted
        taker,                                                      # user signer
        CLASSIC_TOKEN_PROGRAM_ID,                                   # token_x_program
        CLASSIC_TOKEN_PROGRAM_ID,                                   # token_y_program
        MEMO_PROGRAM_ID,                                            # memo_program
        str(event_authority),                                       # event_authority
        program,                                                    # program
        other[3],                                                   # bin_array
    )
    message = b"meteora-order-message"
    digest = hashlib.sha256(message).hexdigest()
    order = JupiterOrderTransaction(
        provider_id="jupiter-swap-v2",
        request_id="order-1",
        quote_id="quote-1",
        observed_at_epoch_ms=1_000_000,
        taker=taker,
        fee_payer=taker,
        input_mint=input_mint,
        output_mint=output_mint,
        input_amount_raw=1_000,
        output_amount_raw=2_000,
        minimum_output_amount_raw=1_900,
        price_impact_pct=0.1,
        fee_bps=0.0,
        router="metis",
        transaction_version=0,
        last_valid_block_height=950,
        recent_blockhash="blockhash",
        message_bytes=message,
        serialized_transaction_bytes=b"unsigned",
        route_plan=(
            JupiterRouteStep(
                amm_key=lb_pair,
                label="Meteora DLMM",
                input_mint=input_mint,
                output_mint=output_mint,
                input_amount_raw=1_000,
                output_amount_raw=2_000,
                percent=100.0,
                bps=10_000,
            ),
        ),
        signature_fee_lamports=5_000,
        prioritization_fee_lamports=0,
        rent_fee_lamports=0,
    )
    validation = TransactionValidationResult(
        message_sha256=digest,
        message_size=len(message),
        fee_payer=taker,
        required_signers=(taker,),
        program_ids=(jupiter,),
        instruction_count=1,
        account_keys=(taker, jupiter),
    )
    inner = SimulatedInnerInstruction(
        outer_instruction_index=0,
        program_id=program,
        account_keys=accounts,
        data=instruction_data(),
        stack_height=2,
    )
    simulation = SimulationEvidence(
        message_sha256=digest,
        recent_blockhash="blockhash",
        cluster="mainnet-beta",
        genesis_hash="genesis",
        provider_id="rpc-primary",
        response_id="sim-1",
        observed_at_epoch_ms=1_001_000,
        context_slot=100,
        current_block_height=900,
        last_valid_block_height=950,
        simulation_error=None,
        units_consumed=100_000,
        fee_lamports=5_000,
        token_balances=(),
        top_level_program_ids=(jupiter,),
        inner_program_ids=(program,),
        inner_instruction_count=1,
        inner_instructions=(inner,),
    )
    sim_gate = SimulationGateResult(
        approved=True,
        message_sha256=digest,
        provider_id="rpc-primary",
        response_id="sim-1",
        context_slot=100,
        units_consumed=100_000,
        fee_lamports=5_000,
        verified_token_account_count=0,
    )
    program_gate = JupiterRouteProgramGateResult(
        passed=True,
        message_sha256=digest,
        router="metis",
        route_labels=("Meteora DLMM",),
        dex_program_ids=(program,),
        top_level_program_ids=(jupiter,),
        inner_program_ids=(program,),
        registry_sha256="a" * 64,
    )
    route_semantics = JupiterRouteSemanticResult(
        passed=True,
        message_sha256=digest,
        jupiter_program_id=jupiter,
        instruction_index=0,
        route_steps=(DecodedJupiterRouteStep(38, 100, 0, 1),),
        input_amount_raw=1_000,
        quoted_output_amount_raw=2_000,
        slippage_bps=50,
        platform_fee_bps=0,
        token_index_mints=((0, input_mint), (1, output_mint)),
    )
    return order, validation, simulation, sim_gate, program_gate, route_semantics, MeteoraCpiPolicy()


def evaluate(values=None):
    order, validation, simulation, sim_gate, program_gate, route_semantics, policy = values or fixture()
    return evaluate_meteora_dlmm_cpis(
        order,
        validation=validation,
        simulation=simulation,
        simulation_gate=sim_gate,
        program_gate=program_gate,
        route_semantics=route_semantics,
        policy=policy,
    )


def test_decodes_pinned_meteora_swap2_borsh_payload():
    decoded = decode_meteora_swap2(instruction_data())
    assert decoded.amount_in_raw == 1_000
    assert decoded.minimum_output_raw == 1_900
    assert decoded.remaining_accounts_info_count == 0


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "exactly 28 bytes"),
        (b"12345678" + instruction_data()[8:], "pinned swap2"),
        (instruction_data(amount=0), "must be positive"),
        (instruction_data(minimum_output=0), "must be positive"),
        (instruction_data(remaining_count=1), "remaining-account slices"),
        (instruction_data() + b"extra", "exactly 28 bytes"),
    ],
)
def test_rejects_malformed_or_unsupported_swap2_payload(data, message):
    with pytest.raises(MeteoraCpiSemanticError, match=message):
        decode_meteora_swap2(data)


def test_accepts_quote_bound_meteora_dlmm_swap2_cpi():
    values = fixture()
    result = evaluate(values)
    assert result.passed is True
    assert result.cpi_count == 1
    assert result.lb_pair_ids == (values[0].route_plan[0].amm_key,)
    assert result.input_amounts_raw == (1_000,)
    assert result.minimum_output_amounts_raw == (1_900,)


@pytest.mark.parametrize(
    ("account_index", "replacement", "message"),
    [
        (0, str(Pubkey.new_unique()), "LB pair differs"),
        (4, str(Pubkey.new_unique()), "input token account is not the taker's"),
        (5, str(Pubkey.new_unique()), "output token account is not the taker's"),
        (6, str(Pubkey.new_unique()), "pair mint accounts differ"),
        (7, str(Pubkey.new_unique()), "pair mint accounts differ"),
        (9, str(Pubkey.new_unique()), "host-fee account must be omitted"),
        (10, str(Pubkey.new_unique()), "user signer differs"),
        (11, str(Pubkey.new_unique()), "classic SPL Token"),
        (12, str(Pubkey.new_unique()), "classic SPL Token"),
        (13, str(Pubkey.new_unique()), "memo program"),
        (14, str(Pubkey.new_unique()), "event-authority PDA"),
        (15, str(Pubkey.new_unique()), "program account differs"),
    ],
)
def test_rejects_mismatched_meteora_fixed_account_semantics(account_index, replacement, message):
    values = list(fixture())
    inner = values[2].inner_instructions[0]
    accounts = list(inner.account_keys)
    accounts[account_index] = replacement
    changed = dataclasses.replace(inner, account_keys=tuple(accounts))
    values[2] = dataclasses.replace(values[2], inner_instructions=(changed,))
    with pytest.raises(MeteoraCpiSemanticError, match=message):
        evaluate(tuple(values))


def test_rejects_wrong_amount_threshold_and_wrong_stack_height():
    values = list(fixture())
    inner = dataclasses.replace(values[2].inner_instructions[0], data=instruction_data(amount=999))
    values[2] = dataclasses.replace(values[2], inner_instructions=(inner,))
    with pytest.raises(MeteoraCpiSemanticError, match="input amount differs"):
        evaluate(tuple(values))

    values = list(fixture())
    inner = dataclasses.replace(values[2].inner_instructions[0], data=instruction_data(minimum_output=2_001))
    values[2] = dataclasses.replace(values[2], inner_instructions=(inner,))
    with pytest.raises(MeteoraCpiSemanticError, match="threshold exceeds"):
        evaluate(tuple(values))

    values = list(fixture())
    inner = dataclasses.replace(values[2].inner_instructions[0], stack_height=3)
    values[2] = dataclasses.replace(values[2], inner_instructions=(inner,))
    with pytest.raises(MeteoraCpiSemanticError, match="depth-2"):
        evaluate(tuple(values))


def test_accepts_optional_bitmap_extension_but_rejects_aliases():
    values = list(fixture())
    inner = values[2].inner_instructions[0]
    accounts = list(inner.account_keys)
    accounts[1] = str(Pubkey.new_unique())
    values[2] = dataclasses.replace(values[2], inner_instructions=(dataclasses.replace(inner, account_keys=tuple(accounts)),))
    assert evaluate(tuple(values)).passed is True

    values = list(fixture())
    inner = values[2].inner_instructions[0]
    accounts = list(inner.account_keys)
    accounts[1] = accounts[2]
    values[2] = dataclasses.replace(values[2], inner_instructions=(dataclasses.replace(inner, account_keys=tuple(accounts)),))
    with pytest.raises(MeteoraCpiSemanticError, match="bitmap-extension account"):
        evaluate(tuple(values))


def test_rejects_missing_bin_arrays_mixed_dex_unknown_cpi_and_mismatched_trace():
    values = list(fixture())
    inner = values[2].inner_instructions[0]
    values[2] = dataclasses.replace(values[2], inner_instructions=(dataclasses.replace(inner, account_keys=inner.account_keys[:16]),))
    with pytest.raises(MeteoraCpiSemanticError, match="missing required bin-array"):
        evaluate(tuple(values))

    values = list(fixture())
    order = dataclasses.replace(values[0], route_plan=(dataclasses.replace(values[0].route_plan[0], label="Raydium CLMM"),))
    gate = dataclasses.replace(values[4], route_labels=("Raydium CLMM",))
    with pytest.raises(MeteoraCpiSemanticError, match="only all-Meteora"):
        evaluate((order, *values[1:4], gate, values[5], values[6]))

    values = list(fixture())
    inner = dataclasses.replace(values[2].inner_instructions[0], program_id=str(Pubkey.new_unique()))
    simulation = dataclasses.replace(values[2], inner_instructions=(inner,), inner_program_ids=(inner.program_id,))
    gate = dataclasses.replace(values[4], inner_program_ids=(inner.program_id,))
    with pytest.raises(MeteoraCpiSemanticError, match="unapproved inner CPI program"):
        evaluate((values[0], values[1], simulation, values[3], gate, values[5], values[6]))
