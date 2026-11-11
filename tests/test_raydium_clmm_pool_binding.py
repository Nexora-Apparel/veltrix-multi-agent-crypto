"""Adversarial tests for Raydium CPI-to-pool evidence binding."""
import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolAccountEvidence, DexPoolEvidenceSnapshot
from veltrix.jupiter_order import JupiterOrderTransaction, JupiterRouteStep
from veltrix.jupiter_swap_effects import CLASSIC_TOKEN_PROGRAM_ID
from veltrix.raydium_clmm_cpi import (
    RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RaydiumClmmCpiResult,
)
from veltrix.raydium_clmm_pool_binding import (
    RaydiumClmmPoolBindingError,
    evaluate_raydium_clmm_cpi_pool_binding,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RaydiumClmmPoolRouteCheck,
    RaydiumClmmPoolState,
)
from veltrix.raydium_clmm_vaults import RaydiumClmmVaultAccount, RaydiumClmmVaultSnapshot
from veltrix.raydium_clmm_aux_accounts import (
    RaydiumClmmAmmConfig,
    RaydiumClmmObservationState,
    RaydiumClmmAuxAccountSnapshot,
)
from veltrix.raydium_clmm_tick_arrays import (
    RaydiumClmmTickArray,
    RaydiumClmmTickArraySnapshot,
    derive_raydium_clmm_tick_array_address,
)
from veltrix.simulation_gate import SimulationEvidence


def fixture():
    key = lambda: str(Pubkey.new_unique())
    taker, mint_in, mint_out, pool, vault0, vault1 = (key() for _ in range(6))
    tick = derive_raydium_clmm_tick_array_address(pool_address=pool, start_tick_index=0)
    message = b"approved-raydium-message"
    digest = hashlib.sha256(message).hexdigest()
    pool_bytes = b"source-pinned-pool-state"
    pool_hash = hashlib.sha256(pool_bytes).hexdigest()
    order = JupiterOrderTransaction(
        provider_id="jupiter-swap-v2", request_id="order", quote_id="quote",
        observed_at_epoch_ms=1000, taker=taker, fee_payer=taker,
        input_mint=mint_in, output_mint=mint_out, input_amount_raw=100,
        output_amount_raw=190, minimum_output_amount_raw=180,
        price_impact_pct=0.1, fee_bps=0.0, router="metis", transaction_version=0,
        last_valid_block_height=950, recent_blockhash="blockhash",
        message_bytes=message, serialized_transaction_bytes=b"unsigned",
        route_plan=(JupiterRouteStep(pool, "Raydium CLMM", mint_in, mint_out, 100, 190, 100.0, 10_000),),
        signature_fee_lamports=5000, prioritization_fee_lamports=0, rent_fee_lamports=0,
    )
    amm_config = key()
    observation = key()
    pool_state = RaydiumClmmPoolState(
        pool_address=pool, bump=1, amm_config=amm_config, pool_creator=key(),
        token_mint_0=mint_in, token_mint_1=mint_out,
        token_vault_0=vault0, token_vault_1=vault1, observation_key=observation,
        mint_decimals_0=6, mint_decimals_1=6, tick_spacing=10, liquidity=1000,
        sqrt_price_x64=1 << 64, tick_current=0, status=0, fee_on=0, seed_index=0,
        account_data_sha256=pool_hash,
    )
    pool_check = RaydiumClmmPoolRouteCheck(True, pool_state, mint_in, mint_out)
    pool_account = DexPoolAccountEvidence(
        amm_key=pool, dex_label="Raydium CLMM", expected_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID, lamports=1, executable=False,
        data_length=len(pool_bytes), data_sha256=pool_hash, data_bytes=pool_bytes,
    )
    pool_evidence = DexPoolEvidenceSnapshot(
        route_message_sha256=digest, registry_sha256="registry", simulation_response_id="sim-1",
        simulation_context_slot=500, provider_id="rpc-primary", cluster="mainnet-beta",
        genesis_hash="genesis", context_slot=501, accounts=(pool_account,),
    )
    vaults = (
        RaydiumClmmVaultAccount(vault0, pool, mint_in, CLASSIC_TOKEN_PROGRAM_ID, pool, 1000, 1, 1, "a" * 64),
        RaydiumClmmVaultAccount(vault1, pool, mint_out, CLASSIC_TOKEN_PROGRAM_ID, pool, 2000, 1, 1, "b" * 64),
    )
    vault_snapshot = RaydiumClmmVaultSnapshot(
        digest, "sim-1", 500, "rpc-primary", "mainnet-beta", "genesis", 502, vaults,
    )
    tick_array = RaydiumClmmTickArray(
        address=tick, pool_address=pool, start_tick_index=0, tick_spacing=10,
        initialized_tick_count=0, recent_epoch=1, ticks=(), data_sha256="c" * 64,
    )
    tick_snapshot = RaydiumClmmTickArraySnapshot(
        digest, "sim-1", 500, "rpc-primary", "mainnet-beta", "genesis", 503,
        (tick_array,), cpi_account_order=(tick,), bitmap_extension_address=None,
    )
    simulation = SimulationEvidence(
        digest, "blockhash", "mainnet-beta", "genesis", "rpc-primary", "sim-1",
        1000, 500, 900, 950, None, 100_000, 5000, (),
    )
    aux_snapshot = RaydiumClmmAuxAccountSnapshot(
        route_message_sha256=digest, simulation_response_id="sim-1",
        simulation_context_slot=500, provider_id="rpc-primary",
        cluster="mainnet-beta", genesis_hash="genesis", context_slot=504,
        amm_configs=(RaydiumClmmAmmConfig(
            address=amm_config, bump=1, index=1, owner=key(),
            protocol_fee_rate=1000, trade_fee_rate=3000,
            tick_spacing=10, fund_fee_rate=1000, data_sha256="d" * 64,
        ),),
        observations=(RaydiumClmmObservationState(
            address=observation, pool_address=pool, initialized=True,
            recent_epoch=1, observation_index=0, latest_observation_timestamp=999,
            latest_tick_cumulative=0, data_sha256="e" * 64,
        ),),
    )
    cpi = RaydiumClmmCpiResult(
        passed=True, message_sha256=digest, program_id=RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
        outer_instruction_index=0, cpi_instruction_index=0, payer=taker,
        input_token_account=key(), output_token_account=key(),
        amm_config=amm_config, pool_state=pool, input_vault=vault0,
        output_vault=vault1, observation_state=observation,
        tick_array_accounts=(tick,), amount_raw=100, minimum_output_raw=180,
        sqrt_price_limit_x64=1, is_base_input=True, stack_height=2,
    )
    return order, cpi, pool_check, pool_evidence, vault_snapshot, tick_snapshot, aux_snapshot, simulation


