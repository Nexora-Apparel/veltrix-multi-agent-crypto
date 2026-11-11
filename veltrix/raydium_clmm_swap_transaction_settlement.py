"""Exact swap amount settlement, slippage checks and vault-delta reconciliation."""
from __future__ import annotations

from dataclasses import dataclass

from veltrix.raydium_clmm_fee_accounting import (
    RaydiumFeeAccountingError,
    RaydiumSwapSettlement,
    settle_raydium_swap_amounts,
)
from veltrix.raydium_clmm_observation import RaydiumClmmObservationState
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState
from veltrix.raydium_clmm_swap_inner_iteration import RaydiumClmmSwapInnerIterationResult
from veltrix.raydium_clmm_swap_state_finalization import (
    RaydiumSwapStateFinalizationError,
    RaydiumSwapStateFinalizationResult,
    finalize_raydium_swap_state_bundle,
)


class RaydiumSwapSettlementError(ValueError):
    """Raised when exact swap settlement or vault reconciliation fails."""


@dataclass(frozen=True, slots=True)
class RaydiumSwapTransactionSettlement:
    state: RaydiumSwapStateFinalizationResult
    amounts: RaydiumSwapSettlement
    amount_returned: int
    transfer_fee_0: int
    transfer_fee_1: int


@dataclass(frozen=True, slots=True)
class RaydiumVaultBalanceReconciliation:
    input_vault_delta: int
    output_vault_delta: int
    input_amount_expected: int
    output_amount_expected: int


def _u64(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << 64):
        raise RaydiumSwapSettlementError(f"{field} must fit u64")


def _u128(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << 128):
        raise RaydiumSwapSettlementError(f"{field} must fit u128")


def _fee_on_token0(pool: RaydiumClmmPoolState, zero_for_one: bool) -> bool:
    if pool.fee_on == 0:
        return zero_for_one
    if pool.fee_on == 1:
        return True
    if pool.fee_on == 2:
        return False
    raise RaydiumSwapSettlementError("PoolState fee_on mode must be 0, 1 or 2")


def settle_raydium_clmm_swap_transaction(
    pool: RaydiumClmmPoolState,
    observation: RaydiumClmmObservationState,
    iteration: RaydiumClmmSwapInnerIterationResult,
    *,
    amount_specified: int,
    zero_for_one: bool,
    is_base_input: bool,
    sqrt_price_limit_x64: int,
    other_amount_threshold: int,
    block_timestamp: int,
) -> RaydiumSwapTransactionSettlement:
    """Mirror exact_internal settlement and swap() slippage threshold checks."""
    if not isinstance(pool, RaydiumClmmPoolState):
        raise RaydiumSwapSettlementError("decoded Raydium PoolState is required")
    for value, field in (
        (amount_specified, "amount_specified"),
        (other_amount_threshold, "other_amount_threshold"),
    ):
        _u64(value, field)
    _u128(sqrt_price_limit_x64, "sqrt_price_limit_x64")
    if not isinstance(zero_for_one, bool) or not isinstance(is_base_input, bool):
        raise RaydiumSwapSettlementError("swap direction and input mode must be boolean")
    if amount_specified == 0:
        raise RaydiumSwapSettlementError("amount_specified must be non-zero")

    try:
        amounts = settle_raydium_swap_amounts(
            iteration.fee_state,
            amount_specified=amount_specified,
            zero_for_one=zero_for_one,
            is_base_input=is_base_input,
            fee_on_token0=_fee_on_token0(pool, zero_for_one),
        )
    except RaydiumFeeAccountingError as exc:
        raise RaydiumSwapSettlementError(f"swap amount settlement failed: {exc}") from exc

    if amounts.amount_0 == 0 or amounts.amount_1 == 0:
        raise RaydiumSwapSettlementError("swap input and output amounts must both be non-zero")

    if zero_for_one:
        if iteration.sqrt_price_x64 > pool.sqrt_price_x64:
            raise RaydiumSwapSettlementError("zero-for-one swap increased the pool sqrt price")
        if sqrt_price_limit_x64 and iteration.sqrt_price_x64 < sqrt_price_limit_x64:
            raise RaydiumSwapSettlementError("zero-for-one swap crossed below its sqrt-price limit")
        input_amount, output_amount = amounts.amount_0, amounts.amount_1
        specified_consumed = amounts.amount_0 if is_base_input else amounts.amount_1
    else:
        if iteration.sqrt_price_x64 < pool.sqrt_price_x64:
            raise RaydiumSwapSettlementError("one-for-zero swap decreased the pool sqrt price")
        if sqrt_price_limit_x64 and iteration.sqrt_price_x64 > sqrt_price_limit_x64:
            raise RaydiumSwapSettlementError("one-for-zero swap crossed above its sqrt-price limit")
        input_amount, output_amount = amounts.amount_1, amounts.amount_0
        specified_consumed = amounts.amount_1 if is_base_input else amounts.amount_0

    if sqrt_price_limit_x64 == 0 and specified_consumed != amount_specified:
        raise RaydiumSwapSettlementError("partial fill is forbidden when no explicit sqrt-price limit is supplied")

    amount_returned = output_amount if is_base_input else input_amount
    if is_base_input:
        if amount_returned < other_amount_threshold:
            raise RaydiumSwapSettlementError("actual output is below other_amount_threshold")
    elif amount_returned > other_amount_threshold:
        raise RaydiumSwapSettlementError("actual input exceeds other_amount_threshold")

    try:
        state = finalize_raydium_swap_state_bundle(
            pool,
            observation,
            iteration,
            block_timestamp=block_timestamp,
            zero_for_one=zero_for_one,
        )
    except RaydiumSwapStateFinalizationError as exc:
        raise RaydiumSwapSettlementError(f"post-swap account finalization failed: {exc}") from exc

    # The pinned swap.rs emits zero transfer fees and invokes the classic token transfer helpers
    # without transfer-fee extensions for these swap legs.
    return RaydiumSwapTransactionSettlement(
        state=state,
        amounts=amounts,
        amount_returned=amount_returned,
        transfer_fee_0=0,
        transfer_fee_1=0,
    )


