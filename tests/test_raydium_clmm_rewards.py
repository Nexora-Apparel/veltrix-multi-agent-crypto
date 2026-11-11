"""Golden and adversarial tests for pinned Raydium reward emissions."""
from dataclasses import replace
import hashlib

import pytest
from solders.pubkey import Pubkey

from veltrix.raydium_clmm_pool_state import (
    RaydiumClmmRewardInfo,
    decode_raydium_clmm_pool_state,
    encode_raydium_clmm_pool_state,
)
from veltrix.raydium_clmm_rewards import apply_raydium_pool_reward_update
from veltrix.raydium_clmm_rewards import (
    REWARD_ENDED,
    REWARD_INITIALIZED,
    REWARD_OPENING,
    REWARD_UNINITIALIZED,
    RaydiumRewardUpdateError,
    update_raydium_reward_infos,
)
from veltrix.raydium_clmm_tick_math import Q64

U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1


def reward(**overrides):
    values = dict(
        reward_state=REWARD_INITIALIZED,
        open_time=100,
        end_time=1000,
        last_update_time=100,
        emissions_per_second_x64=2 * Q64,
        reward_total_emitted=0,
        reward_claimed=0,
        token_mint=str(Pubkey.new_unique()),
        token_vault=str(Pubkey.new_unique()),
        authority=str(Pubkey.new_unique()),
        reward_growth_global_x64=0,
    )
    values.update(overrides)
    return RaydiumClmmRewardInfo(**values)


def update(one, *, timestamp=110, liquidity=100):
    return update_raydium_reward_infos(
        (one, RaydiumClmmRewardInfo(), RaydiumClmmRewardInfo()),
        curr_timestamp=timestamp,
        liquidity=liquidity,
    )[0]


def test_emission_and_global_growth_use_source_ceil_and_floor_math():
    result = update(reward(), timestamp=110, liquidity=100)
    assert result.reward_total_emitted == 20
    assert result.reward_growth_global_x64 == (20 * Q64) // 100
    assert result.last_update_time == 110
    assert result.reward_state == REWARD_OPENING


def test_caps_total_emission_and_recomputes_growth_from_remaining_budget():
    result = update(
        reward(reward_total_emitted=U64_MAX - 5),
        timestamp=110, liquidity=100,
    )
    assert result.reward_total_emitted == U64_MAX
    assert result.reward_growth_global_x64 == (5 * Q64) // 100
    assert result.last_update_time == 110


def test_ends_reward_at_end_time_even_when_current_time_is_later():
    result = update(reward(end_time=105), timestamp=110, liquidity=100)
    assert result.last_update_time == 105
    assert result.reward_state == REWARD_ENDED
    assert result.reward_total_emitted == 10


def test_zero_liquidity_advances_last_update_without_emitting_rewards():
    result = update(reward(), timestamp=110, liquidity=0)
    assert result.last_update_time == 110
    assert result.reward_total_emitted == 0
    assert result.reward_growth_global_x64 == 0


def test_reward_does_not_start_before_or_at_open_time():
    original = reward()
    for timestamp in (99, 100):
        assert update(original, timestamp=timestamp, liquidity=100) == original


def test_uninitialized_and_saturated_rewards_are_untouched():
    uninitialized = RaydiumClmmRewardInfo()
    saturated = reward(reward_total_emitted=U64_MAX, last_update_time=100)
    infos = (uninitialized, saturated, RaydiumClmmRewardInfo())
    assert update_raydium_reward_infos(
        infos, curr_timestamp=500, liquidity=100
    ) == infos


def test_timestamp_regression_uses_saturating_elapsed_time_but_advances_last_update():
    original = reward(last_update_time=120)
    result = update(original, timestamp=110, liquidity=100)
    assert result.last_update_time == 110
    assert result.reward_total_emitted == 0
    assert result.reward_growth_global_x64 == 0


def test_extreme_emission_rate_caps_total_and_recomputes_growth_from_remaining_budget():
    original = reward(
        emissions_per_second_x64=U128_MAX,
        last_update_time=0,
        open_time=0,
        end_time=U64_MAX,
    )
    result = update(original, timestamp=U64_MAX, liquidity=1)
    assert result.reward_total_emitted == U64_MAX
    assert result.reward_growth_global_x64 == U64_MAX * Q64


def test_growth_global_wraps_u128():
    original = reward(reward_growth_global_x64=U128_MAX)
    result = update(original, timestamp=101, liquidity=1)
    assert result.reward_growth_global_x64 == (U128_MAX + 2 * Q64) % (1 << 128)


def test_rejects_wrong_reward_count_invalid_claims_and_integer_domains():
    with pytest.raises(RaydiumRewardUpdateError, match="exactly three"):
        update_raydium_reward_infos((reward(),), curr_timestamp=110, liquidity=100)
    with pytest.raises(RaydiumRewardUpdateError, match="claimed amount"):
        update(reward(reward_total_emitted=1, reward_claimed=2))
    with pytest.raises(RaydiumRewardUpdateError, match="curr_timestamp"):
        update(reward(), timestamp=-1)
    with pytest.raises(RaydiumRewardUpdateError, match="liquidity"):
        update(reward(), liquidity=-1)


def test_reward_state_transitions_from_initialized_to_opening_and_ended():
    opening = update(reward(reward_state=REWARD_INITIALIZED), timestamp=110)
    ended = update(reward(reward_state=REWARD_OPENING, end_time=105), timestamp=110)
    assert opening.reward_state == REWARD_OPENING
    assert ended.reward_state == REWARD_ENDED
    assert REWARD_UNINITIALIZED == 0


def test_pool_reward_update_rehashes_exact_serialized_pool_state():
    from tests.test_raydium_clmm_pool_state import fixture as pool_fixture
    _, evidence, _, _, _, pool_address, *_ = pool_fixture()
    pool = decode_raydium_clmm_pool_state(
        evidence, expected_pool_address=pool_address
    )
    pool = replace(
        pool,
        reward_infos=(reward(), RaydiumClmmRewardInfo(), RaydiumClmmRewardInfo()),
    )
    updated = apply_raydium_pool_reward_update(pool, curr_timestamp=110)
    encoded = encode_raydium_clmm_pool_state(updated)
    assert updated.reward_infos[0].reward_total_emitted == 20
    assert updated.account_data == encoded
    assert updated.account_data_sha256 == hashlib.sha256(encoded).hexdigest()