def evaluate(values=None):
    order, cpi, pool_check, pool_evidence, vaults, ticks, aux, simulation = values or fixture()
    return evaluate_raydium_clmm_cpi_pool_binding(
        order, cpi=cpi, pool_check=pool_check, pool_evidence=pool_evidence,
        vault_snapshot=vaults, tick_array_snapshot=ticks, aux_snapshot=aux, simulation=simulation,
    )


def test_accepts_exact_same_order_pool_vault_and_tick_evidence():
    result = evaluate()
    assert result.passed is True
    assert result.execution_ready is False
    assert result.source_commit == RAYDIUM_CLMM_SOURCE_COMMIT
    assert result.vault_snapshot_slot == 502
    assert result.tick_array_snapshot_slot == 503


def test_rejects_cpi_pool_not_matching_decoded_pool():
    values = list(fixture())
    values[1] = __import__("dataclasses").replace(values[1], pool_state=str(Pubkey.new_unique()))
    with pytest.raises(RaydiumClmmPoolBindingError, match="CPI pool"):
        evaluate(tuple(values))


def test_rejects_input_output_vault_direction_mismatch():
    values = list(fixture())
    values[1] = __import__("dataclasses").replace(values[1], input_vault=values[1].output_vault)
    with pytest.raises(RaydiumClmmPoolBindingError, match="vault order"):
        evaluate(tuple(values))


def test_rejects_vault_from_different_pool_or_wrong_token_program():
    values = list(fixture())
    wrong = __import__("dataclasses").replace(values[4].vaults[0], pool_address=str(Pubkey.new_unique()))
    values[4] = __import__("dataclasses").replace(values[4], vaults=(wrong, values[4].vaults[1]))
    with pytest.raises(RaydiumClmmPoolBindingError, match="different pool"):
        evaluate(tuple(values))
    values = list(fixture())
    wrong = __import__("dataclasses").replace(values[4].vaults[0], token_program_id=RAYDIUM_CLMM_PROGRAM_ID)
    values[4] = __import__("dataclasses").replace(values[4], vaults=(wrong, values[4].vaults[1]))
    with pytest.raises(RaydiumClmmPoolBindingError, match="token program"):
        evaluate(tuple(values))


