from __future__ import annotations

import re

import pytest

from scripts.run_solana_pipeline import build_idempotency_key


def test_github_dispatch_has_stable_key_for_same_run_attempt() -> None:
    env = {"GITHUB_RUN_ID": "44096937097", "GITHUB_RUN_ATTEMPT": "1"}
    assert build_idempotency_key(env) == build_idempotency_key(env)
    assert build_idempotency_key(env) == "solana-intelligence-v2-44096937097-attempt-1"


def test_new_github_dispatch_cannot_reuse_previous_failed_job_key() -> None:
    first = build_idempotency_key({"GITHUB_RUN_ID": "44096937097", "GITHUB_RUN_ATTEMPT": "1"})
    second = build_idempotency_key({"GITHUB_RUN_ID": "44097000000", "GITHUB_RUN_ATTEMPT": "1"})
    assert first != second


def test_actions_rerun_gets_a_new_attempt_key() -> None:
    first = build_idempotency_key({"GITHUB_RUN_ID": "44096937097", "GITHUB_RUN_ATTEMPT": "1"})
    rerun = build_idempotency_key({"GITHUB_RUN_ID": "44096937097", "GITHUB_RUN_ATTEMPT": "2"})
    assert first != rerun


def test_local_invocations_get_distinct_keys() -> None:
    first = build_idempotency_key({})
    second = build_idempotency_key({})
    assert first != second
    assert re.fullmatch(r"solana-intelligence-v2-manual-[0-9a-f-]{36}", first)


@pytest.mark.parametrize(
    "env",
    [
        {"GITHUB_RUN_ID": "not-a-number", "GITHUB_RUN_ATTEMPT": "1"},
        {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "zero"},
    ],
)
def test_invalid_github_run_metadata_fails_closed(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        build_idempotency_key(env)



def test_dashboard_request_id_is_the_durable_idempotency_key_across_duplicate_dispatches() -> None:
    request_id = "7e3f9d2a-1a4b-4c2d-8e6f-123456789abc"
    first = build_idempotency_key({
        "VELTRIX_REQUEST_ID": request_id,
        "GITHUB_RUN_ID": "44096937097",
        "GITHUB_RUN_ATTEMPT": "1",
    })
    duplicate = build_idempotency_key({
        "VELTRIX_REQUEST_ID": request_id,
        "GITHUB_RUN_ID": "44097000000",
        "GITHUB_RUN_ATTEMPT": "1",
    })
    rerun = build_idempotency_key({
        "VELTRIX_REQUEST_ID": request_id,
        "GITHUB_RUN_ID": "44096937097",
        "GITHUB_RUN_ATTEMPT": "2",
    })
    assert first == duplicate == rerun
    assert first == "solana-intelligence-v2-request-" + request_id


@pytest.mark.parametrize("request_id", ["short", "not-a-uuid!!", " "])
def test_invalid_dashboard_request_id_fails_closed(request_id: str) -> None:
    with pytest.raises(ValueError, match="VELTRIX_REQUEST_ID"):
        build_idempotency_key({"VELTRIX_REQUEST_ID": request_id})
