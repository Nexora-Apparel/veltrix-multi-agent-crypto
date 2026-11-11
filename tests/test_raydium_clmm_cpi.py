"""Adversarial tests for the pinned Raydium CLMM classic-token CPI gate."""
import dataclasses
import hashlib
import struct

import pytest
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_route_semantics import JupiterRouteSemanticResult, DecodedJupiterRouteStep
from veltrix.jupiter_swap_effects import (
    CLASSIC_TOKEN_PROGRAM_ID,
    JupiterSwapEffectResult,
    derive_associated_token_address,
)
from veltrix.raydium_clmm_cpi import (
    RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RAYDIUM_CLMM_SWAP_DISCRIMINATOR,
    RAYDIUM_CLMM_SWAP_V2_DISCRIMINATOR,
    DecodedRaydiumClmmSwap,
    RaydiumClmmCpiError,
    RaydiumClmmCpiPolicy,
    decode_raydium_clmm_swap,
    evaluate_raydium_clmm_cpi,
)
from veltrix.simulation_gate import (
    SimulatedInnerInstruction,
    SimulationEvidence,
    SimulationGateResult,
)
from veltrix.transaction_validator import TransactionValidationResult


def swap_data(amount=1_000, threshold=1_980, sqrt_limit=0, is_base_input=1):
    return (
        RAYDIUM_CLMM_SWAP_DISCRIMINATOR
        + struct.pack("<QQ", amount, threshold)
        + int(sqrt_limit).to_bytes(16, "little")
        + bytes((is_base_input,))
    )


def fixture():
    taker = str(Pubkey.new_unique())
    input_mint = str(Pubkey.new_unique())
    output_mint = str(Pubkey.new_unique())
    jupiter = str(Pubkey.new_unique())
    input_ata = derive_associated_token_address(taker, input_mint)
    output_ata = derive_associated_token_address(taker, output_mint)
    other_accounts = tuple(str(Pubkey.new_unique()) for _ in range(6))
    amm_config, pool, input_vault, output_vault, observation, tick_array = other_accounts
    cpi_accounts = (
        taker,
        amm_config,
        pool,
        input_ata,
        output_ata,
        input_vault,
        output_vault,
        observation,
        CLASSIC_TOKEN_PROGRAM_ID,
        tick_array,
    )
    message = b"exact-jupiter-message"
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
        minimum_output_amount_raw=1_950,
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
                label="Raydium CLMM",
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
    cpi = SimulatedInnerInstruction(
        outer_instruction_index=0,
        program_id=RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
        account_keys=cpi_accounts,
        data=swap_data(),
        stack_height=2,
    )
    simulation = SimulationEvidence(
        message_sha256=digest,
        recent_blockhash="blockhash",
        cluster="mainnet-beta",
        genesis_hash="genesis",
        provider_id="rpc-primary",
        response_id="simulation-1",
        observed_at_epoch_ms=1_001_000,
        context_slot=500,
        current_block_height=900,
        last_valid_block_height=950,
        simulation_error=None,
        units_consumed=100_000,
        fee_lamports=5_000,
        token_balances=(),
        top_level_program_ids=(jupiter,),
        inner_program_ids=(RAYDIUM_CLMM_MAINNET_PROGRAM_ID,),
        inner_instruction_count=1,
        inner_instructions=(cpi,),
    )
    simulation_gate = SimulationGateResult(
        approved=True,
        message_sha256=digest,
        provider_id="rpc-primary",
        response_id="simulation-1",
        context_slot=500,
        units_consumed=100_000,
        fee_lamports=5_000,
        verified_token_account_count=2,
    )
    program_gate = JupiterRouteProgramGateResult(
        passed=True,
        message_sha256=digest,
        router="metis",
        route_labels=("Raydium CLMM",),
        dex_program_ids=(RAYDIUM_CLMM_MAINNET_PROGRAM_ID,),
        top_level_program_ids=(jupiter,),
        inner_program_ids=(RAYDIUM_CLMM_MAINNET_PROGRAM_ID,),
        registry_sha256="a" * 64,
    )
    route_semantics = JupiterRouteSemanticResult(
        passed=True,
        message_sha256=digest,
        jupiter_program_id=jupiter,
        instruction_index=0,
        route_steps=(DecodedJupiterRouteStep(26, 100, 0, 1),),
        input_amount_raw=1_000,
        quoted_output_amount_raw=2_000,
        slippage_bps=100,
        platform_fee_bps=0,
        token_index_mints=((0, input_mint), (1, output_mint)),
    )
    swap_effects = JupiterSwapEffectResult(
        passed=True,
        message_sha256=digest,
        input_token_account=input_ata,
        output_token_account=output_ata,
        input_delta_raw=-1_000,
        output_delta_raw=2_000,
        minimum_output_amount_raw=1_950,
        exact_output_amount_raw=2_000,
        simulation_context_slot=500,
    )
    return order, validation, simulation, simulation_gate, program_gate, route_semantics, swap_effects


