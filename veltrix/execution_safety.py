"""Pre-sign execution safety gates. No signer or submitter is implemented here.

The transaction validator consumes a canonical instruction/effect manifest. It
is not a raw Solana message decoder and must not be treated as a signing adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Protocol

from .execution_policy import (
    AuthorizationConsumer,
    AuthorizationError,
    ExecutionMode,
    ExecutionPolicy,
    OperatorAuthorization,
    PolicyError,
)
from .emergency_stop import SQLiteEmergencyStop
from .submission_coordinator import ExecutionState, SubmissionCoordinator


_HEX_256 = re.compile(r"^[0-9a-f]{64}$")


class ExecutionGateError(ValueError):
    def __init__(self, reason_code: str):
        if not isinstance(reason_code, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", reason_code):
            raise ValueError("reason_code is malformed")
        self.reason_code = reason_code
        super().__init__(reason_code)


class SignerBoundary(Protocol):
    """Future interface only. No concrete signer exists in this release."""

    def sign(self, transaction_manifest: "UnsignedTransactionPlan") -> bytes:
        """Sign a fully validated manifest in an isolated future adapter."""
        ...


def _integer(value: int, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ExecutionGateError(f"invalid_{name}")
    return value


def _digest(value: str, name: str) -> str:
    if not isinstance(value, str) or not _HEX_256.fullmatch(value):
        raise ExecutionGateError(f"invalid_{name}")
    return value


def _signed_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionGateError(f"invalid_{name}")
    return value


def _canonical_json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    intent_id: str
    chain: str
    wallet_public_key: str
    input_mint: str
    output_mint: str
    input_base_units: int
    requested_exposure_base_units: int
    minimum_output_base_units: int
    max_slippage_bps: int
    max_priority_fee_base_units: int
    expires_at_ms: int
    allowed_accounts: tuple[str, ...]
    expected_program_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("intent_id", "chain", "wallet_public_key", "input_mint", "output_mint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value.strip() != value:
                raise ExecutionGateError(f"invalid_{name}")
        if self.input_mint == self.output_mint:
            raise ExecutionGateError("identical_input_output_mints")
        for name in ("input_base_units", "requested_exposure_base_units", "minimum_output_base_units", "expires_at_ms"):
            _integer(getattr(self, name), name, minimum=1)
        _integer(self.max_slippage_bps, "max_slippage_bps")
        _integer(self.max_priority_fee_base_units, "max_priority_fee_base_units")
        if self.max_slippage_bps > 10_000:
            raise ExecutionGateError("invalid_max_slippage_bps")
        for name in ("allowed_accounts", "expected_program_ids"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not values:
                raise ExecutionGateError(f"invalid_{name}")
            if any(not isinstance(item, str) or not item or item.strip() != item for item in values):
                raise ExecutionGateError(f"invalid_{name}")
            if len(set(values)) != len(values):
                raise ExecutionGateError(f"duplicate_{name}")

    def canonical_payload(self) -> bytes:
        return _canonical_json({
            "intent_id": self.intent_id,
            "chain": self.chain,
            "wallet_public_key": self.wallet_public_key,
            "input_mint": self.input_mint,
            "output_mint": self.output_mint,
            "input_base_units": self.input_base_units,
            "requested_exposure_base_units": self.requested_exposure_base_units,
            "minimum_output_base_units": self.minimum_output_base_units,
            "max_slippage_bps": self.max_slippage_bps,
            "max_priority_fee_base_units": self.max_priority_fee_base_units,
            "expires_at_ms": self.expires_at_ms,
            "allowed_accounts": sorted(self.allowed_accounts),
            "expected_program_ids": sorted(self.expected_program_ids),
        })

    @property
    def intent_hash(self) -> str:
        return hashlib.sha256(self.canonical_payload()).hexdigest()


@dataclass(frozen=True, slots=True)
class InstructionPlan:
    program_id: str
    operation: str
    accounts: tuple[str, ...]
    data_hash: str
    changes_authority: bool = False
    changes_delegate: bool = False


@dataclass(frozen=True, slots=True)
class AccountEffect:
    account: str
    role: str
    mint: str | None
    owner: str | None
    delta_base_units: int
    authority_before: str | None = None
    authority_after: str | None = None
    delegate_before: str | None = None
    delegate_after: str | None = None


@dataclass(frozen=True, slots=True)
class UnsignedTransactionPlan:
    intent_id: str
    chain: str
    wallet_public_key: str
    input_mint: str
    output_mint: str
    input_base_units: int
    minimum_output_base_units: int
    expected_output_base_units: int
    slippage_bps: int
    priority_fee_base_units: int
    recent_blockhash: str
    blockhash_expires_at_ms: int
    instructions: tuple[InstructionPlan, ...]
    account_effects: tuple[AccountEffect, ...]
    message_hash: str

    def canonical_payload(self) -> bytes:
        return _canonical_json({
            "intent_id": self.intent_id,
            "chain": self.chain,
            "wallet_public_key": self.wallet_public_key,
            "input_mint": self.input_mint,
            "output_mint": self.output_mint,
            "input_base_units": self.input_base_units,
            "minimum_output_base_units": self.minimum_output_base_units,
            "expected_output_base_units": self.expected_output_base_units,
            "slippage_bps": self.slippage_bps,
            "priority_fee_base_units": self.priority_fee_base_units,
            "recent_blockhash": self.recent_blockhash,
            "blockhash_expires_at_ms": self.blockhash_expires_at_ms,
            "instructions": [
                {
                    "program_id": item.program_id,
                    "operation": item.operation,
                    "accounts": list(item.accounts),
                    "data_hash": item.data_hash,
                    "changes_authority": item.changes_authority,
                    "changes_delegate": item.changes_delegate,
                } for item in self.instructions
            ],
            "account_effects": [
                {
                    "account": item.account,
                    "role": item.role,
                    "mint": item.mint,
                    "owner": item.owner,
                    "delta_base_units": item.delta_base_units,
                    "authority_before": item.authority_before,
                    "authority_after": item.authority_after,
                    "delegate_before": item.delegate_before,
                    "delegate_after": item.delegate_after,
                } for item in self.account_effects
            ],
        })

    def computed_message_hash(self) -> str:
        return hashlib.sha256(self.canonical_payload()).hexdigest()


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    intent_id: str
    chain: str
    mint: str
    observed_at_ms: int
    market_observed_at_ms: int
    rpc_observed_at_ms: int
    risk_state: str
    missing_claims: tuple[str, ...]
    risk_flags: tuple[str, ...]
    current_token_exposure_base_units: int
    current_portfolio_exposure_base_units: int
    daily_loss_base_units: int
    evidence_hash: str


@dataclass(frozen=True, slots=True)
class SimulationReceipt:
    intent_id: str
    chain: str
    message_hash: str
    succeeded: bool
    slot: int
    simulated_at_ms: int
    fee_base_units: int
    output_base_units: int
    account_state_hash: str


class TransactionValidator:
    """Validate a normalized unsigned instruction/effect manifest, not raw bytes."""

    @staticmethod
    def validate(
        intent: ExecutionIntent,
        plan: UnsignedTransactionPlan,
        policy: ExecutionPolicy,
        *,
        now_ms: int,
    ) -> None:
        if policy.mode is not ExecutionMode.LIVE:
            raise ExecutionGateError("paper_only_policy")
        if plan.intent_id != intent.intent_id or plan.chain != intent.chain or plan.chain != policy.chain:
            raise ExecutionGateError("transaction_scope_mismatch")
        if plan.wallet_public_key != intent.wallet_public_key:
            raise ExecutionGateError("transaction_wallet_mismatch")
        if plan.input_mint != intent.input_mint or plan.output_mint != intent.output_mint:
            raise ExecutionGateError("transaction_mint_mismatch")
        if plan.input_base_units != intent.input_base_units or plan.minimum_output_base_units != intent.minimum_output_base_units:
            raise ExecutionGateError("transaction_amount_mismatch")
        _integer(plan.expected_output_base_units, "expected_output_base_units", minimum=1)
        if plan.expected_output_base_units < intent.minimum_output_base_units:
            raise ExecutionGateError("minimum_output_not_met")
        _integer(plan.slippage_bps, "slippage_bps")
        _integer(plan.priority_fee_base_units, "priority_fee_base_units")
        if plan.slippage_bps > min(intent.max_slippage_bps, policy.max_slippage_bps):
            raise ExecutionGateError("slippage_limit_exceeded")
        if plan.priority_fee_base_units > min(intent.max_priority_fee_base_units, policy.max_priority_fee_base_units):
            raise ExecutionGateError("priority_fee_limit_exceeded")
        if not isinstance(plan.recent_blockhash, str) or not plan.recent_blockhash:
            raise ExecutionGateError("missing_recent_blockhash")
        expiry = _integer(plan.blockhash_expires_at_ms, "blockhash_expiry", minimum=1)
        if expiry <= now_ms or expiry > intent.expires_at_ms or expiry - now_ms > 120_000:
            raise ExecutionGateError("blockhash_stale_or_unbounded")
        if not isinstance(plan.instructions, tuple) or not 1 <= len(plan.instructions) <= 8:
            raise ExecutionGateError("invalid_instruction_count")
        if not isinstance(plan.account_effects, tuple) or not plan.account_effects:
            raise ExecutionGateError("missing_account_effects")
        _digest(plan.message_hash, "message_hash")
        if not hmac_compare(plan.message_hash, plan.computed_message_hash()):
            raise ExecutionGateError("transaction_manifest_hash_mismatch")

        allowed_programs = set(policy.allowed_program_ids) & set(intent.expected_program_ids)
        swap_count = 0
        compute_budget_count = 0
        for instruction in plan.instructions:
            if not isinstance(instruction, InstructionPlan):
                raise ExecutionGateError("malformed_instruction")
            if not isinstance(instruction.program_id, str) or instruction.program_id not in allowed_programs:
                raise ExecutionGateError("program_not_allowlisted")
            if not isinstance(instruction.operation, str) or instruction.operation not in {"SWAP", "COMPUTE_BUDGET"}:
                raise ExecutionGateError("unknown_instruction")
            if instruction.operation == "SWAP":
                swap_count += 1
            elif instruction.operation == "COMPUTE_BUDGET":
                compute_budget_count += 1
            if instruction.changes_authority or instruction.changes_delegate:
                raise ExecutionGateError("authority_or_delegate_change")
            if not isinstance(instruction.accounts, tuple) or not instruction.accounts:
                raise ExecutionGateError("instruction_accounts_missing")
            if any(
                not isinstance(account, str) or not account or account not in intent.allowed_accounts
                for account in instruction.accounts
            ):
                raise ExecutionGateError("unexpected_instruction_account")
            if len(set(instruction.accounts)) != len(instruction.accounts):
                raise ExecutionGateError("duplicate_instruction_account")
            _digest(instruction.data_hash, "instruction_data_hash")
        if swap_count != 1:
            raise ExecutionGateError("expected_exactly_one_swap_instruction")
        if compute_budget_count > 2:
            raise ExecutionGateError("too_many_compute_budget_instructions")

        input_effects = []
        output_effects = []
        seen_effect_accounts = set()
        for effect in plan.account_effects:
            if not isinstance(effect, AccountEffect):
                raise ExecutionGateError("malformed_account_effect")
            if not isinstance(effect.account, str) or not effect.account or effect.account not in intent.allowed_accounts:
                raise ExecutionGateError("unexpected_account_mutation")
            if not isinstance(effect.role, str):
                raise ExecutionGateError("unknown_account_effect_role")
            if effect.account in seen_effect_accounts:
                raise ExecutionGateError("duplicate_account_effect")
            seen_effect_accounts.add(effect.account)
            _signed_integer(effect.delta_base_units, "account_delta")
            if effect.authority_before != effect.authority_after:
                raise ExecutionGateError("account_authority_changed")
            if effect.delegate_before != effect.delegate_after:
                raise ExecutionGateError("account_delegate_changed")
            if effect.role == "wallet_input":
                if effect.owner != intent.wallet_public_key or effect.mint != intent.input_mint:
                    raise ExecutionGateError("unexpected_input_account")
                input_effects.append(effect.delta_base_units)
            elif effect.role == "wallet_output":
                if effect.owner != intent.wallet_public_key or effect.mint != intent.output_mint:
                    raise ExecutionGateError("unexpected_output_account")
                output_effects.append(effect.delta_base_units)
            elif effect.role not in {"pool_state", "pool_vault", "fee_payer"}:
                raise ExecutionGateError("unknown_account_effect_role")
        if sum(input_effects) != -intent.input_base_units:
            raise ExecutionGateError("wallet_input_delta_mismatch")
        if sum(output_effects) != plan.expected_output_base_units:
            raise ExecutionGateError("wallet_output_delta_mismatch")


def hmac_compare(first: str, second: str) -> bool:
    import hmac
    return hmac.compare_digest(first, second)


class RiskGate:
    @staticmethod
    def validate(
        intent: ExecutionIntent,
        risk: RiskSnapshot,
        policy: ExecutionPolicy,
        *,
        now_ms: int,
    ) -> None:
        if policy.mode is not ExecutionMode.LIVE:
            raise ExecutionGateError("paper_only_policy")
        if risk.intent_id != intent.intent_id or risk.chain != intent.chain or risk.chain != policy.chain or risk.mint != intent.output_mint:
            raise ExecutionGateError("risk_scope_mismatch")
        if risk.risk_state != "LIVE_RISK_APPROVED":
            raise ExecutionGateError("token_risk_not_live_approved")
        if (
            not isinstance(risk.missing_claims, tuple)
            or any(not isinstance(item, str) or not item for item in risk.missing_claims)
            or risk.missing_claims
        ):
            raise ExecutionGateError("risk_claims_missing")
        if (
            not isinstance(risk.risk_flags, tuple)
            or any(not isinstance(item, str) or not item for item in risk.risk_flags)
            or risk.risk_flags
        ):
            raise ExecutionGateError("risk_flags_present")
        for name, observed in (
            ("risk", risk.observed_at_ms),
            ("market", risk.market_observed_at_ms),
            ("rpc", risk.rpc_observed_at_ms),
        ):
            stamp = _integer(observed, f"{name}_timestamp", minimum=1)
            age = now_ms - stamp
            if age < 0 or age > policy.max_market_data_age_ms:
                raise ExecutionGateError(f"{name}_evidence_stale")
        for name, value in (
            ("current_token_exposure", risk.current_token_exposure_base_units),
            ("current_portfolio_exposure", risk.current_portfolio_exposure_base_units),
            ("daily_loss", risk.daily_loss_base_units),
        ):
            _integer(value, name)
        _integer(intent.input_base_units, "trade_size", minimum=1)
        if policy.max_trade_base_units <= 0 or intent.input_base_units > policy.max_trade_base_units:
            raise ExecutionGateError("trade_limit_exceeded")
        if (
            policy.max_token_exposure_base_units <= 0
            or risk.current_token_exposure_base_units + intent.requested_exposure_base_units > policy.max_token_exposure_base_units
        ):
            raise ExecutionGateError("token_exposure_limit_exceeded")
        if (
            policy.max_portfolio_exposure_base_units <= 0
            or risk.current_portfolio_exposure_base_units + intent.requested_exposure_base_units > policy.max_portfolio_exposure_base_units
        ):
            raise ExecutionGateError("portfolio_exposure_limit_exceeded")
        if risk.daily_loss_base_units > policy.max_daily_loss_base_units:
            raise ExecutionGateError("daily_loss_limit_exceeded")
        _digest(risk.evidence_hash, "risk_evidence_hash")


class SimulationGate:
    @staticmethod
    def validate(
        intent: ExecutionIntent,
        plan: UnsignedTransactionPlan,
        receipt: SimulationReceipt,
        policy: ExecutionPolicy,
        *,
        now_ms: int,
    ) -> None:
        if not isinstance(receipt, SimulationReceipt) or receipt.succeeded is not True:
            raise ExecutionGateError("simulation_failed_or_missing")
        if receipt.intent_id != intent.intent_id or receipt.chain != intent.chain or receipt.chain != policy.chain:
            raise ExecutionGateError("simulation_scope_mismatch")
        if receipt.message_hash != plan.message_hash:
            raise ExecutionGateError("simulation_message_mismatch")
        _integer(receipt.slot, "simulation_slot", minimum=1)
        stamp = _integer(receipt.simulated_at_ms, "simulation_timestamp", minimum=1)
        age = now_ms - stamp
        if age < 0 or age > policy.max_market_data_age_ms:
            raise ExecutionGateError("simulation_stale")
        fee = _integer(receipt.fee_base_units, "simulation_fee")
        output = _integer(receipt.output_base_units, "simulation_output", minimum=1)
        if fee > min(intent.max_priority_fee_base_units, policy.max_priority_fee_base_units, plan.priority_fee_base_units):
            raise ExecutionGateError("simulated_fee_limit_exceeded")
        if output < intent.minimum_output_base_units:
            raise ExecutionGateError("simulated_output_below_minimum")
        if output != plan.expected_output_base_units:
            raise ExecutionGateError("simulated_output_mismatch")
        _digest(receipt.account_state_hash, "simulation_account_state_hash")


@dataclass(frozen=True, slots=True)
class ExecutionPreparation:
    intent_id: str
    state: ExecutionState
    live_execution_enabled: bool
    reason_code: str | None
    duplicate: bool = False


class ExecutionSafetyPipeline:
    """Validate and authorize an intent, but deliberately never sign or submit."""

    def __init__(
        self,
        *,
        policy: ExecutionPolicy,
        coordinator: SubmissionCoordinator,
        emergency_stop: SQLiteEmergencyStop,
        authorization_consumer: AuthorizationConsumer,
        authorization_signing_key: bytes,
        clock_ms,
    ):
        if not isinstance(policy, ExecutionPolicy):
            raise TypeError("policy must be an ExecutionPolicy")
        if not isinstance(coordinator, SubmissionCoordinator):
            raise TypeError("coordinator must be a SubmissionCoordinator")
        if not isinstance(emergency_stop, SQLiteEmergencyStop):
            raise TypeError("emergency_stop must be a SQLiteEmergencyStop")
        if not callable(getattr(authorization_consumer, "consume", None)):
            raise TypeError("authorization_consumer must support atomic consume")
        if not isinstance(authorization_signing_key, bytes) or len(authorization_signing_key) < 32:
            raise ValueError("authorization signing key must be injected and at least 32 bytes")
        if not callable(clock_ms):
            raise TypeError("clock_ms must be callable")
        self.policy = policy
        self.coordinator = coordinator
        self.emergency_stop = emergency_stop
        self.authorization_consumer = authorization_consumer
        self._authorization_signing_key = authorization_signing_key
        self.clock_ms = clock_ms

    def _fail(self, intent: ExecutionIntent, now_ms: int, reason_code: str) -> ExecutionPreparation:
        row = self.coordinator.get(intent.intent_id)
        if row is not None and ExecutionState(row["state"]) not in {
            ExecutionState.FAILED, ExecutionState.CANCELLED, ExecutionState.CONFIRMED
        }:
            self.coordinator.fail(intent.intent_id, occurred_at_ms=now_ms, reason_code=reason_code)
        return ExecutionPreparation(intent.intent_id, ExecutionState.FAILED, False, reason_code)

    def prepare(
        self,
        *,
        intent: ExecutionIntent,
        plan: UnsignedTransactionPlan,
        risk: RiskSnapshot,
        simulation: SimulationReceipt,
        authorization: OperatorAuthorization | None,
    ) -> ExecutionPreparation:
        now_ms = self.clock_ms()
        _integer(now_ms, "clock", minimum=1)
        row = self.coordinator.create(
            intent_id=intent.intent_id,
            chain=intent.chain,
            wallet_public_key=intent.wallet_public_key,
            policy_hash=self.policy.policy_hash,
            intent_hash=intent.intent_hash,
            created_at_ms=now_ms,
        )
        current = ExecutionState(row["state"])
        if current is not ExecutionState.CREATED:
            return ExecutionPreparation(intent.intent_id, current, False, row.get("reason_code"), True)

        try:
            if self.policy.mode is not ExecutionMode.LIVE:
                raise ExecutionGateError("live_execution_disabled")
            if now_ms >= intent.expires_at_ms:
                raise ExecutionGateError("intent_expired")
            try:
                if self.emergency_stop.is_active():
                    raise ExecutionGateError("emergency_stop_active")
            except ExecutionGateError:
                raise
            except Exception:
                raise ExecutionGateError("emergency_stop_unavailable") from None

            TransactionValidator.validate(intent, plan, self.policy, now_ms=now_ms)
            self.coordinator.transition(
                intent.intent_id, expected_state=ExecutionState.CREATED,
                next_state=ExecutionState.VALIDATED, occurred_at_ms=now_ms,
                reason_code="transaction_validated",
                evidence_hash=risk.evidence_hash,
            )
            RiskGate.validate(intent, risk, self.policy, now_ms=now_ms)
            SimulationGate.validate(intent, plan, simulation, self.policy, now_ms=now_ms)
            self.coordinator.transition(
                intent.intent_id, expected_state=ExecutionState.VALIDATED,
                next_state=ExecutionState.SIMULATED, occurred_at_ms=now_ms,
                reason_code="simulation_validated",
                evidence_hash=simulation.account_state_hash,
                transaction_hash=plan.message_hash,
            )
            if not isinstance(authorization, OperatorAuthorization):
                raise ExecutionGateError("operator_authorization_missing")
            authorization.verify_and_consume(
                signing_key=self._authorization_signing_key,
                policy=self.policy,
                wallet_public_key=intent.wallet_public_key,
                intent_id=intent.intent_id,
                consumer=self.authorization_consumer,
                now=now_ms // 1000,
            )
            self.coordinator.transition(
                intent.intent_id, expected_state=ExecutionState.SIMULATED,
                next_state=ExecutionState.AUTHORIZED, occurred_at_ms=now_ms,
                reason_code="operator_authorized",
                evidence_hash=risk.evidence_hash,
                transaction_hash=plan.message_hash,
            )
            return ExecutionPreparation(
                intent.intent_id, ExecutionState.AUTHORIZED, False,
                "signing_adapter_not_implemented",
            )
        except ExecutionGateError as exc:
            return self._fail(intent, now_ms, exc.reason_code)
        except (AuthorizationError, PolicyError):
            return self._fail(intent, now_ms, "operator_authorization_rejected")
        except Exception:
            return self._fail(intent, now_ms, "safety_gate_internal_error")
