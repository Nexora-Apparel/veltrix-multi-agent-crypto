"""Adversarial tests for Raydium AMM v4 SwapBaseInV2 CPI semantics."""
import dataclasses
import hashlib
import struct

import pytest
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_route_semantics import DecodedJupiterRouteStep, JupiterRouteSemanticResult
from veltrix.jupiter_swap_effects import derive_associated_token_address
from veltrix.raydium_amm_v4_cpi_semantics import (
    CLASSIC_TOKEN_PROGRAM_ID,
    RAYDIUM_AMM_AUTHORITY_SEED,
    RAYDIUM_AMM_SWAP_BASE_IN_V2_TAG,
    RAYDIUM_AMM_V4_PROGRAM_ID,
    RaydiumAmmV4CpiPolicy,
    RaydiumAmmV4CpiSemanticError,
    decode_raydium_amm_v4_swap_base_in_v2,
    evaluate_raydium_amm_v4_cpis,
)
from veltrix.simulation_gate import SimulatedInnerInstruction, SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult


def instruction_data(*, amount=1_000, minimum_out=1_900):
    return bytes((RAYDIUM_AMM_SWAP_BASE_IN_V2_TAG,)) + struct.pack("<QQ", amount, minimum_out)


def fixture():
    taker = str(Pubkey.new_unique())
    input_mint = str(Pubkey.new_unique())
    output_mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    jupiter = str(Pubkey.new_unique())
    nonce = 254
    authority = Pubkey.create_program_address(
        [RAYDIUM_AMM_AUTHORITY_SEED, bytes((nonce,))],
        Pubkey.from_string(RAYDIUM_AMM_V4_PROGRAM_ID),
    )
    accounts = (
        CLASSIC_TOKEN_PROGRAM_ID,
        pool,
        str(authority),
        str(Pubkey.new_unique()),                                  # coin vault
        str(Pubkey.new_unique()),                                  # PC vault
        derive_associated_token_address(taker, input_mint),        # user source
        derive_associated_token_address(taker, output_mint),       # user destination
        taker,                                                     # signer
    )
    message = b"raydium-amm-v4-order-message"
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
                label="Raydium",
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
        program_id=RAYDIUM_AMM_V4_PROGRAM_ID,
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
        inner_program_ids=(RAYDIUM_AMM_V4_PROGRAM_ID,),
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
        route_labels=("Raydium",),
        dex_program_ids=(RAYDIUM_AMM_V4_PROGRAM_ID,),
        top_level_program_ids=(jupiter,),
        inner_program_ids=(RAYDIUM_AMM_V4_PROGRAM_ID,),
        registry_sha256="a" * 64,
    )
    route_semantics = JupiterRouteSemanticResult(
        passed=True,
        message_sha256=digest,
        jupiter_program_id=jupiter,
        instruction_index=0,
        route_steps=(DecodedJupiterRouteStep(7, 100, 0, 1),),
        input_amount_raw=1_000,
        quoted_output_amount_raw=2_000,
        slippage_bps=50,
        platform_fee_bps=0,
        token_index_mints=((0, input_mint), (1, output_mint)),
    )
    return order, validation, simulation, sim_gate, program_gate, route_semantics, RaydiumAmmV4CpiPolicy()


def evaluate(values=None):
    order, validation, simulation, sim_gate, program_gate, route_semantics, policy = values or fixture()
    return evaluate_raydium_amm_v4_cpis(
        order,
        validation=validation,
        simulation=simulation,
        simulation_gate=sim_gate,
        program_gate=program_gate,
        route_semantics=route_semantics,
        policy=policy,
    )


def test_decodes_swap_base_in_v2_instruction_data():
    decoded = decode_raydium_amm_v4_swap_base_in_v2(instruction_data())
    assert decoded.amount_in_raw == 1_000
    assert decoded.minimum_amount_out_raw == 1_900


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "exactly 17 bytes"),
        (b"\x09" + instruction_data()[1:], "pinned SwapBaseInV2"),
        (instruction_data(amount=0), "must be positive"),
        (instruction_data(minimum_out=0), "must be positive"),
        (instruction_data() + b"extra", "exactly 17 bytes"),
    ],
)
def test_rejects_malformed_or_unsupported_instruction_data(data, message):
    with pytest.raises(RaydiumAmmV4CpiSemanticError, match=message):
        decode_raydium_amm_v4_swap_base_in_v2(data)