def evaluate(values=None, policy=None):
    order, validation, simulation, simulation_gate, program_gate, route_semantics, swap_effects = values or fixture()
    return evaluate_raydium_clmm_cpi(
        order,
        validation=validation,
        simulation=simulation,
        simulation_gate=simulation_gate,
        program_gate=program_gate,
        route_semantics=route_semantics,
        swap_effects=swap_effects,
        policy=policy or RaydiumClmmCpiPolicy(),
    )


def test_decoder_accepts_exact_classic_token_swap_payload():
    decoded = decode_raydium_clmm_swap(swap_data(sqrt_limit=123456))
    assert decoded == DecodedRaydiumClmmSwap(1_000, 1_980, 123456, True)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "missing or truncated"),
        (b"bad-data", "discriminator"),
        (RAYDIUM_CLMM_SWAP_V2_DISCRIMINATOR + bytes(33), "swap_v2"),
        (swap_data()[:-1], "payload length"),
        (swap_data(is_base_input=2), "canonical Borsh boolean"),
        (swap_data(amount=0), "must be positive"),
        (swap_data(threshold=0), "must be positive"),
    ],
)
def test_decoder_rejects_malformed_or_unsupported_swap_payloads(data, message):
    with pytest.raises(RaydiumClmmCpiError, match=message):
        decode_raydium_clmm_swap(data)


def test_accepts_single_hop_raydium_clmm_cpi_matching_jupiter_order():
    result = evaluate()
    assert result.passed is True
    assert result.amount_raw == 1_000
    assert result.minimum_output_raw == 1_980
    assert result.is_base_input is True
    assert result.stack_height == 2
    assert result.source_commit == RAYDIUM_CLMM_SOURCE_COMMIT


@pytest.mark.parametrize(
    ("threshold", "message"),
    [
        (1_979, "allows more slippage"),
        (2_001, "exceeds the quoted"),
    ],
)
def test_rejects_minimum_output_threshold_outside_quote_slippage_bounds(threshold, message):
    values = list(fixture())
    simulation = values[2]
    cpi = dataclasses.replace(simulation.inner_instructions[0], data=swap_data(threshold=threshold))
    values[2] = dataclasses.replace(simulation, inner_instructions=(cpi,))
    with pytest.raises(RaydiumClmmCpiError, match=message):
        evaluate(tuple(values))


def test_rejects_wrong_amount_or_exact_output_mode():
    values = list(fixture())
    simulation = values[2]
    cpi = dataclasses.replace(simulation.inner_instructions[0], data=swap_data(amount=999))
    values[2] = dataclasses.replace(simulation, inner_instructions=(cpi,))
    with pytest.raises(RaydiumClmmCpiError, match="input amount"):
        evaluate(tuple(values))

    values = list(fixture())
    simulation = values[2]
    cpi = dataclasses.replace(simulation.inner_instructions[0], data=swap_data(is_base_input=0))
    values[2] = dataclasses.replace(simulation, inner_instructions=(cpi,))
    with pytest.raises(RaydiumClmmCpiError, match="exact-input"):
        evaluate(tuple(values))


def test_rejects_cpi_not_directly_nested_under_jupiter_route():
    values = list(fixture())
    simulation = values[2]
    cpi = dataclasses.replace(simulation.inner_instructions[0], stack_height=3)
    values[2] = dataclasses.replace(simulation, inner_instructions=(cpi,))
    with pytest.raises(RaydiumClmmCpiError, match="direct child"):
        evaluate(tuple(values))


