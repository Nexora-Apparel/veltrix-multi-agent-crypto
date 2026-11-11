#!/usr/bin/env python3
"""Export deterministic Python math vectors for the pinned upstream Rust oracle."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

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
