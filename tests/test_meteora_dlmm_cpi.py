"""Adversarial tests for the pinned Meteora DLMM CPI decoder."""
import dataclasses
import hashlib
import struct

import pytest
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_route_semantics import JupiterRouteSemanticResult
from veltrix.jupiter_swap_effects import JupiterSwapEffectResult
from veltrix.meteora_dlmm_cpi import (
    CLASSIC_TOKEN_PROGRAM_ID,
    METEORA_DLMM_PROGRAM_ID,
    METEORA_DLMM_SWAP_DISCRIMINATOR,
    DecodedMeteoraDlmmSwap,
    MeteoraDlmmCpiError,
    MeteoraDlmmSwapPolicy,
    decode_meteora_dlmm_swap,
    evaluate_meteora_dlmm_cpi,
)
from veltrix.simulation_gate import (
    SimulatedInnerInstruction,
    SimulationEvidence,
    SimulationGateResult,
)
from veltrix.transaction_validator import TransactionValidationResult


def make_accounts(order):
    event_authority = str(Pubkey.find_program_address(
        [b"__event_authority"], Pubkey.from_string(METEORA_DLMM_PROGRAM_ID)
    )[0])
    return (
        order.route_plan[0].amm_key,
        METEORA_DLMM_PROGRAM_ID,  # omitted optional bitmap extension
        str(Pubkey.new_unique()),  # reserve X
        str(Pubkey.new_unique()),  # reserve Y
        str(Pubkey.new_unique()),  # user token in
        str(Pubkey.new_unique()),  # user token out
        order.input_mint,
        order.output_mint,
        str(Pubkey.new_unique()),  # oracle
        METEORA_DLMM_PROGRAM_ID,  # omitted optional host fee account
        order.taker,
        CLASSIC_TOKEN_PROGRAM_ID,
        CLASSIC_TOKEN_PROGRAM_ID,
        event_authority,
        METEORA_DLMM_PROGRAM_ID,
    )


def swap_data(*, amount_in=1_000, minimum_out=1_980, discriminator=METEORA_DLMM_SWAP_DISCRIMINATOR):
    return discriminator + struct.pack("<QQ", amount_in, minimum_out)


def fixture():
    taker = str(Pubkey.new_unique())
    input_mint = str(Pubkey.new_unique())
    output_mint = str(Pubkey.new_unique())
    jupiter = str(Pubkey.new_unique())
    pool = str(Pubkey.new_unique())
    message = b"meteora-jupiter-order"
    digest = hashlib.sha256(message).hexdigest()
    step = JupiterRouteStep(
        amm_key=pool,
        label="Meteora DLMM",
        input_mint=input_mint,
        output_mint=output_mint,
        input_amount_raw=1_000,
        output_amount_raw=2_000,
        percent=100.0,
        bps=10_000,
    )
    order = JupiterOrderTransaction(
        provider_id="jupiter-swap-v2",
        request_id="order-meteora",
        quote_id="quote-meteora",
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
        route_plan=(step,),
        signature_fee_lamports=5_000,
        prioritization_fee_lamports=0,
        rent_fee_lamports=0,
    )
    accounts = make_accounts(order)
    inner = SimulatedInnerInstruction(
        parent_instruction_index=0,
        ordinal=0,
        program_id=METEORA_DLMM_PROGRAM_ID,
        account_keys=accounts,
        data=swap_data(),
        stack_height=2,
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
    simulation = SimulationEvidence(
        message_sha256=digest,
        recent_blockhash="blockhash",
        cluster="mainnet-beta",
        genesis_hash="genesis",
        provider_id="rpc-primary",
        response_id="sim-meteora",
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
        response_id="sim-meteora",
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
    return order, validation, simulation, sim_gate, program_gate, route_semantics, effects


def evaluate(values=None):
    order, validation, simulation, sim_gate, program_gate, route_semantics, effects = values or fixture()
    return evaluate_meteora_dlmm_cpi(
        order,
        validation=validation,
        simulation=simulation,
        simulation_gate=sim_gate,
        program_gate=program_gate,
        route_semantics=route_semantics,
        swap_effects=effects,
        policy=MeteoraDlmmSwapPolicy(),
    )


def test_decodes_pinned_meteora_dlmm_swap_instruction():
    order, *_ = fixture()
    decoded = decode_meteora_dlmm_swap(
        swap_data(),
        make_accounts(order),
        policy=MeteoraDlmmSwapPolicy(),
    )
    assert decoded.amount_in == 1_000
    assert decoded.minimum_amount_out == 1_980
    assert decoded.lb_pair == order.route_plan[0].amm_key
    assert decoded.user == order.taker
    assert decoded.token_x_mint == order.input_mint
    assert decoded.token_y_mint == order.output_mint


@pytest.mark.parametrize(
    ("data", "accounts", "message"),
    [
        (b"", None, "exactly 24 bytes"),
        (swap_data(discriminator=b"12345678"), None, "not the pinned"),
        (swap_data(amount_in=0), None, "must be positive"),
        (swap_data(minimum_out=0), None, "must be positive"),
        (swap_data(), (), "exactly 15 IDL accounts"),
    ],
)
def test_rejects_invalid_meteora_swap_payload_or_account_count(data, accounts, message):
    order, *_ = fixture()
    with pytest.raises(MeteoraDlmmCpiError, match=message):
        decode_meteora_dlmm_swap(
            data,
            accounts if accounts is not None else make_accounts(order),
            policy=MeteoraDlmmSwapPolicy(),
        )


def test_accepts_meteora_cpi_matching_jupiter_quote_and_simulation():
    result = evaluate()
    assert result.passed is True
    assert result.program_id == METEORA_DLMM_PROGRAM_ID
    assert len(result.decoded_swaps) == 1
    assert result.decoded_swaps[0].minimum_amount_out == 1_980


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("amount_in", 999, "input amount differs"),
        ("minimum_amount_out", 1_900, "more slippage"),
        ("minimum_amount_out", 2_001, "exceeds the quoted"),
    ],
)
def test_rejects_cpi_amounts_outside_quote_or_slippage_policy(field, value, message):
    order, validation, simulation, sim_gate, program_gate, route_semantics, effects = fixture()
    inner = simulation.inner_instructions[0]
    kwargs = {"amount_in": 1_000, "minimum_out": 1_980}
    kwargs["amount_in" if field == "amount_in" else "minimum_out"] = value
    changed = dataclasses.replace(inner, data=swap_data(**kwargs))
    simulation = dataclasses.replace(simulation, inner_instructions=(changed,))
    with pytest.raises(MeteoraDlmmCpiError, match=message):
        evaluate((order, validation, simulation, sim_gate, program_gate, route_semantics, effects))


