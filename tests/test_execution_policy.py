import pytest

from veltrix.execution_policy import (
    AuthorizationError,
    ExecutionMode,
    ExecutionPolicy,
    OperatorAuthorization,
    PolicyError,
)


class MemoryConsumer:
    def __init__(self):
        self.used = set()

    def consume(self, authorization_id, expires_at):
        if authorization_id in self.used:
            return False
        self.used.add(authorization_id)
        return True


def live_policy():
    # LIVE is only used to test authorization semantics. Runtime environment
    # loading deliberately cannot enable this mode in the current release.
    return ExecutionPolicy(mode=ExecutionMode.LIVE)


def test_environment_defaults_to_paper():
    policy = ExecutionPolicy.from_environment({})
    assert policy.mode is ExecutionMode.PAPER


@pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
def test_live_environment_flag_is_rejected(value):
    with pytest.raises(PolicyError, match="disabled"):
        ExecutionPolicy.from_environment({"LIVE_TRADING": value})


@pytest.mark.parametrize("value", ["maybe", "TRUE ", "enabled"])
def test_unknown_live_environment_value_is_rejected(value):
    with pytest.raises(PolicyError):
        ExecutionPolicy.from_environment({"LIVE_TRADING": value})


def test_policy_hash_is_stable_and_sensitive_to_policy_changes():
    first = ExecutionPolicy(max_trade_base_units=100)
    second = ExecutionPolicy(max_trade_base_units=100)
    changed = ExecutionPolicy(max_trade_base_units=101)
    assert first.policy_hash == second.policy_hash
    assert first.policy_hash != changed.policy_hash


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_trade_base_units": -1},
        {"max_slippage_bps": 10_001},
        {"max_market_data_age_ms": 0},
        {"policy_version": 0},
        {"allowed_program_ids": ("program", "program")},
    ],
)
def test_invalid_policy_is_rejected(kwargs):
    with pytest.raises(PolicyError):
        ExecutionPolicy(**kwargs)


def test_authorization_is_bound_and_consumed_once():
    policy = live_policy()
    key = b"k" * 32
    auth = OperatorAuthorization.issue(
        signing_key=key,
        wallet_public_key="wallet-public-key",
        policy=policy,
        intent_id="intent-001",
        ttl_seconds=30,
        now=1_000,
    )
    consumer = MemoryConsumer()
    auth.verify_and_consume(
        signing_key=key,
        policy=policy,
        wallet_public_key="wallet-public-key",
        intent_id="intent-001",
        consumer=consumer,
        now=1_001,
    )
    with pytest.raises(AuthorizationError, match="consumed"):
        auth.verify_and_consume(
            signing_key=key,
            policy=policy,
            wallet_public_key="wallet-public-key",
            intent_id="intent-001",
            consumer=consumer,
            now=1_002,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"wallet_public_key": "different-wallet"},
        {"intent_id": "different-intent"},
        {"policy": ExecutionPolicy(mode=ExecutionMode.LIVE, chain="different-chain")},
    ],
)
def test_authorization_scope_mismatch_is_rejected(overrides):
    policy = live_policy()
    key = b"s" * 32
    auth = OperatorAuthorization.issue(
        signing_key=key,
        wallet_public_key="wallet-public-key",
        policy=policy,
        intent_id="intent-002",
        now=2_000,
    )
    args = {
        "signing_key": key,
        "policy": policy,
        "wallet_public_key": "wallet-public-key",
        "intent_id": "intent-002",
        "consumer": MemoryConsumer(),
        "now": 2_001,
    }
    args.update(overrides)
    with pytest.raises(AuthorizationError, match="scope"):
        auth.verify_and_consume(**args)


def test_expired_authorization_is_rejected_without_consuming():
    policy = live_policy()
    key = b"e" * 32
    auth = OperatorAuthorization.issue(
        signing_key=key,
        wallet_public_key="wallet",
        policy=policy,
        intent_id="intent",
        ttl_seconds=1,
        now=3_000,
    )
    consumer = MemoryConsumer()
    with pytest.raises(AuthorizationError, match="valid"):
        auth.verify_and_consume(
            signing_key=key,
            policy=policy,
            wallet_public_key="wallet",
            intent_id="intent",
            consumer=consumer,
            now=3_001,
        )
    assert not consumer.used


def test_authorization_rejects_short_signing_key_and_long_ttl():
    policy = live_policy()
    with pytest.raises(AuthorizationError):
        OperatorAuthorization.issue(
            signing_key=b"short",
            wallet_public_key="wallet",
            policy=policy,
            intent_id="intent",
            now=4_000,
        )
    with pytest.raises(AuthorizationError):
        OperatorAuthorization.issue(
            signing_key=b"x" * 32,
            wallet_public_key="wallet",
            policy=policy,
            intent_id="intent",
            ttl_seconds=301,
            now=4_000,
        )


def test_paper_policy_cannot_issue_or_consume_live_authorization():
    paper = ExecutionPolicy()
    with pytest.raises(AuthorizationError, match="only meaningful"):
        OperatorAuthorization.issue(
            signing_key=b"p" * 32,
            wallet_public_key="wallet",
            policy=paper,
            intent_id="intent",
            now=5_000,
        )
