"""Adversarial tests for Raydium CLMM tick-array and bitmap mutation."""
from dataclasses import replace

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RaydiumClmmPoolState,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    derive_raydium_clmm_tick_array_address,
)
from veltrix.raydium_clmm_tick_array_mutation import (
    RaydiumClmmTickArrayBitmapExtension,
    RaydiumTickArrayMutationError,
    apply_raydium_tick_array_mutation,
    decode_raydium_tick_array_bitmap_extension,
    derive_raydium_tick_array_bitmap_extension_address,
    TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR,
    TICK_ARRAY_BITMAP_EXTENSION_LENGTH,
)


def pool(**overrides):
    values = dict(
        pool_address=str(Pubkey.new_unique()), bump=1,
        amm_config=str(Pubkey.new_unique()), pool_creator=str(Pubkey.new_unique()),
        token_mint_0=str(Pubkey.new_unique()), token_mint_1=str(Pubkey.new_unique()),
        token_vault_0=str(Pubkey.new_unique()), token_vault_1=str(Pubkey.new_unique()),
        observation_key=str(Pubkey.new_unique()), mint_decimals_0=6, mint_decimals_1=6,
        tick_spacing=1, liquidity=100, sqrt_price_x64=1 << 64, tick_current=0,
        status=0, fee_on=0, seed_index=0, account_data_sha256="a" * 64,
    )
    values.update(overrides)
    return RaydiumClmmPoolState(**values)


def array(p, start=0, initialized=0):
    ticks = [RaydiumClmmTick(tick_index=start + i * p.tick_spacing, liquidity_net=0,
        liquidity_gross=0, orders_amount=0, part_filled_orders_remaining=0,
        initialized=False) for i in range(RAYDIUM_TICK_ARRAY_SIZE)]
    for i in range(initialized):
        ticks[i] = replace(ticks[i], liquidity_gross=1, initialized=True)
    return RaydiumClmmTickArray(
        address=derive_raydium_clmm_tick_array_address(pool_address=p.pool_address, start_tick_index=start), pool_address=p.pool_address,
        start_tick_index=start, tick_spacing=p.tick_spacing,
        initialized_tick_count=initialized, recent_epoch=1,
        ticks=tuple(ticks), data_sha256="b" * 64,
    )


def tick(index, *, gross=1, orders=0, part=0):
    return RaydiumClmmTick(
        tick_index=index, liquidity_net=0, liquidity_gross=gross,
        orders_amount=orders, part_filled_orders_remaining=part,
        initialized=(gross > 0 or orders > 0 or part > 0),
    )


