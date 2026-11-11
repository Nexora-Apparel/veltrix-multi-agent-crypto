"""Source-pinned Raydium CLMM tick-array mutation and bitmap transitions.

Ported from Raydium CLMM source commit
6d7a0f82d2f293c553c69aaf943377367c6c4cc6:
  states/tick_array.rs::update_initialized_tick_count/update_tick_state
  states/pool.rs::get_tick_array_offset/flip_tick_array_bit
  states/tickarray_bitmap_extension.rs::flip_tick_array_bit

This module is an immutable replay primitive; it does not submit transactions.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib

from solders.pubkey import Pubkey

from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_MAX_TICK,
    RAYDIUM_MIN_TICK,
    RaydiumClmmPoolState,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    derive_raydium_clmm_tick_array_address,
    raydium_current_tick_array_start,
)

U8_MAX = 0xFF
U64_MAX = (1 << 64) - 1
DEFAULT_BITMAP_BITS = 1024
DEFAULT_BITMAP_HALF = DEFAULT_BITMAP_BITS // 2
EXTENSION_BITMAPS = 14
EXTENSION_BITMAP_BITS = 512
TICK_ARRAY_BITMAP_EXTENSION_SEED = b"pool_tick_array_bitmap_extension"
TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR = hashlib.sha256(
    b"account:TickArrayBitmapExtension"
).digest()[:8]
TICK_ARRAY_BITMAP_EXTENSION_LENGTH = 8 + 32 + (EXTENSION_BITMAPS * 8 * 8 * 2)


class RaydiumTickArrayMutationError(ValueError):
    """Raised when a tick-array or bitmap state transition is inconsistent."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmTickArrayBitmapExtension:
    address: str
    pool_address: str
    positive_bitmaps: tuple[tuple[int, ...], ...]
    negative_bitmaps: tuple[tuple[int, ...], ...]
    data_sha256: str


@dataclass(frozen=True, slots=True)
class RaydiumTickArrayMutationResult:
    pool: RaydiumClmmPoolState
    tick_array: RaydiumClmmTickArray
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None
    initialized_before: bool
    initialized_after: bool
    bitmap_changed: bool


