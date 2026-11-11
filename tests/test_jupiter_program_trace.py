"""Adversarial tests for Jupiter route trace and integrated preflight gates."""
import dataclasses
import hashlib
import json
import struct

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey

from veltrix.compute_budget import COMPUTE_BUDGET_PROGRAM_ID
from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_program_trace import (
    JupiterProgramRegistrySnapshot,
    JupiterProgramTraceError,
    JupiterRouteProgramPolicy,
    evaluate_jupiter_route_programs,
)
from veltrix.jupiter_swap_effects import CLASSIC_TOKEN_PROGRAM_ID, derive_associated_token_address
from veltrix.jupiter_v6_route import JUPITER_V6_PROGRAM_ID, ROUTE_DISCRIMINATOR
from veltrix.simulation_gate import SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult


def compute_limit(value):
    return Instruction(
        Pubkey.from_string(COMPUTE_BUDGET_PROGRAM_ID),
        bytes([2]) + struct.pack("<I", value),
        [],
    )


def compute_price(value):
    return Instruction(
        Pubkey.from_string(COMPUTE_BUDGET_PROGRAM_ID),
        bytes([3]) + struct.pack("<Q", value),
        [],
    )


def fixture(*, compute_instructions=(), extra_top_level_program_ids=(), jupiter_instruction_count=1):
    taker_key = Pubkey.new_unique()
    jupiter_key = Pubkey.from_string(JUPITER_V6_PROGRAM_ID)
    dex_key = Pubkey.new_unique()
    input_mint_key = Pubkey.new_unique()
    output_mint_key = Pubkey.new_unique()
    amm_key = Pubkey.new_unique()
    token_program = Pubkey.from_string(CLASSIC_TOKEN_PROGRAM_ID)
    input_ata = Pubkey.from_string(
        derive_associated_token_address(str(taker_key), str(input_mint_key))
    )
    output_ata = Pubkey.from_string(
        derive_associated_token_address(str(taker_key), str(output_mint_key))
    )
    event_authority, _ = Pubkey.find_program_address([b"__event_authority"], jupiter_key)
    route_data = (
        ROUTE_DISCRIMINATOR
        + struct.pack("<I", 1)
        + bytes([26, 100, 0, 1])
        + struct.pack("<QQHB", 1_000, 2_000, 250, 0)
    )
    jupiter_instruction = Instruction(
        jupiter_key,
        route_data,
        [
            AccountMeta(token_program, False, False),
            AccountMeta(taker_key, True, True),
            AccountMeta(input_ata, False, True),
            AccountMeta(output_ata, False, True),
            AccountMeta(jupiter_key, False, False),
            AccountMeta(output_mint_key, False, False),
            AccountMeta(jupiter_key, False, False),
            AccountMeta(event_authority, False, False),
            AccountMeta(jupiter_key, False, False),
            AccountMeta(dex_key, False, False),
            AccountMeta(amm_key, False, True),
        ],
    )
    instructions = [jupiter_instruction for _ in range(jupiter_instruction_count)]
    instructions.extend(
        Instruction(Pubkey.from_string(program_id), b"unexpected-top-level", [])
        for program_id in extra_top_level_program_ids
    )
    instructions.extend(compute_instructions)
    blockhash = Hash.new_unique()
    message = MessageV0.try_compile(taker_key, instructions, [], blockhash)
    message_bytes = to_bytes_versioned(message)
    digest = hashlib.sha256(message_bytes).hexdigest()
    account_keys = tuple(str(key) for key in message.account_keys)
    program_ids = tuple(
        account_keys[int(instruction.program_id_index)]
        for instruction in message.instructions
    )
    taker = str(taker_key)
    jupiter_program = JUPITER_V6_PROGRAM_ID
    dex_program = str(dex_key)
    input_mint = str(input_mint_key)
    output_mint = str(output_mint_key)

    route = JupiterRouteStep(
        amm_key=str(amm_key),
        label="Raydium CLMM",
        input_mint=input_mint,
        output_mint=output_mint,
        input_amount_raw=1_000,
        output_amount_raw=2_000,
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
        input_amount_raw=1_000,
        output_amount_raw=2_000,
        minimum_output_amount_raw=1_950,
        price_impact_pct=0.2,
        fee_bps=50.0,
        router="metis",
        transaction_version=0,
        last_valid_block_height=950,
        recent_blockhash=str(blockhash),
        message_bytes=message_bytes,
        serialized_transaction_bytes=b"serialized-transaction",
        route_plan=(route,),
        signature_fee_lamports=5000,
        prioritization_fee_lamports=1000,
        rent_fee_lamports=0,
    )
    validation = TransactionValidationResult(
        message_sha256=digest,
        message_size=len(message_bytes),
        fee_payer=taker,
        required_signers=(taker,),
        program_ids=program_ids,
        instruction_count=len(message.instructions),
        account_keys=account_keys,
    )
    simulation = SimulationEvidence(
        message_sha256=digest,
        recent_blockhash=str(blockhash),
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
        top_level_program_ids=program_ids,
        inner_program_ids=(dex_program,),
        inner_instruction_count=1,
    )
    gate = SimulationGateResult(
        approved=True,
        message_sha256=digest,
        provider_id="rpc-primary",
        response_id="simulation-1",
        context_slot=500,
        units_consumed=100_000,
        fee_lamports=5000,
        verified_token_account_count=0,
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
    return order, validation, simulation, gate, registry, policy


def evaluate(values=None):
    order, validation, simulation, gate, registry, policy = values or fixture()
    return evaluate_jupiter_route_programs(
        order,
        validation=validation,
        simulation=simulation,
        simulation_gate=gate,
        registry=registry,
        policy=policy,
        now_epoch_ms=1_005_000,
    )


def test_accepts_order_route_when_simulated_cpi_trace_matches_registry():
    result = evaluate()
    assert result.passed is True
    assert result.route_labels == ("Raydium CLMM",)
    assert len(result.dex_program_ids) == 1
    assert result.compute_budget.instruction_count == 0
    assert result.compute_budget.effective_compute_unit_limit == 200_000
    assert result.jupiter_route.instruction_name == "route"
    assert result.jupiter_route.fully_decoded is False
    assert result.jupiter_route.remaining_account_count == 2


def test_route_gate_returns_decoded_compute_budget_values():
    values = fixture(compute_instructions=(compute_limit(250_000), compute_price(1_000)))
    result = evaluate(values)
    assert result.compute_budget.compute_unit_limit == 250_000
    assert result.compute_budget.compute_unit_price_micro_lamports == 1_000
    assert result.compute_budget.estimated_priority_fee_lamports == 250


def test_route_gate_rejects_compute_budget_over_policy():
    values = fixture(compute_instructions=(compute_limit(300_001),))
    with pytest.raises(JupiterProgramTraceError, match="Compute Budget validation failed"):
        evaluate(values)


def test_route_gate_rejects_duplicate_compute_budget_instructions():
    values = fixture(compute_instructions=(compute_price(1), compute_price(2)))
    with pytest.raises(JupiterProgramTraceError, match="Compute Budget validation failed"):
        evaluate(values)


def test_program_trace_policy_rejects_routers_without_semantic_decoders():
    _, _, _, _, _, policy = fixture()
    assert policy.allowed_routers == ("metis",)
    for router in ("dflow", "okx", "jupiterz"):
        with pytest.raises(JupiterProgramTraceError, match="only Metis"):
            dataclasses.replace(policy, allowed_routers=(router,))


def test_rejects_top_level_system_token_or_associated_token_instructions():
    for extra_program in (
        "11111111111111111111111111111111",
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    ):
        with pytest.raises(JupiterProgramTraceError, match="top-level message invokes"):
            evaluate(fixture(extra_top_level_program_ids=(extra_program,)))


def test_rejects_system_and_associated_token_programs_in_inner_cpi_trace():
    for extra_program in (
        "11111111111111111111111111111111",
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        COMPUTE_BUDGET_PROGRAM_ID,
    ):
        order, validation, simulation, gate, registry, policy = fixture()
        simulation = dataclasses.replace(
            simulation,
            inner_program_ids=(extra_program,),
            inner_instruction_count=1,
        )
        with pytest.raises(JupiterProgramTraceError, match="inner CPI trace invokes"):
            evaluate((order, validation, simulation, gate, registry, policy))


def test_requires_exactly_one_jupiter_aggregator_instruction():
    with pytest.raises(JupiterProgramTraceError, match="Jupiter V6 instruction validation failed"):
        evaluate(fixture(jupiter_instruction_count=2))


def test_rejects_system_program_in_standard_allowlist_configuration():
    _, _, _, _, _, policy = fixture()
    with pytest.raises(JupiterProgramTraceError, match="standard programs are limited"):
        dataclasses.replace(
            policy,
            standard_program_ids=policy.standard_program_ids + ("11111111111111111111111111111111",),
        )


def test_rejects_unapproved_route_label():
    order, validation, simulation, gate, registry, policy = fixture()
    policy = dataclasses.replace(policy, allowed_dex_labels=("Orca Whirlpool",))
    with pytest.raises(JupiterProgramTraceError, match="quoted route label"):
        evaluate((order, validation, simulation, gate, registry, policy))


def test_rejects_unknown_inner_cpi_program():
    order, validation, simulation, gate, registry, policy = fixture()
    simulation = dataclasses.replace(simulation, inner_program_ids=("unknown-program",))
    with pytest.raises(JupiterProgramTraceError, match="outside the DEX/token allowlist"):
        evaluate((order, validation, simulation, gate, registry, policy))


def test_rejects_route_label_not_present_in_registry():
    order, validation, simulation, gate, registry, policy = fixture()
    pairs = ((validation.program_ids[0], "Jupiter V6"),)
    registry = JupiterProgramRegistrySnapshot(
        observed_at_epoch_ms=1_000_000,
        program_labels=pairs,
        sha256=hashlib.sha256(
            json.dumps(pairs, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest(),
    )
    with pytest.raises(JupiterProgramTraceError, match="no locally pinned program ID"):
        evaluate((order, validation, simulation, gate, registry, policy))


def test_rejects_missing_approved_jupiter_aggregator_program():
    order, validation, simulation, gate, registry, policy = fixture()
    alternate = str(Pubkey.new_unique())
    pairs = tuple(sorted(registry.program_labels + ((alternate, "Jupiter V6"),)))
    registry = JupiterProgramRegistrySnapshot(
        observed_at_epoch_ms=1_000_000,
        program_labels=pairs,
        sha256=hashlib.sha256(
            json.dumps(pairs, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest(),
    )
    policy = dataclasses.replace(policy, allowed_jupiter_program_ids=(alternate,))
    with pytest.raises(JupiterProgramTraceError, match="Jupiter V6 instruction validation failed"):
        evaluate((order, validation, simulation, gate, registry, policy))


def test_rejects_empty_inner_cpi_trace():
    order, validation, simulation, gate, registry, policy = fixture()
    simulation = dataclasses.replace(simulation, inner_program_ids=(), inner_instruction_count=0)
    with pytest.raises(JupiterProgramTraceError, match="inner-CPI program trace"):
        evaluate((order, validation, simulation, gate, registry, policy))


def test_rejects_digest_mismatch():
    order, validation, simulation, gate, registry, policy = fixture()
    simulation = dataclasses.replace(simulation, message_sha256="b" * 64)
    with pytest.raises(JupiterProgramTraceError, match="digests differ"):
        evaluate((order, validation, simulation, gate, registry, policy))


def test_rejects_stale_registry_snapshot():
    order, validation, simulation, gate, registry, policy = fixture()
    registry = dataclasses.replace(registry, observed_at_epoch_ms=1)
    policy = dataclasses.replace(policy, max_registry_age_ms=1_000)
    with pytest.raises(JupiterProgramTraceError, match="stale"):
        evaluate((order, validation, simulation, gate, registry, policy))


def test_rejects_nonmatching_fee_payer_or_blockhash():
    order, validation, simulation, gate, registry, policy = fixture()
    order = dataclasses.replace(order, fee_payer="other-payer")
    with pytest.raises(JupiterProgramTraceError, match="validated fee payer"):
        evaluate((order, validation, simulation, gate, registry, policy))

    order, validation, simulation, gate, registry, policy = fixture()
    simulation = dataclasses.replace(simulation, recent_blockhash="other-blockhash")
    with pytest.raises(JupiterProgramTraceError, match="blockhash differs"):
        evaluate((order, validation, simulation, gate, registry, policy))


def test_rejects_resolved_account_key_evidence_mismatch():
    order, validation, simulation, gate, registry, policy = fixture()
    validation = dataclasses.replace(validation, account_keys=validation.account_keys[:-1])
    with pytest.raises(JupiterProgramTraceError, match="Compute Budget validation failed"):
        evaluate((order, validation, simulation, gate, registry, policy))


def test_rejects_registry_digest_tampering():
    _, _, _, _, registry, _ = fixture()
    with pytest.raises(JupiterProgramTraceError, match="digest"):
        dataclasses.replace(registry, sha256="0" * 64)