def bit(p, start):
    offset = start // (RAYDIUM_TICK_ARRAY_SIZE * p.tick_spacing) + 512
    return bool(p.tick_array_bitmap[offset // 64] & (1 << (offset % 64)))


def test_first_initialized_tick_sets_default_bitmap_and_count():
    p = pool()
    a = array(p)
    result = apply_raydium_tick_array_mutation(p, a, tick_index=0, updated_tick=tick(0))
    assert result.tick_array.initialized_tick_count == 1
    assert result.initialized_before is False and result.initialized_after is True
    assert result.bitmap_changed is True
    assert bit(result.pool, 0) is True


def test_last_tick_removed_clears_default_bitmap_and_count():
    p = pool(tick_array_bitmap=(0,) * 8 + (1,) + (0,) * 7)
    a = array(p, initialized=1)
    result = apply_raydium_tick_array_mutation(p, a, tick_index=0, updated_tick=tick(0, gross=0))
    assert result.tick_array.initialized_tick_count == 0
    assert bit(result.pool, 0) is False
    assert result.bitmap_changed is True


def test_mutating_tick_without_array_initialization_transition_does_not_flip_bitmap():
    p = pool(tick_array_bitmap=(0,) * 8 + (1,) + (0,) * 7)
    a = array(p, initialized=1)
    replacement = replace(a.ticks[0], liquidity_gross=2)
    result = apply_raydium_tick_array_mutation(p, a, tick_index=0, updated_tick=replacement)
    assert result.tick_array.initialized_tick_count == 1
    assert result.bitmap_changed is False
    assert bit(result.pool, 0) is True


def test_stale_bitmap_and_duplicate_flip_attempt_are_rejected():
    p = pool()
    a = array(p, initialized=1)
    with pytest.raises(RaydiumTickArrayMutationError, match="bitmap is stale"):
        apply_raydium_tick_array_mutation(p, a, tick_index=0, updated_tick=tick(0, gross=0))


def test_invalid_tick_array_offset_and_wrong_array_are_rejected():
    p = pool(tick_spacing=10)
    a = array(p)
    with pytest.raises(RaydiumTickArrayMutationError, match="different tick-array"):
        apply_raydium_tick_array_mutation(p, a, tick_index=600, updated_tick=tick(600))
    with pytest.raises(RaydiumTickArrayMutationError, match="invalid tick-array offset"):
        apply_raydium_tick_array_mutation(p, a, tick_index=1, updated_tick=tick(1))


def test_initialized_count_stale_count_is_rejected_before_any_transition():
    p = pool()
    empty = array(p)
    # A zero count cannot be decremented by a valid decoded state; stale records are rejected first.
    with pytest.raises(RaydiumTickArrayMutationError, match="stale before mutation"):
        apply_raydium_tick_array_mutation(p, replace(empty, initialized_tick_count=1),
            tick_index=0, updated_tick=tick(0))
    full = array(p, initialized=RAYDIUM_TICK_ARRAY_SIZE)
    # A 60-slot array cannot overflow u8; count integrity is checked before mutation.
    with pytest.raises(RaydiumTickArrayMutationError, match="stale before mutation"):
        apply_raydium_tick_array_mutation(p, replace(full, initialized_tick_count=59),
            tick_index=0, updated_tick=tick(0))


def test_bitmap_extension_decode_checks_pda_pool_and_layout():
    p = pool()
    address = derive_raydium_tick_array_bitmap_extension_address(p.pool_address)
    data = bytearray(TICK_ARRAY_BITMAP_EXTENSION_LENGTH)
    data[:8] = TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR
    data[8:40] = bytes(Pubkey.from_string(p.pool_address))
    decoded = decode_raydium_tick_array_bitmap_extension(
        address=address, owner=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
        executable=False, data=bytes(data), expected_pool_address=p.pool_address,
    )
    assert decoded.address == address
    assert len(decoded.positive_bitmaps) == 14
    assert len(decoded.negative_bitmaps) == 14



def test_negative_default_bitmap_lower_boundary_stays_in_poolstate_bitmap():
    p = pool(tick_spacing=1)
    start = -(RAYDIUM_TICK_ARRAY_SIZE * 512)
    a = array(p, start=start)
    result = apply_raydium_tick_array_mutation(
        p, a, tick_index=start, updated_tick=tick(start),
    )
    assert result.bitmap_extension is None
    assert result.bitmap_changed is True
    assert result.pool.tick_array_bitmap[0] & 1 == 1

def test_extension_bitmap_mutation_updates_only_the_correct_side():
    p = pool(tick_spacing=1)
    a = array(p, start=60 * 512)
    extension = RaydiumClmmTickArrayBitmapExtension(
        address=derive_raydium_tick_array_bitmap_extension_address(p.pool_address),
        pool_address=p.pool_address,
        positive_bitmaps=((0,) * 8,) * 14,
        negative_bitmaps=((0,) * 8,) * 14,
        data_sha256="c" * 64,
    )
    result = apply_raydium_tick_array_mutation(
        p, a, tick_index=a.start_tick_index, updated_tick=tick(a.start_tick_index),
        bitmap_extension=extension,
    )
    assert result.bitmap_changed is True
    assert any(any(word for word in row) for row in result.bitmap_extension.positive_bitmaps)
    assert not any(any(word for word in row) for row in result.bitmap_extension.negative_bitmaps)


def test_extension_requires_canonical_range_and_account():
    p = pool(tick_spacing=1)
    a = array(p, start=60 * 512)
    with pytest.raises(RaydiumTickArrayMutationError, match="requires its bitmap extension"):
        apply_raydium_tick_array_mutation(p, a, tick_index=a.start_tick_index,
            updated_tick=tick(a.start_tick_index))


def test_bitmap_extension_rejects_bad_discriminator_and_wrong_pool():
    p = pool()
    address = derive_raydium_tick_array_bitmap_extension_address(p.pool_address)
    data = bytearray(TICK_ARRAY_BITMAP_EXTENSION_LENGTH)
    with pytest.raises(RaydiumTickArrayMutationError, match="discriminator"):
        decode_raydium_tick_array_bitmap_extension(
            address=address, owner=RAYDIUM_CLMM_PROGRAM_ID, lamports=1,
            executable=False, data=bytes(data), expected_pool_address=p.pool_address,
        )


def test_negative_extension_boundary_uses_source_asymmetric_bitmap_bit():
    p = pool(tick_spacing=1)
    start = -(60 * 513)
    a = array(p, start=start)
    extension = RaydiumClmmTickArrayBitmapExtension(
        address=derive_raydium_tick_array_bitmap_extension_address(p.pool_address),
        pool_address=p.pool_address,
        positive_bitmaps=((0,) * 8,) * 14,
        negative_bitmaps=((0,) * 8,) * 14,
        data_sha256="d" * 64,
    )
    result = apply_raydium_tick_array_mutation(
        p, a, tick_index=start, updated_tick=tick(start), bitmap_extension=extension,
    )
    assert result.bitmap_changed is True
    assert result.bitmap_extension.negative_bitmaps[0][7] == (1 << 63)
    assert not any(any(word for word in row) for row in result.bitmap_extension.positive_bitmaps)
