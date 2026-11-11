"""Apply and reconcile classic SPL token-vault deltas for a completed Raydium swap."""
from __future__ import annotations

from dataclasses import dataclass, replace

from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState
from veltrix.raydium_clmm_swap_loop_settlement import RaydiumSwapLoopSettlementResult
from veltrix.raydium_clmm_swap_transaction_settlement import (
    RaydiumSwapSettlementError,
    RaydiumVaultBalanceReconciliation,
    reconcile_raydium_swap_vault_balances,
)
from veltrix.raydium_clmm_vaults import (
    CLASSIC_SPL_TOKEN_PROGRAM_ID,
    RaydiumClmmVaultAccount,
    RaydiumClmmVaultError,
    RaydiumClmmVaultSnapshot,
    apply_raydium_clmm_vault_balance_delta,
    encode_raydium_clmm_vault_token_account,
)


class RaydiumSwapVaultSettlementError(ValueError):
    """Raised when classic SPL vault post-swap balances cannot be applied or reconciled."""


@dataclass(frozen=True, slots=True)
class RaydiumSwapVaultSettlementResult:
    expected_post_state: RaydiumClmmVaultSnapshot
    token0_vault_before: int
    token0_vault_after: int
    token1_vault_before: int
    token1_vault_after: int


@dataclass(frozen=True, slots=True)
class RaydiumSwapVaultReconciliationResult:
    reconciliation: RaydiumVaultBalanceReconciliation
    before: RaydiumClmmVaultSnapshot
    after: RaydiumClmmVaultSnapshot


def _vault_roles(
    snapshot: RaydiumClmmVaultSnapshot,
    pool: RaydiumClmmPoolState,
) -> tuple[RaydiumClmmVaultAccount, RaydiumClmmVaultAccount]:
    if not isinstance(snapshot, RaydiumClmmVaultSnapshot):
        raise RaydiumSwapVaultSettlementError("verified Raydium vault snapshot is required")
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumSwapVaultSettlementError("decoded Raydium PoolState is required")
    if not isinstance(snapshot.vaults, tuple) or len(snapshot.vaults) != 2:
        raise RaydiumSwapVaultSettlementError("single-hop Raydium route must have exactly two pool vaults")
    by_address = {vault.address: vault for vault in snapshot.vaults}
    if len(by_address) != 2:
        raise RaydiumSwapVaultSettlementError("vault snapshot contains duplicate account addresses")
    token0 = by_address.get(pool.token_vault_0)
    token1 = by_address.get(pool.token_vault_1)
    if token0 is None or token1 is None:
        raise RaydiumSwapVaultSettlementError("vault snapshot does not contain both PoolState vault addresses")
    for vault, expected_address, expected_mint in (
        (token0, pool.token_vault_0, pool.token_mint_0),
        (token1, pool.token_vault_1, pool.token_mint_1),
    ):
        if (
            vault.address != expected_address
            or vault.pool_address != pool.pool_address
            or vault.mint != expected_mint
            or vault.token_program_id != CLASSIC_SPL_TOKEN_PROGRAM_ID
            or vault.token_authority != pool.pool_address
            or vault.state != 1
        ):
            raise RaydiumSwapVaultSettlementError("vault account identity, mint, authority or classic-token role is invalid")
        try:
            encode_raydium_clmm_vault_token_account(vault)
        except RaydiumClmmVaultError as exc:
            raise RaydiumSwapVaultSettlementError(f"vault source-byte validation failed: {exc}") from exc
    return token0, token1


def _validate_snapshot_binding(
    snapshot: RaydiumClmmVaultSnapshot,
    loop_settlement: RaydiumSwapLoopSettlementResult,
) -> None:
    tick_snapshot = loop_settlement.loop.tick_array_snapshot
    if (
        snapshot.route_message_sha256 != tick_snapshot.route_message_sha256
        or snapshot.simulation_response_id != tick_snapshot.simulation_response_id
        or snapshot.simulation_context_slot != tick_snapshot.simulation_context_slot
        or snapshot.provider_id != tick_snapshot.provider_id
        or snapshot.cluster != tick_snapshot.cluster
        or snapshot.genesis_hash != tick_snapshot.genesis_hash
        or snapshot.context_slot < snapshot.simulation_context_slot
        or tick_snapshot.context_slot < tick_snapshot.simulation_context_slot
    ):
        raise RaydiumSwapVaultSettlementError("vault and CPI tick-array snapshots differ in route/simulation/provider provenance")


