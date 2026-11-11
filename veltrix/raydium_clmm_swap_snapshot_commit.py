"""Commit a completed tick iteration and refresh its provenance-bound account snapshot."""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState
from veltrix.raydium_clmm_tick_arrays import (
    RaydiumClmmTickArray,
    RaydiumClmmTickArraySnapshot,
)
from veltrix.raydium_clmm_tick_array_mutation import (
    RaydiumClmmTickArrayBitmapExtension as MutationBitmapExtension,
    RaydiumTickArrayMutationError,
    to_mutation_bitmap_extension,
    update_tick_array_snapshot_bitmap_extension,
)
from veltrix.raydium_clmm_swap_inner_iteration import RaydiumClmmSwapInnerIterationResult
from veltrix.raydium_clmm_tick_array_commit import (
    RaydiumClmmCommittedTickArrayIteration,
    RaydiumClmmTickArrayCommitError,
    commit_raydium_clmm_inner_iteration,
)


class RaydiumClmmSnapshotCommitError(ValueError):
    """Raised when an account-state commit cannot be rebound to its source snapshot."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmCommittedSnapshotIteration:
    commit: RaydiumClmmCommittedTickArrayIteration
    snapshot: RaydiumClmmTickArraySnapshot


def commit_raydium_clmm_inner_iteration_snapshot(
    pool: RaydiumClmmPoolState,
    tick_array_snapshot: RaydiumClmmTickArraySnapshot,
    iteration: RaydiumClmmSwapInnerIterationResult,
    *,
    tick_array_address: str,
    recent_epoch: int,
) -> RaydiumClmmCommittedSnapshotIteration:
    """Commit one boundary tick and replace its account bytes/hash in the same CPI snapshot."""
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumClmmSnapshotCommitError("decoded Raydium PoolState is required")
    if not isinstance(tick_array_snapshot, RaydiumClmmTickArraySnapshot):
        raise RaydiumClmmSnapshotCommitError("verified Raydium tick-array snapshot is required")
    if not isinstance(iteration, RaydiumClmmSwapInnerIterationResult):
        raise RaydiumClmmSnapshotCommitError("completed Raydium inner-loop iteration is required")
    if not iteration.reached_initialized_tick:
        raise RaydiumClmmSnapshotCommitError("snapshot update requires an initialized tick boundary")
    addresses = tuple(array.address for array in tick_array_snapshot.tick_arrays)
    if addresses.count(tick_array_address) != 1:
        raise RaydiumClmmSnapshotCommitError("target TickArrayState must occur exactly once in snapshot")
    cpi_array_order = tuple(
        address for address in tick_array_snapshot.cpi_account_order
        if address != tick_array_snapshot.bitmap_extension_address
    )
    if cpi_array_order != addresses:
        raise RaydiumClmmSnapshotCommitError("snapshot TickArrayState order differs from CPI remaining accounts")
    array = next(item for item in tick_array_snapshot.tick_arrays if item.address == tick_array_address)
    if array.pool_address != pool.pool_address or tick_array_snapshot.bitmap_extension_address and (
        tick_array_snapshot.bitmap_extension is None
    ):
        raise RaydiumClmmSnapshotCommitError("snapshot account identities are not bound to the supplied PoolState")

    mutation_extension = None
    try:
        if tick_array_snapshot.bitmap_extension is not None:
            mutation_extension = to_mutation_bitmap_extension(
                tick_array_snapshot.bitmap_extension
            )
        committed = commit_raydium_clmm_inner_iteration(
            pool,
            array,
            iteration,
            recent_epoch=recent_epoch,
            bitmap_extension=mutation_extension,
        )
        updated_snapshot = update_tick_array_snapshot_bitmap_extension(
            tick_array_snapshot,
            committed.mutation.bitmap_extension,
        )
    except (RaydiumTickArrayCommitError, RaydiumTickArrayMutationError) as exc:
        raise RaydiumClmmSnapshotCommitError(f"snapshot-bound account commit failed: {exc}") from exc

    updated_arrays = tuple(
        committed.mutation.tick_array if item.address == tick_array_address else item
        for item in tick_array_snapshot.tick_arrays
    )
    updated_snapshot = replace(updated_snapshot, tick_arrays=updated_arrays)
    updated_order = tuple(
        address for address in updated_snapshot.cpi_account_order
        if address != updated_snapshot.bitmap_extension_address
    )
    if updated_order != tuple(item.address for item in updated_snapshot.tick_arrays):
        raise RaydiumClmmSnapshotCommitError("post-commit snapshot no longer preserves CPI account order")

    return RaydiumClmmCommittedSnapshotIteration(
        commit=committed,
        snapshot=updated_snapshot,
    )
