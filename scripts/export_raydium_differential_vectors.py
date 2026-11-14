#!/usr/bin/env python3
"""Export deterministic Python math vectors for the pinned upstream Rust oracle."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from solders.pubkey import Pubkey

from veltrix.raydium_clmm_dynamic_fee import (
    tick_spacing_index_from_tick,
    update_dynamic_fee_reference,
    update_volatility_accumulator,
    compute_total_fee_rate,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_POOL_STATE_LENGTH,
    RaydiumClmmDynamicFeeInfo,
    RaydiumClmmPoolState,
    RaydiumClmmRewardInfo,
    derive_raydium_clmm_pool_address,
    encode_raydium_clmm_pool_state,
)
from veltrix.raydium_clmm_fee_accounting import RaydiumFeeAccountingState
from veltrix.raydium_clmm_dynamic_fee import RaydiumDynamicFeeSwapState
from veltrix.raydium_clmm_swap_inner_iteration import RaydiumClmmSwapInnerIterationResult
from veltrix.raydium_clmm_pool_post_swap_finalization import finalize_raydium_pool_after_swap
from veltrix.raydium_clmm_tick_array_mutation import (
    RaydiumClmmTickArrayBitmapExtension,
    derive_raydium_tick_array_bitmap_extension_address,
    _flip_bitmap,
    encode_raydium_tick_array_bitmap_extension,
)
from veltrix.raydium_clmm_limit_orders import RaydiumLimitOrderMatch
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_SIZE,
    RaydiumClmmTick,
    RaydiumClmmTickArray,
    RaydiumClmmTickArraySnapshot,
    derive_raydium_clmm_tick_array_address,
    encode_raydium_clmm_tick_array,
)
from veltrix.raydium_clmm_tick_math import Q64
from veltrix.raydium_clmm_limit_orders import match_raydium_limit_order
from veltrix.raydium_clmm_tick_crossing import RaydiumRewardGrowth, cross_tick_and_apply_liquidity
from veltrix.raydium_clmm_swap_step_math import compute_swap_step
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick
from veltrix.raydium_clmm_swap_replay import run_raydium_clmm_swap_replay
from veltrix.raydium_clmm_observation import (
    RaydiumClmmObservation,
    RaydiumClmmObservationState,
    apply_raydium_observation_update,
    derive_raydium_clmm_observation_address,
    encode_raydium_clmm_observation_state,
)


SWAP_CASES = (
    {
        "name": "exact_input_zero_for_one_fee_on_input_partial",
        "current_tick": 100,
        "target_tick": 0,
        "liquidity": 1_000_000_000,
        "amount_remaining": 750_000,
        "fee_rate": 3_000,
        "is_base_input": True,
        "zero_for_one": True,
        "is_fee_on_input": True,
    },
    {
        "name": "exact_input_one_for_zero_fee_on_input_partial",
        "current_tick": 0,
        "target_tick": 100,
        "liquidity": 1_000_000_000,
        "amount_remaining": 750_000,
        "fee_rate": 3_000,
        "is_base_input": True,
        "zero_for_one": False,
        "is_fee_on_input": True,
    },
    {
        "name": "exact_input_zero_for_one_fee_on_output_reaches_target",
        "current_tick": 100,
        "target_tick": 0,
        "liquidity": 1_000_000_000,
        "amount_remaining": 10_000_000,
        "fee_rate": 3_000,
        "is_base_input": True,
        "zero_for_one": True,
        "is_fee_on_input": False,
    },
    {
        "name": "exact_output_zero_for_one_fee_on_input",
        "current_tick": 100,
        "target_tick": 0,
        "liquidity": 1_000_000_000,
        "amount_remaining": 100_000,
        "fee_rate": 3_000,
        "is_base_input": False,
        "zero_for_one": True,
        "is_fee_on_input": True,
    },
    {
        "name": "exact_output_one_for_zero_fee_on_output",
        "current_tick": 0,
        "target_tick": 100,
        "liquidity": 1_000_000_000,
        "amount_remaining": 100_000,
        "fee_rate": 3_000,
        "is_base_input": False,
        "zero_for_one": False,
        "is_fee_on_input": False,
    },
    {
        "name": "exact_output_one_for_zero_zero_fee",
        "current_tick": 0,
        "target_tick": 100,
        "liquidity": 1_000_000_000,
        "amount_remaining": 100_000,
        "fee_rate": 0,
        "is_base_input": False,
        "zero_for_one": False,
        "is_fee_on_input": True,
    },
)



def make_dynamic_info() -> RaydiumClmmDynamicFeeInfo:
    return RaydiumClmmDynamicFeeInfo(
        filter_period=60,
        decay_period=3600,
        reduction_factor=5000,
        dynamic_fee_control=1000,
        max_volatility_accumulator=100000,
        tick_spacing_index_reference=3,
        volatility_reference=2000,
        volatility_accumulator=4000,
        last_update_timestamp=1000,
        padding=bytes(46),
    )


def dynamic_fee_vectors() -> dict:
    reference_cases = []
    for name, timestamp, tick_index in (
        ("inside_filter_window", 1059, 10),
        ("at_decay_boundary", 1060, 10),
        ("at_reset_boundary", 4600, 10),
        ("backward_clock", 900, 10),
    ):
        result = update_dynamic_fee_reference(
            make_dynamic_info(),
            tick_spacing_index=tick_index,
            current_timestamp=timestamp,
        )
        reference_cases.append({
            "name": name,
            "current_timestamp": timestamp,
            "tick_spacing_index": tick_index,
            "initial": {
                "filter_period": 60,
                "decay_period": 3600,
                "reduction_factor": 5000,
                "dynamic_fee_control": 1000,
                "max_volatility_accumulator": 100000,
                "tick_spacing_index_reference": 3,
                "volatility_reference": 2000,
                "volatility_accumulator": 4000,
                "last_update_timestamp": 1000,
            },
            "expected": {
                "tick_spacing_index_reference": result.tick_spacing_index_reference,
                "volatility_reference": result.volatility_reference,
                "volatility_accumulator": result.volatility_accumulator,
                "last_update_timestamp": result.last_update_timestamp,
            },
        })

    accumulator_cases = []
    for name, tick_index in (("normal_delta", 8), ("capped_delta", 100)):
        result = update_volatility_accumulator(
            make_dynamic_info(),
            tick_spacing_index=tick_index,
        )
        accumulator_cases.append({
            "name": name,
            "tick_spacing_index": tick_index,
            "initial": {
                "tick_spacing_index_reference": 3,
                "volatility_reference": 2000,
                "max_volatility_accumulator": 100000,
            },
            "expected_volatility_accumulator": result.volatility_accumulator,
        })

    spacing_cases = [
        {"tick": tick, "tick_spacing": spacing, "expected": tick_spacing_index_from_tick(tick, spacing)}
        for tick, spacing in ((-101, 10), (-100, 10), (-1, 10), (1, 10), (101, 10), (123, 1))
    ]
    fee_rate_cases = []
    for name, volatility in (
        ("zero_volatility", 0),
        ("low_volatility", 100),
        ("medium_volatility", 10000),
        ("high_volatility", 100000),
    ):
        info = make_dynamic_info()
        info = RaydiumClmmDynamicFeeInfo(
            filter_period=info.filter_period,
            decay_period=info.decay_period,
            reduction_factor=info.reduction_factor,
            dynamic_fee_control=info.dynamic_fee_control,
            max_volatility_accumulator=info.max_volatility_accumulator,
            tick_spacing_index_reference=info.tick_spacing_index_reference,
            volatility_reference=info.volatility_reference,
            volatility_accumulator=volatility,
            last_update_timestamp=info.last_update_timestamp,
            padding=info.padding,
        )
        fee_rate_cases.append({
            "name": name,
            "base_fee_rate": 3000,
            "tick_spacing": 10,
            "info": {
                "filter_period": info.filter_period,
                "decay_period": info.decay_period,
                "reduction_factor": info.reduction_factor,
                "dynamic_fee_control": info.dynamic_fee_control,
                "max_volatility_accumulator": info.max_volatility_accumulator,
                "tick_spacing_index_reference": info.tick_spacing_index_reference,
                "volatility_reference": info.volatility_reference,
                "volatility_accumulator": info.volatility_accumulator,
                "last_update_timestamp": info.last_update_timestamp,
            },
            "expected_total_fee_rate": compute_total_fee_rate(
                base_fee_rate=3000,
                info=info,
                tick_spacing=10,
            ),
        })

    return {
        "reference_updates": reference_cases,
        "volatility_updates": accumulator_cases,
        "tick_spacing_indexes": spacing_cases,
        "total_fee_rates": fee_rate_cases,
    }


def limit_order_vectors() -> list[dict]:
    cases = (
        ("partial_cohort_then_new_cohort_fee_input", 300, True, True, 3000, True, 500, 200, 7, Q64 * 3 // 4),
        ("input_fee_output_one_for_zero", 300, False, True, 3000, False, 1000, 0, 0, Q64),
        ("exact_output_fee_input", 100, True, False, 3000, True, 1000, 200, 2, Q64 // 2),
        ("exact_output_fee_output", 100, False, False, 3000, False, 100, 50, 4, Q64 // 2),
        ("cap_at_remaining_orders", 2000, True, True, 0, True, 300, 200, 8, Q64),
        ("no_orders_noop", 100, False, True, 3000, True, 0, 0, 1, Q64),
    )
    vectors = []
    for name, swap_amount, zero_for_one, is_base_input, fee_rate, is_fee_on_input, orders, part, phase, ratio in cases:
        tick = RaydiumClmmTick(
            tick_index=0,
            liquidity_net=0,
            liquidity_gross=0,
            orders_amount=orders,
            part_filled_orders_remaining=part,
            initialized=(orders > 0 or part > 0),
            fee_growth_outside_0_x64=0,
            fee_growth_outside_1_x64=0,
            reward_growths_outside_x64=(0, 0, 0),
            order_phase=phase,
            unfilled_ratio_x64=ratio,
            padding=(0, 0, 0),
        )
        updated, result = match_raydium_limit_order(
            tick,
            swap_amount=swap_amount,
            zero_for_one=zero_for_one,
            is_base_input=is_base_input,
            fee_rate=fee_rate,
            is_fee_on_input=is_fee_on_input,
            sqrt_price_x64=Q64,
        )
        vectors.append({
            "name": name,
            "swap_amount": swap_amount,
            "zero_for_one": zero_for_one,
            "is_base_input": is_base_input,
            "fee_rate": fee_rate,
            "is_fee_on_input": is_fee_on_input,
            "orders_amount": orders,
            "part_filled_orders_remaining": part,
            "order_phase": phase,
            "unfilled_ratio_x64": str(ratio),
            "expected": {
                "amount_in": result.amount_in,
                "amount_out": result.amount_out,
                "amm_fee_amount": result.amm_fee_amount,
                "orders_amount": updated.orders_amount,
                "part_filled_orders_remaining": updated.part_filled_orders_remaining,
                "order_phase": updated.order_phase,
                "unfilled_ratio_x64": str(updated.unfilled_ratio_x64),
                "initialized": updated.initialized,
            },
        })
    return vectors


def tick_crossing_vectors() -> list[dict]:
    cases = []
    reward_slots = (
        RaydiumRewardGrowth(True, 80),
        RaydiumRewardGrowth(False, 90),
        RaydiumRewardGrowth(True, 120),
    )
    for zero_for_one in (True, False):
        tick = RaydiumClmmTick(
            tick_index=10,
            liquidity_net=100,
            liquidity_gross=200,
            orders_amount=0,
            part_filled_orders_remaining=0,
            initialized=True,
            fee_growth_outside_0_x64=10,
            fee_growth_outside_1_x64=20,
            reward_growths_outside_x64=(5, 11, 17),
            order_phase=0,
            unfilled_ratio_x64=0,
            padding=(0, 0, 0),
        )
        result = cross_tick_and_apply_liquidity(
            tick,
            current_liquidity=1000,
            fee_growth_global_0_x64=100,
            fee_growth_global_1_x64=200,
            reward_infos=reward_slots,
            zero_for_one=zero_for_one,
        )
        cases.append({
            "zero_for_one": zero_for_one,
            "current_liquidity": 1000,
            "fee_growth_global_0_x64": "100",
            "fee_growth_global_1_x64": "200",
            "tick": {
                "tick_index": 10,
                "liquidity_net": 100,
                "liquidity_gross": 200,
                "fee_growth_outside_0_x64": "10",
                "fee_growth_outside_1_x64": "20",
                "reward_growths_outside_x64": ["5", "11", "17"],
            },
            "rewards": [
                {"initialized": True, "reward_growth_global_x64": "80"},
                {"initialized": False, "reward_growth_global_x64": "90"},
                {"initialized": True, "reward_growth_global_x64": "120"},
            ],
            "expected": {
                "liquidity_net": result.liquidity_net,
                "liquidity_after": result.liquidity_after,
                "fee_growth_outside_0_x64": str(result.tick.fee_growth_outside_0_x64),
                "fee_growth_outside_1_x64": str(result.tick.fee_growth_outside_1_x64),
                "reward_growths_outside_x64": [str(value) for value in result.tick.reward_growths_outside_x64],
            },
        })
    return cases


def pool_finalization_vectors() -> list[dict]:
    cases = []
    for fee_on in (0, 1, 2):
        for zero_for_one in (True, False):
            dynamic_initial = make_dynamic_info()
            dynamic_updated = RaydiumClmmDynamicFeeInfo(
                filter_period=dynamic_initial.filter_period,
                decay_period=dynamic_initial.decay_period,
                reduction_factor=dynamic_initial.reduction_factor,
                dynamic_fee_control=dynamic_initial.dynamic_fee_control,
                max_volatility_accumulator=dynamic_initial.max_volatility_accumulator,
                tick_spacing_index_reference=10,
                volatility_reference=2500,
                volatility_accumulator=5000,
                last_update_timestamp=1060,
                padding=bytes(46),
            )
            pool = RaydiumClmmPoolState(
                pool_address=str(Pubkey.new_unique()),
                bump=1,
                amm_config=str(Pubkey.new_unique()),
                pool_creator=str(Pubkey.new_unique()),
                token_mint_0=str(Pubkey.new_unique()),
                token_mint_1=str(Pubkey.new_unique()),
                token_vault_0=str(Pubkey.new_unique()),
                token_vault_1=str(Pubkey.new_unique()),
                observation_key=str(Pubkey.new_unique()),
                mint_decimals_0=6,
                mint_decimals_1=6,
                tick_spacing=10,
                liquidity=1000,
                sqrt_price_x64=Q64,
                tick_current=0,
                status=0,
                fee_on=fee_on,
                seed_index=0,
                account_data_sha256="a" * 64,
                fee_growth_global_0_x64=10,
                fee_growth_global_1_x64=20,
                protocol_fees_token_0=100,
                protocol_fees_token_1=200,
                fund_fees_token_0=300,
                fund_fees_token_1=400,
                dynamic_fee_info=dynamic_initial,
            )
            fee_state = RaydiumFeeAccountingState(
                amount_specified_remaining=0,
                amount_calculated=100,
                liquidity=456,
                fee_growth_global_x64=999,
                lp_fee=7,
                protocol_fee=8,
                fund_fee=9,
            )
            iteration = RaydiumClmmSwapInnerIterationResult(
                fee_state=fee_state,
                tick_state=None,
                tick=12,
                sqrt_price_x64=Q64 + 1,
                dynamic_fee_state=RaydiumDynamicFeeSwapState(
                    dynamic_updated, 10, 3000, 10
                ),
                liquidity_next=456,
                price_step=None,
                limit_order_match=RaydiumLimitOrderMatch(),
                unfilled_amount_before=0,
                unfilled_amount_after=0,
                reached_initialized_tick=False,
                crossed_tick=False,
                should_break=True,
                swap_complete=True,
            )
            result = finalize_raydium_pool_after_swap(
                pool, iteration, zero_for_one=zero_for_one
            )
            updated = result.pool
            cases.append({
                "fee_on": fee_on,
                "zero_for_one": zero_for_one,
                "tick": 12,
                "sqrt_price_x64": str(Q64 + 1),
                "liquidity": 456,
                "lp_fee": 7,
                "protocol_fee": 8,
                "fund_fee": 9,
                "fee_growth_global_x64": "999",
                "dynamic_fee_info": {
                    "filter_period": dynamic_updated.filter_period,
                    "decay_period": dynamic_updated.decay_period,
                    "reduction_factor": dynamic_updated.reduction_factor,
                    "dynamic_fee_control": dynamic_updated.dynamic_fee_control,
                    "max_volatility_accumulator": dynamic_updated.max_volatility_accumulator,
                    "tick_spacing_index_reference": dynamic_updated.tick_spacing_index_reference,
                    "volatility_reference": dynamic_updated.volatility_reference,
                    "volatility_accumulator": dynamic_updated.volatility_accumulator,
                    "last_update_timestamp": dynamic_updated.last_update_timestamp,
                },
                "expected": {
                    "fee_token0": result.fee_token0,
                    "tick_current": updated.tick_current,
                    "sqrt_price_x64": str(updated.sqrt_price_x64),
                    "liquidity": str(updated.liquidity),
                    "fee_growth_global_0_x64": str(updated.fee_growth_global_0_x64),
                    "fee_growth_global_1_x64": str(updated.fee_growth_global_1_x64),
                    "protocol_fees_token_0": updated.protocol_fees_token_0,
                    "protocol_fees_token_1": updated.protocol_fees_token_1,
                    "fund_fees_token_0": updated.fund_fees_token_0,
                    "fund_fees_token_1": updated.fund_fees_token_1,
                    "dynamic_tick_reference": updated.dynamic_fee_info.tick_spacing_index_reference,
                    "dynamic_volatility_reference": updated.dynamic_fee_info.volatility_reference,
                    "dynamic_volatility_accumulator": updated.dynamic_fee_info.volatility_accumulator,
                    "dynamic_last_update_timestamp": updated.dynamic_fee_info.last_update_timestamp,
                },
            })
    return cases


def tick_array_bitmap_vectors() -> list[dict]:
    cases = []
    for start_index in (-307200, -600, 0, 600, 306600):
        pool = RaydiumClmmPoolState(
            pool_address=str(Pubkey.new_unique()),
            bump=1,
            amm_config=str(Pubkey.new_unique()),
            pool_creator=str(Pubkey.new_unique()),
            token_mint_0=str(Pubkey.new_unique()),
            token_mint_1=str(Pubkey.new_unique()),
            token_vault_0=str(Pubkey.new_unique()),
            token_vault_1=str(Pubkey.new_unique()),
            observation_key=str(Pubkey.new_unique()),
            mint_decimals_0=6,
            mint_decimals_1=6,
            tick_spacing=10,
            liquidity=1000,
            sqrt_price_x64=Q64,
            tick_current=0,
            status=0,
            fee_on=0,
            seed_index=0,
            account_data_sha256="a" * 64,
            tick_array_bitmap=(0,) * 16,
        )
        updated_pool, extension = _flip_bitmap(pool, None, start_index)
        if extension is not None:
            raise AssertionError("default bitmap vector unexpectedly used an extension")
        cases.append({
            "tick_spacing": 10,
            "start_index": start_index,
            "expected_bitmap": list(updated_pool.tick_array_bitmap),
        })

    for start_index in (307200, 307800, -307800, -308400, 443400, -444000):
        pool = RaydiumClmmPoolState(
            pool_address=str(Pubkey.new_unique()),
            bump=1,
            amm_config=str(Pubkey.new_unique()),
            pool_creator=str(Pubkey.new_unique()),
            token_mint_0=str(Pubkey.new_unique()),
            token_mint_1=str(Pubkey.new_unique()),
            token_vault_0=str(Pubkey.new_unique()),
            token_vault_1=str(Pubkey.new_unique()),
            observation_key=str(Pubkey.new_unique()),
            mint_decimals_0=6,
            mint_decimals_1=6,
            tick_spacing=10,
            liquidity=1000,
            sqrt_price_x64=Q64,
            tick_current=0,
            status=0,
            fee_on=0,
            seed_index=0,
            account_data_sha256="a" * 64,
            tick_array_bitmap=(0,) * 16,
        )
        extension = RaydiumClmmTickArrayBitmapExtension(
            address=str(Pubkey.new_unique()),
            pool_address=pool.pool_address,
            positive_bitmaps=tuple((0,) * 8 for _ in range(14)),
            negative_bitmaps=tuple((0,) * 8 for _ in range(14)),
            data_sha256="b" * 64,
        )
        _, updated_extension = _flip_bitmap(pool, extension, start_index)
        cases.append({
            "tick_spacing": 10,
            "start_index": start_index,
            "extension_positive": [list(row) for row in updated_extension.positive_bitmaps],
            "extension_negative": [list(row) for row in updated_extension.negative_bitmaps],
        })
    return cases

def observation_update_vectors() -> list[dict]:
    """Export Python-generated ObservationState transitions for the pinned Rust oracle."""
    pool_id = str(Pubkey.new_unique())
    observation_address, _ = derive_raydium_clmm_observation_address(pool_id)
    zero_padding = bytes(32)

    def make_state(
        *,
        initialized: bool,
        observation_index: int,
        recent_epoch: int = 123,
        overrides: tuple[tuple[int, int, int], ...] = (),
    ) -> RaydiumClmmObservationState:
        observations = [RaydiumClmmObservation(0, 0, zero_padding) for _ in range(100)]
        for index, timestamp, cumulative in overrides:
            observations[index] = RaydiumClmmObservation(timestamp, cumulative, zero_padding)
        return RaydiumClmmObservationState(
            address=observation_address,
            initialized=initialized,
            recent_epoch=recent_epoch,
            observation_index=observation_index,
            pool_id=pool_id,
            observations=tuple(observations),
            padding=zero_padding,
            data_sha256="0" * 64,
        )

    scenarios = (
        {
            "name": "first_write_then_14_15_second_cadence",
            "state": make_state(initialized=False, observation_index=0),
            "updates": (
                (100, 10),
                (114, 2),
                (115, 2),
            ),
        },
        {
            "name": "ring_wrap_and_i64_cumulative_wrap",
            "state": make_state(
                initialized=True,
                observation_index=99,
                overrides=((99, 100, (1 << 63) - 3),),
            ),
            "updates": ((115, 1),),
        },
        {
            "name": "backward_timestamp_noop",
            "state": make_state(
                initialized=True,
                observation_index=4,
                overrides=((4, 100, 77),),
            ),
            "updates": ((90, 9),),
        },
        {
            "name": "maximum_valid_timestamp_tick_product",
            "state": make_state(
                initialized=True,
                observation_index=0,
                overrides=((0, 0, 0),),
            ),
            "updates": ((0xFFFFFFFF, 0x7FFFFFFF),),
        },
    )

    vectors = []
    for scenario in scenarios:
        state = scenario["state"]
        initial = {
            "initialized": state.initialized,
            "recent_epoch": state.recent_epoch,
            "observation_index": state.observation_index,
            "pool_id": state.pool_id,
            "observations": [
                {
                    "index": index,
                    "block_timestamp": observation.block_timestamp,
                    "tick_cumulative": observation.tick_cumulative,
                }
                for index, observation in enumerate(state.observations)
                if observation.block_timestamp or observation.tick_cumulative
            ],
        }
        updates = []
        for timestamp, tick in scenario["updates"]:
            state = apply_raydium_observation_update(
                state, block_timestamp=timestamp, tick=tick
            )
            updates.append({
                "block_timestamp": timestamp,
                "tick": tick,
                "expected": {
                    "initialized": state.initialized,
                    "observation_index": state.observation_index,
                    "observations": [
                        {
                            "index": index,
                            "block_timestamp": observation.block_timestamp,
                            "tick_cumulative": observation.tick_cumulative,
                        }
                        for index, observation in enumerate(state.observations)
                        if observation.block_timestamp or observation.tick_cumulative
                    ],
                },
            })
        vectors.append({
            "name": scenario["name"],
            "initial": initial,
            "updates": updates,
        })
    return vectors


def observation_serialization_vector() -> dict:
    """Export a nontrivial ObservationState account image for Rust byte parity."""
    pool_id = str(Pubkey.new_unique())
    observation_address, _ = derive_raydium_clmm_observation_address(pool_id)
    zero_padding = bytes(32)
    observations = [RaydiumClmmObservation(0, 0, zero_padding) for _ in range(100)]
    observations[0] = RaydiumClmmObservation(1_700_000_001, -123456789, zero_padding)
    observations[7] = RaydiumClmmObservation(1_700_000_015, 987654321, zero_padding)
    observations[99] = RaydiumClmmObservation(0xFFFFFFFF, -(1 << 63) + 17, zero_padding)
    state = RaydiumClmmObservationState(
        address=observation_address,
        initialized=True,
        recent_epoch=0x0102030405060708,
        observation_index=99,
        pool_id=pool_id,
        observations=tuple(observations),
        padding=zero_padding,
        data_sha256="0" * 64,
    )
    encoded = encode_raydium_clmm_observation_state(state)
    return {
        "state": {
            "initialized": state.initialized,
            "recent_epoch": state.recent_epoch,
            "observation_index": state.observation_index,
            "pool_id": state.pool_id,
            "observations": [
                {
                    "index": index,
                    "block_timestamp": observation.block_timestamp,
                    "tick_cumulative": observation.tick_cumulative,
                }
                for index, observation in enumerate(state.observations)
                if observation.block_timestamp or observation.tick_cumulative
            ],
        },
        "serialized_hex": encoded.hex(),
        "serialized_length": len(encoded),
    }


def account_serialization_vectors() -> dict:
    """Export nontrivial PoolState, TickArrayState and bitmap-extension account bytes."""
    program = Pubkey.from_string("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK")
    config = Pubkey.new_unique()
    mint_0, mint_1 = sorted((Pubkey.new_unique(), Pubkey.new_unique()), key=bytes)
    pool_address, bump = Pubkey.find_program_address(
        [b"pool", bytes(config), bytes(mint_0), bytes(mint_1)],
        program,
    )
    creator = Pubkey.new_unique()
    vault_0, vault_1 = Pubkey.new_unique(), Pubkey.new_unique()
    observation = Pubkey.new_unique()
    base_pool_bytes = bytes(RAYDIUM_POOL_STATE_LENGTH)
    dynamic = RaydiumClmmDynamicFeeInfo(
        filter_period=60,
        decay_period=3600,
        reduction_factor=5000,
        dynamic_fee_control=1000,
        max_volatility_accumulator=10000,
        tick_spacing_index_reference=-5,
        volatility_reference=100,
        volatility_accumulator=200,
        last_update_timestamp=123456789,
        padding=bytes(46),
    )
    reward_mint, reward_vault, reward_authority = (
        Pubkey.new_unique(), Pubkey.new_unique(), Pubkey.new_unique()
    )
    rewards = (
        RaydiumClmmRewardInfo(
            reward_state=1,
            open_time=10,
            end_time=100,
            last_update_time=20,
            emissions_per_second_x64=1 << 64,
            reward_total_emitted=500,
            reward_claimed=100,
            token_mint=str(reward_mint),
            token_vault=str(reward_vault),
            authority=str(reward_authority),
            reward_growth_global_x64=1 << 65,
        ),
        RaydiumClmmRewardInfo(),
        RaydiumClmmRewardInfo(),
    )
    pool = RaydiumClmmPoolState(
        pool_address=str(pool_address),
        bump=bump,
        amm_config=str(config),
        pool_creator=str(creator),
        token_mint_0=str(mint_0),
        token_mint_1=str(mint_1),
        token_vault_0=str(vault_0),
        token_vault_1=str(vault_1),
        observation_key=str(observation),
        mint_decimals_0=6,
        mint_decimals_1=9,
        tick_spacing=10,
        liquidity=123456789012345,
        sqrt_price_x64=1 << 64,
        tick_current=-120,
        status=0,
        fee_on=2,
        seed_index=0,
        account_data_sha256=hashlib.sha256(base_pool_bytes).hexdigest(),
        fee_growth_global_0_x64=1 << 80,
        fee_growth_global_1_x64=1 << 81,
        protocol_fees_token_0=12345,
        protocol_fees_token_1=67890,
        fund_fees_token_0=111,
        fund_fees_token_1=222,
        tick_array_bitmap=tuple(
            [0x0123456789ABCDEF, 0xFEDCBA9876543210] + [0] * 14
        ),
        dynamic_fee_info=dynamic,
        reward_infos=rewards,
        account_data=base_pool_bytes,
    )
    pool_bytes = encode_raydium_clmm_pool_state(pool)
    pool_vector = {
        "bump": pool.bump,
        "amm_config": pool.amm_config,
        "pool_creator": pool.pool_creator,
        "token_mint_0": pool.token_mint_0,
        "token_mint_1": pool.token_mint_1,
        "token_vault_0": pool.token_vault_0,
        "token_vault_1": pool.token_vault_1,
        "observation_key": pool.observation_key,
        "mint_decimals_0": pool.mint_decimals_0,
        "mint_decimals_1": pool.mint_decimals_1,
        "tick_spacing": pool.tick_spacing,
        "liquidity": str(pool.liquidity),
        "sqrt_price_x64": str(pool.sqrt_price_x64),
        "tick_current": pool.tick_current,
        "fee_growth_global_0_x64": str(pool.fee_growth_global_0_x64),
        "fee_growth_global_1_x64": str(pool.fee_growth_global_1_x64),
        "protocol_fees_token_0": pool.protocol_fees_token_0,
        "protocol_fees_token_1": pool.protocol_fees_token_1,
        "status": pool.status,
        "fee_on": pool.fee_on,
        "seed_index": pool.seed_index,
        "tick_array_bitmap": list(pool.tick_array_bitmap),
        "fund_fees_token_0": pool.fund_fees_token_0,
        "fund_fees_token_1": pool.fund_fees_token_1,
        "dynamic_fee_info": {
            "filter_period": dynamic.filter_period,
            "decay_period": dynamic.decay_period,
            "reduction_factor": dynamic.reduction_factor,
            "dynamic_fee_control": dynamic.dynamic_fee_control,
            "max_volatility_accumulator": dynamic.max_volatility_accumulator,
            "tick_spacing_index_reference": dynamic.tick_spacing_index_reference,
            "volatility_reference": dynamic.volatility_reference,
            "volatility_accumulator": dynamic.volatility_accumulator,
            "last_update_timestamp": dynamic.last_update_timestamp,
        },
        "reward_infos": [
            {
                "reward_state": reward.reward_state,
                "open_time": reward.open_time,
                "end_time": reward.end_time,
                "last_update_time": reward.last_update_time,
                "emissions_per_second_x64": str(reward.emissions_per_second_x64),
                "reward_total_emitted": reward.reward_total_emitted,
                "reward_claimed": reward.reward_claimed,
                "token_mint": reward.token_mint,
                "token_vault": reward.token_vault,
                "authority": reward.authority,
                "reward_growth_global_x64": str(reward.reward_growth_global_x64),
            }
            for reward in rewards
        ],
        "serialized_hex": pool_bytes.hex(),
        "serialized_length": len(pool_bytes),
    }

    tick_pool = str(Pubkey.new_unique())
    tick_spacing = 10
    start_tick_index = -600
    tick_array_address = derive_raydium_clmm_tick_array_address(
        pool_address=tick_pool, start_tick_index=start_tick_index
    )
    ticks = []
    for index in range(RAYDIUM_TICK_ARRAY_SIZE):
        tick_index = start_tick_index + index * tick_spacing
        if index == 17:
            ticks.append(RaydiumClmmTick(
                tick_index=tick_index,
                liquidity_net=-321,
                liquidity_gross=1000,
                orders_amount=9,
                part_filled_orders_remaining=3,
                initialized=True,
                fee_growth_outside_0_x64=1 << 80,
                fee_growth_outside_1_x64=1 << 81,
                reward_growths_outside_x64=(1 << 64, 2 << 64, 3 << 64),
                order_phase=4,
                unfilled_ratio_x64=1 << 63,
            ))
        else:
            ticks.append(RaydiumClmmTick(
                tick_index=tick_index,
                liquidity_net=0,
                liquidity_gross=0,
                orders_amount=0,
                part_filled_orders_remaining=0,
                initialized=False,
            ))
    tick_array = RaydiumClmmTickArray(
        address=tick_array_address,
        pool_address=tick_pool,
        start_tick_index=start_tick_index,
        tick_spacing=tick_spacing,
        initialized_tick_count=1,
        recent_epoch=0x0102030405060708,
        ticks=tuple(ticks),
        data_sha256="0" * 64,
        padding=bytes((index * 3) % 256 for index in range(107)),
    )
    tick_array_bytes = encode_raydium_clmm_tick_array(tick_array)
    tick_array_vector = {
        "pool_id": tick_pool,
        "start_tick_index": start_tick_index,
        "tick_spacing": tick_spacing,
        "initialized_tick_count": tick_array.initialized_tick_count,
        "recent_epoch": tick_array.recent_epoch,
        "padding_hex": tick_array.padding.hex(),
        "ticks": [
            {
                "tick_index": tick.tick_index,
                "liquidity_net": str(tick.liquidity_net),
                "liquidity_gross": str(tick.liquidity_gross),
                "fee_growth_outside_0_x64": str(tick.fee_growth_outside_0_x64),
                "fee_growth_outside_1_x64": str(tick.fee_growth_outside_1_x64),
                "reward_growths_outside_x64": [str(value) for value in tick.reward_growths_outside_x64],
                "order_phase": tick.order_phase,
                "orders_amount": tick.orders_amount,
                "part_filled_orders_remaining": tick.part_filled_orders_remaining,
                "unfilled_ratio_x64": str(tick.unfilled_ratio_x64),
            }
            for tick in tick_array.ticks
        ],
        "serialized_hex": tick_array_bytes.hex(),
        "serialized_length": len(tick_array_bytes),
    }

    bitmap_pool = str(Pubkey.new_unique())
    positive = [[0] * 8 for _ in range(14)]
    negative = [[0] * 8 for _ in range(14)]
    positive[0][0] = 1
    positive[13][7] = 0xFFFFFFFFFFFFFFFF
    negative[0][7] = 0x0123456789ABCDEF
    negative[13][0] = 0xFEDCBA9876543210
    extension = RaydiumClmmTickArrayBitmapExtension(
        address=derive_raydium_tick_array_bitmap_extension_address(bitmap_pool),
        pool_address=bitmap_pool,
        positive_bitmaps=tuple(tuple(row) for row in positive),
        negative_bitmaps=tuple(tuple(row) for row in negative),
        data_sha256="0" * 64,
    )
    bitmap_bytes = encode_raydium_tick_array_bitmap_extension(extension)
    bitmap_vector = {
        "pool_id": bitmap_pool,
        "positive_bitmaps": positive,
        "negative_bitmaps": negative,
        "serialized_hex": bitmap_bytes.hex(),
        "serialized_length": len(bitmap_bytes),
    }
    return {
        "pool_state": pool_vector,
        "tick_array_state": tick_array_vector,
        "tick_array_bitmap_extension": bitmap_vector,
    }


def swap_replay_vector(zero_for_one: bool = False, is_base_input: bool = True, amount_specified: int = 1_000_000) -> dict:
    """Run one deterministic Python swap replay for the pinned Rust swap_internal oracle."""
    program = Pubkey.from_string("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK")
    config = Pubkey.new_unique()
    mint_0, mint_1 = sorted((Pubkey.new_unique(), Pubkey.new_unique()), key=bytes)
    pool_address, bump = Pubkey.find_program_address(
        [b"pool", bytes(config), bytes(mint_0), bytes(mint_1)],
        program,
    )
    observation_address, _ = derive_raydium_clmm_observation_address(str(pool_address))
    bitmap = [0] * 16
    bitmap[8] = 1
    base_pool_bytes = bytes(RAYDIUM_POOL_STATE_LENGTH)
    pool = RaydiumClmmPoolState(
        pool_address=str(pool_address),
        bump=bump,
        amm_config=str(config),
        pool_creator=str(Pubkey.new_unique()),
        token_mint_0=str(mint_0),
        token_mint_1=str(mint_1),
        token_vault_0=str(Pubkey.new_unique()),
        token_vault_1=str(Pubkey.new_unique()),
        observation_key=observation_address,
        mint_decimals_0=6,
        mint_decimals_1=6,
        tick_spacing=10,
        liquidity=1_000_000,
        sqrt_price_x64=get_sqrt_price_at_tick(10 if zero_for_one else 0),
        tick_current=10 if zero_for_one else 0,
        status=0,
        fee_on=0,
        seed_index=0,
        account_data_sha256=hashlib.sha256(base_pool_bytes).hexdigest(),
        tick_array_bitmap=tuple(bitmap),
        account_data=base_pool_bytes,
    )
    initial_pool_bytes = encode_raydium_clmm_pool_state(pool)
    pool = replace(
        pool,
        account_data=initial_pool_bytes,
        account_data_sha256=hashlib.sha256(initial_pool_bytes).hexdigest(),
    )

    ticks = []
    for index in range(RAYDIUM_TICK_ARRAY_SIZE):
        tick_index = index * pool.tick_spacing
        if index == (0 if zero_for_one else 1):
            ticks.append(RaydiumClmmTick(
                tick_index=tick_index,
                liquidity_net=500,
                liquidity_gross=1000,
                orders_amount=0,
                part_filled_orders_remaining=0,
                initialized=True,
            ))
        else:
            ticks.append(RaydiumClmmTick(
                tick_index=tick_index,
                liquidity_net=0,
                liquidity_gross=0,
                orders_amount=0,
                part_filled_orders_remaining=0,
                initialized=False,
            ))
    tick_array = RaydiumClmmTickArray(
        address=derive_raydium_clmm_tick_array_address(
            pool_address=pool.pool_address,
            start_tick_index=0,
        ),
        pool_address=pool.pool_address,
        start_tick_index=0,
        tick_spacing=pool.tick_spacing,
        initialized_tick_count=1,
        recent_epoch=1,
        ticks=tuple(ticks),
        data_sha256="0" * 64,
    )
    initial_tick_array_bytes = encode_raydium_clmm_tick_array(tick_array)
    tick_array = replace(
        tick_array,
        data_sha256=hashlib.sha256(initial_tick_array_bytes).hexdigest(),
    )

    observation = RaydiumClmmObservationState(
        address=observation_address,
        initialized=False,
        recent_epoch=1,
        observation_index=0,
        pool_id=pool.pool_address,
        observations=tuple(
            RaydiumClmmObservation(0, 0, bytes(32))
            for _ in range(100)
        ),
        padding=bytes(32),
        data_sha256="0" * 64,
    )
    initial_observation_bytes = encode_raydium_clmm_observation_state(observation)
    observation = replace(
        observation,
        data_sha256=hashlib.sha256(initial_observation_bytes).hexdigest(),
    )

    sqrt_price_limit = get_sqrt_price_at_tick(0 if zero_for_one else 10)
    block_timestamp = 1_700_000_000
    recent_epoch = 0  # host-side Solana Clock default used by the pinned Rust test harness
    base_fee_rate = 3_000
    protocol_fee_rate = 1_000
    fund_fee_rate = 500
    snapshot = RaydiumClmmTickArraySnapshot(
        route_message_sha256="a" * 64,
        simulation_response_id="simulation-1",
        simulation_context_slot=100,
        provider_id="fixture-rpc",
        cluster="devnet",
        genesis_hash="fixture-genesis",
        context_slot=101,
        tick_arrays=(tick_array,),
        cpi_account_order=(tick_array.address,),
    )
    result = run_raydium_clmm_swap_replay(
        pool,
        observation,
        snapshot,
        amount_specified=amount_specified,
        sqrt_price_limit_x64=sqrt_price_limit,
        zero_for_one=zero_for_one,
        is_base_input=is_base_input,
        block_timestamp=block_timestamp,
        recent_epoch=recent_epoch,
        base_fee_rate=base_fee_rate,
        protocol_fee_rate=protocol_fee_rate,
        fund_fee_rate=fund_fee_rate,
    )
    final_tick_array_bytes = encode_raydium_clmm_tick_array(result.tick_arrays[0])
    final_observation_bytes = encode_raydium_clmm_observation_state(result.observation_state)
    return {
        "initial_pool_hex": initial_pool_bytes.hex(),
        "initial_tick_array_hex": initial_tick_array_bytes.hex(),
        "initial_observation_hex": initial_observation_bytes.hex(),
        "amount_specified": amount_specified,
        "sqrt_price_limit_x64": str(sqrt_price_limit),
        "zero_for_one": zero_for_one,
        "is_base_input": is_base_input,
        "block_timestamp": block_timestamp,
        "trade_fee_rate": base_fee_rate,
        "protocol_fee_rate": protocol_fee_rate,
        "fund_fee_rate": fund_fee_rate,
        "normalize_tick_array_epoch": any(item.reached_initialized_tick for item in result.iterations),
        "expected": {
            "amount_0": result.settlement.amount_0,
            "amount_1": result.settlement.amount_1,
            "trade_fee_0": result.settlement.trade_fee_0,
            "trade_fee_1": result.settlement.trade_fee_1,
            "tick": result.pool.tick_current,
            "sqrt_price_x64": str(result.pool.sqrt_price_x64),
            "liquidity": str(result.pool.liquidity),
            "remaining_amount": result.fee_state.amount_specified_remaining,
            "pool_hex": result.pool.account_data.hex(),
            "tick_array_hex": final_tick_array_bytes.hex(),
            "observation_hex": final_observation_bytes.hex(),
        },
    }


def build_vectors() -> dict:
    ticks = (-443636, -100000, -1000, -100, -1, 0, 1, 10, 100, 1000, 100000, 443636)
    tick_vectors = [
        {"tick": tick, "sqrt_price_x64": str(get_sqrt_price_at_tick(tick))}
        for tick in ticks
    ]
    swap_vectors = []
    for case in SWAP_CASES:
        current = get_sqrt_price_at_tick(case["current_tick"])
        target = get_sqrt_price_at_tick(case["target_tick"])
        result = compute_swap_step(
            current,
            target,
            case["liquidity"],
            case["amount_remaining"],
            case["fee_rate"],
            is_base_input=case["is_base_input"],
            zero_for_one=case["zero_for_one"],
            is_fee_on_input=case["is_fee_on_input"],
        )
        swap_vectors.append(
            {
                **case,
                "sqrt_price_current_x64": str(current),
                "sqrt_price_target_x64": str(target),
                "expected": {
                    "sqrt_price_next_x64": str(result.sqrt_price_next_x64),
                    "amount_in": result.amount_in,
                    "amount_out": result.amount_out,
                    "fee_amount": result.fee_amount,
                    "reached_target": result.reached_target,
                },
            }
        )
    return {
        "source_commit": "6d7a0f82d2f293c553c69aaf943377367c6c4cc6",
        "ticks": tick_vectors,
        "swap_steps": swap_vectors,
        "dynamic_fee": dynamic_fee_vectors(),
        "limit_orders": limit_order_vectors(),
        "tick_crossing": tick_crossing_vectors(),
        "pool_finalization": pool_finalization_vectors(),
        "tick_array_bitmap": tick_array_bitmap_vectors(),
        "observation_updates": observation_update_vectors(),
        "observation_serialization": observation_serialization_vector(),
        "account_serialization": account_serialization_vectors(),
        "swap_replays": [
            swap_replay_vector(False, True, 1_000_000),
            swap_replay_vector(True, True, 1_000_000),
            swap_replay_vector(False, False, 100),
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(build_vectors(), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
