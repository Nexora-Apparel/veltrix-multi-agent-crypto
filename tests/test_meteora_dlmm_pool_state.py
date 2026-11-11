"""Tests for same-slot Meteora LB-pair and reserve state verification."""
import dataclasses
import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_swap_effects import (
    CLASSIC_TOKEN_PROGRAM_ID,
    JupiterSwapEffectResult,
    derive_associated_token_address,
)
from veltrix.meteora_dlmm_cpi import (
    METEORA_DLMM_PROGRAM_ID,
    DecodedMeteoraDlmmSwap,
    MeteoraDlmmCpiResult,
)
from veltrix.meteora_dlmm_pool_state import (
    LB_PAIR_ACCOUNT_SIZE,
    LB_PAIR_OFFSETS,
    METEORA_LB_PAIR_DISCRIMINATOR,
    METEORA_ORACLE_DISCRIMINATOR,
    MeteoraDlmmPoolStateError,
    evaluate_meteora_dlmm_pool_state,
    parse_classic_spl_token_account,
    parse_meteora_lb_pair_state,
)
from veltrix.simulation_gate import (
    SimulatedAccountState,
    SimulatedTokenBalance,
    SimulationEvidence,
    SimulationGateResult,
)


def pubkey():
    return str(Pubkey.new_unique())


def lb_pair_data(token_x, token_y, reserve_x, reserve_y, oracle):
    data = bytearray(LB_PAIR_ACCOUNT_SIZE)
    data[:8] = METEORA_LB_PAIR_DISCRIMINATOR
    for name, value in (
        ("token_x_mint", token_x),
        ("token_y_mint", token_y),
        ("reserve_x", reserve_x),
        ("reserve_y", reserve_y),
        ("oracle", oracle),
    ):
        offset = LB_PAIR_OFFSETS[name]
        data[offset:offset + 32] = bytes(Pubkey.from_string(value))
    return bytes(data)


def token_account_data(mint, authority, amount):
    data = bytearray(165)
    data[0:32] = bytes(Pubkey.from_string(mint))
    data[32:64] = bytes(Pubkey.from_string(authority))
    data[64:72] = amount.to_bytes(8, "little")
    data[108] = 1
    return bytes(data)


def snapshot(address, owner, pre_data, post_data, *, executable=False):
    return SimulatedAccountState(
        address=address,
        pre_owner_program_id=owner,
        post_owner_program_id=owner,
        pre_lamports=10_000,
        post_lamports=10_000,
        pre_executable=executable,
        post_executable=executable,
        pre_data=pre_data,
        post_data=post_data,
    )


