"""Pinned Raydium CLMM reward emission and global-growth transition.

Source: PoolState::update_reward_infos in states/pool.rs at
6d7a0f82d2f293c553c69aaf943377367c6c4cc6.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib

from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_POOL_REWARD_COUNT,
    RaydiumClmmRewardInfo,
    RaydiumClmmPoolState,
    encode_raydium_clmm_pool_state,
)
from veltrix.raydium_clmm_tick_math import Q64

U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1
REWARD_UNINITIALIZED = 0
REWARD_INITIALIZED = 1
REWARD_OPENING = 2
REWARD_ENDED = 3


class RaydiumRewardUpdateError(ValueError):
    """Raised when reward state cannot be updated safely."""


def _u64(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= U64_MAX:
        raise RaydiumRewardUpdateError(f"{field} must fit u64")


def _u128(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= U128_MAX:
        raise RaydiumRewardUpdateError(f"{field} must fit u128")


def _mul_div_floor_or_zero(a: int, b: int, denominator: int) -> int:
    result = a * b // denominator
    return result if result <= U128_MAX else 0


def _mul_div_ceil_or_zero(a: int, b: int, denominator: int) -> int:
    result = (a * b + denominator - 1) // denominator
    return result if result <= U128_MAX else 0


def update_raydium_reward_infos(
    reward_infos: tuple[RaydiumClmmRewardInfo, ...],
    *,
    curr_timestamp: int,
    liquidity: int,
) -> tuple[RaydiumClmmRewardInfo, ...]:
    """Port PoolState::update_reward_infos and its saturating/capped emission math."""
    if not isinstance(reward_infos, tuple) or len(reward_infos) != RAYDIUM_POOL_REWARD_COUNT:
        raise RaydiumRewardUpdateError("exactly three decoded Raydium reward records are required")
    _u64(curr_timestamp, "curr_timestamp")
    _u128(liquidity, "liquidity")
    updated = list(reward_infos)

    for index, reward in enumerate(reward_infos):
        if not isinstance(reward, RaydiumClmmRewardInfo):
            raise RaydiumRewardUpdateError(f"reward_infos[{index}] is not a decoded RewardInfo")
        if reward.reward_state not in (REWARD_UNINITIALIZED, REWARD_INITIALIZED, REWARD_OPENING, REWARD_ENDED):
            raise RaydiumRewardUpdateError(f"reward_infos[{index}] has an invalid reward state")
        for value, field in (
            (reward.open_time, "open_time"),
            (reward.end_time, "end_time"),
            (reward.last_update_time, "last_update_time"),
            (reward.reward_total_emitted, "reward_total_emitted"),
            (reward.reward_claimed, "reward_claimed"),
        ):
            _u64(value, f"reward_infos[{index}].{field}")
        for value, field in (
            (reward.emissions_per_second_x64, "emissions_per_second_x64"),
            (reward.reward_growth_global_x64, "reward_growth_global_x64"),
        ):
            _u128(value, f"reward_infos[{index}].{field}")
        if reward.reward_claimed > reward.reward_total_emitted:
            raise RaydiumRewardUpdateError(f"reward_infos[{index}] claimed amount exceeds total emitted")
        if not reward.initialized or reward.reward_total_emitted == U64_MAX:
            continue
        if curr_timestamp <= reward.open_time:
            continue

        latest_update_timestamp = min(curr_timestamp, reward.end_time)
        if liquidity != 0:
            time_delta = max(0, latest_update_timestamp - reward.last_update_time)
            if time_delta != 0:
                reward_delta = _mul_div_ceil_or_zero(
                    time_delta, reward.emissions_per_second_x64, Q64
                )
                reward_growth_delta = _mul_div_floor_or_zero(
                    time_delta, reward.emissions_per_second_x64, liquidity
                )
                remaining = U64_MAX - reward.reward_total_emitted
                if reward_delta <= remaining:
                    total_emitted = reward.reward_total_emitted + reward_delta
                else:
                    total_emitted = U64_MAX
                    reward_growth_delta = _mul_div_floor_or_zero(
                        remaining, Q64, liquidity
                    )
                growth = (
                    reward.reward_growth_global_x64 + reward_growth_delta
                ) % (1 << 128)
            else:
                total_emitted = reward.reward_total_emitted
                growth = reward.reward_growth_global_x64
        else:
            total_emitted = reward.reward_total_emitted
            growth = reward.reward_growth_global_x64

        reward_state = reward.reward_state
        if latest_update_timestamp >= reward.open_time and latest_update_timestamp < reward.end_time:
            reward_state = REWARD_OPENING
        elif latest_update_timestamp == reward.end_time:
            reward_state = REWARD_ENDED

        updated[index] = replace(
            reward,
            last_update_time=latest_update_timestamp,
            reward_total_emitted=total_emitted,
            reward_growth_global_x64=growth,
            reward_state=reward_state,
        )

    return tuple(updated)


def apply_raydium_pool_reward_update(
    pool: RaydiumClmmPoolState,
    *,
    curr_timestamp: int,
) -> RaydiumClmmPoolState:
    """Update PoolState reward records and commit exact serialized account bytes."""
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumRewardUpdateError("decoded Raydium PoolState is required")
    updated_infos = update_raydium_reward_infos(
        pool.reward_infos,
        curr_timestamp=curr_timestamp,
        liquidity=pool.liquidity,
    )
    updated_pool = replace(pool, reward_infos=updated_infos)
    if updated_pool.account_data:
        encoded = encode_raydium_clmm_pool_state(updated_pool)
        updated_pool = replace(
            updated_pool,
            account_data=encoded,
            account_data_sha256=hashlib.sha256(encoded).hexdigest(),
        )
    return updated_pool
