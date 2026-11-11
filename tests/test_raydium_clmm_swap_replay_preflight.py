from dataclasses import replace
from types import SimpleNamespace

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_cpi import (
    RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
    RaydiumClmmCpiResult,
)
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState
from veltrix.raydium_clmm_swap_loop import RaydiumClmmSwapLoopResult
from veltrix.raydium_clmm_swap_loop_settlement import RaydiumSwapLoopSettlementResult
from veltrix.raydium_clmm_swap_replay_preflight import (
    RaydiumSwapReplayPreflightError,
    verify_raydium_swap_replay_preflight,
)
from veltrix.raydium_clmm_swap_transaction_settlement import (
    RaydiumSwapSettlement,
    RaydiumSwapTransactionSettlement,
)
from veltrix.raydium_clmm_swap_vault_settlement import apply_raydium_clmm_swap_vault_settlement
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTickArraySnapshot
from veltrix.raydium_clmm_vaults import (
    CLASSIC_SPL_TOKEN_PROGRAM_ID,
    RaydiumClmmVaultAccount,
    RaydiumClmmVaultSnapshot,
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


def make_vault(p, *, token0, amount):
    mint = p.token_mint_0 if token0 else p.token_mint_1
    address = p.token_vault_0 if token0 else p.token_vault_1
    data = bytearray(165)
    data[:32] = bytes(Pubkey.from_string(mint))
    data[32:64] = bytes(Pubkey.from_string(p.pool_address))
    data[64:72] = amount.to_bytes(8, "little")
    data[108] = 1
    raw = bytes(data)
    import hashlib
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


def artifacts():
    p = pool()
    route_hash = "c" * 64
    array_address = key()
    tick_snapshot = RaydiumClmmTickArraySnapshot(
        route_message_sha256=route_hash,
        simulation_response_id="sim-preflight",
        simulation_context_slot=100,
        provider_id="rpc",
        cluster="devnet",
        genesis_hash="d" * 64,
        context_slot=101,
        tick_arrays=(),
        cpi_account_order=(array_address,),
    )
    before = RaydiumClmmVaultSnapshot(
        route_message_sha256=route_hash,
        simulation_response_id="sim-preflight",
        simulation_context_slot=100,
        provider_id="rpc",
        cluster="devnet",
        genesis_hash="d" * 64,
        context_slot=101,
        vaults=(make_vault(p, token0=True, amount=500), make_vault(p, token0=False, amount=400)),
    )
    loop = RaydiumClmmSwapLoopResult(
        pool=p,
        tick_array_snapshot=tick_snapshot,
        tick_array_queue=None,
        fee_state=None,
        dynamic_fee_state=None,
        final_iteration=SimpleNamespace(swap_complete=True),
        zero_for_one=True,
        is_base_input=True,
        amount_specified=100,
        sqrt_price_limit_x64=999,
        inner_iterations=2,
        swap_complete=True,
    )
    state = SimpleNamespace(
        pool=replace(p, tick_current=5),
        observation=SimpleNamespace(address=p.observation_key),
    )
    swap = RaydiumSwapTransactionSettlement(
        state=state,
        amounts=RaydiumSwapSettlement(
            amount_0=100,
            amount_1=80,
            trade_fee_0=0,
            trade_fee_1=0,
        ),
        amount_returned=80,
        transfer_fee_0=0,
        transfer_fee_1=0,
    )
    loop_settlement = RaydiumSwapLoopSettlementResult(loop=loop, settlement=swap)
    vault_settlement = apply_raydium_clmm_swap_vault_settlement(before, loop_settlement)
    cpi = RaydiumClmmCpiResult(
        passed=True,
        message_sha256=route_hash,
        program_id=RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
        outer_instruction_index=0,
        cpi_instruction_index=1,
        payer=key(),
        input_token_account=key(),
        output_token_account=key(),
        amm_config=p.amm_config,
        pool_state=p.pool_address,
        input_vault=p.token_vault_0,
        output_vault=p.token_vault_1,
        observation_state=p.observation_key,
        tick_array_accounts=(array_address,),
        amount_raw=100,
        minimum_output_raw=70,
        sqrt_price_limit_x64=999,
        is_base_input=True,
        stack_height=2,
    )
    return cpi, loop_settlement, vault_settlement


def test_preflight_binds_cpi_loop_settlement_and_expected_vault_post_state():
    cpi, loop_settlement, vault_settlement = artifacts()
    result = verify_raydium_swap_replay_preflight(cpi, loop_settlement, vault_settlement)
    assert result.execution_ready is False
    assert result.pool_after.tick_current == 5
    assert result.expected_vaults_after.vaults[0].amount_raw == 600
    assert result.expected_vaults_after.vaults[1].amount_raw == 320


@pytest.mark.parametrize(
    "change,match",
    [
        (lambda c, l, v: replace(c, input_vault=l.loop.pool.token_vault_1), "vault order"),
        (lambda c, l, v: replace(c, amount_raw=99), "amount, price limit"),
        (lambda c, l, v: replace(c, minimum_output_raw=81), "minimum-output"),
        (lambda c, l, v: replace(c, tick_array_accounts=("other",)), "trailing tick-array"),
        (lambda c, l, v: replace(c, message_sha256="0" * 64), "message digests"),
    ],
)
def test_preflight_rejects_cross_artifact_mismatch(change, match):
    cpi, loop_settlement, vault_settlement = artifacts()
    bad_cpi = change(cpi, loop_settlement, vault_settlement)
    with pytest.raises(RaydiumSwapReplayPreflightError, match=match):
        verify_raydium_swap_replay_preflight(bad_cpi, loop_settlement, vault_settlement)


def test_preflight_rejects_noncompleted_loop_and_nonzero_transfer_fee():
    cpi, loop_settlement, vault_settlement = artifacts()
    bad_loop = replace(loop_settlement.loop, swap_complete=False)
    with pytest.raises(RaydiumSwapReplayPreflightError, match="not complete"):
        verify_raydium_swap_replay_preflight(
            cpi, replace(loop_settlement, loop=bad_loop), vault_settlement
        )
    bad_swap = replace(loop_settlement.settlement, transfer_fee_0=1)
    with pytest.raises(RaydiumSwapReplayPreflightError, match="transfer fees"):
        verify_raydium_swap_replay_preflight(
            cpi, replace(loop_settlement, settlement=bad_swap), vault_settlement
        )
