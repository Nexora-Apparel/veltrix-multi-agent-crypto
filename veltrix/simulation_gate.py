"""Fail-closed gate for fresh Solana simulation evidence.

This module consumes normalized evidence from a separately implemented RPC
adapter. It does not call RPC, sign, submit, or broadcast transactions.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

from veltrix.transaction_validator import (
    TransactionIntent,
    TransactionValidationResult,
)


class SimulationGateError(ValueError):
    """Raised when simulation evidence is missing, stale, inconsistent, or unsafe."""


def _nonnegative_int(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SimulationGateError(f"{field} must be a non-negative integer")


def _nonempty(value: object, field: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise SimulationGateError(f"{field} must be a non-empty canonical string")


@dataclass(frozen=True, slots=True)
class ExpectedTokenDelta:
    token_account: str
    mint: str
    owner: str
    delta_raw: int | None
    minimum_delta_raw: int | None = None
    maximum_delta_raw: int | None = None

    def __post_init__(self) -> None:
        for field in ("token_account", "mint", "owner"):
            _nonempty(getattr(self, field), field)
        if self.delta_raw is not None and (
            isinstance(self.delta_raw, bool) or not isinstance(self.delta_raw, int)
        ):
            raise SimulationGateError("expected token delta must be an integer or None")
        for field in ("minimum_delta_raw", "maximum_delta_raw"):
            value = getattr(self, field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise SimulationGateError(f"{field} must be an integer or None")
        bounded = self.minimum_delta_raw is not None or self.maximum_delta_raw is not None
        if (self.delta_raw is None) != bounded:
            raise SimulationGateError("expected token delta must use either exact or bounded mode")
        if self.minimum_delta_raw is not None and self.maximum_delta_raw is not None:
            if self.minimum_delta_raw > self.maximum_delta_raw:
                raise SimulationGateError("minimum token delta exceeds maximum token delta")


@dataclass(frozen=True, slots=True)
class SimulatedTokenBalance:
    token_account: str
    mint: str
    owner: str
    pre_amount_raw: int
    post_amount_raw: int
    token_program_id: str | None = None

    def __post_init__(self) -> None:
        for field in ("token_account", "mint", "owner"):
            _nonempty(getattr(self, field), field)
        _nonnegative_int(self.pre_amount_raw, "pre_amount_raw")
        _nonnegative_int(self.post_amount_raw, "post_amount_raw")
        if self.token_program_id is not None:
            _nonempty(self.token_program_id, "token_program_id")


@dataclass(frozen=True, slots=True)
class SimulatedInnerInstruction:
    """Resolved CPI instruction bytes and account keys returned by RPC simulation."""

    outer_instruction_index: int
    program_id: str
    account_keys: tuple[str, ...]
    data: bytes
    stack_height: int | None = None

    def __post_init__(self) -> None:
        _nonnegative_int(self.outer_instruction_index, "outer_instruction_index")
        _nonempty(self.program_id, "inner instruction program_id")
        if not isinstance(self.account_keys, tuple):
            raise SimulationGateError("inner instruction account_keys must be an immutable tuple")
        for account in self.account_keys:
            _nonempty(account, "inner instruction account key")
        if not isinstance(self.data, bytes) or len(self.data) > 4096:
            raise SimulationGateError("inner instruction data must be immutable bytes no longer than 4096 bytes")
        if self.stack_height is not None:
            if isinstance(self.stack_height, bool) or not isinstance(self.stack_height, int) or self.stack_height < 1:
                raise SimulationGateError("inner instruction stack_height must be a positive integer or None")


@dataclass(frozen=True, slots=True)
class SimulatedAccountState:
    """Raw pre/post account bytes captured inside the same simulation context."""

    address: str
    pre_owner: str
    post_owner: str
    pre_lamports: int
    post_lamports: int
    pre_executable: bool
    post_executable: bool
    pre_data: bytes
    post_data: bytes
    pre_data_sha256: str
    post_data_sha256: str

    def __post_init__(self) -> None:
        for field in ("address", "pre_owner", "post_owner"):
            _nonempty(getattr(self, field), field)
        for field in ("pre_lamports", "post_lamports"):
            _nonnegative_int(getattr(self, field), field)
        if not isinstance(self.pre_executable, bool) or not isinstance(self.post_executable, bool):
            raise SimulationGateError("simulated account executable flags must be booleans")
        if self.pre_owner != self.post_owner or self.pre_executable != self.post_executable:
            raise SimulationGateError("simulated account owner or executable status changed")
        for field in ("pre_data", "post_data"):
            value = getattr(self, field)
            if not isinstance(value, bytes) or len(value) > 1_000_000:
                raise SimulationGateError(f"{field} must be immutable account bytes no larger than 1 MB")
        for data_field, digest_field in (
            ("pre_data", "pre_data_sha256"),
            ("post_data", "post_data_sha256"),
        ):
            digest = getattr(self, digest_field)
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise SimulationGateError(f"{digest_field} must be a lowercase SHA-256 digest")
            if hashlib.sha256(getattr(self, data_field)).hexdigest() != digest:
                raise SimulationGateError(f"{digest_field} does not match its retained account bytes")


@dataclass(frozen=True, slots=True)
class SimulationEvidence:
    message_sha256: str
    recent_blockhash: str
    cluster: str
    genesis_hash: str
    provider_id: str
    response_id: str
    observed_at_epoch_ms: int
    context_slot: int
    current_block_height: int
    last_valid_block_height: int
    simulation_error: str | None
    units_consumed: int
    fee_lamports: int
    token_balances: tuple[SimulatedTokenBalance, ...]
    top_level_program_ids: tuple[str, ...] = ()
    inner_program_ids: tuple[str, ...] = ()
    inner_instruction_count: int = 0
    inner_instructions: tuple[SimulatedInnerInstruction, ...] = ()
    account_states: tuple[SimulatedAccountState, ...] = ()
    logs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in (
            "message_sha256", "recent_blockhash", "cluster", "genesis_hash",
            "provider_id", "response_id",
        ):
            _nonempty(getattr(self, field), field)
        if not re.fullmatch(r"[0-9a-f]{64}", self.message_sha256):
            raise SimulationGateError("message_sha256 must be a lowercase SHA-256 digest")
        for field in (
            "observed_at_epoch_ms", "context_slot", "current_block_height",
            "last_valid_block_height", "units_consumed", "fee_lamports",
        ):
            _nonnegative_int(getattr(self, field), field)
        if self.simulation_error is not None and not isinstance(self.simulation_error, str):
            raise SimulationGateError("simulation_error must be a string or None")
        if not isinstance(self.token_balances, tuple) or any(
            not isinstance(item, SimulatedTokenBalance) for item in self.token_balances
        ):
            raise SimulationGateError("token_balances must be an immutable tuple of validated balances")
        for field in ("top_level_program_ids", "inner_program_ids"):
            values = getattr(self, field)
            if not isinstance(values, tuple) or any(not isinstance(value, str) or not value for value in values):
                raise SimulationGateError(f"{field} must be an immutable tuple of program IDs")
        _nonnegative_int(self.inner_instruction_count, "inner_instruction_count")
        if self.inner_instruction_count != len(self.inner_program_ids):
            raise SimulationGateError("inner instruction count does not match the captured program trace")
        if not isinstance(self.inner_instructions, tuple) or any(
            not isinstance(item, SimulatedInnerInstruction) for item in self.inner_instructions
        ):
            raise SimulationGateError("inner_instructions must be an immutable tuple of validated CPI instructions")
        if self.inner_instructions:
            if len(self.inner_instructions) != self.inner_instruction_count:
                raise SimulationGateError("detailed inner instruction count differs from the program trace")
            if tuple(item.program_id for item in self.inner_instructions) != self.inner_program_ids:
                raise SimulationGateError("detailed inner instruction program IDs differ from the program trace")
        if not isinstance(self.account_states, tuple) or any(
            not isinstance(item, SimulatedAccountState) for item in self.account_states
        ):
            raise SimulationGateError("account_states must be an immutable tuple of validated pre/post account states")
        account_addresses = tuple(item.address for item in self.account_states)
        if len(account_addresses) != len(set(account_addresses)):
            raise SimulationGateError("simulation contains duplicate raw account-state records")
        if not isinstance(self.logs, tuple) or len(self.logs) > 512 or any(
            not isinstance(line, str) or len(line) > 4096 for line in self.logs
        ) or sum(len(line.encode("utf-8")) for line in self.logs) > 65_536:
            raise SimulationGateError("simulation logs must be a bounded immutable tuple of strings")
        keys = [(item.token_account, item.mint, item.owner) for item in self.token_balances]
        if len(keys) != len(set(keys)):
            raise SimulationGateError("simulation contains duplicate token-account balance records")


@dataclass(frozen=True, slots=True)
class SimulationGatePolicy:
    expected_cluster: str
    expected_genesis_hash: str
    allowed_provider_ids: tuple[str, ...]
    max_evidence_age_ms: int = 10_000
    max_future_skew_ms: int = 1_000
    max_compute_units: int = 300_000
    max_fee_lamports: int = 10_000_000

    def __post_init__(self) -> None:
        _nonempty(self.expected_cluster, "expected_cluster")
        _nonempty(self.expected_genesis_hash, "expected_genesis_hash")
        if not isinstance(self.allowed_provider_ids, tuple) or not self.allowed_provider_ids:
            raise SimulationGateError("allowed_provider_ids must be a non-empty immutable tuple")
        for provider in self.allowed_provider_ids:
            _nonempty(provider, "allowed provider ID")
        if len(set(self.allowed_provider_ids)) != len(self.allowed_provider_ids):
            raise SimulationGateError("allowed provider IDs must not contain duplicates")
        for field in (
            "max_evidence_age_ms", "max_future_skew_ms",
            "max_compute_units", "max_fee_lamports",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SimulationGateError(f"{field} must be a non-negative integer")
        if self.max_evidence_age_ms == 0 or self.max_compute_units == 0:
            raise SimulationGateError("evidence age and compute ceilings must be positive")


@dataclass(frozen=True, slots=True)
class SimulationGateResult:
    approved: bool
    message_sha256: str
    provider_id: str
    response_id: str
    context_slot: int
    units_consumed: int
    fee_lamports: int
    verified_token_account_count: int


def evaluate_simulation(
    evidence: SimulationEvidence,
    *,
    intent: TransactionIntent,
    validation: TransactionValidationResult,
    expected_token_deltas: tuple[ExpectedTokenDelta, ...],
    policy: SimulationGatePolicy,
    now_epoch_ms: int,
    minimum_context_slot: int,
) -> SimulationGateResult:
    """Accept only fresh successful simulation matching the exact approved intent."""
    if not isinstance(evidence, SimulationEvidence):
        raise SimulationGateError("simulation evidence is required")
    if not isinstance(intent, TransactionIntent):
        raise SimulationGateError("validated transaction intent is required")
    if not isinstance(validation, TransactionValidationResult):
        raise SimulationGateError("successful structural transaction validation is required")
    if not isinstance(policy, SimulationGatePolicy):
        raise SimulationGateError("simulation policy is required")
    if not isinstance(expected_token_deltas, tuple) or not expected_token_deltas:
        raise SimulationGateError("explicit expected token-account deltas are required")
    if any(not isinstance(item, ExpectedTokenDelta) for item in expected_token_deltas):
        raise SimulationGateError("expected token deltas contain invalid records")
    _nonnegative_int(now_epoch_ms, "now_epoch_ms")
    _nonnegative_int(minimum_context_slot, "minimum_context_slot")

    expected_keys = [(x.token_account, x.mint, x.owner) for x in expected_token_deltas]
    if len(expected_keys) != len(set(expected_keys)):
        raise SimulationGateError("expected token deltas contain duplicate accounts")

    if evidence.message_sha256 != validation.message_sha256:
        raise SimulationGateError("simulation message digest does not match validated transaction")
    if evidence.recent_blockhash != intent.recent_blockhash:
        raise SimulationGateError("simulation blockhash does not match approved transaction intent")
    if evidence.cluster != policy.expected_cluster or evidence.genesis_hash != policy.expected_genesis_hash:
        raise SimulationGateError("simulation cluster identity does not match policy")
    if evidence.provider_id not in policy.allowed_provider_ids:
        raise SimulationGateError("simulation provider is not allowlisted")
    if evidence.simulation_error is not None:
        raise SimulationGateError("transaction simulation reported an execution error")
    if evidence.observed_at_epoch_ms > now_epoch_ms + policy.max_future_skew_ms:
        raise SimulationGateError("simulation evidence timestamp is in the future")
    if now_epoch_ms - evidence.observed_at_epoch_ms > policy.max_evidence_age_ms:
        raise SimulationGateError("simulation evidence is stale")
    if evidence.context_slot < minimum_context_slot:
        raise SimulationGateError("simulation context slot is older than the required minimum")
    if evidence.current_block_height > evidence.last_valid_block_height:
        raise SimulationGateError("transaction blockhash has expired")
    if evidence.units_consumed > policy.max_compute_units:
        raise SimulationGateError("simulation compute usage exceeds policy")
    if evidence.fee_lamports > policy.max_fee_lamports:
        raise SimulationGateError("simulation fee exceeds policy")

    actual = {
        (item.token_account, item.mint, item.owner): item.post_amount_raw - item.pre_amount_raw
        for item in evidence.token_balances
    }
    expected = {
        (item.token_account, item.mint, item.owner): item
        for item in expected_token_deltas
    }
    if set(actual) != set(expected):
        raise SimulationGateError("simulated token-account set does not exactly match approved effects")
    for key, expectation in expected.items():
        actual_delta = actual[key]
        if expectation.delta_raw is not None:
            if actual_delta != expectation.delta_raw:
                raise SimulationGateError("simulated token-account balance delta differs from approved effect")
            continue
        if expectation.minimum_delta_raw is not None and actual_delta < expectation.minimum_delta_raw:
            raise SimulationGateError("simulated token-account balance delta is below the approved minimum")
        if expectation.maximum_delta_raw is not None and actual_delta > expectation.maximum_delta_raw:
            raise SimulationGateError("simulated token-account balance delta exceeds the approved maximum")

    return SimulationGateResult(
        approved=True,
        message_sha256=validation.message_sha256,
        provider_id=evidence.provider_id,
        response_id=evidence.response_id,
        context_slot=evidence.context_slot,
        units_consumed=evidence.units_consumed,
        fee_lamports=evidence.fee_lamports,
        verified_token_account_count=len(expected),
    )
