"""Cross-bind the simulated Raydium CPI, full swap replay and vault post-state."""
from __future__ import annotations

from dataclasses import dataclass

from veltrix.raydium_clmm_cpi import (
    RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RaydiumClmmCpiResult,
)
from veltrix.raydium_clmm_pool_state import RaydiumClmmPoolState
from veltrix.raydium_clmm_swap_loop_settlement import RaydiumSwapLoopSettlementResult
from veltrix.raydium_clmm_swap_vault_settlement import RaydiumSwapVaultSettlementResult
from veltrix.raydium_clmm_vaults import RaydiumClmmVaultSnapshot


class RaydiumSwapReplayPreflightError(ValueError):
    """Raised when simulated CPI, deterministic replay and vault post-state diverge."""


@dataclass(frozen=True, slots=True)
class RaydiumSwapReplayPreflightResult:
    cpi: RaydiumClmmCpiResult
    loop_settlement: RaydiumSwapLoopSettlementResult
    vault_settlement: RaydiumSwapVaultSettlementResult
    pool_after: RaydiumClmmPoolState
    observation_after: object
    expected_vaults_after: RaydiumClmmVaultSnapshot
    execution_ready: bool = False


def verify_raydium_swap_replay_preflight(
    cpi: RaydiumClmmCpiResult,
    loop_settlement: RaydiumSwapLoopSettlementResult,
    vault_settlement: RaydiumSwapVaultSettlementResult,
) -> RaydiumSwapReplayPreflightResult:
    """Require all pre-broadcast artifacts to describe the same pinned swap."""
    if not isinstance(cpi, RaydiumClmmCpiResult) or cpi.passed is not True:
        raise RaydiumSwapReplayPreflightError("successful Raydium CLMM CPI evidence is required")
    if not isinstance(loop_settlement, RaydiumSwapLoopSettlementResult):
        raise RaydiumSwapReplayPreflightError("completed outer-loop settlement is required")
    if not isinstance(vault_settlement, RaydiumSwapVaultSettlementResult):
        raise RaydiumSwapReplayPreflightError("expected post-swap vault settlement is required")

    loop = loop_settlement.loop
    settlement = loop_settlement.settlement
    pool = loop.pool
    tick_snapshot = loop.tick_array_snapshot
    vault_snapshot = vault_settlement.expected_post_state

    if loop.swap_complete is not True or loop.final_iteration.swap_complete is not True:
        raise RaydiumSwapReplayPreflightError("outer swap loop is not complete")
    if loop.is_base_input is not True or cpi.is_base_input is not True:
        raise RaydiumSwapReplayPreflightError("only the pinned exact-input Raydium swap CPI is supported")
    if cpi.source_commit != RAYDIUM_CLMM_SOURCE_COMMIT:
        raise RaydiumSwapReplayPreflightError("Raydium CPI source commit differs from the pinned implementation")
    if cpi.program_id != RAYDIUM_CLMM_MAINNET_PROGRAM_ID or cpi.stack_height != 2:
        raise RaydiumSwapReplayPreflightError("CPI program or direct-child stack position is unsupported")
    if (
        cpi.message_sha256 != tick_snapshot.route_message_sha256
        or vault_snapshot.route_message_sha256 != cpi.message_sha256
    ):
        raise RaydiumSwapReplayPreflightError("CPI, tick-array and vault evidence message digests differ")
    if (
        cpi.pool_state != pool.pool_address
        or cpi.amm_config != pool.amm_config
        or cpi.observation_state != pool.observation_key
    ):
        raise RaydiumSwapReplayPreflightError("CPI pool/config/observation accounts differ from decoded PoolState")
    expected_input_vault = pool.token_vault_0 if loop.zero_for_one else pool.token_vault_1
    expected_output_vault = pool.token_vault_1 if loop.zero_for_one else pool.token_vault_0
    if cpi.input_vault != expected_input_vault or cpi.output_vault != expected_output_vault:
        raise RaydiumSwapReplayPreflightError("CPI input/output vault order differs from swap direction")
    if (
        cpi.tick_array_accounts != tick_snapshot.cpi_account_order
        or len(set(cpi.tick_array_accounts)) != len(cpi.tick_array_accounts)
    ):
        raise RaydiumSwapReplayPreflightError("CPI trailing tick-array/extension account order differs from fetched snapshot")
    if (
        cpi.amount_raw != loop.amount_specified
        or cpi.sqrt_price_limit_x64 != loop.sqrt_price_limit_x64
        or cpi.is_base_input != loop.is_base_input
    ):
        raise RaydiumSwapReplayPreflightError("CPI amount, price limit or input mode differs from deterministic replay")
    if cpi.minimum_output_raw <= 0 or settlement.amount_returned < cpi.minimum_output_raw:
        raise RaydiumSwapReplayPreflightError("deterministic output does not satisfy the simulated CPI minimum-output threshold")
    if settlement.transfer_fee_0 != 0 or settlement.transfer_fee_1 != 0:
        raise RaydiumSwapReplayPreflightError("non-zero token transfer fees are unsupported by the pinned swap instruction")
    if settlement.amounts.amount_0 <= 0 or settlement.amounts.amount_1 <= 0:
        raise RaydiumSwapReplayPreflightError("settled token0/token1 amounts must both be positive")

    before = vault_settlement
    if (
        before.token0_vault_after - before.token0_vault_before
        != (settlement.amounts.amount_0 if loop.zero_for_one else -settlement.amounts.amount_0)
        or before.token1_vault_after - before.token1_vault_before
        != (-settlement.amounts.amount_1 if loop.zero_for_one else settlement.amounts.amount_1)
    ):
        raise RaydiumSwapReplayPreflightError("expected vault post-state deltas differ from exact swap amounts")
    if (
        vault_snapshot.provider_id != tick_snapshot.provider_id
        or vault_snapshot.cluster != tick_snapshot.cluster
        or vault_snapshot.genesis_hash != tick_snapshot.genesis_hash
        or vault_snapshot.simulation_response_id != tick_snapshot.simulation_response_id
        or vault_snapshot.simulation_context_slot != tick_snapshot.simulation_context_slot
    ):
        raise RaydiumSwapReplayPreflightError("vault post-state provenance differs from the CPI tick-array snapshot")

    state = settlement.state
    return RaydiumSwapReplayPreflightResult(
        cpi=cpi,
        loop_settlement=loop_settlement,
        vault_settlement=vault_settlement,
        pool_after=state.pool,
        observation_after=state.observation,
        expected_vaults_after=vault_snapshot,
        execution_ready=False,
    )
