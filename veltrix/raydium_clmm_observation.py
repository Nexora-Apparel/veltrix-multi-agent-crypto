"""Pinned Raydium CLMM ObservationState decoder and PDA verification."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib

from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolAccountEvidence
from veltrix.raydium_clmm_pool_state import RAYDIUM_CLMM_PROGRAM_ID


RAYDIUM_OBSERVATION_SEED = b"observation"
RAYDIUM_OBSERVATION_COUNT = 100
RAYDIUM_OBSERVATION_RECORD_LENGTH = 44
RAYDIUM_OBSERVATION_STATE_DISCRIMINATOR = hashlib.sha256(
    b"account:ObservationState"
).digest()[:8]
RAYDIUM_OBSERVATION_STATE_LENGTH = (
    8 + 1 + 8 + 2 + 32
    + RAYDIUM_OBSERVATION_COUNT * RAYDIUM_OBSERVATION_RECORD_LENGTH
    + 32
)


class RaydiumClmmObservationError(ValueError):
    """Raised when Raydium ObservationState evidence is malformed."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmObservation:
    block_timestamp: int
    tick_cumulative: int
    padding: bytes


@dataclass(frozen=True, slots=True)
class RaydiumClmmObservationState:
    address: str
    initialized: bool
    recent_epoch: int
    observation_index: int
    pool_id: str
    observations: tuple[RaydiumClmmObservation, ...]
    padding: bytes
    data_sha256: str


def derive_raydium_clmm_observation_address(pool_address: str) -> tuple[str, int]:
    try:
        pool = Pubkey.from_string(pool_address)
        address, bump = Pubkey.find_program_address(
            [RAYDIUM_OBSERVATION_SEED, bytes(pool)],
            Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID),
        )
        return str(address), bump
    except Exception as exc:
        raise RaydiumClmmObservationError("Raydium observation PDA derivation failed") from exc


def decode_raydium_clmm_observation_state(
    account: DexPoolAccountEvidence,
    *,
    expected_address: str,
    expected_pool_address: str,
) -> RaydiumClmmObservationState:
    if not isinstance(account, DexPoolAccountEvidence):
        raise RaydiumClmmObservationError("verified Raydium observation account evidence is required")
    if account.dex_label not in ("Raydium CLMM", "Raydium CLMM Observation"):
        raise RaydiumClmmObservationError("account evidence is not labelled as Raydium CLMM observation state")
    if (
        account.expected_program_id != RAYDIUM_CLMM_PROGRAM_ID
        or account.owner_program_id != RAYDIUM_CLMM_PROGRAM_ID
        or account.executable is not False
        or isinstance(account.lamports, bool)
        or not isinstance(account.lamports, int)
        or account.lamports <= 0
    ):
        raise RaydiumClmmObservationError("observation account owner, executable flag or lamports are invalid")
    try:
        address = str(Pubkey.from_string(account.amm_key))
        expected = str(Pubkey.from_string(expected_address))
        pool_id = str(Pubkey.from_string(expected_pool_address))
    except Exception as exc:
        raise RaydiumClmmObservationError("observation or pool public key is invalid") from exc
    if address != expected:
        raise RaydiumClmmObservationError("observation account address differs from PoolState.observation_key")
    data = account.data_bytes
    if (
        not isinstance(data, bytes)
        or len(data) != RAYDIUM_OBSERVATION_STATE_LENGTH
        or account.data_length != RAYDIUM_OBSERVATION_STATE_LENGTH
        or hashlib.sha256(data).hexdigest() != account.data_sha256
        or data[:8] != RAYDIUM_OBSERVATION_STATE_DISCRIMINATOR
    ):
        raise RaydiumClmmObservationError("ObservationState discriminator, length or account hash is invalid")

    initialized_byte = data[8]
    if initialized_byte not in (0, 1):
        raise RaydiumClmmObservationError("ObservationState initialized flag is not a boolean")
    initialized = bool(initialized_byte)
    recent_epoch = int.from_bytes(data[9:17], "little")
    observation_index = int.from_bytes(data[17:19], "little")
    observed_pool = str(Pubkey.from_bytes(data[19:51]))
    if observed_pool != pool_id:
        raise RaydiumClmmObservationError("ObservationState belongs to a different pool")
    canonical, _ = derive_raydium_clmm_observation_address(observed_pool)
    if address != canonical:
        raise RaydiumClmmObservationError("observation account is not the canonical pool-derived PDA")
    if observation_index >= RAYDIUM_OBSERVATION_COUNT:
        raise RaydiumClmmObservationError("ObservationState ring index is outside the 100-entry buffer")

    observations = []
    offset = 51
    for index in range(RAYDIUM_OBSERVATION_COUNT):
        record = data[offset:offset + RAYDIUM_OBSERVATION_RECORD_LENGTH]
        timestamp = int.from_bytes(record[0:4], "little")
        cumulative = int.from_bytes(record[4:12], "little", signed=True)
        padding = record[12:44]
        if any(padding):
            raise RaydiumClmmObservationError(
                f"ObservationState record {index} contains unsupported non-zero reserved padding"
            )
        observations.append(RaydiumClmmObservation(timestamp, cumulative, padding))
        offset += RAYDIUM_OBSERVATION_RECORD_LENGTH

    padding = data[offset:offset + 32]
    if len(padding) != 32 or any(padding):
        raise RaydiumClmmObservationError("ObservationState contains unsupported non-zero account padding")
    if not initialized and any(
        observation.block_timestamp or observation.tick_cumulative
        for observation in observations
    ):
        raise RaydiumClmmObservationError("uninitialized ObservationState contains populated observations")

    return RaydiumClmmObservationState(
        address=address,
        initialized=initialized,
        recent_epoch=recent_epoch,
        observation_index=observation_index,
        pool_id=observed_pool,
        observations=tuple(observations),
        padding=padding,
        data_sha256=account.data_sha256,
    )


