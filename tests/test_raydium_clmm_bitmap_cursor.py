"""Pinned source tests for default and extension tick-array bitmap traversal."""
from dataclasses import replace

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState
from veltrix.raydium_clmm_tick_array_mutation import (
    RaydiumClmmTickArrayBitmapExtension,
    derive_raydium_tick_array_bitmap_extension_address,
    _extension_location,
)
from veltrix.raydium_clmm_bitmap_cursor import (
    RaydiumBitmapCursorError,
    first_raydium_tick_array_start_index,
    is_raydium_tick_array_initialized,
    next_initialized_raydium_tick_array_start_index,
)


def pool(*, spacing=1, current_tick=0, bitmap=None):
    return RaydiumClmmPoolState(
        pool_address=str(Pubkey.new_unique()),
        bump=1,
        amm_config=str(Pubkey.new_unique()),
        pool_creator=str(Pubkey.new_unique()),
        token_mint_0=str(Pubkey.new_unique()),
        token_mint_1=str(Pubkey.new_unique()),
        token_vault_0=str(Pubkey.new_unique()),
        token_vault_1=str(Pubkey.new_unique()),
        observation_key=str(Pubkey.new_unique()),
        mint_decimals_0=6,
        mint_decimals_1=6,
        tick_spacing=spacing,
        liquidity=1,
        sqrt_price_x64=1 << 64,
        tick_current=current_tick,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data_sha256="",
        tick_array_bitmap=bitmap if bitmap is not None else (0,) * 16,
    )


def extension_for(p, *, positive=None, negative=None):
    zeros = tuple((0,) * 8 for _ in range(14))
    return RaydiumClmmTickArrayBitmapExtension(
        address=derive_raydium_tick_array_bitmap_extension_address(p.pool_address),
        pool_address=p.pool_address,
        positive_bitmaps=positive if positive is not None else zeros,
        negative_bitmaps=negative if negative is not None else zeros,
        data_sha256="",
    )


def test_first_array_returns_current_array_when_its_bitmap_bit_is_set():
    p = pool(spacing=1)
    words = list(p.tick_array_bitmap)
    words[8] |= 1  # start 0 -> compressed bit 512
    p = replace(p, tick_array_bitmap=tuple(words))
    assert first_raydium_tick_array_start_index(
        p, zero_for_one=True
    ) == (True, 0)


@pytest.mark.parametrize(
    "zero_for_one,start,word,bit",
    [
        (True, -60, 7, 63),
        (False, 60, 8, 1),
    ],
)
def test_next_default_bitmap_search_finds_first_set_bit_in_direction(
    zero_for_one, start, word, bit
):
    p = pool(spacing=1)
    words = list(p.tick_array_bitmap)
    words[word] |= 1 << bit
    p = replace(p, tick_array_bitmap=tuple(words))
    assert next_initialized_raydium_tick_array_start_index(
        p,
        last_tick_array_start_index=0,
        zero_for_one=zero_for_one,
    ) == start


def test_first_array_skips_uninitialized_current_array_and_finds_next_bit():
    p = pool(spacing=1)
    words = list(p.tick_array_bitmap)
    words[7] |= (1 << 63) | (1 << 62)
    p = replace(p, tick_array_bitmap=tuple(words))
    assert first_raydium_tick_array_start_index(
        p, zero_for_one=True
    ) == (False, -60)
    assert next_initialized_raydium_tick_array_start_index(
        p, last_tick_array_start_index=-60, zero_for_one=True
    ) == -120


def test_missing_extension_is_reported_only_when_spacing_requires_it():
    p = pool(spacing=10, current_tick=306_600)
    with pytest.raises(RaydiumBitmapCursorError, match="extension is required"):
        next_initialized_raydium_tick_array_start_index(
            p, last_tick_array_start_index=306_600, zero_for_one=False
        )
    wide = pool(spacing=15, current_tick=442_800)
    assert next_initialized_raydium_tick_array_start_index(
        wide, last_tick_array_start_index=442_800, zero_for_one=False
    ) is None


@pytest.mark.parametrize("start,side", [(307_200, "positive"), (-307_800, "negative")])
def test_extension_bitmap_search_crosses_default_boundary(start, side):
    p = pool(spacing=10, current_tick=start - 600 if side == "positive" else start + 600)
    positive = [list(row) for row in tuple((0,) * 8 for _ in range(14))]
    negative = [list(row) for row in tuple((0,) * 8 for _ in range(14))]
    bitmap_side, group, bit_index = _extension_location(start, p.tick_spacing)
    rows = positive if bitmap_side == "positive" else negative
    word, bit = divmod(bit_index, 64)
    rows[group][word] |= 1 << bit
    extension = extension_for(
        p,
        positive=tuple(tuple(row) for row in positive),
        negative=tuple(tuple(row) for row in negative),
    )
    result = next_initialized_raydium_tick_array_start_index(
        p,
        last_tick_array_start_index=p.tick_current,
        zero_for_one=(side == "negative"),
        bitmap_extension=extension,
    )
    assert result == start


def test_extension_must_belong_to_the_same_pool():
    p = pool(spacing=10, current_tick=306_600)
    extension = extension_for(replace(p, pool_address=str(Pubkey.new_unique())))
    with pytest.raises(RaydiumBitmapCursorError, match="different pool"):
        is_raydium_tick_array_initialized(
            p, start_tick_index=307_200, bitmap_extension=extension
        )
