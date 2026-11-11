"""Adversarial tests for Jupiter v6 route instruction semantics."""
import dataclasses
import hashlib
import struct

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_route_semantics import (
    JUPITER_ROUTE_DISCRIMINATOR,
    JupiterRouteSemanticError,
    JupiterRouteSemanticPolicy,
    _bind_route_mints,
    decode_jupiter_route_instruction_data,
    evaluate_jupiter_route_semantics,
)
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.simulation_gate import SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult


def route_data(*, variant=26, percent=100, input_index=0, output_index=1, amount_in=1_000, amount_out=2_000, slippage=50, platform_fee=0):
    return (
        JUPITER_ROUTE_DISCRIMINATOR
        + struct.pack("<I", 1)
        + bytes((variant, percent, input_index, output_index))
        + struct.pack("<QQHB", amount_in, amount_out, slippage, platform_fee)
    )


def fixture():
    taker = str(Pubkey.new_unique())
    input_mint = str(Pubkey.new_unique())
    output_mint = str(Pubkey.new_unique())
    jupiter = str(Pubkey.new_unique())
    token_program = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    sentinel = Pubkey.from_string(jupiter)
    source = Pubkey.from_string(__import__("veltrix.jupiter_swap_effects", fromlist=["derive_associated_token_address"]).derive_associated_token_address(taker, input_mint))
    destination = Pubkey.from_string(__import__("veltrix.jupiter_swap_effects", fromlist=["derive_associated_token_address"]).derive_associated_token_address(taker, output_mint))
    route_ix = Instruction(
        Pubkey.from_string(jupiter),
        route_data(),
        [
            AccountMeta(token_program, False, False),
            AccountMeta(Pubkey.from_string(taker), True, False),
            AccountMeta(source, False, True),
            AccountMeta(destination, False, True),
            AccountMeta(sentinel, False, False),
            AccountMeta(Pubkey.from_string(output_mint), False, False),
            AccountMeta(sentinel, False, False),
        ],
    )
    message = MessageV0.try_compile(Pubkey.from_string(taker), [route_ix], [], Hash.default())
    message_bytes = to_bytes_versioned(message)
    digest = hashlib.sha256(message_bytes).hexdigest()
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
        price_impact_pct=0.2,
        fee_bps=0.0,
        router="metis",
        transaction_version=0,
        last_valid_block_height=950,
        recent_blockhash=str(message.recent_blockhash),
        message_bytes=message_bytes,
        serialized_transaction_bytes=b"unsigned",
        route_plan=(
            JupiterRouteStep(
                amm_key=str(Pubkey.new_unique()),
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
    keys = tuple(str(key) for key in message.account_keys)
    validation = TransactionValidationResult(
        message_sha256=digest,
        message_size=len(message_bytes),
        fee_payer=taker,
        required_signers=(taker,),
        program_ids=(jupiter,),
        instruction_count=1,
        account_keys=keys,
    )
    simulation = SimulationEvidence(
        message_sha256=digest,
        recent_blockhash=str(message.recent_blockhash),
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
        inner_program_ids=(str(Pubkey.new_unique()),),
        inner_instruction_count=1,
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
        dex_program_ids=(str(Pubkey.new_unique()),),
        top_level_program_ids=(jupiter,),
        inner_program_ids=simulation.inner_program_ids,
        registry_sha256="a" * 64,
    )
    policy = JupiterRouteSemanticPolicy(jupiter_program_ids=(jupiter,))
    return order, validation, simulation, sim_gate, program_gate, policy


def evaluate(values=None):
    order, validation, simulation, sim_gate, program_gate, policy = values or fixture()
    return evaluate_jupiter_route_semantics(
        order,
        validation=validation,
        simulation=simulation,
        simulation_gate=sim_gate,
        program_gate=program_gate,
        policy=policy,
    )


def test_decodes_supported_jupiter_route_payload():
    decoded = decode_jupiter_route_instruction_data(route_data())
    assert decoded.steps[0].swap_variant_index == 26
    assert decoded.steps[0].percent == 100
    assert decoded.steps[0].input_index == 0
    assert decoded.steps[0].output_index == 1
    assert decoded.input_amount_raw == 1_000
    assert decoded.quoted_output_amount_raw == 2_000
    assert decoded.slippage_bps == 50
    assert decoded.platform_fee_bps == 0


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "missing or truncated"),
        (b"12345678" + route_data()[8:], "discriminator"),
        (JUPITER_ROUTE_DISCRIMINATOR + struct.pack("<I", 0), "length"),
        (JUPITER_ROUTE_DISCRIMINATOR + struct.pack("<I", 13), "length"),
        (route_data(variant=8), "unsupported"),
        (route_data(percent=0), "percent"),
        (route_data(input_index=0, output_index=0), "indexes"),
        (route_data(amount_in=0), "amounts"),
        (route_data() + b"extra", "trailing"),
    ],
)
def test_rejects_malformed_or_unsupported_route_payload(payload, message):
    with pytest.raises(JupiterRouteSemanticError, match=message):
        decode_jupiter_route_instruction_data(payload)


