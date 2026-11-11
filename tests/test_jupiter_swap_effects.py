"""Adversarial tests for Jupiter quote-to-user-balance effect verification."""
import dataclasses
import hashlib
import pytest
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_swap_effects import (
    CLASSIC_TOKEN_PROGRAM_ID,
    JupiterSwapEffectError,
    JupiterSwapEffectPolicy,
    build_expected_jupiter_token_deltas,
    derive_associated_token_address,
    evaluate_jupiter_swap_effects,
)
from veltrix.simulation_gate import SimulationEvidence, SimulationGateResult, SimulatedTokenBalance
from veltrix.transaction_validator import TransactionValidationResult


def fixture():
    taker = str(Pubkey.new_unique())
    input_mint = str(Pubkey.new_unique())
    output_mint = str(Pubkey.new_unique())
    message = b"jupiter-swap-message"
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
        price_impact_pct=0.2,
        fee_bps=50.0,
        router="metis",
        transaction_version=0,
        last_valid_block_height=950,
        recent_blockhash="recent-blockhash",
        message_bytes=message,
        serialized_transaction_bytes=b"serialized",
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
        prioritization_fee_lamports=1_000,
        rent_fee_lamports=0,
    )
    expected = build_expected_jupiter_token_deltas(order)
    balances = (
        SimulatedTokenBalance(
            token_account=expected[0].token_account,
            mint=input_mint,
            owner=taker,
            pre_amount_raw=5_000,
            post_amount_raw=4_000,
            token_program_id=CLASSIC_TOKEN_PROGRAM_ID,
        ),
        SimulatedTokenBalance(
            token_account=expected[1].token_account,
            mint=output_mint,
            owner=taker,
            pre_amount_raw=100,
            post_amount_raw=2_100,
            token_program_id=CLASSIC_TOKEN_PROGRAM_ID,
        ),
    )
    validation = TransactionValidationResult(
        message_sha256=digest,
        message_size=len(message),
        fee_payer=taker,
        required_signers=(taker,),
        program_ids=("program",),
        instruction_count=1,
        account_keys=(taker, "program"),
    )
    simulation = SimulationEvidence(
        message_sha256=digest,
        recent_blockhash="recent-blockhash",
        cluster="mainnet-beta",
        genesis_hash="genesis-hash",
        provider_id="rpc-primary",
        response_id="simulation-1",
        observed_at_epoch_ms=1_001_000,
        context_slot=500,
        current_block_height=900,
        last_valid_block_height=950,
        simulation_error=None,
        units_consumed=100_000,
        fee_lamports=5_000,
        token_balances=balances,
    )
    gate = SimulationGateResult(
        approved=True,
        message_sha256=digest,
        provider_id="rpc-primary",
        response_id="simulation-1",
        context_slot=500,
        units_consumed=100_000,
        fee_lamports=5_000,
        verified_token_account_count=2,
    )
    return order, validation, simulation, gate


def evaluate(values=None, policy=None):
    order, validation, simulation, gate = values or fixture()
    return evaluate_jupiter_swap_effects(
        order,
        validation=validation,
        simulation=simulation,
        simulation_gate=gate,
        policy=policy or JupiterSwapEffectPolicy(),
        now_epoch_ms=1_005_000,
    )


def test_derives_classic_associated_token_accounts():
    owner = str(Pubkey.new_unique())
    mint = str(Pubkey.new_unique())
    first = derive_associated_token_address(owner, mint)
    second = derive_associated_token_address(owner, mint)
    assert first == second
    assert str(Pubkey.from_string(first))


def test_builds_exact_input_and_minimum_output_delta_expectations():
    order, _, _, _ = fixture()
    expected = build_expected_jupiter_token_deltas(order)
    assert len(expected) == 2
    assert expected[0].delta_raw == -1_000
    assert expected[1].delta_raw is None
    assert expected[1].minimum_delta_raw == 1_950
    assert expected[0].owner == order.taker
    assert expected[1].owner == order.taker