def test_rejects_missing_or_extra_tick_array_accounts():
    values = list(fixture())
    values[1] = __import__("dataclasses").replace(values[1], tick_array_accounts=())
    with pytest.raises(RaydiumClmmPoolBindingError, match="tick-array accounts"):
        evaluate(tuple(values))
    values = list(fixture())
    values[5] = __import__("dataclasses").replace(values[5], tick_arrays=())
    with pytest.raises(RaydiumClmmPoolBindingError, match="tick-array accounts"):
        evaluate(tuple(values))


def test_rejects_stale_or_cross_provider_evidence():
    values = list(fixture())
    values[5] = __import__("dataclasses").replace(values[5], provider_id="rpc-secondary")
    with pytest.raises(RaydiumClmmPoolBindingError, match="provider/cluster"):
        evaluate(tuple(values))
    values = list(fixture())
    values[5] = __import__("dataclasses").replace(values[5], context_slot=501)
    with pytest.raises(RaydiumClmmPoolBindingError, match="monotonically fresh"):
        evaluate(tuple(values))


def test_rejects_pool_account_hash_mismatch():
    values = list(fixture())
    pool = __import__("dataclasses").replace(values[2].pool, account_data_sha256="f" * 64)
    values[2] = __import__("dataclasses").replace(values[2], pool=pool)
    with pytest.raises(RaydiumClmmPoolBindingError, match="PoolState hash"):
        evaluate(tuple(values))


def test_rejects_wrong_route_mint_direction():
    values = list(fixture())
    values[2] = __import__("dataclasses").replace(values[2], route_input_mint=str(Pubkey.new_unique()))
    with pytest.raises(RaydiumClmmPoolBindingError, match="mint direction"):
        evaluate(tuple(values))


def test_rejects_cpi_config_or_observation_role_mismatch():
    values = list(fixture())
    values[1] = __import__("dataclasses").replace(values[1], amm_config=str(Pubkey.new_unique()))
    with pytest.raises(RaydiumClmmPoolBindingError, match="AMM config or observation"):
        evaluate(tuple(values))
    values = list(fixture())
    values[1] = __import__("dataclasses").replace(values[1], observation_state=str(Pubkey.new_unique()))
    with pytest.raises(RaydiumClmmPoolBindingError, match="AMM config or observation"):
        evaluate(tuple(values))


def test_rejects_auxiliary_config_tick_spacing_or_observation_pool_mismatch():
    values = list(fixture())
    wrong_config = __import__("dataclasses").replace(values[6].amm_configs[0], tick_spacing=20)
    values[6] = __import__("dataclasses").replace(values[6], amm_configs=(wrong_config,))
    with pytest.raises(RaydiumClmmPoolBindingError, match="tick spacing"):
        evaluate(tuple(values))
    values = list(fixture())
    wrong_observation = __import__("dataclasses").replace(
        values[6].observations[0], pool_address=str(Pubkey.new_unique())
    )
    values[6] = __import__("dataclasses").replace(values[6], observations=(wrong_observation,))
    with pytest.raises(RaydiumClmmPoolBindingError, match="observation account"):
        evaluate(tuple(values))


def test_rejects_tick_array_snapshot_that_omits_cpi_remaining_account_order():
    values = list(fixture())
    values[5] = __import__("dataclasses").replace(
        values[5], cpi_account_order=(), bitmap_extension_address=None
    )
    with pytest.raises(RaydiumClmmPoolBindingError, match="exact CPI remaining-account order"):
        evaluate(tuple(values))


def test_binds_bitmap_extension_without_mistaking_it_for_a_tick_array():
    values = list(fixture())
    bitmap = str(Pubkey.new_unique())
    values[1] = __import__("dataclasses").replace(
        values[1], tick_array_accounts=(values[1].tick_array_accounts[0], bitmap)
    )
    values[5] = __import__("dataclasses").replace(
        values[5],
        cpi_account_order=(values[1].tick_array_accounts[0], bitmap),
        bitmap_extension_address=bitmap,
        bitmap_extension_sha256="f" * 64,
    )
    result = evaluate(tuple(values))
    assert result.tick_array_addresses == (values[1].tick_array_accounts[0],)
