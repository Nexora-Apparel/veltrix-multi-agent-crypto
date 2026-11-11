"""Read-only Raydium CLMM AMM-config and observation account verification.

Account layouts and PDA seeds are pinned to raydium-io/raydium-clmm
6d7a0f82d2f293c553c69aaf943377367c6c4cc6. This module only verifies
account identity and provenance; it does not authorize execution.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolEvidenceSnapshot
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RaydiumClmmPoolRouteCheck,
)
from veltrix.raydium_clmm_vaults import RaydiumClmmVaultSnapshot
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTickArraySnapshot
from veltrix.simulation_gate import SimulationEvidence
from veltrix.solana_rpc_simulation import SolanaRpcSimulationAdapter


AMM_CONFIG_SEED = b"amm_config"
OBSERVATION_SEED = b"observation"
AMM_CONFIG_DISCRIMINATOR = hashlib.sha256(b"account:AmmConfig").digest()[:8]
OBSERVATION_STATE_DISCRIMINATOR = hashlib.sha256(b"account:ObservationState").digest()[:8]
AMM_CONFIG_DATA_LENGTH = 117
OBSERVATION_COUNT = 100
OBSERVATION_ENTRY_LENGTH = 44
OBSERVATION_STATE_DATA_LENGTH = 8 + 1 + 8 + 2 + 32 + OBSERVATION_COUNT * OBSERVATION_ENTRY_LENGTH + 32
FEE_RATE_DENOMINATOR = 1_000_000


class RaydiumClmmAuxAccountError(ValueError):
    """Raised when Raydium AMM config or observation evidence is invalid."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmAmmConfig:
    address: str
    bump: int
    index: int
    owner: str
    protocol_fee_rate: int
    trade_fee_rate: int
    tick_spacing: int
    fund_fee_rate: int
    data_sha256: str


@dataclass(frozen=True, slots=True)
class RaydiumClmmObservationState:
    address: str
    pool_address: str
    initialized: bool
    recent_epoch: int
    observation_index: int
    latest_observation_timestamp: int
    latest_tick_cumulative: int
    data_sha256: str


@dataclass(frozen=True, slots=True)
class RaydiumClmmAuxAccountSnapshot:
    route_message_sha256: str
    simulation_response_id: str
    simulation_context_slot: int
    provider_id: str
    cluster: str
    genesis_hash: str
    context_slot: int
    amm_configs: tuple[RaydiumClmmAmmConfig, ...]
    observations: tuple[RaydiumClmmObservationState, ...]
    source_commit: str = RAYDIUM_CLMM_SOURCE_COMMIT
    execution_ready: bool = False


@dataclass(frozen=True, slots=True)
class RaydiumClmmAuxAccountPolicy:
    max_observation_age_seconds: int = 86_400

    def __post_init__(self) -> None:
        value = self.max_observation_age_seconds
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2_592_000:
            raise RaydiumClmmAuxAccountError("max_observation_age_seconds must be between 1 and 2592000")


def derive_raydium_clmm_amm_config_address(*, index: int) -> tuple[str, int]:
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 65_535:
        raise RaydiumClmmAuxAccountError("AMM config index must be a u16")
    address, bump = Pubkey.find_program_address(
        [AMM_CONFIG_SEED, index.to_bytes(2, "big")],
        Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID),
    )
    return str(address), bump


def derive_raydium_clmm_observation_address(*, pool_address: str) -> str:
    try:
        pool = Pubkey.from_string(pool_address)
    except Exception as exc:
        raise RaydiumClmmAuxAccountError("observation pool address is invalid") from exc
    address, _ = Pubkey.find_program_address(
        [OBSERVATION_SEED, bytes(pool)],
        Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID),
    )
    return str(address)


def _canonical_pubkey(raw: bytes, field: str) -> str:
    if len(raw) != 32:
        raise RaydiumClmmAuxAccountError(f"{field} public key bytes are truncated")
    try:
        return str(Pubkey.from_bytes(raw))
    except Exception as exc:
        raise RaydiumClmmAuxAccountError(f"{field} public key is invalid") from exc


