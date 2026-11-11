"""Composite Meteora paper-trade preflight.

A pass means the candidate has passed the listed structural, simulation,
route, CPI, account-state and cost ceilings for PAPER SIMULATION ONLY. This
module never authorizes wallet signing, transaction submission or live mode.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_route_semantics import JupiterRouteSemanticResult
from veltrix.jupiter_swap_effects import JupiterSwapEffectResult
from veltrix.meteora_dlmm_cpi import (
    METEORA_DLMM_PROGRAM_ID,
    MeteoraDlmmCpiResult,
)
from veltrix.meteora_dlmm_pool_state import MeteoraDlmmPoolStateResult
from veltrix.simulation_gate import SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult


class MeteoraPaperPreflightError(ValueError):
    """Raised when any required Meteora paper-trade gate or risk ceiling fails."""


@dataclass(frozen=True, slots=True)
class MeteoraPaperPreflightPolicy:
    max_order_age_ms: int = 10_000
    max_future_skew_ms: int = 1_000
    max_price_impact_pct: float = 1.0
    max_fee_bps: float = 100.0
    max_slippage_bps: int = 100
    max_compute_units: int = 300_000
    max_fee_lamports: int = 10_000_000

    def __post_init__(self) -> None:
        for field in ("max_order_age_ms", "max_future_skew_ms", "max_slippage_bps", "max_compute_units", "max_fee_lamports"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise MeteoraPaperPreflightError(f"{field} must be a non-negative integer")
        if self.max_order_age_ms == 0 or self.max_compute_units == 0:
            raise MeteoraPaperPreflightError("order-age and compute ceilings must be positive")
        for field in ("max_price_impact_pct", "max_fee_bps"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise MeteoraPaperPreflightError(f"{field} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class MeteoraPaperPreflightResult:
    passed: bool
    decision: str
    paper_only: bool
    live_execution_eligible: bool
    execution_enabled: bool
    message_sha256: str
    order_request_id: str
    simulation_provider_id: str
    simulation_response_id: str
    simulation_context_slot: int
    simulated_compute_units: int
    simulated_fee_lamports: int
    price_impact_pct: float
    fee_bps: float
    slippage_bps: int
    input_amount_raw: int
    output_amount_raw: int


def evaluate_meteora_paper_preflight(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    simulation: SimulationEvidence,
    simulation_gate: SimulationGateResult,
    program_gate: JupiterRouteProgramGateResult,
    route_semantics: JupiterRouteSemanticResult,
    swap_effects: JupiterSwapEffectResult,
    cpi_result: MeteoraDlmmCpiResult,
    pool_state: MeteoraDlmmPoolStateResult,
    policy: MeteoraPaperPreflightPolicy,
    now_epoch_ms: int,
) -> MeteoraPaperPreflightResult:
    """Compose every required Meteora gate into an explicitly paper-only verdict."""
    typed = (
        (validation, TransactionValidationResult, "structural transaction validation"),
        (simulation, SimulationEvidence, "simulation evidence"),
        (simulation_gate, SimulationGateResult, "SimulationGate result"),
        (program_gate, JupiterRouteProgramGateResult, "Jupiter program trace result"),
        (route_semantics, JupiterRouteSemanticResult, "Jupiter route semantic result"),
        (swap_effects, JupiterSwapEffectResult, "quote-to-balance effect result"),
        (cpi_result, MeteoraDlmmCpiResult, "Meteora CPI result"),
        (pool_state, MeteoraDlmmPoolStateResult, "Meteora pool-state result"),
        (policy, MeteoraPaperPreflightPolicy, "paper preflight policy"),
    )
    if not isinstance(order, JupiterOrderTransaction):
        raise MeteoraPaperPreflightError("Jupiter order is required")
    for value, expected_type, label in typed:
        if not isinstance(value, expected_type):
            raise MeteoraPaperPreflightError(f"{label} is required")
    if isinstance(now_epoch_ms, bool) or not isinstance(now_epoch_ms, int) or now_epoch_ms <= 0:
        raise MeteoraPaperPreflightError("now_epoch_ms must be a positive integer")

    gates = (
        (simulation_gate.approved, "SimulationGate did not approve the simulation"),
        (program_gate.passed, "Jupiter program trace gate did not pass"),
        (route_semantics.passed, "Jupiter route semantic gate did not pass"),
        (swap_effects.passed, "quote-to-balance effect gate did not pass"),
        (cpi_result.passed, "Meteora CPI semantic gate did not pass"),
        (pool_state.passed, "Meteora pool-state gate did not pass"),
    )
    if any(value is not True for value, _ in gates):
        raise MeteoraPaperPreflightError(next(message for value, message in gates if value is not True))

    digest = hashlib.sha256(order.message_bytes).hexdigest()
    bound_digests = (
        validation.message_sha256,
        simulation.message_sha256,
        simulation_gate.message_sha256,
        program_gate.message_sha256,
        route_semantics.message_sha256,
        swap_effects.message_sha256,
        cpi_result.message_sha256,
        pool_state.message_sha256,
    )
    if any(value != digest for value in bound_digests):
        raise MeteoraPaperPreflightError("one or more safety gate results are not bound to the exact Jupiter message")
    if (
        simulation.provider_id != simulation_gate.provider_id
        or simulation.response_id != simulation_gate.response_id
        or simulation.context_slot != simulation_gate.context_slot
        or swap_effects.simulation_context_slot != simulation.context_slot
    ):
        raise MeteoraPaperPreflightError("simulation evidence, token effects and SimulationGate do not share one RPC response and slot")
    if simulation.simulation_error is not None:
        raise MeteoraPaperPreflightError("simulation reported an execution error")
    if simulation.current_block_height > simulation.last_valid_block_height:
        raise MeteoraPaperPreflightError("Jupiter recent blockhash expired before the paper preflight")
    if simulation.last_valid_block_height != order.last_valid_block_height:
        raise MeteoraPaperPreflightError("simulation last-valid block height differs from the Jupiter order")
    if simulation.recent_blockhash != order.recent_blockhash:
        raise MeteoraPaperPreflightError("simulation recent blockhash differs from the Jupiter order")
    if order.taker != order.fee_payer or validation.fee_payer != order.taker:
        raise MeteoraPaperPreflightError("order taker is not the structurally validated fee payer")
    if order.router != "metis" or len(order.route_plan) != 1:
        raise MeteoraPaperPreflightError("only a single-hop Metis route is supported by this paper preflight")
    if order.route_plan[0].label != "Meteora DLMM":
        raise MeteoraPaperPreflightError("paper preflight only supports a Meteora DLMM route")
    if cpi_result.program_id != METEORA_DLMM_PROGRAM_ID:
        raise MeteoraPaperPreflightError("Meteora CPI result is not from the pinned DLMM program")
    if pool_state.pool_state.address != order.route_plan[0].amm_key:
        raise MeteoraPaperPreflightError("verified pool state does not match the Jupiter route pool")
    if program_gate.router != order.router or tuple(program_gate.top_level_program_ids) != tuple(validation.program_ids):
        raise MeteoraPaperPreflightError("Jupiter program trace differs from structural validation")
    if tuple(simulation.top_level_program_ids) != tuple(validation.program_ids):
        raise MeteoraPaperPreflightError("simulated top-level program trace differs from structural validation")
    if tuple(program_gate.route_labels) != ("Meteora DLMM",):
        raise MeteoraPaperPreflightError("Jupiter program trace does not identify the exact Meteora route")
    if simulation.inner_instruction_count != len(simulation.inner_instructions):
        raise MeteoraPaperPreflightError("complete simulated CPI instruction data is required")
    if simulation.inner_program_ids != tuple(item.program_id for item in simulation.inner_instructions):
        raise MeteoraPaperPreflightError("simulated CPI program IDs differ from the preserved inner instruction records")

    if order.observed_at_epoch_ms <= 0 or order.observed_at_epoch_ms > now_epoch_ms + policy.max_future_skew_ms:
        raise MeteoraPaperPreflightError("Jupiter order timestamp is invalid or future-dated")
    if now_epoch_ms - order.observed_at_epoch_ms > policy.max_order_age_ms:
        raise MeteoraPaperPreflightError("Jupiter order quote is stale")
    if simulation.observed_at_epoch_ms < order.observed_at_epoch_ms:
        raise MeteoraPaperPreflightError("simulation predates the Jupiter order quote")
    if order.price_impact_pct > policy.max_price_impact_pct:
        raise MeteoraPaperPreflightError("Jupiter quoted price impact exceeds the paper risk ceiling")
    if order.fee_bps > policy.max_fee_bps:
        raise MeteoraPaperPreflightError("Jupiter quoted fee exceeds the paper risk ceiling")
    if route_semantics.slippage_bps > policy.max_slippage_bps:
        raise MeteoraPaperPreflightError("encoded Jupiter slippage exceeds the paper risk ceiling")
    if route_semantics.platform_fee_bps != 0:
        raise MeteoraPaperPreflightError("non-zero Jupiter platform fee is not approved")
    if simulation.units_consumed > policy.max_compute_units:
        raise MeteoraPaperPreflightError("simulated compute usage exceeds the paper risk ceiling")
    if simulation.fee_lamports > policy.max_fee_lamports:
        raise MeteoraPaperPreflightError("simulated network fee exceeds the paper risk ceiling")
    if (
        swap_effects.input_delta_raw != -order.input_amount_raw
        or swap_effects.output_delta_raw != order.output_amount_raw
        or swap_effects.minimum_output_amount_raw != order.minimum_output_amount_raw
    ):
        raise MeteoraPaperPreflightError("simulated user token balance effects differ from the exact Jupiter order")

    return MeteoraPaperPreflightResult(
        passed=True,
        decision="PAPER_SIMULATION_ONLY",
        paper_only=True,
        live_execution_eligible=False,
        execution_enabled=False,
        message_sha256=digest,
        order_request_id=order.request_id,
        simulation_provider_id=simulation.provider_id,
        simulation_response_id=simulation.response_id,
        simulation_context_slot=simulation.context_slot,
        simulated_compute_units=simulation.units_consumed,
        simulated_fee_lamports=simulation.fee_lamports,
        price_impact_pct=order.price_impact_pct,
        fee_bps=order.fee_bps,
        slippage_bps=route_semantics.slippage_bps,
        input_amount_raw=order.input_amount_raw,
        output_amount_raw=order.output_amount_raw,
    )
