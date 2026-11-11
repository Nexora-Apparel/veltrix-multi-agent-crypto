import hashlib
from dataclasses import replace

import pytest

from veltrix.raydium_clmm_simulation_state import (
    RaydiumClmmAccountCommitment,
    RaydiumClmmPostStateValidation,
    RaydiumClmmSimulationStateBinding,
    RaydiumClmmSimulationStateError,
    validate_raydium_clmm_post_state_commitments,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_POOL_STATE_DISCRIMINATOR,
    RAYDIUM_POOL_STATE_LENGTH,
)
from veltrix.simulation_gate import SimulatedAccountState


def _fixture():
    address = "PoolStateFixture111111111111111111111111111"
    pre = bytearray(RAYDIUM_POOL_STATE_LENGTH)
    pre[:8] = RAYDIUM_POOL_STATE_DISCRIMINATOR
    post = bytearray(pre)
    post[237] = 1
    pre, post = bytes(pre), bytes(post)
    pre_hash = hashlib.sha256(pre).hexdigest()
    post_hash = hashlib.sha256(post).hexdigest()
    commitment = RaydiumClmmAccountCommitment(
        address=address,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        data_sha256=pre_hash,
        data_length=RAYDIUM_POOL_STATE_LENGTH,
        lamports=1,
        role="pool_state",
    )
    state = SimulatedAccountState(
        address=address,
        pre_owner=RAYDIUM_CLMM_PROGRAM_ID,
        post_owner=RAYDIUM_CLMM_PROGRAM_ID,
        pre_lamports=1,
        post_lamports=1,
        pre_executable=False,
        post_executable=False,
        pre_data=pre,
        post_data=post,
        pre_data_sha256=pre_hash,
        post_data_sha256=post_hash,
    )
    binding = RaydiumClmmSimulationStateBinding(
        passed=True,
        message_sha256="a" * 64,
        provider_id="fixture-rpc",
        simulation_response_id="fixture-response",
        simulation_context_slot=100,
        bound_account_addresses=(address,),
        pre_state_sha256=((address, pre_hash),),
        post_state_sha256=((address, post_hash),),
        account_roles=((address, "pool_state"),),
        execution_ready=False,
    )
    return binding, commitment, state


def test_post_state_validator_accepts_exact_bound_account_set_and_hashes():
    binding, commitment, state = _fixture()
    result = validate_raydium_clmm_post_state_commitments(
        binding, commitments=(commitment,), account_states=(state,)
    )
    assert isinstance(result, RaydiumClmmPostStateValidation)
    assert result.passed is True
    assert result.account_count == 1
    assert result.account_addresses == (commitment.address,)
    assert result.execution_ready is False
    assert len(result.post_state_sha256) == 64


def test_post_state_validator_rejects_missing_or_unexpected_account():
    binding, commitment, state = _fixture()
    with pytest.raises(RaydiumClmmSimulationStateError, match="account set"):
        validate_raydium_clmm_post_state_commitments(
            binding, commitments=(commitment,), account_states=()
        )
    extra = replace(state, address="UnexpectedAccount111111111111111111111111")
    with pytest.raises(RaydiumClmmSimulationStateError, match="account set"):
        validate_raydium_clmm_post_state_commitments(
            binding, commitments=(commitment,), account_states=(state, extra)
        )


def test_post_state_validator_rejects_wrong_anchor_discriminator():
    binding, commitment, state = _fixture()
    bad = bytearray(state.post_data)
    bad[:8] = bytes(8)
    bad = bytes(bad)
    bad_state = replace(
        state,
        post_data=bad,
        post_data_sha256=hashlib.sha256(bad).hexdigest(),
    )
    bad_binding = replace(
        binding,
        post_state_sha256=((state.address, bad_state.post_data_sha256),),
    )
    with pytest.raises(RaydiumClmmSimulationStateError, match="discriminator"):
        validate_raydium_clmm_post_state_commitments(
            bad_binding, commitments=(commitment,), account_states=(bad_state,)
        )


def test_post_state_validator_rejects_owner_and_pre_hash_mismatch():
    binding, commitment, state = _fixture()
    wrong_owner = replace(state, pre_owner="WrongProgram111111111111111111111111111", post_owner="WrongProgram111111111111111111111111111")
    with pytest.raises(RaydiumClmmSimulationStateError, match="owner"):
        validate_raydium_clmm_post_state_commitments(
            binding, commitments=(commitment,), account_states=(wrong_owner,)
        )
    wrong_commitment = replace(commitment, data_sha256="0" * 64)
    with pytest.raises(RaydiumClmmSimulationStateError, match="pre-state hash"):
        validate_raydium_clmm_post_state_commitments(
            binding, commitments=(wrong_commitment,), account_states=(state,)
        )


def test_post_state_validator_rejects_hash_or_role_drift():
    binding, commitment, state = _fixture()
    wrong_hash_binding = replace(
        binding, post_state_sha256=((state.address, "f" * 64),)
    )
    with pytest.raises(RaydiumClmmSimulationStateError, match="post-state hashes"):
        validate_raydium_clmm_post_state_commitments(
            wrong_hash_binding, commitments=(commitment,), account_states=(state,)
        )
    wrong_role = replace(commitment, role="unknown_role")
    with pytest.raises(RaydiumClmmSimulationStateError, match="roles"):
        validate_raydium_clmm_post_state_commitments(
            binding, commitments=(wrong_role,), account_states=(state,)
        )


def test_post_state_validator_rejects_pre_hash_binding_and_lamport_drift():
    binding, commitment, state = _fixture()
    bad_pre_binding = replace(
        binding, pre_state_sha256=((state.address, "0" * 64),)
    )
    with pytest.raises(RaydiumClmmSimulationStateError, match="pre-state hash"):
        validate_raydium_clmm_post_state_commitments(
            bad_pre_binding, commitments=(commitment,), account_states=(state,)
        )
    wrong_lamports = replace(commitment, lamports=2)
    with pytest.raises(RaydiumClmmSimulationStateError, match="pre-state hash"):
        validate_raydium_clmm_post_state_commitments(
            binding, commitments=(wrong_lamports,), account_states=(state,)
        )


def test_post_state_validator_rejects_duplicate_bound_account_addresses():
    binding, commitment, state = _fixture()
    duplicate_binding = replace(
        binding,
        bound_account_addresses=(state.address, state.address),
    )
    with pytest.raises(RaydiumClmmSimulationStateError, match="duplicate"):
        validate_raydium_clmm_post_state_commitments(
            duplicate_binding, commitments=(commitment,), account_states=(state,)
        )
