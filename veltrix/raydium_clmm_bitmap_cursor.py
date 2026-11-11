"""Pinned Raydium PoolState bitmap next-initialized-array selection."""
from __future__ import annotations

from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_MAX_TICK,
    RAYDIUM_MIN_TICK,
    RaydiumClmmPoolState,
)
from veltrix.raydium_clmm_tick_array_mutation import (
    RaydiumClmmTickArrayBitmapExtension,
    _extension_location,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    raydium_current_tick_array_start,
)


class RaydiumBitmapCursorError(ValueError):
    """Raised when a PoolState bitmap cursor cannot safely advance."""


def _validate_bitmap_words(rows, *, row_count: int, word_count: int, field: str) -> None:
    if not isinstance(rows, tuple) or len(rows) != row_count:
        raise RaydiumBitmapCursorError(
            f"{field} must contain exactly {row_count} rows"
        )
    for row_index, row in enumerate(rows):
        if not isinstance(row, tuple) or len(row) != word_count:
            raise RaydiumBitmapCursorError(
                f"{field} row {row_index} must contain exactly {word_count} u64 words"
            )
        for word in row:
            if isinstance(word, bool) or not isinstance(word, int) or not 0 <= word < (1 << 64):
                raise RaydiumBitmapCursorError(
                    f"{field} contains a word outside the u64 domain"
                )


def _default_bitmap_initialized(
    pool: RaydiumClmmPoolState,
    start_tick_index: int,
) -> bool:
    tick_count = RAYDIUM_TICK_ARRAY_SIZE * pool.tick_spacing
    offset = start_tick_index // tick_count + 512
    if not 0 <= offset < 1024:
        raise RaydiumBitmapCursorError("tick-array start lies outside the default bitmap")
    word, bit = divmod(offset, 64)
    return bool((pool.tick_array_bitmap[word] >> bit) & 1)


def is_raydium_tick_array_initialized(
    pool: RaydiumClmmPoolState,
    *,
    start_tick_index: int,
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None = None,
) -> bool:
    """Check one initialized bit using the default bitmap or extension bitmap."""
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumBitmapCursorError("decoded Raydium PoolState is required")
    if isinstance(pool.tick_spacing, bool) or not isinstance(pool.tick_spacing, int) or not 1 <= pool.tick_spacing <= 0xFFFF:
        raise RaydiumBitmapCursorError("PoolState tick spacing must be a positive u16")
    if isinstance(start_tick_index, bool) or not isinstance(start_tick_index, int):
        raise RaydiumBitmapCursorError("tick-array start index must be an integer")
    _validate_bitmap_words(
        pool.tick_array_bitmap, row_count=1, word_count=16,
        field="PoolState default bitmap",
    )
    tick_count = RAYDIUM_TICK_ARRAY_SIZE * pool.tick_spacing
    if start_tick_index != raydium_current_tick_array_start(start_tick_index, pool.tick_spacing):
        raise RaydiumBitmapCursorError("tick-array start index is not aligned to its spacing")
    min_start = raydium_current_tick_array_start(RAYDIUM_MIN_TICK, pool.tick_spacing)
    max_start = raydium_current_tick_array_start(RAYDIUM_MAX_TICK, pool.tick_spacing)
    if not min_start <= start_tick_index <= max_start:
        raise RaydiumBitmapCursorError("tick-array start index is outside the protocol range")
    default_boundary = tick_count * 512
    if -default_boundary <= start_tick_index < default_boundary:
        if len(pool.tick_array_bitmap) != 16:
            raise RaydiumBitmapCursorError("PoolState default bitmap must contain 16 u64 words")
        return _default_bitmap_initialized(pool, start_tick_index)
    if pool.tick_spacing >= 15:
        return False
    if bitmap_extension is None:
        raise RaydiumBitmapCursorError("bitmap extension is required for this tick-array range")
    if bitmap_extension.pool_address != pool.pool_address:
        raise RaydiumBitmapCursorError("bitmap extension belongs to a different pool")
    _validate_bitmap_words(
        bitmap_extension.positive_bitmaps, row_count=14, word_count=8,
        field="positive bitmap extension",
    )
    _validate_bitmap_words(
        bitmap_extension.negative_bitmaps, row_count=14, word_count=8,
        field="negative bitmap extension",
    )
    side, group, bit_index = _extension_location(
        start_tick_index, pool.tick_spacing
    )
    rows = (
        bitmap_extension.positive_bitmaps
        if side == "positive"
        else bitmap_extension.negative_bitmaps
    )
    word, bit = divmod(bit_index, 64)
    return bool((rows[group][word] >> bit) & 1)


def next_initialized_raydium_tick_array_start_index(
    pool: RaydiumClmmPoolState,
    *,
    last_tick_array_start_index: int,
    zero_for_one: bool,
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None = None,
) -> int | None:
    """Mirror PoolState::next_tick_array_index_with_extension_info.

    Search advances one array at a time across the default and extension
    bitmap domains. It returns the first set bit in swap direction, None at
    the protocol boundary, and fails closed if a required extension is absent.
    """
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumBitmapCursorError("decoded Raydium PoolState is required")
    if not isinstance(zero_for_one, bool):
        raise RaydiumBitmapCursorError("swap direction must be boolean")
    if isinstance(last_tick_array_start_index, bool) or not isinstance(last_tick_array_start_index, int):
        raise RaydiumBitmapCursorError("last tick-array start must be an integer")
    spacing = pool.tick_spacing
    if isinstance(spacing, bool) or not isinstance(spacing, int) or not 1 <= spacing <= 0xFFFF:
        raise RaydiumBitmapCursorError("PoolState tick spacing must be a positive u16")
    tick_count = RAYDIUM_TICK_ARRAY_SIZE * spacing
    last = raydium_current_tick_array_start(last_tick_array_start_index, spacing)
    minimum = raydium_current_tick_array_start(RAYDIUM_MIN_TICK, spacing)
    maximum = raydium_current_tick_array_start(RAYDIUM_MAX_TICK, spacing)
    candidate = last - tick_count if zero_for_one else last + tick_count

    while minimum <= candidate <= maximum:
        if is_raydium_tick_array_initialized(
            pool,
            start_tick_index=candidate,
            bitmap_extension=bitmap_extension,
        ):
            return candidate
        candidate += -tick_count if zero_for_one else tick_count
    return None


def first_raydium_tick_array_start_index(
    pool: RaydiumClmmPoolState,
    *,
    zero_for_one: bool,
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None = None,
) -> tuple[bool, int]:
    """Mirror PoolState::first_tick_array_index_with_extension_info."""
    current = raydium_current_tick_array_start(
        pool.tick_current, pool.tick_spacing
    )
    current_initialized = is_raydium_tick_array_initialized(
        pool,
        start_tick_index=current,
        bitmap_extension=bitmap_extension,
    )
    if current_initialized:
        return True, current
    next_start = next_initialized_raydium_tick_array_start_index(
        pool,
        last_tick_array_start_index=current,
        zero_for_one=zero_for_one,
        bitmap_extension=bitmap_extension,
    )
    if next_start is None:
        raise RaydiumBitmapCursorError("no initialized tick array remains in the swap direction")
    return False, next_start
