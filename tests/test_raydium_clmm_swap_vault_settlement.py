from dataclasses import replace
from types import SimpleNamespace
import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState
from veltrix.raydium_clmm_swap_loop import RaydiumClmmSwapLoopResult
from veltrix.raydium_clmm_swap_loop_settlement import RaydiumSwapLoopSettlementResult
from veltrix.raydium_clmm_swap_transaction_settlement import (
    RaydiumSwapSettlement,
    RaydiumSwapTransactionSettlement,
)
from veltrix.raydium_clmm_swap_vault_settlement import (
    RaydiumSwapVaultSettlementError,
    apply_raydium_clmm_swap_vault_settlement,
    reconcile_raydium_clmm_swap_vault_snapshots,
)
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTickArraySnapshot
from veltrix.raydium_clmm_vaults import (
    CLASSIC_SPL_TOKEN_PROGRAM_ID,
    RaydiumClmmVaultAccount,
    RaydiumClmmVaultSnapshot,
    apply_raydium_clmm_vault_balance_delta,
    derive_raydium_clmm_vault_address,
)


def key():
    return str(Pubkey.new_unique())


def pool():
    address = key()
    mint0, mint1 = key(), key()
    return RaydiumClmmPoolState(
        pool_address=address,
        bump=1,
        amm_config=key(),
        pool_creator=key(),
        token_mint_0=mint0,
        token_mint_1=mint1,
        token_vault_0=derive_raydium_clmm_vault_address(pool_address=address, mint=mint0),
        token_vault_1=derive_raydium_clmm_vault_address(pool_address=address, mint=mint1),
        observation_key=key(),
        mint_decimals_0=6,
        mint_decimals_1=6,
        tick_spacing=1,
        liquidity=1000,
        sqrt_price_x64=1 << 64,
        tick_current=0,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data_sha256="a" * 64,
    )


def vault(p, *, token0, amount):
    mint = p.token_mint_0 if token0 else p.token_mint_1
    address = p.token_vault_0 if token0 else p.token_vault_1
    data = bytearray(165)
    data[:32] = bytes(Pubkey.from_string(mint))
    data[32:64] = bytes(Pubkey.from_string(p.pool_address))
    data[64:72] = amount.to_bytes(8, "little")
    data[108] = 1
    raw = bytes(data)
    return RaydiumClmmVaultAccount(
        address=address,
        pool_address=p.pool_address,
        mint=mint,
        token_program_id=CLASSIC_SPL_TOKEN_PROGRAM_ID,
        token_authority=p.pool_address,
        amount_raw=amount,
        state=1,
        lamports=1,
        data_sha256=hashlib.sha256(raw).hexdigest(),
        account_data=raw,
    )


def snapshots(p, *, amount0=500, amount1=400):
    route_hash = "c" * 64
    tick_snapshot = RaydiumClmmTickArraySnapshot(
        route_message_sha256=route_hash,
        simulation_response_id="sim-vault",
        simulation_context_slot=100,
        provider_id="rpc",
        cluster="devnet",
        genesis_hash="d" * 64,
        context_slot=101,
        tick_arrays=(),
        cpi_account_order=(),
    )
    before = RaydiumClmmVaultSnapshot(
        route_message_sha256=route_hash,
        simulation_response_id="sim-vault",
        simulation_context_slot=100,
        provider_id="rpc",
        cluster="devnet",
        genesis_hash="d" * 64,
        context_slot=101,
        vaults=(vault(p, token0=True, amount=amount0), vault(p, token0=False, amount=amount1)),
    )
    return tick_snapshot, before


