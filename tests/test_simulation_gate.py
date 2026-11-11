"""Adversarial tests for the fail-closed Solana SimulationGate."""
import dataclasses

import pytest

from veltrix.simulation_gate import (
    ExpectedTokenDelta,
    SimulatedTokenBalance,
    SimulatedAccountState,
    SimulationEvidence,
    SimulationGateError,
    SimulationGatePolicy,
    evaluate_simulation,
)
from veltrix.transaction_validator import (
    InstructionExpectation,
    TransactionIntent,
    TransactionValidationResult,
)


def valid_fixture():
    digest = "a" * 64
    blockhash = "approved-blockhash"
    intent = TransactionIntent(
        fee_payer="fee-payer",
        required_signers=("fee-payer",),
        recent_blockhash=blockhash,
        instructions=(
            # Structural validation is represented by its immutable result.
            # Instruction expectations are supplied by the preceding validator.
            InstructionExpectation("approved-program", (), b"approved"),
        ),
    )
    validation = TransactionValidationResult(
        message_sha256=digest,
        message_size=128,
        fee_payer="fee-payer",
        required_signers=("fee-payer",),
        program_ids=("approved-program",),
        instruction_count=1,
    )
    expected = (
        ExpectedTokenDelta("token-account-1", "mint-1", "owner-1", -250),
        ExpectedTokenDelta("token-account-2", "mint-1", "owner-2", 240),
    )
    evidence = SimulationEvidence(
        message_sha256=digest,
        recent_blockhash=blockhash,
        cluster="mainnet-beta",
        genesis_hash="mainnet-genesis",
        provider_id="rpc-primary",
        response_id="response-001",
        observed_at_epoch_ms=1_000_000,
        context_slot=500,
        current_block_height=900,
        last_valid_block_height=950,
        simulation_error=None,
        units_consumed=120_000,
        fee_lamports=5_000,
        token_balances=(
            SimulatedTokenBalance("token-account-1", "mint-1", "owner-1", 1_000, 750),
            SimulatedTokenBalance("token-account-2", "mint-1", "owner-2", 10, 250),
        ),
    )
    policy = SimulationGatePolicy(
        expected_cluster="mainnet-beta",
        expected_genesis_hash="mainnet-genesis",
        allowed_provider_ids=("rpc-primary",),
        max_evidence_age_ms=10_000,
        max_future_skew_ms=1_000,
        max_compute_units=200_000,
        max_fee_lamports=10_000,
    )
    return intent, validation, expected, evidence, policy


def evaluate(evidence=None, *, intent=None, validation=None, expected=None, policy=None):
    base = valid_fixture()
    intent = intent or base[0]
    validation = validation or base[1]
    expected = expected or base[2]
    evidence = evidence or base[3]
    policy = policy or base[4]
    return evaluate_simulation(
        evidence,
        intent=intent,
        validation=validation,
        expected_token_deltas=expected,
        policy=policy,
        now_epoch_ms=1_005_000,
        minimum_context_slot=490,
    )


def test_accepts_fresh_successful_simulation_with_exact_token_deltas():
    result = evaluate()
    assert result.approved is True
    assert result.message_sha256 == "a" * 64
    assert result.verified_token_account_count == 2
    assert result.units_consumed == 120_000


def test_accepts_bounded_output_delta_above_minimum():
    intent, validation, expected, evidence, policy = valid_fixture()
    bounded = (
        expected[0],
        ExpectedTokenDelta("token-account-2", "mint-1", "owner-2", None, minimum_delta_raw=240),
    )
    balances = list(evidence.token_balances)
    balances[1] = dataclasses.replace(balances[1], post_amount_raw=275)
    result = evaluate(
        dataclasses.replace(evidence, token_balances=tuple(balances)),
        intent=intent,
        validation=validation,
        expected=bounded,
        policy=policy,
    )
    assert result.approved is True
    assert result.verified_token_account_count == 2


def test_rejects_bounded_output_delta_below_minimum():
    intent, validation, expected, evidence, policy = valid_fixture()
    bounded = (
        expected[0],
        ExpectedTokenDelta("token-account-2", "mint-1", "owner-2", None, minimum_delta_raw=240),
    )
    balances = list(evidence.token_balances)
    balances[1] = dataclasses.replace(balances[1], post_amount_raw=249)
    evidence = dataclasses.replace(evidence, token_balances=tuple(balances))
    with pytest.raises(SimulationGateError, match="below the approved minimum"):
        evaluate(
            evidence,
            intent=intent,
            validation=validation,
            expected=bounded,
            policy=policy,
        )


