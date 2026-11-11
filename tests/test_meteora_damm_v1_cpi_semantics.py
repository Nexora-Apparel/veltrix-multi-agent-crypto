"""Adversarial tests for the pinned Meteora DAMM v1 swap CPI gate."""
import dataclasses
import hashlib
import struct

import pytest
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_route_semantics import DecodedJupiterRouteStep, JupiterRouteSemanticResult
from veltrix.jupiter_swap_effects import derive_associated_token_address
from veltrix.meteora_damm_v1_cpi_semantics import (
    CLASSIC_TOKEN_PROGRAM_ID,
    METEORA_DAMM_V1_PROGRAM_ID,
    METEORA_DAMM_V1_SWAP_DISCRIMINATOR,
    METEORA_VAULT_PROGRAM_ID,
    MeteoraDammV1CpiPolicy,
    MeteoraDammV1CpiSemanticError,
    decode_meteora_damm_v1_swap,
    evaluate_meteora_damm_v1_cpis,
)
from veltrix.simulation_gate import SimulatedInnerInstruction, SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult


def instruction_data(*, amount=1_000, minimum_out=1_900):
    return METEORA_DAMM_V1_SWAP_DISCRIMINATOR + struct.pack("<QQ", amount, minimum_out)


def fixture():
    taker = str(Pubkey.new_unique())
    input_mint = str(Pubkey.new_unique())
    output_mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    jupiter = str(Pubkey.new_unique())
    accounts = (
        pool,                                                        # pool
        derive_associated_token_address(taker, input_mint),           # user source
        derive_associated_token_address(taker, output_mint),          # user destination
        str(Pubkey.new_unique()),                                    # a_vault
        str(Pubkey.new_unique()),                                    # b_vault
        str(Pubkey.new_unique()),                                    # a_token_vault
        str(Pubkey.new_unique()),                                    # b_token_vault
        str(Pubkey.new_unique()),                                    # a_vault_lp_mint
        str(Pubkey.new_unique()),                                    # b_vault_lp_mint
        str(Pubkey.new_unique()),                                    # a_vault_lp
        str(Pubkey.new_unique()),                                    # b_vault_lp
        str(Pubkey.new_unique()),                                    # protocol token fee
        taker,                                                       # user signer
        METEORA_VAULT_PROGRAM_ID,
        CLASSIC_TOKEN_PROGRAM_ID,
    )
    message = b"meteora-damm-v1-order-message"
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
                amm_key=pool,
                label="Meteora",
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
        program_id=METEORA_DAMM_V1_PROGRAM_ID,
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
        inner_program_ids=(METEORA_DAMM_V1_PROGRAM_ID,),
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
        route_labels=("Meteora",),
        dex_program_ids=(METEORA_DAMM_V1_PROGRAM_ID,),
        top_level_program_ids=(jupiter,),
        inner_program_ids=(METEORA_DAMM_V1_PROGRAM_ID,),
        registry_sha256="a" * 64,
    )
    route_semantics = JupiterRouteSemanticResult(
        passed=True,
        message_sha256=digest,
        jupiter_program_id=jupiter,
        instruction_index=0,
        route_steps=(DecodedJupiterRouteStep(19, 100, 0, 1),),
        input_amount_raw=1_000,
        quoted_output_amount_raw=2_000,
        slippage_bps=50,
        platform_fee_bps=0,
        token_index_mints=((0, input_mint), (1, output_mint)),
    )
    return order, validation, simulation, sim_gate, program_gate, route_semantics, MeteoraDammV1CpiPolicy()


def evaluate(values=None):
    order, validation, simulation, sim_gate, program_gate, route_semantics, policy = values or fixture()
    return evaluate_meteora_damm_v1_cpis(
        order,
        validation=validation,
        simulation=simulation,
        simulation_gate=sim_gate,
        program_gate=program_gate,
        route_semantics=route_semantics,
        policy=policy,
    )


def test_decodes_pinned_meteora_damm_v1_swap_payload():
    decoded = decode_meteora_damm_v1_swap(instruction_data())
    assert decoded.input_amount_raw == 1_000
    assert decoded.minimum_output_amount_raw == 1_900


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "exactly 24 bytes"),
        (b"12345678" + instruction_data()[8:], "pinned swap"),
        (instruction_data(amount=0), "must be positive"),
        (instruction_data(minimum_out=0), "must be positive"),
        (instruction_data() + b"extra", "exactly 24 bytes"),
    ],
)
def test_rejects_malformed_or_unsupported_swap_payload(data, message):
    with pytest.raises(MeteoraDammV1CpiSemanticError, match=message):
        decode_meteora_damm_v1_swap(data)


