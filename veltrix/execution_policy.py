"""Fail-closed execution policy and one-use operator authorization primitives.

This module defines policy and authorization data only. It does not load wallet
secrets, sign transactions, or submit orders. Live execution remains unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import json
import secrets
import time
from typing import Mapping, Protocol


class ExecutionMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class PolicyError(ValueError):
    """Raised when execution policy is missing, unsafe, or contradictory."""


class AuthorizationError(ValueError):
    """Raised when an operator authorization is invalid or cannot be consumed."""


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Immutable, deterministic policy snapshot.

    Amount limits are integer base units. The current release only permits
    PAPER mode; LIVE is represented so authorization claims can be precisely
    bound without enabling execution.
    """

    mode: ExecutionMode = ExecutionMode.PAPER
    chain: str = "solana-mainnet"
    max_trade_base_units: int = 0
    max_token_exposure_base_units: int = 0
    max_portfolio_exposure_base_units: int = 0
    max_daily_loss_base_units: int = 0
    max_slippage_bps: int = 0
    max_priority_fee_base_units: int = 0
    max_market_data_age_ms: int = 5_000
    allowed_program_ids: tuple[str, ...] = ()
    policy_version: int = 1

    def __post_init__(self) -> None:
        if not self.chain or self.chain.strip() != self.chain:
            raise PolicyError("chain must be a non-empty canonical identifier")
        for name in (
            "max_trade_base_units",
            "max_token_exposure_base_units",
            "max_portfolio_exposure_base_units",
            "max_daily_loss_base_units",
            "max_slippage_bps",
            "max_priority_fee_base_units",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PolicyError(f"{name} must be a non-negative integer")
        if self.max_market_data_age_ms <= 0:
            raise PolicyError("max_market_data_age_ms must be positive")
        if not 0 <= self.max_slippage_bps <= 10_000:
            raise PolicyError("max_slippage_bps must be between 0 and 10000")
        if self.policy_version < 1:
            raise PolicyError("policy_version must be positive")
        if len(set(self.allowed_program_ids)) != len(self.allowed_program_ids):
            raise PolicyError("allowed_program_ids must not contain duplicates")
        if any(not item or item.strip() != item for item in self.allowed_program_ids):
            raise PolicyError("program IDs must be non-empty canonical strings")

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> "ExecutionPolicy":
        """Build the only currently supported runtime policy: PAPER.

        LIVE_TRADING=true is explicitly rejected for backwards compatibility.
        Unknown truthy values are rejected rather than interpreted as false.
        """
        raw = env.get("LIVE_TRADING", "false").strip().lower()
        if raw not in {"false", "0", "no", "off", ""}:
            if raw in {"true", "1", "yes", "on"}:
                raise PolicyError("live execution is disabled in this release")
            raise PolicyError("LIVE_TRADING must be an explicit false value")
        return cls(
            mode=ExecutionMode.PAPER,
            chain=env.get("EXECUTION_CHAIN", "solana-mainnet"),
            max_market_data_age_ms=_strict_int(
                env.get("EXECUTION_MAX_MARKET_DATA_AGE_MS", "5000"),
                "EXECUTION_MAX_MARKET_DATA_AGE_MS",
                minimum=1,
            ),
        )

    def canonical_payload(self) -> bytes:
        data = {
            "mode": self.mode.value,
            "chain": self.chain,
            "max_trade_base_units": self.max_trade_base_units,
            "max_token_exposure_base_units": self.max_token_exposure_base_units,
            "max_portfolio_exposure_base_units": self.max_portfolio_exposure_base_units,
            "max_daily_loss_base_units": self.max_daily_loss_base_units,
            "max_slippage_bps": self.max_slippage_bps,
            "max_priority_fee_base_units": self.max_priority_fee_base_units,
            "max_market_data_age_ms": self.max_market_data_age_ms,
            "allowed_program_ids": sorted(self.allowed_program_ids),
            "policy_version": self.policy_version,
        }
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()

    @property
    def policy_hash(self) -> str:
        return hashlib.sha256(self.canonical_payload()).hexdigest()


def _strict_int(raw: str, name: str, *, minimum: int = 0) -> int:
    try:
        value = int(raw, 10)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"{name} must be an integer") from exc
    if str(value) != str(raw).strip() or value < minimum:
        raise PolicyError(f"{name} must be an integer >= {minimum}")
    return value


