"""Read-only Jupiter Swap API V2 quote adapter.

This adapter intentionally omits the taker parameter. Jupiter therefore returns
a quote with no assembled transaction. It cannot sign, execute, or broadcast.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any

import requests
from solders.pubkey import Pubkey


JUPITER_ORDER_URL = "https://api.jup.ag/swap/v2/order"
ALLOWED_ROUTERS = frozenset({"metis", "jupiterz", "dflow", "okx"})


class JupiterQuoteError(RuntimeError):
    """Raised when Jupiter returns an invalid, unsafe, or incomplete quote."""


def _canonical_pubkey(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise JupiterQuoteError(f"{field} must be a non-empty public key")
    try:
        canonical = str(Pubkey.from_string(value))
    except Exception as exc:
        raise JupiterQuoteError(f"{field} is not a valid Solana public key") from exc
    if canonical != value:
        raise JupiterQuoteError(f"{field} is not canonical base58")
    return canonical


def _raw_amount(value: object, field: str) -> int:
    if not isinstance(value, str) or not value or not value.isdigit():
        raise JupiterQuoteError(f"{field} must be a decimal integer string")
    amount = int(value)
    if amount <= 0:
        raise JupiterQuoteError(f"{field} must be positive")
    return amount


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise JupiterQuoteError(f"{field} must be a finite number")
    return float(value)


@dataclass(frozen=True, slots=True)
class JupiterRouteStep:
    amm_key: str
    label: str
    input_mint: str
    output_mint: str
    input_amount_raw: int
    output_amount_raw: int
    percent: float
    bps: int


@dataclass(frozen=True, slots=True)
class JupiterQuote:
    provider_id: str
    request_id: str
    quote_id: str | None
    observed_at_epoch_ms: int
    input_mint: str
    output_mint: str
    input_amount_raw: int
    output_amount_raw: int
    minimum_output_amount_raw: int
    price_impact_pct: float
    fee_bps: float
    fee_mint: str | None
    router: str
    mode: str | None
    slippage_bps: int | None
    signature_fee_lamports: int
    prioritization_fee_lamports: int
    rent_fee_lamports: int
    gasless: bool
    expire_at: str | None
    route_plan: tuple[JupiterRouteStep, ...]


@dataclass(frozen=True, slots=True)
class JupiterQuotePolicy:
    allowed_routers: tuple[str, ...] = ("metis", "jupiterz", "dflow", "okx")
    max_price_impact_pct: float = 5.0
    max_fee_bps: float = 300.0
    max_route_steps: int = 12

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_routers, tuple) or not self.allowed_routers:
            raise JupiterQuoteError("allowed_routers must be a non-empty immutable tuple")
        if any(router not in ALLOWED_ROUTERS for router in self.allowed_routers):
            raise JupiterQuoteError("allowed_routers contains an unknown Jupiter router")
        if len(set(self.allowed_routers)) != len(self.allowed_routers):
            raise JupiterQuoteError("allowed_routers must not contain duplicates")
        for field in ("max_price_impact_pct", "max_fee_bps"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise JupiterQuoteError(f"{field} must be finite and non-negative")
        if isinstance(self.max_route_steps, bool) or not isinstance(self.max_route_steps, int) or not 1 <= self.max_route_steps <= 64:
            raise JupiterQuoteError("max_route_steps must be between 1 and 64")


class JupiterSwapV2QuoteAdapter:
    """Fetches quote-only Jupiter orders with no taker and no transaction payload."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 8.0,
        session: Any | None = None,
        clock_ms: Any | None = None,
    ) -> None:
        if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
            raise JupiterQuoteError("api_key must be non-empty when provided")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 20:
            raise JupiterQuoteError("timeout_seconds must be greater than zero and at most 20")
        self._api_key = api_key
        self.timeout_seconds = float(timeout_seconds)
        self._session = session or requests.Session()
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def quote_exact_in(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        *,
        policy: JupiterQuotePolicy,
    ) -> JupiterQuote:
        input_mint = _canonical_pubkey(input_mint, "input_mint")
        output_mint = _canonical_pubkey(output_mint, "output_mint")
        if input_mint == output_mint:
            raise JupiterQuoteError("input and output mints must differ")
        if isinstance(amount_raw, bool) or not isinstance(amount_raw, int) or amount_raw <= 0:
            raise JupiterQuoteError("amount_raw must be a positive integer")
        if not isinstance(policy, JupiterQuotePolicy):
            raise JupiterQuoteError("a JupiterQuotePolicy is required")

        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "swapMode": "ExactIn",
        }
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
                raise JupiterQuoteError(f"Jupiter order endpoint returned HTTP {response.status_code}")
            body = response.json()
        except JupiterQuoteError:
            raise
        except Exception as exc:
            raise JupiterQuoteError("Jupiter quote transport or JSON decoding failed") from exc

        if not isinstance(body, dict):
            raise JupiterQuoteError("Jupiter returned a malformed quote object")
        if body.get("error") is not None or body.get("errorCode") is not None:
            raise JupiterQuoteError("Jupiter could not produce an executable order quote")
        if body.get("transaction") is not None:
            raise JupiterQuoteError("quote-only request unexpectedly returned a transaction payload")
        if body.get("taker") not in (None, ""):
            raise JupiterQuoteError("quote-only response unexpectedly binds a taker wallet")

        request_id = body.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            raise JupiterQuoteError("Jupiter quote omitted requestId")
        router = body.get("router")
        if router not in policy.allowed_routers:
            raise JupiterQuoteError("Jupiter selected a router outside policy")

        response_input = _canonical_pubkey(body.get("inputMint"), "response inputMint")
        response_output = _canonical_pubkey(body.get("outputMint"), "response outputMint")
        if response_input != input_mint or response_output != output_mint:
            raise JupiterQuoteError("Jupiter quote mint pair differs from the request")
        input_amount = _raw_amount(body.get("inAmount"), "inAmount")
        output_amount = _raw_amount(body.get("outAmount"), "outAmount")
        minimum_output = _raw_amount(body.get("otherAmountThreshold"), "otherAmountThreshold")
        if input_amount != amount_raw:
            raise JupiterQuoteError("Jupiter quote input amount differs from the requested amount")
        if minimum_output > output_amount:
            raise JupiterQuoteError("minimum output threshold exceeds quoted output")

        impact = _finite_number(body.get("priceImpact"), "priceImpact")
        if impact > policy.max_price_impact_pct:
            raise JupiterQuoteError("quoted price impact exceeds policy")
        fee_bps = _finite_number(body.get("feeBps", 0), "feeBps")
        if fee_bps > policy.max_fee_bps:
            raise JupiterQuoteError("quoted fee basis points exceed policy")

        route = body.get("routePlan")
        if not isinstance(route, list) or not route or len(route) > policy.max_route_steps:
            raise JupiterQuoteError("routePlan is missing, empty, or exceeds the configured step limit")
        steps: list[JupiterRouteStep] = []
        for index, item in enumerate(route):
            if not isinstance(item, dict) or not isinstance(item.get("swapInfo"), dict):
                raise JupiterQuoteError(f"routePlan step {index} is malformed")
            info = item["swapInfo"]
            amm_key = _canonical_pubkey(info.get("ammKey"), f"routePlan[{index}].ammKey")
            label = info.get("label")
            if not isinstance(label, str) or not label:
                raise JupiterQuoteError(f"routePlan step {index} has no DEX label")
            step_input = _canonical_pubkey(info.get("inputMint"), f"routePlan[{index}].inputMint")
            step_output = _canonical_pubkey(info.get("outputMint"), f"routePlan[{index}].outputMint")
            percent = _finite_number(item.get("percent"), f"routePlan[{index}].percent")
            bps = item.get("bps")
            if percent < 0 or percent > 100 or isinstance(bps, bool) or not isinstance(bps, int) or not 0 <= bps <= 10_000:
                raise JupiterQuoteError(f"routePlan step {index} has invalid split percentages")
            steps.append(JupiterRouteStep(
                amm_key=amm_key,
                label=label,
                input_mint=step_input,
                output_mint=step_output,
                input_amount_raw=_raw_amount(info.get("inAmount"), f"routePlan[{index}].inAmount"),
                output_amount_raw=_raw_amount(info.get("outAmount"), f"routePlan[{index}].outAmount"),
                percent=percent,
                bps=bps,
            ))

        def fee_value(name: str) -> int:
            value = body.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise JupiterQuoteError(f"{name} must be a non-negative integer")
            return value

        gasless = body.get("gasless", False)
        if not isinstance(gasless, bool):
            raise JupiterQuoteError("gasless must be a boolean")
        slippage = body.get("slippageBps")
        if slippage is not None and (
            isinstance(slippage, bool) or not isinstance(slippage, int) or not 0 <= slippage <= 10_000
        ):
            raise JupiterQuoteError("slippageBps is invalid")
        fee_mint = body.get("feeMint")
        if fee_mint is not None:
            fee_mint = _canonical_pubkey(fee_mint, "feeMint")
        mode = body.get("mode")
        if mode is not None and not isinstance(mode, str):
            raise JupiterQuoteError("mode is malformed")
        quote_id = body.get("quoteId")
        if quote_id is not None and not isinstance(quote_id, str):
            raise JupiterQuoteError("quoteId is malformed")
        expire_at = body.get("expireAt")
        if expire_at is not None and not isinstance(expire_at, str):
            raise JupiterQuoteError("expireAt is malformed")

        observed_at = self._clock_ms()
        if isinstance(observed_at, bool) or not isinstance(observed_at, int) or observed_at <= 0:
            raise JupiterQuoteError("local quote observation clock is invalid")

        return JupiterQuote(
            provider_id="jupiter-swap-v2",
            request_id=request_id,
            quote_id=quote_id,
            observed_at_epoch_ms=observed_at,
            input_mint=input_mint,
            output_mint=output_mint,
            input_amount_raw=input_amount,
            output_amount_raw=output_amount,
            minimum_output_amount_raw=minimum_output,
            price_impact_pct=impact,
            fee_bps=fee_bps,
            fee_mint=fee_mint,
            router=router,
            mode=mode,
            slippage_bps=slippage,
            signature_fee_lamports=fee_value("signatureFeeLamports"),
            prioritization_fee_lamports=fee_value("prioritizationFeeLamports"),
            rent_fee_lamports=fee_value("rentFeeLamports"),
            gasless=gasless,
            expire_at=expire_at,
            route_plan=tuple(steps),
        )