def test_accepts_quote_bound_meteora_damm_v1_swap_cpi():
    values = fixture()
    result = evaluate(values)
    assert result.passed is True
    assert result.cpi_count == 1
    assert result.pool_ids == (values[0].route_plan[0].amm_key,)
    assert result.input_amounts_raw == (1_000,)
    assert result.minimum_output_amounts_raw == (1_900,)


@pytest.mark.parametrize(
    ("account_index", "replacement", "message"),
    [
        (0, str(Pubkey.new_unique()), "pool differs"),
        (1, str(Pubkey.new_unique()), "input token account is not the taker's"),
        (2, str(Pubkey.new_unique()), "output token account is not the taker's"),
        (12, str(Pubkey.new_unique()), "user signer differs"),
        (13, str(Pubkey.new_unique()), "vault program differs"),
        (14, str(Pubkey.new_unique()), "classic SPL Token"),
    ],
)
def test_rejects_mismatched_damm_v1_accounts(account_index, replacement, message):
    values = list(fixture())
    inner = values[2].inner_instructions[0]
    accounts = list(inner.account_keys)
    accounts[account_index] = replacement
    values[2] = dataclasses.replace(
        values[2],
        inner_instructions=(dataclasses.replace(inner, account_keys=tuple(accounts)),),
    )
    with pytest.raises(MeteoraDammV1CpiSemanticError, match=message):
        evaluate(tuple(values))


def test_rejects_wrong_amount_threshold_depth_and_remaining_accounts():
    values = list(fixture())
    inner = dataclasses.replace(values[2].inner_instructions[0], data=instruction_data(amount=999))
    values[2] = dataclasses.replace(values[2], inner_instructions=(inner,))
    with pytest.raises(MeteoraDammV1CpiSemanticError, match="input amount differs"):
        evaluate(tuple(values))

    values = list(fixture())
    inner = dataclasses.replace(values[2].inner_instructions[0], data=instruction_data(minimum_out=2_001))
    values[2] = dataclasses.replace(values[2], inner_instructions=(inner,))
    with pytest.raises(MeteoraDammV1CpiSemanticError, match="threshold exceeds"):
        evaluate(tuple(values))

    values = list(fixture())
    inner = dataclasses.replace(values[2].inner_instructions[0], stack_height=3)
    values[2] = dataclasses.replace(values[2], inner_instructions=(inner,))
    with pytest.raises(MeteoraDammV1CpiSemanticError, match="depth-2"):
        evaluate(tuple(values))

    values = list(fixture())
    inner = dataclasses.replace(inner, account_keys=inner.account_keys + (str(Pubkey.new_unique()),))
    values[2] = dataclasses.replace(values[2], inner_instructions=(inner,))
    with pytest.raises(MeteoraDammV1CpiSemanticError, match="exactly 15"):
        evaluate(tuple(values))


def test_rejects_mixed_dex_unknown_program_and_incomplete_trace():
    values = list(fixture())
    order = dataclasses.replace(values[0], route_plan=(dataclasses.replace(values[0].route_plan[0], label="Meteora DLMM"),))
    gate = dataclasses.replace(values[4], route_labels=("Meteora DLMM",))
    with pytest.raises(MeteoraDammV1CpiSemanticError, match="only all-Meteora-DAMM"):
        evaluate((order, values[1], values[2], values[3], gate, values[5], values[6]))

    values = list(fixture())
    inner = dataclasses.replace(values[2].inner_instructions[0], program_id=str(Pubkey.new_unique()))
    simulation = dataclasses.replace(values[2], inner_instructions=(inner,), inner_program_ids=(inner.program_id,))
    gate = dataclasses.replace(values[4], inner_program_ids=(inner.program_id,))
    with pytest.raises(MeteoraDammV1CpiSemanticError, match="unapproved inner CPI program"):
        evaluate((values[0], values[1], simulation, values[3], gate, values[5], values[6]))

    values = list(fixture())
    simulation = dataclasses.replace(values[2], inner_instructions=())
    with pytest.raises(MeteoraDammV1CpiSemanticError, match="missing or inconsistent"):
        evaluate((values[0], values[1], simulation, values[3], values[4], values[5], values[6]))