def apply_raydium_clmm_swap_vault_settlement(
    before: RaydiumClmmVaultSnapshot,
    loop_settlement: RaydiumSwapLoopSettlementResult,
) -> RaydiumSwapVaultSettlementResult:
    """Create the exact expected post-transfer vault models from pre-swap balances."""
    if not isinstance(loop_settlement, RaydiumSwapLoopSettlementResult):
        raise RaydiumSwapVaultSettlementError("completed outer-loop settlement result is required")
    loop = loop_settlement.loop
    settlement = loop_settlement.settlement
    if loop.swap_complete is not True or loop.final_iteration.swap_complete is not True:
        raise RaydiumSwapVaultSettlementError("vault settlement requires a complete swap")
    if settlement.transfer_fee_0 != 0 or settlement.transfer_fee_1 != 0:
        raise RaydiumSwapVaultSettlementError("non-zero transfer-fee extensions are outside the pinned classic-token swap path")
    _validate_snapshot_binding(before, loop_settlement)
    token0, token1 = _vault_roles(before, loop.pool)
    amount0 = settlement.amounts.amount_0
    amount1 = settlement.amounts.amount_1
    if isinstance(amount0, bool) or not isinstance(amount0, int) or amount0 <= 0:
        raise RaydiumSwapVaultSettlementError("settled token0 amount must be a positive integer")
    if isinstance(amount1, bool) or not isinstance(amount1, int) or amount1 <= 0:
        raise RaydiumSwapVaultSettlementError("settled token1 amount must be a positive integer")

    if loop.zero_for_one:
        delta0, delta1 = amount0, -amount1
    else:
        delta0, delta1 = -amount0, amount1
    try:
        updated0 = apply_raydium_clmm_vault_balance_delta(token0, delta_raw=delta0)
        updated1 = apply_raydium_clmm_vault_balance_delta(token1, delta_raw=delta1)
    except RaydiumClmmVaultError as exc:
        raise RaydiumSwapVaultSettlementError(f"expected vault balance update failed: {exc}") from exc

    updated_vaults = tuple(
        updated0 if vault.address == updated0.address else
        updated1 if vault.address == updated1.address else vault
        for vault in before.vaults
    )
    expected = replace(before, vaults=updated_vaults)
    return RaydiumSwapVaultSettlementResult(
        expected_post_state=expected,
        token0_vault_before=token0.amount_raw,
        token0_vault_after=updated0.amount_raw,
        token1_vault_before=token1.amount_raw,
        token1_vault_after=updated1.amount_raw,
    )


def reconcile_raydium_clmm_swap_vault_snapshots(
    before: RaydiumClmmVaultSnapshot,
    after: RaydiumClmmVaultSnapshot,
    loop_settlement: RaydiumSwapLoopSettlementResult,
) -> RaydiumSwapVaultReconciliationResult:
    """Verify actual post-CPI vault snapshots against the completed swap's transfer amounts."""
    if not isinstance(loop_settlement, RaydiumSwapLoopSettlementResult):
        raise RaydiumSwapVaultSettlementError("completed outer-loop settlement result is required")
    _validate_snapshot_binding(before, loop_settlement)
    if (
        after.route_message_sha256 != before.route_message_sha256
        or after.simulation_response_id != before.simulation_response_id
        or after.simulation_context_slot != before.simulation_context_slot
        or after.provider_id != before.provider_id
        or after.cluster != before.cluster
        or after.genesis_hash != before.genesis_hash
        or after.context_slot < before.context_slot
        or after.context_slot < after.simulation_context_slot
    ):
        raise RaydiumSwapVaultSettlementError("post-swap vault snapshot provenance or context slot is inconsistent")
    if (
        loop_settlement.settlement.transfer_fee_0 != 0
        or loop_settlement.settlement.transfer_fee_1 != 0
    ):
        raise RaydiumSwapVaultSettlementError("non-zero transfer-fee extensions are outside the pinned classic-token swap path")
    token0_before, token1_before = _vault_roles(before, loop_settlement.loop.pool)
    token0_after, token1_after = _vault_roles(after, loop_settlement.loop.pool)
    if (
        token0_before.address != token0_after.address
        or token1_before.address != token1_after.address
        or token0_before.mint != token0_after.mint
        or token1_before.mint != token1_after.mint
    ):
        raise RaydiumSwapVaultSettlementError("post-swap vault account roles differ from the pre-swap snapshot")
    try:
        reconciliation = reconcile_raydium_swap_vault_balances(
            zero_for_one=loop_settlement.loop.zero_for_one,
            amount_0=loop_settlement.settlement.amounts.amount_0,
            amount_1=loop_settlement.settlement.amounts.amount_1,
            token0_vault_before=token0_before.amount_raw,
            token0_vault_after=token0_after.amount_raw,
            token1_vault_before=token1_before.amount_raw,
            token1_vault_after=token1_after.amount_raw,
        )
    except RaydiumSwapSettlementError as exc:
        raise RaydiumSwapVaultSettlementError(f"post-swap vault delta mismatch: {exc}") from exc
    return RaydiumSwapVaultReconciliationResult(
        reconciliation=reconciliation,
        before=before,
        after=after,
    )
