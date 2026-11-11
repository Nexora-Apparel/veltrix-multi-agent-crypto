"""Pinned Raydium CLMM ObservationState decoder and update transition."""
from __future__ import annotations

from dataclasses import dataclass, replace, replace
import hashlib

from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolAccountEvidence
from veltrix.raydium_clmm_pool_state import RAYDIUM_CLMM_PROGRAM_ID


RAYDIUM_OBSERVATION_SEED = b"observation"
RAYDIUM_OBSERVATION_COUNT = 100
RAYDIUM_OBSERVATION_UPDATE_DURATION = 15
RAYDIUM_OBSERVATION_RECORD_LENGTH = 44
RAYDIUM_OBSERVATION_STATE_LENGTH = 8 + 1 + 8 + 2 + 32 + (
    RAYDIUM_OBSERVATION_RECORD_LENGTH * RAYDIUM_OBSERVATION_COUNT
) + 8 * 4
RAYDIUM_OBSERVATION_DISCRIMINATOR = hashlib.sha256(b"account:ObservationState").digest()[:8]


class RaydiumObservationError(ValueError):
    """Raised for invalid or inconsistent Raydium observation state."""


@dataclass(frozen=True, slots=True)
class RaydiumObservation:
    block_timestamp: int
    tick_cumulative: int
    padding: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class RaydiumObservationState:
    address: str
    initialized: bool
    recent_epoch: int
    observation_index: int
    pool_id: str
    observations: tuple[RaydiumObservation, ...]
    padding: tuple[int, int, int, int]
    data_sha256: str


def derive_raydium_observation_address(pool_address: str) -> tuple[str, int]:
    try:
        pool = Pubkey.from_string(pool_address)
        address, bump = Pubkey.find_program_address(
            [RAYDIUM_OBSERVATION_SEED, bytes(pool)],
            Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID),
        )
        return str(address), bump
    except Exception as exc:
        raise RaydiumObservationError("Raydium observation PDA derivation failed") from exc


def decode_raydium_observation_state(
    account: DexPoolAccountEvidence,
    *,
    expected_pool_address: str,
    expected_address: str,
) -> RaydiumObservationState:
    if not isinstance(account, DexPoolAccountEvidence):
        raise RaydiumObservationError("owner-verified Raydium observation account evidence is required")
    if (
        account.expected_program_id != RAYDIUM_CLMM_PROGRAM_ID
        or account.owner_program_id != RAYDIUM_CLMM_PROGRAM_ID
        or account.executable is not False
        or isinstance(account.lamports, bool)
        or not isinstance(account.lamports, int)
        or account.lamports <= 0
    ):
        raise RaydiumObservationError("observation owner, executable flag or lamports are invalid")
    try:
        address = str(Pubkey.from_string(account.amm_key))
        pool_address = str(Pubkey.from_string(expected_pool_address))
        expected = str(Pubkey.from_string(expected_address))
    except Exception as exc:
        raise RaydiumObservationError("observation or pool address is invalid") from exc
    derived, _ = derive_raydium_observation_address(pool_address)
    if address != expected or address != derived:
        raise RaydiumObservationError("observation account is not the canonical PDA for PoolState")
    data = account.data_bytes
    if (
        not isinstance(data, bytes)
        or len(data) != RAYDIUM_OBSERVATION_STATE_LENGTH
        or account.data_length != RAYDIUM_OBSERVATION_STATE_LENGTH
        or hashlib.sha256(data).hexdigest() != account.data_sha256
        or data[:8] != RAYDIUM_OBSERVATION_DISCRIMINATOR
    ):
        raise RaydiumObservationError("ObservationState discriminator, length or account hash is invalid")

    initialized_byte = data[8]
    if initialized_byte not in (0, 1):
        raise RaydiumObservationError("ObservationState initialized flag is not a boolean")
    initialized = bool(initialized_byte)
    recent_epoch = int.from_bytes(data[9:17], "little")
    observation_index = int.from_bytes(data[17:19], "little")
    pool_id = str(Pubkey.from_bytes(data[19:51]))
    if pool_id != pool_address:
        raise RaydiumObservationError("ObservationState belongs to a different pool")
    if observation_index >= RAYDIUM_OBSERVATION_COUNT:
        raise RaydiumObservationError("ObservationState index is outside the 100-slot ring")

    observations = []
    offset = 51
    for _ in range(RAYDIUM_OBSERVATION_COUNT):
        timestamp = int.from_bytes(data[offset:offset + 4], "little")
        cumulative = int.from_bytes(data[offset + 4:offset + 12], "little", signed=True)
        padding = tuple(
            int.from_bytes(data[offset + 12 + index * 8:offset + 20 + index * 8], "little")
            for index in range(4)
        )
        if any(padding):
            raise RaydiumObservationError("Observation record contains unsupported non-zero padding")
        observations.append(RaydiumObservation(timestamp, cumulative, padding))
        offset += RAYDIUM_OBSERVATION_RECORD_LENGTH

    state_padding = tuple(
        int.from_bytes(data[offset + index * 8:offset + (index + 1) * 8], "little")
        for index in range(4)
    )
    if any(state_padding):
        raise RaydiumObservationError("ObservationState contains unsupported non-zero reserved padding")
    if not initialized and any(
        observation.block_timestamp or observation.tick_cumulative
        for observation in observations
    ):
        raise RaydiumObservationError("uninitialized ObservationState contains populated observations")

    return RaydiumObservationState(
        address=address,
        initialized=initialized,
        recent_epoch=recent_epoch,
        observation_index=observation_index,
        pool_id=pool_id,
        observations=tuple(observations),
        padding=state_padding,
        data_sha256=account.data_sha256,
    )


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
        return replace(state, initialized=True, observations=tuple(observations))

    last = observations[index]
    delta_time = max(0, block_timestamp - last.block_timestamp)
    if delta_time < RAYDIUM_OBSERVATION_UPDATE_DURATION:
        return state

    delta_tick_cumulative = max(I64_MIN, min(I64_MAX, tick * delta_time))
    next_cumulative = (
        (last.tick_cumulative + delta_tick_cumulative - I64_MIN) % (1 << 64)
    ) + I64_MIN
    next_index = 0 if index == RAYDIUM_OBSERVATION_COUNT - 1 else index + 1
    next_observation = replace(
        observations[next_index],
        block_timestamp=block_timestamp,
        tick_cumulative=next_cumulative,
    )
    observations[next_index] = next_observation
    return replace(
        state,
        observation_index=next_index,
        observations=tuple(observations),
    )
