"""Fail-closed Meteora DLMM CPI decoder for Jupiter Metis routes.

Schema and discriminators are pinned to MeteoraAg/dlmm-sdk IDL commit
1532e540db41ac405dbf4c5bb2ddfceaa1f98ff1. This gate decodes only the
classic-token `swap` instruction and does not infer pool state from labels.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct

from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_route_semantics import JupiterRouteSemanticResult
from veltrix.jupiter_swap_effects import JupiterSwapEffectResult
from veltrix.simulation_gate import SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult


METEORA_DLMM_PROGRAM_ID = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
METEORA_DLMM_IDL_COMMIT = "1532e540db41ac405dbf4c5bb2ddfceaa1f98ff1"
METEORA_DLMM_SWAP_DISCRIMINATOR = bytes((248, 198, 158, 145, 225, 117, 135, 200))
METEORA_DLMM_SWAP_ACCOUNT_COUNT = 15
CLASSIC_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
OPTIONAL_ACCOUNT_SENTINEL = METEORA_DLMM_PROGRAM_ID


class MeteoraDlmmCpiError(ValueError):
    """Raised when a Meteora DLMM CPI does not match the approved Jupiter route."""


@dataclass(frozen=True, slots=True)
class MeteoraDlmmSwapPolicy:
    program_id: str = METEORA_DLMM_PROGRAM_ID
    max_slippage_bps: int = 100
    allow_bitmap_extension: bool = True

    def __post_init__(self) -> None:
        try:
            if str(Pubkey.from_string(self.program_id)) != self.program_id:
                raise ValueError("non-canonical")
        except Exception as exc:
            raise MeteoraDlmmCpiError("Meteora program ID must be a canonical public key") from exc
        if self.program_id != METEORA_DLMM_PROGRAM_ID:
            raise MeteoraDlmmCpiError("Meteora program ID differs from the pinned DLMM deployment")
        if isinstance(self.max_slippage_bps, bool) or not isinstance(self.max_slippage_bps, int) or not 0 <= self.max_slippage_bps <= 10_000:
            raise MeteoraDlmmCpiError("max_slippage_bps must be between 0 and 10000")
        if not isinstance(self.allow_bitmap_extension, bool):
            raise MeteoraDlmmCpiError("allow_bitmap_extension must be a boolean")


@dataclass(frozen=True, slots=True)
class DecodedMeteoraDlmmSwap:
    amount_in: int
    minimum_amount_out: int
    lb_pair: str
    reserve_x: str
    reserve_y: str
    user_token_in: str
    user_token_out: str
    bitmap_extension: str
    token_x_mint: str
    token_y_mint: str
    oracle: str
    user: str
    event_authority: str


@dataclass(frozen=True, slots=True)
class MeteoraDlmmCpiResult:
    passed: bool
    message_sha256: str
    program_id: str
    decoded_swaps: tuple[DecodedMeteoraDlmmSwap, ...]
    max_slippage_bps: int
    idl_commit: str = METEORA_DLMM_IDL_COMMIT


def decode_meteora_dlmm_swap(data: bytes, account_keys: tuple[str, ...], *, policy: MeteoraDlmmSwapPolicy) -> DecodedMeteoraDlmmSwap:
    if not isinstance(policy, MeteoraDlmmSwapPolicy):
        raise MeteoraDlmmCpiError("Meteora DLMM swap policy is required")
    if not isinstance(data, bytes) or len(data) != 24:
        raise MeteoraDlmmCpiError("Meteora DLMM swap data must contain the 8-byte discriminator and two u64 arguments")
    if data[:8] != METEORA_DLMM_SWAP_DISCRIMINATOR:
        raise MeteoraDlmmCpiError("inner Meteora instruction is not the pinned DLMM swap variant")
    if not isinstance(account_keys, tuple) or len(account_keys) != METEORA_DLMM_SWAP_ACCOUNT_COUNT:
        raise MeteoraDlmmCpiError("Meteora DLMM swap must contain exactly 15 IDL accounts")
    for key in account_keys:
        try:
            if str(Pubkey.from_string(key)) != key:
                raise ValueError("non-canonical")
        except Exception as exc:
            raise MeteoraDlmmCpiError("Meteora DLMM CPI contains a non-canonical account public key") from exc

    amount_in, minimum_amount_out = struct.unpack("<QQ", data[8:])
    if amount_in <= 0 or minimum_amount_out <= 0:
        raise MeteoraDlmmCpiError("Meteora DLMM swap amounts must be positive")
    if account_keys[0] == OPTIONAL_ACCOUNT_SENTINEL:
        raise MeteoraDlmmCpiError("Meteora DLMM lb_pair account is missing")
    if account_keys[1] != OPTIONAL_ACCOUNT_SENTINEL and not policy.allow_bitmap_extension:
        raise MeteoraDlmmCpiError("Meteora bitmap extension is outside local policy")
    if account_keys[2] == OPTIONAL_ACCOUNT_SENTINEL or account_keys[3] == OPTIONAL_ACCOUNT_SENTINEL:
        raise MeteoraDlmmCpiError("Meteora DLMM reserve accounts are missing")
    if account_keys[4] == OPTIONAL_ACCOUNT_SENTINEL or account_keys[5] == OPTIONAL_ACCOUNT_SENTINEL:
        raise MeteoraDlmmCpiError("Meteora DLMM user token accounts are missing")
    if account_keys[6] == account_keys[7]:
        raise MeteoraDlmmCpiError("Meteora DLMM token X and Y mints must differ")
    if account_keys[8] == OPTIONAL_ACCOUNT_SENTINEL:
        raise MeteoraDlmmCpiError("Meteora DLMM oracle account is missing")
    if account_keys[9] != OPTIONAL_ACCOUNT_SENTINEL:
        raise MeteoraDlmmCpiError("Meteora host-fee account is not permitted by the zero-platform-fee policy")
    if account_keys[10] == OPTIONAL_ACCOUNT_SENTINEL:
        raise MeteoraDlmmCpiError("Meteora DLMM user authority is missing")
    if account_keys[11] != CLASSIC_TOKEN_PROGRAM_ID or account_keys[12] != CLASSIC_TOKEN_PROGRAM_ID:
        raise MeteoraDlmmCpiError("Meteora DLMM CPI must use classic SPL Token programs for both mints")
    if account_keys[13] != str(Pubkey.find_program_address([b"__event_authority"], Pubkey.from_string(policy.program_id))[0]):
        raise MeteoraDlmmCpiError("Meteora DLMM event authority PDA does not match the pinned program")
    if account_keys[14] != policy.program_id:
        raise MeteoraDlmmCpiError("Meteora DLMM self-program account does not match the pinned program")
    return DecodedMeteoraDlmmSwap(
        amount_in=amount_in,
        minimum_amount_out=minimum_amount_out,
        lb_pair=account_keys[0],
        reserve_x=account_keys[2],
        reserve_y=account_keys[3],
        user_token_in=account_keys[4],
        user_token_out=account_keys[5],
        bitmap_extension=account_keys[1],
        token_x_mint=account_keys[6],
        token_y_mint=account_keys[7],
        oracle=account_keys[8],
        user=account_keys[10],
        event_authority=account_keys[13],
    )


def evaluate_meteora_dlmm_cpi(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    simulation: SimulationEvidence,
    simulation_gate: SimulationGateResult,
    program_gate: JupiterRouteProgramGateResult,
    route_semantics: JupiterRouteSemanticResult,
    swap_effects: JupiterSwapEffectResult,
    policy: MeteoraDlmmSwapPolicy,
) -> MeteoraDlmmCpiResult:
    """Approve only pure-Meteora Metis routes whose exact simulated CPI matches each quote hop."""
    if not isinstance(order, JupiterOrderTransaction):
        raise MeteoraDlmmCpiError("Jupiter order is required")
    if not isinstance(validation, TransactionValidationResult):
        raise MeteoraDlmmCpiError("structural transaction validation is required")
    if not isinstance(simulation, SimulationEvidence):
        raise MeteoraDlmmCpiError("simulation evidence is required")
    if not isinstance(simulation_gate, SimulationGateResult) or simulation_gate.approved is not True:
        raise MeteoraDlmmCpiError("successful SimulationGate approval is required")
    if not isinstance(program_gate, JupiterRouteProgramGateResult) or program_gate.passed is not True:
        raise MeteoraDlmmCpiError("successful Jupiter program trace approval is required")
    if not isinstance(route_semantics, JupiterRouteSemanticResult) or route_semantics.passed is not True:
        raise MeteoraDlmmCpiError("successful Jupiter route semantic approval is required")
    if not isinstance(swap_effects, JupiterSwapEffectResult) or swap_effects.passed is not True:
        raise MeteoraDlmmCpiError("successful quote-to-balance effect approval is required")
    if not isinstance(policy, MeteoraDlmmSwapPolicy):
        raise MeteoraDlmmCpiError("Meteora DLMM policy is required")

    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if any(value != digest for value in (
        validation.message_sha256, simulation.message_sha256, simulation_gate.message_sha256,
        program_gate.message_sha256, route_semantics.message_sha256, swap_effects.message_sha256,
    )):
        raise MeteoraDlmmCpiError("order and safety gate message digests differ")
    if simulation.provider_id != simulation_gate.provider_id or simulation.response_id != simulation_gate.response_id:
        raise MeteoraDlmmCpiError("simulation evidence and SimulationGate refer to different RPC responses")
    if simulation.context_slot != simulation_gate.context_slot:
        raise MeteoraDlmmCpiError("simulation context slot differs from SimulationGate")
    if simulation.simulation_error is not None:
        raise MeteoraDlmmCpiError("simulation reported an execution error")
    if order.router != "metis" or route_semantics.jupiter_program_id not in validation.program_ids:
        raise MeteoraDlmmCpiError("Meteora CPI decoder requires the pinned Metis Jupiter route")
    if (
        program_gate.router != order.router
        or tuple(program_gate.top_level_program_ids) != tuple(validation.program_ids)
        or tuple(simulation.top_level_program_ids) != tuple(validation.program_ids)
    ):
        raise MeteoraDlmmCpiError("Jupiter program trace differs from the validated order message")
    if tuple(program_gate.route_labels) != tuple(dict.fromkeys(step.label for step in order.route_plan)):
        raise MeteoraDlmmCpiError("Jupiter program trace route labels differ from the order")
    if any(step.label != "Meteora DLMM" for step in order.route_plan):
        raise MeteoraDlmmCpiError("mixed or non-Meteora DEX routes are not yet supported")
    if "Meteora DLMM" not in program_gate.route_labels:
        raise MeteoraDlmmCpiError("Meteora DLMM is absent from the approved Jupiter route labels")
    if (
        policy.program_id not in program_gate.inner_program_ids
        or policy.program_id not in program_gate.dex_program_ids
    ):
        raise MeteoraDlmmCpiError("pinned Meteora DLMM program is absent from the approved DEX CPI trace")
    if not simulation.inner_instructions:
        raise MeteoraDlmmCpiError("complete simulated inner instruction data is required")
    if tuple(item.program_id for item in simulation.inner_instructions) != tuple(simulation.inner_program_ids):
        raise MeteoraDlmmCpiError("full simulated CPI instruction records differ from the program ID trace")

    meteora_instructions = [
        item for item in simulation.inner_instructions
        if item.program_id == policy.program_id
    ]
    if len(meteora_instructions) != len(order.route_plan):
        raise MeteoraDlmmCpiError("number of Meteora DLMM swap CPIs differs from the quoted route hops")
    if any(item.parent_instruction_index != route_semantics.instruction_index for item in meteora_instructions):
        raise MeteoraDlmmCpiError("Meteora CPI is not nested under the validated Jupiter route instruction")

    decoded_by_pool = {}
    for item in meteora_instructions:
        decoded = decode_meteora_dlmm_swap(item.data, item.account_keys, policy=policy)
        if decoded.lb_pair in decoded_by_pool:
            raise MeteoraDlmmCpiError("duplicate Meteora DLMM CPI for the same pool")
        decoded_by_pool[decoded.lb_pair] = decoded

    results = []
    for step in order.route_plan:
        decoded = decoded_by_pool.get(step.amm_key)
        if decoded is None:
            raise MeteoraDlmmCpiError("quoted Meteora pool is absent from the simulated CPI instructions")
        if decoded.user != order.taker:
            raise MeteoraDlmmCpiError("Meteora DLMM CPI user authority differs from the Jupiter taker")
        if {decoded.token_x_mint, decoded.token_y_mint} != {step.input_mint, step.output_mint}:
            raise MeteoraDlmmCpiError("Meteora DLMM CPI token mint pair differs from the quote route hop")
        if decoded.amount_in != step.input_amount_raw:
            raise MeteoraDlmmCpiError("Meteora DLMM CPI input amount differs from the quote route hop")
        if decoded.minimum_amount_out > step.output_amount_raw:
            raise MeteoraDlmmCpiError("Meteora DLMM CPI minimum output exceeds the quoted route-hop output")
        required_minimum = (
            step.output_amount_raw * (10_000 - policy.max_slippage_bps) + 9_999
        ) // 10_000
        if decoded.minimum_amount_out < required_minimum:
            raise MeteoraDlmmCpiError("Meteora DLMM CPI minimum output allows more slippage than local policy")
        results.append(decoded)

    return MeteoraDlmmCpiResult(
        passed=True,
        message_sha256=digest,
        program_id=policy.program_id,
        decoded_swaps=tuple(results),
        max_slippage_bps=policy.max_slippage_bps,
    )
