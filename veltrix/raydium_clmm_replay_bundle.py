"""Immutable account-set binding for deterministic Raydium CLMM replay.

This validates one internally consistent, byte-committed account bundle. It
performs no RPC calls, signing, transaction construction, or execution.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from veltrix.raydium_clmm_observation import (
    RaydiumClmmObservationState,
    encode_raydium_clmm_observation_state,
)
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RaydiumClmmPoolState,
    encode_raydium_clmm_pool_state,
)
from veltrix.raydium_clmm_tick_array_mutation import (
    RaydiumClmmTickArrayBitmapExtension,
    encode_raydium_tick_array_bitmap_extension,
)
from veltrix.raydium_clmm_tick_arrays import (
    RaydiumClmmTickArray,
    encode_raydium_clmm_tick_array,
)
from veltrix.raydium_clmm_vaults import (
    RaydiumClmmVaultAccount,
    encode_raydium_clmm_vault_token_account,
)


class RaydiumReplayBundleError(ValueError):
    """Raised when replay accounts cannot be bound to one valid pool snapshot."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmReplayAccountCommitment:
    address: str
    account_kind: str
    data_sha256: str


@dataclass(frozen=True, slots=True)
class RaydiumClmmReplayAccountBundle:
    source_commit: str
    pool: RaydiumClmmPoolState
    tick_arrays: tuple[RaydiumClmmTickArray, ...]
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None
    observation: RaydiumClmmObservationState
    vaults: tuple[RaydiumClmmVaultAccount, ...]
    account_commitments: tuple[RaydiumClmmReplayAccountCommitment, ...]
    bundle_sha256: str
    execution_ready: bool = False


def _verify_serialized_account(address, kind, data, expected_hash, serializer):
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise RaydiumReplayBundleError(f"{kind} SHA-256 commitment is missing")
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise RaydiumReplayBundleError(f"{kind} bytes do not match their SHA-256 commitment")
    try:
        encoded = serializer()
    except Exception as exc:
        raise RaydiumReplayBundleError(f"{kind} failed canonical serialization: {exc}") from exc
    if encoded != data:
        raise RaydiumReplayBundleError(f"{kind} model fields differ from its committed account bytes")
    return RaydiumClmmReplayAccountCommitment(address, kind, expected_hash)


