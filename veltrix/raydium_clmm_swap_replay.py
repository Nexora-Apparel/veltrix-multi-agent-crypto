"""Deterministic, local Raydium CLMM swap replay over committed account bundles.

This engine performs no RPC, signing, wallet approval, transaction construction,
submission, or broadcast. It returns a fully staged immutable account bundle.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_bitmap_traversal import RaydiumClmmBitmapTraversalResult
from veltrix.raydium_clmm_bitmap_cursor import (
    RaydiumBitmapCursorError,
    first_raydium_tick_array_start_index,
    next_initialized_raydium_tick_array_start_index,
)
from veltrix.raydium_clmm_dynamic_fee import (
    RaydiumDynamicFeeSwapState,
    initialize_dynamic_fee_swap_state,
)
from veltrix.raydium_clmm_fee_accounting import (
    RaydiumFeeAccountingState,
    RaydiumSwapSettlement,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_MAX_TICK,
    RAYDIUM_MIN_TICK,
)
from veltrix.raydium_clmm_replay_bundle import (
    RaydiumClmmReplayAccountBundle,
    build_raydium_clmm_replay_account_bundle,
)
from veltrix.raydium_clmm_swap_loop import (
    RaydiumSwapLoopError,
    apply_raydium_swap_loop_boundary,
    compute_raydium_swap_loop_pre_step,
    finalize_raydium_swap_loop,
)
from veltrix.raydium_clmm_tick_cursor import (
    RaydiumTickCursorError,
    select_next_initialized_tick_from_live_bitmap,
)
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick
from veltrix.raydium_clmm_tick_arrays import raydium_current_tick_array_start
from veltrix.raydium_clmm_rewards import apply_raydium_pool_reward_update
from veltrix.raydium_clmm_vaults import apply_raydium_clmm_vault_balance_delta


class RaydiumSwapReplayError(ValueError):
    """Raised when a deterministic local swap replay cannot safely complete."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmSwapReplayResult:
    final_bundle: RaydiumClmmReplayAccountBundle
    settlement: RaydiumSwapSettlement
    outer_iterations: int
    inner_iterations: int
    boundary_crossings: int
    amount_specified_remaining: int
    execution_ready: bool = False


def _verify_initial_bitmap_traversal(
    pool,
    traversal: RaydiumClmmBitmapTraversalResult,
    *,
    tick_array_starts: tuple[int, ...],
    bitmap_extension,
) -> None:
    """Recompute the proof's ordered initialized-array sequence from committed bits."""
    if traversal.pool_address != pool.pool_address:
        raise RaydiumSwapReplayError("initial bitmap traversal proof belongs to a different pool")
    if not isinstance(traversal.zero_for_one, bool):
        raise RaydiumSwapReplayError("initial bitmap traversal direction is malformed")
    if not traversal.bounded_interval_complete:
        raise RaydiumSwapReplayError("initial bitmap traversal interval is incomplete")
    current = raydium_current_tick_array_start(
        pool.tick_current, pool.tick_spacing
    )
    if current != traversal.current_array_start_index:
        raise RaydiumSwapReplayError("bitmap traversal current-array anchor differs from PoolState.tick_current")
    try:
        _, first = first_raydium_tick_array_start_index(
            pool,
            zero_for_one=traversal.zero_for_one,
            bitmap_extension=bitmap_extension,
        )
        if first != traversal.first_initialized_array_start_index:
            raise RaydiumSwapReplayError("bitmap traversal first initialized array is not source-derived")
        expected = [first]
        cursor = first
        last = traversal.last_cpi_array_start_index
        if raydium_current_tick_array_start(last, pool.tick_spacing) != last:
            raise RaydiumSwapReplayError("last CPI array start is not canonically aligned")
        while cursor != last:
            next_start = next_initialized_raydium_tick_array_start_index(
                pool,
                last_tick_array_start_index=cursor,
                zero_for_one=traversal.zero_for_one,
                bitmap_extension=bitmap_extension,
            )
            if next_start is None:
                raise RaydiumSwapReplayError("bitmap traversal ends before the last CPI array")
            if traversal.zero_for_one and next_start < last:
                raise RaydiumSwapReplayError("bitmap traversal overshoots the last CPI array")
            if not traversal.zero_for_one and next_start > last:
                raise RaydiumSwapReplayError("bitmap traversal overshoots the last CPI array")
            if next_start in expected:
                raise RaydiumSwapReplayError("bitmap traversal contains a cycle")
            expected.append(next_start)
            cursor = next_start
            if len(expected) > len(tick_array_starts):
                raise RaydiumSwapReplayError("bitmap traversal exceeds the supplied tick-array account set")
    except RaydiumBitmapCursorError as exc:
        raise RaydiumSwapReplayError(f"initial bitmap traversal could not be recomputed: {exc}") from exc

    expected_tuple = tuple(expected)
    if (
        expected_tuple != traversal.expected_initialized_array_starts
        or expected_tuple != traversal.supplied_initialized_array_starts
        or expected_tuple != traversal.supplied_array_starts
        or set(expected_tuple) != set(tick_array_starts)
        or len(expected_tuple) != len(tick_array_starts)
    ):
        raise RaydiumSwapReplayError("initial bitmap proof does not match the exact committed CPI array sequence")
    expected_extension_hash = (
        bitmap_extension.data_sha256 if bitmap_extension is not None else None
    )
    if traversal.bitmap_extension_sha256 != expected_extension_hash:
        raise RaydiumSwapReplayError("bitmap traversal proof extension commitment is stale")