def test_accepts_simulated_output_above_minimum_even_if_it_differs_from_quote():
    order, validation, simulation, gate = fixture()
    balances = list(simulation.token_balances)
    balances[1] = dataclasses.replace(balances[1], post_amount_raw=2_150)
    simulation = dataclasses.replace(simulation, token_balances=tuple(balances))
    result = evaluate((order, validation, simulation, gate))
    assert result.passed is True
    assert result.input_delta_raw == -1_000
    assert result.output_delta_raw == 2_050
    assert result.output_delta_raw >= result.minimum_output_amount_raw


@pytest.mark.parametrize(
    ("balance_index", "field", "value", "message"),
    [
        (0, "post_amount_raw", 4_001, "input spend differs"),
        (1, "post_amount_raw", 2_049, "below Jupiter's minimum"),
        (0, "token_program_id", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "classic SPL Token"),
        (1, "owner", str(Pubkey.new_unique()), "mint or owner"),
        (1, "mint", str(Pubkey.new_unique()), "mint or owner"),
    ],
)
def test_rejects_token_balance_effects_outside_exact_quote(balance_index, field, value, message):
    order, validation, simulation, gate = fixture()
    balances = list(simulation.token_balances)
    balances[balance_index] = dataclasses.replace(balances[balance_index], **{field: value})
    simulation = dataclasses.replace(simulation, token_balances=tuple(balances))
    with pytest.raises(JupiterSwapEffectError, match=message):
        evaluate((order, validation, simulation, gate))


def test_rejects_output_below_minimum_even_if_exact_expected_delta_is_changed():
    order, validation, simulation, gate = fixture()
    order = dataclasses.replace(order, output_amount_raw=1_900, minimum_output_amount_raw=1_950)
    with pytest.raises(JupiterSwapEffectError, match="invalid"):
        evaluate((order, validation, simulation, gate))


def test_rejects_simulated_output_below_slippage_minimum():
    order, validation, simulation, gate = fixture()
    balances = list(simulation.token_balances)
    balances[1] = dataclasses.replace(balances[1], post_amount_raw=2_049)
    simulation = dataclasses.replace(simulation, token_balances=tuple(balances))
    with pytest.raises(JupiterSwapEffectError, match="below Jupiter's minimum"):
        evaluate((order, validation, simulation, gate))


def test_rejects_stale_order_quote():
    order, validation, simulation, gate = fixture()
    order = dataclasses.replace(order, observed_at_epoch_ms=1)
    with pytest.raises(JupiterSwapEffectError, match="stale"):
        evaluate((order, validation, simulation, gate), JupiterSwapEffectPolicy(max_order_age_ms=1_000))


def test_rejects_digest_or_simulation_response_mismatch():
    order, validation, simulation, gate = fixture()
    simulation = dataclasses.replace(simulation, message_sha256="b" * 64)
    with pytest.raises(JupiterSwapEffectError, match="digests differ"):
        evaluate((order, validation, simulation, gate))

    order, validation, simulation, gate = fixture()
    gate = dataclasses.replace(gate, response_id="other-response")
    with pytest.raises(JupiterSwapEffectError, match="different RPC responses"):
        evaluate((order, validation, simulation, gate))


def test_rejects_wrong_fee_payer_and_blockhash():
    order, validation, simulation, gate = fixture()
    order = dataclasses.replace(order, fee_payer=str(Pubkey.new_unique()))
    with pytest.raises(JupiterSwapEffectError, match="fee payer"):
        evaluate((order, validation, simulation, gate))

    order, validation, simulation, gate = fixture()
    simulation = dataclasses.replace(simulation, recent_blockhash="wrong-blockhash")
    with pytest.raises(JupiterSwapEffectError, match="blockhash differs"):
        evaluate((order, validation, simulation, gate))


def test_rejects_extra_or_missing_token_account_snapshots():
    order, validation, simulation, gate = fixture()
    simulation = dataclasses.replace(simulation, token_balances=simulation.token_balances[:1])
    gate = dataclasses.replace(gate, verified_token_account_count=1)
    with pytest.raises(JupiterSwapEffectError, match="exactly two"):
        evaluate((order, validation, simulation, gate))


def test_rejects_simulation_predating_the_order():
    order, validation, simulation, gate = fixture()
    simulation = dataclasses.replace(simulation, observed_at_epoch_ms=999_999)
    with pytest.raises(JupiterSwapEffectError, match="predates"):
        evaluate((order, validation, simulation, gate))
