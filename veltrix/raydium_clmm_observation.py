"""Pinned Raydium CLMM ObservationState decoder and PDA verification."""
from __future__ import annotations

from dataclasses import dataclass
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