def test_rejects_unrecognized_meteora_cpi_and_missing_full_inner_evidence():
    order, validation, simulation, sim_gate, program_gate, route_semantics, effects = fixture()
    changed = dataclasses.replace(simulation.inner_instructions[0], data=b"unknown!" + bytes(16))
    simulation = dataclasses.replace(simulation, inner_instructions=(changed,))
    with pytest.raises(MeteoraDlmmCpiError, match="not the pinned"):
        evaluate((order, validation, simulation, sim_gate, program_gate, route_semantics, effects))

    order, validation, simulation, sim_gate, program_gate, route_semantics, effects = fixture()
    simulation = dataclasses.replace(simulation, inner_instructions=())
    with pytest.raises(MeteoraDlmmCpiError, match="complete simulated inner instruction"):
        evaluate((order, validation, simulation, sim_gate, program_gate, route_semantics, effects))


def test_rejects_pool_authority_or_mint_pair_mismatch():
    order, validation, simulation, sim_gate, program_gate, route_semantics, effects = fixture()
    accounts = list(simulation.inner_instructions[0].account_keys)
    accounts[0] = str(Pubkey.new_unique())
    inner = dataclasses.replace(simulation.inner_instructions[0], account_keys=tuple(accounts))
    simulation = dataclasses.replace(simulation, inner_instructions=(inner,))
    with pytest.raises(MeteoraDlmmCpiError, match="pool is absent"):
        evaluate((order, validation, simulation, sim_gate, program_gate, route_semantics, effects))

    order, validation, simulation, sim_gate, program_gate, route_semantics, effects = fixture()
    accounts = list(simulation.inner_instructions[0].account_keys)
    accounts[10] = str(Pubkey.new_unique())
    inner = dataclasses.replace(simulation.inner_instructions[0], account_keys=tuple(accounts))
    simulation = dataclasses.replace(simulation, inner_instructions=(inner,))
    with pytest.raises(MeteoraDlmmCpiError, match="authority differs"):
        evaluate((order, validation, simulation, sim_gate, program_gate, route_semantics, effects))


def test_rejects_mixed_dex_route_and_wrong_cpi_parent_instruction():
    order, validation, simulation, sim_gate, program_gate, route_semantics, effects = fixture()
    order = dataclasses.replace(
        order,
        route_plan=(dataclasses.replace(order.route_plan[0], label="Raydium CLMM"),),
    )
    with pytest.raises(MeteoraDlmmCpiError, match="mixed or non-Meteora"):
        evaluate((order, validation, simulation, sim_gate, program_gate, route_semantics, effects))

    order, validation, simulation, sim_gate, program_gate, route_semantics, effects = fixture()
    inner = dataclasses.replace(simulation.inner_instructions[0], parent_instruction_index=1)
    simulation = dataclasses.replace(simulation, inner_instructions=(inner,))
    with pytest.raises(MeteoraDlmmCpiError, match="not nested"):
        evaluate((order, validation, simulation, sim_gate, program_gate, route_semantics, effects))



def test_rejects_bitmap_extension_when_policy_disables_it():
    order, *_ = fixture()
    accounts = list(make_accounts(order))
    accounts[1] = str(Pubkey.new_unique())
    with pytest.raises(MeteoraDlmmCpiError, match="bitmap extension"):
        decode_meteora_dlmm_swap(
            swap_data(),
            tuple(accounts),
            policy=MeteoraDlmmSwapPolicy(allow_bitmap_extension=False),
        )