def _integer(value: int, minimum: int, maximum: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RaydiumTickArrayMutationError(f"{field} is outside its integer domain")


def _ticks_per_array(tick_spacing: int) -> int:
    _integer(tick_spacing, 1, 0xFFFF, "tick_spacing")
    return RAYDIUM_TICK_ARRAY_SIZE * tick_spacing


def derive_raydium_tick_array_bitmap_extension_address(pool_address: str) -> str:
    try:
        pool = Pubkey.from_string(pool_address)
        program = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
        address, _ = Pubkey.find_program_address(
            [TICK_ARRAY_BITMAP_EXTENSION_SEED, bytes(pool)], program
        )
        return str(address)
    except Exception as exc:
        raise RaydiumTickArrayMutationError("bitmap-extension PDA derivation failed") from exc


def decode_raydium_tick_array_bitmap_extension(
    *, address: str, owner: str, lamports: int, executable: bool, data: bytes,
    expected_pool_address: str,
) -> RaydiumClmmTickArrayBitmapExtension:
    if owner != RAYDIUM_CLMM_PROGRAM_ID:
        raise RaydiumTickArrayMutationError("bitmap-extension owner is not the pinned Raydium program")
    if executable is not False:
        raise RaydiumTickArrayMutationError("bitmap-extension account must not be executable")
    _integer(lamports, 1, U64_MAX, "bitmap-extension lamports")
    if not isinstance(data, bytes) or len(data) != TICK_ARRAY_BITMAP_EXTENSION_LENGTH:
        raise RaydiumTickArrayMutationError("bitmap-extension account has an invalid byte length")
    if data[:8] != TICK_ARRAY_BITMAP_EXTENSION_DISCRIMINATOR:
        raise RaydiumTickArrayMutationError("bitmap-extension discriminator is invalid")
    try:
        pool = str(Pubkey.from_string(expected_pool_address))
        observed_pool = str(Pubkey.from_bytes(data[8:40]))
        observed_address = str(Pubkey.from_string(address))
    except Exception as exc:
        raise RaydiumTickArrayMutationError("bitmap-extension address or pool identity is invalid") from exc
    if observed_pool != pool:
        raise RaydiumTickArrayMutationError("bitmap extension belongs to a different pool")
    if observed_address != derive_raydium_tick_array_bitmap_extension_address(pool):
        raise RaydiumTickArrayMutationError("bitmap-extension address is not the canonical Raydium PDA")
    values = [
        int.from_bytes(data[offset:offset + 8], "little")
        for offset in range(40, len(data), 8)
    ]
    positive = tuple(tuple(values[i * 8:(i + 1) * 8]) for i in range(EXTENSION_BITMAPS))
    negative_start = EXTENSION_BITMAPS * 8
    negative = tuple(
        tuple(values[negative_start + i * 8:negative_start + (i + 1) * 8])
        for i in range(EXTENSION_BITMAPS)
    )
    return RaydiumClmmTickArrayBitmapExtension(
        address=observed_address, pool_address=pool,
        positive_bitmaps=positive, negative_bitmaps=negative,
        data_sha256=hashlib.sha256(data).hexdigest(),
    )


def _extension_location(start_tick_index: int, tick_spacing: int) -> tuple[str, int, int]:
    ticks_per_array = _ticks_per_array(tick_spacing)
    max_tick_in_bitmap = ticks_per_array * EXTENSION_BITMAP_BITS
    if not -0x7FFFFFFF <= start_tick_index <= 0x7FFFFFFF:
        raise RaydiumTickArrayMutationError("tick-array start index must fit i32")
    if start_tick_index % ticks_per_array:
        raise RaydiumTickArrayMutationError("tick-array start index is not canonically aligned")
    if -max_tick_in_bitmap <= start_tick_index < max_tick_in_bitmap:
        raise RaydiumTickArrayMutationError("tick-array belongs in the PoolState default bitmap")
    if start_tick_index == 0:
        raise RaydiumTickArrayMutationError("zero tick-array start index is invalid for extension")
    if max_tick_in_bitmap >= RAYDIUM_MAX_TICK or -max_tick_in_bitmap <= RAYDIUM_MIN_TICK:
        raise RaydiumTickArrayMutationError("extension bitmap is not valid for this tick spacing")
    min_array_start = (RAYDIUM_MIN_TICK // ticks_per_array) * ticks_per_array
    max_array_start = (RAYDIUM_MAX_TICK // ticks_per_array) * ticks_per_array
    if not min_array_start <= start_tick_index <= max_array_start:
        raise RaydiumTickArrayMutationError("tick-array start index exceeds protocol tick-array bounds")
    magnitude_remainder = abs(start_tick_index) % max_tick_in_bitmap
    bitmap_offset = magnitude_remainder // ticks_per_array
    if start_tick_index < 0 and magnitude_remainder:
        bitmap_offset = EXTENSION_BITMAP_BITS - bitmap_offset
    if not 0 <= bitmap_offset < EXTENSION_BITMAP_BITS:
        raise RaydiumTickArrayMutationError("extension bitmap bit offset is invalid")
    array_offset = abs(start_tick_index) // max_tick_in_bitmap - 1
    if start_tick_index < 0 and magnitude_remainder == 0:
        array_offset -= 1
    if not 0 <= array_offset < EXTENSION_BITMAPS:
        raise RaydiumTickArrayMutationError("extension bitmap array offset is invalid")
    return ("negative" if start_tick_index < 0 else "positive", array_offset, bitmap_offset)


def _read_bitmap_bit(pool: RaydiumClmmPoolState, extension, start: int) -> bool:
    ticks_per_array = _ticks_per_array(pool.tick_spacing)
    if -ticks_per_array * EXTENSION_BITMAP_BITS <= start < ticks_per_array * EXTENSION_BITMAP_BITS:
        offset = start // ticks_per_array + DEFAULT_BITMAP_HALF
        if not 0 <= offset < DEFAULT_BITMAP_BITS:
            raise RaydiumTickArrayMutationError("default bitmap offset is outside 1024 bits")
        word, bit = divmod(offset, 64)
        return bool(pool.tick_array_bitmap[word] & (1 << bit))
    if extension is None:
        raise RaydiumTickArrayMutationError("overflow tick-array requires its bitmap extension")
    side, array_offset, bit_offset = _extension_location(start, pool.tick_spacing)
    rows = extension.negative_bitmaps if side == "negative" else extension.positive_bitmaps
    word, bit = divmod(bit_offset, 64)
    return bool(rows[array_offset][word] & (1 << bit))


def _flip_bitmap(pool: RaydiumClmmPoolState, extension, start: int):
    ticks_per_array = _ticks_per_array(pool.tick_spacing)
    if -ticks_per_array * EXTENSION_BITMAP_BITS <= start < ticks_per_array * EXTENSION_BITMAP_BITS:
        offset = start // ticks_per_array + DEFAULT_BITMAP_HALF
        if not 0 <= offset < DEFAULT_BITMAP_BITS:
            raise RaydiumTickArrayMutationError("default bitmap offset is outside 1024 bits")
        words = list(pool.tick_array_bitmap)
        word, bit = divmod(offset, 64)
        words[word] ^= 1 << bit
        return replace(pool, tick_array_bitmap=tuple(words)), extension
    if extension is None:
        raise RaydiumTickArrayMutationError("overflow tick-array requires its bitmap extension")
    side, array_offset, bit_offset = _extension_location(start, pool.tick_spacing)
    rows = [list(row) for row in (extension.negative_bitmaps if side == "negative" else extension.positive_bitmaps)]
    word, bit = divmod(bit_offset, 64)
    rows[array_offset][word] ^= 1 << bit
    if side == "negative":
        extension = replace(extension, negative_bitmaps=tuple(tuple(row) for row in rows))
    else:
        extension = replace(extension, positive_bitmaps=tuple(tuple(row) for row in rows))
    return pool, extension


def apply_raydium_tick_array_mutation(
    pool: RaydiumClmmPoolState,
    tick_array: RaydiumClmmTickArray,
    *,
    tick_index: int,
    updated_tick: RaydiumClmmTick,
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None = None,
) -> RaydiumTickArrayMutationResult:
    """Replace one TickState, adjust checked initialized count, and flip bitmap only on 0<->1."""
    if not isinstance(pool, RaydiumClmmPoolState) or not isinstance(tick_array, RaydiumClmmTickArray):
        raise RaydiumTickArrayMutationError("decoded Raydium pool and tick-array states are required")
    if not isinstance(updated_tick, RaydiumClmmTick):
        raise RaydiumTickArrayMutationError("replacement TickState is required")
    if tick_array.pool_address != pool.pool_address or tick_array.tick_spacing != pool.tick_spacing:
        raise RaydiumTickArrayMutationError("tick array does not belong to the supplied pool")
    if len(tick_array.ticks) != RAYDIUM_TICK_ARRAY_SIZE:
        raise RaydiumTickArrayMutationError("tick array must contain exactly 60 TickState records")
    if tick_array.address != derive_raydium_clmm_tick_array_address(
        pool_address=pool.pool_address, start_tick_index=tick_array.start_tick_index
    ):
        raise RaydiumTickArrayMutationError("tick-array address is not the canonical Raydium PDA")
    if bitmap_extension is not None:
        if not isinstance(bitmap_extension, RaydiumClmmTickArrayBitmapExtension):
            raise RaydiumTickArrayMutationError("decoded bitmap-extension state is required")
        if (
            bitmap_extension.pool_address != pool.pool_address
            or bitmap_extension.address != derive_raydium_tick_array_bitmap_extension_address(pool.pool_address)
        ):
            raise RaydiumTickArrayMutationError("bitmap extension does not belong to the supplied pool")
        for rows in (bitmap_extension.positive_bitmaps, bitmap_extension.negative_bitmaps):
            if len(rows) != EXTENSION_BITMAPS or any(len(row) != 8 for row in rows):
                raise RaydiumTickArrayMutationError("bitmap extension must contain 14 arrays of eight u64 words")
            for row in rows:
                for word in row:
                    _integer(word, 0, U64_MAX, "bitmap-extension word")
    _integer(tick_index, RAYDIUM_MIN_TICK, RAYDIUM_MAX_TICK, "tick_index")
    _ticks_per_array(pool.tick_spacing)
    canonical_start = raydium_current_tick_array_start(tick_index, pool.tick_spacing)
    if canonical_start != tick_array.start_tick_index:
        raise RaydiumTickArrayMutationError("tick index resolves to a different tick-array start index")
    delta = tick_index - tick_array.start_tick_index
    if delta < 0 or delta % pool.tick_spacing:
        raise RaydiumTickArrayMutationError("tick index has an invalid tick-array offset")
    offset = delta // pool.tick_spacing
    if offset >= RAYDIUM_TICK_ARRAY_SIZE:
        raise RaydiumTickArrayMutationError("tick-array offset exceeds the 60 tick slots")
    if updated_tick.tick_index != tick_index:
        raise RaydiumTickArrayMutationError("replacement tick index differs from the requested slot")
    if updated_tick.initialized is not (updated_tick.liquidity_gross > 0 or updated_tick.orders_amount > 0 or updated_tick.part_filled_orders_remaining > 0):
        raise RaydiumTickArrayMutationError("replacement tick initialized flag disagrees with its source fields")
    _integer(updated_tick.liquidity_net, -(1 << 127), (1 << 127) - 1, "liquidity_net")
    if updated_tick.liquidity_gross < 0 or updated_tick.orders_amount < 0 or updated_tick.part_filled_orders_remaining < 0:
        raise RaydiumTickArrayMutationError("replacement tick contains negative unsigned state")
    if updated_tick.liquidity_gross > U64_MAX or updated_tick.orders_amount > U64_MAX or updated_tick.part_filled_orders_remaining > U64_MAX:
        raise RaydiumTickArrayMutationError("replacement tick exceeds its source u64 domain")
    if updated_tick.liquidity_gross > 0 and abs(updated_tick.liquidity_net) > updated_tick.liquidity_gross:
        raise RaydiumTickArrayMutationError("replacement tick net liquidity exceeds gross liquidity")
    if any(updated_tick.padding):
        raise RaydiumTickArrayMutationError("replacement tick reserved padding must remain zero")
    if tick_array.initialized_tick_count != sum(1 for tick in tick_array.ticks if tick.initialized):
        raise RaydiumTickArrayMutationError("tick-array initialized count is stale before mutation")
    current_tick = tick_array.ticks[offset]
    if current_tick.tick_index not in (tick_index, 0):
        raise RaydiumTickArrayMutationError("existing tick slot index is inconsistent with its offset")
    initialized_before = current_tick.initialized
    initialized_after = updated_tick.initialized
    count = tick_array.initialized_tick_count
    if initialized_before != initialized_after:
        if initialized_after:
            if count >= U8_MAX:
                raise RaydiumTickArrayMutationError("initialized tick count overflows u8")
            count += 1
        else:
            if count <= 0:
                raise RaydiumTickArrayMutationError("initialized tick count underflows u8")
            count -= 1
    if count != 0 and count > RAYDIUM_TICK_ARRAY_SIZE:
        raise RaydiumTickArrayMutationError("initialized tick count exceeds the 60 tick slots")

    bit_is_set = _read_bitmap_bit(pool, bitmap_extension, tick_array.start_tick_index)
    array_was_initialized = tick_array.initialized_tick_count > 0
    if bit_is_set != array_was_initialized:
        raise RaydiumTickArrayMutationError("pool bitmap is stale relative to the pre-mutation tick-array count")
    array_is_initialized = count > 0
    next_pool, next_extension = pool, bitmap_extension
    bitmap_changed = array_was_initialized != array_is_initialized
    if bitmap_changed:
        next_pool, next_extension = _flip_bitmap(pool, bitmap_extension, tick_array.start_tick_index)
    ticks = list(tick_array.ticks)
    ticks[offset] = updated_tick
    next_array = replace(
        tick_array, initialized_tick_count=count, ticks=tuple(ticks), data_sha256=""
    )
    # A mutated in-memory state has no account-byte commitment until serialized.
    return RaydiumTickArrayMutationResult(
        pool=next_pool, tick_array=next_array, bitmap_extension=next_extension,
        initialized_before=array_was_initialized, initialized_after=array_is_initialized,
        bitmap_changed=bitmap_changed,
    )
