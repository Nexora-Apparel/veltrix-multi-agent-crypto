"""Narrow semantic gate for Jupiter-invoked Raydium CLMM swap_v2 CPIs.

This first DEX decoder accepts only classic SPL-token, exact-input swaps routed
through Raydium CLMM's pinned swap_v2 instruction. It does not sign or submit.
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


RAYDIUM_CLMM_MAINNET_PROGRAM_ID = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
CLASSIC_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
RAYDIUM_CLMM_SOURCE_COMMIT = "6d7a0f82d2f293c553c69aaf943377367c6c4cc6"
RAYDIUM_SWAP_V2_DISCRIMINATOR = hashlib.sha256(b"global:swap_v2").digest()[:8]
RAYDIUM_SWAP_V2_FIXED_ACCOUNT_COUNT = 13
MAX_RAYDIUM_REMAINING_ACCOUNTS = 8


class RaydiumCpiSemanticError(ValueError):
    """Raised when Raydium CLMM CPI bytes or accounts differ from the approved route."""


@dataclass(frozen=True, slots=True)
class DecodedRaydiumSwapV2:
    amount_raw: int
    other_amount_threshold_raw: int
    sqrt_price_limit_x64: int
    is_base_input: bool


@dataclass(frozen=True, slots=True)
class RaydiumCpiPolicy:
    allowed_program_ids: tuple[str, ...] = (RAYDIUM_CLMM_MAINNET_PROGRAM_ID,)
    allowed_auxiliary_program_ids: tuple[str, ...] = (CLASSIC_TOKEN_PROGRAM_ID,)
    max_cpi_count: int = 12
    max_remaining_accounts: int = MAX_RAYDIUM_REMAINING_ACCOUNTS

    def __post_init__(self) -> None:
        for field in ("allowed_program_ids", "allowed_auxiliary_program_ids"):
            values = getattr(self, field)
            if not isinstance(values, tuple) or not values:
                raise RaydiumCpiSemanticError(f"{field} must be a non-empty immutable tuple")
            for value in values:
                try:
                    if str(Pubkey.from_string(value)) != value:
                        raise ValueError("non-canonical")
                except Exception as exc:
                    raise RaydiumCpiSemanticError(f"{field} contains an invalid public key") from exc
            if len(set(values)) != len(values):
                raise RaydiumCpiSemanticError(f"{field} must not contain duplicates")
        if set(self.allowed_program_ids) & set(self.allowed_auxiliary_program_ids):
            raise RaydiumCpiSemanticError("DEX and auxiliary program allowlists must not overlap")
        for field, low, high in (
            ("max_cpi_count", 1, 64),
            ("max_remaining_accounts", 1, MAX_RAYDIUM_REMAINING_ACCOUNTS),
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise RaydiumCpiSemanticError(f"{field} is outside its permitted range")


@dataclass(frozen=True, slots=True)
class RaydiumCpiSemanticResult:
    passed: bool
    message_sha256: str
    outer_instruction_index: int
    cpi_count: int
    pool_ids: tuple[str, ...]
    input_amounts_raw: tuple[int, ...]
    minimum_output_amounts_raw: tuple[int, ...]
    source_commit: str = RAYDIUM_CLMM_SOURCE_COMMIT


def decode_raydium_swap_v2(data: bytes) -> DecodedRaydiumSwapV2:
    if not isinstance(data, bytes) or len(data) != 41:
        raise RaydiumCpiSemanticError("Raydium swap_v2 data must be exactly 41 bytes")
    if data[:8] != RAYDIUM_SWAP_V2_DISCRIMINATOR:
        raise RaydiumCpiSemanticError("Raydium CPI is not the pinned swap_v2 instruction")
    amount, threshold = struct.unpack("<QQ", data[8:24])
    sqrt_limit = int.from_bytes(data[24:40], "little")
    is_base_input = data[40]
    if amount <= 0 or threshold <= 0:
        raise RaydiumCpiSemanticError("Raydium swap amount and output threshold must be positive")
    if is_base_input not in (0, 1):
        raise RaydiumCpiSemanticError("Raydium exact-input flag is not a canonical Borsh boolean")
    if sqrt_limit != 0:
        raise RaydiumCpiSemanticError("custom Raydium sqrt-price limits are not supported")
    return DecodedRaydiumSwapV2(
        amount_raw=amount,
        other_amount_threshold_raw=threshold,
        sqrt_price_limit_x64=sqrt_limit,
        is_base_input=bool(is_base_input),
    )


def _canonical_account(value: str, field: str) -> str:
    try:
        canonical = str(Pubkey.from_string(value))
    except Exception as exc:
        raise RaydiumCpiSemanticError(f"{field} is not a valid public key") from exc
    if canonical != value:
        raise RaydiumCpiSemanticError(f"{field} is not canonical")
    return canonical


def evaluate_raydium_clmm_cpis(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    simulation: SimulationEvidence,
    simulation_gate: SimulationGateResult,
    program_gate: JupiterRouteProgramGateResult,
    route_semantics: JupiterRouteSemanticResult,
    policy: RaydiumCpiPolicy,
) -> RaydiumCpiSemanticResult:
    """Decode every Raydium CLMM CPI and bind its pool/amount/accounts to the quote."""
    if not isinstance(order, JupiterOrderTransaction):
        raise RaydiumCpiSemanticError("Jupiter order is required")
    if not isinstance(validation, TransactionValidationResult):
        raise RaydiumCpiSemanticError("validated transaction is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumCpiSemanticError("detailed simulation evidence is required")
    if not isinstance(simulation_gate, SimulationGateResult) or simulation_gate.approved is not True:
        raise RaydiumCpiSemanticError("successful simulation gate is required")
    if not isinstance(program_gate, JupiterRouteProgramGateResult) or program_gate.passed is not True:
        raise RaydiumCpiSemanticError("successful Jupiter program trace gate is required")
    if not isinstance(route_semantics, JupiterRouteSemanticResult) or route_semantics.passed is not True:
        raise RaydiumCpiSemanticError("successful Jupiter route semantic gate is required")
    if not isinstance(policy, RaydiumCpiPolicy):
        raise RaydiumCpiSemanticError("Raydium CPI policy is required")

    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if any(value != digest for value in (
        validation.message_sha256, simulation.message_sha256, simulation_gate.message_sha256,
        program_gate.message_sha256, route_semantics.message_sha256,
    )):
        raise RaydiumCpiSemanticError("order, validation, simulation and semantic evidence digests differ")
    if simulation.simulation_error is not None:
        raise RaydiumCpiSemanticError("simulation reported an execution error")
    if (
        simulation.recent_blockhash != order.recent_blockhash
        or simulation.last_valid_block_height != order.last_valid_block_height
    ):
        raise RaydiumCpiSemanticError("simulation blockhash or last-valid height differs from the Jupiter order")
    if (
        tuple(simulation.top_level_program_ids) != tuple(validation.program_ids)
        or tuple(program_gate.top_level_program_ids) != tuple(validation.program_ids)
    ):
        raise RaydiumCpiSemanticError("top-level program trace differs from the validated transaction")
    if simulation.provider_id != simulation_gate.provider_id or simulation.response_id != simulation_gate.response_id:
        raise RaydiumCpiSemanticError("simulation gate and detailed evidence refer to different RPC responses")
    if simulation.context_slot != simulation_gate.context_slot:
        raise RaydiumCpiSemanticError("simulation context slots differ")
    if tuple(item.program_id for item in simulation.inner_instructions) != simulation.inner_program_ids:
        raise RaydiumCpiSemanticError("detailed inner CPI evidence is missing or inconsistent")
    if tuple(program_gate.inner_program_ids) != simulation.inner_program_ids:
        raise RaydiumCpiSemanticError("Jupiter program trace differs from the detailed simulation CPI evidence")
    if tuple(program_gate.route_labels) != tuple(dict.fromkeys(step.label for step in order.route_plan)):
        raise RaydiumCpiSemanticError("Jupiter program trace labels differ from the quote route plan")
    if not set(policy.allowed_program_ids).intersection(program_gate.dex_program_ids):
        raise RaydiumCpiSemanticError("Raydium program is not present in the approved DEX program trace")
    if len(simulation.inner_instructions) != simulation.inner_instruction_count or not simulation.inner_instructions:
        raise RaydiumCpiSemanticError("complete detailed inner CPI instruction evidence is required")
    if route_semantics.instruction_index < 0:
        raise RaydiumCpiSemanticError("Jupiter route instruction index is invalid")
    if not order.route_plan or len(order.route_plan) > policy.max_cpi_count:
        raise RaydiumCpiSemanticError("Jupiter route plan is empty or exceeds the CPI count policy")
    if len(route_semantics.route_steps) != len(order.route_plan):
        raise RaydiumCpiSemanticError("decoded Jupiter route-step count differs from the quote")
    if any(step.label != "Raydium CLMM" for step in order.route_plan):
        raise RaydiumCpiSemanticError("this DEX semantic gate supports only all-Raydium-CLMM routes")
    if any(step.swap_variant_index != 26 for step in route_semantics.route_steps):
        raise RaydiumCpiSemanticError("decoded Jupiter route contains a non-Ray​​dium-CLMM swap enum")

    dex_cpis = []
    for inner in simulation.inner_instructions:
        if inner.outer_instruction_index != route_semantics.instruction_index:
            continue
        if inner.program_id in policy.allowed_program_ids:
            dex_cpis.append(inner)
        elif inner.program_id not in policy.allowed_auxiliary_program_ids:
            raise RaydiumCpiSemanticError("simulation contains an unapproved inner CPI program")
    if len(dex_cpis) != len(order.route_plan):
        raise RaydiumCpiSemanticError("Raydium CLMM CPI count differs from the quoted route step count")

    pool_ids = []
    amounts = []
    thresholds = []
    seen_pools = set()
    token_index_accounts: dict[int, str] = {}
    for index, (step, inner, decoded_route_step) in enumerate(zip(order.route_plan, dex_cpis, route_semantics.route_steps)):
        if inner.program_id not in policy.allowed_program_ids:
            raise RaydiumCpiSemanticError("Raydium CPI program ID is not locally pinned")
        if inner.stack_height != 2:
            raise RaydiumCpiSemanticError("Raydium CPI must be a direct depth-2 child of the Jupiter route instruction")
        if len(inner.account_keys) <= RAYDIUM_SWAP_V2_FIXED_ACCOUNT_COUNT:
            raise RaydiumCpiSemanticError("Raydium swap_v2 CPI is missing required tick-array remaining accounts")
        if len(inner.account_keys) > RAYDIUM_SWAP_V2_FIXED_ACCOUNT_COUNT + policy.max_remaining_accounts:
            raise RaydiumCpiSemanticError("Raydium swap_v2 CPI has too many remaining accounts")
        accounts = tuple(_canonical_account(value, f"Raydium CPI account {position}") for position, value in enumerate(inner.account_keys))
        if len(set(accounts)) != len(accounts):
            raise RaydiumCpiSemanticError("Raydium swap_v2 CPI contains duplicate account keys")
        if accounts[0] != order.taker:
            raise RaydiumCpiSemanticError("Raydium swap payer differs from the Jupiter taker")
        if accounts[2] != step.amm_key:
            raise RaydiumCpiSemanticError("Raydium pool state differs from the quoted AMM key")
        if accounts[3] == accounts[4] or accounts[5] == accounts[6]:
            raise RaydiumCpiSemanticError("Raydium user token accounts or pool vaults unexpectedly alias")
        for token_index, account in (
            (decoded_route_step.input_index, accounts[3]),
            (decoded_route_step.output_index, accounts[4]),
        ):
            previous_account = token_index_accounts.setdefault(token_index, account)
            if previous_account != account:
                raise RaydiumCpiSemanticError("Raydium CPI token accounts do not preserve Jupiter route token-index flow")
        if accounts[8] != CLASSIC_TOKEN_PROGRAM_ID:
            raise RaydiumCpiSemanticError("Raydium swap_v2 must use classic SPL Token for this supported path")
        if accounts[9] != TOKEN_2022_PROGRAM_ID or accounts[10] != MEMO_PROGRAM_ID:
            raise RaydiumCpiSemanticError("Raydium swap_v2 token-2022 or memo program account layout differs from the pinned contract")
        if accounts[11] != step.input_mint or accounts[12] != step.output_mint:
            raise RaydiumCpiSemanticError("Raydium vault mint accounts differ from the quoted route step mints")
        decoded = decode_raydium_swap_v2(inner.data)
        if not decoded.is_base_input:
            raise RaydiumCpiSemanticError("Raydium exact-output swap is not supported")
        if decoded.amount_raw != step.input_amount_raw:
            raise RaydiumCpiSemanticError("Raydium exact-input amount differs from the quoted route step")
        if decoded.other_amount_threshold_raw > step.output_amount_raw:
            raise RaydiumCpiSemanticError("Raydium minimum output threshold exceeds the quoted route-step output")
        if inner.program_id not in program_gate.dex_program_ids:
            raise RaydiumCpiSemanticError("Raydium CPI program is absent from the approved DEX program trace")
        if accounts[2] in seen_pools:
            raise RaydiumCpiSemanticError("duplicate Raydium pool state appears in the same route")
        seen_pools.add(accounts[2])
        pool_ids.append(accounts[2])
        amounts.append(decoded.amount_raw)
        thresholds.append(decoded.other_amount_threshold_raw)

    expected_input_ata = derive_associated_token_address(order.taker, order.input_mint)
    expected_output_ata = derive_associated_token_address(order.taker, order.output_mint)
    output_indexes = tuple(index for index, mint in route_semantics.token_index_mints if mint == order.output_mint)
    if len(output_indexes) != 1:
        raise RaydiumCpiSemanticError("decoded Jupiter route does not identify exactly one final output token index")
    if token_index_accounts.get(0) != expected_input_ata:
        raise RaydiumCpiSemanticError("Raydium route input token account is not the taker's classic SPL input ATA")
    if token_index_accounts.get(output_indexes[0]) != expected_output_ata:
        raise RaydiumCpiSemanticError("Raydium route output token account is not the taker's classic SPL output ATA")

    return RaydiumCpiSemanticResult(
        passed=True,
        message_sha256=digest,
        outer_instruction_index=route_semantics.instruction_index,
        cpi_count=len(dex_cpis),
        pool_ids=tuple(pool_ids),
        input_amounts_raw=tuple(amounts),
        minimum_output_amounts_raw=tuple(thresholds),
    )
