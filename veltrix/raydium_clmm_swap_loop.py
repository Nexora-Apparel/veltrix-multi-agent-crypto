"""Source-ordered Raydium CLMM outer swap loop over verified tick-array accounts."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib

from veltrix.raydium_clmm_dynamic_fee import (
    RaydiumDynamicFeeError,
    RaydiumDynamicFeeSwapState,
    initialize_dynamic_fee_swap_state,
)
from veltrix.raydium_clmm_fee_accounting import RaydiumFeeAccountingState
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RAYDIUM_POOL_STATE_LENGTH,
    RaydiumClmmPoolState,
    RaydiumClmmPoolStateError,
    encode_raydium_clmm_pool_state,
)
from veltrix.raydium_clmm_rewards import (
    RaydiumRewardUpdateError,
    apply_raydium_pool_reward_update,
)
from veltrix.raydium_clmm_swap_inner_iteration import (
    RaydiumClmmSwapInnerIterationError,
    RaydiumClmmSwapInnerIterationResult,
    run_raydium_clmm_swap_inner_iteration,
)
from veltrix.raydium_clmm_swap_tick_array_commit import (
    RaydiumSwapTickArrayCommitError,
    commit_raydium_swap_inner_tick_array,
)
from veltrix.raydium_clmm_tick_array_mutation import encode_raydium_tick_array_bitmap_extension
from veltrix.raydium_clmm_tick_array_cursor import (
    RaydiumClmmTickArrayQueue,
    RaydiumTickArrayCursorError,
    select_next_tick_for_swap,
)
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTickArraySnapshot
from veltrix.raydium_clmm_tick_math import (
    RAYDIUM_MAX_SQRT_PRICE_X64,
    RAYDIUM_MIN_SQRT_PRICE_X64,
    RaydiumTickMathError,
    get_sqrt_price_at_tick,
)


class RaydiumClmmSwapLoopError(ValueError):
    """Raised when the complete Raydium CLMM swap loop cannot be replayed safely."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmSwapLoopResult:
    pool: RaydiumClmmPoolState
    tick_array_snapshot: RaydiumClmmTickArraySnapshot
    tick_array_queue: RaydiumClmmTickArrayQueue
    fee_state: RaydiumFeeAccountingState
    dynamic_fee_state: RaydiumDynamicFeeSwapState | None
    final_iteration: RaydiumClmmSwapInnerIterationResult
    zero_for_one: bool
    is_base_input: bool
    amount_specified: int
    sqrt_price_limit_x64: int
    inner_iterations: int
    swap_complete: bool


def _u64(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << 64):
        raise RaydiumClmmSwapLoopError(f"{field} must fit u64")


def _fee_on_token0(pool: RaydiumClmmPoolState, zero_for_one: bool) -> bool:
    if pool.fee_on == 0:
        return zero_for_one
    if pool.fee_on == 1:
        return True
    if pool.fee_on == 2:
        return False
    raise RaydiumClmmSwapLoopError("PoolState fee_on mode must be 0, 1 or 2")


def _fee_on_input(pool: RaydiumClmmPoolState, zero_for_one: bool) -> bool:
    if pool.fee_on == 0:
        return True
    if pool.fee_on == 1:
        return zero_for_one
    if pool.fee_on == 2:
        return not zero_for_one
    raise RaydiumClmmSwapLoopError("PoolState fee_on mode must be 0, 1 or 2")


def _replace_current_array(
    queue: RaydiumClmmTickArrayQueue,
    updated_array,
) -> RaydiumClmmTickArrayQueue:
    arrays = list(queue.arrays)
    indexes = [i for i, item in enumerate(arrays) if item.address == updated_array.address]
    if len(indexes) != 1:
        raise RaydiumClmmSwapLoopError("mutated TickArrayState is not present exactly once in the CPI queue")
    arrays[indexes[0]] = updated_array
    if queue.current is None or queue.current.address != updated_array.address:
        raise RaydiumClmmSwapLoopError("mutated TickArrayState is not the active queue account")
    return replace(queue, arrays=tuple(arrays), current=updated_array)


