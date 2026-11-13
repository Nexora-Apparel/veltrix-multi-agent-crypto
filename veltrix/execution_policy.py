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
import re
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
        if not isinstance(self.mode, ExecutionMode):
            raise PolicyError("mode must be an ExecutionMode")
        if not isinstance(self.chain, str) or not self.chain or self.chain.strip() != self.chain:
            raise PolicyError("chain must be a non-empty canonical identifier")
        for name in (
            "max_trade_base_units",
            "max_token_exposure_base_units",
            "max_portfolio_exposure_base_units",
            "max_daily_loss_base_units",
            "max_slippage_bps",
            "max_priority_fee_base_units",
            "max_market_data_age_ms",
            "policy_version",
        ):
            value = getattr(self, name)
            minimum = 1 if name in {"max_market_data_age_ms", "policy_version"} else 0
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise PolicyError(f"{name} must be an integer >= {minimum}")
        if self.max_slippage_bps > 10_000:
            raise PolicyError("max_slippage_bps must be between 0 and 10000")
        if not isinstance(self.allowed_program_ids, tuple):
            raise PolicyError("allowed_program_ids must be an immutable tuple")
        if any(
            not isinstance(item, str) or not item or item.strip() != item
            for item in self.allowed_program_ids
        ):
            raise PolicyError("program IDs must be non-empty canonical strings")
        if len(set(self.allowed_program_ids)) != len(self.allowed_program_ids):
            raise PolicyError("allowed_program_ids must not contain duplicates")

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> "ExecutionPolicy":
        """Build the only currently supported runtime policy: PAPER.

        LIVE_TRADING=true is explicitly rejected for backwards compatibility.
        Unknown values are rejected rather than interpreted as false.
        """
        raw_flag = env.get("LIVE_TRADING", "false")
        if not isinstance(raw_flag, str):
            raise PolicyError("LIVE_TRADING must be a string")
        raw = raw_flag.strip().lower()
        if raw not in {"false", "0", "no", "off", ""}:
            if raw in {"true", "1", "yes", "on"}:
                raise PolicyError("live execution is disabled in this release")
            raise PolicyError("LIVE_TRADING must be an explicit false value")
        chain = env.get("EXECUTION_CHAIN", "solana-mainnet")
        if not isinstance(chain, str):
            raise PolicyError("EXECUTION_CHAIN must be a string")
        return cls(
            mode=ExecutionMode.PAPER,
            chain=chain,
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
    if isinstance(raw, bool):
        raise PolicyError(f"{name} must be an integer")
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
    intent_hash: str
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
        intent_hash: str,
        issued_at: int,
        expires_at: int,
    ) -> bytes:
        values = {
            "authorization_id": authorization_id,
            "wallet_public_key": wallet_public_key,
            "chain": chain,
            "policy_hash": policy_hash,
            "intent_id": intent_id,
            "intent_hash": intent_hash,
            "issued_at": issued_at,
            "expires_at": expires_at,
        }
        return json.dumps(values, sort_keys=True, separators=(",", ":")).encode()

    @staticmethod
    def _validate_signing_key(signing_key: bytes) -> None:
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise AuthorizationError("authorization signing key must be at least 32 bytes")

    @staticmethod
    def _validate_clock(value: int, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AuthorizationError(f"{name} must be a non-negative integer timestamp")
        return value

    @classmethod
    def issue(
        cls,
        *,
        signing_key: bytes,
        wallet_public_key: str,
        policy: ExecutionPolicy,
        intent_id: str,
        intent_hash: str,
        ttl_seconds: int = 60,
        now: int | None = None,
    ) -> "OperatorAuthorization":
        cls._validate_signing_key(signing_key)
        if not isinstance(policy, ExecutionPolicy) or policy.mode is not ExecutionMode.LIVE:
            raise AuthorizationError("operator authorization is only meaningful for a live policy")
        if (
            not isinstance(wallet_public_key, str)
            or not wallet_public_key
            or wallet_public_key.strip() != wallet_public_key
            or not isinstance(intent_id, str)
            or not intent_id
            or intent_id.strip() != intent_id
            or not isinstance(intent_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", intent_hash)
        ):
            raise AuthorizationError("wallet public key and intent ID must be canonical strings")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 300:
            raise AuthorizationError("authorization TTL must be an integer between 1 and 300 seconds")
        issued = cls._validate_clock(int(time.time()) if now is None else now, "issued_at")
        expires = issued + ttl_seconds
        auth_id = secrets.token_hex(16)
        payload = cls._payload(
            auth_id, wallet_public_key, policy.chain, policy.policy_hash,
            intent_id, intent_hash, issued, expires,
        )
        signature = hmac.new(signing_key, payload, hashlib.sha256).hexdigest()
        return cls(
            auth_id, wallet_public_key, policy.chain, policy.policy_hash,
            intent_id, intent_hash, issued, expires, signature,
        )

    def verify_and_consume(
        self,
        *,
        signing_key: bytes,
        policy: ExecutionPolicy,
        wallet_public_key: str,
        intent_id: str,
        intent_hash: str,
        consumer: AuthorizationConsumer,
        now: int | None = None,
    ) -> None:
        self._validate_signing_key(signing_key)
        if not isinstance(policy, ExecutionPolicy) or policy.mode is not ExecutionMode.LIVE:
            raise AuthorizationError("policy is not live")
        if not callable(getattr(consumer, "consume", None)):
            raise AuthorizationError("an atomic authorization consumer is required")
        current = self._validate_clock(int(time.time()) if now is None else now, "current time")
        issued = self._validate_clock(self.issued_at, "issued_at")
        expires = self._validate_clock(self.expires_at, "expires_at")
        if expires <= issued or expires - issued > 300:
            raise AuthorizationError("authorization lifetime is invalid")
        if current < issued or current >= expires:
            raise AuthorizationError("authorization is not currently valid")
        if (
            not isinstance(self.authorization_id, str)
            or not self.authorization_id
            or not isinstance(self.signature, str)
            or not isinstance(self.policy_hash, str)
            or not isinstance(self.chain, str)
            or not isinstance(self.wallet_public_key, str)
            or not isinstance(self.intent_id, str)
            or not isinstance(self.intent_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.intent_hash)
        ):
            raise AuthorizationError("authorization fields are malformed")
        if (
            self.wallet_public_key != wallet_public_key
            or self.chain != policy.chain
            or self.policy_hash != policy.policy_hash
            or self.intent_id != intent_id
            or self.intent_hash != intent_hash
        ):
            raise AuthorizationError("authorization scope does not match request")
        payload = self._payload(
            self.authorization_id, self.wallet_public_key, self.chain,
            self.policy_hash, self.intent_id, self.intent_hash, issued, expires,
        )
        expected = hmac.new(signing_key, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.signature):
            raise AuthorizationError("authorization signature is invalid")
        try:
            consumed = consumer.consume(self.authorization_id, expires)
        except Exception as exc:
            raise AuthorizationError("authorization could not be consumed atomically") from exc
        if not consumed:
            raise AuthorizationError("authorization has already been consumed")
