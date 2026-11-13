from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
import re

import pytest

from veltrix.authorization_store import SQLiteAuthorizationConsumer
from veltrix.emergency_stop import EmergencyStopError, SQLiteEmergencyStop
from veltrix.execution_policy import (
    AuthorizationError,
    ExecutionMode,
    ExecutionPolicy,
    OperatorAuthorization,
)
from veltrix.execution_safety import (
    AccountEffect,
    ExecutionGateError,
    ExecutionIntent,
    ExecutionSafetyPipeline,
    ExecutionState,
    InstructionPlan,
    RiskGate,
    RiskSnapshot,
    SimulationGate,
    SimulationReceipt,
    TransactionValidator,
    UnsignedTransactionPlan,
)
from veltrix.submission_coordinator import (
    ExecutionAuditStore,
    ExecutionDisabled,
    ExecutionState as StoredExecutionState,
    IdempotencyConflict,
    SubmissionCoordinator,
)


NOW_MS = 1_000_000
KEY = b"execution-safety-test-key-32-bytes!!"
WALLET = "wallet-public-key"
INPUT_MINT = "input-mint"
OUTPUT_MINT = "output-mint"


def live_policy():
    return ExecutionPolicy(
        mode=ExecutionMode.LIVE,
        chain="solana-devnet",
        max_trade_base_units=2_000,
        max_token_exposure_base_units=5_000,
        max_portfolio_exposure_base_units=10_000,
        max_daily_loss_base_units=100,
        max_slippage_bps=100,
        max_priority_fee_base_units=50,
        max_market_data_age_ms=5_000,
        allowed_program_ids=("swap-program", "compute-budget-program"),
    )


def make_intent():
    return ExecutionIntent(
        intent_id="intent-0001",
        chain="solana-devnet",
        wallet_public_key=WALLET,
        input_mint=INPUT_MINT,
        output_mint=OUTPUT_MINT,
        input_base_units=1_000,
        requested_exposure_base_units=1_000,
        minimum_output_base_units=900,
        max_slippage_bps=100,
        max_priority_fee_base_units=40,
        expires_at_ms=NOW_MS + 10_000,
        allowed_accounts=("wallet-in", "wallet-out", "pool-vault", "fee-payer"),
        expected_program_ids=("swap-program", "compute-budget-program"),
    )


def make_plan(intent=None):
    intent = intent or make_intent()
    plan = UnsignedTransactionPlan(
        intent_id=intent.intent_id,
        chain=intent.chain,
        wallet_public_key=intent.wallet_public_key,
        input_mint=intent.input_mint,
        output_mint=intent.output_mint,
        input_base_units=intent.input_base_units,
        minimum_output_base_units=intent.minimum_output_base_units,
        expected_output_base_units=950,
        slippage_bps=75,
        priority_fee_base_units=10,
        recent_blockhash="recent-blockhash",
        blockhash_expires_at_ms=NOW_MS + 4_000,
        instructions=(
            InstructionPlan(
                "swap-program", "SWAP",
                ("wallet-in", "wallet-out", "pool-vault"),
                "a" * 64,
            ),
            InstructionPlan(
                "compute-budget-program", "COMPUTE_BUDGET",
                ("fee-payer",), "b" * 64,
            ),
        ),
        account_effects=(
            AccountEffect("wallet-in", "wallet_input", INPUT_MINT, WALLET, -1_000),
            AccountEffect("wallet-out", "wallet_output", OUTPUT_MINT, WALLET, 950),
            AccountEffect("pool-vault", "pool_vault", INPUT_MINT, "pool-owner", 1_000),
            AccountEffect("fee-payer", "fee_payer", None, WALLET, -10),
        ),
        message_hash="0" * 64,
    )
    return replace(plan, message_hash=plan.computed_message_hash())


