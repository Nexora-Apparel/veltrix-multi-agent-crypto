"""Source-ordered Python composition of the pinned Raydium CLMM swap loop.

This orchestrator composes independently tested transitions. It is an offline
state replay only: it does not build, sign, submit, or broadcast transactions.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib

from veltrix.raydium_clmm_dynamic_fee import (
    RaydiumDynamicFeeSwapState,
    initialize_dynamic_fee_swap_state,
)
from veltrix.raydium_clmm_fee_accounting import (
    RaydiumFeeAccountingState,
    RaydiumSwapSettlement,
    settle_raydium_swap_amounts,
)
from veltrix.raydium_clmm_observation import (
    RaydiumClmmObservationState,
    apply_raydium_observation_update,
)
from veltrix.raydium_clmm_pool_post_swap_finalization import finalize_raydium_pool_after_swap

from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RaydiumClmmPoolState,
    update_raydium_pool_reward_infos,
)
from veltrix.raydium_clmm_swap_inner_iteration import (
    RaydiumClmmSwapInnerIterationError,
    RaydiumClmmSwapInnerIterationResult,
    run_raydium_clmm_swap_inner_iteration,
)
from veltrix.raydium_clmm_tick_array_cursor import (
    RaydiumClmmTickArrayQueue,
    RaydiumTickArrayCursorError,
    initialize_raydium_tick_array_queue,
    select_next_tick_for_swap,
)
from veltrix.raydium_clmm_tick_array_mutation import (
    RaydiumClmmTickArrayBitmapExtension as MutationBitmapExtension,
    RaydiumTickArrayMutationError,
    apply_raydium_tick_array_mutation,
    encode_raydium_tick_array_bitmap_extension,
)
from veltrix.raydium_clmm_tick_arrays import (
    RaydiumClmmTickArray,
    RaydiumClmmTickArrayBitmapExtension as SnapshotBitmapExtension,
    RaydiumClmmTickArraySnapshot,
)
from veltrix.raydium_clmm_tick_math import (
    RAYDIUM_MAX_SQRT_PRICE_X64,
    RAYDIUM_MIN_SQRT_PRICE_X64,
    get_sqrt_price_at_tick,
)


class RaydiumClmmSwapReplayError(ValueError):
    """Raised when a complete source-ordered Raydium swap replay cannot finish."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmSwapReplayResult:
    pool: RaydiumClmmPoolState
    observation_state: RaydiumClmmObservationState
    tick_arrays: tuple[RaydiumClmmTickArray, ...]
    bitmap_extension: SnapshotBitmapExtension | None
    fee_state: RaydiumFeeAccountingState
    settlement: RaydiumSwapSettlement
    iterations: tuple[RaydiumClmmSwapInnerIterationResult, ...]
    consumed_tick_array_addresses: tuple[str, ...]
    selected_tick_array_addresses: tuple[str, ...]


def _fee_on_token0(pool: RaydiumClmmPoolState, zero_for_one: bool) -> bool:
    if pool.fee_on == 0:
        return zero_for_one
    if pool.fee_on == 1:
        return True
    if pool.fee_on == 2:
        return False
    raise RaydiumClmmSwapReplayError("PoolState fee_on mode must be 0, 1 or 2")


def _to_mutation_extension(
    extension: SnapshotBitmapExtension | None,
) -> MutationBitmapExtension | None:
    if extension is None:
        return None
    if len(extension.positive_bitmaps) != 14 * 8 or len(extension.negative_bitmaps) != 14 * 8:
        raise RaydiumClmmSwapReplayError("snapshot bitmap extension must contain two flattened 14x8 word arrays")
    positive = tuple(
        tuple(extension.positive_bitmaps[row * 8:(row + 1) * 8])
        for row in range(14)
    )
    negative = tuple(
        tuple(extension.negative_bitmaps[row * 8:(row + 1) * 8])
        for row in range(14)
    )
    return MutationBitmapExtension(
        address=extension.address,
        pool_address=extension.pool_address,
        positive_bitmaps=positive,
        negative_bitmaps=negative,
        data_sha256=extension.data_sha256,
    )


