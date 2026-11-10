"""Adversarial tests for binding Jupiter route pools to simulated inner CPI."""
import dataclasses
import hashlib
import json
import pytest
from solders.pubkey import Pubkey

from veltrix.jupiter_instruction_decoder import DecodedJupiterRouteStep, JupiterSwapInstructionResult
from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_program_trace import (
    JupiterProgramRegistrySnapshot,
    JupiterProgramTraceError,
    JupiterRouteProgramPolicy,
    evaluate_jupiter_route_programs,
)
from veltrix.simulation_gate import (
    SimulatedInnerInstruction,
    SimulationEvidence,
    SimulationGateResult,
)
from veltrix.transaction_validator import TransactionValidationResult


def fixture():
    taker = str(Pubkey.new_unique())
    jupiter_program = str(Pubkey.new_unique())
    dex_program = str(Pubkey.new_unique())
    input_mint = str(Pubkey.new_unique())
    output_mint = str(Pubkey.new_unique())
    amm_pool = str(Pubkey.new_unique())
    input_ata = str(Pubkey.new_unique())
    output_ata = str(Pubkey.new_unique())
    message = b"exact-order-message"
    digest = hashlib.sha256(message).hexdigest()
    route = JupiterRouteStep(
        amm_key=amm_pool,
        label="Raydium CLMM",
        input_mint=input_mint,
        output_mint=output_mint,
        input_amount_raw=1000,
        output_amount_raw=2000,
        percent=100.0,
        bps=10_000,
    )
    order = JupiterOrderTransaction(
        provider_id="jupiter-swap-v2",
        request_id="order-1",
        quote_id=None,
        observed_at_epoch_ms=1_000_000,
        taker=taker,
        fee_payer=taker,
        input_mint=input_mint,
        output_mint=output_mint,
        input_amount_raw=1000,
        output_amount_raw=2000,
        minimum_output_amount_raw=1950,
        price_impact_pct=0.2,
        fee_bps=50.0,
        router="metis",
        transaction_version=0,
        last_valid_block_height=950,
        recent_blockhash="blockhash-1",
        message_bytes=message,
        serialized_transaction_bytes=b"serialized-transaction",
        route_plan=(route,),
        signature_fee_lamports=5000,
        prioritization_fee_lamports=1000,
        rent_fee_lamports=0,
    )
    validation = TransactionValidationResult(
        message_sha256=digest,
        message_size=len(message),
        fee_payer=taker,
        required_signers=(taker,),
        program_ids=(jupiter_program,),
        instruction_count=1,
        account_keys=(taker, jupiter_program, dex_program, amm_pool, input_ata, output_ata),
    )
    jupiter_instruction = JupiterSwapInstructionResult(
        message_sha256=digest,
        instruction_name="route",
        program_id=jupiter_program,
        transfer_authority=taker,
        input_token_account=input_ata,
        output_token_account=output_ata,
        input_mint=input_mint,
        output_mint=output_mint,
        input_amount_raw=1000,
        quoted_output_amount_raw=2000,
        slippage_bps=100,
        platform_fee_bps=0,
        shared_route_id=None,
        top_level_instruction_index=0,
        route_plan=(
            DecodedJupiterRouteStep(
                swap_variant="RaydiumClmm",
                dex_label="Raydium CLMM",
                variant_fields=(),
                percent=100,
                input_index=0,
                output_index=1,
            ),
        ),
    )
    inner_instruction = SimulatedInnerInstruction(
        top_level_instruction_index=0,
        program_id=dex_program,
        account_keys=(amm_pool, input_ata, output_ata),
        data=b"cpi-instruction-data",
        stack_height=2,
    )
    simulation = SimulationEvidence(
        message_sha256=digest,
        recent_blockhash="blockhash-1",
        cluster="mainnet-beta",
        genesis_hash="genesis",
        provider_id="rpc-primary",
        response_id="simulation-1",
        observed_at_epoch_ms=1_000_000,
        context_slot=500,
        current_block_height=900,
        last_valid_block_height=950,
        simulation_error=None,
        units_consumed=100_000,
        fee_lamports=5000,
        token_balances=(),
        top_level_program_ids=(jupiter_program,),
        inner_program_ids=(dex_program,),
        inner_instruction_count=1,
        inner_instructions=(inner_instruction,),
    )
    gate = SimulationGateResult(
        approved=True,
        message_sha256=digest,
        provider_id="rpc-primary",
        response_id="simulation-1",
        context_slot=500,
        units_consumed=100_000,
        fee_lamports=5000,
        verified_token_account_count=2,
    )
    pairs = tuple(sorted((
        (jupiter_program, "Jupiter V6"),
        (dex_program, "Raydium CLMM"),
    )))
    registry_digest = hashlib.sha256(
        json.dumps(pairs, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    registry = JupiterProgramRegistrySnapshot(
        observed_at_epoch_ms=1_000_000,
        program_labels=pairs,
        sha256=registry_digest,
    )
    policy = JupiterRouteProgramPolicy(
        allowed_dex_labels=("Raydium CLMM",),
        allowed_dex_program_ids=(dex_program,),
        allowed_jupiter_program_ids=(jupiter_program,),
        allowed_routers=("metis",),
    )
    return order, validation, jupiter_instruction, simulation, gate, registry, policy


def evaluate(values=None):
    order, validation, jupiter_instruction, simulation, gate, registry, policy = values or fixture()
    return evaluate_jupiter_route_programs(
        order,
        validation=validation,
        jupiter_instruction=jupiter_instruction,
        simulation=simulation,
        simulation_gate=gate,
        registry=registry,
        policy=policy,
        now_epoch_ms=1_005_000,
    )


def test_accepts_route_when_pool_and_user_accounts_match_simulated_cpi():
    result = evaluate()
    assert result.passed is True
    assert result.route_labels == ("Raydium CLMM",)
    assert len(result.dex_program_ids) == 1
    assert result.route_pool_accounts_verified == 1
    assert result.cpi_instruction_count == 1
    assert result.parent_instruction_index == 0


def test_rejects_unapproved_route_label():
    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    policy = dataclasses.replace(policy, allowed_dex_labels=("Orca Whirlpool",))
    with pytest.raises(JupiterProgramTraceError, match="outside local policy"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))


def test_rejects_unknown_inner_cpi_program():
    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    simulation = dataclasses.replace(
        simulation,
        inner_program_ids=("unknown-program",),
        inner_instructions=(dataclasses.replace(simulation.inner_instructions[0], program_id="unknown-program"),),
    )
    with pytest.raises(JupiterProgramTraceError, match="outside the route allowlist"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))


