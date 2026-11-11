"""Adversarial tests for the composite paper-only Meteora preflight."""
import dataclasses
import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_route_semantics import JupiterRouteSemanticResult
from veltrix.jupiter_swap_effects import JupiterSwapEffectResult
from veltrix.meteora_dlmm_cpi import (
    METEORA_DLMM_PROGRAM_ID,
    DecodedMeteoraDlmmSwap,
    MeteoraDlmmCpiResult,
)
from veltrix.meteora_dlmm_pool_state import (
    MeteoraDlmmPoolStateResult,
    MeteoraLbPairState,
    SplTokenAccountState,
)
from veltrix.meteora_paper_preflight import (
    MeteoraPaperPreflightError,
    MeteoraPaperPreflightPolicy,
    evaluate_meteora_paper_preflight,
)
from veltrix.simulation_gate import (
    SimulatedInnerInstruction,
    SimulationEvidence,
    SimulationGateResult,
)
from veltrix.transaction_validator import TransactionValidationResult


def key():
    return str(Pubkey.new_unique())


def fixture():
    taker, input_mint, output_mint, jupiter, pool, reserve_x, reserve_y, oracle = [key() for _ in range(8)]
    message = b"composite-meteora-paper-message"
    digest = hashlib.sha256(message).hexdigest()
    order = JupiterOrderTransaction(
        provider_id="jupiter-swap-v2",
        request_id="paper-preflight-1",
        quote_id="quote-1",
        observed_at_epoch_ms=1_000_000,
        taker=taker,
        fee_payer=taker,
        input_mint=input_mint,
        output_mint=output_mint,
        input_amount_raw=1_000,
        output_amount_raw=2_000,
        minimum_output_amount_raw=1_950,
        price_impact_pct=0.2,
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
        parent_instruction_index=0,
        ordinal=0,
        program_id=METEORA_DLMM_PROGRAM_ID,
        account_keys=(),
        data=b"swap-cpi",
        stack_height=2,
    )
    simulation = SimulationEvidence(
        message_sha256=digest,
        recent_blockhash="blockhash",
        cluster="mainnet-beta",
        genesis_hash="genesis",
        provider_id="rpc-primary",
        response_id="sim-paper-1",
        observed_at_epoch_ms=1_001_000,
        context_slot=500,
        current_block_height=900,
        last_valid_block_height=950,
        simulation_error=None,
        units_consumed=100_000,
        fee_lamports=5_000,
        token_balances=(),
        top_level_program_ids=(jupiter,),
        inner_program_ids=(METEORA_DLMM_PROGRAM_ID,),
        inner_instruction_count=1,
        inner_instructions=(inner,),
    )
    sim_gate = SimulationGateResult(
        approved=True,
        message_sha256=digest,
        provider_id="rpc-primary",
        response_id="sim-paper-1",
        context_slot=500,
        units_consumed=100_000,
        fee_lamports=5_000,
        verified_token_account_count=2,
    )
    program_gate = JupiterRouteProgramGateResult(
        passed=True,
        message_sha256=digest,
        router="metis",
        route_labels=("Meteora DLMM",),
        dex_program_ids=(METEORA_DLMM_PROGRAM_ID,),
        top_level_program_ids=(jupiter,),
        inner_program_ids=(METEORA_DLMM_PROGRAM_ID,),
        registry_sha256="a" * 64,
    )
    route_semantics = JupiterRouteSemanticResult(
        passed=True,
        message_sha256=digest,
        jupiter_program_id=jupiter,
        instruction_index=0,
        route_steps=(),
        input_amount_raw=1_000,
        quoted_output_amount_raw=2_000,
        slippage_bps=50,
        platform_fee_bps=0,
        token_index_mints=((0, input_mint), (1, output_mint)),
    )
    effects = JupiterSwapEffectResult(
        passed=True,
        message_sha256=digest,
        input_token_account="input-ata",
        output_token_account="output-ata",
        input_delta_raw=-1_000,
        output_delta_raw=2_000,
        minimum_output_amount_raw=1_950,
        exact_output_amount_raw=2_000,
        simulation_context_slot=500,
    )
    cpi = MeteoraDlmmCpiResult(
        passed=True,
        message_sha256=digest,
        program_id=METEORA_DLMM_PROGRAM_ID,
        decoded_swaps=(
            DecodedMeteoraDlmmSwap(
                amount_in=1_000,
                minimum_amount_out=1_980,
                lb_pair=pool,
                reserve_x=reserve_x,
                reserve_y=reserve_y,
                user_token_in="input-ata",
                user_token_out="output-ata",
                bitmap_extension=METEORA_DLMM_PROGRAM_ID,
                token_x_mint=input_mint,
                token_y_mint=output_mint,
                oracle=oracle,
                user=taker,
                event_authority=key(),
            ),
        ),
        max_slippage_bps=100,
    )
    pool_state = MeteoraDlmmPoolStateResult(
        passed=True,
        message_sha256=digest,
        pool_state=MeteoraLbPairState(
            address=pool,
            token_x_mint=input_mint,
            token_y_mint=output_mint,
            reserve_x=reserve_x,
            reserve_y=reserve_y,
            oracle=oracle,
        ),
        reserve_x_state=SplTokenAccountState(reserve_x, input_mint, pool, 10_000, 11_000),
        reserve_y_state=SplTokenAccountState(reserve_y, output_mint, pool, 20_000, 18_000),
        oracle_address=oracle,
    )
    policy = MeteoraPaperPreflightPolicy()
    return order, validation, simulation, sim_gate, program_gate, route_semantics, effects, cpi, pool_state, policy