def _to_snapshot_extension(
    extension: MutationBitmapExtension | None,
) -> SnapshotBitmapExtension | None:
    if extension is None:
        return None
    raw = encode_raydium_tick_array_bitmap_extension(extension)
    digest = hashlib.sha256(raw).hexdigest()
    return SnapshotBitmapExtension(
        address=extension.address,
        pool_address=extension.pool_address,
        positive_bitmaps=tuple(word for row in extension.positive_bitmaps for word in row),
        negative_bitmaps=tuple(word for row in extension.negative_bitmaps for word in row),
        data_sha256=digest,
        data_bytes=raw,
    )


def run_raydium_clmm_swap_replay(
    pool: RaydiumClmmPoolState,
    observation_state: RaydiumClmmObservationState,
    tick_array_snapshot: RaydiumClmmTickArraySnapshot,
    *,
    amount_specified: int,
    sqrt_price_limit_x64: int,
    zero_for_one: bool,
    is_base_input: bool,
    block_timestamp: int,
    recent_epoch: int,
    base_fee_rate: int,
    protocol_fee_rate: int,
    fund_fee_rate: int,
    max_inner_iterations: int = 4096,
) -> RaydiumClmmSwapReplayResult:
    """Replay swap_internal ordering over verified Python state snapshots.

    The supplied snapshot preserves the exact CPI tick-array order. Every
    boundary mutation is committed to the immutable in-memory pool/array state
    before the next outer tick search. No partial result is returned on error.
    """
    if not isinstance(pool, RaydiumClmmPoolState) or pool.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT:
        raise RaydiumClmmSwapReplayError("PoolState from the pinned Raydium source revision is required")
    if not isinstance(observation_state, RaydiumClmmObservationState):
        raise RaydiumClmmSwapReplayError("decoded ObservationState is required")
    if not isinstance(tick_array_snapshot, RaydiumClmmTickArraySnapshot):
        raise RaydiumClmmSwapReplayError("decoded CPI TickArrayState snapshot is required")
    if not isinstance(zero_for_one, bool) or not isinstance(is_base_input, bool):
        raise RaydiumClmmSwapReplayError("swap direction and amount mode must be booleans")
    if isinstance(amount_specified, bool) or not isinstance(amount_specified, int) or not 0 < amount_specified < (1 << 64):
        raise RaydiumClmmSwapReplayError("amount_specified must be a positive u64")
    if isinstance(sqrt_price_limit_x64, bool) or not isinstance(sqrt_price_limit_x64, int):
        raise RaydiumClmmSwapReplayError("sqrt price limit must be an integer")
    if not RAYDIUM_MIN_SQRT_PRICE_X64 < sqrt_price_limit_x64 < RAYDIUM_MAX_SQRT_PRICE_X64:
        raise RaydiumClmmSwapReplayError("sqrt price limit is outside the pinned Raydium bounds")
    if zero_for_one:
        if sqrt_price_limit_x64 >= pool.sqrt_price_x64:
            raise RaydiumClmmSwapReplayError("zero-for-one price limit must be below the current sqrt price")
    elif sqrt_price_limit_x64 <= pool.sqrt_price_x64:
        raise RaydiumClmmSwapReplayError("one-for-zero price limit must be above the current sqrt price")
    if pool.status & (1 << 4):
        raise RaydiumClmmSwapReplayError("PoolState has swaps disabled")
    if isinstance(block_timestamp, bool) or not isinstance(block_timestamp, int) or not 0 <= block_timestamp <= 0xFFFFFFFF:
        raise RaydiumClmmSwapReplayError("block timestamp must fit u32")
    if isinstance(recent_epoch, bool) or not isinstance(recent_epoch, int) or not 0 <= recent_epoch < (1 << 64):
        raise RaydiumClmmSwapReplayError("recent epoch must fit u64")
    if isinstance(max_inner_iterations, bool) or not isinstance(max_inner_iterations, int) or max_inner_iterations <= 0:
        raise RaydiumClmmSwapReplayError("max_inner_iterations must be a positive integer")
    if observation_state.pool_id != pool.pool_address or observation_state.address != pool.observation_key:
        raise RaydiumClmmSwapReplayError("ObservationState identity does not match PoolState.pool_id/observation_key")

    try:
        pool = update_raydium_pool_reward_infos(
            pool,
            current_timestamp=block_timestamp,
            recent_epoch=recent_epoch,
        )
    except Exception as exc:
        raise RaydiumClmmSwapReplayError(f"PoolState reward/epoch pre-swap update failed: {exc}") from exc

    try:
        queue, first_array_contains_pool_tick = initialize_raydium_tick_array_queue(
            pool,
            tick_array_snapshot=tick_array_snapshot,
            zero_for_one=zero_for_one,
        )
    except RaydiumTickArrayCursorError as exc:
        raise RaydiumClmmSwapReplayError(f"initial tick-array selection failed: {exc}") from exc

    fee_state = RaydiumFeeAccountingState(
        amount_specified_remaining=amount_specified,
        amount_calculated=0,
        liquidity=pool.liquidity,
    )
    dynamic_state: RaydiumDynamicFeeSwapState | None = None
    if pool.dynamic_fee_info.enabled:
        try:
            dynamic_state = initialize_dynamic_fee_swap_state(
                info=pool.dynamic_fee_info,
                tick=pool.tick_current,
                tick_spacing=pool.tick_spacing,
                base_fee_rate=base_fee_rate,
                block_timestamp=block_timestamp,
            )
        except Exception as exc:
            raise RaydiumClmmSwapReplayError(f"dynamic-fee state initialization failed: {exc}") from exc

    current_tick = pool.tick_current
    current_sqrt_price = pool.sqrt_price_x64
    iterations: list[RaydiumClmmSwapInnerIterationResult] = []
    last_iteration: RaydiumClmmSwapInnerIterationResult | None = None

    for _outer in range(max_inner_iterations):
        if fee_state.amount_specified_remaining == 0 or current_sqrt_price == sqrt_price_limit_x64:
            break
        try:
            traversal = select_next_tick_for_swap(
                pool,
                tick_array_snapshot=tick_array_snapshot,
                queue=queue,
                current_tick_index=current_tick,
                zero_for_one=zero_for_one,
                first_tick_array_contains_pool_tick=first_array_contains_pool_tick,
            )
        except RaydiumTickArrayCursorError as exc:
            raise RaydiumClmmSwapReplayError(f"next initialized tick selection failed: {exc}") from exc
        queue = traversal.queue
        first_array_contains_pool_tick = traversal.first_tick_array_contains_pool_tick
        next_tick_state = traversal.tick
        next_tick = next_tick_state.tick_index
        try:
            next_tick_sqrt_price = get_sqrt_price_at_tick(next_tick)
        except Exception as exc:
            raise RaydiumClmmSwapReplayError(f"next initialized tick price is invalid: {exc}") from exc
        target_price = (
            max(sqrt_price_limit_x64, next_tick_sqrt_price)
            if zero_for_one
            else min(sqrt_price_limit_x64, next_tick_sqrt_price)
        )
        is_price_limit_target = target_price == sqrt_price_limit_x64

        inner_broke = False
        for _inner in range(max_inner_iterations - len(iterations)):
            try:
                iteration = run_raydium_clmm_swap_inner_iteration(
                    pool,
                    fee_state,
                    current_tick=current_tick,
                    next_tick=next_tick,
                    next_tick_state=next_tick_state,
                    sqrt_price_current_x64=current_sqrt_price,
                    sqrt_price_next_x64=next_tick_sqrt_price,
                    target_price_x64=target_price,
                    base_fee_rate=base_fee_rate,
                    protocol_fee_rate=protocol_fee_rate,
                    fund_fee_rate=fund_fee_rate,
                    zero_for_one=zero_for_one,
                    is_base_input=is_base_input,
                    dynamic_fee_state=dynamic_state,
                    is_price_limit_target=is_price_limit_target,
                )
            except RaydiumClmmSwapInnerIterationError as exc:
                raise RaydiumClmmSwapReplayError(f"inner swap iteration failed: {exc}") from exc
            iterations.append(iteration)
            last_iteration = iteration
            fee_state = iteration.fee_state
            current_tick = iteration.tick
            current_sqrt_price = iteration.sqrt_price_x64
            dynamic_state = iteration.dynamic_fee_state
            next_tick_state = iteration.tick_state
            if iteration.should_break:
                inner_broke = True
                break
        if not inner_broke:
            raise RaydiumClmmSwapReplayError("inner swap loop exceeded its deterministic iteration budget")

        if iteration.reached_initialized_tick:
            current_array = queue.current
            if current_array is None:
                raise RaydiumClmmSwapReplayError("initialized tick was reached without an active TickArrayState")
            try:
                mutation = apply_raydium_tick_array_mutation(
                    pool,
                    current_array,
                    tick_index=next_tick,
                    updated_tick=iteration.tick_state,
                    recent_epoch=recent_epoch,
                    bitmap_extension=_to_mutation_extension(tick_array_snapshot.bitmap_extension),
                )
            except RaydiumTickArrayMutationError as exc:
                raise RaydiumClmmSwapReplayError(f"tick-array boundary commit failed: {exc}") from exc
            pool = mutation.pool
            updated_array = mutation.tick_array
            updated_arrays = tuple(
                updated_array if item.address == current_array.address else item
                for item in tick_array_snapshot.tick_arrays
            )
            if not any(item.address == current_array.address for item in tick_array_snapshot.tick_arrays):
                raise RaydiumClmmSwapReplayError("active TickArrayState is missing from its immutable snapshot")
            updated_extension = _to_snapshot_extension(mutation.bitmap_extension)
            tick_array_snapshot = replace(
                tick_array_snapshot,
                tick_arrays=updated_arrays,
                bitmap_extension=updated_extension,
                bitmap_extension_sha256=(
                    updated_extension.data_sha256 if updated_extension is not None else None
                ),
            )
            updated_queue_arrays = tuple(
                updated_array if item.address == current_array.address else item
                for item in queue.arrays
            )
            queue = replace(queue, arrays=updated_queue_arrays, current=updated_array)

        if iteration.swap_complete:
            break
    else:
        raise RaydiumClmmSwapReplayError("outer swap loop exceeded its deterministic iteration budget")

    if last_iteration is None or last_iteration.swap_complete is not True:
        raise RaydiumClmmSwapReplayError(
            "swap replay did not exhaust the specified amount or reach the user price limit"
        )

    if current_tick != pool.tick_current:
        try:
            observation_state = apply_raydium_observation_update(
                observation_state,
                block_timestamp=block_timestamp,
                tick=pool.tick_current,
            )
        except Exception as exc:
            raise RaydiumClmmSwapReplayError(f"ObservationState post-swap update failed: {exc}") from exc

    try:
        settlement = settle_raydium_swap_amounts(
            fee_state,
            amount_specified=amount_specified,
            zero_for_one=zero_for_one,
            is_base_input=is_base_input,
            fee_on_token0=_fee_on_token0(pool, zero_for_one),
        )
        finalized = finalize_raydium_pool_after_swap(
            pool,
            last_iteration,
            zero_for_one=zero_for_one,
        )
    except Exception as exc:
        raise RaydiumClmmSwapReplayError(f"post-swap settlement/finalization failed: {exc}") from exc

    return RaydiumClmmSwapReplayResult(
        pool=finalized.pool,
        observation_state=observation_state,
        tick_arrays=tick_array_snapshot.tick_arrays,
        bitmap_extension=tick_array_snapshot.bitmap_extension,
        fee_state=fee_state,
        settlement=settlement,
        iterations=tuple(iterations),
        consumed_tick_array_addresses=queue.consumed_addresses,
        selected_tick_array_addresses=queue.selected_addresses,
    )
