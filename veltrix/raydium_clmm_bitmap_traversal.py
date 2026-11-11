"""Reproduce Raydium CLMM initialized tick-array traversal from PoolState bitmaps.

This module proves that the CPI-supplied tick-array accounts contain every
initialized array in the bounded traversal interval, in the exact order
Raydium's pinned bitmap search would select. It is evidence-only: it does not
prove how far the swap consumed liquidity or authorize execution.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RAYDIUM_MAX_TICK,
    RAYDIUM_MIN_TICK,
    RaydiumClmmPoolRouteCheck,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTickArraySnapshot,
)


DEFAULT_BITMAP_ARRAYS = 512
EXTENSION_BITMAP_BLOCKS = 14
EXTENSION_BITMAP_WORDS_PER_BLOCK = 8
U64_MAX = (1 << 64) - 1


class RaydiumClmmBitmapTraversalError(ValueError):
    """Raised when PoolState bitmap evidence cannot prove CPI array traversal."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmBitmapTraversalResult:
    passed: bool
    pool_address: str
    zero_for_one: bool
    current_array_start_index: int
    first_initialized_array_start_index: int
    last_cpi_array_start_index: int
    expected_initialized_array_starts: tuple[int, ...]
    supplied_initialized_array_starts: tuple[int, ...]
    supplied_array_starts: tuple[int, ...]
    bitmap_extension_sha256: str | None
    bounded_interval_complete: bool
    execution_ready: bool = False


def _tick_array_count(tick_spacing: int) -> int:
    if isinstance(tick_spacing, bool) or not isinstance(tick_spacing, int) or tick_spacing <= 0:
        raise RaydiumClmmBitmapTraversalError("pool tick spacing must be a positive integer")
    return RAYDIUM_TICK_ARRAY_SIZE * tick_spacing