def test_rejects_route_label_not_present_in_registry():
    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    only_jupiter = ((validation.program_ids[0], "Jupiter V6"),)
    registry = JupiterProgramRegistrySnapshot(
        observed_at_epoch_ms=1_000_000,
        program_labels=only_jupiter,
        sha256=hashlib.sha256(
            json.dumps(only_jupiter, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest(),
    )
    with pytest.raises(JupiterProgramTraceError, match="no locally pinned program ID"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))


def test_rejects_missing_jupiter_aggregator_program():
    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    validation = dataclasses.replace(validation, program_ids=("unknown-program",))
    simulation = dataclasses.replace(simulation, top_level_program_ids=("unknown-program",))
    with pytest.raises(JupiterProgramTraceError, match="does not invoke an approved Jupiter"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))


def test_rejects_empty_inner_cpi_trace():
    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    simulation = dataclasses.replace(
        simulation,
        inner_program_ids=(),
        inner_instruction_count=0,
        inner_instructions=(),
    )
    with pytest.raises(JupiterProgramTraceError, match="inner-CPI program trace"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))


def test_rejects_digest_mismatch():
    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    simulation = dataclasses.replace(simulation, message_sha256="b" * 64)
    with pytest.raises(JupiterProgramTraceError, match="digests differ"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))


def test_rejects_stale_registry_snapshot():
    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    registry = dataclasses.replace(registry, observed_at_epoch_ms=1)
    policy = dataclasses.replace(policy, max_registry_age_ms=1_000)
    with pytest.raises(JupiterProgramTraceError, match="stale"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))


def test_rejects_nonmatching_fee_payer_or_blockhash():
    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    order = dataclasses.replace(order, fee_payer="other-payer")
    with pytest.raises(JupiterProgramTraceError, match="not the transaction fee payer"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))

    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    simulation = dataclasses.replace(simulation, recent_blockhash="other-blockhash")
    with pytest.raises(JupiterProgramTraceError, match="blockhash differs"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))


def test_rejects_inner_program_missing_from_validated_accounts():
    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    validation = dataclasses.replace(validation, account_keys=(validation.fee_payer, validation.program_ids[0]))
    with pytest.raises(JupiterProgramTraceError, match="inner CPI program ID is absent"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))


def test_rejects_registry_digest_tampering():
    _, _, _, _, _, registry, _ = fixture()
    with pytest.raises(JupiterProgramTraceError, match="digest"):
        dataclasses.replace(registry, sha256="0" * 64)


def test_rejects_pool_not_present_in_dex_cpi_accounts():
    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    simulation = dataclasses.replace(
        simulation,
        inner_instructions=(dataclasses.replace(simulation.inner_instructions[0], account_keys=(jupiter_instruction.input_token_account, jupiter_instruction.output_token_account)),),
    )
    with pytest.raises(JupiterProgramTraceError, match="does not bind route pool"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))


def test_rejects_inner_instruction_nested_under_wrong_parent():
    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    simulation = dataclasses.replace(
        simulation,
        inner_instructions=(dataclasses.replace(simulation.inner_instructions[0], top_level_instruction_index=1),),
    )
    with pytest.raises(JupiterProgramTraceError, match="not nested under"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))


def test_rejects_missing_user_token_account_in_cpi_trace():
    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    simulation = dataclasses.replace(
        simulation,
        inner_instructions=(dataclasses.replace(
            simulation.inner_instructions[0],
            account_keys=(order.route_plan[0].amm_key, jupiter_instruction.input_token_account),
        ),),
    )
    with pytest.raises(JupiterProgramTraceError, match="does not reference both"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))


def test_rejects_cpi_stack_height_above_policy():
    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    simulation = dataclasses.replace(
        simulation,
        inner_instructions=(dataclasses.replace(simulation.inner_instructions[0], stack_height=9),),
    )
    with pytest.raises(JupiterProgramTraceError, match="stack height"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))


def test_rejects_cpi_instruction_without_pool_payload():
    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    simulation = dataclasses.replace(
        simulation,
        inner_instructions=(dataclasses.replace(simulation.inner_instructions[0], data=b""),),
    )
    with pytest.raises(JupiterProgramTraceError, match="does not bind route pool"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))


def test_rejects_decoded_instruction_for_different_message():
    order, validation, jupiter_instruction, simulation, gate, registry, policy = fixture()
    jupiter_instruction = dataclasses.replace(jupiter_instruction, message_sha256="f" * 64)
    with pytest.raises(JupiterProgramTraceError, match="different message"):
        evaluate((order, validation, jupiter_instruction, simulation, gate, registry, policy))
