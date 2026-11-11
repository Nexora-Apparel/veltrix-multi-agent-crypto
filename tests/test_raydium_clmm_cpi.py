"""Adversarial tests for the source-pinned Raydium CLMM CPI gate."""
import dataclasses
import hashlib
import struct

import pytest
from solders.pubkey import Pubkey

from veltrix.compute_budget import ComputeBudgetResult
from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_swap_effects import CLASSIC_TOKEN_PROGRAM_ID, JupiterSwapEffectResult, derive_associated_token_address
from veltrix.jupiter_v6_route import DecodedJupiterRouteStep, JupiterV6RouteInspection
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
from veltrix.simulation_gate import SimulatedInnerInstruction, SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult


def swap_data(amount=1_000, threshold=1_980, sqrt_limit=1, is_base_input=1):
    return RAYDIUM_CLMM_SWAP_DISCRIMINATOR + struct.pack("<QQ", amount, threshold) + int(sqrt_limit).to_bytes(16, "little") + bytes((is_base_input,))


def fixture():
    taker = str(Pubkey.new_unique())
    input_mint = str(Pubkey.new_unique())
    output_mint = str(Pubkey.new_unique())
    jupiter = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
    input_ata = derive_associated_token_address(taker, input_mint)
    output_ata = derive_associated_token_address(taker, output_mint)
    amm_config, pool, input_vault, output_vault, observation, tick_array = tuple(str(Pubkey.new_unique()) for _ in range(6))
    cpi_accounts = (taker, amm_config, pool, input_ata, output_ata, input_vault, output_vault, observation, CLASSIC_TOKEN_PROGRAM_ID, tick_array)
    message = b"exact-jupiter-message"
    digest = hashlib.sha256(message).hexdigest()
    order = JupiterOrderTransaction(
        provider_id="jupiter-swap-v2", request_id="order-1", quote_id="quote-1",
        observed_at_epoch_ms=1_000_000, taker=taker, fee_payer=taker,
        input_mint=input_mint, output_mint=output_mint, input_amount_raw=1_000,
        output_amount_raw=2_000, minimum_output_amount_raw=1_950,
        price_impact_pct=0.1, fee_bps=0.0, router="metis", transaction_version=0,
        last_valid_block_height=950, recent_blockhash="blockhash",
        message_bytes=message, serialized_transaction_bytes=b"unsigned",
        route_plan=(JupiterRouteStep(pool, "Raydium CLMM", input_mint, output_mint, 1_000, 2_000, 100.0, 10_000),),
        signature_fee_lamports=5_000, prioritization_fee_lamports=0, rent_fee_lamports=0,
    )
    validation = TransactionValidationResult(digest, len(message), taker, (taker,), (jupiter,), 1, (taker, jupiter))
    route_inspection = JupiterV6RouteInspection(
        message_sha256=digest, instruction_name="route", program_id=jupiter, instruction_index=0,
        shared_authority_id=None,
        route_steps=(DecodedJupiterRouteStep(26, "Raydium CLMM", 100, 0, 1, pool),),
        input_amount_raw=1_000, quoted_output_amount_raw=2_000, slippage_bps=250,
        platform_fee_bps=0, remaining_account_count=1, fully_decoded=True, blocking_reasons=(),
    )
    cpi = SimulatedInnerInstruction(0, RAYDIUM_CLMM_MAINNET_PROGRAM_ID, cpi_accounts, swap_data(), 2)
    simulation = SimulationEvidence(
        digest, "blockhash", "mainnet-beta", "genesis", "rpc-primary", "simulation-1",
        1_001_000, 500, 900, 950, None, 100_000, 5_000, (),
        top_level_program_ids=(jupiter,), inner_program_ids=(RAYDIUM_CLMM_MAINNET_PROGRAM_ID,),
        inner_instruction_count=1, inner_instructions=(cpi,),
    )
    simulation_gate = SimulationGateResult(True, digest, "rpc-primary", "simulation-1", 500, 100_000, 5_000, 2)
    program_gate = JupiterRouteProgramGateResult(
        True, digest, "metis", ("Raydium CLMM",), (RAYDIUM_CLMM_MAINNET_PROGRAM_ID,),
        (jupiter,), (RAYDIUM_CLMM_MAINNET_PROGRAM_ID,), "a" * 64,
        ComputeBudgetResult(None, None, 300_000, 0, 0), route_inspection,
    )
    effects = JupiterSwapEffectResult(
        True, digest, input_ata, output_ata, -1_000, 2_000, 1_950, 2_000, 500,
    )
    return order, validation, simulation, simulation_gate, program_gate, route_inspection, effects