def make_risk(intent=None, **overrides):
    intent = intent or make_intent()
    values = {
        "intent_id": intent.intent_id,
        "chain": intent.chain,
        "mint": intent.output_mint,
        "observed_at_ms": NOW_MS,
        "market_observed_at_ms": NOW_MS,
        "rpc_observed_at_ms": NOW_MS,
        "risk_state": "LIVE_RISK_APPROVED",
        "missing_claims": (),
        "risk_flags": (),
        "current_token_exposure_base_units": 1_000,
        "current_portfolio_exposure_base_units": 2_000,
        "daily_loss_base_units": 0,
        "evidence_hash": "d" * 64,
    }
    values.update(overrides)
    return RiskSnapshot(**values)


def make_simulation(intent=None, plan=None, **overrides):
    intent = intent or make_intent()
    plan = plan or make_plan(intent)
    values = {
        "intent_id": intent.intent_id,
        "chain": intent.chain,
        "message_hash": plan.message_hash,
        "succeeded": True,
        "slot": 123,
        "simulated_at_ms": NOW_MS,
        "fee_base_units": 8,
        "output_base_units": plan.expected_output_base_units,
        "account_state_hash": "c" * 64,
    }
    values.update(overrides)
    return SimulationReceipt(**values)


def make_authorization(intent=None, policy=None):
    intent = intent or make_intent()
    policy = policy or live_policy()
    return OperatorAuthorization.issue(
        signing_key=KEY,
        wallet_public_key=intent.wallet_public_key,
        policy=policy,
        intent_id=intent.intent_id,
        ttl_seconds=60,
        now=NOW_MS // 1000,
    )


def make_pipeline(tmp_path, *, policy=None, emergency=None):
    policy = policy or live_policy()
    coordinator = SubmissionCoordinator(tmp_path / "execution.sqlite3")
    stop = emergency or SQLiteEmergencyStop(tmp_path / "emergency.sqlite3")
    stop.set_active(False, reason_code="operator_clear", changed_at_ms=NOW_MS)
    consumer = SQLiteAuthorizationConsumer(tmp_path / "authorization.sqlite3")
    pipeline = ExecutionSafetyPipeline(
        policy=policy,
        coordinator=coordinator,
        emergency_stop=stop,
        authorization_consumer=consumer,
        authorization_signing_key=KEY,
        clock_ms=lambda: NOW_MS,
    )
    return pipeline, coordinator, stop, consumer


class FakeSigner:
    def __init__(self):
        self.calls = 0

    def sign(self, _manifest):
        self.calls += 1
        raise AssertionError("the Stage 09 release must never call a signer")


def test_validated_pipeline_stops_at_authorized_without_signing(tmp_path):
    intent = make_intent()
    policy = live_policy()
    pipeline, coordinator, _, _ = make_pipeline(tmp_path, policy=policy)
    fake_signer = FakeSigner()

    result = pipeline.prepare(
        intent=intent,
        plan=make_plan(intent),
        risk=make_risk(intent),
        simulation=make_simulation(intent),
        authorization=make_authorization(intent, policy),
    )

    assert result.state is ExecutionState.AUTHORIZED
    assert result.live_execution_enabled is False
    assert result.reason_code == "signing_adapter_not_implemented"
    assert fake_signer.calls == 0
    assert not hasattr(pipeline, "sign")
    assert coordinator.get(intent.intent_id)["state"] == "AUTHORIZED"


