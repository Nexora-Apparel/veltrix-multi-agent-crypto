"""Bind Raydium pre/post simulation account bytes to decoded account evidence.

This is a provenance and state-capture gate only. It does not replay the
Raydium swap loop, prove consumed ticks, calculate price impact, or authorize
signing/submission.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

from veltrix.dex_pool_evidence import DexPoolEvidenceSnapshot
from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_swap_effects import CLASSIC_TOKEN_PROGRAM_ID
from veltrix.raydium_clmm_aux_accounts import (
    AMM_CONFIG_DATA_LENGTH,
    OBSERVATION_STATE_DATA_LENGTH,
    RaydiumClmmAuxAccountSnapshot,
)
from veltrix.raydium_clmm_cpi import RAYDIUM_CLMM_SOURCE_COMMIT, RaydiumClmmCpiResult
from veltrix.raydium_clmm_bitmap_traversal import (
    RaydiumClmmBitmapTraversalError,
    verify_raydium_clmm_bitmap_traversal,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_POOL_STATE_LENGTH,
    RaydiumClmmPoolRouteCheck,
)
from veltrix.raydium_clmm_tick_arrays import (
    RAYDIUM_TICK_ARRAY_DATA_LENGTH,
    RaydiumClmmTickArraySnapshot,
)
from veltrix.raydium_clmm_vaults import (
    CLASSIC_SPL_TOKEN_ACCOUNT_LENGTH,
    RaydiumClmmVaultSnapshot,
)
from veltrix.simulation_gate import SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult


class RaydiumClmmSimulationStateError(ValueError):
    """Raised when simulated pre/post account state cannot be bound to Raydium evidence."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmAccountCommitment:
    address: str
    owner_program_id: str
    data_sha256: str
    data_length: int
    lamports: int | None = None
    role: str = ""

    def __post_init__(self) -> None:
        for field in ("address", "owner_program_id", "role"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value or value.strip() != value:
                raise RaydiumClmmSimulationStateError(f"{field} must be a canonical non-empty string")
        if not isinstance(self.data_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.data_sha256):
            raise RaydiumClmmSimulationStateError("account commitment SHA-256 must be lowercase hexadecimal")
        if isinstance(self.data_length, bool) or not isinstance(self.data_length, int) or self.data_length <= 0:
            raise RaydiumClmmSimulationStateError("account commitment data length must be positive")
        if self.lamports is not None and (
            isinstance(self.lamports, bool) or not isinstance(self.lamports, int) or self.lamports <= 0
        ):
            raise RaydiumClmmSimulationStateError("known account lamports must be positive")


@dataclass(frozen=True, slots=True)
class RaydiumClmmSimulationStateBinding:
    passed: bool
    message_sha256: str
    provider_id: str
    simulation_response_id: str
    simulation_context_slot: int
    bound_account_addresses: tuple[str, ...]
    pre_state_sha256: tuple[tuple[str, str], ...]
    post_state_sha256: tuple[tuple[str, str], ...]
    account_roles: tuple[tuple[str, str], ...]
    execution_ready: bool = False


def build_raydium_clmm_account_commitments(
    cpi: RaydiumClmmCpiResult,
    *,
    pool_evidence: DexPoolEvidenceSnapshot,
    vault_snapshot: RaydiumClmmVaultSnapshot,
    tick_array_snapshot: RaydiumClmmTickArraySnapshot,
    aux_snapshot: RaydiumClmmAuxAccountSnapshot,
) -> tuple[RaydiumClmmAccountCommitment, ...]:
    """Build the exact expected account role/hash set from independently decoded evidence."""
    if not isinstance(cpi, RaydiumClmmCpiResult) or cpi.passed is not True:
        raise RaydiumClmmSimulationStateError("successful Raydium CLMM CPI evidence is required")
    for value, expected_type, label in (
        (pool_evidence, DexPoolEvidenceSnapshot, "pool"),
        (vault_snapshot, RaydiumClmmVaultSnapshot, "vault"),
        (tick_array_snapshot, RaydiumClmmTickArraySnapshot, "tick-array"),
        (aux_snapshot, RaydiumClmmAuxAccountSnapshot, "auxiliary"),
    ):
        if not isinstance(value, expected_type):
            raise RaydiumClmmSimulationStateError(f"verified {label} snapshot is required")

    pool_accounts = [item for item in pool_evidence.accounts if item.amm_key == cpi.pool_state]
    if len(pool_accounts) != 1:
        raise RaydiumClmmSimulationStateError("exactly one owner-verified PoolState account must match the CPI pool")
    pool_account = pool_accounts[0]
    if (
        pool_account.owner_program_id != RAYDIUM_CLMM_PROGRAM_ID
        or pool_account.expected_program_id != RAYDIUM_CLMM_PROGRAM_ID
        or pool_account.executable is not False
        or pool_account.data_length != RAYDIUM_POOL_STATE_LENGTH
    ):
        raise RaydiumClmmSimulationStateError("PoolState owner or executable metadata is invalid")
    commitments = [
        RaydiumClmmAccountCommitment(
            address=pool_account.amm_key,
            owner_program_id=pool_account.owner_program_id,
            data_sha256=pool_account.data_sha256,
            data_length=pool_account.data_length,
            lamports=pool_account.lamports,
            role="pool_state",
        )
    ]

    if (
        len(vault_snapshot.vaults) != 2
        or {item.address for item in vault_snapshot.vaults} != {cpi.input_vault, cpi.output_vault}
        or any(item.pool_address != cpi.pool_state for item in vault_snapshot.vaults)
    ):
        raise RaydiumClmmSimulationStateError("decoded vaults must exactly match the CPI input/output vaults and pool")
    for vault in vault_snapshot.vaults:
        commitments.append(RaydiumClmmAccountCommitment(
            address=vault.address,
            owner_program_id=vault.token_program_id,
            data_sha256=vault.data_sha256,
            data_length=CLASSIC_SPL_TOKEN_ACCOUNT_LENGTH,
            lamports=vault.lamports,
            role="pool_vault",
        ))

    if not tick_array_snapshot.tick_arrays:
        raise RaydiumClmmSimulationStateError("decoded CPI tick-array evidence is required")
    for tick_array in tick_array_snapshot.tick_arrays:
        commitments.append(RaydiumClmmAccountCommitment(
            address=tick_array.address,
            owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
            data_sha256=tick_array.data_sha256,
            data_length=RAYDIUM_TICK_ARRAY_DATA_LENGTH,
            role="tick_array",
        ))
    if tick_array_snapshot.bitmap_extension_address is not None:
        extension = tick_array_snapshot.bitmap_extension
        if extension is None or extension.address != tick_array_snapshot.bitmap_extension_address:
            raise RaydiumClmmSimulationStateError("decoded bitmap extension is missing or has a different address")
        commitments.append(RaydiumClmmAccountCommitment(
            address=extension.address,
            owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
            data_sha256=extension.data_sha256,
            data_length=1832,
            role="tick_array_bitmap_extension",
        ))

    if aux_snapshot.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT:
        raise RaydiumClmmSimulationStateError("auxiliary account source revision is not pinned")
    configs = [item for item in aux_snapshot.amm_configs if item.address == cpi.amm_config]
    observations = [item for item in aux_snapshot.observations if item.address == cpi.observation_state]
    if len(configs) != 1 or len(observations) != 1:
        raise RaydiumClmmSimulationStateError("CPI AMM config and observation accounts must each match one decoded auxiliary record")
    commitments.append(RaydiumClmmAccountCommitment(
        address=configs[0].address,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        data_sha256=configs[0].data_sha256,
        data_length=AMM_CONFIG_DATA_LENGTH,
        role="amm_config",
    ))
    commitments.append(RaydiumClmmAccountCommitment(
        address=observations[0].address,
        owner_program_id=RAYDIUM_CLMM_PROGRAM_ID,
        data_sha256=observations[0].data_sha256,
        data_length=OBSERVATION_STATE_DATA_LENGTH,
        role="observation_state",
    ))

    addresses = tuple(item.address for item in commitments)
    if len(addresses) != len(set(addresses)):
        raise RaydiumClmmSimulationStateError("Raydium evidence reuses an account address across incompatible roles")
    if any(item.owner_program_id == "" for item in commitments):
        raise RaydiumClmmSimulationStateError("Raydium account commitment has no owner program")
    if any(item.role == "pool_vault" and item.owner_program_id != CLASSIC_TOKEN_PROGRAM_ID for item in commitments):
        raise RaydiumClmmSimulationStateError("pool vault commitment is not owned by the classic SPL Token program")
    return tuple(commitments)


def evaluate_raydium_clmm_simulation_state_binding(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    simulation: SimulationEvidence,
    simulation_gate: SimulationGateResult,
    pool_check: RaydiumClmmPoolRouteCheck,
    cpi: RaydiumClmmCpiResult,
    pool_evidence: DexPoolEvidenceSnapshot,
    vault_snapshot: RaydiumClmmVaultSnapshot,
    tick_array_snapshot: RaydiumClmmTickArraySnapshot,
    aux_snapshot: RaydiumClmmAuxAccountSnapshot,
) -> RaydiumClmmSimulationStateBinding:
    """Bind every Raydium account's decoded pre-state hash to the exact simulation capture."""
    if not isinstance(order, JupiterOrderTransaction):
        raise RaydiumClmmSimulationStateError("Jupiter order is required")
    if not isinstance(cpi, RaydiumClmmCpiResult) or cpi.passed is not True:
        raise RaydiumClmmSimulationStateError("successful Raydium CPI evidence is required")
    if not isinstance(pool_evidence, DexPoolEvidenceSnapshot):
        raise RaydiumClmmSimulationStateError("verified PoolState account evidence is required")
    if not isinstance(vault_snapshot, RaydiumClmmVaultSnapshot):
        raise RaydiumClmmSimulationStateError("verified vault snapshot is required")
    if not isinstance(tick_array_snapshot, RaydiumClmmTickArraySnapshot):
        raise RaydiumClmmSimulationStateError("verified tick-array snapshot is required")
    if not isinstance(aux_snapshot, RaydiumClmmAuxAccountSnapshot):
        raise RaydiumClmmSimulationStateError("verified auxiliary account snapshot is required")
    if not isinstance(validation, TransactionValidationResult):
        raise RaydiumClmmSimulationStateError("validated transaction result is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumClmmSimulationStateError("simulation evidence is required")
    if not isinstance(simulation_gate, SimulationGateResult) or simulation_gate.approved is not True:
        raise RaydiumClmmSimulationStateError("successful SimulationGate approval is required")
    if not isinstance(pool_check, RaydiumClmmPoolRouteCheck) or pool_check.passed is not True:
        raise RaydiumClmmSimulationStateError("successful PoolState route check is required")

    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if (
        validation.message_sha256 != digest
        or simulation.message_sha256 != digest
        or simulation_gate.message_sha256 != digest
        or simulation_gate.provider_id != simulation.provider_id
        or simulation_gate.response_id != simulation.response_id
        or simulation_gate.context_slot != simulation.context_slot
        or simulation.provider_id != pool_evidence.provider_id
        or simulation.provider_id != vault_snapshot.provider_id
        or simulation.provider_id != tick_array_snapshot.provider_id
        or simulation.provider_id != aux_snapshot.provider_id
        or simulation.cluster != pool_evidence.cluster
        or simulation.cluster != vault_snapshot.cluster
        or simulation.cluster != tick_array_snapshot.cluster
        or simulation.cluster != aux_snapshot.cluster
        or simulation.genesis_hash != pool_evidence.genesis_hash
        or simulation.genesis_hash != vault_snapshot.genesis_hash
        or simulation.genesis_hash != tick_array_snapshot.genesis_hash
        or simulation.genesis_hash != aux_snapshot.genesis_hash
    ):
        raise RaydiumClmmSimulationStateError("simulation, gate and decoded Raydium evidence provenance differs")
    if (
        pool_evidence.route_message_sha256 != digest
        or vault_snapshot.route_message_sha256 != digest
        or tick_array_snapshot.route_message_sha256 != digest
        or aux_snapshot.route_message_sha256 != digest
        or pool_evidence.simulation_response_id != simulation.response_id
        or vault_snapshot.simulation_response_id != simulation.response_id
        or tick_array_snapshot.simulation_response_id != simulation.response_id
        or aux_snapshot.simulation_response_id != simulation.response_id
        or pool_evidence.simulation_context_slot != simulation.context_slot
        or vault_snapshot.simulation_context_slot != simulation.context_slot
        or tick_array_snapshot.simulation_context_slot != simulation.context_slot
        or aux_snapshot.simulation_context_slot != simulation.context_slot
    ):
        raise RaydiumClmmSimulationStateError("decoded Raydium snapshots are not bound to the exact simulation response")
    if (
        pool_check.pool.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT
        or pool_check.pool.pool_address != cpi.pool_state
        or cpi.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT
    ):
        raise RaydiumClmmSimulationStateError("PoolState route or CPI evidence does not match the pinned source and pool")
    if (
        tuple(tick_array_snapshot.cpi_account_order) != tuple(cpi.tick_array_accounts)
        or tuple(item.address for item in tick_array_snapshot.tick_arrays)
        != tuple(address for address in cpi.tick_array_accounts if address != tick_array_snapshot.bitmap_extension_address)
        or tick_array_snapshot.bitmap_extension_sha256 != (
            tick_array_snapshot.bitmap_extension.data_sha256
            if tick_array_snapshot.bitmap_extension is not None else None
        )
    ):
        raise RaydiumClmmSimulationStateError("tick-array snapshot does not preserve exact CPI account and bitmap-extension evidence")
    if not (
        pool_evidence.context_slot <= vault_snapshot.context_slot
        and vault_snapshot.context_slot <= tick_array_snapshot.context_slot
        and tick_array_snapshot.context_slot <= aux_snapshot.context_slot
    ):
        raise RaydiumClmmSimulationStateError("Raydium account evidence context slots are not monotonically fresh")
    try:
        verify_raydium_clmm_bitmap_traversal(
            order,
            pool_check=pool_check,
            pool_evidence=pool_evidence,
            tick_array_snapshot=tick_array_snapshot,
        )
    except RaydiumClmmBitmapTraversalError as exc:
        raise RaydiumClmmSimulationStateError(f"Raydium bitmap traversal proof is invalid: {exc}") from exc

    commitments = build_raydium_clmm_account_commitments(
        cpi,
        pool_evidence=pool_evidence,
        vault_snapshot=vault_snapshot,
        tick_array_snapshot=tick_array_snapshot,
        aux_snapshot=aux_snapshot,
    )
    expected_addresses = tuple(item.address for item in commitments)
    if not set(expected_addresses).issubset(set(validation.account_keys)):
        raise RaydiumClmmSimulationStateError("required Raydium account is absent from the validated transaction message")
    observed = {item.address: item for item in simulation.account_states}
    missing = tuple(address for address in expected_addresses if address not in observed)
    if missing:
        raise RaydiumClmmSimulationStateError(
            "simulation omitted required Raydium pre/post account state: " + ",".join(missing)
        )

    pre_hashes = []
    post_hashes = []
    roles = []
    for commitment in commitments:
        state = observed[commitment.address]
        if (
            state.pre_owner != commitment.owner_program_id
            or state.post_owner != commitment.owner_program_id
            or state.pre_executable is not False
            or state.post_executable is not False
            or state.pre_lamports <= 0
            or state.pre_data_sha256 != commitment.data_sha256
            or len(state.pre_data) != commitment.data_length
            or len(state.post_data) != commitment.data_length
        ):
            raise RaydiumClmmSimulationStateError(
                f"simulation pre-state does not match decoded {commitment.role} evidence for {commitment.address}"
            )
        if commitment.lamports is not None and state.pre_lamports != commitment.lamports:
            raise RaydiumClmmSimulationStateError(
                f"simulation pre-state lamports differ from decoded {commitment.role} evidence for {commitment.address}"
            )
        pre_hashes.append((commitment.address, state.pre_data_sha256))
        post_hashes.append((commitment.address, state.post_data_sha256))
        roles.append((commitment.address, commitment.role))

    return RaydiumClmmSimulationStateBinding(
        passed=True,
        message_sha256=digest,
        provider_id=simulation.provider_id,
        simulation_response_id=simulation.response_id,
        simulation_context_slot=simulation.context_slot,
        bound_account_addresses=expected_addresses,
        pre_state_sha256=tuple(pre_hashes),
        post_state_sha256=tuple(post_hashes),
        account_roles=tuple(roles),
        execution_ready=False,
    )
