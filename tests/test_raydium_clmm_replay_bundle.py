"""Adversarial account-set binding tests for deterministic Raydium replay."""
from dataclasses import replace
import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_observation import (
    RAYDIUM_OBSERVATION_COUNT,
    RaydiumClmmObservation,
    RaydiumClmmObservationState,
    derive_raydium_clmm_observation_address,
    encode_raydium_clmm_observation_state,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RaydiumClmmPoolState,
    encode_raydium_clmm_pool_state,
    derive_raydium_clmm_pool_address,
)
from veltrix.raydium_clmm_tick_array_mutation import (
    RaydiumClmmTickArrayBitmapExtension,
    derive_raydium_tick_array_bitmap_extension_address,
    encode_raydium_tick_array_bitmap_extension,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    derive_raydium_clmm_tick_array_address,
    encode_raydium_clmm_tick_array,
)
from veltrix.raydium_clmm_vaults import (
    CLASSIC_SPL_TOKEN_PROGRAM_ID,
    RaydiumClmmVaultAccount,
    derive_raydium_clmm_vault_address,
    encode_raydium_clmm_vault_token_account,
)
from veltrix.raydium_clmm_replay_bundle import (
    RaydiumReplayBundleError,
    build_raydium_clmm_replay_account_bundle,
)


def fixture():
    program = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
    config = Pubkey.new_unique()
    mint0, mint1 = sorted((Pubkey.new_unique(), Pubkey.new_unique()), key=bytes)
    creator = Pubkey.new_unique()
    vault0 = derive_raydium_clmm_vault_address(
        pool_address="11111111111111111111111111111111", mint=str(mint0)
    )
    vault1 = derive_raydium_clmm_vault_address(
        pool_address="11111111111111111111111111111111", mint=str(mint1)
    )
    pool_address, bump = Pubkey.find_program_address(
        [b"pool", bytes(config), bytes(mint0), bytes(mint1)], program
    )
    vault0 = derive_raydium_clmm_vault_address(
        pool_address=str(pool_address), mint=str(mint0)
    )
    vault1 = derive_raydium_clmm_vault_address(
        pool_address=str(pool_address), mint=str(mint1)
    )
    observation_address, _ = derive_raydium_clmm_observation_address(str(pool_address))
    pool = RaydiumClmmPoolState(
        pool_address=str(pool_address),
        bump=bump,
        amm_config=str(config),
        pool_creator=str(creator),
        token_mint_0=str(mint0),
        token_mint_1=str(mint1),
        token_vault_0=vault0,
        token_vault_1=vault1,
        observation_key=observation_address,
        mint_decimals_0=6,
        mint_decimals_1=9,
        tick_spacing=10,
        liquidity=1_000_000,
        sqrt_price_x64=1 << 64,
        tick_current=0,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data_sha256=hashlib.sha256(bytes(1544)).hexdigest(),
        account_data=bytes(1544),
    )
    pool_bytes = encode_raydium_clmm_pool_state(pool)
    pool = replace(
        pool,
        account_data=pool_bytes,
        account_data_sha256=hashlib.sha256(pool_bytes).hexdigest(),
    )

    ticks = tuple(
        RaydiumClmmTick(
            tick_index=index * pool.tick_spacing,
            liquidity_net=0,
            liquidity_gross=0,
            orders_amount=0,
            part_filled_orders_remaining=0,
            initialized=False,
        )
        for index in range(RAYDIUM_TICK_ARRAY_SIZE)
    )
    tick_array = RaydiumClmmTickArray(
        address=derive_raydium_clmm_tick_array_address(
            pool_address=pool.pool_address, start_tick_index=0
        ),
        pool_address=pool.pool_address,
        start_tick_index=0,
        tick_spacing=pool.tick_spacing,
        initialized_tick_count=0,
        recent_epoch=1,
        ticks=ticks,
        data_sha256="",
    )
    tick_bytes = encode_raydium_clmm_tick_array(tick_array)
    tick_array = replace(
        tick_array, data_sha256=hashlib.sha256(tick_bytes).hexdigest()
    )

    observations = tuple(
        RaydiumClmmObservation(0, 0, bytes(32))
        for _ in range(RAYDIUM_OBSERVATION_COUNT)
    )
    observation = RaydiumClmmObservationState(
        address=observation_address,
        initialized=False,
        recent_epoch=1,
        observation_index=0,
        pool_id=pool.pool_address,
        observations=observations,
        padding=bytes(32),
        data_sha256="",
    )
    observation_bytes = encode_raydium_clmm_observation_state(observation)
    observation = replace(
        observation,
        data_sha256=hashlib.sha256(observation_bytes).hexdigest(),
    )

    vaults = []
    for address, mint, amount in (
        (vault0, str(mint0), 10_000),
        (vault1, str(mint1), 20_000),
    ):
        raw = bytearray(165)
        raw[0:32] = bytes(Pubkey.from_string(mint))
        raw[32:64] = bytes(pool_address)
        raw[64:72] = amount.to_bytes(8, "little")
        raw[108] = 1
        raw = bytes(raw)
        vault = RaydiumClmmVaultAccount(
            address=address,
            pool_address=str(pool_address),
            mint=mint,
            token_program_id=CLASSIC_SPL_TOKEN_PROGRAM_ID,
            token_authority=str(pool_address),
            amount_raw=amount,
            state=1,
            lamports=1,
            data_sha256=hashlib.sha256(raw).hexdigest(),
            account_data=raw,
        )
        assert encode_raydium_clmm_vault_token_account(vault) == raw
        vaults.append(vault)
    return pool, tick_array, observation, tuple(vaults)