def test_rejects_invalid_exact_and_bounded_delta_modes():
    with pytest.raises(SimulationGateError, match="either exact or bounded"):
        ExpectedTokenDelta("account", "mint", "owner", 10, minimum_delta_raw=5)
    with pytest.raises(SimulationGateError, match="either exact or bounded"):
        ExpectedTokenDelta("account", "mint", "owner", None)
    with pytest.raises(SimulationGateError, match="exceeds maximum"):
        ExpectedTokenDelta("account", "mint", "owner", None, minimum_delta_raw=11, maximum_delta_raw=10)


def test_rejects_missing_simulation_evidence():
    intent, validation, expected, _, policy = valid_fixture()
    with pytest.raises(SimulationGateError, match="evidence is required"):
        evaluate_simulation(
            None,
            intent=intent,
            validation=validation,
            expected_token_deltas=expected,
            policy=policy,
            now_epoch_ms=1_005_000,
            minimum_context_slot=490,
        )


def test_rejects_message_digest_mismatch():
    _, _, _, evidence, _ = valid_fixture()
    with pytest.raises(SimulationGateError, match="digest"):
        evaluate(dataclasses.replace(evidence, message_sha256="b" * 64))


def test_rejects_blockhash_mismatch():
    _, _, _, evidence, _ = valid_fixture()
    with pytest.raises(SimulationGateError, match="blockhash"):
        evaluate(dataclasses.replace(evidence, recent_blockhash="different"))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("cluster", "devnet", "cluster identity"),
        ("genesis_hash", "wrong-genesis", "cluster identity"),
        ("provider_id", "unapproved-rpc", "provider"),
        ("simulation_error", "InstructionError", "execution error"),
        ("observed_at_epoch_ms", 900_000, "stale"),
        ("observed_at_epoch_ms", 1_010_001, "future"),
        ("context_slot", 489, "context slot"),
        ("current_block_height", 951, "expired"),
        ("units_consumed", 200_001, "compute"),
        ("fee_lamports", 10_001, "fee"),
    ],
)
def test_rejects_unsafe_evidence_fields(field, value, message):
    _, _, _, evidence, _ = valid_fixture()
    with pytest.raises(SimulationGateError, match=message):
        evaluate(dataclasses.replace(evidence, **{field: value}))


def test_rejects_missing_token_account_delta():
    _, _, _, evidence, _ = valid_fixture()
    changed = dataclasses.replace(evidence, token_balances=evidence.token_balances[:1])
    with pytest.raises(SimulationGateError, match="set does not exactly match"):
        evaluate(changed)


def test_rejects_unexpected_token_account_delta():
    _, _, _, evidence, _ = valid_fixture()
    extra = SimulatedTokenBalance("unexpected", "mint-1", "owner-3", 0, 1)
    changed = dataclasses.replace(evidence, token_balances=evidence.token_balances + (extra,))
    with pytest.raises(SimulationGateError, match="set does not exactly match"):
        evaluate(changed)


def test_rejects_wrong_token_amount_delta():
    _, _, _, evidence, _ = valid_fixture()
    first = dataclasses.replace(evidence.token_balances[0], post_amount_raw=749)
    changed = dataclasses.replace(evidence, token_balances=(first, evidence.token_balances[1]))
    with pytest.raises(SimulationGateError, match="balance delta"):
        evaluate(changed)


def test_rejects_wrong_mint_or_owner_even_when_amount_matches():
    _, _, _, evidence, _ = valid_fixture()
    first = dataclasses.replace(evidence.token_balances[0], mint="attacker-mint")
    changed = dataclasses.replace(evidence, token_balances=(first, evidence.token_balances[1]))
    with pytest.raises(SimulationGateError, match="set does not exactly match"):
        evaluate(changed)


def test_rejects_duplicate_simulated_balance_records():
    _, _, _, evidence, _ = valid_fixture()
    with pytest.raises(SimulationGateError, match="duplicate"):
        dataclasses.replace(evidence, token_balances=evidence.token_balances + (evidence.token_balances[0],))