def _fee_on_token0(fee_on: int, zero_for_one: bool) -> bool:
    if fee_on == 0:
        return zero_for_one
    if fee_on == 1:
        return True
    if fee_on == 2:
        return False
    raise RaydiumSwapReplayError("PoolState fee_on mode is invalid")


def replay_raydium_clmm_swap(
    bundle: RaydiumClmmReplayAccountBundle,
    traversal: RaydiumClmmBitmapTraversalResult,
    *,
    amount_specified: int,
    sqrt_price_limit_x64: int,
    base_fee_rate: int,
    protocol_fee_rate: int,
    fund_fee_rate: int,
    block_timestamp: int,
    recent_epoch: int,
    zero_for_one: bool,
    is_base_input: bool,
    max_outer_iterations: int = 256,
    max_inner_iterations: int = 512,
) -> RaydiumClmmSwapReplayResult:
    """Run the deterministic swap loop and return an atomically staged account bundle.

    The supplied bitmap traversal proof binds the initial CPI array set. After
    each boundary transition, the engine re-reads the mutated PoolState and
    extension bitmaps while retaining the active CPI array cursor. All account
    mutations remain local immutable values until final bundle validation.
    """
    if not isinstance(bundle, RaydiumClmmReplayAccountBundle):
        raise RaydiumSwapReplayError("validated Raydium replay account bundle is required")
    if not isinstance(traversal, RaydiumClmmBitmapTraversalResult) or traversal.passed is not True:
        raise RaydiumSwapReplayError("passed initial bitmap traversal proof is required")
    if traversal.zero_for_one is not zero_for_one:
        raise RaydiumSwapReplayError("initial bitmap traversal direction differs from the requested swap")
    starts = tuple(item.start_tick_index for item in bundle.tick_arrays)
    _verify_initial_bitmap_traversal(
        bundle.pool,
        traversal,
        tick_array_starts=starts,
        bitmap_extension=bundle.bitmap_extension,
    )
    if not isinstance(zero_for_one, bool) or not isinstance(is_base_input, bool):
        raise RaydiumSwapReplayError("swap direction and exactness flags must be boolean")
    if isinstance(amount_specified, bool) or not isinstance(amount_specified, int) or not 0 < amount_specified < (1 << 64):
        raise RaydiumSwapReplayError("amount_specified must be a positive u64")
    if isinstance(block_timestamp, bool) or not isinstance(block_timestamp, int) or not 0 <= block_timestamp <= 0xFFFFFFFF:
        raise RaydiumSwapReplayError("block timestamp must fit u32")
    if isinstance(recent_epoch, bool) or not isinstance(recent_epoch, int) or not 0 <= recent_epoch < (1 << 64):
        raise RaydiumSwapReplayError("recent epoch must fit u64")
    for value, field in (
        (base_fee_rate, "base fee rate"),
        (protocol_fee_rate, "protocol fee rate"),
        (fund_fee_rate, "fund fee rate"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1_000_000:
            raise RaydiumSwapReplayError(f"{field} must be in [0, 1,000,000)")
    if protocol_fee_rate + fund_fee_rate > 1_000_000:
        raise RaydiumSwapReplayError("protocol and fund fee rates exceed the fee denominator")
    for value, field in (
        (max_outer_iterations, "max_outer_iterations"),
        (max_inner_iterations, "max_inner_iterations"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RaydiumSwapReplayError(f"{field} must be a positive integer")

    # Rebuild the initial bundle to ensure its aggregate and per-account commitments still hold.
    try:
        checked_bundle = build_raydium_clmm_replay_account_bundle(
            bundle.pool,
            tick_arrays=bundle.tick_arrays,
            bitmap_extension=bundle.bitmap_extension,
            observation=bundle.observation,
            vaults=bundle.vaults,
        )
    except Exception as exc:
        raise RaydiumSwapReplayError(f"initial replay account bundle is invalid: {exc}") from exc
    if checked_bundle.bundle_sha256 != bundle.bundle_sha256:
        raise RaydiumSwapReplayError("initial replay bundle commitment is stale")

    pool = checked_bundle.pool
    observation = checked_bundle.observation
    bitmap_extension = checked_bundle.bitmap_extension
    tick_arrays = {
        item.start_tick_index: item for item in checked_bundle.tick_arrays
    }
    vaults = {item.address: item for item in checked_bundle.vaults}

    fee_on_token0 = _fee_on_token0(pool.fee_on, zero_for_one)
    is_fee_on_input = fee_on_token0 == zero_for_one
    current_tick = pool.tick_current
    current_sqrt_price_x64 = pool.sqrt_price_x64
    sqrt_price_limit_min = get_sqrt_price_at_tick(RAYDIUM_MIN_TICK)
    sqrt_price_limit_max = get_sqrt_price_at_tick(RAYDIUM_MAX_TICK)
    if not sqrt_price_limit_min < sqrt_price_limit_x64 < sqrt_price_limit_max:
        raise RaydiumSwapReplayError("sqrt price limit is outside the strict Raydium protocol range")
    if zero_for_one:
        if not sqrt_price_limit_x64 < current_sqrt_price_x64:
            raise RaydiumSwapReplayError("zero-for-one sqrt price limit must be below the current price")
    elif not sqrt_price_limit_x64 > current_sqrt_price_x64:
        raise RaydiumSwapReplayError("one-for-zero sqrt price limit must be above the current price")

    # Upstream updates reward emissions before entering the swap loop.
    pool = apply_raydium_pool_reward_update(
        pool, curr_timestamp=block_timestamp
    )
    dynamic_fee_state: RaydiumDynamicFeeSwapState | None = None
    if pool.dynamic_fee_info.enabled:
        dynamic_fee_state = initialize_dynamic_fee_swap_state(
            info=pool.dynamic_fee_info,
            tick=current_tick,
            tick_spacing=pool.tick_spacing,
            base_fee_rate=base_fee_rate,
            block_timestamp=block_timestamp,
        )

    fee_state = RaydiumFeeAccountingState(
        amount_specified_remaining=amount_specified,
        amount_calculated=0,
        liquidity=pool.liquidity,
        fee_growth_global_x64=(
            pool.fee_growth_global_0_x64
            if fee_on_token0
            else pool.fee_growth_global_1_x64
        ),
        lp_fee=0,
        protocol_fee=0,
        fund_fee=0,
    )

    active_array_start_index: int | None = None
    outer_iterations = 0
    inner_iterations = 0
    boundary_crossings = 0

    while (
        fee_state.amount_specified_remaining != 0
        and current_sqrt_price_x64 != sqrt_price_limit_x64
    ):
        if outer_iterations >= max_outer_iterations:
            raise RaydiumSwapReplayError("swap replay exceeded max_outer_iterations")
        try:
            selection = select_next_initialized_tick_from_live_bitmap(
                pool,
                tick_arrays=tuple(tick_arrays.values()),
                current_tick_index=current_tick,
                zero_for_one=zero_for_one,
                bitmap_extension=bitmap_extension,
                current_array_start_index=active_array_start_index,
            )
        except RaydiumTickCursorError as exc:
            raise RaydiumSwapReplayError(f"next initialized tick selection failed: {exc}") from exc

        cursor = selection.cursor
        active_array_start_index = cursor.tick_array_start_index
        active_tick_array = tick_arrays.get(active_array_start_index)
        if active_tick_array is None:
            raise RaydiumSwapReplayError("selected TickArrayState disappeared from the replay account set")

        tick_boundary_price_x64 = get_sqrt_price_at_tick(cursor.tick.tick_index)
        target_price_x64 = tick_boundary_price_x64
        if zero_for_one and target_price_x64 < sqrt_price_limit_x64:
            target_price_x64 = sqrt_price_limit_x64
        elif not zero_for_one and target_price_x64 > sqrt_price_limit_x64:
            target_price_x64 = sqrt_price_limit_x64

        liquidity_next = fee_state.liquidity
        inner_completed = False
        for _ in range(max_inner_iterations):
            if inner_iterations >= max_outer_iterations * max_inner_iterations:
                raise RaydiumSwapReplayError("swap replay exceeded the global inner-step safety bound")
            previous_remaining = fee_state.amount_specified_remaining
            previous_price = current_sqrt_price_x64
            previous_tick = current_tick
            previous_dynamic_state = dynamic_fee_state

            pre_step = compute_raydium_swap_loop_pre_step(
                fee_state,
                sqrt_price_current_x64=current_sqrt_price_x64,
                target_price_x64=target_price_x64,
                base_fee_rate=base_fee_rate,
                zero_for_one=zero_for_one,
                is_base_input=is_base_input,
                is_fee_on_input=is_fee_on_input,
                protocol_fee_rate=protocol_fee_rate,
                fund_fee_rate=fund_fee_rate,
                dynamic_fee_state=dynamic_fee_state,
            )
            boundary = apply_raydium_swap_loop_boundary(
                pre_step,
                pool=pool,
                tick_array=active_tick_array,
                bitmap_extension=bitmap_extension,
                cursor=cursor,
                current_tick=current_tick,
                current_sqrt_price_x64=current_sqrt_price_x64,
                tick_boundary_price_x64=tick_boundary_price_x64,
                updated_reward_infos=pool.reward_infos,
                recent_epoch=recent_epoch,
                zero_for_one=zero_for_one,
                is_base_input=is_base_input,
                is_fee_on_input=is_fee_on_input,
                protocol_fee_rate=protocol_fee_rate,
                fund_fee_rate=fund_fee_rate,
            )
            fee_state = boundary.fee_accounting_state
            pool = boundary.pool
            bitmap_extension = boundary.bitmap_extension
            current_tick = boundary.tick_current
            current_sqrt_price_x64 = pre_step.swap_step.sqrt_price_next_x64
            liquidity_next = boundary.liquidity_next
            dynamic_fee_state = boundary.dynamic_fee_state
            if boundary.mutation is not None:
                active_tick_array = boundary.tick_array
                tick_arrays[active_array_start_index] = active_tick_array
            if boundary.crossing is not None:
                boundary_crossings += 1
            inner_iterations += 1

            if (
                fee_state.amount_specified_remaining == 0
                or current_sqrt_price_x64 == target_price_x64
            ):
                inner_completed = True
                break
            if (
                fee_state.amount_specified_remaining == previous_remaining
                and current_sqrt_price_x64 == previous_price
                and current_tick == previous_tick
                and dynamic_fee_state == previous_dynamic_state
            ):
                raise RaydiumSwapReplayError("swap inner loop made no state progress")
        if not inner_completed:
            raise RaydiumSwapReplayError("swap replay exceeded max_inner_iterations")

        # Raydium stages liquidity during the spacing loop and commits it after the loop exits.
        fee_state = replace(fee_state, liquidity=liquidity_next)
        outer_iterations += 1

    finalized = finalize_raydium_swap_loop(
        pool,
        observation,
        fee_state,
        amount_specified=amount_specified,
        final_tick=current_tick,
        final_sqrt_price_x64=current_sqrt_price_x64,
        final_liquidity=fee_state.liquidity,
        block_timestamp=block_timestamp,
        zero_for_one=zero_for_one,
        is_base_input=is_base_input,
        dynamic_fee_state=dynamic_fee_state,
    )

    # Stage the two vault balance deltas locally. Any failure aborts the whole result.
    if zero_for_one:
        vault0_delta = finalized.settlement.amount_0
        vault1_delta = -finalized.settlement.amount_1
    else:
        vault0_delta = -finalized.settlement.amount_0
        vault1_delta = finalized.settlement.amount_1
    try:
        vaults[pool.token_vault_0] = apply_raydium_clmm_vault_balance_delta(
            vaults[pool.token_vault_0], delta_raw=vault0_delta
        )
        vaults[pool.token_vault_1] = apply_raydium_clmm_vault_balance_delta(
            vaults[pool.token_vault_1], delta_raw=vault1_delta
        )
        final_bundle = build_raydium_clmm_replay_account_bundle(
            finalized.pool,
            tick_arrays=tuple(tick_arrays.values()),
            bitmap_extension=bitmap_extension,
            observation=finalized.observation,
            vaults=tuple(vaults.values()),
        )
    except Exception as exc:
        raise RaydiumSwapReplayError(f"atomic staged account settlement failed: {exc}") from exc

    return RaydiumClmmSwapReplayResult(
        final_bundle=final_bundle,
        settlement=finalized.settlement,
        outer_iterations=outer_iterations,
        inner_iterations=inner_iterations,
        boundary_crossings=boundary_crossings,
        amount_specified_remaining=fee_state.amount_specified_remaining,
        execution_ready=False,
    )
