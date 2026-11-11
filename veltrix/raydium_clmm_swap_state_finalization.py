"""Atomic finalization of Raydium ObservationState and PoolState after a swap."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from veltrix.raydium_clmm_observation import (
    RaydiumClmmObservationError,
    RaydiumClmmObservationState,
    apply_raydium_observation_update,
    encode_raydium_clmm_observation_state,
)
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState
from veltrix.raydium_clmm_pool_post_swap_finalization import (
    RaydiumPoolPostSwapFinalizationError,
    RaydiumPoolPostSwapFinalizationResult,
    finalize_raydium_pool_after_swap,
)
from veltrix.raydium_clmm_swap_inner_iteration import RaydiumClmmSwapInnerIterationResult


class RaydiumSwapStateFinalizationError(ValueError):
    """Raised when the final pool/oracle state bundle cannot be committed."""


@dataclass(frozen=True, slots=True)
class RaydiumSwapStateFinalizationResult:
    pool: RaydiumClmmPoolState
    observation: RaydiumClmmObservationState
    pool_finalization: RaydiumPoolPostSwapFinalizationResult
    observation_updated: bool


def finalize_raydium_swap_state_bundle(
    pool: RaydiumClmmPoolState,
    observation: RaydiumClmmObservationState,
    iteration: RaydiumClmmSwapInnerIterationResult,
    *,
    block_timestamp: int,
    zero_for_one: bool,
) -> RaydiumSwapStateFinalizationResult:
    """Mirror swap.rs:798-821; record the previous tick before applying final PoolState."""
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumSwapStateFinalizationError("decoded Raydium PoolState is required")
    if not isinstance(observation, RaydiumClmmObservationState):
        raise RaydiumSwapStateFinalizationError("decoded Raydium ObservationState is required")
    if observation.address != pool.observation_key or observation.pool_id != pool.pool_address:
        raise RaydiumSwapStateFinalizationError("ObservationState identity does not match PoolState")
    try:
        original_bytes = encode_raydium_clmm_observation_state(observation)
    except RaydiumClmmObservationError as exc:
        raise RaydiumSwapStateFinalizationError(f"ObservationState serialization failed: {exc}") from exc
    if hashlib.sha256(original_bytes).hexdigest() != observation.data_sha256:
        raise RaydiumSwapStateFinalizationError("ObservationState source hash does not match its decoded fields")

    if iteration.tick != pool.tick_current:
        try:
            next_observation = apply_raydium_observation_update(
                observation,
                block_timestamp=block_timestamp,
                tick=pool.tick_current,
            )
        except RaydiumClmmObservationError as exc:
            raise RaydiumSwapStateFinalizationError(f"ObservationState update failed: {exc}") from exc
    else:
        next_observation = observation

    try:
        pool_result = finalize_raydium_pool_after_swap(
            pool,
            iteration,
            zero_for_one=zero_for_one,
        )
    except RaydiumPoolPostSwapFinalizationError as exc:
        raise RaydiumSwapStateFinalizationError(f"PoolState finalization failed: {exc}") from exc

    try:
        next_bytes = encode_raydium_clmm_observation_state(next_observation)
    except RaydiumClmmObservationError as exc:
        raise RaydiumSwapStateFinalizationError(f"final ObservationState serialization failed: {exc}") from exc
    if hashlib.sha256(next_bytes).hexdigest() != next_observation.data_sha256:
        raise RaydiumSwapStateFinalizationError("final ObservationState hash does not match serialized bytes")

    return RaydiumSwapStateFinalizationResult(
        pool=pool_result.pool,
        observation=next_observation,
        pool_finalization=pool_result,
        observation_updated=next_observation is not observation,
    )