def evaluate(values=None, policy=None):
    order, validation, simulation, simulation_gate, program_gate, route_inspection, effects = values or fixture()
    return evaluate_raydium_clmm_cpi(
        order, validation=validation, simulation=simulation, simulation_gate=simulation_gate,
        program_gate=program_gate, route_inspection=route_inspection, swap_effects=effects,
        policy=policy or RaydiumClmmCpiPolicy(),
    )


def test_decoder_accepts_exact_classic_token_swap_payload():
    assert decode_raydium_clmm_swap(swap_data(sqrt_limit=123456)) == DecodedRaydiumClmmSwap(1_000, 1_980, 123456, True)


@pytest.mark.parametrize("data,match", [
    (b"", "missing or truncated"),
    (b"bad-data", "discriminator"),
    (RAYDIUM_CLMM_SWAP_V2_DISCRIMINATOR + bytes(33), "swap_v2"),
    (swap_data()[:-1], "payload length"),
    (swap_data(is_base_input=2), "canonical Borsh boolean"),
    (swap_data(amount=0), "must be positive"),
    (swap_data(threshold=0), "must be positive"),
    (swap_data(sqrt_limit=0), "sqrt-price limit must be non-zero"),
])
def test_decoder_rejects_malformed_or_unsupported_swap_payloads(data, match):
    with pytest.raises(RaydiumClmmCpiError, match=match):
        decode_raydium_clmm_swap(data)


def test_accepts_single_hop_raydium_clmm_cpi_matching_jupiter_order():
    result = evaluate()
    assert result.passed is True
    assert result.amount_raw == 1_000
    assert result.minimum_output_raw == 1_980
    assert result.stack_height == 2
    assert result.source_commit == RAYDIUM_CLMM_SOURCE_COMMIT
    assert result.pool_state == fixture()[0].route_plan[0].amm_key


def test_rejects_wrong_pool_or_outer_instruction_binding():
    values = list(fixture())
    values[0] = dataclasses.replace(values[0], route_plan=(dataclasses.replace(values[0].route_plan[0], amm_key=str(Pubkey.new_unique())),))
    with pytest.raises(RaydiumClmmCpiError, match="quoted Raydium CLMM pool"):
        evaluate(tuple(values))
    values = list(fixture())
    values[2] = dataclasses.replace(values[2], inner_instructions=(dataclasses.replace(values[2].inner_instructions[0], outer_instruction_index=2),))
    with pytest.raises(RaydiumClmmCpiError, match="not nested"):
        evaluate(tuple(values))


def test_rejects_cpi_not_directly_nested_under_jupiter_route():
    values = list(fixture())
    values[2] = dataclasses.replace(values[2], inner_instructions=(dataclasses.replace(values[2].inner_instructions[0], stack_height=3),))
    with pytest.raises(RaydiumClmmCpiError, match="direct child"):
        evaluate(tuple(values))


def test_rejects_duplicate_or_missing_raydium_cpi_records():
    values = list(fixture())
    values[2] = dataclasses.replace(values[2], inner_instructions=())
    with pytest.raises(RaydiumClmmCpiError, match="complete raw"):
        evaluate(tuple(values))
    values = list(fixture())
    cpi = values[2].inner_instructions[0]
    values[2] = dataclasses.replace(values[2], inner_program_ids=(cpi.program_id, cpi.program_id), inner_instruction_count=2, inner_instructions=(cpi, dataclasses.replace(cpi, outer_instruction_index=0)))
    values[4] = dataclasses.replace(values[4], inner_program_ids=(cpi.program_id, cpi.program_id))
    with pytest.raises(RaydiumClmmCpiError, match="exactly one"):
        evaluate(tuple(values))