def test_rejects_duplicate_expected_balance_records():
    intent, validation, expected, evidence, policy = valid_fixture()
    with pytest.raises(SimulationGateError, match="expected token deltas contain duplicate"):
        evaluate(
            evidence,
            intent=intent,
            validation=validation,
            expected=expected + (expected[0],),
            policy=policy,
        )


def test_rejects_non_approved_genesis_hash_policy_mismatch():
    _, _, _, _, policy = valid_fixture()
    wrong = dataclasses.replace(policy, expected_genesis_hash="other")
    with pytest.raises(SimulationGateError, match="cluster identity"):
        evaluate(policy=wrong)


def test_rejects_invalid_policy_with_no_rpc_allowlist():
    with pytest.raises(SimulationGateError, match="non-empty"):
        SimulationGatePolicy("mainnet-beta", "genesis", ())


def test_rejects_invalid_balance_values():
    with pytest.raises(SimulationGateError, match="non-negative integer"):
        SimulatedTokenBalance("account", "mint", "owner", -1, 0)


def test_simulation_evidence_accepts_hash_bound_raw_pre_post_account_state():
    _, _, _, evidence, _ = valid_fixture()
    pre_data = b"verified-pool-pre-state"
    post_data = b"verified-pool-post-state"
    state = SimulatedAccountState(
        address="observed-pool",
        pre_owner="raydium-program",
        post_owner="raydium-program",
        pre_lamports=10,
        post_lamports=10,
        pre_executable=False,
        post_executable=False,
        pre_data=pre_data,
        post_data=post_data,
        pre_data_sha256=__import__("hashlib").sha256(pre_data).hexdigest(),
        post_data_sha256=__import__("hashlib").sha256(post_data).hexdigest(),
    )
    changed = dataclasses.replace(evidence, account_states=(state,))
    assert changed.account_states[0].pre_data == pre_data
    assert changed.account_states[0].post_data == post_data


def test_simulated_account_state_rejects_tampered_raw_data_digest():
    pre_data = b"before"
    post_data = b"after!"
    with pytest.raises(SimulationGateError, match="does not match"):
        SimulatedAccountState(
            address="observed-pool",
            pre_owner="raydium-program",
            post_owner="raydium-program",
            pre_lamports=1,
            post_lamports=1,
            pre_executable=False,
            post_executable=False,
            pre_data=pre_data,
            post_data=post_data,
            pre_data_sha256="0" * 64,
            post_data_sha256=__import__("hashlib").sha256(post_data).hexdigest(),
        )


def test_simulation_evidence_rejects_raw_state_duplicate_of_token_account():
    _, _, _, evidence, _ = valid_fixture()
    raw = b"token account raw bytes"
    state = SimulatedAccountState(
        address=evidence.token_balances[0].token_account,
        pre_owner="token-program",
        post_owner="token-program",
        pre_lamports=1,
        post_lamports=1,
        pre_executable=False,
        post_executable=False,
        pre_data=raw,
        post_data=raw,
        pre_data_sha256=__import__("hashlib").sha256(raw).hexdigest(),
        post_data_sha256=__import__("hashlib").sha256(raw).hexdigest(),
    )
    with pytest.raises(SimulationGateError, match="must not duplicate tracked token"):
        dataclasses.replace(evidence, account_states=(state,))


def test_simulation_gate_binds_raw_account_state_to_validated_message_keys():
    intent, validation, _, evidence, _ = valid_fixture()
    raw = b"observed account pre/post"
    state = SimulatedAccountState(
        address="observed-account",
        pre_owner="owner-program",
        post_owner="owner-program",
        pre_lamports=1,
        post_lamports=1,
        pre_executable=False,
        post_executable=False,
        pre_data=raw,
        post_data=raw,
        pre_data_sha256=__import__("hashlib").sha256(raw).hexdigest(),
        post_data_sha256=__import__("hashlib").sha256(raw).hexdigest(),
    )
    evidence = dataclasses.replace(evidence, account_states=(state,))
    validated = dataclasses.replace(validation, account_keys=("observed-account",))
    assert evaluate(evidence, intent=intent, validation=validated).approved is True
    with pytest.raises(SimulationGateError, match="outside the validated transaction"):
        evaluate(evidence, intent=intent, validation=validation)