def fixture():
    taker = pubkey()
    input_mint = pubkey()
    output_mint = pubkey()
    pool = pubkey()
    reserve_x = pubkey()
    reserve_y = pubkey()
    oracle = pubkey()
    reserve_x_authority = pubkey()
    reserve_y_authority = pubkey()
    input_ata = derive_associated_token_address(taker, input_mint)
    output_ata = derive_associated_token_address(taker, output_mint)
    message = b"meteora-pool-state-message"
    digest = hashlib.sha256(message).hexdigest()
    order = JupiterOrderTransaction(
        provider_id="jupiter-swap-v2",
        request_id="pool-state-order",
        quote_id="pool-state-quote",
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
    decoded = DecodedMeteoraDlmmSwap(
        amount_in=1_000,
        minimum_amount_out=1_980,
        lb_pair=pool,
        reserve_x=reserve_x,
        reserve_y=reserve_y,
        user_token_in=input_ata,
        user_token_out=output_ata,
        bitmap_extension=METEORA_DLMM_PROGRAM_ID,
        token_x_mint=input_mint,
        token_y_mint=output_mint,
        oracle=oracle,
        user=taker,
        event_authority=pubkey(),
    )
    cpi = MeteoraDlmmCpiResult(
        passed=True,
        message_sha256=digest,
        program_id=METEORA_DLMM_PROGRAM_ID,
        decoded_swaps=(decoded,),
        max_slippage_bps=100,
    )
    pool_data = lb_pair_data(input_mint, output_mint, reserve_x, reserve_y, oracle)
    reserve_x_pre = token_account_data(input_mint, reserve_x_authority, 10_000)
    reserve_x_post = token_account_data(input_mint, reserve_x_authority, 11_000)
    reserve_y_pre = token_account_data(output_mint, reserve_y_authority, 20_000)
    reserve_y_post = token_account_data(output_mint, reserve_y_authority, 18_000)
    account_states = (
        snapshot(pool, METEORA_DLMM_PROGRAM_ID, pool_data, pool_data),
        snapshot(reserve_x, CLASSIC_TOKEN_PROGRAM_ID, reserve_x_pre, reserve_x_post),
        snapshot(reserve_y, CLASSIC_TOKEN_PROGRAM_ID, reserve_y_pre, reserve_y_post),
        snapshot(oracle, METEORA_DLMM_PROGRAM_ID, METEORA_ORACLE_DISCRIMINATOR + bytes(32), METEORA_ORACLE_DISCRIMINATOR + bytes(32)),
    )
    simulation = SimulationEvidence(
        message_sha256=digest,
        recent_blockhash="blockhash",
        cluster="mainnet-beta",
        genesis_hash="genesis",
        provider_id="rpc-primary",
        response_id="sim-state",
        observed_at_epoch_ms=1_001_000,
        context_slot=500,
        current_block_height=900,
        last_valid_block_height=950,
        simulation_error=None,
        units_consumed=100_000,
        fee_lamports=5_000,
        token_balances=(
            SimulatedTokenBalance(input_ata, input_mint, taker, 5_000, 4_000, CLASSIC_TOKEN_PROGRAM_ID),
            SimulatedTokenBalance(output_ata, output_mint, taker, 100, 2_100, CLASSIC_TOKEN_PROGRAM_ID),
        ),
        account_states=account_states,
    )
    gate = SimulationGateResult(
        approved=True,
        message_sha256=digest,
        provider_id="rpc-primary",
        response_id="sim-state",
        context_slot=500,
        units_consumed=100_000,
        fee_lamports=5_000,
        verified_token_account_count=2,
    )
    effects = JupiterSwapEffectResult(
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
    return order, simulation, gate, cpi, effects


def evaluate(values=None):
    order, simulation, gate, cpi, effects = values or fixture()
    return evaluate_meteora_dlmm_pool_state(
        order,
        simulation=simulation,
        simulation_gate=gate,
        cpi_result=cpi,
        swap_effects=effects,
    )


def test_parses_pinned_meteora_lb_pair_layout_and_static_links():
    order, simulation, _, _, _ = fixture()
    pool = parse_meteora_lb_pair_state(simulation.account_states[0])
    assert pool.address == order.route_plan[0].amm_key
    assert pool.token_x_mint == order.input_mint
    assert pool.token_y_mint == order.output_mint


def test_parses_classic_spl_reserve_balances():
    _, simulation, _, _, _ = fixture()
    reserve = parse_classic_spl_token_account(simulation.account_states[1])
    assert reserve.pre_amount == 10_000
    assert reserve.post_amount == 11_000


def test_accepts_pool_reserve_oracle_and_taker_ata_state_match():
    result = evaluate()
    assert result.passed is True
    assert result.pool_state.reserve_x == result.reserve_x_state.address
    assert result.pool_state.reserve_y == result.reserve_y_state.address


def test_rejects_missing_pool_or_reserve_account_snapshots():
    order, simulation, gate, cpi, effects = fixture()
    simulation = dataclasses.replace(simulation, account_states=simulation.account_states[:2])
    with pytest.raises(MeteoraDlmmPoolStateError, match="account state is missing"):
        evaluate((order, simulation, gate, cpi, effects))


def test_rejects_lb_pair_reserve_or_mint_link_mismatch():
    order, simulation, gate, cpi, effects = fixture()
    decoded = cpi.decoded_swaps[0]
    changed = dataclasses.replace(decoded, reserve_x=pubkey())
    cpi = dataclasses.replace(cpi, decoded_swaps=(changed,))
    with pytest.raises(MeteoraDlmmPoolStateError, match="on-chain LB-pair state"):
        evaluate((order, simulation, gate, cpi, effects))


def test_rejects_reserve_wrong_mint_or_non_token_program_owner():
    order, simulation, gate, cpi, effects = fixture()
    states = list(simulation.account_states)
    wrong = token_account_data(pubkey(), pubkey(), 10_000)
    states[1] = dataclasses.replace(states[1], pre_data=wrong, post_data=wrong)
    simulation = dataclasses.replace(simulation, account_states=tuple(states))
    with pytest.raises(MeteoraDlmmPoolStateError, match="mint or token authority changed"):
        evaluate((order, simulation, gate, cpi, effects))

    order, simulation, gate, cpi, effects = fixture()
    states = list(simulation.account_states)
    states[1] = dataclasses.replace(
        states[1],
        pre_owner_program_id=METEORA_DLMM_PROGRAM_ID,
        post_owner_program_id=METEORA_DLMM_PROGRAM_ID,
    )
    simulation = dataclasses.replace(simulation, account_states=tuple(states))
    with pytest.raises(MeteoraDlmmPoolStateError, match="classic SPL Token"):
        evaluate((order, simulation, gate, cpi, effects))


def test_rejects_invalid_oracle_discriminator_and_changed_pool_links():
    order, simulation, gate, cpi, effects = fixture()
    states = list(simulation.account_states)
    states[3] = dataclasses.replace(
        states[3],
        pre_data=b"badoracle" + bytes(32),
        post_data=b"badoracle" + bytes(32),
    )
    simulation = dataclasses.replace(simulation, account_states=tuple(states))
    with pytest.raises(MeteoraDlmmPoolStateError, match="oracle account type"):
        evaluate((order, simulation, gate, cpi, effects))

    order, simulation, gate, cpi, effects = fixture()
    changed = bytearray(simulation.account_states[0].post_data)
    changed[LB_PAIR_OFFSETS["reserve_x"]:LB_PAIR_OFFSETS["reserve_x"] + 32] = bytes(Pubkey.from_string(pubkey()))
    states = list(simulation.account_states)
    states[0] = dataclasses.replace(states[0], post_data=bytes(changed))
    simulation = dataclasses.replace(simulation, account_states=tuple(states))
    with pytest.raises(MeteoraDlmmPoolStateError, match="links changed"):
        evaluate((order, simulation, gate, cpi, effects))


def test_rejects_meteora_cpi_not_using_taker_associated_token_accounts():
    order, simulation, gate, cpi, effects = fixture()
    decoded = dataclasses.replace(cpi.decoded_swaps[0], user_token_in=pubkey())
    cpi = dataclasses.replace(cpi, decoded_swaps=(decoded,))
    with pytest.raises(MeteoraDlmmPoolStateError, match="not the taker's exact"):
        evaluate((order, simulation, gate, cpi, effects))


def test_rejects_state_evidence_from_another_simulation_slot():
    order, simulation, gate, cpi, effects = fixture()
    effects = dataclasses.replace(effects, simulation_context_slot=499)
    with pytest.raises(MeteoraDlmmPoolStateError, match="same simulation slot"):
        evaluate((order, simulation, gate, cpi, effects))