def build_raydium_clmm_replay_account_bundle(
    pool: RaydiumClmmPoolState,
    *,
    tick_arrays: tuple[RaydiumClmmTickArray, ...],
    observation: RaydiumClmmObservationState,
    vaults: tuple[RaydiumClmmVaultAccount, ...],
    bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None = None,
) -> RaydiumClmmReplayAccountBundle:
    """Bind the complete required account set to one canonical Raydium pool."""
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumReplayBundleError("decoded Raydium PoolState is required")
    if pool.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT:
        raise RaydiumReplayBundleError("PoolState source commit is not the pinned Raydium source")
    if not isinstance(tick_arrays, tuple) or not tick_arrays:
        raise RaydiumReplayBundleError("at least one decoded TickArrayState is required")
    if not isinstance(observation, RaydiumClmmObservationState):
        raise RaydiumReplayBundleError("decoded ObservationState is required")
    if not isinstance(vaults, tuple) or len(vaults) != 2:
        raise RaydiumReplayBundleError("exactly the two PoolState token vaults are required")

    commitments = [
        _verify_serialized_account(
            pool.pool_address,
            "PoolState",
            pool.account_data,
            pool.account_data_sha256,
            lambda: encode_raydium_clmm_pool_state(pool),
        )
    ]

    starts = set()
    addresses = {pool.pool_address}
    for tick_array in tick_arrays:
        if not isinstance(tick_array, RaydiumClmmTickArray):
            raise RaydiumReplayBundleError("tick-array set contains a malformed account")
        if tick_array.pool_address != pool.pool_address:
            raise RaydiumReplayBundleError("TickArrayState belongs to a different pool")
        if tick_array.tick_spacing != pool.tick_spacing:
            raise RaydiumReplayBundleError("TickArrayState tick spacing differs from PoolState")
        if tick_array.start_tick_index in starts or tick_array.address in addresses:
            raise RaydiumReplayBundleError("tick-array set contains duplicate starts or account addresses")
        starts.add(tick_array.start_tick_index)
        addresses.add(tick_array.address)
        commitments.append(
            _verify_serialized_account(
                tick_array.address,
                "TickArrayState",
                encode_raydium_clmm_tick_array(tick_array),
                tick_array.data_sha256,
                lambda item=tick_array: encode_raydium_clmm_tick_array(item),
            )
        )

    if bitmap_extension is not None:
        if bitmap_extension.pool_address != pool.pool_address:
            raise RaydiumReplayBundleError("bitmap extension belongs to a different pool")
        if bitmap_extension.address in addresses:
            raise RaydiumReplayBundleError("bitmap-extension address duplicates another account")
        addresses.add(bitmap_extension.address)
        commitments.append(
            _verify_serialized_account(
                bitmap_extension.address,
                "TickArrayBitmapExtension",
                encode_raydium_tick_array_bitmap_extension(bitmap_extension),
                bitmap_extension.data_sha256,
                lambda: encode_raydium_tick_array_bitmap_extension(bitmap_extension),
            )
        )

    if observation.pool_id != pool.pool_address:
        raise RaydiumReplayBundleError("ObservationState belongs to a different pool")
    if observation.address != pool.observation_key:
        raise RaydiumReplayBundleError("ObservationState address differs from PoolState.observation_key")
    if observation.address in addresses:
        raise RaydiumReplayBundleError("ObservationState address duplicates another account")
    addresses.add(observation.address)
    commitments.append(
        _verify_serialized_account(
            observation.address,
            "ObservationState",
            encode_raydium_clmm_observation_state(observation),
            observation.data_sha256,
            lambda: encode_raydium_clmm_observation_state(observation),
        )
    )

    expected_vaults = {
        pool.token_vault_0: pool.token_mint_0,
        pool.token_vault_1: pool.token_mint_1,
    }
    seen_vaults = set()
    for vault in vaults:
        if not isinstance(vault, RaydiumClmmVaultAccount):
            raise RaydiumReplayBundleError("vault set contains a malformed account")
        if vault.pool_address != pool.pool_address:
            raise RaydiumReplayBundleError("vault belongs to a different pool")
        if expected_vaults.get(vault.address) != vault.mint:
            raise RaydiumReplayBundleError("vault address/mint pair differs from PoolState")
        if vault.address in seen_vaults or vault.address in addresses:
            raise RaydiumReplayBundleError("vault set contains a duplicate account")
        if len(vault.account_data) != 165 or int.from_bytes(vault.account_data[64:72], "little") != vault.amount_raw:
            raise RaydiumReplayBundleError("vault amount differs from its source token-account bytes")
        seen_vaults.add(vault.address)
        addresses.add(vault.address)
        commitments.append(
            _verify_serialized_account(
                vault.address,
                "SPLTokenVault",
                encode_raydium_clmm_vault_token_account(vault),
                vault.data_sha256,
                lambda item=vault: encode_raydium_clmm_vault_token_account(item),
            )
        )
    if seen_vaults != set(expected_vaults):
        raise RaydiumReplayBundleError("vault set does not contain both PoolState token vaults")

    ordered = tuple(sorted(commitments, key=lambda item: (item.account_kind, item.address)))
    digest = hashlib.sha256()
    digest.update(b"VELTRIX-RAYDIUM-REPLAY-BUNDLE-v1\0")
    digest.update(RAYDIUM_CLMM_SOURCE_COMMIT.encode("ascii"))
    digest.update(pool.pool_address.encode("ascii"))
    for item in ordered:
        digest.update(item.account_kind.encode("ascii"))
        digest.update(b"\0")
        digest.update(item.address.encode("ascii"))
        digest.update(bytes.fromhex(item.data_sha256))
    return RaydiumClmmReplayAccountBundle(
        source_commit=RAYDIUM_CLMM_SOURCE_COMMIT,
        pool=pool,
        tick_arrays=tuple(sorted(tick_arrays, key=lambda item: item.start_tick_index)),
        bitmap_extension=bitmap_extension,
        observation=observation,
        vaults=tuple(sorted(vaults, key=lambda item: item.address)),
        account_commitments=ordered,
        bundle_sha256=digest.hexdigest(),
        execution_ready=False,
    )
