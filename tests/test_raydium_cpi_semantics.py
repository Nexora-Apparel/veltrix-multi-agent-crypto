"""Adversarial tests for the narrow Raydium CLMM swap_v2 CPI gate."""
import dataclasses
import hashlib
import struct

import pytest
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_route_semantics import JupiterRouteSemanticResult
from veltrix.raydium_cpi_semantics import (
    CLASSIC_TOKEN_PROGRAM_ID,
    MEMO_PROGRAM_ID,
    RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
    RAYDIUM_SWAP_V2_DISCRIMINATOR,
    TOKEN_2022_PROGRAM_ID,
    DecodedRaydiumSwapV2,
    RaydiumCpiPolicy,
    RaydiumCpiSemanticError,
    decode_raydium_swap_v2,
    evaluate_raydium_clmm_cpis,
)
from veltrix.simulation_gate import (
    SimulatedInnerInstruction,
    SimulationEvidence,
    SimulationGateResult,
)
from veltrix.transaction_validator import TransactionValidationResult


def instruction_data(*, amount=1_000, threshold=1_900, sqrt_limit=0, is_base_input=1):
    return (
        RAYDIUM_SWAP_V2_DISCRIMINATOR
        + struct.pack("<QQ", amount, threshold)
        + int(sqrt_limit).to_bytes(16, "little")
        + bytes((is_base_input,))
    )


def fixture():
    taker = str(Pubkey.new_unique())
    input_mint = str(Pubkey.new_unique())
    output_mint = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    jupiter = str(Pubkey.new_unique())
    other_accounts = [str(Pubkey.new_unique()) for _ in range(9)]
    # Raydium SwapSingleV2 fixed accounts followed by one tick-array account.
    accounts = (
        taker,                    # payer
        other_accounts[0],        # amm_config
        pool,                     # pool_state / Jupiter ammKey
        other_accounts[1],        # input token account
        other_accounts[2],        # output token account
        other_accounts[3],        # input vault
        other_accounts[4],        # output vault
        other_accounts[5],        # observation state
        CLASSIC_TOKEN_PROGRAM_ID,
        TOKEN_2022_PROGRAM_ID,
        MEMO_PROGRAM_ID,
        input_mint,
        output_mint,
        other_accounts[6],        # tick array remaining account
    )
    message = b"unsigned-message-for-test"
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
    inner = SimulatedInnerInstruction(
        outer_instruction_index=0,
        program_id=RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
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
        inner_program_ids=(RAYDIUM_CLMM_MAINNET_PROGRAM_ID,),
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
        route_steps=(),
        input_amount_raw=1_000,
        quoted_output_amount_raw=2_000,
        slippage_bps=50,
        platform_fee_bps=0,
        token_index_mints=((0, input_mint), (1, output_mint)),
    )
    return order, validation, simulation, sim_gate, program_gate, route_semantics, RaydiumCpiPolicy()


def evaluate(values=None):
    order, validation, simulation, sim_gate, program_gate, route_semantics, policy = values or fixture()
    return evaluate_raydium_clmm_cpis(
        order,
        validation=validation,
        simulation=simulation,
        simulation_gate=sim_gate,
        program_gate=program_gate,
        route_semantics=route_semantics,
        policy=policy,
    )


def test_decodes_exact_ray_swap_v2_borsh_payload():
    decoded = decode_raydium_swap_v2(instruction_data())
    assert decoded == DecodedRaydiumSwapV2(
        amount_raw=1_000,
        other_amount_threshold_raw=1_900,
        sqrt_price_limit_x64=0,
        is_base_input=True,
    )


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "exactly 41 bytes"),
        (b"12345678" + instruction_data()[8:], "pinned swap_v2"),
        (instruction_data(amount=0), "must be positive"),
        (instruction_data(threshold=0), "must be positive"),
        (instruction_data(sqrt_limit=1), "sqrt-price limits"),
        (instruction_data(is_base_input=2), "canonical Borsh boolean"),
        (instruction_data() + b"extra", "exactly 41 bytes"),
    ],
)
def test_rejects_malformed_or_unsupported_swap_v2_payload(data, message):
    with pytest.raises(RaydiumCpiSemanticError, match=message):
        decode_raydium_swap_v2(data)


def test_accepts_quote_bound_classic_token_raydium_clmm_cpi():
    result = evaluate()
    assert result.passed is True
    assert result.cpi_count == 1
    assert result.pool_ids == (fixture()[0].route_plan[0].amm_key,)
    assert result.input_amounts_raw == (1_000,)
    assert result.minimum_output_amounts_raw == (1_900,)