def test_accepts_quote_bound_raydium_amm_v4_v2_cpi():
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
        (0, str(Pubkey.new_unique()), "classic SPL Token"),
        (1, str(Pubkey.new_unique()), "pool account differs"),
        (2, str(Pubkey.new_unique()), "authority PDA is invalid"),
        (3, str(Pubkey.new_unique()), "coin and PC vaults unexpectedly alias"),
        (5, str(Pubkey.new_unique()), "input token account is not the taker's"),
        (6, str(Pubkey.new_unique()), "output token account is not the taker's"),
        (7, str(Pubkey.new_unique()), "signer differs"),
    ],
)
def test_rejects_mismatched_raydium_amm_v4_accounts(account_index, replacement, message):
    values = list(fixture())
    inner = values[2].inner_instructions[0]
    accounts = list(inner.account_keys)
    if account_index == 3:
        accounts[3] = accounts[4]
    else:
        accounts[account_index] = replacement
    values[2] = dataclasses.replace(
        values[2],
        inner_instructions=(dataclasses.replace(inner, account_keys=tuple(accounts)),),
    )
    with pytest.raises(RaydiumAmmV4CpiSemanticError, match=message):
        evaluate(tuple(values))


def test_rejects_wrong_amount_threshold_depth_and_unknown_cpi():
    values = list(fixture())
    inner = dataclasses.replace(values[2].inner_instructions[0], data=instruction_data(amount=999))
    values[2] = dataclasses.replace(values[2], inner_instructions=(inner,))
    with pytest.raises(RaydiumAmmV4CpiSemanticError, match="input amount differs"):
        evaluate(tuple(values))

    values = list(fixture())
    inner = dataclasses.replace(values[2].inner_instructions[0], data=instruction_data(minimum_out=2_001))
    values[2] = dataclasses.replace(values[2], inner_instructions=(inner,))
    with pytest.raises(RaydiumAmmV4CpiSemanticError, match="threshold exceeds"):
        evaluate(tuple(values))

    values = list(fixture())
    inner = dataclasses.replace(values[2].inner_instructions[0], stack_height=3)
    values[2] = dataclasses.replace(values[2], inner_instructions=(inner,))
    with pytest.raises(RaydiumAmmV4CpiSemanticError, match="depth-2"):
        evaluate(tuple(values))

    values = list(fixture())
    inner = dataclasses.replace(values[2].inner_instructions[0], program_id=str(Pubkey.new_unique()))
    values[2] = dataclasses.replace(values[2], inner_instructions=(inner,), inner_program_ids=(inner.program_id,))
    gate = dataclasses.replace(values[4], inner_program_ids=(inner.program_id,))
    with pytest.raises(RaydiumAmmV4CpiSemanticError, match="unapproved inner CPI program"):
        evaluate((values[0], values[1], values[2], values[3], gate, values[5], values[6]))


def test_rejects_mixed_dex_and_stale_or_mismatched_evidence():
    values = list(fixture())
    order = dataclasses.replace(values[0], route_plan=(dataclasses.replace(values[0].route_plan[0], label="Meteora DLMM"),))
    gate = dataclasses.replace(values[4], route_labels=("Meteora DLMM",))
    with pytest.raises(RaydiumAmmV4CpiSemanticError, match="only all-Raydium"):
        evaluate((order, values[1], values[2], values[3], gate, values[5], values[6]))

    values = list(fixture())
    simulation = dataclasses.replace(values[2], message_sha256="b" * 64)
    with pytest.raises(RaydiumAmmV4CpiSemanticError, match="digests differ"):
        evaluate((values[0], values[1], simulation, values[3], values[4], values[5], values[6]))
