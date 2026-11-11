"""Reproduce Raydium CLMM initialized tick-array traversal from PoolState bitmaps.

This module proves that the CPI-supplied tick-array accounts contain every
initialized array in the bounded traversal interval, in the exact order
Raydium's pinned bitmap search would select. It is evidence-only: it does not
prove how far the swap consumed liquidity or authorize execution.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct

from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolEvidenceSnapshot
from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_POOL_STATE_LENGTH,
    RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET,
    RAYDIUM_POOL_TICK_ARRAY_BITMAP_WORDS,
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RAYDIUM_MAX_TICK,
    RAYDIUM_MIN_TICK,
    RaydiumClmmPoolRouteCheck,
    RaydiumClmmPoolState,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTickArraySnapshot,
    raydium_current_tick_array_start,
)


DEFAULT_BITMAP_ARRAYS = 512
EXTENSION_BITMAP_BLOCKS = 14
EXTENSION_BITMAP_WORDS_PER_BLOCK = 8
EXTENSION_BITMAP_SEED = b"pool_tick_array_bitmap_extension"
EXTENSION_BITMAP_DISCRIMINATOR = hashlib.sha256(b"account:TickArrayBitmapExtension").digest()[:8]
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
    raw = extension.data_bytes
    if (
        not isinstance(raw, bytes)
        or len(raw) != 1832
        or raw[:8] != hashlib.sha256(b"account:TickArrayBitmapExtension").digest()[:8]
        or raw[8:40] != bytes(Pubkey.from_string(pool_address))
        or extension.data_sha256 != snapshot.bitmap_extension_sha256
        or not isinstance(extension.data_sha256, str)
        or len(extension.data_sha256) != 64
        or any(character not in "0123456789abcdef" for character in extension.data_sha256)
        or hashlib.sha256(raw).hexdigest() != extension.data_sha256
    ):
        raise RaydiumClmmBitmapTraversalError("raw bitmap extension bytes, pool identity or SHA-256 digest are inconsistent")
    words_per_side = EXTENSION_BITMAP_BLOCKS * EXTENSION_BITMAP_WORDS_PER_BLOCK
    raw_positive = tuple(
        int.from_bytes(raw[40 + index * 8:48 + index * 8], "little")
        for index in range(words_per_side)
    )
    negative_start = 40 + words_per_side * 8
    raw_negative = tuple(
        int.from_bytes(raw[negative_start + index * 8:negative_start + (index + 1) * 8], "little")
        for index in range(words_per_side)
    )
    if extension.positive_bitmaps != raw_positive or extension.negative_bitmaps != raw_negative:
        raise RaydiumClmmBitmapTraversalError("decoded bitmap words differ from the retained raw extension account")
    if (
        len(extension.positive_bitmaps) != EXTENSION_BITMAP_BLOCKS * EXTENSION_BITMAP_WORDS_PER_BLOCK
        or len(extension.negative_bitmaps) != EXTENSION_BITMAP_BLOCKS * EXTENSION_BITMAP_WORDS_PER_BLOCK
        or any(isinstance(word, bool) or not isinstance(word, int) or not 0 <= word <= U64_MAX
               for word in (*extension.positive_bitmaps, *extension.negative_bitmaps))
    ):
        raise RaydiumClmmBitmapTraversalError("bitmap extension words do not match the pinned 14x512-bit layout")
    try:
        pool_key = Pubkey.from_string(pool_address)
        program_key = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
        canonical_address = str(Pubkey.find_program_address(
            [EXTENSION_BITMAP_SEED, bytes(pool_key)], program_key
        )[0])
    except Exception as exc:
        raise RaydiumClmmBitmapTraversalError("bitmap extension PDA derivation failed") from exc
    if extension.address != canonical_address:
        raise RaydiumClmmBitmapTraversalError("bitmap extension address is not the canonical pool PDA")
    encoded = (
        EXTENSION_BITMAP_DISCRIMINATOR
        + bytes(pool_key)
        + b"".join(word.to_bytes(8, "little") for word in extension.positive_bitmaps)
        + b"".join(word.to_bytes(8, "little") for word in extension.negative_bitmaps)
    )
    if hashlib.sha256(encoded).hexdigest() != extension.data_sha256:
        raise RaydiumClmmBitmapTraversalError("decoded bitmap extension words do not match the owner-verified account digest")


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
    pool_evidence: DexPoolEvidenceSnapshot,
    tick_array_snapshot: RaydiumClmmTickArraySnapshot,
) -> RaydiumClmmBitmapTraversalResult:
    """Require the CPI account sequence to cover every initialized bitmap array in its span."""
    if not isinstance(order, JupiterOrderTransaction):
        raise RaydiumClmmBitmapTraversalError("Jupiter order is required")
    if not isinstance(pool_check, RaydiumClmmPoolRouteCheck) or pool_check.passed is not True:
        raise RaydiumClmmBitmapTraversalError("successful Raydium PoolState route evidence is required")
    if not isinstance(pool_evidence, DexPoolEvidenceSnapshot):
        raise RaydiumClmmBitmapTraversalError("owner-verified PoolState account evidence is required")
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
    order_digest = hashlib.sha256(order.message_bytes).hexdigest()
    if (
        tick_array_snapshot.route_message_sha256 != order_digest
        or pool_evidence.route_message_sha256 != order_digest
        or pool_evidence.simulation_response_id != tick_array_snapshot.simulation_response_id
        or pool_evidence.simulation_context_slot != tick_array_snapshot.simulation_context_slot
        or pool_evidence.provider_id != tick_array_snapshot.provider_id
        or pool_evidence.cluster != tick_array_snapshot.cluster
        or pool_evidence.genesis_hash != tick_array_snapshot.genesis_hash
        or tick_array_snapshot.provider_id == ""
        or tick_array_snapshot.cluster == ""
        or tick_array_snapshot.genesis_hash == ""
        or pool_evidence.context_slot > tick_array_snapshot.context_slot
        or tick_array_snapshot.context_slot < tick_array_snapshot.simulation_context_slot
    ):
        raise RaydiumClmmBitmapTraversalError("pool and tick-array snapshots do not share the same order, simulation and provider provenance")
    pool_accounts = [item for item in pool_evidence.accounts if item.amm_key == pool.pool_address]
    if len(pool_accounts) != 1:
        raise RaydiumClmmBitmapTraversalError("PoolState evidence must contain exactly one account for the decoded pool")
    pool_account = pool_accounts[0]
    if (
        pool_account.data_length != RAYDIUM_POOL_STATE_LENGTH
        or len(pool_account.data_bytes) != RAYDIUM_POOL_STATE_LENGTH
        or pool_account.data_sha256 != pool.account_data_sha256
        or hashlib.sha256(pool_account.data_bytes).hexdigest() != pool.account_data_sha256
        or pool_account.owner_program_id != RAYDIUM_CLMM_PROGRAM_ID
        or pool_account.expected_program_id != RAYDIUM_CLMM_PROGRAM_ID
        or pool_account.executable is not False
        or pool_account.lamports <= 0
    ):
        raise RaydiumClmmBitmapTraversalError("PoolState bitmap is not bound to the owner-verified 1544-byte account")
    bitmap_start = RAYDIUM_POOL_TICK_ARRAY_BITMAP_OFFSET
    bitmap_end = bitmap_start + RAYDIUM_POOL_TICK_ARRAY_BITMAP_WORDS * 8
    raw_words = tuple(
        int.from_bytes(pool_account.data_bytes[bitmap_start + index * 8:bitmap_start + (index + 1) * 8], "little")
        for index in range(RAYDIUM_POOL_TICK_ARRAY_BITMAP_WORDS)
    )
    if len(pool.tick_array_bitmap) != RAYDIUM_POOL_TICK_ARRAY_BITMAP_WORDS or raw_words != pool.tick_array_bitmap:
        raise RaydiumClmmBitmapTraversalError("decoded PoolState bitmap words differ from the owner-verified account bytes")
    raw_tick_spacing = int.from_bytes(pool_account.data_bytes[235:237], "little")
    raw_tick_current = struct.unpack_from("<i", pool_account.data_bytes, 269)[0]
    raw_mint_0 = str(Pubkey.from_bytes(pool_account.data_bytes[73:105]))
    raw_mint_1 = str(Pubkey.from_bytes(pool_account.data_bytes[105:137]))
    if (
        raw_tick_spacing != pool.tick_spacing
        or raw_tick_current != pool.tick_current
        or raw_mint_0 != pool.token_mint_0
        or raw_mint_1 != pool.token_mint_1
    ):
        raise RaydiumClmmBitmapTraversalError("PoolState tick spacing, current tick or mint pair differs from owner-verified account bytes")
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
    if starts != expected:
        raise RaydiumClmmBitmapTraversalError("CPI tick-array accounts include uninitialized, skipped or reordered bitmap boundaries")
    if any(item.initialized_tick_count <= 0 for item in tick_array_snapshot.tick_arrays):
        raise RaydiumClmmBitmapTraversalError("CPI tick-array account is marked initialized but has no initialized tick records")
    supplied_initialized = starts

    # This verifier intentionally accepts only the exact initialized-array
    # sequence selected by the bitmap. Although the upstream swap loop can advance
    # through supplied accounts while searching for the next selected array, this
    # gate rejects unused/uninitialized padding accounts rather than broadening the
    # accepted transaction shape without a separate account-consumption proof.
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


def next_initialized_tick_array_start_index(
    pool: RaydiumClmmPoolState,
    *,
    tick_array_snapshot: RaydiumClmmTickArraySnapshot,
    last_tick_array_start_index: int,
    zero_for_one: bool,
) -> int | None:
    """Return the next set bitmap boundary in swap direction, including extension words.

    This is the pinned PoolState::next_tick_array_index_with_extension_info
    search contract. It scans strictly beyond the normalized last array and
    returns the first initialized boundary, or None when the direction is
    exhausted. Missing extension evidence is rejected when the search crosses
    the default bitmap range for a spacing that requires extension coverage.
    """
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumClmmBitmapTraversalError("decoded Raydium PoolState is required")
    if not isinstance(tick_array_snapshot, RaydiumClmmTickArraySnapshot):
        raise RaydiumClmmBitmapTraversalError("tick-array snapshot is required for bitmap extension validation")
    if isinstance(last_tick_array_start_index, bool) or not isinstance(last_tick_array_start_index, int) or not -(1 << 31) <= last_tick_array_start_index < (1 << 31):
        raise RaydiumClmmBitmapTraversalError("last tick-array start index must fit i32")
    if not isinstance(zero_for_one, bool):
        raise RaydiumClmmBitmapTraversalError("swap direction must be boolean")
    tick_count = _tick_array_count(pool.tick_spacing)
    if len(pool.tick_array_bitmap) != RAYDIUM_POOL_TICK_ARRAY_BITMAP_WORDS or any(
        isinstance(word, bool) or not isinstance(word, int) or not 0 <= word <= U64_MAX
        for word in pool.tick_array_bitmap
    ):
        raise RaydiumClmmBitmapTraversalError("PoolState default bitmap must contain exactly sixteen valid u64 words")
    last = raydium_current_tick_array_start(last_tick_array_start_index, pool.tick_spacing)
    minimum, maximum = _valid_tick_array_bounds(tick_count)
    if not minimum <= last <= maximum:
        raise RaydiumClmmBitmapTraversalError("last tick-array start index is outside valid PoolState bitmap bounds")
    extension = tick_array_snapshot.bitmap_extension
    _validate_extension(pool.pool_address, extension, tick_array_snapshot)

    candidate = last - tick_count if zero_for_one else last + tick_count
    while minimum <= candidate <= maximum:
        if _is_initialized(pool, extension, candidate, tick_count):
            return candidate
        candidate += -tick_count if zero_for_one else tick_count
    return None