@pytest.mark.parametrize(
    ("account_index", "replacement", "message"),
    [
        (0, str(Pubkey.new_unique()), "payer differs"),
        (2, str(Pubkey.new_unique()), "pool state differs"),
        (8, TOKEN_2022_PROGRAM_ID, "classic SPL Token"),
        (9, str(Pubkey.new_unique()), "token-2022 or memo"),
        (10, str(Pubkey.new_unique()), "token-2022 or memo"),
        (11, str(Pubkey.new_unique()), "vault mint accounts differ"),
        (12, str(Pubkey.new_unique()), "vault mint accounts differ"),
    ],
)
def test_rejects_unexpected_raydium_account_semantics(account_index, replacement, message):
    values = fixture()
    inner = values[2].inner_instructions[0]
    accounts = list(inner.account_keys)
    accounts[account_index] = replacement
    changed = dataclasses.replace(inner, account_keys=tuple(accounts))
    values = list(values)
    values[2] = dataclasses.replace(
        values[2],
        inner_instructions=(changed,),
    )
    values[2] = dataclasses.replace(values[2], inner_program_ids=(changed.program_id,))
    with pytest.raises(RaydiumCpiSemanticError, match=message):
        evaluate(tuple(values))


def test_rejects_wrong_pool_amount_and_exact_output_mode():
    values = fixture()
    order, validation, simulation, sim_gate, program_gate, route_semantics, policy = values
    changed = dataclasses.replace(simulation.inner_instructions[0], data=instruction_data(amount=999))
    simulation = dataclasses.replace(simulation, inner_instructions=(changed,))
    with pytest.raises(RaydiumCpiSemanticError, match="input amount differs"):
        evaluate((order, validation, simulation, sim_gate, program_gate, route_semantics, policy))

    values = fixture()
    order, validation, simulation, sim_gate, program_gate, route_semantics, policy = values
    changed = dataclasses.replace(simulation.inner_instructions[0], data=instruction_data(is_base_input=0))
    simulation = dataclasses.replace(simulation, inner_instructions=(changed,))
    with pytest.raises(RaydiumCpiSemanticError, match="exact-output"):
        evaluate((order, validation, simulation, sim_gate, program_gate, route_semantics, policy))


def test_rejects_missing_detailed_cpi_trace_and_wrong_outer_index():
    values = fixture()
    order, validation, simulation, sim_gate, program_gate, route_semantics, policy = values
    simulation = dataclasses.replace(simulation, inner_instructions=())
    with pytest.raises(RaydiumCpiSemanticError, match="missing or inconsistent"):
        evaluate((order, validation, simulation, sim_gate, program_gate, route_semantics, policy))

    values = fixture()
    order, validation, simulation, sim_gate, program_gate, route_semantics, policy = values
    changed = dataclasses.replace(simulation.inner_instructions[0], outer_instruction_index=1)
    simulation = dataclasses.replace(simulation, inner_instructions=(changed,))
    with pytest.raises(RaydiumCpiSemanticError, match="CPI count differs"):
        evaluate((order, validation, simulation, sim_gate, program_gate, route_semantics, policy))


def test_rejects_unapproved_inner_program_and_mixed_dex_route():
    values = fixture()
    order, validation, simulation, sim_gate, program_gate, route_semantics, policy = values
    changed = dataclasses.replace(simulation.inner_instructions[0], program_id=str(Pubkey.new_unique()))
    simulation = dataclasses.replace(
        simulation,
        inner_instructions=(changed,),
        inner_program_ids=(changed.program_id,),
    )
    program_gate = dataclasses.replace(program_gate, inner_program_ids=(changed.program_id,))
    with pytest.raises(RaydiumCpiSemanticError, match="unapproved inner CPI program"):
        evaluate((order, validation, simulation, sim_gate, program_gate, route_semantics, policy))

    values = fixture()
    order, validation, simulation, sim_gate, program_gate, route_semantics, policy = values
    order = dataclasses.replace(
        order,
        route_plan=(dataclasses.replace(order.route_plan[0], label="Meteora"),),
    )
    with pytest.raises(RaydiumCpiSemanticError, match="only all"):
        evaluate((order, validation, simulation, sim_gate, program_gate, route_semantics, policy))


def test_rejects_simulation_digest_or_program_trace_mismatch():
    values = fixture()
    order, validation, simulation, sim_gate, program_gate, route_semantics, policy = values
    simulation = dataclasses.replace(simulation, message_sha256="b" * 64)
    with pytest.raises(RaydiumCpiSemanticError, match="digests differ"):
        evaluate((order, validation, simulation, sim_gate, program_gate, route_semantics, policy))

    values = fixture()
    order, validation, simulation, sim_gate, program_gate, route_semantics, policy = values
    program_gate = dataclasses.replace(program_gate, inner_program_ids=("unexpected",))
    with pytest.raises(RaydiumCpiSemanticError, match="trace differs"):
        evaluate((order, validation, simulation, sim_gate, program_gate, route_semantics, policy))