def decode_raydium_clmm_amm_config(
    *,
    address: str,
    owner_program_id: str,
    lamports: int,
    executable: bool,
    data: bytes,
) -> RaydiumClmmAmmConfig:
    if owner_program_id != RAYDIUM_CLMM_PROGRAM_ID:
        raise RaydiumClmmAuxAccountError("AMM config owner is not the pinned Raydium CLMM program")
    if executable is not False or isinstance(lamports, bool) or not isinstance(lamports, int) or lamports <= 0:
        raise RaydiumClmmAuxAccountError("AMM config must be non-executable and have positive lamports")
    if not isinstance(data, bytes) or len(data) != AMM_CONFIG_DATA_LENGTH:
        raise RaydiumClmmAuxAccountError("AMM config data length differs from the pinned 117-byte layout")
    if data[:8] != AMM_CONFIG_DISCRIMINATOR:
        raise RaydiumClmmAuxAccountError("AMM config Anchor discriminator is invalid")
    try:
        observed_address = str(Pubkey.from_string(address))
    except Exception as exc:
        raise RaydiumClmmAuxAccountError("AMM config address is invalid") from exc
    bump = data[8]
    index = int.from_bytes(data[9:11], "little")
    owner = _canonical_pubkey(data[11:43], "AMM config owner")
    protocol_fee_rate = int.from_bytes(data[43:47], "little")
    trade_fee_rate = int.from_bytes(data[47:51], "little")
    tick_spacing = int.from_bytes(data[51:53], "little")
    fund_fee_rate = int.from_bytes(data[53:57], "little")
    derived_address, derived_bump = derive_raydium_clmm_amm_config_address(index=index)
    if observed_address != derived_address or bump != derived_bump:
        raise RaydiumClmmAuxAccountError("AMM config address or bump does not match its canonical index PDA")
    if owner == str(Pubkey.default()):
        raise RaydiumClmmAuxAccountError("AMM config owner must not be the default public key")
    if not 0 <= protocol_fee_rate <= FEE_RATE_DENOMINATOR:
        raise RaydiumClmmAuxAccountError("AMM config protocol fee rate exceeds the protocol denominator")
    if not 0 <= trade_fee_rate < FEE_RATE_DENOMINATOR:
        raise RaydiumClmmAuxAccountError("AMM config trade fee rate exceeds the protocol denominator")
    if not 1 <= tick_spacing <= 1000:
        raise RaydiumClmmAuxAccountError("AMM config tick spacing is outside the pinned protocol range")
    if fund_fee_rate + protocol_fee_rate > FEE_RATE_DENOMINATOR:
        raise RaydiumClmmAuxAccountError("AMM config fund plus protocol fee exceeds the protocol denominator")
    return RaydiumClmmAmmConfig(
        address=observed_address,
        bump=bump,
        index=index,
        owner=owner,
        protocol_fee_rate=protocol_fee_rate,
        trade_fee_rate=trade_fee_rate,
        tick_spacing=tick_spacing,
        fund_fee_rate=fund_fee_rate,
        data_sha256=hashlib.sha256(data).hexdigest(),
    )