def test_default_environment_policy_cannot_enter_live_pipeline(tmp_path):
    intent = make_intent()
    paper = ExecutionPolicy.from_environment({})
    pipeline, coordinator, _, _ = make_pipeline(tmp_path, policy=paper)
    fake_signer = FakeSigner()

    result = pipeline.prepare(
        intent=intent,
        plan=make_plan(intent),
        risk=make_risk(intent),
        simulation=make_simulation(intent),
        authorization=None,
    )

    assert result.state is ExecutionState.FAILED
    assert result.reason_code == "live_execution_disabled"
    assert fake_signer.calls == 0
    assert coordinator.get(intent.intent_id)["state"] == "FAILED"


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("unknown_program", "program_not_allowlisted"),
        ("unexpected_account", "unexpected_instruction_account"),
        ("authority_change", "authority_or_delegate_change"),
        ("wallet_delta", "wallet_input_delta_mismatch"),
        ("message_hash", "transaction_manifest_hash_mismatch"),
        ("stale_blockhash", "blockhash_stale_or_unbounded"),
    ],
)
def test_transaction_validator_rejects_adversarial_manifests(mutation, reason):
    intent = make_intent()
    plan = make_plan(intent)
    if mutation == "unknown_program":
        instructions = (replace(plan.instructions[0], program_id="unknown-program"), plan.instructions[1])
        plan = replace(plan, instructions=instructions, message_hash="0" * 64)
        plan = replace(plan, message_hash=plan.computed_message_hash())
    elif mutation == "unexpected_account":
        instructions = (replace(plan.instructions[0], accounts=("wallet-in", "attacker", "pool-vault")), plan.instructions[1])
        plan = replace(plan, instructions=instructions, message_hash="0" * 64)
        plan = replace(plan, message_hash=plan.computed_message_hash())
    elif mutation == "authority_change":
        instructions = (replace(plan.instructions[0], changes_authority=True), plan.instructions[1])
        plan = replace(plan, instructions=instructions, message_hash="0" * 64)
        plan = replace(plan, message_hash=plan.computed_message_hash())
    elif mutation == "wallet_delta":
        effects = (replace(plan.account_effects[0], delta_base_units=-999), *plan.account_effects[1:])
        plan = replace(plan, account_effects=effects, message_hash="0" * 64)
        plan = replace(plan, message_hash=plan.computed_message_hash())
    elif mutation == "message_hash":
        plan = replace(plan, message_hash="f" * 64)
    elif mutation == "stale_blockhash":
        plan = replace(plan, blockhash_expires_at_ms=NOW_MS)
        plan = replace(plan, message_hash="0" * 64)
        plan = replace(plan, message_hash=plan.computed_message_hash())

    with pytest.raises(ExecutionGateError, match=reason):
        TransactionValidator.validate(intent, plan, live_policy(), now_ms=NOW_MS)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"risk_state": "EVIDENCE_COLLECTED_REVIEW_REQUIRED"}, "token_risk_not_live_approved"),
        ({"missing_claims": ("holder_inventory",)}, "risk_claims_missing"),
        ({"risk_flags": ("mint_authority_active",)}, "risk_flags_present"),
        ({"market_observed_at_ms": NOW_MS - 10_000}, "market_evidence_stale"),
        ({"current_token_exposure_base_units": 5_000}, "token_exposure_limit_exceeded"),
        ({"current_portfolio_exposure_base_units": 10_000}, "portfolio_exposure_limit_exceeded"),
        ({"daily_loss_base_units": 101}, "daily_loss_limit_exceeded"),
    ],
)
def test_risk_gate_rejects_missing_stale_or_over_limit_evidence(overrides, reason):
    intent = make_intent()
    with pytest.raises(ExecutionGateError, match=reason):
        RiskGate.validate(intent, make_risk(intent, **overrides), live_policy(), now_ms=NOW_MS)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"succeeded": False}, "simulation_failed_or_missing"),
        ({"simulated_at_ms": NOW_MS - 10_000}, "simulation_stale"),
        ({"output_base_units": 899}, "simulated_output_below_minimum"),
        ({"output_base_units": 951}, "simulated_output_mismatch"),
        ({"fee_base_units": 51}, "simulated_fee_limit_exceeded"),
        ({"message_hash": "f" * 64}, "simulation_message_mismatch"),
    ],
)
def test_simulation_gate_rejects_failed_stale_or_mismatched_receipts(overrides, reason):
    intent = make_intent()
    plan = make_plan(intent)
    receipt = make_simulation(intent, plan, **overrides)
    with pytest.raises(ExecutionGateError, match=reason):
        SimulationGate.validate(intent, plan, receipt, live_policy(), now_ms=NOW_MS)


def test_emergency_stop_is_active_by_default_and_survives_restart(tmp_path):
    database = tmp_path / "stop.sqlite3"
    first = SQLiteEmergencyStop(database)
    assert first.is_active() is True
    first.set_active(False, reason_code="operator_clear", changed_at_ms=NOW_MS)
    assert SQLiteEmergencyStop(database).is_active() is False
    SQLiteEmergencyStop(database).set_active(True, reason_code="manual_stop", changed_at_ms=NOW_MS + 1)
    restarted = SQLiteEmergencyStop(database)
    assert restarted.is_active() is True
    assert [event["reason_code"] for event in restarted.events()] == [
        "operator_clear", "manual_stop"
    ]
    with sqlite3.connect(database) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM execution_emergency_stop_events")