def evaluate(values=None):
    order, validation, simulation, sim_gate, program_gate, route_semantics, effects, cpi, pool_state, policy = values or fixture()
    return evaluate_meteora_paper_preflight(
        order,
        validation=validation,
        simulation=simulation,
        simulation_gate=sim_gate,
        program_gate=program_gate,
        route_semantics=route_semantics,
        swap_effects=effects,
        cpi_result=cpi,
        pool_state=pool_state,
        policy=policy,
        now_epoch_ms=1_005_000,
    )


def test_accepts_all_meteora_safety_gates_for_paper_only():
    result = evaluate()
    assert result.passed is True
    assert result.decision == "PAPER_SIMULATION_ONLY"
    assert result.paper_only is True
    assert result.live_execution_eligible is False
    assert result.execution_enabled is False
    assert result.input_amount_raw == 1_000
    assert result.output_amount_raw == 2_000


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("observed_at_epoch_ms", 1, "quote is stale"),
        ("price_impact_pct", 2.0, "price impact"),
        ("fee_bps", 101.0, "quoted fee"),
        ("last_valid_block_height", 899, "last-valid block height"),
        ("recent_blockhash", "wrong-blockhash", "recent blockhash"),
        ("router", "dflow", "single-hop Metis"),
    ],
)
def test_rejects_unsafe_order_fields(field, value, message):
    values = list(fixture())
    values[0] = dataclasses.replace(values[0], **{field: value})
    with pytest.raises(MeteoraPaperPreflightError, match=message):
        evaluate(tuple(values))


def test_rejects_digest_mismatch_and_unapproved_gate():
    values = list(fixture())
    values[7] = dataclasses.replace(values[7], message_sha256="b" * 64)
    with pytest.raises(MeteoraPaperPreflightError, match="not bound"):
        evaluate(tuple(values))

    values = list(fixture())
    values[3] = dataclasses.replace(values[3], approved=False)
    with pytest.raises(MeteoraPaperPreflightError, match="did not approve"):
        evaluate(tuple(values))


def test_rejects_compute_fee_slippage_and_platform_fee_over_policy():
    values = list(fixture())
    values[2] = dataclasses.replace(values[2], units_consumed=300_001)
    with pytest.raises(MeteoraPaperPreflightError, match="compute usage"):
        evaluate(tuple(values))

    values = list(fixture())
    values[2] = dataclasses.replace(values[2], fee_lamports=10_000_001)
    with pytest.raises(MeteoraPaperPreflightError, match="network fee"):
        evaluate(tuple(values))

    values = list(fixture())
    values[5] = dataclasses.replace(values[5], slippage_bps=101)
    with pytest.raises(MeteoraPaperPreflightError, match="slippage"):
        evaluate(tuple(values))

    values = list(fixture())
    values[5] = dataclasses.replace(values[5], platform_fee_bps=1)
    with pytest.raises(MeteoraPaperPreflightError, match="platform fee"):
        evaluate(tuple(values))


def test_rejects_mismatched_simulation_response_and_expired_blockhash():
    values = list(fixture())
    values[2] = dataclasses.replace(values[2], response_id="different-response")
    with pytest.raises(MeteoraPaperPreflightError, match="one RPC response"):
        evaluate(tuple(values))

    values = list(fixture())
    values[2] = dataclasses.replace(values[2], current_block_height=951)
    with pytest.raises(MeteoraPaperPreflightError, match="expired"):
        evaluate(tuple(values))
