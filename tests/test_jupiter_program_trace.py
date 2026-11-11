"""Adversarial tests for Jupiter route labels against simulated CPI program IDs."""
import dataclasses
import hashlib
import json
import pytest
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_program_trace import (
    JupiterProgramRegistrySnapshot,
    JupiterProgramTraceError,
    JupiterRouteProgramPolicy,
    evaluate_jupiter_route_programs,
)
from veltrix.simulation_gate import SimulationEvidence, SimulationGateResult, SimulatedTokenBalance
from veltrix.transaction_validator import TransactionValidationResult


def fixture():
    taker = str(Pubkey.new_unique())
    jupiter_program = str(Pubkey.new_unique())
    dex_program = str(Pubkey.new_unique())
    message = b"exact-order-message"
    digest = hashlib.sha256(message).hexdigest()
    route = JupiterRouteStep(
        amm_key="amm-pool",
        label="Raydium CLMM",
        input_mint="input-mint",
        output_mint="output-mint",
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
        input_mint="input-mint",
        output_mint="output-mint",
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
        account_keys=(taker, jupiter_program, dex_program),
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



def test_program_trace_policy_rejects_routers_without_semantic_decoders():
    _, _, _, _, _, policy = fixture()
    assert policy.allowed_routers == ("metis",)
    for router in ("dflow", "okx", "jupiterz"):
        with pytest.raises(JupiterProgramTraceError, match="only Metis"):
            dataclasses.replace(policy, allowed_routers=(router,))


def test_accepts_order_route_when_simulated_cpi_trace_matches_jupiter_registry():
    result = evaluate()
    assert result.passed is True
    assert result.route_labels == ("Raydium CLMM",)
    assert len(result.dex_program_ids) == 1


def test_rejects_top_level_system_or_token_program_instructions():
    for extra_program in (
        "11111111111111111111111111111111",
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    ):
        order, validation, simulation, gate, registry, policy = fixture()
        programs = validation.program_ids + (extra_program,)
        validation = dataclasses.replace(validation, program_ids=programs)
        simulation = dataclasses.replace(simulation, top_level_program_ids=programs)
        with pytest.raises(JupiterProgramTraceError, match="top-level message invokes"):
            evaluate((order, validation, simulation, gate, registry, policy))


def test_rejects_system_and_associated_token_programs_in_inner_cpi_trace():
    for extra_program in (
        "11111111111111111111111111111111",
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        "ComputeBudget111111111111111111111111111111",
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
    order, validation, simulation, gate, registry, policy = fixture()
    programs = validation.program_ids + validation.program_ids
    validation = dataclasses.replace(validation, program_ids=programs)
    simulation = dataclasses.replace(simulation, top_level_program_ids=programs)
    with pytest.raises(JupiterProgramTraceError, match="exactly one approved Jupiter"):
        evaluate((order, validation, simulation, gate, registry, policy))


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
    with pytest.raises(JupiterProgramTraceError, match="outside local policy"):
        evaluate((order, validation, simulation, gate, registry, policy))


def test_rejects_unknown_inner_cpi_program():
    order, validation, simulation, gate, registry, policy = fixture()
    simulation = dataclasses.replace(simulation, inner_program_ids=("unknown-program",))
    with pytest.raises(JupiterProgramTraceError, match="outside the route allowlist"):
        evaluate((order, validation, simulation, gate, registry, policy))


def test_rejects_route_label_not_present_in_registry():
    order, validation, simulation, gate, registry, policy = fixture()
    registry = JupiterProgramRegistrySnapshot(
        observed_at_epoch_ms=1_000_000,
        program_labels=((validation.program_ids[0], "Jupiter V6"),),
        sha256=hashlib.sha256(
            json.dumps(((validation.program_ids[0], "Jupiter V6"),), separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest(),
    )
    with pytest.raises(JupiterProgramTraceError, match="no locally pinned program ID"):
        evaluate((order, validation, simulation, gate, registry, policy))


def test_rejects_missing_jupiter_aggregator_program():
    order, validation, simulation, gate, registry, policy = fixture()
    validation = dataclasses.replace(validation, program_ids=("unknown-program",))
    simulation = dataclasses.replace(simulation, top_level_program_ids=("unknown-program",))
    with pytest.raises(JupiterProgramTraceError, match="does not invoke an approved Jupiter"):
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
    with pytest.raises(JupiterProgramTraceError, match="not the transaction fee payer"):
        evaluate((order, validation, simulation, gate, registry, policy))

    order, validation, simulation, gate, registry, policy = fixture()
    simulation = dataclasses.replace(simulation, recent_blockhash="other-blockhash")
    with pytest.raises(JupiterProgramTraceError, match="blockhash differs"):
        evaluate((order, validation, simulation, gate, registry, policy))


def test_rejects_inner_program_missing_from_validated_accounts():
    order, validation, simulation, gate, registry, policy = fixture()
    validation = dataclasses.replace(validation, account_keys=(validation.fee_payer, validation.program_ids[0]))
    with pytest.raises(JupiterProgramTraceError, match="absent from validated account keys"):
        evaluate((order, validation, simulation, gate, registry, policy))


def test_rejects_registry_digest_tampering():
    _, _, _, _, registry, _ = fixture()
    with pytest.raises(JupiterProgramTraceError, match="digest"):
        dataclasses.replace(registry, sha256="0" * 64)
