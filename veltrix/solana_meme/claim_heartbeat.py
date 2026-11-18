"""Fenced heartbeats for long-running per-row worker claims."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable


class RowClaimHeartbeat:
    """Renew owned row claims without changing their fencing identity."""

    def __init__(
        self,
        mints: Iterable[str],
        renew: Callable[[tuple[str, ...]], bool] | None,
        *,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self._pending = set(mints)
        self._renew = renew
        self._interval = interval_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "RowClaimHeartbeat":
        if self._renew is not None and self._pending:
            self._thread = threading.Thread(
                target=self._run,
                name="veltrix-row-claim-heartbeat",
                daemon=True,
            )
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            with self._lock:
                pending = tuple(sorted(self._pending))
            if not pending:
                return
            try:
                if self._renew is None or not self._renew(pending):
                    self._lost.set()
                    return
            except Exception:
                self._lost.set()
                return

    def complete(self, mint: str) -> None:
        with self._lock:
            self._pending.discard(mint)

    def assert_active(self) -> None:
        if self._lost.is_set():
            from ..orchestration import LeaseBusy
            raise LeaseBusy("row-claim heartbeat failed or ownership was lost")

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
