"""Fetch and persist a quote-only Jupiter V2 preflight without a wallet balance.

This is intentionally not an order or transaction. It does not set a taker,
construct a transaction, call Solana RPC, sign, or broadcast anything.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

from veltrix.jupiter_quote import JupiterQuotePolicy, JupiterSwapV2QuoteAdapter


WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DEFAULT_AMOUNT_RAW = 5_000_000  # 0.005 SOL (9 decimals)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Request a read-only Jupiter SOL/USDC quote.")
    parser.add_argument("--amount-raw", type=int, default=DEFAULT_AMOUNT_RAW)
    parser.add_argument("--output", type=Path, default=Path("artifacts/jupiter-quote-preflight.json"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.amount_raw != DEFAULT_AMOUNT_RAW:
        print("This preflight is pinned to the approved 0.005 SOL test amount.", file=sys.stderr)
        return 2
    adapter = JupiterSwapV2QuoteAdapter(api_key=os.getenv("JUPITER_API_KEY") or None)
    policy = JupiterQuotePolicy(
        allowed_routers=("metis", "jupiterz", "dflow", "okx"),
        max_price_impact_pct=5.0,
        max_fee_bps=300.0,
        max_route_steps=12,
    )
    try:
        quote = adapter.quote_exact_in(
            WSOL_MINT, USDC_MINT, args.amount_raw, policy=policy
        )
    except Exception as exc:
        report = {
            "evidence_type": "jupiter_quote_only_preflight",
            "status": "failed",
            "input_mint": WSOL_MINT,
            "output_mint": USDC_MINT,
            "input_amount_raw": args.amount_raw,
            "input_amount_sol": "0.005",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "wallet_balance_required": False,
            "transaction_constructed": False,
            "signed": False,
            "submitted": False,
            "execution_ready": False,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 1

    report = {
        "evidence_type": "jupiter_quote_only_preflight",
        "status": "quote_received",
        "quote": asdict(quote),
        "wallet_balance_required": False,
        "taker_wallet_bound": False,
        "transaction_constructed": False,
        "signed": False,
        "submitted": False,
        "execution_ready": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