def test_rejects_wrong_outer_instruction_index_or_duplicate_raydium_cpi():
    values = list(fixture())
    simulation = values[2]
    cpi = dataclasses.replace(simulation.inner_instructions[0], outer_instruction_index=1)
    values[2] = dataclasses.replace(simulation, inner_instructions=(cpi,))
    with pytest.raises(RaydiumClmmCpiError, match="not nested"):
        evaluate(tuple(values))

    values = list(fixture())
    simulation = values[2]
    values[2] = dataclasses.replace(
        simulation,
        inner_program_ids=(RAYDIUM_CLMM_MAINNET_PROGRAM_ID,) * 2,
        inner_instruction_count=2,
        inner_instructions=simulation.inner_instructions * 2,
    )
    values[4] = dataclasses.replace(
        values[4],
        inner_program_ids=(RAYDIUM_CLMM_MAINNET_PROGRAM_ID,) * 2,
    )
    with pytest.raises(RaydiumClmmCpiError, match="exactly one"):
        evaluate(tuple(values))


def test_rejects_missing_detailed_cpi_evidence():
    values = list(fixture())
    values[2] = dataclasses.replace(values[2], inner_instructions=())
    with pytest.raises(RaydiumClmmCpiError, match="complete raw"):
        evaluate(tuple(values))


def test_rejects_wrong_payer_user_accounts_or_token_program():
    values = list(fixture())
    simulation = values[2]
    accounts = list(simulation.inner_instructions[0].account_keys)
    accounts[0] = str(Pubkey.new_unique())
    cpi = dataclasses.replace(simulation.inner_instructions[0], account_keys=tuple(accounts))
    values[2] = dataclasses.replace(simulation, inner_instructions=(cpi,))
    with pytest.raises(RaydiumClmmCpiError, match="payer differs"):
        evaluate(tuple(values))

    values = list(fixture())
    simulation = values[2]
    accounts = list(simulation.inner_instructions[0].account_keys)
    accounts[8] = str(Pubkey.new_unique())
    cpi = dataclasses.replace(simulation.inner_instructions[0], account_keys=tuple(accounts))
    values[2] = dataclasses.replace(simulation, inner_instructions=(cpi,))
    with pytest.raises(RaydiumClmmCpiError, match="classic SPL Token"):
        evaluate(tuple(values))


def test_rejects_unpinned_program_and_unsupported_route_combinations():
    values = list(fixture())
    values[4] = dataclasses.replace(values[4], dex_program_ids=(str(Pubkey.new_unique()),))
    with pytest.raises(RaydiumClmmCpiError, match="not present in the pinned"):
        evaluate(tuple(values))

    values = list(fixture())
    values[0] = dataclasses.replace(values[0], router="dflow")
    with pytest.raises(RaydiumClmmCpiError, match="Metis"):
        evaluate(tuple(values))


def test_policy_rejects_noncanonical_or_nonpinned_program_id():
    with pytest.raises(RaydiumClmmCpiError, match="invalid"):
        RaydiumClmmCpiPolicy(program_id="not-a-pubkey")
    with pytest.raises(RaydiumClmmCpiError, match="only the source-pinned"):
        RaydiumClmmCpiPolicy(program_id=str(Pubkey.new_unique()))


def test_rejects_cpi_pool_different_from_quoted_amm_key():
    values = list(fixture())
    values[0] = dataclasses.replace(
        values[0],
        route_plan=(dataclasses.replace(values[0].route_plan[0], amm_key=str(Pubkey.new_unique())),),
    )
    with pytest.raises(RaydiumClmmCpiError, match="quoted AMM pool key"):
        evaluate(tuple(values))


def test_rejects_duplicate_remaining_tick_array_account():
    values = list(fixture())
    simulation = values[2]
    accounts = list(simulation.inner_instructions[0].account_keys)
    accounts.append(accounts[9])
    cpi = dataclasses.replace(simulation.inner_instructions[0], account_keys=tuple(accounts))
    values[2] = dataclasses.replace(simulation, inner_instructions=(cpi,))
    with pytest.raises(RaydiumClmmCpiError, match="unexpectedly alias"):
        evaluate(tuple(values))
