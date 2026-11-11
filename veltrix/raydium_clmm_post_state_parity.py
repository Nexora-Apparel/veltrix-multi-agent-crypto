"""Independent simulated post-state parity and pool-vault reconciliation."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from solders.pubkey import Pubkey

from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
)
from veltrix.raydium_clmm_swap_finalization import RaydiumSwapFinalizationResult
from veltrix.raydium_clmm_vaults import (
    CLASSIC_SPL_TOKEN_PROGRAM_ID,
    TOKEN_ACCOUNT_INITIALIZED,
    RaydiumClmmVaultSnapshot,
)
from veltrix.simulation_gate import ExpectedTokenDelta, SimulationEvidence


class RaydiumSwapPostStateParityError(ValueError):
    """Raised when simulated account bytes or vault deltas do not match replay."""


@dataclass(frozen=True, slots=True)
class RaydiumSwapSimulationPlan:
    observed_account_addresses: tuple[str, ...]
    expected_token_deltas: tuple[ExpectedTokenDelta, ...]


@dataclass(frozen=True, slots=True)
class RaydiumSwapPostStateParityResult:
    passed: bool
    message_sha256: str
    provider_id: str
    cluster: str
    genesis_hash: str
    context_slot: int
    verified_account_count: int
    verified_vault_count: int
    amount_0_delta_raw: int
    amount_1_delta_raw: int
    execution_ready: bool = False


def build_raydium_swap_simulation_plan(
    finalization: RaydiumSwapFinalizationResult,
) -> RaydiumSwapSimulationPlan:
    """Build exact simulateTransaction account captures and pool-vault deltas."""
    if not isinstance(finalization, RaydiumSwapFinalizationResult):
        raise RaydiumSwapPostStateParityError("serialized Raydium swap finalization is required")
    pool = finalization.pool
    if not isinstance(finalization.zero_for_one, bool):
        raise RaydiumSwapPostStateParityError("finalized swap direction must be boolean")
    addresses = [
        pool.pool_address,
        finalization.observation.address,
        *(array.address for array in finalization.tick_arrays),
    ]
    if finalization.bitmap_extension is not None:
        addresses.append(finalization.bitmap_extension.address)
    try:
        if any(str(Pubkey.from_string(address)) != address for address in addresses):
            raise ValueError("non-canonical")
    except Exception as exc:
        raise RaydiumSwapPostStateParityError("simulation plan contains a non-canonical account address") from exc
    if len(addresses) != len(set(addresses)):
        raise RaydiumSwapPostStateParityError("simulation plan contains duplicate observed account addresses")

    amount0 = finalization.settlement.amount_0
    amount1 = finalization.settlement.amount_1
    delta0 = amount0 if finalization.zero_for_one else -amount0
    delta1 = -amount1 if finalization.zero_for_one else amount1
    expected_deltas = (
        ExpectedTokenDelta(
            token_account=pool.token_vault_0,
            mint=pool.token_mint_0,
            owner=pool.pool_address,
            delta_raw=delta0,
        ),
        ExpectedTokenDelta(
            token_account=pool.token_vault_1,
            mint=pool.token_mint_1,
            owner=pool.pool_address,
            delta_raw=delta1,
        ),
    )
    if set(addresses).intersection(item.token_account for item in expected_deltas):
        raise RaydiumSwapPostStateParityError("pool vaults must be tracked as token balances, not observed program accounts")
    if len(addresses) + len(expected_deltas) > 100:
        raise RaydiumSwapPostStateParityError("simulation account capture exceeds the 100-account limit")
    return RaydiumSwapSimulationPlan(
        observed_account_addresses=tuple(addresses),
        expected_token_deltas=expected_deltas,
    )


def verify_raydium_swap_post_state_parity(
    finalization: RaydiumSwapFinalizationResult,
    simulation: SimulationEvidence,
    vault_snapshot: RaydiumClmmVaultSnapshot,
    *,
    expected_message_sha256: str,
) -> RaydiumSwapPostStateParityResult:
    """Compare every replayed account byte and both pool-vault balance deltas.

    The simulation must capture PoolState, all CPI TickArrays, optional bitmap
    extension and ObservationState as observed accounts, and both pool vaults
    as tracked token balances in the same simulation context.
    """
    if not isinstance(finalization, RaydiumSwapFinalizationResult):
        raise RaydiumSwapPostStateParityError("completed serialized Raydium swap state is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumSwapPostStateParityError("independent Solana simulation evidence is required")
    if not isinstance(vault_snapshot, RaydiumClmmVaultSnapshot):
        raise RaydiumSwapPostStateParityError("verified Raydium pool-vault snapshot is required")
    if (
        not isinstance(expected_message_sha256, str)
        or len(expected_message_sha256) != 64
        or any(char not in "0123456789abcdef" for char in expected_message_sha256)
        or simulation.message_sha256 != expected_message_sha256
    ):
        raise RaydiumSwapPostStateParityError("simulation message digest differs from the exact replayed transaction")
    if simulation.simulation_error is not None:
        raise RaydiumSwapPostStateParityError("Solana simulation returned an execution error")
    if RAYDIUM_CLMM_PROGRAM_ID not in simulation.top_level_program_ids and (
        RAYDIUM_CLMM_PROGRAM_ID not in simulation.inner_program_ids
    ):
        raise RaydiumSwapPostStateParityError("simulation did not execute the pinned Raydium CLMM program")
    if not simulation.provider_id or not simulation.cluster or not simulation.genesis_hash:
        raise RaydiumSwapPostStateParityError("simulation provider/cluster provenance is incomplete")
    if (
        vault_snapshot.route_message_sha256 != expected_message_sha256
        or vault_snapshot.simulation_response_id != simulation.response_id
        or vault_snapshot.simulation_context_slot != simulation.context_slot
        or vault_snapshot.provider_id != simulation.provider_id
        or vault_snapshot.cluster != simulation.cluster
        or vault_snapshot.genesis_hash != simulation.genesis_hash
        or vault_snapshot.context_slot < simulation.context_slot
    ):
        raise RaydiumSwapPostStateParityError("pool-vault evidence is not bound to the same simulation/provider context")

    pool = finalization.pool
    if (
        finalization.pool_data != pool.raw_data
        or hashlib.sha256(finalization.pool_data).hexdigest() != pool.account_data_sha256
    ):
        raise RaydiumSwapPostStateParityError("final PoolState bytes or digest are inconsistent")
    if (
        len(finalization.tick_arrays) != len(finalization.tick_array_data)
        or len(finalization.tick_arrays) != len(finalization.source_tick_array_sha256)
    ):
        raise RaydiumSwapPostStateParityError("final TickArrayState data and source commitments are misaligned")
    if (
        finalization.observation_data != finalization.observation.raw_data
        or hashlib.sha256(finalization.observation_data).hexdigest() != finalization.observation.data_sha256
    ):
        raise RaydiumSwapPostStateParityError("final ObservationState bytes or digest are inconsistent")

    expected_accounts: list[tuple[str, str, bytes, str]] = [
        (
            pool.pool_address,
            finalization.source_pool_sha256,
            finalization.pool_data,
            "PoolState",
        )
    ]
    for index, (array, raw) in enumerate(zip(finalization.tick_arrays, finalization.tick_array_data)):
        if raw != array.raw_data or hashlib.sha256(raw).hexdigest() != array.data_sha256:
            raise RaydiumSwapPostStateParityError(f"final TickArrayState {index} bytes or digest are inconsistent")
        expected_accounts.append((
            array.address,
            finalization.source_tick_array_sha256[index],
            raw,
            f"TickArrayState[{index}]",
        ))
    if finalization.bitmap_extension is not None:
        if finalization.bitmap_extension_data is None or finalization.source_bitmap_extension_sha256 is None:
            raise RaydiumSwapPostStateParityError("final bitmap-extension bytes or source commitment are missing")
        if hashlib.sha256(finalization.bitmap_extension_data).hexdigest() != finalization.bitmap_extension.data_sha256:
            raise RaydiumSwapPostStateParityError("final bitmap-extension digest is inconsistent")
        expected_accounts.append((
            finalization.bitmap_extension.address,
            finalization.source_bitmap_extension_sha256,
            finalization.bitmap_extension_data,
            "TickArrayBitmapExtension",
        ))
    elif finalization.bitmap_extension_data is not None or finalization.source_bitmap_extension_sha256 is not None:
        raise RaydiumSwapPostStateParityError("bitmap-extension state and its account bytes disagree")
    expected_accounts.append((
        finalization.observation.address,
        finalization.source_observation_sha256,
        finalization.observation_data,
        "ObservationState",
    ))

    expected_addresses = [item[0] for item in expected_accounts]
    if len(set(expected_addresses)) != len(expected_addresses):
        raise RaydiumSwapPostStateParityError("serialized post-state contains duplicate account addresses")
    observed = {}
    for account in simulation.account_states:
        if account.address in observed:
            raise RaydiumSwapPostStateParityError("simulation contains duplicate observed account states")
        observed[account.address] = account

    for address, source_hash, expected_post, label in expected_accounts:
        account = observed.get(address)
        if account is None:
            raise RaydiumSwapPostStateParityError(f"simulation omitted {label} post-state bytes")
        if (
            account.pre_owner != RAYDIUM_CLMM_PROGRAM_ID
            or account.post_owner != RAYDIUM_CLMM_PROGRAM_ID
            or account.pre_executable
            or account.post_executable
            or account.pre_lamports <= 0
            or account.post_lamports <= 0
        ):
            raise RaydiumSwapPostStateParityError(f"{label} owner, executable status or lamports are invalid")
        if account.pre_data_sha256 != source_hash:
            raise RaydiumSwapPostStateParityError(f"{label} pre-state does not match the replay's source commitment")
        if account.post_data != expected_post:
            raise RaydiumSwapPostStateParityError(f"{label} simulated post-state bytes differ from deterministic replay")
        if account.post_data_sha256 != hashlib.sha256(expected_post).hexdigest():
            raise RaydiumSwapPostStateParityError(f"{label} simulated post-state digest is inconsistent")

    expected_vaults = {
        pool.token_vault_0: pool.token_mint_0,
        pool.token_vault_1: pool.token_mint_1,
    }
    pool_vaults = [
        item for item in vault_snapshot.vaults
        if item.pool_address == pool.pool_address
    ]
    if len(pool_vaults) != 2 or {item.address for item in pool_vaults} != set(expected_vaults):
        raise RaydiumSwapPostStateParityError("verified vault snapshot does not contain exactly both pool vaults")
    for vault in pool_vaults:
        if (
            vault.mint != expected_vaults[vault.address]
            or vault.token_authority != pool.pool_address
            or vault.token_program_id != CLASSIC_SPL_TOKEN_PROGRAM_ID
            or vault.state != TOKEN_ACCOUNT_INITIALIZED
        ):
            raise RaydiumSwapPostStateParityError("pool-vault mint, authority, token program or initialized state is invalid")

    token_balances = {}
    for balance in simulation.token_balances:
        if balance.token_account in token_balances:
            raise RaydiumSwapPostStateParityError("simulation contains duplicate token-account balances")
        token_balances[balance.token_account] = balance
    observed_deltas = {}
    for address, mint in expected_vaults.items():
        balance = token_balances.get(address)
        if balance is None:
            raise RaydiumSwapPostStateParityError("simulation did not track both Raydium pool vault token balances")
        if (
            balance.mint != mint
            or balance.owner != pool.pool_address
            or balance.token_program_id != CLASSIC_SPL_TOKEN_PROGRAM_ID
        ):
            raise RaydiumSwapPostStateParityError("simulated pool-vault token balance identity differs from the verified vault")
        if balance.pre_amount_raw > (1 << 64) - 1 or balance.post_amount_raw > (1 << 64) - 1:
            raise RaydiumSwapPostStateParityError("simulated pool-vault balance exceeds u64")
        if vault_snapshot.context_slot == simulation.context_slot:
            snapshot_vault = next(item for item in pool_vaults if item.address == address)
            if snapshot_vault.amount_raw != balance.pre_amount_raw:
                raise RaydiumSwapPostStateParityError("same-slot vault snapshot differs from simulation pre-state balance")
        observed_deltas[address] = balance.post_amount_raw - balance.pre_amount_raw

    expected_delta_0 = finalization.settlement.amount_0 if finalization.zero_for_one else -finalization.settlement.amount_0
    expected_delta_1 = -finalization.settlement.amount_1 if finalization.zero_for_one else finalization.settlement.amount_1
    if observed_deltas[pool.token_vault_0] != expected_delta_0:
        raise RaydiumSwapPostStateParityError("token-0 pool-vault delta differs from replay settlement")
    if observed_deltas[pool.token_vault_1] != expected_delta_1:
        raise RaydiumSwapPostStateParityError("token-1 pool-vault delta differs from replay settlement")

    return RaydiumSwapPostStateParityResult(
        passed=True,
        message_sha256=expected_message_sha256,
        provider_id=simulation.provider_id,
        cluster=simulation.cluster,
        genesis_hash=simulation.genesis_hash,
        context_slot=simulation.context_slot,
        verified_account_count=len(expected_accounts),
        verified_vault_count=2,
        amount_0_delta_raw=observed_deltas[pool.token_vault_0],
        amount_1_delta_raw=observed_deltas[pool.token_vault_1],
        execution_ready=False,
    )
