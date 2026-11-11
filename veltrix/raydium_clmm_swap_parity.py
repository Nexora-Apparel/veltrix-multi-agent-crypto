"""Exact serialized post-state and pool-vault reconciliation for Raydium CLMM.

Compares local source-pinned replay bytes with account data returned by the same
Solana simulateTransaction context, then reconciles both pool vault token deltas
against the swap settlement. This is evidence evaluation only, never execution.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RaydiumClmmPoolState,
)
from veltrix.raydium_clmm_swap_finalization import (
    RaydiumSerializedSwapAccount,
    RaydiumSwapFinalizedState,
)
from veltrix.raydium_clmm_swap_loop_execution import RaydiumSwapLoopExecution
from veltrix.raydium_clmm_tick_array_mutation import RaydiumClmmTickArrayBitmapExtension
from veltrix.raydium_clmm_tick_arrays import RaydiumClmmTickArray
from veltrix.raydium_clmm_vaults import (
    CLASSIC_SPL_TOKEN_PROGRAM_ID,
    RaydiumClmmVaultSnapshot,
)
from veltrix.simulation_gate import SimulatedAccountState, SimulationEvidence


class RaydiumSwapParityError(ValueError):
    """Raised when simulated post-state or pool-vault deltas differ from replay."""


@dataclass(frozen=True, slots=True)
class RaydiumAccountParity:
    address: str
    pre_data_sha256: str
    expected_post_sha256: str
    observed_post_sha256: str
    exact_post_state_match: bool


@dataclass(frozen=True, slots=True)
class RaydiumVaultReconciliation:
    address: str
    mint: str
    pre_amount_raw: int
    expected_delta_raw: int
    expected_post_amount_raw: int
    observed_post_amount_raw: int
    reconciled: bool


@dataclass(frozen=True, slots=True)
class RaydiumSwapParityReport:
    passed: bool
    message_sha256: str
    simulation_response_id: str
    context_slot: int
    account_comparisons: tuple[RaydiumAccountParity, ...]
    vault_reconciliations: tuple[RaydiumVaultReconciliation, ...]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _expected_account_pairs(
    final: RaydiumSwapFinalizedState,
    *,
    pool_data: bytes,
    observation_data: bytes,
    tick_array_data: tuple[bytes, ...],
    bitmap_extension_data: bytes | None,
) -> tuple[tuple[RaydiumSerializedSwapAccount, bytes], ...]:
    if len(final.tick_array_accounts) != len(tick_array_data):
        raise RaydiumSwapParityError("initial and final TickArray account counts differ")
    pairs: list[tuple[RaydiumSerializedSwapAccount, bytes]] = [
        (final.pool_account, pool_data),
        (final.observation_account, observation_data),
    ]
    pairs.extend(zip(final.tick_array_accounts, tick_array_data))
    if final.bitmap_extension_account is not None:
        if bitmap_extension_data is None:
            raise RaydiumSwapParityError("original bitmap-extension bytes are required for post-state parity")
        pairs.append((final.bitmap_extension_account, bitmap_extension_data))
    elif bitmap_extension_data is not None:
        raise RaydiumSwapParityError("bitmap-extension source bytes were supplied but no final extension exists")
    addresses = [account.address for account, _ in pairs]
    if len(addresses) != len(set(addresses)):
        raise RaydiumSwapParityError("finalized Raydium account set contains duplicate addresses")
    return tuple(pairs)


def reconcile_raydium_swap_simulation(
    initial_pool: RaydiumClmmPoolState,
    execution: RaydiumSwapLoopExecution,
    finalized: RaydiumSwapFinalizedState,
    simulation: SimulationEvidence,
    vault_snapshot: RaydiumClmmVaultSnapshot,
    *,
    expected_message_sha256: str,
    initial_pool_account_data: bytes,
    initial_observation_account_data: bytes,
    initial_tick_array_account_data: tuple[bytes, ...],
    initial_bitmap_extension_account_data: bytes | None = None,
) -> RaydiumSwapParityReport:
    """Require byte-identical simulated account post-state and exact vault deltas."""
    if not isinstance(initial_pool, RaydiumClmmPoolState):
        raise RaydiumSwapParityError("decoded original Raydium PoolState is required")
    if not isinstance(execution, RaydiumSwapLoopExecution) or execution.complete is not True:
        raise RaydiumSwapParityError("a fully terminated swap-loop replay is required")
    if not isinstance(finalized, RaydiumSwapFinalizedState):
        raise RaydiumSwapParityError("serialized Raydium final state is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumSwapParityError("same-context Solana simulation evidence is required")
    if not isinstance(vault_snapshot, RaydiumClmmVaultSnapshot):
        raise RaydiumSwapParityError("verified Raydium pool-vault snapshot is required")
    if (
        not isinstance(expected_message_sha256, str)
        or len(expected_message_sha256) != 64
        or any(c not in "0123456789abcdef" for c in expected_message_sha256)
        or simulation.message_sha256 != expected_message_sha256
        or vault_snapshot.route_message_sha256 != expected_message_sha256
    ):
        raise RaydiumSwapParityError("simulation and vault evidence are not bound to the expected transaction message")
    if simulation.simulation_error is not None:
        raise RaydiumSwapParityError("Solana transaction simulation returned an execution error")
    if (
        vault_snapshot.simulation_response_id != simulation.response_id
        or vault_snapshot.simulation_context_slot != simulation.context_slot
        or vault_snapshot.provider_id != simulation.provider_id
        or vault_snapshot.cluster != simulation.cluster
        or vault_snapshot.genesis_hash != simulation.genesis_hash
        or vault_snapshot.context_slot < simulation.context_slot
    ):
        raise RaydiumSwapParityError("vault and transaction simulation provenance or context slot is inconsistent")

    expected_accounts = _expected_account_pairs(
        finalized,
        pool_data=initial_pool_account_data,
        observation_data=initial_observation_account_data,
        tick_array_data=initial_tick_array_account_data,
        bitmap_extension_data=initial_bitmap_extension_account_data,
    )
    expected_by_address = {account.address: (account, pre_data) for account, pre_data in expected_accounts}
    observed_accounts = {}
    for item in simulation.account_states:
        if not isinstance(item, SimulatedAccountState):
            raise RaydiumSwapParityError("simulation contains an invalid observed account record")
        if item.address in observed_accounts:
            raise RaydiumSwapParityError("simulation contains duplicate observed account addresses")
        observed_accounts[item.address] = item

    comparisons = []
    for address, (expected, pre_data) in expected_by_address.items():
        observed = observed_accounts.get(address)
        if observed is None:
            raise RaydiumSwapParityError(f"simulation omitted required post-state account {address}")
        if (
            observed.pre_owner != RAYDIUM_CLMM_PROGRAM_ID
            or observed.post_owner != RAYDIUM_CLMM_PROGRAM_ID
            or observed.pre_executable is not False
            or observed.post_executable is not False
        ):
            raise RaydiumSwapParityError(f"simulated Raydium account {address} has invalid owner or executable state")
        if observed.pre_data != pre_data or observed.pre_data_sha256 != _sha256(pre_data):
            raise RaydiumSwapParityError(f"simulated pre-state bytes for {address} differ from the replay source snapshot")
        if observed.post_data != expected.data or observed.post_data_sha256 != expected.data_sha256:
            raise RaydiumSwapParityError(f"simulated post-state bytes for {address} differ from serialized replay")
        comparisons.append(RaydiumAccountParity(
            address=address,
            pre_data_sha256=observed.pre_data_sha256,
            expected_post_sha256=expected.data_sha256,
            observed_post_sha256=observed.post_data_sha256,
            exact_post_state_match=True,
        ))

    if (
        finalized.pool.pool_address != initial_pool.pool_address
        or finalized.pool.token_vault_0 != initial_pool.token_vault_0
        or finalized.pool.token_vault_1 != initial_pool.token_vault_1
        or finalized.pool.token_mint_0 != initial_pool.token_mint_0
        or finalized.pool.token_mint_1 != initial_pool.token_mint_1
    ):
        raise RaydiumSwapParityError("finalized pool vault or mint identities differ from original PoolState")

    vaults = {vault.address: vault for vault in vault_snapshot.vaults}
    expected_vault_addresses = {initial_pool.token_vault_0, initial_pool.token_vault_1}
    if set(vaults) != expected_vault_addresses:
        raise RaydiumSwapParityError("vault snapshot must contain exactly the two canonical PoolState vaults")
    simulated_balances = {balance.token_account: balance for balance in simulation.token_balances}
    if len(simulated_balances) != len(simulation.token_balances):
        raise RaydiumSwapParityError("simulation contains duplicate token-balance records")

    amount0 = finalized.settlement.amount_0
    amount1 = finalized.settlement.amount_1
    if execution.state.zero_for_one:
        delta_by_address = {
            initial_pool.token_vault_0: amount0,
            initial_pool.token_vault_1: -amount1,
        }
    else:
        delta_by_address = {
            initial_pool.token_vault_0: -amount0,
            initial_pool.token_vault_1: amount1,
        }

    vault_results = []
    for address in (initial_pool.token_vault_0, initial_pool.token_vault_1):
        vault = vaults[address]
        balance = simulated_balances.get(address)
        if balance is None:
            raise RaydiumSwapParityError(f"simulation omitted token-balance evidence for pool vault {address}")
        expected_mint = (
            initial_pool.token_mint_0 if address == initial_pool.token_vault_0
            else initial_pool.token_mint_1
        )
        if (
            vault.pool_address != initial_pool.pool_address
            or vault.mint != expected_mint
            or vault.token_authority != initial_pool.pool_address
            or vault.token_program_id != CLASSIC_SPL_TOKEN_PROGRAM_ID
            or vault.state != 1
            or balance.mint != expected_mint
            or balance.owner != initial_pool.pool_address
            or balance.token_program_id != CLASSIC_SPL_TOKEN_PROGRAM_ID
        ):
            raise RaydiumSwapParityError(f"pool vault {address} mint, authority or token program differs from PoolState")
        if vault_snapshot.context_slot == simulation.context_slot and vault.amount_raw != balance.pre_amount_raw:
            raise RaydiumSwapParityError(f"same-slot vault snapshot balance differs from simulation pre-state for {address}")
        delta = delta_by_address[address]
        expected_post = balance.pre_amount_raw + delta
        if not 0 <= expected_post < (1 << 64):
            raise RaydiumSwapParityError(f"expected pool vault balance for {address} overflows or underflows u64")
        reconciled = balance.post_amount_raw == expected_post
        if not reconciled:
            raise RaydiumSwapParityError(
                f"pool vault {address} post balance differs from replay settlement: expected {expected_post}, observed {balance.post_amount_raw}"
            )
        vault_results.append(RaydiumVaultReconciliation(
            address=address,
            mint=expected_mint,
            pre_amount_raw=balance.pre_amount_raw,
            expected_delta_raw=delta,
            expected_post_amount_raw=expected_post,
            observed_post_amount_raw=balance.post_amount_raw,
            reconciled=True,
        ))

    return RaydiumSwapParityReport(
        passed=True,
        message_sha256=simulation.message_sha256,
        simulation_response_id=simulation.response_id,
        context_slot=simulation.context_slot,
        account_comparisons=tuple(comparisons),
        vault_reconciliations=tuple(vault_results),
    )