def test_binds_pool_tick_array_observation_and_both_vaults_to_one_committed_bundle():
    pool, tick_array, observation, vaults = fixture()
    bundle = build_raydium_clmm_replay_account_bundle(
        pool,
        tick_arrays=(tick_array,),
        observation=observation,
        vaults=vaults,
    )
    assert bundle.source_commit == pool.source_commit
    assert bundle.execution_ready is False
    assert len(bundle.account_commitments) == 5
    assert len(bundle.bundle_sha256) == 64
    assert bundle.pool.pool_address == pool.pool_address


def test_bundle_commitment_is_order_independent_for_tick_arrays_and_vaults():
    pool, tick_array, observation, vaults = fixture()
    first = build_raydium_clmm_replay_account_bundle(
        pool, tick_arrays=(tick_array,), observation=observation, vaults=vaults
    )
    second = build_raydium_clmm_replay_account_bundle(
        pool, tick_arrays=(tick_array,), observation=observation,
        vaults=tuple(reversed(vaults)),
    )
    assert first.bundle_sha256 == second.bundle_sha256


def test_rejects_vault_amount_that_differs_from_committed_token_account_bytes():
    pool, tick_array, observation, vaults = fixture()
    changed = replace(vaults[0], amount_raw=vaults[0].amount_raw + 1)
    with pytest.raises(RaydiumReplayBundleError, match="amount differs"):
        build_raydium_clmm_replay_account_bundle(
            pool, tick_arrays=(tick_array,), observation=observation,
            vaults=(changed, vaults[1]),
        )


def test_rejects_cross_pool_observation_and_duplicate_tick_array():
    pool, tick_array, observation, vaults = fixture()
    wrong = replace(observation, pool_id=str(Pubkey.new_unique()))
    with pytest.raises(RaydiumReplayBundleError, match="different pool"):
        build_raydium_clmm_replay_account_bundle(
            pool, tick_arrays=(tick_array,), observation=wrong, vaults=vaults
        )
    with pytest.raises(RaydiumReplayBundleError, match="duplicate"):
        build_raydium_clmm_replay_account_bundle(
            pool, tick_arrays=(tick_array, tick_array),
            observation=observation, vaults=vaults,
        )


def test_rejects_incomplete_or_stale_account_sets():
    pool, tick_array, observation, vaults = fixture()
    with pytest.raises(RaydiumReplayBundleError, match="at least one"):
        build_raydium_clmm_replay_account_bundle(
            pool, tick_arrays=(), observation=observation, vaults=vaults
        )
    with pytest.raises(RaydiumReplayBundleError, match="exactly the two"):
        build_raydium_clmm_replay_account_bundle(
            pool, tick_arrays=(tick_array,), observation=observation, vaults=vaults[:1]
        )
    changed = replace(tick_array, data_sha256="0" * 64)
    with pytest.raises(RaydiumReplayBundleError, match="commitment"):
        build_raydium_clmm_replay_account_bundle(
            pool, tick_arrays=(changed,), observation=observation, vaults=vaults
        )


def test_bitmap_extension_must_be_bound_to_the_same_pool_and_committed_bytes():
    pool, tick_array, observation, vaults = fixture()
    empty = tuple((0,) * 8 for _ in range(14))
    extension = RaydiumClmmTickArrayBitmapExtension(
        address=derive_raydium_tick_array_bitmap_extension_address(pool.pool_address),
        pool_address=pool.pool_address,
        positive_bitmaps=empty,
        negative_bitmaps=empty,
        data_sha256="",
    )
    raw = encode_raydium_tick_array_bitmap_extension(extension)
    extension = replace(extension, data_sha256=hashlib.sha256(raw).hexdigest())
    bundle = build_raydium_clmm_replay_account_bundle(
        pool, tick_arrays=(tick_array,), observation=observation,
        vaults=vaults, bitmap_extension=extension,
    )
    assert len(bundle.account_commitments) == 6
    wrong = replace(extension, pool_address=str(Pubkey.new_unique()))
    with pytest.raises(RaydiumReplayBundleError, match="different pool"):
        build_raydium_clmm_replay_account_bundle(
            pool, tick_arrays=(tick_array,), observation=observation,
            vaults=vaults, bitmap_extension=wrong,
        )


def test_rejects_tick_array_count_that_disagrees_with_default_pool_bitmap():
    pool, tick_array, observation, vaults = fixture()
    words = list(pool.tick_array_bitmap)
    words[8] |= 1  # start tick 0 maps to bit 512 in the default bitmap
    changed_pool = replace(pool, tick_array_bitmap=tuple(words))
    pool_bytes = encode_raydium_clmm_pool_state(changed_pool)
    changed_pool = replace(
        changed_pool,
        account_data=pool_bytes,
        account_data_sha256=hashlib.sha256(pool_bytes).hexdigest(),
    )
    with pytest.raises(RaydiumReplayBundleError, match="bitmap bit"):
        build_raydium_clmm_replay_account_bundle(
            changed_pool, tick_arrays=(tick_array,),
            observation=observation, vaults=vaults,
        )
