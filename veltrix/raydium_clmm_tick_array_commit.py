"""Commit one completed inner-loop tick transition into PoolState/TickArrayState bytes."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_POOL_STATE_LENGTH,
    RaydiumClmmPoolState,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_DATA_LENGTH,
    RaydiumClmmTickArray,
    encode_raydium_clmm_tick_array,
)
from veltrix.raydium_clmm_tick_array_mutation import (
    RaydiumClmmTickArrayBitmapExtension,
    RaydiumTickArrayMutationError,
    RaydiumTickArrayMutationResult,
    apply_raydium_tick_array_mutation,
    encode_raydium_tick_array_bitmap_extension,
)
from veltrix.raydium_clmm_swap_inner_iteration import (
    RaydiumClmmSwapInnerIterationResult,
)


class RaydiumClmmTickArrayCommitError(ValueError):
    """Raised when a completed swap iteration cannot be committed to account bytes."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmCommittedTickArrayIteration:
    mutation: RaydiumTickArrayMutationResult
    pool_data_sha256: str
    tick_array_data_sha256: str
    bitmap_extension_data_sha256: str | None


def commit_raydium_clmm_inner_iteration(
    pool: RaydiumClmmPoolState,
    tick_array: RaydiumClmmTickArray,
    iteration: RaydiumClmmSwapInnerIterationResult,
    *,
    recent_epoch: int,
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None = None,
) -> RaydiumClmmCommittedTickArrayIteration:
    """Persist the final tick from one completed boundary iteration into exact account layouts."""
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumClmmTickArrayCommitError("decoded Raydium PoolState is required")
    if not isinstance(tick_array, RaydiumClmmTickArray):
        raise RaydiumClmmTickArrayCommitError("decoded Raydium TickArrayState is required")
    if not isinstance(iteration, RaydiumClmmSwapInnerIterationResult):
        raise RaydiumClmmTickArrayCommitError("completed Raydium inner-loop iteration is required")
    if not iteration.reached_initialized_tick:
        raise RaydiumClmmTickArrayCommitError("TickArrayState can only be committed after reaching an initialized tick")
    if (
        not isinstance(pool.account_data, bytes)
        or len(pool.account_data) != RAYDIUM_POOL_STATE_LENGTH
        or hashlib.sha256(pool.account_data).hexdigest() != pool.account_data_sha256
    ):
        raise RaydiumClmmTickArrayCommitError("PoolState original bytes and SHA-256 commitment are required")
    if (
        not isinstance(tick_array.data_sha256, str)
        or len(tick_array.data_sha256) != 64
        or any(character not in "0123456789abcdef" for character in tick_array.data_sha256)
    ):
        raise RaydiumClmmTickArrayCommitError("TickArrayState source SHA-256 commitment is invalid")
    if tick_array.pool_address != pool.pool_address or tick_array.tick_spacing != pool.tick_spacing:
        raise RaydiumClmmTickArrayCommitError("TickArrayState does not belong to the supplied PoolState")
    if len(tick_array.ticks) != 60:
        raise RaydiumClmmTickArrayCommitError("TickArrayState must contain exactly 60 tick records")
    tick_index = iteration.tick_state.tick_index
    if not -(1 << 31) <= tick_index < (1 << 31):
        raise RaydiumClmmTickArrayCommitError("iteration tick index must fit i32")

    try:
        mutation = apply_raydium_tick_array_mutation(
            pool,
            tick_array,
            tick_index=tick_index,
            updated_tick=iteration.tick_state,
            recent_epoch=recent_epoch,
            bitmap_extension=bitmap_extension,
        )
        if (
            not isinstance(mutation.pool.account_data, bytes)
            or len(mutation.pool.account_data) != RAYDIUM_POOL_STATE_LENGTH
            or hashlib.sha256(mutation.pool.account_data).hexdigest() != mutation.pool.account_data_sha256
        ):
            raise RaydiumClmmTickArrayCommitError("mutated PoolState bytes do not match their post-state commitment")
        encoded_tick_array = encode_raydium_clmm_tick_array(mutation.tick_array)
        tick_array_hash = hashlib.sha256(encoded_tick_array).hexdigest()
        if mutation.tick_array.data_sha256 != tick_array_hash:
            raise RaydiumClmmTickArrayCommitError("mutated TickArrayState bytes do not match their post-state commitment")
        extension_hash = None
        if mutation.bitmap_extension is not None:
            encoded_extension = encode_raydium_tick_array_bitmap_extension(mutation.bitmap_extension)
            extension_hash = hashlib.sha256(encoded_extension).hexdigest()
            if mutation.bitmap_extension.data_sha256 != extension_hash:
                raise RaydiumClmmTickArrayCommitError("mutated bitmap-extension bytes do not match their post-state commitment")
    except RaydiumTickArrayMutationError as exc:
        raise RaydiumClmmTickArrayCommitError(f"TickArrayState mutation failed: {exc}") from exc

    return RaydiumClmmCommittedTickArrayIteration(
        mutation=mutation,
        pool_data_sha256=mutation.pool.account_data_sha256,
        tick_array_data_sha256=tick_array_hash,
        bitmap_extension_data_sha256=extension_hash,
    )
