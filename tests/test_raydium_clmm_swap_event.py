import base64
import hashlib
import struct
from types import SimpleNamespace
from dataclasses import replace

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_cpi import (
    RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
    RaydiumClmmCpiResult,
)
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState
from veltrix.raydium_clmm_swap_event import (
    RAYDIUM_SWAP_EVENT_DISCRIMINATOR,
    RaydiumSwapEventError,
    decode_raydium_clmm_swap_event,
    extract_raydium_clmm_swap_event,
    reconcile_raydium_clmm_swap_event,
)
from veltrix.raydium_clmm_swap_loop import RaydiumClmmSwapLoopResult
from veltrix.raydium_clmm_swap_loop_settlement import RaydiumSwapLoopSettlementResult
from veltrix.raydium_clmm_swap_transaction_settlement import (
    RaydiumSwapSettlement,
    RaydiumSwapTransactionSettlement,
)
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTickArraySnapshot


def key():
    return str(Pubkey.new_unique())


def pool():
    return RaydiumClmmPoolState(
        pool_address=key(),
        bump=1,
        amm_config=key(),
        pool_creator=key(),
        token_mint_0=key(),
        token_mint_1=key(),
        token_vault_0=key(),
        token_vault_1=key(),
        observation_key=key(),
        mint_decimals_0=6,
        mint_decimals_1=6,
        tick_spacing=1,
        liquidity=500,
        sqrt_price_x64=100,
        tick_current=5,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data_sha256="a" * 64,
    )


def event_bytes(*, pool_state, sender, token0, token1, amount0=100, amount1=80, direction=True,
                sqrt_price=100, liquidity=500, tick=5, fee0=1, fee1=0, transfer0=0, transfer1=0,
                bool_byte=None):
    direction_byte = int(direction) if bool_byte is None else bool_byte
    return (
        RAYDIUM_SWAP_EVENT_DISCRIMINATOR
        + bytes(Pubkey.from_string(pool_state))
        + bytes(Pubkey.from_string(sender))
        + bytes(Pubkey.from_string(token0))
        + bytes(Pubkey.from_string(token1))
        + struct.pack("<QQQQ", amount0, transfer0, amount1, transfer1)
        + bytes([direction_byte])
        + sqrt_price.to_bytes(16, "little")
        + liquidity.to_bytes(16, "little")
        + struct.pack("<iQQ", tick, fee0, fee1)
    )


def artifacts():
    p = pool()
    route_hash = "c" * 64
    snapshot = RaydiumClmmTickArraySnapshot(
        route_message_sha256=route_hash,
        simulation_response_id="sim-event",
        simulation_context_slot=100,
        provider_id="rpc",
        cluster="devnet",
        genesis_hash="d" * 64,
        context_slot=101,
        tick_arrays=(),
        cpi_account_order=(),
    )
    loop = RaydiumClmmSwapLoopResult(
        pool=p,
        tick_array_snapshot=snapshot,
        tick_array_queue=None,
        fee_state=None,
        dynamic_fee_state=None,
        final_iteration=SimpleNamespace(swap_complete=True),
        zero_for_one=True,
        is_base_input=True,
        amount_specified=100,
        sqrt_price_limit_x64=99,
        inner_iterations=2,
        swap_complete=True,
    )
    settlement = RaydiumSwapTransactionSettlement(
        state=SimpleNamespace(pool=p, observation=SimpleNamespace(address=p.observation_key)),
        amounts=RaydiumSwapSettlement(
            amount_0=100,
            amount_1=80,
            trade_fee_0=1,
            trade_fee_1=0,
        ),
        amount_returned=80,
        transfer_fee_0=0,
        transfer_fee_1=0,
    )
    loop_settlement = RaydiumSwapLoopSettlementResult(loop=loop, settlement=settlement)
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
        tick_array_accounts=(),
        amount_raw=100,
        minimum_output_raw=70,
        sqrt_price_limit_x64=99,
        is_base_input=True,
        stack_height=2,
    )
    raw = event_bytes(
        pool_state=p.pool_address,
        sender=cpi.payer,
        token0=cpi.input_token_account,
        token1=cpi.output_token_account,
    )
    return p, cpi, loop_settlement, raw


def test_swap_event_decodes_and_extracts_only_from_raydim_program_invocation():
    p, cpi, loop_settlement, raw = artifacts()
    encoded = base64.b64encode(raw).decode()
    logs = (
        "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 invoke [1]",
        f"Program {RAYDIUM_CLMM_MAINNET_PROGRAM_ID} invoke [2]",
        "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [3]",
        "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA success",
        f"Program data: {encoded}",
        f"Program {RAYDIUM_CLMM_MAINNET_PROGRAM_ID} success",
        "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 success",
    )
    event = extract_raydium_clmm_swap_event(logs)
    assert event.amount_0 == 100
    assert event.amount_1 == 80
    assert event.zero_for_one is True
    result = reconcile_raydium_clmm_swap_event(event, cpi, loop_settlement)
    assert result.matched is True


def test_event_extraction_ignores_nested_token_program_data_and_requires_one_event():
    _, _, _, raw = artifacts()
    encoded = base64.b64encode(raw).decode()
    token_only = (
        "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 invoke [1]",
        f"Program {RAYDIUM_CLMM_MAINNET_PROGRAM_ID} invoke [2]",
        "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [3]",
        f"Program data: {encoded}",
        "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA success",
        f"Program {RAYDIUM_CLMM_MAINNET_PROGRAM_ID} success",
        "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 success",
    )
    with pytest.raises(RaydiumSwapEventError, match="exactly one"):
        extract_raydium_clmm_swap_event(token_only)
    raydium_event = (
        "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 invoke [1]",
        f"Program {RAYDIUM_CLMM_MAINNET_PROGRAM_ID} invoke [2]",
        f"Program data: {encoded}",
        f"Program data: {encoded}",
        f"Program {RAYDIUM_CLMM_MAINNET_PROGRAM_ID} success",
        "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 success",
    )
    with pytest.raises(RaydiumSwapEventError, match="exactly one"):
        extract_raydium_clmm_swap_event(raydium_event)


def test_event_decoder_rejects_invalid_discriminator_length_and_boolean():
    _, cpi, _, raw = artifacts()
    with pytest.raises(RaydiumSwapEventError, match="221 bytes"):
        decode_raydium_clmm_swap_event(raw[:-1])
    with pytest.raises(RaydiumSwapEventError, match="discriminator"):
        decode_raydium_clmm_swap_event(bytes(221))
    bad_bool = event_bytes(
        pool_state=cpi.pool_state,
        sender=cpi.payer,
        token0=cpi.input_token_account,
        token1=cpi.output_token_account,
        bool_byte=2,
    )
    with pytest.raises(RaydiumSwapEventError, match="boolean"):
        decode_raydium_clmm_swap_event(bad_bool)


def test_event_reconciliation_rejects_amount_direction_state_and_transfer_fee_mismatch():
    _, cpi, loop_settlement, raw = artifacts()
    event = decode_raydium_clmm_swap_event(raw)
    with pytest.raises(RaydiumSwapEventError, match="amounts or trade fees"):
        reconcile_raydium_clmm_swap_event(
            replace(event, amount_1=81), cpi, loop_settlement
        )
    with pytest.raises(RaydiumSwapEventError, match="direction"):
        reconcile_raydium_clmm_swap_event(
            replace(event, zero_for_one=False), cpi, loop_settlement
        )
    with pytest.raises(RaydiumSwapEventError, match="transfer fees"):
        reconcile_raydium_clmm_swap_event(
            replace(event, transfer_fee_0=1), cpi, loop_settlement
        )
