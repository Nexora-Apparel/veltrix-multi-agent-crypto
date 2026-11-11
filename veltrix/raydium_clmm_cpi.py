"""Fail-closed Raydium CLMM classic-token CPI semantic gate.

Pinned to raydium-io/raydium-clmm source commit 6d7a0f82d2f293c553c69aaf943377367c6c4cc6.
Only the legacy `swap` instruction with a single direct Jupiter route step is
supported. The Token-2022 `swap_v2` path and multi-hop routes are rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct

from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_route_semantics import (
    JupiterRouteSemanticResult,
    JupiterRouteSemanticError,
)
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_swap_effects import (
    CLASSIC_TOKEN_PROGRAM_ID,
    JupiterSwapEffectResult,
    derive_associated_token_address,
)
from veltrix.simulation_gate import SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult


RAYDIUM_CLMM_SOURCE_COMMIT = "6d7a0f82d2f293c553c69aaf943377367c6c4cc6"
RAYDIUM_CLMM_MAINNET_PROGRAM_ID = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
RAYDIUM_CLMM_SWAP_DISCRIMINATOR = hashlib.sha256(b"global:swap").digest()[:8]
RAYDIUM_CLMM_SWAP_V2_DISCRIMINATOR = hashlib.sha256(b"global:swap_v2").digest()[:8]
RAYDIUM_CLMM_FIXED_ACCOUNT_COUNT = 10
RAYDIUM_CLMM_MAX_ACCOUNT_COUNT = 32


class RaydiumClmmCpiError(ValueError):
    """Raised when a Raydium CLMM CPI does not match the approved Jupiter route."""


@dataclass(frozen=True, slots=True)
class DecodedRaydiumClmmSwap:
    amount_raw: int
    other_amount_threshold_raw: int
    sqrt_price_limit_x64: int
    is_base_input: bool


@dataclass(frozen=True, slots=True)
class RaydiumClmmCpiPolicy:
    program_id: str = RAYDIUM_CLMM_MAINNET_PROGRAM_ID
    max_slippage_bps: int = 100
    max_account_count: int = RAYDIUM_CLMM_MAX_ACCOUNT_COUNT

    def __post_init__(self) -> None:
        try:
            if str(Pubkey.from_string(self.program_id)) != self.program_id:
                raise ValueError("non-canonical")
        except Exception as exc:
            raise RaydiumClmmCpiError("Raydium CLMM program ID is invalid") from exc
        if self.program_id != RAYDIUM_CLMM_MAINNET_PROGRAM_ID:
            raise RaydiumClmmCpiError("only the source-pinned Raydium CLMM mainnet program ID is supported")
        if isinstance(self.max_slippage_bps, bool) or not isinstance(self.max_slippage_bps, int) or not 0 <= self.max_slippage_bps <= 10_000:
            raise RaydiumClmmCpiError("max_slippage_bps must be between 0 and 10000")
        if isinstance(self.max_account_count, bool) or not isinstance(self.max_account_count, int) or not RAYDIUM_CLMM_FIXED_ACCOUNT_COUNT <= self.max_account_count <= RAYDIUM_CLMM_MAX_ACCOUNT_COUNT:
            raise RaydiumClmmCpiError("max_account_count is outside the supported account range")


def decode_raydium_clmm_swap(data: bytes) -> DecodedRaydiumClmmSwap:
    """Decode the pinned Anchor swap args: u64, u64, u128, bool."""
    if not isinstance(data, bytes) or len(data) < 8:
        raise RaydiumClmmCpiError("Raydium CLMM CPI data is missing or truncated")
    if data[:8] == RAYDIUM_CLMM_SWAP_V2_DISCRIMINATOR:
        raise RaydiumClmmCpiError("Raydium CLMM swap_v2 Token-2022-capable instruction is unsupported")
    if data[:8] != RAYDIUM_CLMM_SWAP_DISCRIMINATOR:
        raise RaydiumClmmCpiError("Raydium CLMM CPI discriminator is not the supported swap instruction")
    if len(data) != 41:
        raise RaydiumClmmCpiError("Raydium CLMM swap instruction has an invalid Borsh payload length")
    amount, threshold = struct.unpack("<QQ", data[8:24])
    sqrt_price_limit = int.from_bytes(data[24:40], "little")
    bool_value = data[40]
    if bool_value not in (0, 1):
        raise RaydiumClmmCpiError("Raydium CLMM is_base_input is not a canonical Borsh boolean")
    if amount <= 0 or threshold <= 0:
        raise RaydiumClmmCpiError("Raydium CLMM swap amount and minimum-output threshold must be positive")
    return DecodedRaydiumClmmSwap(
        amount_raw=amount,
        other_amount_threshold_raw=threshold,
        sqrt_price_limit_x64=sqrt_price_limit,
        is_base_input=bool(bool_value),
    )


@dataclass(frozen=True, slots=True)
class RaydiumClmmCpiResult:
    passed: bool
    message_sha256: str
    program_id: str
    outer_instruction_index: int
    cpi_instruction_index: int
    payer: str
    amm_config: str
    input_token_account: str
    output_token_account: str
    input_vault: str
    output_vault: str
    observation_state: str
    tick_array: str
    pool_state: str
    amount_raw: int
    minimum_output_raw: int
    sqrt_price_limit_x64: int
    is_base_input: bool
    stack_height: int
    source_commit: str = RAYDIUM_CLMM_SOURCE_COMMIT


def evaluate_raydium_clmm_cpi(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    simulation: SimulationEvidence,
    simulation_gate: SimulationGateResult,
    program_gate: JupiterRouteProgramGateResult,
    route_semantics: JupiterRouteSemanticResult,
    swap_effects: JupiterSwapEffectResult,
    policy: RaydiumClmmCpiPolicy,
) -> RaydiumClmmCpiResult:
    """Require one direct Raydium CLMM CPI to encode the exact single-hop order."""
    if not isinstance(order, JupiterOrderTransaction):
        raise RaydiumClmmCpiError("Jupiter order is required")
    if not isinstance(validation, TransactionValidationResult):
        raise RaydiumClmmCpiError("structural transaction validation is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumClmmCpiError("simulation evidence is required")
    if not isinstance(simulation_gate, SimulationGateResult) or simulation_gate.approved is not True:
        raise RaydiumClmmCpiError("successful SimulationGate approval is required")
    if not isinstance(program_gate, JupiterRouteProgramGateResult) or program_gate.passed is not True:
        raise RaydiumClmmCpiError("successful Jupiter program trace gate is required")
    if not isinstance(route_semantics, JupiterRouteSemanticResult) or route_semantics.passed is not True:
        raise RaydiumClmmCpiError("successful Jupiter route semantic gate is required")
    if not isinstance(swap_effects, JupiterSwapEffectResult) or swap_effects.passed is not True:
        raise RaydiumClmmCpiError("successful Jupiter quote-to-balance effect gate is required")
    if not isinstance(policy, RaydiumClmmCpiPolicy):
        raise RaydiumClmmCpiError("Raydium CLMM CPI policy is required")

    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if any(value != digest for value in (
        validation.message_sha256,
        simulation.message_sha256,
        simulation_gate.message_sha256,
        program_gate.message_sha256,
        route_semantics.message_sha256,
        swap_effects.message_sha256,
    )):
        raise RaydiumClmmCpiError("order, validation, simulation and upstream gate digests differ")
    if simulation.provider_id != simulation_gate.provider_id or simulation.response_id != simulation_gate.response_id:
        raise RaydiumClmmCpiError("simulation evidence and gate result refer to different RPC responses")
    if simulation.context_slot != simulation_gate.context_slot:
        raise RaydiumClmmCpiError("simulation evidence and gate result context slots differ")
    if (
        tuple(program_gate.top_level_program_ids) != tuple(validation.program_ids)
        or tuple(simulation.top_level_program_ids) != tuple(validation.program_ids)
        or tuple(program_gate.inner_program_ids) != tuple(simulation.inner_program_ids)
    ):
        raise RaydiumClmmCpiError("program trace evidence differs from the validated transaction or simulation")
    if simulation.simulation_error is not None:
        raise RaydiumClmmCpiError("simulation reported an execution error")
    if swap_effects.simulation_context_slot != simulation.context_slot:
        raise RaydiumClmmCpiError("quote-to-balance evidence refers to a different simulation slot")
    if order.taker != order.fee_payer or validation.fee_payer != order.taker:
        raise RaydiumClmmCpiError("order taker is not the validated transaction fee payer")
    if order.router != "metis" or route_semantics.jupiter_program_id not in program_gate.top_level_program_ids:
        raise RaydiumClmmCpiError("route does not use the supported Metis Jupiter aggregator path")
    if route_semantics.instruction_index < 0:
        raise RaydiumClmmCpiError("Jupiter route instruction index is invalid")
    if len(order.route_plan) != 1 or order.route_plan[0].label != "Raydium CLMM":
        raise RaydiumClmmCpiError("only a single-hop Raydium CLMM route is supported")
    if len(route_semantics.route_steps) != 1:
        raise RaydiumClmmCpiError("route semantic evidence contains an invalid route step count")
    if policy.program_id not in program_gate.dex_program_ids:
        raise RaydiumClmmCpiError("Raydium CLMM program ID is not present in the pinned DEX trace")
    if program_gate.route_labels != ("Raydium CLMM",):
        raise RaydiumClmmCpiError("program trace route labels do not identify the supported single-hop Raydium CLMM route")
    if (
        route_semantics.route_steps[0].swap_variant_index != 26
        or route_semantics.input_amount_raw != order.input_amount_raw
        or route_semantics.quoted_output_amount_raw != order.output_amount_raw
    ):
        raise RaydiumClmmCpiError("Jupiter route semantic evidence differs from the supported Raydium CLMM quote")
    if not simulation.inner_instructions or len(simulation.inner_instructions) != simulation.inner_instruction_count:
        raise RaydiumClmmCpiError("complete raw inner-CPI instruction evidence is required")
    if tuple(item.program_id for item in simulation.inner_instructions) != simulation.inner_program_ids:
        raise RaydiumClmmCpiError("raw CPI evidence differs from the simulation program trace")

    candidates = [
        (index, item)
        for index, item in enumerate(simulation.inner_instructions)
        if item.program_id == policy.program_id
    ]
    if len(candidates) != 1:
        raise RaydiumClmmCpiError("simulation must contain exactly one Raydium CLMM CPI for the single-hop route")
    cpi_index, cpi = candidates[0]
    if cpi.outer_instruction_index != route_semantics.instruction_index:
        raise RaydiumClmmCpiError("Raydium CLMM CPI is not nested under the validated Jupiter route instruction")
    if cpi.stack_height != 2:
        raise RaydiumClmmCpiError("Raydium CLMM CPI must be a direct child of the Jupiter route instruction")
    if not RAYDIUM_CLMM_FIXED_ACCOUNT_COUNT <= len(cpi.account_keys) <= policy.max_account_count:
        raise RaydiumClmmCpiError("Raydium CLMM CPI account list is outside the supported fixed-plus-tick-array range")

    accounts = cpi.account_keys
    try:
        for account in accounts:
            if str(Pubkey.from_string(account)) != account:
                raise ValueError("non-canonical")
    except Exception as exc:
        raise RaydiumClmmCpiError("Raydium CLMM CPI contains a non-canonical account public key") from exc
    payer, amm_config, pool_state, input_account, output_account, input_vault, output_vault, observation, token_program, tick_array = accounts[:10]
    expected_input = derive_associated_token_address(order.taker, order.input_mint)
    expected_output = derive_associated_token_address(order.taker, order.output_mint)
    if payer != order.taker:
        raise RaydiumClmmCpiError("Raydium CLMM CPI payer differs from the Jupiter taker")
    if input_account != expected_input or output_account != expected_output:
        raise RaydiumClmmCpiError("single-hop Raydium CLMM user token accounts differ from the taker's input/output ATAs")
    if swap_effects.input_token_account != expected_input or swap_effects.output_token_account != expected_output:
        raise RaydiumClmmCpiError("quote-to-balance gate token accounts differ from the Raydium CPI user accounts")
    if swap_effects.input_delta_raw != -order.input_amount_raw or swap_effects.output_delta_raw != order.output_amount_raw:
        raise RaydiumClmmCpiError("quote-to-balance effects differ from the exact Jupiter order amounts")
    if token_program != CLASSIC_TOKEN_PROGRAM_ID:
        raise RaydiumClmmCpiError("Raydium CLMM CPI does not use the classic SPL Token program")
    if input_vault == output_vault or input_account == output_account:
        raise RaydiumClmmCpiError("Raydium CLMM CPI input/output token accounts or vaults unexpectedly alias")
    if len(set(accounts[:10])) != 10:
        raise RaydiumClmmCpiError("Raydium CLMM fixed account positions unexpectedly alias")

    decoded = decode_raydium_clmm_swap(cpi.data)
    quoted_step = order.route_plan[0]
    if not decoded.is_base_input:
        raise RaydiumClmmCpiError("Raydium CLMM CPI must use exact-input mode")
    if decoded.amount_raw != quoted_step.input_amount_raw or decoded.amount_raw != order.input_amount_raw:
        raise RaydiumClmmCpiError("Raydium CLMM CPI input amount differs from the exact Jupiter route amount")
    if decoded.other_amount_threshold_raw > quoted_step.output_amount_raw:
        raise RaydiumClmmCpiError("Raydium CLMM minimum-output threshold exceeds the quoted route output")
    minimum_allowed = (
        quoted_step.output_amount_raw * (10_000 - min(policy.max_slippage_bps, route_semantics.slippage_bps))
        + 9_999
    ) // 10_000
    if decoded.other_amount_threshold_raw < minimum_allowed:
        raise RaydiumClmmCpiError("Raydium CLMM minimum-output threshold allows more slippage than local policy")

    return RaydiumClmmCpiResult(
        passed=True,
        message_sha256=digest,
        program_id=policy.program_id,
        outer_instruction_index=cpi.outer_instruction_index,
        cpi_instruction_index=cpi_index,
        payer=payer,
        amm_config=amm_config,
        input_token_account=input_account,
        output_token_account=output_account,
        input_vault=input_vault,
        output_vault=output_vault,
        observation_state=observation,
        tick_array=tick_array,
        pool_state=pool_state,
        amount_raw=decoded.amount_raw,
        minimum_output_raw=decoded.other_amount_threshold_raw,
        sqrt_price_limit_x64=decoded.sqrt_price_limit_x64,
        is_base_input=decoded.is_base_input,
        stack_height=cpi.stack_height,
    )