def test_emergency_stop_unavailable_fails_closed(tmp_path):
    store = SQLiteEmergencyStop(tmp_path / "working.sqlite3")
    store.database_path = str(tmp_path / "missing-parent" / "stop.sqlite3")
    with pytest.raises(EmergencyStopError, match="unavailable"):
        store.is_active()


def test_active_emergency_stop_prevents_authorization_and_signer_calls(tmp_path):
    intent = make_intent()
    policy = live_policy()
    stop = SQLiteEmergencyStop(tmp_path / "stop.sqlite3")
    coordinator = SubmissionCoordinator(tmp_path / "execution.sqlite3")
    pipeline = ExecutionSafetyPipeline(
        policy=policy,
        coordinator=coordinator,
        emergency_stop=stop,
        authorization_consumer=SQLiteAuthorizationConsumer(tmp_path / "auth.sqlite3"),
        authorization_signing_key=KEY,
        clock_ms=lambda: NOW_MS,
    )
    fake_signer = FakeSigner()
    result = pipeline.prepare(
        intent=intent,
        plan=make_plan(intent),
        risk=make_risk(intent),
        simulation=make_simulation(intent),
        authorization=make_authorization(intent, policy),
    )
    assert result.state is ExecutionState.FAILED
    assert result.reason_code == "emergency_stop_active"
    assert fake_signer.calls == 0


def test_authorization_replay_is_rejected_by_pipeline(tmp_path):
    intent = make_intent()
    policy = live_policy()
    pipeline, coordinator, _, consumer = make_pipeline(tmp_path, policy=policy)
    authorization = make_authorization(intent, policy)
    authorization.verify_and_consume(
        signing_key=KEY,
        policy=policy,
        wallet_public_key=intent.wallet_public_key,
        intent_id=intent.intent_id,
        consumer=consumer,
        now=NOW_MS // 1000,
    )
    result = pipeline.prepare(
        intent=intent,
        plan=make_plan(intent),
        risk=make_risk(intent),
        simulation=make_simulation(intent),
        authorization=authorization,
    )
    assert result.state is ExecutionState.FAILED
    assert result.reason_code == "operator_authorization_rejected"
    assert coordinator.get(intent.intent_id)["state"] == "FAILED"


def test_coordinator_is_idempotent_and_rejects_scope_reuse(tmp_path):
    store = SubmissionCoordinator(tmp_path / "execution.sqlite3")
    values = {
        "intent_id": "intent-0002",
        "chain": "solana-devnet",
        "wallet_public_key": WALLET,
        "policy_hash": "a" * 64,
        "intent_hash": "b" * 64,
        "created_at_ms": NOW_MS,
    }
    first = store.create(**values)
    duplicate = store.create(**values)
    assert first["state"] == duplicate["state"] == "CREATED"
    with pytest.raises(IdempotencyConflict):
        store.create(**{**values, "intent_hash": "c" * 64})


def test_coordinator_blocks_sign_submit_and_confirm_transitions(tmp_path):
    store = SubmissionCoordinator(tmp_path / "execution.sqlite3")
    store.create(
        intent_id="intent-0003", chain="solana-devnet",
        wallet_public_key=WALLET, policy_hash="a" * 64,
        intent_hash="b" * 64, created_at_ms=NOW_MS,
    )
    store.transition(
        "intent-0003", expected_state="CREATED", next_state="VALIDATED",
        occurred_at_ms=NOW_MS + 1, reason_code="validated",
    )
    with pytest.raises(ExecutionDisabled):
        store.transition(
            "intent-0003", expected_state="VALIDATED", next_state="SIGNED",
            occurred_at_ms=NOW_MS + 2, reason_code="signing_disabled",
        )
    with pytest.raises(ExecutionDisabled):
        store.transition(
            "intent-0003", expected_state="VALIDATED", next_state="SUBMITTED",
            occurred_at_ms=NOW_MS + 2, reason_code="submission_disabled",
        )