class AuthorizationConsumer(Protocol):
    def consume(self, authorization_id: str, expires_at: int) -> bool:
        """Atomically record an authorization ID once; false means replay."""


@dataclass(frozen=True, slots=True)
class OperatorAuthorization:
    """Signed, short-lived authorization bound to one exact execution intent."""

    authorization_id: str
    wallet_public_key: str
    chain: str
    policy_hash: str
    intent_id: str
    issued_at: int
    expires_at: int
    signature: str

    @staticmethod
    def _payload(
        authorization_id: str,
        wallet_public_key: str,
        chain: str,
        policy_hash: str,
        intent_id: str,
        issued_at: int,
        expires_at: int,
    ) -> bytes:
        values = {
            "authorization_id": authorization_id,
            "wallet_public_key": wallet_public_key,
            "chain": chain,
            "policy_hash": policy_hash,
            "intent_id": intent_id,
            "issued_at": issued_at,
            "expires_at": expires_at,
        }
        return json.dumps(values, sort_keys=True, separators=(",", ":")).encode()

    @classmethod
    def issue(
        cls,
        *,
        signing_key: bytes,
        wallet_public_key: str,
        policy: ExecutionPolicy,
        intent_id: str,
        ttl_seconds: int = 60,
        now: int | None = None,
    ) -> "OperatorAuthorization":
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise AuthorizationError("authorization signing key must be at least 32 bytes")
        if policy.mode is not ExecutionMode.LIVE:
            raise AuthorizationError("operator authorization is only meaningful for a live policy")
        if not wallet_public_key or not intent_id:
            raise AuthorizationError("wallet public key and intent ID are required")
        if isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= 300:
            raise AuthorizationError("authorization TTL must be between 1 and 300 seconds")
        issued = int(time.time()) if now is None else int(now)
        expires = issued + ttl_seconds
        auth_id = secrets.token_hex(16)
        payload = cls._payload(
            auth_id, wallet_public_key, policy.chain, policy.policy_hash,
            intent_id, issued, expires,
        )
        signature = hmac.new(signing_key, payload, hashlib.sha256).hexdigest()
        return cls(auth_id, wallet_public_key, policy.chain, policy.policy_hash,
                   intent_id, issued, expires, signature)

    def verify_and_consume(
        self,
        *,
        signing_key: bytes,
        policy: ExecutionPolicy,
        wallet_public_key: str,
        intent_id: str,
        consumer: AuthorizationConsumer,
        now: int | None = None,
    ) -> None:
        current = int(time.time()) if now is None else int(now)
        if policy.mode is not ExecutionMode.LIVE:
            raise AuthorizationError("policy is not live")
        if current < self.issued_at or current >= self.expires_at:
            raise AuthorizationError("authorization is not currently valid")
        if self.expires_at - self.issued_at > 300:
            raise AuthorizationError("authorization lifetime exceeds maximum")
        if (
            self.wallet_public_key != wallet_public_key
            or self.chain != policy.chain
            or self.policy_hash != policy.policy_hash
            or self.intent_id != intent_id
        ):
            raise AuthorizationError("authorization scope does not match request")
        payload = self._payload(
            self.authorization_id, self.wallet_public_key, self.chain,
            self.policy_hash, self.intent_id, self.issued_at, self.expires_at,
        )
        expected = hmac.new(signing_key, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.signature):
            raise AuthorizationError("authorization signature is invalid")
        if not consumer.consume(self.authorization_id, self.expires_at):
            raise AuthorizationError("authorization has already been consumed")
