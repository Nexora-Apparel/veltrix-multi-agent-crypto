"""Narrow Raydium AMM v4 SwapBaseInV2 CPI semantic gate.

Supports only the orderbook-disabled exact-input instruction (tag 16) with
classic SPL Token. The older orderbook route and exact-output are rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct

from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_route_semantics import JupiterRouteSemanticResult
from veltrix.jupiter_swap_effects import derive_associated_token_address
from veltrix.simulation_gate import SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult


RAYDIUM_AMM_V4_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_AMM_V4_SOURCE_COMMIT = "91739d8d9f5cf347e0ce14ebe4d2bb7069fb31b6"
RAYDIUM_AMM_AUTHORITY_SEED = b"amm authority"
RAYDIUM_AMM_SWAP_BASE_IN_V2_TAG = 16
CLASSIC_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
RAYDIUM_AMM_SWAP_BASE_IN_V2_ACCOUNT_COUNT = 8


class RaydiumAmmV4CpiSemanticError(ValueError):
    """Raised when Raydium AMM v4 CPI bytes or accounts differ from the approved route."""


@dataclass(frozen=True, slots=True)
class DecodedRaydiumAmmV4Swap:
    amount_in_raw: int
    minimum_amount_out_raw: int


@dataclass(frozen=True, slots=True)
class RaydiumAmmV4CpiPolicy:
    allowed_program_ids: tuple[str, ...] = (RAYDIUM_AMM_V4_PROGRAM_ID,)
    allowed_auxiliary_program_ids: tuple[str, ...] = (CLASSIC_TOKEN_PROGRAM_ID,)
    max_cpi_count: int = 12

    def __post_init__(self) -> None:
        for field in ("allowed_program_ids", "allowed_auxiliary_program_ids"):
            values = getattr(self, field)
            if not isinstance(values, tuple) or not values:
                raise RaydiumAmmV4CpiSemanticError(f"{field} must be a non-empty immutable tuple")
            for value in values:
                try:
                    if str(Pubkey.from_string(value)) != value:
                        raise ValueError("non-canonical")
                except Exception as exc:
                    raise RaydiumAmmV4CpiSemanticError(f"{field} contains an invalid public key") from exc
            if len(set(values)) != len(values):
                raise RaydiumAmmV4CpiSemanticError(f"{field} must not contain duplicates")
        if set(self.allowed_program_ids) & set(self.allowed_auxiliary_program_ids):
            raise RaydiumAmmV4CpiSemanticError("DEX and auxiliary program allowlists must not overlap")
        if isinstance(self.max_cpi_count, bool) or not isinstance(self.max_cpi_count, int) or not 1 <= self.max_cpi_count <= 64:
            raise RaydiumAmmV4CpiSemanticError("max_cpi_count is outside its permitted range")


@dataclass(frozen=True, slots=True)
class RaydiumAmmV4CpiSemanticResult:
    passed: bool
    message_sha256: str
    outer_instruction_index: int
    cpi_count: int
    pool_ids: tuple[str, ...]
    input_amounts_raw: tuple[int, ...]
    minimum_output_amounts_raw: tuple[int, ...]
    source_commit: str = RAYDIUM_AMM_V4_SOURCE_COMMIT


def decode_raydium_amm_v4_swap_base_in_v2(data: bytes) -> DecodedRaydiumAmmV4Swap:
    if not isinstance(data, bytes) or len(data) != 17:
        raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 SwapBaseInV2 data must be exactly 17 bytes")
    if data[0] != RAYDIUM_AMM_SWAP_BASE_IN_V2_TAG:
        raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 CPI is not the pinned SwapBaseInV2 instruction")
    amount_in, minimum_out = struct.unpack("<QQ", data[1:17])
    if amount_in <= 0 or minimum_out <= 0:
        raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 input amount and minimum output must be positive")
    return DecodedRaydiumAmmV4Swap(amount_in, minimum_out)


def _canonical_account(value: str, field: str) -> str:
    try:
        canonical = str(Pubkey.from_string(value))
    except Exception as exc:
        raise RaydiumAmmV4CpiSemanticError(f"{field} is not a valid public key") from exc
    if canonical != value:
        raise RaydiumAmmV4CpiSemanticError(f"{field} is not canonical")
    return canonical


def _authority_matches(authority: str, program_id: str) -> bool:
    program = Pubkey.from_string(program_id)
    expected = Pubkey.from_string(authority)
    for nonce in range(256):
        try:
            derived = Pubkey.create_program_address(
                [RAYDIUM_AMM_AUTHORITY_SEED, bytes((nonce,))],
                program,
            )
        except Exception:
            continue
        if derived == expected:
            return True
    return False


def evaluate_raydium_amm_v4_cpis(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    simulation: SimulationEvidence,
    simulation_gate: SimulationGateResult,
    program_gate: JupiterRouteProgramGateResult,
    route_semantics: JupiterRouteSemanticResult,
    policy: RaydiumAmmV4CpiPolicy,
) -> RaydiumAmmV4CpiSemanticResult:
    """Decode each Raydium AMM v4 SwapBaseInV2 CPI and bind it to the quote."""
    if not isinstance(order, JupiterOrderTransaction):
        raise RaydiumAmmV4CpiSemanticError("Jupiter order is required")
    if not isinstance(validation, TransactionValidationResult):
        raise RaydiumAmmV4CpiSemanticError("validated transaction is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumAmmV4CpiSemanticError("detailed simulation evidence is required")
    if not isinstance(simulation_gate, SimulationGateResult) or simulation_gate.approved is not True:
        raise RaydiumAmmV4CpiSemanticError("successful simulation gate is required")
    if not isinstance(program_gate, JupiterRouteProgramGateResult) or program_gate.passed is not True:
        raise RaydiumAmmV4CpiSemanticError("successful Jupiter program trace gate is required")
    if not isinstance(route_semantics, JupiterRouteSemanticResult) or route_semantics.passed is not True:
        raise RaydiumAmmV4CpiSemanticError("successful Jupiter route semantic gate is required")
    if not isinstance(policy, RaydiumAmmV4CpiPolicy):
        raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 CPI policy is required")

    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if any(value != digest for value in (
        validation.message_sha256, simulation.message_sha256, simulation_gate.message_sha256,
        program_gate.message_sha256, route_semantics.message_sha256,
    )):
        raise RaydiumAmmV4CpiSemanticError("order, validation, simulation and semantic evidence digests differ")
    if simulation.simulation_error is not None:
        raise RaydiumAmmV4CpiSemanticError("simulation reported an execution error")
    if (
        simulation.recent_blockhash != order.recent_blockhash
        or simulation.last_valid_block_height != order.last_valid_block_height
    ):
        raise RaydiumAmmV4CpiSemanticError("simulation blockhash or last-valid height differs from the Jupiter order")
    if simulation.provider_id != simulation_gate.provider_id or simulation.response_id != simulation_gate.response_id:
        raise RaydiumAmmV4CpiSemanticError("simulation gate and detailed evidence refer to different RPC responses")
    if simulation.context_slot != simulation_gate.context_slot:
        raise RaydiumAmmV4CpiSemanticError("simulation context slots differ")
    if (
        tuple(simulation.top_level_program_ids) != tuple(validation.program_ids)
        or tuple(program_gate.top_level_program_ids) != tuple(validation.program_ids)
    ):
        raise RaydiumAmmV4CpiSemanticError("top-level program trace differs from the validated transaction")
    if tuple(item.program_id for item in simulation.inner_instructions) != simulation.inner_program_ids:
        raise RaydiumAmmV4CpiSemanticError("detailed inner CPI evidence is missing or inconsistent")
    if tuple(program_gate.inner_program_ids) != simulation.inner_program_ids:
        raise RaydiumAmmV4CpiSemanticError("Jupiter program trace differs from detailed simulation CPI evidence")
    if tuple(program_gate.route_labels) != tuple(dict.fromkeys(step.label for step in order.route_plan)):
        raise RaydiumAmmV4CpiSemanticError("Jupiter program trace labels differ from the quote route plan")
    if not set(policy.allowed_program_ids).intersection(program_gate.dex_program_ids):
        raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 program is not present in the approved DEX program trace")
    if len(simulation.inner_instructions) != simulation.inner_instruction_count or not simulation.inner_instructions:
        raise RaydiumAmmV4CpiSemanticError("complete detailed inner CPI instruction evidence is required")
    if not order.route_plan or len(order.route_plan) > policy.max_cpi_count:
        raise RaydiumAmmV4CpiSemanticError("Jupiter route plan is empty or exceeds the CPI count policy")
    if len(route_semantics.route_steps) != len(order.route_plan):
        raise RaydiumAmmV4CpiSemanticError("decoded Jupiter route-step count differs from the quote")
    if any(step.label != "Raydium" for step in order.route_plan):
        raise RaydiumAmmV4CpiSemanticError("this DEX semantic gate supports only all-Raydium-AMM routes")
    if any(step.swap_variant_index != 7 for step in route_semantics.route_steps):
        raise RaydiumAmmV4CpiSemanticError("decoded Jupiter route contains a non-Raydium swap enum")

    dex_cpis = []
    for inner in simulation.inner_instructions:
        if inner.outer_instruction_index != route_semantics.instruction_index:
            continue
        if inner.program_id in policy.allowed_program_ids:
            dex_cpis.append(inner)
        elif inner.program_id not in policy.allowed_auxiliary_program_ids:
            raise RaydiumAmmV4CpiSemanticError("simulation contains an unapproved inner CPI program")
    if len(dex_cpis) != len(order.route_plan):
        raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 CPI count differs from the quoted route step count")

    pools = []
    amounts = []
    minimum_outputs = []
    seen_pools = set()
    token_index_accounts: dict[int, str] = {}
    for step, inner, decoded_route_step in zip(order.route_plan, dex_cpis, route_semantics.route_steps):
        if inner.stack_height != 2:
            raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 CPI must be a direct depth-2 child of the Jupiter route instruction")
        if len(inner.account_keys) != RAYDIUM_AMM_SWAP_BASE_IN_V2_ACCOUNT_COUNT:
            raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 SwapBaseInV2 CPI must contain exactly 8 accounts")
        accounts = tuple(_canonical_account(value, f"Raydium AMM v4 CPI account {position}") for position, value in enumerate(inner.account_keys))
        if len(set(accounts)) != len(accounts):
            raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 CPI contains duplicate account keys")
        if accounts[0] != CLASSIC_TOKEN_PROGRAM_ID:
            raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 route must use classic SPL Token")
        if accounts[1] != step.amm_key:
            raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 pool account differs from the quoted AMM key")
        if not _authority_matches(accounts[2], inner.program_id):
            raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 authority PDA is invalid")
        if accounts[3] == accounts[4]:
            raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 coin and PC vaults unexpectedly alias")
        if accounts[5] == accounts[6]:
            raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 user token accounts unexpectedly alias")
        if accounts[7] != order.taker:
            raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 user signer differs from the Jupiter taker")
        decoded = decode_raydium_amm_v4_swap_base_in_v2(inner.data)
        if decoded.amount_in_raw != step.input_amount_raw:
            raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 input amount differs from the quoted route-step amount")
        if decoded.minimum_amount_out_raw > step.output_amount_raw:
            raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 minimum output threshold exceeds the quoted route-step output")
        for token_index, account in (
            (decoded_route_step.input_index, accounts[5]),
            (decoded_route_step.output_index, accounts[6]),
        ):
            previous_account = token_index_accounts.setdefault(token_index, account)
            if previous_account != account:
                raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 token accounts do not preserve Jupiter route token-index flow")
        if accounts[1] in seen_pools:
            raise RaydiumAmmV4CpiSemanticError("duplicate Raydium AMM v4 pool appears in the same route")
        seen_pools.add(accounts[1])
        pools.append(accounts[1])
        amounts.append(decoded.amount_in_raw)
        minimum_outputs.append(decoded.minimum_amount_out_raw)

    expected_input_ata = derive_associated_token_address(order.taker, order.input_mint)
    expected_output_ata = derive_associated_token_address(order.taker, order.output_mint)
    output_indexes = tuple(index for index, mint in route_semantics.token_index_mints if mint == order.output_mint)
    if len(output_indexes) != 1:
        raise RaydiumAmmV4CpiSemanticError("decoded Jupiter route does not identify exactly one final output token index")
    if token_index_accounts.get(0) != expected_input_ata:
        raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 route input token account is not the taker's classic SPL input ATA")
    if token_index_accounts.get(output_indexes[0]) != expected_output_ata:
        raise RaydiumAmmV4CpiSemanticError("Raydium AMM v4 route output token account is not the taker's classic SPL output ATA")

    return RaydiumAmmV4CpiSemanticResult(
        passed=True,
        message_sha256=digest,
        outer_instruction_index=route_semantics.instruction_index,
        cpi_count=len(dex_cpis),
        pool_ids=tuple(pools),
        input_amounts_raw=tuple(amounts),
        minimum_output_amounts_raw=tuple(minimum_outputs),
    )