def test_rejects_wrong_payer_user_accounts_token_program_or_duplicate_tick_arrays():
    values = list(fixture())
    accounts = list(values[2].inner_instructions[0].account_keys)
    accounts[0] = str(Pubkey.new_unique())
    values[2] = dataclasses.replace(values[2], inner_instructions=(dataclasses.replace(values[2].inner_instructions[0], account_keys=tuple(accounts)),))
    with pytest.raises(RaydiumClmmCpiError, match="payer differs"):
        evaluate(tuple(values))
    values = list(fixture())
    accounts = list(values[2].inner_instructions[0].account_keys)
    accounts[8] = str(Pubkey.new_unique())
    values[2] = dataclasses.replace(values[2], inner_instructions=(dataclasses.replace(values[2].inner_instructions[0], account_keys=tuple(accounts)),))
    with pytest.raises(RaydiumClmmCpiError, match="classic SPL Token"):
        evaluate(tuple(values))
    values = list(fixture())
    accounts = list(values[2].inner_instructions[0].account_keys)
    accounts.append(accounts[9])
    values[2] = dataclasses.replace(values[2], inner_instructions=(dataclasses.replace(values[2].inner_instructions[0], account_keys=tuple(accounts)),))
    with pytest.raises(RaydiumClmmCpiError, match="alias"):
        evaluate(tuple(values))


@pytest.mark.parametrize("threshold,match", [
    (1_979, "below Jupiter's order threshold"),
    (2_001, "exceeds the quoted"),
])
def test_rejects_minimum_output_threshold_outside_quote(threshold, match):
    values = list(fixture())
    cpi = dataclasses.replace(values[2].inner_instructions[0], data=swap_data(threshold=threshold))
    values[2] = dataclasses.replace(values[2], inner_instructions=(cpi,))
    with pytest.raises(RaydiumClmmCpiError, match=match):
        evaluate(tuple(values))


def test_rejects_more_slippage_than_local_policy():
    values = list(fixture())
    cpi = dataclasses.replace(values[2].inner_instructions[0], data=swap_data(threshold=1_950))
    values[2] = dataclasses.replace(values[2], inner_instructions=(cpi,))
    with pytest.raises(RaydiumClmmCpiError, match="allows more slippage"):
        evaluate(tuple(values), RaydiumClmmCpiPolicy(max_slippage_bps=100))


def test_rejects_exact_output_mode_or_amount_mismatch():
    values = list(fixture())
    values[2] = dataclasses.replace(values[2], inner_instructions=(dataclasses.replace(values[2].inner_instructions[0], data=swap_data(amount=999)),))
    with pytest.raises(RaydiumClmmCpiError, match="input amount"):
        evaluate(tuple(values))
    values = list(fixture())
    values[2] = dataclasses.replace(values[2], inner_instructions=(dataclasses.replace(values[2].inner_instructions[0], data=swap_data(is_base_input=0)),))
    with pytest.raises(RaydiumClmmCpiError, match="exact-input"):
        evaluate(tuple(values))


def test_rejects_mismatched_jupiter_inspection_and_unapproved_dex_program():
    values = list(fixture())
    values[5] = dataclasses.replace(values[5], instruction_index=1)
    with pytest.raises(RaydiumClmmCpiError, match="different route inspection"):
        evaluate(tuple(values))
    values = list(fixture())
    values[4] = dataclasses.replace(values[4], dex_program_ids=(str(Pubkey.new_unique()),))
    with pytest.raises(RaydiumClmmCpiError, match="pinned DEX trace"):
        evaluate(tuple(values))


def test_policy_rejects_unpinned_program_id():
    with pytest.raises(RaydiumClmmCpiError, match="source-pinned"):
        RaydiumClmmCpiPolicy(program_id=str(Pubkey.new_unique()))