def run_raydium_clmm_swap_loop(
    pool: RaydiumClmmPoolState,
    *,
    tick_array_snapshot: RaydiumClmmTickArraySnapshot,
    tick_array_queue: RaydiumClmmTickArrayQueue,
    first_tick_array_contains_pool_tick: bool,
    amount_specified: int,
    sqrt_price_limit_x64: int,
    zero_for_one: bool,
    is_base_input: bool,
    base_fee_rate: int,
    protocol_fee_rate: int,
    fund_fee_rate: int,
    block_timestamp: int,
    recent_epoch: int,
    max_inner_iterations: int = 4096,
) -> RaydiumClmmSwapLoopResult:
    """Run the full outer/inner loop, committing each reached tick before advancing."""
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumClmmSwapLoopError("decoded Raydium PoolState is required")
    if pool.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT:
        raise RaydiumClmmSwapLoopError("PoolState source revision is not pinned")
    if pool.swaps_enabled is not True:
        raise RaydiumClmmSwapLoopError("PoolState swap status is disabled")
    if not isinstance(pool.account_data, bytes) or len(pool.account_data) != RAYDIUM_POOL_STATE_LENGTH:
        raise RaydiumClmmSwapLoopError("verified raw PoolState account bytes are required for safe bitmap/state commits")
    try:
        canonical_pool_bytes = encode_raydium_clmm_pool_state(pool)
    except RaydiumClmmPoolStateError as exc:
        raise RaydiumClmmSwapLoopError(f"PoolState source-byte validation failed: {exc}") from exc
    if canonical_pool_bytes != pool.account_data or hashlib.sha256(pool.account_data).hexdigest() != pool.account_data_sha256:
        raise RaydiumClmmSwapLoopError("PoolState raw bytes and SHA-256 do not match the decoded state")
    if not isinstance(tick_array_snapshot, RaydiumClmmTickArraySnapshot):
        raise RaydiumClmmSwapLoopError("verified CPI tick-array snapshot is required")
    if not isinstance(tick_array_queue, RaydiumClmmTickArrayQueue) or tick_array_queue.current is None:
        raise RaydiumClmmSwapLoopError("preselected Raydium tick-array queue is required")
    if (
        tick_array_queue.pool_address != pool.pool_address
        or tick_array_queue.tick_spacing != pool.tick_spacing
        or tick_array_queue.arrays != tick_array_snapshot.tick_arrays
    ):
        raise RaydiumClmmSwapLoopError("tick-array queue differs from the pool-bound CPI snapshot")
    try:
        snapshot_queue = RaydiumClmmTickArrayQueue.from_snapshot(
            pool_address=pool.pool_address,
            tick_spacing=pool.tick_spacing,
            snapshot=tick_array_snapshot,
        )
    except RaydiumTickArrayCursorError as exc:
        raise RaydiumClmmSwapLoopError(f"CPI tick-array order validation failed: {exc}") from exc
    if snapshot_queue.arrays != tick_array_queue.arrays:
        raise RaydiumClmmSwapLoopError("tick-array queue does not match the exact CPI snapshot order")
    if not isinstance(first_tick_array_contains_pool_tick, bool):
        raise RaydiumClmmSwapLoopError("first-array current-tick flag must be boolean")
    if not isinstance(zero_for_one, bool) or not isinstance(is_base_input, bool):
        raise RaydiumClmmSwapLoopError("swap direction and input mode must be boolean")
    _u64(amount_specified, "amount_specified")
    _u64(block_timestamp, "block_timestamp")
    _u64(recent_epoch, "recent_epoch")
    if amount_specified == 0:
        raise RaydiumClmmSwapLoopError("amount_specified must be non-zero")
    if block_timestamp > 0xFFFFFFFF:
        raise RaydiumClmmSwapLoopError("Raydium swap block timestamp must fit u32")
    if isinstance(max_inner_iterations, bool) or not isinstance(max_inner_iterations, int) or not 1 <= max_inner_iterations <= 100_000:
        raise RaydiumClmmSwapLoopError("max_inner_iterations must be in [1, 100000]")
    for value, name, maximum in (
        (base_fee_rate, "base_fee_rate", 999_999),
        (protocol_fee_rate, "protocol_fee_rate", 1_000_000),
        (fund_fee_rate, "fund_fee_rate", 1_000_000),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise RaydiumClmmSwapLoopError(f"{name} is outside the Raydium fee-rate domain")
    if protocol_fee_rate + fund_fee_rate > 1_000_000:
        raise RaydiumClmmSwapLoopError("protocol and fund fee rates exceed the fee denominator")
    if isinstance(sqrt_price_limit_x64, bool) or not isinstance(sqrt_price_limit_x64, int) or not 0 <= sqrt_price_limit_x64 < (1 << 128):
        raise RaydiumClmmSwapLoopError("sqrt_price_limit_x64 must fit u128")

    effective_limit = sqrt_price_limit_x64
    if effective_limit == 0:
        effective_limit = (
            RAYDIUM_MIN_SQRT_PRICE_X64 + 1
            if zero_for_one else RAYDIUM_MAX_SQRT_PRICE_X64 - 1
        )
    if zero_for_one:
        if not RAYDIUM_MIN_SQRT_PRICE_X64 < effective_limit < pool.sqrt_price_x64:
            raise RaydiumClmmSwapLoopError("zero-for-one sqrt-price limit must be inside the current/minimum price interval")
    elif not pool.sqrt_price_x64 < effective_limit < RAYDIUM_MAX_SQRT_PRICE_X64:
        raise RaydiumClmmSwapLoopError("one-for-zero sqrt-price limit must be inside the current/maximum price interval")

    extension = tick_array_snapshot.bitmap_extension
    if tick_array_snapshot.bitmap_extension_address is None:
        if extension is not None or tick_array_snapshot.bitmap_extension_sha256 is not None:
            raise RaydiumClmmSwapLoopError("bitmap extension evidence is inconsistent")
    elif (
        extension is None
        or extension.address != tick_array_snapshot.bitmap_extension_address
        or extension.pool_address != pool.pool_address
        or extension.data_sha256 != tick_array_snapshot.bitmap_extension_sha256
    ):
        raise RaydiumClmmSwapLoopError("bitmap extension does not match the CPI snapshot")
    if extension is not None:
        if not isinstance(extension.data_bytes, bytes) or len(extension.data_bytes) != 1832:
            raise RaydiumClmmSwapLoopError("verified raw bitmap-extension bytes are required")
        encoded_extension = encode_raydium_tick_array_bitmap_extension(extension)
        if (
            encoded_extension != extension.data_bytes
            or hashlib.sha256(extension.data_bytes).hexdigest() != extension.data_sha256
        ):
            raise RaydiumClmmSwapLoopError("bitmap-extension raw bytes, words and SHA-256 do not match")

    try:
        pool = apply_raydium_pool_reward_update(
            pool, curr_timestamp=block_timestamp
        )
    except RaydiumRewardUpdateError as exc:
        raise RaydiumClmmSwapLoopError(f"pre-swap reward update failed: {exc}") from exc

    fee_token0 = _fee_on_token0(pool, zero_for_one)
    fee_on_input = _fee_on_input(pool, zero_for_one)
    fee_state = RaydiumFeeAccountingState(
        amount_specified_remaining=amount_specified,
        amount_calculated=0,
        liquidity=pool.liquidity,
        fee_growth_global_x64=(
            pool.fee_growth_global_0_x64 if fee_token0
            else pool.fee_growth_global_1_x64
        ),
    )

    dynamic_fee_state = None
    if pool.dynamic_fee_info.enabled:
        try:
            dynamic_fee_state = initialize_dynamic_fee_swap_state(
                info=pool.dynamic_fee_info,
                tick=pool.tick_current,
                tick_spacing=pool.tick_spacing,
                base_fee_rate=base_fee_rate,
                block_timestamp=block_timestamp,
            )
        except RaydiumDynamicFeeError as exc:
            raise RaydiumClmmSwapLoopError(f"dynamic-fee swap initialization failed: {exc}") from exc

    current_tick = pool.tick_current
    current_sqrt_price = pool.sqrt_price_x64
    queue = tick_array_queue
    snapshot = tick_array_snapshot
    first_array_contains_current = first_tick_array_contains_pool_tick
    total_iterations = 0
    final_iteration = None

    while fee_state.amount_specified_remaining != 0 and current_sqrt_price != effective_limit:
        try:
            traversal = select_next_tick_for_swap(
                pool,
                tick_array_snapshot=snapshot,
                queue=queue,
                current_tick_index=current_tick,
                zero_for_one=zero_for_one,
                first_tick_array_contains_pool_tick=first_array_contains_current,
            )
        except RaydiumTickArrayCursorError as exc:
            raise RaydiumClmmSwapLoopError(f"next initialized tick selection failed: {exc}") from exc
        queue = traversal.queue
        first_array_contains_current = traversal.first_tick_array_contains_pool_tick
        next_tick_state = traversal.tick
        next_tick = next_tick_state.tick_index
        try:
            sqrt_price_next = get_sqrt_price_at_tick(next_tick)
        except RaydiumTickMathError as exc:
            raise RaydiumClmmSwapLoopError(f"next initialized tick price is invalid: {exc}") from exc

        if zero_for_one:
            target_price = max(sqrt_price_next, effective_limit)
            is_price_limit_target = effective_limit >= sqrt_price_next
        else:
            target_price = min(sqrt_price_next, effective_limit)
            is_price_limit_target = effective_limit <= sqrt_price_next

        while True:
            if total_iterations >= max_inner_iterations:
                raise RaydiumClmmSwapLoopError("maximum inner-iteration count exceeded before swap completion")
            total_iterations += 1
            try:
                iteration = run_raydium_clmm_swap_inner_iteration(
                    pool,
                    fee_state,
                    current_tick=current_tick,
                    next_tick=next_tick,
                    next_tick_state=next_tick_state,
                    sqrt_price_current_x64=current_sqrt_price,
                    sqrt_price_next_x64=sqrt_price_next,
                    target_price_x64=target_price,
                    base_fee_rate=base_fee_rate,
                    protocol_fee_rate=protocol_fee_rate,
                    fund_fee_rate=fund_fee_rate,
                    zero_for_one=zero_for_one,
                    is_base_input=is_base_input,
                    dynamic_fee_state=dynamic_fee_state,
                    is_price_limit_target=is_price_limit_target,
                )
            except RaydiumClmmSwapInnerIterationError as exc:
                raise RaydiumClmmSwapLoopError(f"inner swap iteration failed: {exc}") from exc

            final_iteration = iteration
            fee_state = iteration.fee_state
            current_tick = iteration.tick
            current_sqrt_price = iteration.sqrt_price_x64
            dynamic_fee_state = iteration.dynamic_fee_state

            if iteration.reached_initialized_tick:
                try:
                    committed = commit_raydium_swap_inner_tick_array(
                        pool,
                        queue.current,
                        iteration,
                        recent_epoch=recent_epoch,
                        bitmap_extension=snapshot.bitmap_extension,
                    )
                except RaydiumSwapTickArrayCommitError as exc:
                    raise RaydiumClmmSwapLoopError(f"tick-array boundary commit failed: {exc}") from exc
                pool = committed.pool
                updated_array = committed.tick_array
                arrays = list(snapshot.tick_arrays)
                matches = [i for i, item in enumerate(arrays) if item.address == updated_array.address]
                if len(matches) != 1:
                    raise RaydiumClmmSwapLoopError("committed tick array is not present exactly once in the CPI snapshot")
                arrays[matches[0]] = updated_array
                queue = _replace_current_array(queue, updated_array)
                snapshot = replace(
                    snapshot,
                    tick_arrays=tuple(arrays),
                    bitmap_extension=committed.bitmap_extension,
                    bitmap_extension_sha256=(
                        committed.bitmap_extension.data_sha256
                        if committed.bitmap_extension is not None else None
                    ),
                )

            if iteration.should_break:
                if iteration.swap_complete:
                    break
                if not iteration.reached_initialized_tick:
                    raise RaydiumClmmSwapLoopError(
                        "inner loop exited at a non-boundary target without completing the swap"
                    )
                break
            if iteration.swap_complete or iteration.reached_initialized_tick:
                raise RaydiumClmmSwapLoopError("inner-loop state flags are inconsistent with the pinned swap control flow")

        if final_iteration is not None and final_iteration.swap_complete:
            break

    if final_iteration is None or final_iteration.swap_complete is not True:
        raise RaydiumClmmSwapLoopError("outer swap loop ended without reaching amount exhaustion or the price limit")

    return RaydiumClmmSwapLoopResult(
        pool=pool,
        tick_array_snapshot=snapshot,
        tick_array_queue=queue,
        fee_state=fee_state,
        dynamic_fee_state=dynamic_fee_state,
        final_iteration=final_iteration,
        zero_for_one=zero_for_one,
        is_base_input=is_base_input,
        amount_specified=amount_specified,
        sqrt_price_limit_x64=sqrt_price_limit_x64,
        inner_iterations=total_iterations,
        swap_complete=True,
    )