def encode_raydium_clmm_observation_state(
    state: RaydiumClmmObservationState,
) -> bytes:
    """Serialize the exact pinned ObservationState account layout."""
    if not isinstance(state, RaydiumClmmObservationState):
        raise RaydiumClmmObservationError("decoded Raydium ObservationState is required")
    try:
        address = str(Pubkey.from_string(state.address))
        pool = Pubkey.from_string(state.pool_id)
        canonical, _ = derive_raydium_clmm_observation_address(state.pool_id)
        if address != state.address or address != canonical:
            raise ValueError("non-canonical observation PDA")
    except Exception as exc:
        raise RaydiumClmmObservationError("ObservationState identity or PDA is invalid") from exc
    if not isinstance(state.initialized, bool):
        raise RaydiumClmmObservationError("ObservationState initialized flag must be boolean")
    if isinstance(state.recent_epoch, bool) or not isinstance(state.recent_epoch, int) or not 0 <= state.recent_epoch <= 0xFFFFFFFFFFFFFFFF:
        raise RaydiumClmmObservationError("ObservationState recent epoch must fit u64")
    if isinstance(state.observation_index, bool) or not isinstance(state.observation_index, int) or not 0 <= state.observation_index < RAYDIUM_OBSERVATION_COUNT:
        raise RaydiumClmmObservationError("ObservationState ring index is outside the 100-entry buffer")
    if not isinstance(state.observations, tuple) or len(state.observations) != RAYDIUM_OBSERVATION_COUNT:
        raise RaydiumClmmObservationError("ObservationState must contain exactly 100 observations")
    if not isinstance(state.padding, bytes) or len(state.padding) != 32 or any(state.padding):
        raise RaydiumClmmObservationError("ObservationState account padding must be 32 zero bytes")

    data = bytearray(RAYDIUM_OBSERVATION_STATE_LENGTH)
    data[:8] = RAYDIUM_OBSERVATION_STATE_DISCRIMINATOR
    data[8] = int(state.initialized)
    data[9:17] = state.recent_epoch.to_bytes(8, "little")
    data[17:19] = state.observation_index.to_bytes(2, "little")
    data[19:51] = bytes(pool)
    offset = 51
    for index, observation in enumerate(state.observations):
        if not isinstance(observation, RaydiumClmmObservation):
            raise RaydiumClmmObservationError("ObservationState contains a malformed observation record")
        timestamp = observation.block_timestamp
        cumulative = observation.tick_cumulative
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or not 0 <= timestamp <= 0xFFFFFFFF:
            raise RaydiumClmmObservationError(f"observation {index} timestamp must fit u32")
        if isinstance(cumulative, bool) or not isinstance(cumulative, int) or not -(1 << 63) <= cumulative < (1 << 63):
            raise RaydiumClmmObservationError(f"observation {index} cumulative must fit i64")
        if not isinstance(observation.padding, bytes) or len(observation.padding) != 32 or any(observation.padding):
            raise RaydiumClmmObservationError(f"observation {index} reserved padding must be 32 zero bytes")
        if not state.initialized and (timestamp or cumulative):
            raise RaydiumClmmObservationError("uninitialized ObservationState cannot contain populated observations")
        data[offset:offset + 4] = timestamp.to_bytes(4, "little")
        data[offset + 4:offset + 12] = cumulative.to_bytes(8, "little", signed=True)
        data[offset + 12:offset + 44] = observation.padding
        offset += RAYDIUM_OBSERVATION_RECORD_LENGTH
    data[offset:] = state.padding
    if len(data) != RAYDIUM_OBSERVATION_STATE_LENGTH:
        raise RaydiumClmmObservationError("serialized ObservationState length is inconsistent")
    return bytes(data)


