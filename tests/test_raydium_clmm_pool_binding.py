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
    RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET,
    RAYDIUM_POOL_STATE_LENGTH,
    RaydiumClmmPoolRouteCheck,
    RaydiumClmmPoolState,
)
from veltrix.raydium_clmm_vaults import RaydiumClmmVaultAccount, RaydiumClmmVaultSnapshot
from veltrix.raydium_clmm_aux_accounts import (
    AMM_CONFIG_DATA_LENGTH,
    OBSERVATION_STATE_DATA_LENGTH,
    RaydiumClmmAmmConfig,
    RaydiumClmmObservationState,
    RaydiumClmmAuxAccountSnapshot,
)
from veltrix.raydium_clmm_tick_arrays import (
    RaydiumClmmTickArray,
    RaydiumClmmTickArraySnapshot,
    RaydiumClmmTickArrayBitmapExtension,
    RaydiumClmmTick,
    RAYDIUM_TICK_ARRAY_DATA_LENGTH,
    RAYDIUM_TICK_ARRAY_DISCRIMINATOR,
    derive_raydium_clmm_tick_array_address,
)
from veltrix.simulation_gate import SimulatedAccountState, SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult
from veltrix.raydium_clmm_simulation_state import (
    RaydiumClmmSimulationStateError,
    build_raydium_clmm_account_commitments,
    evaluate_raydium_clmm_simulation_state_binding,
)