def loop_settlement(p, tick_snapshot, *, zero_for_one, amount0=100, amount1=80, transfer_fee0=0):
    loop = RaydiumClmmSwapLoopResult(
        pool=p,
        tick_array_snapshot=tick_snapshot,
        tick_array_queue=None,
        fee_state=None,
        dynamic_fee_state=None,
        final_iteration=SimpleNamespace(swap_complete=True),
        zero_for_one=zero_for_one,
        is_base_input=True,
        amount_specified=amount0 if zero_for_one else amount1,
        sqrt_price_limit_x64=1,
        inner_iterations=2,
        swap_complete=True,
    )
    settlement = RaydiumSwapTransactionSettlement(
        state=None,
        amounts=RaydiumSwapSettlement(
            amount_0=amount0,
            amount_1=amount1,
            trade_fee_0=0,
            trade_fee_1=0,
        ),
        amount_returned=amount1 if zero_for_one else amount0,
        transfer_fee_0=transfer_fee0,
        transfer_fee_1=0,
    )
    return RaydiumSwapLoopSettlementResult(loop=loop, settlement=settlement)


@pytest.mark.parametrize(
    "zero_for_one,expected0,expected1",
    [
        (True, 600, 320),
        (False, 420, 500),
    ],
)
def test_expected_post_swap_vault_balances_apply_exact_directional_transfer(zero_for_one, expected0, expected1):
    p = pool()
    tick_snapshot, before = snapshots(p)
    handoff = loop_settlement(p, tick_snapshot, zero_for_one=zero_for_one)
    result = apply_raydium_clmm_swap_vault_settlement(before, handoff)
    by_address = {item.address: item for item in result.expected_post_state.vaults}
    assert by_address[p.token_vault_0].amount_raw == expected0
    assert by_address[p.token_vault_1].amount_raw == expected1
    assert result.token0_vault_before == 500
    assert result.token1_vault_before == 400


def test_actual_post_swap_vault_snapshot_reconciles_against_expected_amounts():
    p = pool()
    tick_snapshot, before = snapshots(p)
    handoff = loop_settlement(p, tick_snapshot, zero_for_one=True)
    expected = apply_raydium_clmm_swap_vault_settlement(before, handoff).expected_post_state
    after = replace(expected, context_slot=102)
    result = reconcile_raydium_clmm_swap_vault_snapshots(before, after, handoff)
    assert result.reconciliation.input_vault_delta == 100
    assert result.reconciliation.output_vault_delta == 80


def test_reconciliation_rejects_wrong_actual_delta_and_nonzero_transfer_fee():
    p = pool()
    tick_snapshot, before = snapshots(p)
    handoff = loop_settlement(p, tick_snapshot, zero_for_one=True)
    expected = apply_raydium_clmm_swap_vault_settlement(before, handoff).expected_post_state
    token0, token1 = expected.vaults
    # Increase token0 by one extra unit while retaining a valid serialized account/hash.
    wrong_token0 = apply_raydium_clmm_vault_balance_delta(token0, delta_raw=1)
    wrong = replace(expected, vaults=(wrong_token0, token1), context_slot=102)
    with pytest.raises(RaydiumSwapVaultSettlementError, match="delta mismatch"):
        reconcile_raydium_clmm_swap_vault_snapshots(before, wrong, handoff)

    fee_handoff = loop_settlement(p, tick_snapshot, zero_for_one=True, transfer_fee0=1)
    with pytest.raises(RaydiumSwapVaultSettlementError, match="transfer-fee"):
        apply_raydium_clmm_swap_vault_settlement(before, fee_handoff)


def test_vault_settlement_fails_closed_on_underflow_and_tampered_decoded_amount():
    p = pool()
    tick_snapshot, before = snapshots(p, amount1=50)
    handoff = loop_settlement(p, tick_snapshot, zero_for_one=True, amount1=80)
    with pytest.raises(RaydiumSwapVaultSettlementError, match="balance update failed"):
        apply_raydium_clmm_swap_vault_settlement(before, handoff)

    token0, token1 = before.vaults
    tampered = replace(token0, amount_raw=token0.amount_raw + 1)
    bad_snapshot = replace(before, vaults=(tampered, token1))
    with pytest.raises(RaydiumSwapVaultSettlementError, match="source-byte validation"):
        apply_raydium_clmm_swap_vault_settlement(bad_snapshot, loop_settlement(p, tick_snapshot, zero_for_one=True))
