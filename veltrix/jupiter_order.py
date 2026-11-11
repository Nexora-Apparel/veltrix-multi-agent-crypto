"""Jupiter Swap API V2 order intake with no signing or execution.

The taker is explicit, transaction versions are capped at v0, gasless and
third-party fee-payer orders are rejected, and the returned message is only
prepared for subsequent independent validation/simulation.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import math
import time
from typing import Any

import requests
from solders.message import MessageV0, to_bytes_versioned
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from veltrix.jupiter_quote import (
    ALLOWED_ROUTERS,
    JUPITER_ORDER_URL,
    JupiterRouteStep,
    _canonical_pubkey,
    _finite_number,
    _raw_amount,
)


class JupiterOrderIntakeError(RuntimeError):
    """Raised when Jupiter order data is malformed or violates local policy."""


@dataclass(frozen=True, slots=True)
class JupiterOrderPolicy:
    allowed_routers: tuple[str, ...] = ("metis",)
    max_price_impact_pct: float = 5.0
    max_fee_bps: float = 300.0
    max_route_steps: int = 12
    require_self_paid_fees: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_routers, tuple) or not self.allowed_routers:
            raise JupiterOrderIntakeError("allowed_routers must be a non-empty immutable tuple")
        if any(router not in ALLOWED_ROUTERS for router in self.allowed_routers):
            raise JupiterOrderIntakeError("allowed_routers contains an unknown Jupiter router")
        if len(set(self.allowed_routers)) != len(self.allowed_routers):
            raise JupiterOrderIntakeError("allowed_routers must not contain duplicates")
        if set(self.allowed_routers) != {"metis"}:
            raise JupiterOrderIntakeError("only Metis is approved until alternate routers have independent semantic decoders")
        for field in ("max_price_impact_pct", "max_fee_bps"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise JupiterOrderIntakeError(f"{field} must be finite and non-negative")
        if isinstance(self.max_route_steps, bool) or not isinstance(self.max_route_steps, int) or not 1 <= self.max_route_steps <= 64:
            raise JupiterOrderIntakeError("max_route_steps must be between 1 and 64")
        if not isinstance(self.require_self_paid_fees, bool):
            raise JupiterOrderIntakeError("require_self_paid_fees must be a boolean")


@dataclass(frozen=True, slots=True)
class JupiterOrderTransaction:
    provider_id: str
    request_id: str
    quote_id: str | None
    observed_at_epoch_ms: int
    taker: str
    fee_payer: str
    input_mint: str
    output_mint: str
    input_amount_raw: int
    output_amount_raw: int
    minimum_output_amount_raw: int
    price_impact_pct: float
    fee_bps: float
    router: str
    transaction_version: int
    last_valid_block_height: int
    recent_blockhash: str
    message_bytes: bytes
    serialized_transaction_bytes: bytes
    route_plan: tuple[JupiterRouteStep, ...]
    signature_fee_lamports: int
    prioritization_fee_lamports: int
    rent_fee_lamports: int


class JupiterSwapV2OrderAdapter:
    """Fetches an unsigned v0 Jupiter order for later validation and simulation."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 8.0,
        session: Any | None = None,
        clock_ms: Any | None = None,
    ) -> None:
        if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
            raise JupiterOrderIntakeError("api_key must be non-empty when provided")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 20:
            raise JupiterOrderIntakeError("timeout_seconds must be greater than zero and at most 20")
        self._api_key = api_key
        self.timeout_seconds = float(timeout_seconds)
        self._session = session or requests.Session()
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def order_exact_in(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        *,
        taker: str,
        policy: JupiterOrderPolicy,
    ) -> JupiterOrderTransaction:
        input_mint = _canonical_pubkey(input_mint, "input_mint")
        output_mint = _canonical_pubkey(output_mint, "output_mint")
        taker = _canonical_pubkey(taker, "taker")
        if input_mint == output_mint:
            raise JupiterOrderIntakeError("input and output mints must differ")
        if isinstance(amount_raw, bool) or not isinstance(amount_raw, int) or amount_raw <= 0:
            raise JupiterOrderIntakeError("amount_raw must be a positive integer")
        if not isinstance(policy, JupiterOrderPolicy):
            raise JupiterOrderIntakeError("a JupiterOrderPolicy is required")

        excluded = sorted(ALLOWED_ROUTERS - set(policy.allowed_routers))
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "swapMode": "ExactIn",
            "taker": taker,
            "maxSupportedTransactionVersion": "0",
        }
        if excluded:
            params["excludeRouters"] = ",".join(excluded)
        headers = {"Accept": "application/json"}
        if self._api_key is not None:
            headers["x-api-key"] = self._api_key
        try:
            response = self._session.get(
                JUPITER_ORDER_URL,
                params=params,
                headers=headers,
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise JupiterOrderIntakeError(f"Jupiter order endpoint returned HTTP {response.status_code}")
            body = response.json()
        except JupiterOrderIntakeError:
            raise
        except Exception as exc:
            raise JupiterOrderIntakeError("Jupiter order transport or JSON decoding failed") from exc

        if not isinstance(body, dict):
            raise JupiterOrderIntakeError("Jupiter returned a malformed order object")
        if body.get("error") is not None or body.get("errorCode") is not None:
            raise JupiterOrderIntakeError("Jupiter could not produce the requested order")
        request_id = body.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            raise JupiterOrderIntakeError("Jupiter order omitted requestId")
        if body.get("taker") != taker:
            raise JupiterOrderIntakeError("Jupiter order taker does not match the requested wallet")
        router = body.get("router")
        if router not in policy.allowed_routers:
            raise JupiterOrderIntakeError("Jupiter selected a router outside local policy")

        response_input = _canonical_pubkey(body.get("inputMint"), "response inputMint")
        response_output = _canonical_pubkey(body.get("outputMint"), "response outputMint")
        if response_input != input_mint or response_output != output_mint:
            raise JupiterOrderIntakeError("Jupiter order mint pair differs from the request")
        input_amount = _raw_amount(body.get("inAmount"), "inAmount")
        output_amount = _raw_amount(body.get("outAmount"), "outAmount")
        minimum_output = _raw_amount(body.get("otherAmountThreshold"), "otherAmountThreshold")
        if input_amount != amount_raw:
            raise JupiterOrderIntakeError("Jupiter order input amount differs from the requested amount")
        if minimum_output > output_amount:
            raise JupiterOrderIntakeError("minimum output threshold exceeds quoted output")
        impact = _finite_number(body.get("priceImpact"), "priceImpact")
        if impact > policy.max_price_impact_pct:
            raise JupiterOrderIntakeError("quoted price impact exceeds policy")
        fee_bps = _finite_number(body.get("feeBps", 0), "feeBps")
        if fee_bps > policy.max_fee_bps:
            raise JupiterOrderIntakeError("quoted fee basis points exceed policy")

        transaction_version = body.get("transactionVersion")
        if isinstance(transaction_version, bool) or transaction_version != 0:
            raise JupiterOrderIntakeError("only Solana transaction version 0 is supported")
        last_valid = body.get("lastValidBlockHeight")
        if isinstance(last_valid, str) and last_valid.isdigit():
            last_valid = int(last_valid)
        if isinstance(last_valid, bool) or not isinstance(last_valid, int) or last_valid <= 0:
            raise JupiterOrderIntakeError("lastValidBlockHeight is missing or invalid")

        transaction_b64 = body.get("transaction")
        if not isinstance(transaction_b64, str) or not transaction_b64:
            raise JupiterOrderIntakeError("Jupiter order omitted its transaction payload")
        try:
            transaction_bytes = base64.b64decode(transaction_b64, validate=True)
            transaction = VersionedTransaction.from_bytes(transaction_bytes)
            message = transaction.message
            if not isinstance(message, MessageV0):
                raise JupiterOrderIntakeError("Jupiter order did not return a v0 message")
            message.sanitize()
            message_bytes = to_bytes_versioned(message)
        except JupiterOrderIntakeError:
            raise
        except Exception as exc:
            raise JupiterOrderIntakeError("Jupiter transaction payload is not a valid serialized v0 transaction") from exc

        required_signatures = int(message.header.num_required_signatures)
        if required_signatures < 1 or len(transaction.signatures) != required_signatures:
            raise JupiterOrderIntakeError("Jupiter transaction signer vector does not match the message header")
        if any(signature != Signature.default() for signature in transaction.signatures):
            raise JupiterOrderIntakeError("pre-signed or partially signed Jupiter transactions are not accepted")
        if not message.account_keys or str(message.account_keys[0]) != taker:
            raise JupiterOrderIntakeError("taker wallet must be the transaction fee payer")
        if not message.is_signer(0):
            raise JupiterOrderIntakeError("taker fee payer is not a required signer")

        gasless = body.get("gasless")
        if gasless is not False:
            raise JupiterOrderIntakeError("gasless or unspecified fee sponsorship is not approved")
        if policy.require_self_paid_fees:
            for field in ("signatureFeePayer", "prioritizationFeePayer", "rentFeePayer"):
                payer = body.get(field)
                if payer is not None and _canonical_pubkey(payer, field) != taker:
                    raise JupiterOrderIntakeError(f"{field} is not the requested taker wallet")
            if body.get("signatureFeePayer") != taker:
                raise JupiterOrderIntakeError("signature fee payer must be the requested taker wallet")

        route = body.get("routePlan")
        if not isinstance(route, list) or not route or len(route) > policy.max_route_steps:
            raise JupiterOrderIntakeError("routePlan is missing, empty, or exceeds the configured step limit")
        steps: list[JupiterRouteStep] = []
        for index, item in enumerate(route):
            if not isinstance(item, dict) or not isinstance(item.get("swapInfo"), dict):
                raise JupiterOrderIntakeError(f"routePlan step {index} is malformed")
            info = item["swapInfo"]
            label = info.get("label")
            if not isinstance(label, str) or not label:
                raise JupiterOrderIntakeError(f"routePlan step {index} has no DEX label")
            percent = _finite_number(item.get("percent"), f"routePlan[{index}].percent")
            bps = item.get("bps")
            if percent < 0 or percent > 100 or isinstance(bps, bool) or not isinstance(bps, int) or not 0 <= bps <= 10_000:
                raise JupiterOrderIntakeError(f"routePlan step {index} has invalid split percentages")
            steps.append(JupiterRouteStep(
                amm_key=_canonical_pubkey(info.get("ammKey"), f"routePlan[{index}].ammKey"),
                label=label,
                input_mint=_canonical_pubkey(info.get("inputMint"), f"routePlan[{index}].inputMint"),
                output_mint=_canonical_pubkey(info.get("outputMint"), f"routePlan[{index}].outputMint"),
                input_amount_raw=_raw_amount(info.get("inAmount"), f"routePlan[{index}].inAmount"),
                output_amount_raw=_raw_amount(info.get("outAmount"), f"routePlan[{index}].outAmount"),
                percent=percent,
                bps=bps,
            ))

        def fee_value(name: str) -> int:
            value = body.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise JupiterOrderIntakeError(f"{name} must be a non-negative integer")
            return value

        quote_id = body.get("quoteId")
        if quote_id is not None and not isinstance(quote_id, str):
            raise JupiterOrderIntakeError("quoteId is malformed")
        observed_at = self._clock_ms()
        if isinstance(observed_at, bool) or not isinstance(observed_at, int) or observed_at <= 0:
            raise JupiterOrderIntakeError("local order observation clock is invalid")

        return JupiterOrderTransaction(
            provider_id="jupiter-swap-v2",
            request_id=request_id,
            quote_id=quote_id,
            observed_at_epoch_ms=observed_at,
            taker=taker,
            fee_payer=str(message.account_keys[0]),
            input_mint=input_mint,
            output_mint=output_mint,
            input_amount_raw=input_amount,
            output_amount_raw=output_amount,
            minimum_output_amount_raw=minimum_output,
            price_impact_pct=impact,
            fee_bps=fee_bps,
            router=router,
            transaction_version=transaction_version,
            last_valid_block_height=last_valid,
            recent_blockhash=str(message.recent_blockhash),
            message_bytes=message_bytes,
            serialized_transaction_bytes=transaction_bytes,
            route_plan=tuple(steps),
            signature_fee_lamports=fee_value("signatureFeeLamports"),
            prioritization_fee_lamports=fee_value("prioritizationFeeLamports"),
            rent_fee_lamports=fee_value("rentFeeLamports"),
        )


    def order_exact_in_for_configured_taker(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        *,
        policy: JupiterOrderPolicy,
    ) -> JupiterOrderTransaction:
        """Request an unsigned order using the configured public taker address."""
        from veltrix.config import settings

        taker = settings().jupiter_taker_public_key
        return self.order_exact_in(
            input_mint,
            output_mint,
            amount_raw,
            taker=taker,
            policy=policy,
        )
