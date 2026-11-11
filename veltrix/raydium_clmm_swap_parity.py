"""Exact serialized post-state and pool-vault reconciliation for Raydium CLMM.

Compares local source-pinned replay bytes with account data returned by the same
Solana simulateTransaction context, then reconciles both pool vault token deltas
against the swap settlement. This is evidence evaluation only, never execution.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib

from solders.pubkey import Pubkey

from veltrix.raydium_clmm_observation import RaydiumClmmObservationState
from veltrix.raydium_clmm_fee_accounting import settle_raydium_swap_amounts
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_CLMM_SOURCE_COMMIT,
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
from veltrix.simulation_gate import ExpectedTokenDelta, SimulatedAccountState, SimulationEvidence


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
class RaydiumSwapSimulationPlan:
    observed_account_addresses: tuple[str, ...]
    expected_token_deltas: tuple[ExpectedTokenDelta, ...]


@dataclass(frozen=True, slots=True)
class RaydiumSwapParityReport:
    passed: bool
    message_sha256: str
    simulation_response_id: str
    context_slot: int
    account_comparisons: tuple[RaydiumAccountParity, ...]
    vault_reconciliations: tuple[RaydiumVaultReconciliation, ...]


def _sha256(data: bytes) -> str:
    if not isinstance(data, bytes):
        raise RaydiumSwapParityError("account source data must be immutable bytes")
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


def build_raydium_swap_simulation_plan(
    initial_pool: RaydiumClmmPoolState,
    execution: RaydiumSwapLoopExecution,
    finalized: RaydiumSwapFinalizedState,
) -> RaydiumSwapSimulationPlan:
    """Build the exact RPC account-capture and vault-delta request for simulateTransaction."""
    if not isinstance(initial_pool, RaydiumClmmPoolState):
        raise RaydiumSwapParityError("decoded original Raydium PoolState is required")
    if not isinstance(execution, RaydiumSwapLoopExecution) or execution.complete is not True:
        raise RaydiumSwapParityError("a fully terminated swap-loop replay is required")
    if not isinstance(finalized, RaydiumSwapFinalizedState):
        raise RaydiumSwapParityError("serialized Raydium final state is required")
    if finalized.pool.pool_address != initial_pool.pool_address:
        raise RaydiumSwapParityError("finalized swap belongs to a different PoolState")
    addresses = [
        finalized.pool_account.address,
        finalized.observation_account.address,
        *(account.address for account in finalized.tick_array_accounts),
    ]
    if finalized.bitmap_extension_account is not None:
        addresses.append(finalized.bitmap_extension_account.address)
    try:
        if any(str(Pubkey.from_string(address)) != address for address in addresses):
            raise ValueError("non-canonical")
    except Exception as exc:
        raise RaydiumSwapParityError("simulation plan contains a non-canonical account address") from exc
    if len(addresses) != len(set(addresses)):
        raise RaydiumSwapParityError("simulation plan contains duplicate observed account addresses")

    amount0 = finalized.settlement.amount_0
    amount1 = finalized.settlement.amount_1
    if execution.state.zero_for_one:
        delta0, delta1 = amount0, -amount1
    else:
        delta0, delta1 = -amount0, amount1
    deltas = (
        ExpectedTokenDelta(
            token_account=initial_pool.token_vault_0,
            mint=initial_pool.token_mint_0,
            owner=initial_pool.pool_address,
            delta_raw=delta0,
        ),
        ExpectedTokenDelta(
            token_account=initial_pool.token_vault_1,
            mint=initial_pool.token_mint_1,
            owner=initial_pool.pool_address,
            delta_raw=delta1,
        ),
    )
    if set(addresses).intersection(item.token_account for item in deltas):
        raise RaydiumSwapParityError("pool vault token accounts must not be duplicated in observed account addresses")
    if len(addresses) + len(deltas) > 100:
        raise RaydiumSwapParityError("simulation plan exceeds the 100-account capture limit")
    return RaydiumSwapSimulationPlan(
        observed_account_addresses=tuple(addresses),
        expected_token_deltas=deltas,
    )


def reconcile_raydium_swap_simulation(
    initial_pool: RaydiumClmmPoolState,
    execution: RaydiumSwapLoopExecution,
    finalized: RaydiumSwapFinalizedState,
    simulation: SimulationEvidence,
    vault_snapshot: RaydiumClmmVaultSnapshot,
    *,
    expected_message_sha256: str,
    initial_pool_account_data: bytes,
    initial_observation: RaydiumClmmObservationState,
    initial_observation_account_data: bytes,
    initial_tick_arrays: tuple[RaydiumClmmTickArray, ...],
    initial_tick_array_account_data: tuple[bytes, ...],
    initial_bitmap_extension: RaydiumClmmTickArrayBitmapExtension | None = None,
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
    if initial_pool.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT:
        raise RaydiumSwapParityError("original PoolState source commit differs from the pinned Raydium version")
    if _sha256(initial_pool_account_data) != initial_pool.account_data_sha256:
        raise RaydiumSwapParityError("original PoolState bytes do not match the decoded source commitment")
    if not isinstance(initial_observation, RaydiumClmmObservationState):
        raise RaydiumSwapParityError("decoded original ObservationState is required")
    if (
        _sha256(initial_observation_account_data) != initial_observation.data_sha256
        or initial_observation.address != initial_pool.observation_key
        or initial_observation.pool_id != initial_pool.pool_address
    ):
        raise RaydiumSwapParityError("original ObservationState bytes or identity differ from the source snapshot")
    if (
        len(initial_tick_arrays) != len(initial_tick_array_account_data)
        or len(initial_tick_arrays) != len(finalized.tick_arrays)
        or len(initial_tick_arrays) != len(execution.tick_arrays)
    ):
        raise RaydiumSwapParityError("original, executed and finalized TickArray sequences must align")
    for index, (original, raw, executed, finalized_array) in enumerate(zip(
        initial_tick_arrays, initial_tick_array_account_data,
        execution.tick_arrays, finalized.tick_arrays,
    )):
        if (
            _sha256(raw) != original.data_sha256
            or original.address != executed.address
            or original.address != finalized_array.address
            or original.pool_address != initial_pool.pool_address
            or replace(finalized_array, data_sha256=executed.data_sha256) != executed
        ):
            raise RaydiumSwapParityError(f"TickArray {index} source commitment or final state lineage is inconsistent")
    if finalized.observation.address != initial_observation.address or finalized.observation.pool_id != initial_pool.pool_address:
        raise RaydiumSwapParityError("final ObservationState identity differs from the original pool")
    if (
        finalized.pool.tick_current != execution.state.tick
        or finalized.pool.sqrt_price_x64 != execution.state.sqrt_price_x64
        or finalized.pool.liquidity != execution.state.fee_state.liquidity
        or finalized.pool.tick_array_bitmap != execution.state.pool.tick_array_bitmap
        or finalized.pool.reward_infos != execution.updated_reward_infos
    ):
        raise RaydiumSwapParityError("final PoolState fields differ from the completed swap-loop state")

    fee_on_token0 = (
        execution.state.zero_for_one if initial_pool.fee_on == 0
        else initial_pool.fee_on == 1 if initial_pool.fee_on in (1, 2)
        else None
    )
    if fee_on_token0 is None:
        raise RaydiumSwapParityError("original PoolState fee_on mode is invalid")
    expected_settlement = settle_raydium_swap_amounts(
        execution.state.fee_state,
        amount_specified=execution.state.amount_specified,
        zero_for_one=execution.state.zero_for_one,
        is_base_input=execution.state.is_base_input,
        fee_on_token0=fee_on_token0,
    )
    if finalized.settlement != expected_settlement:
        raise RaydiumSwapParityError("serialized finalization settlement differs from completed swap state")

    if (
        (initial_bitmap_extension is None) != (initial_bitmap_extension_account_data is None)
        or (initial_bitmap_extension is None) != (finalized.bitmap_extension is None)
    ):
        raise RaydiumSwapParityError("original and finalized bitmap-extension evidence is incomplete")
    if initial_bitmap_extension is not None:
        if (
            _sha256(initial_bitmap_extension_account_data) != initial_bitmap_extension.data_sha256
            or finalized.bitmap_extension.address != initial_bitmap_extension.address
            or finalized.bitmap_extension.pool_address != initial_pool.pool_address
        ):
            raise RaydiumSwapParityError("bitmap-extension source commitment or final identity is inconsistent")

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

    if (
        finalized.pool.account_data_sha256 != finalized.pool_account.data_sha256
        or finalized.observation.data_sha256 != finalized.observation_account.data_sha256
        or tuple(item.data_sha256 for item in finalized.tick_arrays)
        != tuple(item.data_sha256 for item in finalized.tick_array_accounts)
    ):
        raise RaydiumSwapParityError("finalized account objects and serialized SHA-256 commitments differ")
    if finalized.bitmap_extension is not None:
        if (
            finalized.bitmap_extension_account is None
            or finalized.bitmap_extension.data_sha256 != finalized.bitmap_extension_account.data_sha256
            or execution.bitmap_extension is None
            or replace(
                finalized.bitmap_extension,
                data_sha256=execution.bitmap_extension.data_sha256,
            ) != execution.bitmap_extension
        ):
            raise RaydiumSwapParityError("final bitmap-extension state differs from the executed mutation lineage")
    elif execution.bitmap_extension is not None:
        raise RaydiumSwapParityError("execution bitmap-extension state is missing from serialized finalization")
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
    if len(vaults) != 2 or len(vault_snapshot.vaults) != 2 or set(vaults) != expected_vault_addresses:
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
