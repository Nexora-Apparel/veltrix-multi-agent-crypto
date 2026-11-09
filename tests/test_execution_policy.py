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


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "live"},
        {"max_market_data_age_ms": True},
        {"max_market_data_age_ms": 1.5},
        {"policy_version": True},
        {"policy_version": 1.5},
        {"allowed_program_ids": ["program"]},
        {"allowed_program_ids": (None,)},
        {"chain": 123},
    ],
)
def test_policy_rejects_wrong_types(kwargs):
    with pytest.raises(PolicyError):
        ExecutionPolicy(**kwargs)


@pytest.mark.parametrize(
    "env",
    [
        {"LIVE_TRADING": True},
        {"EXECUTION_CHAIN": 123},
        {"EXECUTION_MAX_MARKET_DATA_AGE_MS": "1.5"},
        {"EXECUTION_MAX_MARKET_DATA_AGE_MS": "0"},
    ],
)
def test_environment_rejects_malformed_values(env):
    with pytest.raises(PolicyError):
        ExecutionPolicy.from_environment(env)


@pytest.mark.parametrize("ttl", [True, 1.5, "30", 0, 301])
def test_authorization_rejects_non_integer_or_out_of_range_ttl(ttl):
    with pytest.raises(AuthorizationError):
        OperatorAuthorization.issue(
            signing_key=b"t" * 32,
            wallet_public_key="wallet",
            policy=live_policy(),
            intent_id="intent",
            ttl_seconds=ttl,
            now=6_000,
        )


def test_verification_rejects_invalid_key_and_malformed_clock():
    policy = live_policy()
    auth = OperatorAuthorization.issue(
        signing_key=b"v" * 32,
        wallet_public_key="wallet",
        policy=policy,
        intent_id="intent",
        now=7_000,
    )
    with pytest.raises(AuthorizationError, match="signing key"):
        auth.verify_and_consume(
            signing_key=b"short",
            policy=policy,
            wallet_public_key="wallet",
            intent_id="intent",
            consumer=MemoryConsumer(),
            now=7_001,
        )
    with pytest.raises(AuthorizationError, match="timestamp"):
        auth.verify_and_consume(
            signing_key=b"v" * 32,
            policy=policy,
            wallet_public_key="wallet",
            intent_id="intent",
            consumer=MemoryConsumer(),
            now=True,
        )


def test_tampered_expiry_is_rejected_before_consumption():
    from dataclasses import replace

    policy = live_policy()
    key = b"z" * 32
    auth = OperatorAuthorization.issue(
        signing_key=key,
        wallet_public_key="wallet",
        policy=policy,
        intent_id="intent",
        now=8_000,
    )
    tampered = replace(auth, expires_at=auth.expires_at + 1)
    consumer = MemoryConsumer()
    with pytest.raises(AuthorizationError, match="signature"):
        tampered.verify_and_consume(
            signing_key=key,
            policy=policy,
            wallet_public_key="wallet",
            intent_id="intent",
            consumer=consumer,
            now=8_001,
        )
    assert not consumer.used


def test_consumer_failure_fails_closed():
    class BrokenConsumer:
        def consume(self, authorization_id, expires_at):
            raise OSError("storage unavailable")

    policy = live_policy()
    auth = OperatorAuthorization.issue(
        signing_key=b"b" * 32,
        wallet_public_key="wallet",
        policy=policy,
        intent_id="intent",
        now=9_000,
    )
    with pytest.raises(AuthorizationError, match="atomically"):
        auth.verify_and_consume(
            signing_key=b"b" * 32,
            policy=policy,
            wallet_public_key="wallet",
            intent_id="intent",
            consumer=BrokenConsumer(),
            now=9_001,
        )
