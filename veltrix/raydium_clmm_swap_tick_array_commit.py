"""Commit one completed Raydium inner-loop tick transition into TickArrayState."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState, encode_raydium_clmm_pool_state
from veltrix.raydium_clmm_tick_arrays import (
    RaydiumClmmTickArray,
    RaydiumClmmTickArrayBitmapExtension,
    encode_raydium_clmm_tick_array,
)
from veltrix.raydium_clmm_tick_array_mutation import (
    RaydiumTickArrayMutationError,
    RaydiumTickArrayMutationResult,
    apply_raydium_tick_array_mutation,
    encode_raydium_tick_array_bitmap_extension,
)
from veltrix.raydium_clmm_swap_inner_iteration import RaydiumClmmSwapInnerIterationResult


class RaydiumSwapTickArrayCommitError(ValueError):
    """Raised when an inner-loop tick transition cannot be committed atomically."""


@dataclass(frozen=True, slots=True)
class RaydiumSwapTickArrayCommitResult:
    pool: RaydiumClmmPoolState
    tick_array: RaydiumClmmTickArray
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None
    mutation: RaydiumTickArrayMutationResult | None
    committed: bool


def commit_raydium_swap_inner_tick_array(
    pool: RaydiumClmmPoolState,
    tick_array: RaydiumClmmTickArray,
    iteration: RaydiumClmmSwapInnerIterationResult,
    *,
    recent_epoch: int,
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None = None,
) -> RaydiumSwapTickArrayCommitResult:
    """Persist the updated TickState only after the inner iteration completes."""
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumSwapTickArrayCommitError("decoded Raydium PoolState is required")
    if not isinstance(tick_array, RaydiumClmmTickArray):
        raise RaydiumSwapTickArrayCommitError("decoded Raydium TickArrayState is required")
    if not isinstance(iteration, RaydiumClmmSwapInnerIterationResult):
        raise RaydiumSwapTickArrayCommitError("completed Raydium inner-loop result is required")
    if isinstance(recent_epoch, bool) or not isinstance(recent_epoch, int) or not 0 <= recent_epoch < (1 << 64):
        raise RaydiumSwapTickArrayCommitError("recent epoch must fit u64")
    if iteration.reached_initialized_tick is False:
        return RaydiumSwapTickArrayCommitResult(
            pool=pool,
            tick_array=tick_array,
            bitmap_extension=bitmap_extension,
            mutation=None,
            committed=False,
        )
    if iteration.reached_initialized_tick is not True:
        raise RaydiumSwapTickArrayCommitError("initialized-tick boundary flag must be boolean")
    try:
        mutation = apply_raydium_tick_array_mutation(
            pool,
            tick_array,
            tick_index=iteration.tick_state.tick_index,
            updated_tick=iteration.tick_state,
            recent_epoch=recent_epoch,
            bitmap_extension=bitmap_extension,
        )
    except RaydiumTickArrayMutationError as exc:
        raise RaydiumSwapTickArrayCommitError(f"TickArrayState mutation failed: {exc}") from exc

    encoded_array = encode_raydium_clmm_tick_array(mutation.tick_array)
    if hashlib.sha256(encoded_array).hexdigest() != mutation.tick_array.data_sha256:
        raise RaydiumSwapTickArrayCommitError("committed TickArrayState hash does not match serialized bytes")
    if mutation.pool.account_data:
        encoded_pool = encode_raydium_clmm_pool_state(mutation.pool)
        if encoded_pool != mutation.pool.account_data:
            raise RaydiumSwapTickArrayCommitError("committed PoolState bytes differ from lossless serialization")
        if hashlib.sha256(encoded_pool).hexdigest() != mutation.pool.account_data_sha256:
            raise RaydiumSwapTickArrayCommitError("committed PoolState hash does not match serialized bytes")
    if mutation.bitmap_extension is not None and mutation.bitmap_extension.data_bytes:
        encoded_extension = encode_raydium_tick_array_bitmap_extension(mutation.bitmap_extension)
        if hashlib.sha256(encoded_extension).hexdigest() != mutation.bitmap_extension.data_sha256:
            raise RaydiumSwapTickArrayCommitError("committed bitmap-extension hash does not match serialized bytes")

    return RaydiumSwapTickArrayCommitResult(
        pool=mutation.pool,
        tick_array=mutation.tick_array,
        bitmap_extension=mutation.bitmap_extension,
        mutation=mutation,
        committed=True,
    )
