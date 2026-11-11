"""Compose a completed outer-loop replay with exact swap settlement."""
from __future__ import annotations

from dataclasses import dataclass

from veltrix.raydium_clmm_observation import RaydiumClmmObservationState
from veltrix.raydium_clmm_swap_loop import (
    RaydiumClmmSwapLoopResult,
)
from veltrix.raydium_clmm_swap_transaction_settlement import (
    RaydiumSwapSettlementError,
    RaydiumSwapTransactionSettlement,
    settle_raydium_clmm_swap_transaction,
)


class RaydiumSwapLoopSettlementError(ValueError):
    """Raised when an outer-loop result cannot be settled as a complete swap."""


@dataclass(frozen=True, slots=True)
class RaydiumSwapLoopSettlementResult:
    loop: RaydiumClmmSwapLoopResult
    settlement: RaydiumSwapTransactionSettlement


def settle_raydium_clmm_swap_loop(
    loop: RaydiumClmmSwapLoopResult,
    observation: RaydiumClmmObservationState,
    *,
    other_amount_threshold: int,
    block_timestamp: int,
) -> RaydiumSwapLoopSettlementResult:
    """Settle only the final whole-swap state returned by the outer loop."""
    if not isinstance(loop, RaydiumClmmSwapLoopResult):
        raise RaydiumSwapLoopSettlementError("completed Raydium outer-loop result is required")
    if loop.swap_complete is not True or loop.final_iteration.swap_complete is not True:
        raise RaydiumSwapLoopSettlementError("outer loop has not reached amount exhaustion or the user price limit")
    if loop.fee_state != loop.final_iteration.fee_state:
        raise RaydiumSwapLoopSettlementError("outer-loop fee state differs from its final inner iteration")
    try:
        settlement = settle_raydium_clmm_swap_transaction(
            loop.pool,
            observation,
            loop.final_iteration,
            amount_specified=loop.amount_specified,
            zero_for_one=loop.zero_for_one,
            is_base_input=loop.is_base_input,
            sqrt_price_limit_x64=loop.sqrt_price_limit_x64,
            other_amount_threshold=other_amount_threshold,
            block_timestamp=block_timestamp,
        )
    except RaydiumSwapSettlementError as exc:
        raise RaydiumSwapLoopSettlementError(f"final swap settlement failed: {exc}") from exc
    return RaydiumSwapLoopSettlementResult(loop=loop, settlement=settlement)
