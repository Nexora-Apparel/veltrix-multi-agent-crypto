"""Read-only PumpPortal adapter for Solana token-creation discovery.

This adapter subscribes only to the free new-token feed. It does not request
trade streams and has no wallet, signing, swap, or transaction capabilities.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Iterator
from typing import Any, Callable

from .candidates import CandidateRejected, SolanaCandidate, normalize_candidate


class PumpPortalError(RuntimeError):
    """Raised when the PumpPortal launch stream cannot be consumed safely."""


class PumpPortalDiscovery:
    name = "pumpportal-new-token"
    endpoint = "wss://pumpportal.fun/api/data"

    def __init__(
        self,
        *,
        websocket_factory: Callable[..., Any] | None = None,
        timeout_seconds: float = 30.0,
        endpoint: str | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self.endpoint = endpoint or self.endpoint
        self._websocket_factory = websocket_factory
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def _connect(self) -> Any:
        factory = self._websocket_factory
        if factory is None:
            try:
                import websocket
            except ImportError as exc:
                raise PumpPortalError(
                    "websocket-client is required for PumpPortal discovery"
                ) from exc
            factory = websocket.create_connection
        try:
            return factory(self.endpoint, timeout=self.timeout_seconds)
        except Exception as exc:
            raise PumpPortalError(f"PumpPortal connection failed: {exc}") from exc

    @staticmethod
    def _decode(raw: Any) -> dict[str, Any] | None:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if not isinstance(raw, str):
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _candidate(payload: dict[str, Any], received_at_ms: int) -> SolanaCandidate | None:
        if payload.get("txType") != "create":
            return None
        try:
            return normalize_candidate(
                {"chain": "solana", "mint": payload.get("mint")},
                source=PumpPortalDiscovery.name,
                observed_at_ms=received_at_ms,
            )
        except CandidateRejected:
            return None

    def stream_new_tokens(
        self,
        *,
        stop_event: threading.Event | None = None,
        max_messages: int | None = None,
    ) -> Iterator[SolanaCandidate]:
        """Yield normalized token creations from one socket connection."""
        if max_messages is not None and (
            isinstance(max_messages, bool) or not isinstance(max_messages, int) or max_messages < 0
        ):
            raise ValueError("max_messages must be a non-negative integer")
        if max_messages == 0:
            return

        ws = self._connect()
        received = 0
        try:
            # The only subscription used by this product is free new-token discovery.
            ws.send(json.dumps({"method": "subscribeNewToken"}))
            while stop_event is None or not stop_event.is_set():
                if max_messages is not None and received >= max_messages:
                    break
                try:
                    raw = ws.recv()
                except Exception as exc:
                    if exc.__class__.__name__ in {"WebSocketTimeoutException", "TimeoutError"}:
                        continue
                    raise PumpPortalError(f"PumpPortal stream failed: {exc}") from exc
                if raw in (None, ""):
                    continue
                received += 1
                payload = self._decode(raw)
                if payload is None:
                    continue
                candidate = self._candidate(payload, self.clock_ms())
                if candidate is not None:
                    yield candidate
        finally:
            try:
                ws.close()
            except Exception:
                pass