def _bitmap_bit(words: tuple[int, ...], bit_index: int) -> bool:
    if not 0 <= bit_index < len(words) * 64:
        raise RaydiumClmmBitmapTraversalError("bitmap bit index is outside the decoded bitmap")
    return bool((words[bit_index // 64] >> (bit_index % 64)) & 1)


def _validate_extension(pool_address: str, extension, snapshot: RaydiumClmmTickArraySnapshot) -> None:
    if extension is None:
        if snapshot.bitmap_extension_address is not None or snapshot.bitmap_extension_sha256 is not None:
            raise RaydiumClmmBitmapTraversalError("bitmap extension address/digest exists without decoded bitmap words")
        return
    if extension.pool_address != pool_address:
        raise RaydiumClmmBitmapTraversalError("bitmap extension words belong to a different pool")
    if extension.address != snapshot.bitmap_extension_address:
        raise RaydiumClmmBitmapTraversalError("decoded bitmap extension address differs from the CPI snapshot")
    if (
        extension.data_sha256 != snapshot.bitmap_extension_sha256
        or not isinstance(extension.data_sha256, str)
        or len(extension.data_sha256) != 64
        or any(character not in "0123456789abcdef" for character in extension.data_sha256)
    ):
        raise RaydiumClmmBitmapTraversalError("decoded bitmap extension digest differs from the CPI snapshot")
    if (
        len(extension.positive_bitmaps) != EXTENSION_BITMAP_BLOCKS * EXTENSION_BITMAP_WORDS_PER_BLOCK
        or len(extension.negative_bitmaps) != EXTENSION_BITMAP_BLOCKS * EXTENSION_BITMAP_WORDS_PER_BLOCK
        or any(isinstance(word, bool) or not isinstance(word, int) or not 0 <= word <= U64_MAX
               for word in (*extension.positive_bitmaps, *extension.negative_bitmaps))
    ):
        raise RaydiumClmmBitmapTraversalError("bitmap extension words do not match the pinned 14x512-bit layout")


def _is_initialized(pool, extension, start: int, tick_count: int) -> bool:
    boundary = DEFAULT_BITMAP_ARRAYS * tick_count
    if -boundary <= start < boundary:
        compressed = start // tick_count + DEFAULT_BITMAP_ARRAYS
        return _bitmap_bit(pool.tick_array_bitmap, compressed)

    # Raydium's extension is only valid outside the default 1024-array bitmap.
    if extension is None:
        if pool.tick_spacing >= 15:
            return False
        raise RaydiumClmmBitmapTraversalError(
            "CPI traversal crosses the default bitmap boundary without extension bitmap evidence"
        )

    absolute = abs(start)
    block = absolute // boundary - 1
    remainder = absolute % boundary
    if start < 0 and remainder == 0:
        block -= 1
    if not 0 <= block < EXTENSION_BITMAP_BLOCKS:
        raise RaydiumClmmBitmapTraversalError("tick-array start lies outside the pinned extension bitmap range")
    if start >= 0:
        bit = remainder // tick_count
        words = extension.positive_bitmaps
    else:
        bit = 0 if remainder == 0 else DEFAULT_BITMAP_ARRAYS - remainder // tick_count
        words = extension.negative_bitmaps
    return _bitmap_bit(words[block * EXTENSION_BITMAP_WORDS_PER_BLOCK:(block + 1) * EXTENSION_BITMAP_WORDS_PER_BLOCK], bit)


def _valid_tick_array_bounds(tick_count: int) -> tuple[int, int]:
    minimum = (RAYDIUM_MIN_TICK // tick_count) * tick_count
    maximum = (RAYDIUM_MAX_TICK // tick_count) * tick_count
    return minimum, maximum


def verify_raydium_clmm_bitmap_traversal(
    order: JupiterOrderTransaction,
    *,
    pool_check: RaydiumClmmPoolRouteCheck,
    tick_array_snapshot: RaydiumClmmTickArraySnapshot,
) -> RaydiumClmmBitmapTraversalResult:
    """Require the CPI account sequence to cover every initialized bitmap array in its span."""
    if not isinstance(order, JupiterOrderTransaction):
        raise RaydiumClmmBitmapTraversalError("Jupiter order is required")
    if not isinstance(pool_check, RaydiumClmmPoolRouteCheck) or pool_check.passed is not True:
        raise RaydiumClmmBitmapTraversalError("successful Raydium PoolState route evidence is required")
    if not isinstance(tick_array_snapshot, RaydiumClmmTickArraySnapshot):
        raise RaydiumClmmBitmapTraversalError("decoded CPI tick-array snapshot is required")

    pool = pool_check.pool
    if pool.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT:
        raise RaydiumClmmBitmapTraversalError("PoolState source revision is not pinned")
    if pool_check.route_input_mint != order.input_mint or pool_check.route_output_mint != order.output_mint:
        raise RaydiumClmmBitmapTraversalError("pool route mint direction differs from the Jupiter order")
    if pool.tick_array_bitmap is None or len(pool.tick_array_bitmap) != 16:
        raise RaydiumClmmBitmapTraversalError("PoolState default bitmap must contain exactly 1024 bits")
    if any(isinstance(word, bool) or not isinstance(word, int) or not 0 <= word <= U64_MAX
           for word in pool.tick_array_bitmap):
        raise RaydiumClmmBitmapTraversalError("PoolState default bitmap contains invalid u64 words")
    if (
        tick_array_snapshot.route_message_sha256 != hashlib.sha256(order.message_bytes).hexdigest()
        or tick_array_snapshot.provider_id == ""
        or tick_array_snapshot.cluster == ""
        or tick_array_snapshot.genesis_hash == ""
        or tick_array_snapshot.context_slot < tick_array_snapshot.simulation_context_slot
    ):
        raise RaydiumClmmBitmapTraversalError("tick-array snapshot identity or provenance is invalid")
    _validate_extension(pool.pool_address, tick_array_snapshot.bitmap_extension, tick_array_snapshot)
    expected_account_order = tuple(
        address for address in tick_array_snapshot.cpi_account_order
        if address != tick_array_snapshot.bitmap_extension_address
    )
    observed_array_order = tuple(item.address for item in tick_array_snapshot.tick_arrays)
    if (
        not tick_array_snapshot.cpi_account_order
        or len(set(tick_array_snapshot.cpi_account_order)) != len(tick_array_snapshot.cpi_account_order)
        or expected_account_order != observed_array_order
        or (tick_array_snapshot.bitmap_extension_address is not None
            and tick_array_snapshot.bitmap_extension_address not in tick_array_snapshot.cpi_account_order)
    ):
        raise RaydiumClmmBitmapTraversalError("decoded tick arrays do not preserve the exact CPI trailing-account order")

    tick_count = _tick_array_count(pool.tick_spacing)
    current = (pool.tick_current // tick_count) * tick_count
    minimum, maximum = _valid_tick_array_bounds(tick_count)
    starts = tuple(item.start_tick_index for item in tick_array_snapshot.tick_arrays)
    if not starts or len(set(starts)) != len(starts):
        raise RaydiumClmmBitmapTraversalError("CPI tick-array starts are empty or duplicated")
    if any(start % tick_count != 0 or not minimum <= start <= maximum for start in starts):
        raise RaydiumClmmBitmapTraversalError("CPI tick-array start is not a valid pool-aligned array boundary")

    zero_for_one = order.input_mint == pool.token_mint_0
    if zero_for_one:
        if any(right >= left for left, right in zip(starts, starts[1:])):
            raise RaydiumClmmBitmapTraversalError("CPI tick arrays are not strictly descending for zero-for-one")
        if starts[0] > current or starts[-1] > current:
            raise RaydiumClmmBitmapTraversalError("zero-for-one CPI arrays begin above the current pool tick array")
        last = starts[-1]
        candidates = range(current, last - tick_count, -tick_count)
    else:
        if any(right <= left for left, right in zip(starts, starts[1:])):
            raise RaydiumClmmBitmapTraversalError("CPI tick arrays are not strictly ascending for one-for-zero")
        if starts[0] < current or starts[-1] < current:
            raise RaydiumClmmBitmapTraversalError("one-for-zero CPI arrays begin below the current pool tick array")
        last = starts[-1]
        candidates = range(current, last + tick_count, tick_count)

    expected = tuple(
        start for start in candidates
        if minimum <= start <= maximum and _is_initialized(
            pool, tick_array_snapshot.bitmap_extension, start, tick_count
        )
    )
    if not expected:
        raise RaydiumClmmBitmapTraversalError("PoolState bitmap has no initialized array in the CPI traversal interval")

    supplied_set = set(starts)
    missing = tuple(start for start in expected if start not in supplied_set)
    if missing:
        raise RaydiumClmmBitmapTraversalError(
            "CPI tick-array accounts omit initialized PoolState bitmap boundaries: "
            + ",".join(str(item) for item in missing)
        )
    supplied_initialized = tuple(start for start in starts if start in set(expected))
    if supplied_initialized != expected:
        raise RaydiumClmmBitmapTraversalError("CPI initialized tick-array sequence differs from PoolState bitmap traversal")

    # Require the first initialized array Raydium would select from the current tick
    # to be present; uninitialized candidate accounts before it may be skipped by
    # the pinned swap loop, but cannot conceal an earlier initialized boundary.
    first = expected[0]
    if first not in supplied_set:
        raise RaydiumClmmBitmapTraversalError("CPI omits the first initialized tick array selected by Raydium")

    return RaydiumClmmBitmapTraversalResult(
        passed=True,
        pool_address=pool.pool_address,
        zero_for_one=zero_for_one,
        current_array_start_index=current,
        first_initialized_array_start_index=first,
        last_cpi_array_start_index=last,
        expected_initialized_array_starts=expected,
        supplied_initialized_array_starts=supplied_initialized,
        supplied_array_starts=starts,
        bitmap_extension_sha256=tick_array_snapshot.bitmap_extension_sha256,
        bounded_interval_complete=True,
        execution_ready=False,
    )
