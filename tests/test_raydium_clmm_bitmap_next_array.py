import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_bitmap_traversal import (
    RaydiumClmmBitmapTraversalError,
    next_initialized_tick_array_start_index,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RaydiumClmmPoolState,
)
from veltrix.raydium_clmm_tick_array_mutation import (
    TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR,
    derive_raydium_tick_array_bitmap_extension_address,
)
from veltrix.raydium_clmm_tick_arrays import (
    RaydiumClmmTickArrayBitmapExtension,
    RaydiumClmmTickArraySnapshot,
)


def key():
    return str(Pubkey.new_unique())


def pool(*, spacing, bitmap=None):
    words = tuple(bitmap if bitmap is not None else (0,) * 16)
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
        tick_spacing=spacing,
        liquidity=1,
        sqrt_price_x64=1 << 64,
        tick_current=0,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data_sha256="0" * 64,
        tick_array_bitmap=words,
    )


def set_default_bit(words, start, spacing):
    tick_count = 60 * spacing
    bit_index = start // tick_count + 512
    words[bit_index // 64] |= 1 << (bit_index % 64)


def extension_snapshot(pool_address, *, positive=None, negative=None):
    positive = list(positive if positive is not None else (0,) * 112)
    negative = list(negative if negative is not None else (0,) * 112)
    raw = (
        TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR
        + bytes(Pubkey.from_string(pool_address))
        + b"".join(word.to_bytes(8, "little") for word in positive)
        + b"".join(word.to_bytes(8, "little") for word in negative)
    )
    address = derive_raydium_tick_array_bitmap_extension_address(pool_address)
    digest = hashlib.sha256(raw).hexdigest()
    extension = RaydiumClmmTickArrayBitmapExtension(
        address=address,
        pool_address=pool_address,
        positive_bitmaps=tuple(positive),
        negative_bitmaps=tuple(negative),
        data_sha256=digest,
        data_bytes=raw,
    )
    snapshot = RaydiumClmmTickArraySnapshot(
        route_message_sha256="a" * 64,
        simulation_response_id="sim",
        simulation_context_slot=100,
        provider_id="rpc",
        cluster="devnet",
        genesis_hash="b" * 64,
        context_slot=101,
        tick_arrays=(),
        bitmap_extension_address=address,
        bitmap_extension_sha256=digest,
        bitmap_extension=extension,
    )
    return snapshot


def test_next_initialized_array_searches_strictly_in_each_direction():
    words = [0] * 16
    set_default_bit(words, -600, 10)
    set_default_bit(words, 0, 10)
    set_default_bit(words, 600, 10)
    p = pool(spacing=10, bitmap=words)
    snapshot = RaydiumClmmTickArraySnapshot(
        route_message_sha256="a" * 64,
        simulation_response_id="sim",
        simulation_context_slot=100,
        provider_id="rpc",
        cluster="devnet",
        genesis_hash="b" * 64,
        context_slot=101,
        tick_arrays=(),
    )
    assert next_initialized_tick_array_start_index(
        p, tick_array_snapshot=snapshot, last_tick_array_start_index=0, zero_for_one=True
    ) == -600
    assert next_initialized_tick_array_start_index(
        p, tick_array_snapshot=snapshot, last_tick_array_start_index=0, zero_for_one=False
    ) == 600


def test_next_initialized_array_returns_none_when_default_bitmap_range_is_exhausted():
    p = pool(spacing=15)
    snapshot = RaydiumClmmTickArraySnapshot(
        route_message_sha256="a" * 64,
        simulation_response_id="sim",
        simulation_context_slot=100,
        provider_id="rpc",
        cluster="devnet",
        genesis_hash="b" * 64,
        context_slot=101,
        tick_arrays=(),
    )
    assert next_initialized_tick_array_start_index(
        p, tick_array_snapshot=snapshot, last_tick_array_start_index=0, zero_for_one=True
    ) is None
    assert next_initialized_tick_array_start_index(
        p, tick_array_snapshot=snapshot, last_tick_array_start_index=0, zero_for_one=False
    ) is None


def test_next_initialized_array_requires_extension_for_low_spacing_outside_default_bitmap():
    p = pool(spacing=1)
    snapshot = RaydiumClmmTickArraySnapshot(
        route_message_sha256="a" * 64,
        simulation_response_id="sim",
        simulation_context_slot=100,
        provider_id="rpc",
        cluster="devnet",
        genesis_hash="b" * 64,
        context_slot=101,
        tick_arrays=(),
    )
    with pytest.raises(RaydiumClmmBitmapTraversalError, match="without extension"):
        next_initialized_tick_array_start_index(
            p, tick_array_snapshot=snapshot, last_tick_array_start_index=0, zero_for_one=False
        )


def test_next_initialized_array_reads_positive_and_negative_extension_bits():
    p = pool(spacing=1)
    boundary = 512 * 60

    positive = [0] * 112
    positive[0] = 1
    positive_snapshot = extension_snapshot(p.pool_address, positive=positive)
    assert next_initialized_tick_array_start_index(
        p,
        tick_array_snapshot=positive_snapshot,
        last_tick_array_start_index=0,
        zero_for_one=False,
    ) == boundary

    negative = [0] * 112
    negative[7] = 1 << 63
    negative_snapshot = extension_snapshot(p.pool_address, negative=negative)
    assert next_initialized_tick_array_start_index(
        p,
        tick_array_snapshot=negative_snapshot,
        last_tick_array_start_index=0,
        zero_for_one=True,
    ) == -(boundary + 60)


def test_next_initialized_array_rejects_stale_extension_hash_and_unaligned_cursor():
    p = pool(spacing=1)
    snapshot = extension_snapshot(p.pool_address)
    bad_extension = RaydiumClmmTickArrayBitmapExtension(
        address=snapshot.bitmap_extension.address,
        pool_address=snapshot.bitmap_extension.pool_address,
        positive_bitmaps=snapshot.bitmap_extension.positive_bitmaps,
        negative_bitmaps=snapshot.bitmap_extension.negative_bitmaps,
        data_sha256="0" * 64,
        data_bytes=snapshot.bitmap_extension.data_bytes,
    )
    bad_snapshot = RaydiumClmmTickArraySnapshot(
        route_message_sha256=snapshot.route_message_sha256,
        simulation_response_id=snapshot.simulation_response_id,
        simulation_context_slot=snapshot.simulation_context_slot,
        provider_id=snapshot.provider_id,
        cluster=snapshot.cluster,
        genesis_hash=snapshot.genesis_hash,
        context_slot=snapshot.context_slot,
        tick_arrays=(),
        bitmap_extension_address=snapshot.bitmap_extension_address,
        bitmap_extension_sha256="0" * 64,
        bitmap_extension=bad_extension,
    )
    with pytest.raises(RaydiumClmmBitmapTraversalError, match="raw bitmap extension"):
        next_initialized_tick_array_start_index(
            p, tick_array_snapshot=bad_snapshot, last_tick_array_start_index=0, zero_for_one=False
        )
    default_pool = pool(spacing=15)
    default_snapshot = RaydiumClmmTickArraySnapshot(
        route_message_sha256="a" * 64,
        simulation_response_id="sim",
        simulation_context_slot=100,
        provider_id="rpc",
        cluster="devnet",
        genesis_hash="b" * 64,
        context_slot=101,
        tick_arrays=(),
    )
    assert next_initialized_tick_array_start_index(
        default_pool,
        tick_array_snapshot=default_snapshot,
        last_tick_array_start_index=1,
        zero_for_one=False,
    ) is None
