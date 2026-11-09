"""Read-only PumpPortal new-token stream adapter.

Only subscribes to new-token creation events. It never subscribes to paid
trade streams and contains no transaction or wallet-signing functionality.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any, Callable

from .candidates import CandidateRejected, SolanaCandidate, normalize_candidate


class PumpPortalError(RuntimeError):
    """Raised when the launch stream cannot be consumed safely."""


class PumpPortalDiscovery:
    name = "pumpportal-new-token"
    endpoint = "wss://pumpportal.fun/api/data"

    def __init__(self, *, websocket_factory: Callable[..., Any] | None = None,
                 timeout_seconds: float = 30.0, endpoint: str | None = None) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self.endpoint = endpoint or self.endpoint
        self._websocket_factory = websocket_factory

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
        # Fail closed: only explicitly identified token-creation events qualify.
        if payload.get("txType") != "create":
            return None
        mint = payload.get("mint")
        if not isinstance(mint, str):
            return None
        try:
            return normalize_candidate(
                {"chain": "solana", "mint": mint},
                source=PumpPortalDiscovery.name,
                observed_at_ms=received_at_ms,
            )
        except CandidateRejected:
            return None

    def stream_new_tokens(self, *, stop_event: threading.Event | None = None,
                          max_messages: int | None = None) -> Iterator[SolanaCandidate]:
        """Yield normalized launches from one socket; max_messages supports bounded probes."""
        if max_messages is not None and max_messages < 0:
            raise ValueError("max_messages cannot be negative")
        if max_messages == 0:
            return
        ws = self._connect()
        received = 0
        try:
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
                candidate = self._candidate(payload, int(time.time() * 1000))
                if candidate is not None:
                    yield candidate
        finally:
            try:
                ws.close()
            except Exception:
                pass
