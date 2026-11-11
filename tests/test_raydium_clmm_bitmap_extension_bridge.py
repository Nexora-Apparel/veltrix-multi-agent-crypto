import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_tick_array_mutation import (
    TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR,
    RaydiumTickArrayMutationError,
    RaydiumClmmTickArrayBitmapExtension as MutationBitmapExtension,
    derive_raydium_tick_array_bitmap_extension_address,
    encode_raydium_tick_array_bitmap_extension,
    to_mutation_bitmap_extension,
    to_snapshot_bitmap_extension,
    update_tick_array_snapshot_bitmap_extension,
)
from veltrix.raydium_clmm_tick_arrays import (
    RaydiumClmmTickArrayBitmapExtension as SnapshotBitmapExtension,
    RaydiumClmmTickArraySnapshot,
)


def extension_fixture():
    pool = str(Pubkey.new_unique())
    address = derive_raydium_tick_array_bitmap_extension_address(pool)
    positive = [0] * 112
    negative = [0] * 112
    positive[0] = 1
    negative[111] = 1 << 63
    raw = (
        TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR
        + bytes(Pubkey.from_string(pool))
        + b"".join(word.to_bytes(8, "little") for word in positive)
        + b"".join(word.to_bytes(8, "little") for word in negative)
    )
    digest = hashlib.sha256(raw).hexdigest()
    snapshot_extension = SnapshotBitmapExtension(
        address=address,
        pool_address=pool,
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
        cpi_account_order=(address,),
        bitmap_extension_address=address,
        bitmap_extension_sha256=digest,
        bitmap_extension=snapshot_extension,
    )
    return snapshot, snapshot_extension


def test_bitmap_extension_bridge_roundtrips_exact_flat_and_nested_words():
    snapshot, flat = extension_fixture()
    mutation = to_mutation_bitmap_extension(flat)
    assert mutation.positive_bitmaps[0][0] == 1
    assert mutation.negative_bitmaps[13][7] == 1 << 63
    assert encode_raydium_tick_array_bitmap_extension(mutation) == flat.data_bytes

    converted = to_snapshot_bitmap_extension(mutation)
    assert converted.positive_bitmaps == flat.positive_bitmaps
    assert converted.negative_bitmaps == flat.negative_bitmaps
    assert converted.data_bytes == flat.data_bytes
    assert converted.data_sha256 == flat.data_sha256

    updated_snapshot = update_tick_array_snapshot_bitmap_extension(snapshot, mutation)
    assert updated_snapshot.bitmap_extension_address == flat.address
    assert updated_snapshot.bitmap_extension_sha256 == flat.data_sha256
    assert updated_snapshot.bitmap_extension.data_bytes == flat.data_bytes
    assert updated_snapshot.cpi_account_order == snapshot.cpi_account_order


def test_bitmap_extension_bridge_rejects_stale_hash_words_and_noncanonical_pda():
    _, flat = extension_fixture()
    with pytest.raises(RaydiumTickArrayMutationError, match="SHA-256"):
        to_mutation_bitmap_extension(
            SnapshotBitmapExtension(
                address=flat.address,
                pool_address=flat.pool_address,
                positive_bitmaps=flat.positive_bitmaps,
                negative_bitmaps=flat.negative_bitmaps,
                data_sha256="0" * 64,
                data_bytes=flat.data_bytes,
            )
        )
    with pytest.raises(RaydiumTickArrayMutationError, match="words"):
        to_mutation_bitmap_extension(
            SnapshotBitmapExtension(
                address=flat.address,
                pool_address=flat.pool_address,
                positive_bitmaps=(True,) + flat.positive_bitmaps[1:],
                negative_bitmaps=flat.negative_bitmaps,
                data_sha256=flat.data_sha256,
                data_bytes=flat.data_bytes,
            )
        )
    with pytest.raises(RaydiumTickArrayMutationError, match="PDA"):
        to_mutation_bitmap_extension(
            SnapshotBitmapExtension(
                address=str(Pubkey.new_unique()),
                pool_address=flat.pool_address,
                positive_bitmaps=flat.positive_bitmaps,
                negative_bitmaps=flat.negative_bitmaps,
                data_sha256=flat.data_sha256,
                data_bytes=flat.data_bytes,
            )
        )


def test_snapshot_bridge_cannot_drop_an_extension_in_the_cpi_account_list():
    snapshot, _ = extension_fixture()
    with pytest.raises(RaydiumTickArrayMutationError, match="cannot discard"):
        update_tick_array_snapshot_bitmap_extension(snapshot, None)