def reconcile_raydium_swap_vault_balances(
    *,
    zero_for_one: bool,
    amount_0: int,
    amount_1: int,
    token0_vault_before: int,
    token0_vault_after: int,
    token1_vault_before: int,
    token1_vault_after: int,
) -> RaydiumVaultBalanceReconciliation:
    """Verify post-CPI token-vault deltas against the pinned swap transfer amounts."""
    if not isinstance(zero_for_one, bool):
        raise RaydiumSwapSettlementError("swap direction must be boolean")
    for value, field in (
        (amount_0, "amount_0"),
        (amount_1, "amount_1"),
        (token0_vault_before, "token0_vault_before"),
        (token0_vault_after, "token0_vault_after"),
        (token1_vault_before, "token1_vault_before"),
        (token1_vault_after, "token1_vault_after"),
    ):
        _u64(value, field)

    if zero_for_one:
        if token0_vault_after < token0_vault_before:
            raise RaydiumSwapSettlementError("input token0 vault balance decreased")
        if token1_vault_before < token1_vault_after:
            raise RaydiumSwapSettlementError("output token1 vault balance increased")
        input_delta = token0_vault_after - token0_vault_before
        output_delta = token1_vault_before - token1_vault_after
        input_expected, output_expected = amount_0, amount_1
    else:
        if token1_vault_after < token1_vault_before:
            raise RaydiumSwapSettlementError("input token1 vault balance decreased")
        if token0_vault_before < token0_vault_after:
            raise RaydiumSwapSettlementError("output token0 vault balance increased")
        input_delta = token1_vault_after - token1_vault_before
        output_delta = token0_vault_before - token0_vault_after
        input_expected, output_expected = amount_1, amount_0

    if input_delta != input_expected:
        raise RaydiumSwapSettlementError("input vault balance delta differs from swap amount")
    if output_delta != output_expected:
        raise RaydiumSwapSettlementError("output vault balance delta differs from swap amount")

    return RaydiumVaultBalanceReconciliation(
        input_vault_delta=input_delta,
        output_vault_delta=output_delta,
        input_amount_expected=input_expected,
        output_amount_expected=output_expected,
    )
