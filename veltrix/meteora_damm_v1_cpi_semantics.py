"""Narrow semantic gate for Jupiter-invoked Meteora DAMM v1 swap CPIs.

Supports only classic SPL-token exact-input swaps using the pinned DAMM v1
15-account layout and no remaining depeg/referral accounts.
"""
from __future__ import annotations

from dataclasses import dataclass
import struct

from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_route_semantics import JupiterRouteSemanticResult
from veltrix.jupiter_swap_effects import derive_associated_token_address
from veltrix.simulation_gate import SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult


METEORA_DAMM_V1_PROGRAM_ID = "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB"
METEORA_DAMM_V1_SOURCE_COMMIT = "5386170f55d32deed0b4a128a36fd6a34d339610"
METEORA_DAMM_V1_SWAP_DISCRIMINATOR = bytes((248, 198, 158, 145, 225, 117, 135, 200))
METEORA_VAULT_PROGRAM_ID = "24Uqj9JCLxUeoC3hGfh5W3s9FM9uCHDS2SG3LYwBpyTi"
CLASSIC_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
METEORA_DAMM_V1_SWAP_ACCOUNT_COUNT = 15


class MeteoraDammV1CpiSemanticError(ValueError):
    """Raised when Meteora DAMM v1 CPI bytes or accounts differ from the approved route."""


@dataclass(frozen=True, slots=True)
class DecodedMeteoraDammV1Swap:
    input_amount_raw: int
    minimum_output_amount_raw: int


@dataclass(frozen=True, slots=True)
class MeteoraDammV1CpiPolicy:
    allowed_program_ids: tuple[str, ...] = (METEORA_DAMM_V1_PROGRAM_ID,)
    allowed_auxiliary_program_ids: tuple[str, ...] = (CLASSIC_TOKEN_PROGRAM_ID,)
    max_cpi_count: int = 12

    def __post_init__(self) -> None:
        for field in ("allowed_program_ids", "allowed_auxiliary_program_ids"):
            values = getattr(self, field)
            if not isinstance(values, tuple) or not values:
                raise MeteoraDammV1CpiSemanticError(f"{field} must be a non-empty immutable tuple")
            for value in values:
                try:
                    if str(Pubkey.from_string(value)) != value:
                        raise ValueError("non-canonical")
                except Exception as exc:
                    raise MeteoraDammV1CpiSemanticError(f"{field} contains an invalid public key") from exc
            if len(set(values)) != len(values):
                raise MeteoraDammV1CpiSemanticError(f"{field} must not contain duplicates")
        if set(self.allowed_program_ids) & set(self.allowed_auxiliary_program_ids):
            raise MeteoraDammV1CpiSemanticError("DEX and auxiliary program allowlists must not overlap")
        if isinstance(self.max_cpi_count, bool) or not isinstance(self.max_cpi_count, int) or not 1 <= self.max_cpi_count <= 64:
            raise MeteoraDammV1CpiSemanticError("max_cpi_count is outside its permitted range")


@dataclass(frozen=True, slots=True)
class MeteoraDammV1CpiSemanticResult:
    passed: bool
    message_sha256: str
    outer_instruction_index: int
    cpi_count: int
    pool_ids: tuple[str, ...]
    input_amounts_raw: tuple[int, ...]
    minimum_output_amounts_raw: tuple[int, ...]
    source_commit: str = METEORA_DAMM_V1_SOURCE_COMMIT


def decode_meteora_damm_v1_swap(data: bytes) -> DecodedMeteoraDammV1Swap:
    if not isinstance(data, bytes) or len(data) != 24:
        raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 swap data must be exactly 24 bytes")
    if data[:8] != METEORA_DAMM_V1_SWAP_DISCRIMINATOR:
        raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 CPI is not the pinned swap instruction")
    amount_in, minimum_out = struct.unpack("<QQ", data[8:24])
    if amount_in <= 0 or minimum_out <= 0:
        raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 input amount and minimum output must be positive")
    return DecodedMeteoraDammV1Swap(amount_in, minimum_out)


def _canonical_account(value: str, field: str) -> str:
    try:
        canonical = str(Pubkey.from_string(value))
    except Exception as exc:
        raise MeteoraDammV1CpiSemanticError(f"{field} is not a valid public key") from exc
    if canonical != value:
        raise MeteoraDammV1CpiSemanticError(f"{field} is not canonical")
    return canonical