def decode_raydium_clmm_observation_state(
    *,
    address: str,
    expected_pool_address: str,
    owner_program_id: str,
    lamports: int,
    executable: bool,
    data: bytes,
    observed_at_epoch_ms: int,
    policy: RaydiumClmmAuxAccountPolicy | None = None,
) -> RaydiumClmmObservationState:
    policy = policy or RaydiumClmmAuxAccountPolicy()
    if not isinstance(policy, RaydiumClmmAuxAccountPolicy):
        raise RaydiumClmmAuxAccountError("Raydium auxiliary account policy is required")
    if owner_program_id != RAYDIUM_CLMM_PROGRAM_ID:
        raise RaydiumClmmAuxAccountError("observation account owner is not the pinned Raydium CLMM program")
    if executable is not False or isinstance(lamports, bool) or not isinstance(lamports, int) or lamports <= 0:
        raise RaydiumClmmAuxAccountError("observation account must be non-executable and have positive lamports")
    if not isinstance(data, bytes) or len(data) != OBSERVATION_STATE_DATA_LENGTH:
        raise RaydiumClmmAuxAccountError("ObservationState length differs from the pinned 4483-byte layout")
    if data[:8] != OBSERVATION_STATE_DISCRIMINATOR:
        raise RaydiumClmmAuxAccountError("ObservationState Anchor discriminator is invalid")
    if isinstance(observed_at_epoch_ms, bool) or not isinstance(observed_at_epoch_ms, int) or observed_at_epoch_ms < 0:
        raise RaydiumClmmAuxAccountError("simulation observation timestamp is invalid")
    try:
        observed_address = str(Pubkey.from_string(address))
        expected_pool = str(Pubkey.from_string(expected_pool_address))
        observed_pool = _canonical_pubkey(data[19:51], "observation pool")
    except Exception as exc:
        raise RaydiumClmmAuxAccountError("observation address or pool identity is invalid") from exc
    if observed_address != derive_raydium_clmm_observation_address(pool_address=expected_pool):
        raise RaydiumClmmAuxAccountError("observation account address does not match the canonical pool PDA")
    if observed_pool != expected_pool:
        raise RaydiumClmmAuxAccountError("observation state belongs to a different pool")
    initialized = data[8]
    if initialized not in (0, 1) or initialized != 1:
        raise RaydiumClmmAuxAccountError("observation state is not initialized")
    recent_epoch = int.from_bytes(data[9:17], "little")
    observation_index = int.from_bytes(data[17:19], "little")
    if observation_index >= OBSERVATION_COUNT:
        raise RaydiumClmmAuxAccountError("observation ring index is outside the 100-entry array")
    offset = 51 + observation_index * OBSERVATION_ENTRY_LENGTH
    latest_timestamp = int.from_bytes(data[offset:offset + 4], "little")
    latest_tick_cumulative = int.from_bytes(data[offset + 4:offset + 12], "little", signed=True)
    if latest_timestamp == 0:
        raise RaydiumClmmAuxAccountError("latest observation timestamp is uninitialized")
    now_u32 = (observed_at_epoch_ms // 1000) & 0xFFFF_FFFF
    age = (now_u32 - latest_timestamp) & 0xFFFF_FFFF
    if age >= 0x8000_0000 or age > policy.max_observation_age_seconds:
        raise RaydiumClmmAuxAccountError("latest observation is stale or is in the future relative to simulation")
    return RaydiumClmmObservationState(
        address=observed_address,
        pool_address=expected_pool,
        initialized=True,
        recent_epoch=recent_epoch,
        observation_index=observation_index,
        latest_observation_timestamp=latest_timestamp,
        latest_tick_cumulative=latest_tick_cumulative,
        data_sha256=hashlib.sha256(data).hexdigest(),
    )


def fetch_raydium_clmm_aux_account_snapshot(
    pool_evidence: DexPoolEvidenceSnapshot,
    *,
    pool_checks: tuple[RaydiumClmmPoolRouteCheck, ...],
    vault_snapshot: RaydiumClmmVaultSnapshot,
    tick_array_snapshot: RaydiumClmmTickArraySnapshot,
    simulation: SimulationEvidence,
    rpc_adapter: SolanaRpcSimulationAdapter,
    policy: RaydiumClmmAuxAccountPolicy | None = None,
) -> RaydiumClmmAuxAccountSnapshot:
    """Fetch and verify each pool's config and observation account after all prior evidence."""
    if not isinstance(pool_evidence, DexPoolEvidenceSnapshot):
        raise RaydiumClmmAuxAccountError("provenance-bound pool evidence is required")
    if not isinstance(vault_snapshot, RaydiumClmmVaultSnapshot) or not isinstance(tick_array_snapshot, RaydiumClmmTickArraySnapshot):
        raise RaydiumClmmAuxAccountError("verified vault and tick-array snapshots are required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumClmmAuxAccountError("simulation evidence is required")
    if not isinstance(rpc_adapter, SolanaRpcSimulationAdapter):
        raise RaydiumClmmAuxAccountError("provider-pinned Solana RPC adapter is required")
    if not isinstance(pool_checks, tuple) or not pool_checks or any(
        not isinstance(item, RaydiumClmmPoolRouteCheck) or item.passed is not True for item in pool_checks
    ):
        raise RaydiumClmmAuxAccountError("successful Raydium pool route checks are required")
    policy = policy or RaydiumClmmAuxAccountPolicy()
    if not isinstance(policy, RaydiumClmmAuxAccountPolicy):
        raise RaydiumClmmAuxAccountError("Raydium auxiliary account policy is required")

    digest = simulation.message_sha256
    for evidence in (pool_evidence, vault_snapshot, tick_array_snapshot):
        if (
            evidence.route_message_sha256 != digest
            or evidence.simulation_response_id != simulation.response_id
            or evidence.simulation_context_slot != simulation.context_slot
            or evidence.provider_id != simulation.provider_id
            or evidence.cluster != simulation.cluster
            or evidence.genesis_hash != simulation.genesis_hash
        ):
            raise RaydiumClmmAuxAccountError("pool, vault, tick and simulation evidence provenance differs")
    if (
        rpc_adapter.provider_id != simulation.provider_id
        or rpc_adapter.cluster != simulation.cluster
        or rpc_adapter.expected_genesis_hash != simulation.genesis_hash
    ):
        raise RaydiumClmmAuxAccountError("auxiliary account RPC adapter differs from the simulation provider")
    if hashlib.sha256(simulation.message_sha256.encode()).hexdigest() == "":
        raise RaydiumClmmAuxAccountError("unreachable digest validation")
    expected: dict[str, tuple[str, str]] = {}
    for check in pool_checks:
        pool = check.pool
        expected[pool.amm_config] = (pool.pool_address, "amm_config")
        expected[pool.observation_key] = (pool.pool_address, "observation")
    if len(expected) != len(pool_checks) * 2:
        raise RaydiumClmmAuxAccountError("one auxiliary account address is reused across conflicting pool roles")
    minimum_slot = max(
        simulation.context_slot, pool_evidence.context_slot,
        vault_snapshot.context_slot, tick_array_snapshot.context_slot,
    )
    snapshot = rpc_adapter.fetch_account_snapshot(tuple(sorted(expected)), min_context_slot=minimum_slot)
    if (
        snapshot.provider_id != simulation.provider_id
        or snapshot.cluster != simulation.cluster
        or snapshot.genesis_hash != simulation.genesis_hash
        or snapshot.context_slot < minimum_slot
        or len(snapshot.accounts) != len(expected)
    ):
        raise RaydiumClmmAuxAccountError("auxiliary account snapshot provenance, freshness or count is invalid")

    configs = []
    observations = []
    seen = set()
    for account in snapshot.accounts:
        if account.address in seen or account.address not in expected:
            raise RaydiumClmmAuxAccountError("auxiliary account snapshot contains duplicate or unexpected accounts")
        seen.add(account.address)
        pool_address, role = expected[account.address]
        if role == "amm_config":
            config = decode_raydium_clmm_amm_config(
                address=account.address, owner_program_id=account.owner,
                lamports=account.lamports, executable=account.executable, data=account.data,
            )
            pool = next(item.pool for item in pool_checks if item.pool.pool_address == pool_address)
            if config.tick_spacing != pool.tick_spacing:
                raise RaydiumClmmAuxAccountError("AMM config tick spacing differs from PoolState")
            configs.append(config)
        else:
            observation = decode_raydium_clmm_observation_state(
                address=account.address, expected_pool_address=pool_address,
                owner_program_id=account.owner, lamports=account.lamports,
                executable=account.executable, data=account.data,
                observed_at_epoch_ms=simulation.observed_at_epoch_ms, policy=policy,
            )
            observations.append(observation)
    if seen != set(expected) or len(configs) != len(pool_checks) or len(observations) != len(pool_checks):
        raise RaydiumClmmAuxAccountError("auxiliary account snapshot omitted a required config or observation")
    return RaydiumClmmAuxAccountSnapshot(
        route_message_sha256=digest,
        simulation_response_id=simulation.response_id,
        simulation_context_slot=simulation.context_slot,
        provider_id=snapshot.provider_id,
        cluster=snapshot.cluster,
        genesis_hash=snapshot.genesis_hash,
        context_slot=snapshot.context_slot,
        amm_configs=tuple(configs),
        observations=tuple(observations),
        execution_ready=False,
    )