def fixture():
    key = lambda: str(Pubkey.new_unique())
    taker, mint_in, mint_out, pool, vault0, vault1 = (key() for _ in range(6))
    tick = derive_raydium_clmm_tick_array_address(pool_address=pool, start_tick_index=0)
    message = b"approved-raydium-message"
    digest = hashlib.sha256(message).hexdigest()
    pool_words = [0] * 16
    pool_words[8] = 1  # default bitmap bit 512: current array at tick 0
    pool_bytes_mut = bytearray(RAYDIUM_POOL_STATE_LENGTH)
    pool_bytes_mut[73:105] = bytes(Pubkey.from_string(mint_in))
    pool_bytes_mut[105:137] = bytes(Pubkey.from_string(mint_out))
    pool_bytes_mut[235:237] = (10).to_bytes(2, "little")
    pool_bytes_mut[269:273] = (0).to_bytes(4, "little", signed=True)
    for index, word in enumerate(pool_words):
        start = RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET + index * 8
        pool_bytes_mut[start:start + 8] = word.to_bytes(8, "little")
    pool_bytes = bytes(pool_bytes_mut)
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
        account_data_sha256=pool_hash, tick_array_bitmap=tuple(pool_words),
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
    vault0_data = bytearray(165)
    vault0_data[0:32] = bytes(Pubkey.from_string(mint_in))
    vault0_data[32:64] = bytes(Pubkey.from_string(pool))
    vault0_data[64:72] = (1000).to_bytes(8, "little")
    vault0_data[108] = 1
    vault0_data = bytes(vault0_data)
    vault1_data = bytearray(165)
    vault1_data[0:32] = bytes(Pubkey.from_string(mint_out))
    vault1_data[32:64] = bytes(Pubkey.from_string(pool))
    vault1_data[64:72] = (2000).to_bytes(8, "little")
    vault1_data[108] = 1
    vault1_data = bytes(vault1_data)
    vaults = (
        RaydiumClmmVaultAccount(vault0, pool, mint_in, CLASSIC_TOKEN_PROGRAM_ID, pool, 1000, 1, 1, hashlib.sha256(vault0_data).hexdigest()),
        RaydiumClmmVaultAccount(vault1, pool, mint_out, CLASSIC_TOKEN_PROGRAM_ID, pool, 2000, 1, 1, hashlib.sha256(vault1_data).hexdigest()),
    )
    vault_snapshot = RaydiumClmmVaultSnapshot(
        digest, "sim-1", 500, "rpc-primary", "mainnet-beta", "genesis", 502, vaults,
    )
    tick_data_mut = bytearray(RAYDIUM_TICK_ARRAY_DATA_LENGTH)
    tick_data_mut[:8] = RAYDIUM_TICK_ARRAY_DISCRIMINATOR
    tick_data_mut[8:40] = bytes(Pubkey.from_string(pool))
    tick_data_mut[40:44] = (0).to_bytes(4, "little", signed=True)
    tick_data_mut[10_124] = 1
    tick_data = bytes(tick_data_mut)
    tick_array = RaydiumClmmTickArray(
        address=tick, pool_address=pool, start_tick_index=0, tick_spacing=10,
        initialized_tick_count=1, recent_epoch=1,
        ticks=(RaydiumClmmTick(
            tick_index=0, liquidity_net=0, liquidity_gross=1,
            orders_amount=0, part_filled_orders_remaining=0, initialized=True,
        ),),
        data_sha256=hashlib.sha256(tick_data).hexdigest(),
    )
    tick_snapshot = RaydiumClmmTickArraySnapshot(
        digest, "sim-1", 500, "rpc-primary", "mainnet-beta", "genesis", 503,
        (tick_array,), cpi_account_order=(tick,), bitmap_extension_address=None,
    )
    config_data = bytes(AMM_CONFIG_DATA_LENGTH)
    observation_data = bytes(OBSERVATION_STATE_DATA_LENGTH)
    aux_snapshot = RaydiumClmmAuxAccountSnapshot(
        route_message_sha256=digest, simulation_response_id="sim-1",
        simulation_context_slot=500, provider_id="rpc-primary",
        cluster="mainnet-beta", genesis_hash="genesis", context_slot=504,
        amm_configs=(RaydiumClmmAmmConfig(
            address=amm_config, bump=1, index=1, owner=key(),
            protocol_fee_rate=1000, trade_fee_rate=3000,
            tick_spacing=10, fund_fee_rate=1000,
            data_sha256=hashlib.sha256(config_data).hexdigest(),
        ),),
        observations=(RaydiumClmmObservationState(
            address=observation, pool_address=pool, initialized=True,
            recent_epoch=1, observation_index=0, latest_observation_timestamp=999,
            latest_tick_cumulative=0,
            data_sha256=hashlib.sha256(observation_data).hexdigest(),
        ),),
    )
    def simulated_state(address, owner, data, *, lamports=1, post_data=None):
        post_data = data if post_data is None else post_data
        return SimulatedAccountState(
            address=address, pre_owner=owner, post_owner=owner,
            pre_lamports=lamports, post_lamports=lamports,
            pre_executable=False, post_executable=False,
            pre_data=data, post_data=post_data,
            pre_data_sha256=hashlib.sha256(data).hexdigest(),
            post_data_sha256=hashlib.sha256(post_data).hexdigest(),
        )
    post_vault0 = bytearray(vault0_data)
    post_vault0[64:72] = (900).to_bytes(8, "little")
    post_vault1 = bytearray(vault1_data)
    post_vault1[64:72] = (2100).to_bytes(8, "little")
    account_states = (
        simulated_state(pool, RAYDIUM_CLMM_PROGRAM_ID, pool_bytes, lamports=1),
        simulated_state(vault0, CLASSIC_TOKEN_PROGRAM_ID, vault0_data, post_data=bytes(post_vault0)),
        simulated_state(vault1, CLASSIC_TOKEN_PROGRAM_ID, vault1_data, post_data=bytes(post_vault1)),
        simulated_state(tick, RAYDIUM_CLMM_PROGRAM_ID, tick_data),
        simulated_state(amm_config, RAYDIUM_CLMM_PROGRAM_ID, config_data),
        simulated_state(observation, RAYDIUM_CLMM_PROGRAM_ID, observation_data),
    )
    simulation = SimulationEvidence(
        digest, "blockhash", "mainnet-beta", "genesis", "rpc-primary", "sim-1",
        1000, 500, 900, 950, None, 100_000, 5000, (),
        account_states=account_states,
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


def evaluate_simulation_state(values=None, *, validation=None, simulation=None, simulation_gate=None):
    order, cpi, pool_check, pool_evidence, vaults, ticks, aux, base_simulation = values or fixture()
    simulation = simulation or base_simulation
    commitments = build_raydium_clmm_account_commitments(
        cpi, pool_evidence=pool_evidence, vault_snapshot=vaults,
        tick_array_snapshot=ticks, aux_snapshot=aux,
    )
    validation = validation or TransactionValidationResult(
        message_sha256=order.message_sha256 if hasattr(order, "message_sha256") else hashlib.sha256(order.message_bytes).hexdigest(),
        message_size=len(order.message_bytes),
        fee_payer=order.fee_payer,
        required_signers=(order.fee_payer,),
        program_ids=(cpi.program_id,),
        instruction_count=1,
        account_keys=tuple(item.address for item in commitments),
    )
    simulation_gate = simulation_gate or SimulationGateResult(
        approved=True,
        message_sha256=hashlib.sha256(order.message_bytes).hexdigest(),
        provider_id=simulation.provider_id,
        response_id=simulation.response_id,
        context_slot=simulation.context_slot,
        units_consumed=simulation.units_consumed,
        fee_lamports=simulation.fee_lamports,
        verified_token_account_count=0,
    )
    return evaluate_raydium_clmm_simulation_state_binding(
        order,
        validation=validation,
        simulation=simulation,
        simulation_gate=simulation_gate,
        pool_check=pool_check,
        cpi=cpi,
        pool_evidence=pool_evidence,
        vault_snapshot=vaults,
        tick_array_snapshot=ticks,
        aux_snapshot=aux,
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
    with pytest.raises(RaydiumClmmPoolBindingError, match="tick-array|CPI remaining-account order"):
        evaluate(tuple(values))
    values = list(fixture())
    values[5] = __import__("dataclasses").replace(values[5], tick_arrays=())
    with pytest.raises(RaydiumClmmPoolBindingError, match="tick-array|CPI remaining-account order"):
        evaluate(tuple(values))


def test_rejects_stale_or_cross_provider_evidence():
    values = list(fixture())
    values[5] = __import__("dataclasses").replace(values[5], provider_id="rpc-secondary")
    with pytest.raises(RaydiumClmmPoolBindingError, match="provider/cluster|pool and tick-array snapshots"):
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
    from veltrix.raydium_clmm_cpi_tick_arrays import (
        TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR,
        derive_raydium_clmm_tick_array_bitmap_extension_address,
    )
    pool_address = values[2].pool.pool_address
    bitmap = derive_raydium_clmm_tick_array_bitmap_extension_address(pool_address=pool_address)
    positive = (0,) * 112
    negative = (0,) * 112
    bitmap_bytes = (
        TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR
        + bytes(Pubkey.from_string(pool_address))
        + b"".join(word.to_bytes(8, "little") for word in positive)
        + b"".join(word.to_bytes(8, "little") for word in negative)
    )
    bitmap_hash = hashlib.sha256(bitmap_bytes).hexdigest()
    extension = RaydiumClmmTickArrayBitmapExtension(
        address=bitmap, pool_address=pool_address,
        positive_bitmaps=positive, negative_bitmaps=negative,
        data_sha256=bitmap_hash, data_bytes=bitmap_bytes,
    )
    values[1] = __import__("dataclasses").replace(
        values[1], tick_array_accounts=(values[1].tick_array_accounts[0], bitmap)
    )
    values[5] = __import__("dataclasses").replace(
        values[5],
        cpi_account_order=(values[1].tick_array_accounts[0], bitmap),
        bitmap_extension_address=bitmap,
        bitmap_extension_sha256=bitmap_hash,
        bitmap_extension=extension,
    )
    result = evaluate(tuple(values))
    assert result.tick_array_addresses == (values[1].tick_array_accounts[0],)


def test_binds_all_decoded_raydium_accounts_to_same_simulation_pre_state():
    result = evaluate_simulation_state()
    assert result.passed is True
    assert result.execution_ready is False
    assert len(result.bound_account_addresses) == 6
    assert {role for _, role in result.account_roles} == {
        "pool_state", "pool_vault", "tick_array", "amm_config", "observation_state"
    }
    post_hashes = dict(result.post_state_sha256)
    pre_hashes = dict(result.pre_state_sha256)
    assert post_hashes != pre_hashes


def test_rejects_missing_simulated_raydium_account_state():
    values = list(fixture())
    changed = __import__("dataclasses").replace(
        values[7], account_states=values[7].account_states[:-1]
    )
    with pytest.raises(RaydiumClmmSimulationStateError, match="omitted required Raydium"):
        evaluate_simulation_state(tuple(values), simulation=changed)


def test_rejects_simulation_pre_state_hash_that_differs_from_decoded_pool_evidence():
    values = list(fixture())
    states = list(values[7].account_states)
    original = states[0]
    tampered = b"X" + original.pre_data[1:]
    states[0] = __import__("dataclasses").replace(
        original,
        pre_data=tampered,
        pre_data_sha256=hashlib.sha256(tampered).hexdigest(),
    )
    changed = __import__("dataclasses").replace(values[7], account_states=tuple(states))
    with pytest.raises(RaydiumClmmSimulationStateError, match="pre-state does not match decoded pool_state"):
        evaluate_simulation_state(tuple(values), simulation=changed)


def test_rejects_simulation_pre_state_with_wrong_account_owner():
    values = list(fixture())
    states = list(values[7].account_states)
    states[0] = __import__("dataclasses").replace(
        states[0], pre_owner=CLASSIC_TOKEN_PROGRAM_ID, post_owner=CLASSIC_TOKEN_PROGRAM_ID
    )
    changed = __import__("dataclasses").replace(values[7], account_states=tuple(states))
    with pytest.raises(RaydiumClmmSimulationStateError, match="pre-state does not match decoded pool_state"):
        evaluate_simulation_state(tuple(values), simulation=changed)


def test_rejects_raydium_account_missing_from_validated_transaction_keys():
    values = list(fixture())
    commitments = build_raydium_clmm_account_commitments(
        values[1], pool_evidence=values[3], vault_snapshot=values[4],
        tick_array_snapshot=values[5], aux_snapshot=values[6],
    )
    validation = TransactionValidationResult(
        message_sha256=hashlib.sha256(values[0].message_bytes).hexdigest(),
        message_size=len(values[0].message_bytes),
        fee_payer=values[0].fee_payer,
        required_signers=(values[0].fee_payer,),
        program_ids=(values[1].program_id,),
        instruction_count=1,
        account_keys=tuple(item.address for item in commitments[:-1]),
    )
    with pytest.raises(RaydiumClmmSimulationStateError, match="absent from the validated"):
        evaluate_simulation_state(tuple(values), validation=validation)


def test_rejects_post_state_account_layout_length_change():
    values = list(fixture())
    states = list(values[7].account_states)
    original = states[3]
    post_data = original.post_data[:-1]
    states[3] = __import__("dataclasses").replace(
        original,
        post_data=post_data,
        post_data_sha256=hashlib.sha256(post_data).hexdigest(),
    )
    changed = __import__("dataclasses").replace(values[7], account_states=tuple(states))
    with pytest.raises(RaydiumClmmSimulationStateError, match="pre-state does not match decoded tick_array"):
        evaluate_simulation_state(tuple(values), simulation=changed)
