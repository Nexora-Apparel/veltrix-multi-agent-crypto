"""Request an unsigned Jupiter v0 order; never sign, simulate, or submit it."""
from __future__ import annotations

import argparse
import base64
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys

from veltrix.jupiter_order import JupiterOrderPolicy, JupiterSwapV2OrderAdapter
from veltrix.jupiter_quote_preflight import DEFAULT_AMOUNT_RAW, USDC_MINT, WSOL_MINT
from veltrix.config import settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Request an unsigned Jupiter SOL/USDC order.")
    parser.add_argument("--amount-raw", type=int, default=DEFAULT_AMOUNT_RAW)
    parser.add_argument("--output", type=Path, default=Path("artifacts/jupiter-unsigned-order-preflight.json"))
    return parser


def _write(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    args = build_parser().parse_args()
    if args.amount_raw != DEFAULT_AMOUNT_RAW:
        print("This preflight is pinned to the approved 0.005 SOL test amount.", file=sys.stderr)
        return 2

    taker = settings().jupiter_taker_public_key
    adapter = JupiterSwapV2OrderAdapter(api_key=os.getenv("JUPITER_API_KEY") or None)
    policy = JupiterOrderPolicy(
        allowed_routers=("metis",),
        max_price_impact_pct=5.0,
        max_fee_bps=300.0,
        max_route_steps=12,
        require_self_paid_fees=True,
    )
    try:
        order = adapter.order_exact_in_for_configured_taker(
            WSOL_MINT, USDC_MINT, args.amount_raw, policy=policy
        )
    except Exception as exc:
        report = {
            "evidence_type": "jupiter_unsigned_order_preflight",
            "status": "failed",
            "provider_id": "jupiter-swap-v2",
            "taker": taker,
            "input_mint": WSOL_MINT,
            "output_mint": USDC_MINT,
            "input_amount_raw": args.amount_raw,
            "input_amount_sol": "0.005",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "wallet_balance_checked": False,
            "funds_moved": False,
            "transaction_constructed": False,
            "transaction_signed": False,
            "transaction_submitted": False,
            "transaction_broadcast": False,
            "execution_ready": False,
        }
        _write(args.output, report)
        print(json.dumps(report, indent=2))
        return 1

    tx = order.serialized_transaction_bytes
    message = order.message_bytes
    report = {
        "evidence_type": "jupiter_unsigned_order_preflight",
        "status": "unsigned_order_received",
        "provider_id": order.provider_id,
        "request_id": order.request_id,
        "quote_id": order.quote_id,
        "observed_at_epoch_ms": order.observed_at_epoch_ms,
        "taker": order.taker,
        "fee_payer": order.fee_payer,
        "input_mint": order.input_mint,
        "output_mint": order.output_mint,
        "input_amount_raw": order.input_amount_raw,
        "output_amount_raw": order.output_amount_raw,
        "minimum_output_amount_raw": order.minimum_output_amount_raw,
        "price_impact_pct": order.price_impact_pct,
        "fee_bps": order.fee_bps,
        "router": order.router,
        "transaction_version": order.transaction_version,
        "last_valid_block_height": order.last_valid_block_height,
        "recent_blockhash": order.recent_blockhash,
        "route_plan": [asdict(step) for step in order.route_plan],
        "signature_fee_lamports": order.signature_fee_lamports,
        "prioritization_fee_lamports": order.prioritization_fee_lamports,
        "rent_fee_lamports": order.rent_fee_lamports,
        "unsigned_transaction_base64": base64.b64encode(tx).decode("ascii"),
        "message_base64": base64.b64encode(message).decode("ascii"),
        "transaction_sha256": hashlib.sha256(tx).hexdigest(),
        "message_sha256": hashlib.sha256(message).hexdigest(),
        "wallet_balance_checked": False,
        "funds_moved": False,
        "transaction_constructed": True,
        "transaction_signed": False,
        "transaction_submitted": False,
        "transaction_broadcast": False,
        "execution_ready": False,
    }
    _write(args.output, report)
    print(json.dumps({k: v for k, v in report.items() if k not in {"unsigned_transaction_base64", "message_base64"}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
