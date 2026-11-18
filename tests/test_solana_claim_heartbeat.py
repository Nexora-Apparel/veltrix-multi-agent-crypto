import threading
import time

import pytest

from veltrix.orchestration import LeaseBusy
from veltrix.solana_meme.claim_heartbeat import RowClaimHeartbeat


def test_terminal_write_and_heartbeat_retirement_are_atomic():
    renewals = []
    started = threading.Event()

    def renew(mints):
        renewals.append(mints)
        return True

    with RowClaimHeartbeat(("mint-a",), renew, interval_seconds=0.01) as heartbeat:
        def terminal_write():
            started.set()
            time.sleep(0.04)
            return "committed"

        assert heartbeat.complete_after("mint-a", terminal_write) == "committed"
        assert started.is_set()
        time.sleep(0.03)
        heartbeat.assert_active()

    # The heartbeat ticked while the terminal write held the lock. It must
    # observe an empty pending set after commit, not renew a completed row.
    assert renewals == []


def test_failed_renewal_fails_closed():
    with RowClaimHeartbeat(
        ("mint-a",),
        lambda _mints: False,
        interval_seconds=0.01,
    ) as heartbeat:
        time.sleep(0.03)
        with pytest.raises(LeaseBusy, match="ownership was lost"):
            heartbeat.assert_active()


def test_failed_terminal_write_keeps_claim_in_heartbeat_set():
    renewals = []
    with RowClaimHeartbeat(
        ("mint-a",),
        lambda mints: renewals.append(mints) or True,
        interval_seconds=0.01,
    ) as heartbeat:
        def failed_write():
            raise RuntimeError("database write failed")

        with pytest.raises(RuntimeError, match="database write failed"):
            heartbeat.complete_after("mint-a", failed_write)
        time.sleep(0.03)
        heartbeat.assert_active()

    assert renewals
    assert renewals[0] == ("mint-a",)