def test_accepts_exact_serialized_route_instruction_matching_order_and_quote():
    order, _, _, _, _, _ = fixture()
    result = evaluate()
    assert result.passed is True
    assert result.input_amount_raw == 1_000
    assert result.quoted_output_amount_raw == 2_000
    assert result.slippage_bps == 50
    assert result.platform_fee_bps == 0
    assert dict(result.token_index_mints)[0] == order.input_mint


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("input_amount_raw", 999, "input amount"),
        ("quoted_output_amount_raw", 2_001, "quoted output"),
        ("slippage_bps", 101, "slippage exceeds"),
        ("platform_fee_bps", 1, "platform fee exceeds"),
    ],
)
def test_rejects_encoded_amounts_and_fees_outside_policy(field, value, expected_error):
    order, validation, simulation, sim_gate, program_gate, policy = fixture()
    encoded = {
        "input_amount_raw": 1_000,
        "quoted_output_amount_raw": 2_000,
        "slippage_bps": 50,
        "platform_fee_bps": 0,
    }
    encoded[field] = value
    instruction = route_data(**{
        "amount_in": encoded["input_amount_raw"],
        "amount_out": encoded["quoted_output_amount_raw"],
        "slippage": encoded["slippage_bps"],
        "platform_fee": encoded["platform_fee_bps"],
    })
    # The message bytes and digest are intentionally replaced together so this
    # test reaches the semantic policy rather than failing digest binding first.
    message = MessageV0.try_compile(
        Pubkey.from_string(order.taker),
        [Instruction(
            Pubkey.from_string(policy.jupiter_program_ids[0]),
            instruction,
            [
                AccountMeta(Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"), False, False),
                AccountMeta(Pubkey.from_string(order.taker), True, False),
                AccountMeta(Pubkey.from_string(__import__("veltrix.jupiter_swap_effects", fromlist=["derive_associated_token_address"]).derive_associated_token_address(order.taker, order.input_mint)), False, True),
                AccountMeta(Pubkey.from_string(__import__("veltrix.jupiter_swap_effects", fromlist=["derive_associated_token_address"]).derive_associated_token_address(order.taker, order.output_mint)), False, True),
                AccountMeta(Pubkey.from_string(policy.jupiter_program_ids[0]), False, False),
                AccountMeta(Pubkey.from_string(order.output_mint), False, False),
                AccountMeta(Pubkey.from_string(policy.jupiter_program_ids[0]), False, False),
            ],
        )],
        [],
        Hash.default(),
    )
    message_bytes = to_bytes_versioned(message)
    digest = hashlib.sha256(message_bytes).hexdigest()
    order = dataclasses.replace(order, message_bytes=message_bytes, recent_blockhash=str(message.recent_blockhash))
    validation = dataclasses.replace(
        validation,
        message_sha256=digest,
        message_size=len(message_bytes),
        program_ids=(policy.jupiter_program_ids[0],),
        account_keys=tuple(str(key) for key in message.account_keys),
    )
    simulation = dataclasses.replace(
        simulation,
        message_sha256=digest,
        recent_blockhash=str(message.recent_blockhash),
        top_level_program_ids=(policy.jupiter_program_ids[0],),
    )
    sim_gate = dataclasses.replace(sim_gate, message_sha256=digest)
    program_gate = dataclasses.replace(
        program_gate,
        message_sha256=digest,
        top_level_program_ids=(policy.jupiter_program_ids[0],),
    )
    if field == "slippage_bps":
        policy = dataclasses.replace(policy, max_slippage_bps=100)
    with pytest.raises(JupiterRouteSemanticError, match=expected_error):
        evaluate((order, validation, simulation, sim_gate, program_gate, policy))


def test_rejects_unknown_route_label_even_when_program_is_pinned():
    order, validation, simulation, sim_gate, program_gate, policy = fixture()
    order = dataclasses.replace(
        order,
        route_plan=(dataclasses.replace(order.route_plan[0], label="Unknown DEX"),),
    )
    program_gate = dataclasses.replace(program_gate, route_labels=("Unknown DEX",))
    with pytest.raises(JupiterRouteSemanticError, match="unsupported DEX label"):
        evaluate((order, validation, simulation, sim_gate, program_gate, policy))


def test_rejects_simulation_or_program_gate_digest_mismatch():
    order, validation, simulation, sim_gate, program_gate, policy = fixture()
    sim_gate = dataclasses.replace(sim_gate, message_sha256="b" * 64)
    with pytest.raises(JupiterRouteSemanticError, match="digests differ"):
        evaluate((order, validation, simulation, sim_gate, program_gate, policy))


def test_rejects_router_without_a_pinned_route_instruction_decoder():
    order, validation, simulation, sim_gate, program_gate, policy = fixture()
    order = dataclasses.replace(order, router="dflow")
    with pytest.raises(JupiterRouteSemanticError, match="router is outside"):
        evaluate((order, validation, simulation, sim_gate, program_gate, policy))


def test_rejects_policy_for_an_invalid_jupiter_program_public_key():
    with pytest.raises(JupiterRouteSemanticError, match="invalid public key"):
        JupiterRouteSemanticPolicy(jupiter_program_ids=("not-a-public-key",))