def evaluate_meteora_damm_v1_cpis(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    simulation: SimulationEvidence,
    simulation_gate: SimulationGateResult,
    program_gate: JupiterRouteProgramGateResult,
    route_semantics: JupiterRouteSemanticResult,
    policy: MeteoraDammV1CpiPolicy,
) -> MeteoraDammV1CpiSemanticResult:
    """Decode each Meteora DAMM v1 swap CPI and bind it to the Jupiter quote."""
    if not isinstance(order, JupiterOrderTransaction):
        raise MeteoraDammV1CpiSemanticError("Jupiter order is required")
    if not isinstance(validation, TransactionValidationResult):
        raise MeteoraDammV1CpiSemanticError("validated transaction is required")
    if not isinstance(simulation, SimulationEvidence):
        raise MeteoraDammV1CpiSemanticError("detailed simulation evidence is required")
    if not isinstance(simulation_gate, SimulationGateResult) or simulation_gate.approved is not True:
        raise MeteoraDammV1CpiSemanticError("successful simulation gate is required")
    if not isinstance(program_gate, JupiterRouteProgramGateResult) or program_gate.passed is not True:
        raise MeteoraDammV1CpiSemanticError("successful Jupiter program trace gate is required")
    if not isinstance(route_semantics, JupiterRouteSemanticResult) or route_semantics.passed is not True:
        raise MeteoraDammV1CpiSemanticError("successful Jupiter route semantic gate is required")
    if not isinstance(policy, MeteoraDammV1CpiPolicy):
        raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 CPI policy is required")

    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if any(value != digest for value in (
        validation.message_sha256, simulation.message_sha256, simulation_gate.message_sha256,
        program_gate.message_sha256, route_semantics.message_sha256,
    )):
        raise MeteoraDammV1CpiSemanticError("order, validation, simulation and semantic evidence digests differ")
    if simulation.simulation_error is not None:
        raise MeteoraDammV1CpiSemanticError("simulation reported an execution error")
    if (
        simulation.recent_blockhash != order.recent_blockhash
        or simulation.last_valid_block_height != order.last_valid_block_height
    ):
        raise MeteoraDammV1CpiSemanticError("simulation blockhash or last-valid height differs from the Jupiter order")
    if simulation.provider_id != simulation_gate.provider_id or simulation.response_id != simulation_gate.response_id:
        raise MeteoraDammV1CpiSemanticError("simulation gate and detailed evidence refer to different RPC responses")
    if simulation.context_slot != simulation_gate.context_slot:
        raise MeteoraDammV1CpiSemanticError("simulation context slots differ")
    if (
        tuple(simulation.top_level_program_ids) != tuple(validation.program_ids)
        or tuple(program_gate.top_level_program_ids) != tuple(validation.program_ids)
    ):
        raise MeteoraDammV1CpiSemanticError("top-level program trace differs from the validated transaction")
    if tuple(item.program_id for item in simulation.inner_instructions) != simulation.inner_program_ids:
        raise MeteoraDammV1CpiSemanticError("detailed inner CPI evidence is missing or inconsistent")
    if tuple(program_gate.inner_program_ids) != simulation.inner_program_ids:
        raise MeteoraDammV1CpiSemanticError("Jupiter program trace differs from detailed simulation CPI evidence")
    if tuple(program_gate.route_labels) != tuple(dict.fromkeys(step.label for step in order.route_plan)):
        raise MeteoraDammV1CpiSemanticError("Jupiter program trace labels differ from the quote route plan")
    if not set(policy.allowed_program_ids).intersection(program_gate.dex_program_ids):
        raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 program is not present in the approved DEX program trace")
    if len(simulation.inner_instructions) != simulation.inner_instruction_count or not simulation.inner_instructions:
        raise MeteoraDammV1CpiSemanticError("complete detailed inner CPI instruction evidence is required")
    if not order.route_plan or len(order.route_plan) > policy.max_cpi_count:
        raise MeteoraDammV1CpiSemanticError("Jupiter route plan is empty or exceeds the CPI count policy")
    if len(route_semantics.route_steps) != len(order.route_plan):
        raise MeteoraDammV1CpiSemanticError("decoded Jupiter route-step count differs from the quote")
    if any(step.label != "Meteora" for step in order.route_plan):
        raise MeteoraDammV1CpiSemanticError("this DEX semantic gate supports only all-Meteora-DAMM-v1 routes")
    if any(step.swap_variant_index != 19 for step in route_semantics.route_steps):
        raise MeteoraDammV1CpiSemanticError("decoded Jupiter route contains a non-Meteora swap enum")

    dex_cpis = []
    for inner in simulation.inner_instructions:
        if inner.outer_instruction_index != route_semantics.instruction_index:
            continue
        if inner.program_id in policy.allowed_program_ids:
            dex_cpis.append(inner)
        elif inner.program_id not in policy.allowed_auxiliary_program_ids:
            raise MeteoraDammV1CpiSemanticError("simulation contains an unapproved inner CPI program")
    if len(dex_cpis) != len(order.route_plan):
        raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 CPI count differs from the quoted route step count")

    pools = []
    amounts = []
    minimum_outputs = []
    seen_pools = set()
    token_index_accounts: dict[int, str] = {}
    for step, inner, decoded_route_step in zip(order.route_plan, dex_cpis, route_semantics.route_steps):
        if inner.stack_height != 2:
            raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 CPI must be a direct depth-2 child of the Jupiter route instruction")
        if len(inner.account_keys) != METEORA_DAMM_V1_SWAP_ACCOUNT_COUNT:
            raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 swap CPI must contain exactly 15 accounts; remaining depeg/referral accounts are unsupported")
        accounts = tuple(_canonical_account(value, f"Meteora DAMM v1 CPI account {position}") for position, value in enumerate(inner.account_keys))
        if len(set(accounts)) != len(accounts):
            raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 swap CPI contains duplicate account keys")
        if accounts[0] != step.amm_key:
            raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 pool differs from the quoted AMM key")
        if accounts[1] == accounts[2]:
            raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 user token accounts unexpectedly alias")
        if accounts[12] != order.taker:
            raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 user signer differs from the Jupiter taker")
        if accounts[13] != METEORA_VAULT_PROGRAM_ID:
            raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 vault program differs from the pinned program ID")
        if accounts[14] != CLASSIC_TOKEN_PROGRAM_ID:
            raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 route must use classic SPL Token")
        decoded = decode_meteora_damm_v1_swap(inner.data)
        if decoded.input_amount_raw != step.input_amount_raw:
            raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 input amount differs from the quoted route-step amount")
        if decoded.minimum_output_amount_raw > step.output_amount_raw:
            raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 minimum output threshold exceeds the quoted route-step output")
        for token_index, account in (
            (decoded_route_step.input_index, accounts[1]),
            (decoded_route_step.output_index, accounts[2]),
        ):
            previous_account = token_index_accounts.setdefault(token_index, account)
            if previous_account != account:
                raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 token accounts do not preserve Jupiter route token-index flow")
        if accounts[0] in seen_pools:
            raise MeteoraDammV1CpiSemanticError("duplicate Meteora DAMM v1 pool appears in the same route")
        seen_pools.add(accounts[0])
        pools.append(accounts[0])
        amounts.append(decoded.input_amount_raw)
        minimum_outputs.append(decoded.minimum_output_amount_raw)

    expected_input_ata = derive_associated_token_address(order.taker, order.input_mint)
    expected_output_ata = derive_associated_token_address(order.taker, order.output_mint)
    output_indexes = tuple(index for index, mint in route_semantics.token_index_mints if mint == order.output_mint)
    if len(output_indexes) != 1:
        raise MeteoraDammV1CpiSemanticError("decoded Jupiter route does not identify exactly one final output token index")
    if token_index_accounts.get(0) != expected_input_ata:
        raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 route input token account is not the taker's classic SPL input ATA")
    if token_index_accounts.get(output_indexes[0]) != expected_output_ata:
        raise MeteoraDammV1CpiSemanticError("Meteora DAMM v1 route output token account is not the taker's classic SPL output ATA")

    return MeteoraDammV1CpiSemanticResult(
        passed=True,
        message_sha256=digest,
        outer_instruction_index=route_semantics.instruction_index,
        cpi_count=len(dex_cpis),
        pool_ids=tuple(pools),
        input_amounts_raw=tuple(amounts),
        minimum_output_amounts_raw=tuple(minimum_outputs),
    )
