#!/usr/bin/env python3
"""Export deterministic Python math vectors for the pinned upstream Rust oracle."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from veltrix.raydium_clmm_dynamic_fee import (
    tick_spacing_index_from_tick,
    update_dynamic_fee_reference,
    update_volatility_accumulator,
    compute_total_fee_rate,
)
from veltrix.raydium_clmm_pool_state import RaydiumClmmDynamicFeeInfo
from veltrix.raydium_clmm_swap_step_math import compute_swap_step
from veltrix.raydium_clmm_tick_math import get_sqrt_price_at_tick


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