def _commit_observation_state(
    state: RaydiumClmmObservationState,
) -> RaydiumClmmObservationState:
    encoded = encode_raydium_clmm_observation_state(state)
    return replace(state, data_sha256=hashlib.sha256(encoded).hexdigest())


RAYDIUM_OBSERVATION_UPDATE_DURATION = 15
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1


def apply_raydium_observation_update(
    state: RaydiumClmmObservationState,
    *,
    block_timestamp: int,
    tick: int,
) -> RaydiumClmmObservationState:
    """Port ObservationState::update from the pinned Raydium oracle.rs."""
    if not isinstance(state, RaydiumClmmObservationState):
        raise RaydiumClmmObservationError("decoded Raydium ObservationState is required")
    if isinstance(block_timestamp, bool) or not isinstance(block_timestamp, int) or not 0 <= block_timestamp <= 0xFFFFFFFF:
        raise RaydiumClmmObservationError("block_timestamp must fit u32")
    if isinstance(tick, bool) or not isinstance(tick, int) or not -(1 << 31) <= tick < (1 << 31):
        raise RaydiumClmmObservationError("tick must fit i32")

    observations = list(state.observations)
    index = state.observation_index
    if not state.initialized:
        current = observations[index]
        observations[index] = replace(
            current, block_timestamp=block_timestamp, tick_cumulative=0
        )
        return _commit_observation_state(
            replace(state, initialized=True, observations=tuple(observations))
        )

    last = observations[index]
    delta_time = max(0, block_timestamp - last.block_timestamp)
    if delta_time < RAYDIUM_OBSERVATION_UPDATE_DURATION:
        return state

    delta_tick_cumulative = max(I64_MIN, min(I64_MAX, tick * delta_time))
    next_cumulative = (
        (last.tick_cumulative + delta_tick_cumulative - I64_MIN) % (1 << 64)
    ) + I64_MIN
    next_index = 0 if index == RAYDIUM_OBSERVATION_COUNT - 1 else index + 1
    observations[next_index] = replace(
        observations[next_index],
        block_timestamp=block_timestamp,
        tick_cumulative=next_cumulative,
    )
    return _commit_observation_state(
        replace(
            state,
            observation_index=next_index,
            observations=tuple(observations),
        )
    )
