"""Controlled, read-only Solana transaction simulation for sell-route research.

This provider asks Jupiter to assemble an unsigned sell transaction for a
configured public test-wallet address, then calls Solana simulateTransaction.
It never signs, submits, broadcasts, or executes the transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import math
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .base import ProviderError
from ..solana_meme.candidates import canonical_mint
from ..solana_meme.sellability import WRAPPED_SOL_MINT


@dataclass(frozen=True)
class SolanaSellSimulationObservation:
    mint: str
    taker: str
    request_id: str
    observed_at_ms: int
    simulated_slot: int
    units_consumed: int | None
    logs: tuple[str, ...]
    state: str
    error: str | None
    signature_verification: bool = False
    transaction_submitted: bool = False


class JupiterSolanaSellSimulationProvider:
    """Build an unsigned Jupiter order and simulate it against a Solana RPC."""

    provider_id = "jupiter-solana-simulate-transaction"
    order_endpoint = "https://api.jup.ag/swap/v2/order"

    def __init__(
        self,
        rpc_url: str,
        taker_public_key: str,
        *,
        api_key: str = "",
        session: Any | None = None,
        timeout_seconds: float = 15.0,
        minimum_request_interval_seconds: float = 2.1,
        clock_ms: Callable[[], int] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        parsed = urlparse(rpc_url) if isinstance(rpc_url, str) else None
        if parsed is None or parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("simulation RPC URL must be an HTTPS URL")
        self.taker = canonical_mint(taker_public_key)
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if (isinstance(minimum_request_interval_seconds, bool)
                or not math.isfinite(minimum_request_interval_seconds)
                or minimum_request_interval_seconds < 2.0):
            raise ValueError("Jupiter request interval must be at least 2 seconds")
        self.rpc_url = rpc_url
        self.api_key = api_key.strip() if isinstance(api_key, str) else ""
        self.session = session or requests
        self.timeout_seconds = timeout_seconds
        self.minimum_request_interval_seconds = minimum_request_interval_seconds
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._last_order_started: float | None = None

    def _throttle_order(self) -> None:
        with self._lock:
            now = self.monotonic()
            if self._last_order_started is not None:
                delay = self.minimum_request_interval_seconds - (now - self._last_order_started)
                if delay > 0:
                    self.sleep(delay)
                    now = self.monotonic()
            self._last_order_started = now

    def simulate_sell(
        self, mint: str, input_amount_raw: int,
    ) -> SolanaSellSimulationObservation:
        canonical = canonical_mint(mint)
        if (isinstance(input_amount_raw, bool) or not isinstance(input_amount_raw, int)
                or input_amount_raw <= 0):
            raise ValueError("input_amount_raw must be a positive integer")

        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        params = {
            "inputMint": canonical,
            "outputMint": WRAPPED_SOL_MINT,
            "amount": str(input_amount_raw),
            "taker": self.taker,
            "swapMode": "ExactIn",
        }
        self._throttle_order()
        try:
            order_response = self.session.get(
                self.order_endpoint, params=params, headers=headers,
                timeout=self.timeout_seconds,
            )
            order_response.raise_for_status()
            order = order_response.json()
        except Exception as exc:
            raise ProviderError(
                f"Jupiter simulation order failed: {type(exc).__name__}"
            ) from exc

        if not isinstance(order, dict) or order.get("error") or order.get("errorCode") is not None:
            raise ProviderError("Jupiter returned an error while assembling simulation order")
        if order.get("inputMint") != canonical or order.get("outputMint") != WRAPPED_SOL_MINT:
            raise ProviderError("Jupiter simulation order token pair mismatch")
        try:
            amount = int(order.get("inAmount"))
        except (TypeError, ValueError) as exc:
            raise ProviderError("Jupiter simulation order input amount is malformed") from exc
        if amount != input_amount_raw:
            raise ProviderError("Jupiter simulation order amount mismatch")
        request_id = order.get("requestId")
        transaction = order.get("transaction")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ProviderError("Jupiter simulation order request ID is missing")
        if not isinstance(transaction, str) or not transaction:
            raise ProviderError("Jupiter did not return an unsigned transaction")
        try:
            raw_transaction = base64.b64decode(transaction, validate=True)
        except (ValueError, TypeError) as exc:
            raise ProviderError("Jupiter transaction is not valid base64") from exc
        if not raw_transaction:
            raise ProviderError("Jupiter returned an empty transaction")

        rpc_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "simulateTransaction",
            "params": [
                transaction,
                {
                    "encoding": "base64",
                    "sigVerify": False,
                    "replaceRecentBlockhash": True,
                    "commitment": "processed",
                    "innerInstructions": True,
                },
            ],
        }
        try:
            rpc_response = self.session.post(
                self.rpc_url, json=rpc_payload,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout_seconds,
            )
            rpc_response.raise_for_status()
            rpc_body = rpc_response.json()
        except Exception as exc:
            raise ProviderError(
                f"Solana transaction simulation failed: {type(exc).__name__}"
            ) from exc
        if not isinstance(rpc_body, dict) or rpc_body.get("error") or not isinstance(rpc_body.get("result"), dict):
            raise ProviderError("Solana RPC returned an invalid simulation response")
        result = rpc_body["result"]
        context = result.get("context")
        value = result.get("value")
        if not isinstance(context, dict) or not isinstance(value, dict):
            raise ProviderError("Solana RPC simulation context/value is missing")
        slot = context.get("slot")
        if isinstance(slot, bool) or not isinstance(slot, int) or slot <= 0:
            raise ProviderError("Solana RPC simulation slot is invalid")
        logs_value = value.get("logs")
        if logs_value is not None and (
            not isinstance(logs_value, list) or any(not isinstance(item, str) for item in logs_value)
        ):
            raise ProviderError("Solana RPC simulation logs are malformed")
        units = value.get("unitsConsumed")
        if units is not None and (
            isinstance(units, bool) or not isinstance(units, int) or units < 0
        ):
            raise ProviderError("Solana RPC simulation unitsConsumed is invalid")
        error = value.get("err")
        now = self.clock_ms()
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ProviderError("local simulation timestamp is invalid")
        return SolanaSellSimulationObservation(
            mint=canonical,
            taker=self.taker,
            request_id=request_id,
            observed_at_ms=now,
            simulated_slot=slot,
            units_consumed=units,
            logs=tuple(logs_value or ()),
            state="SIMULATION_SUCCEEDED" if error is None else "SIMULATION_FAILED",
            error=None if error is None else str(error)[:500],
        )