def test_coordinator_concurrent_duplicate_intents_are_idempotent(tmp_path):
    store = SubmissionCoordinator(tmp_path / "concurrent.sqlite3")
    values = {
        "intent_id": "intent-concurrent",
        "chain": "solana-devnet",
        "wallet_public_key": WALLET,
        "policy_hash": "a" * 64,
        "intent_hash": "b" * 64,
        "created_at_ms": NOW_MS,
    }
    with ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(executor.map(lambda _: store.create(**values), range(16)))
    assert {row["state"] for row in rows} == {"CREATED"}
    assert len(ExecutionAuditStore(store.database_path).events("intent-concurrent")) == 1


def test_execution_audit_is_append_only_and_has_no_secret_columns(tmp_path):
    store = SubmissionCoordinator(tmp_path / "audit.sqlite3")
    store.create(
        intent_id="intent-audit", chain="solana-devnet",
        wallet_public_key=WALLET, policy_hash="a" * 64,
        intent_hash="b" * 64, created_at_ms=NOW_MS,
    )
    audit = ExecutionAuditStore(store.database_path)
    events = audit.events("intent-audit")
    assert len(events) == 1
    assert events[0]["event_type"] == "intent_created"
    with sqlite3.connect(store.database_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(execution_audit_events)")}
        assert not any("secret" in name or "private" in name or "seed" in name for name in columns)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE execution_audit_events SET state='SIGNED'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM execution_audit_events")



@pytest.mark.parametrize(
    "case,reason",
    [
        ("paper_policy", "live_execution_disabled"),
        ("emergency_active", "emergency_stop_active"),
        ("emergency_unavailable", "emergency_stop_unavailable"),
        ("unknown_program", "program_not_allowlisted"),
        ("stale_market", "market_evidence_stale"),
        ("failed_simulation", "simulation_failed_or_missing"),
        ("missing_authorization", "operator_authorization_missing"),
    ],
)
def test_every_pipeline_rejection_stops_before_fake_signer(tmp_path, case, reason):
    intent = make_intent()
    policy = ExecutionPolicy.from_environment({}) if case == "paper_policy" else live_policy()
    pipeline, coordinator, stop, _consumer = make_pipeline(
        tmp_path / case, policy=policy
    )
    plan = make_plan(intent)
    risk = make_risk(intent)
    simulation = make_simulation(intent, plan)
    authorization = make_authorization(intent, policy) if policy.mode is ExecutionMode.LIVE else None
    if case == "emergency_active":
        stop.set_active(True, reason_code="manual_stop", changed_at_ms=NOW_MS)
    elif case == "emergency_unavailable":
        stop.database_path = str(tmp_path / "unavailable-parent" / "stop.sqlite3")
    elif case == "unknown_program":
        instructions = (replace(plan.instructions[0], program_id="unknown"), plan.instructions[1])
        plan = replace(plan, instructions=instructions, message_hash="0" * 64)
        plan = replace(plan, message_hash=plan.computed_message_hash())
    elif case == "stale_market":
        risk = make_risk(intent, market_observed_at_ms=NOW_MS - 10_000)
    elif case == "failed_simulation":
        simulation = make_simulation(intent, plan, succeeded=False)
    elif case == "missing_authorization":
        authorization = None

    fake_signer = FakeSigner()
    result = pipeline.prepare(
        intent=intent,
        plan=plan,
        risk=risk,
        simulation=simulation,
        authorization=authorization,
    )

    assert result.state is ExecutionState.FAILED
    assert result.reason_code == reason
    assert coordinator.get(intent.intent_id)["state"] == "FAILED"
    assert fake_signer.calls == 0


def test_audit_store_cannot_record_unavailable_signed_states(tmp_path):
    audit = ExecutionAuditStore(tmp_path / "audit.sqlite3")
    with pytest.raises(ExecutionDisabled):
        audit.append(
            intent_id="intent-signed",
            event_type="state_transition",
            state=StoredExecutionState.SIGNED,
            occurred_at_ms=NOW_MS,
            reason_code="signed",
        )


def test_failed_intent_is_terminal_and_cannot_be_reopened(tmp_path):
    store = SubmissionCoordinator(tmp_path / "terminal.sqlite3")
    store.create(
        intent_id="intent-terminal", chain="solana-devnet",
        wallet_public_key=WALLET, policy_hash="a" * 64,
        intent_hash="b" * 64, created_at_ms=NOW_MS,
    )
    store.fail("intent-terminal", occurred_at_ms=NOW_MS + 1, reason_code="gate_failed")
    with pytest.raises(ValueError, match="not allowed"):
        store.transition(
            "intent-terminal", expected_state="FAILED", next_state="VALIDATED",
            occurred_at_ms=NOW_MS + 2,
        )


def test_safety_modules_have_no_wallet_signing_or_rpc_submission_adapter():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    modules = (
        "veltrix/execution_safety.py",
        "veltrix/execution_policy.py",
        "veltrix/authorization_store.py",
        "veltrix/emergency_stop.py",
        "veltrix/submission_coordinator.py",
    )
    forbidden = (
        "solders.keypair",
        "solana.rpc",
        "sign_transaction(",
        "send_transaction(",
        "keypair.from_seed",
        "keypair.from_bytes",
    )
    for relative in modules:
        source = (root / relative).read_text(encoding="utf-8").lower()
        assert not any(marker in source for marker in forbidden), relative
        assert not re.search(
            r"\\b(private_key|seed_phrase|mnemonic|secret_key)\\s*=",
            source,
        ), relative



def test_authorized_intent_is_idempotent_and_not_reauthorized_after_restart(tmp_path):
    intent = make_intent()
    policy = live_policy()
    pipeline, coordinator, stop, consumer = make_pipeline(tmp_path, policy=policy)
    authorization = make_authorization(intent, policy)
    args = {
        "intent": intent,
        "plan": make_plan(intent),
        "risk": make_risk(intent),
        "simulation": make_simulation(intent),
        "authorization": authorization,
    }
    first = pipeline.prepare(**args)
    restarted_coordinator = SubmissionCoordinator(coordinator.database_path)
    restarted_pipeline = ExecutionSafetyPipeline(
        policy=policy,
        coordinator=restarted_coordinator,
        emergency_stop=SQLiteEmergencyStop(stop.database_path),
        authorization_consumer=SQLiteAuthorizationConsumer(consumer.database_path),
        authorization_signing_key=KEY,
        clock_ms=lambda: NOW_MS,
    )
    second = restarted_pipeline.prepare(**args)

    assert first.state is ExecutionState.AUTHORIZED
    assert second.state is ExecutionState.AUTHORIZED
    assert second.duplicate is True
    assert coordinator.get(intent.intent_id)["state"] == "AUTHORIZED"
    assert len(ExecutionAuditStore(coordinator.database_path).events(intent.intent_id)) == 4


def test_audit_api_rejects_unapproved_secret_metadata(tmp_path):
    audit = ExecutionAuditStore(tmp_path / "audit.sqlite3")
    with pytest.raises(TypeError):
        audit.append(
            intent_id="intent-secret",
            event_type="preflight",
            state=StoredExecutionState.CREATED,
            occurred_at_ms=NOW_MS,
            reason_code="preflight",
            private_key="must_not_be_persisted",
        )
    assert audit.events("intent-secret") == []


def test_transaction_validator_rejects_duplicate_instruction_accounts():
    intent = make_intent()
    plan = make_plan(intent)
    instructions = (
        replace(plan.instructions[0], accounts=("wallet-in", "wallet-in", "pool-vault")),
        plan.instructions[1],
    )
    tampered = replace(plan, instructions=instructions, message_hash="0" * 64)
    tampered = replace(tampered, message_hash=tampered.computed_message_hash())
    with pytest.raises(ExecutionGateError, match="duplicate_instruction_account"):
        TransactionValidator.validate(intent, tampered, live_policy(), now_ms=NOW_MS)
