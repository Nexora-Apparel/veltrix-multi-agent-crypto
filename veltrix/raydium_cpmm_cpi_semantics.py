"""Narrow semantic gate for Jupiter-invoked Raydium CPMM swap_base_input CPIs.

Supports only classic SPL-token exact-input swaps using the pinned Raydium
CPMM account layout and instruction discriminator. No signing or submission.
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


RAYDIUM_CPMM_PROGRAM_ID = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
RAYDIUM_CPMM_SOURCE_COMMIT = "3d69122790e97cd6aff24e8ba95940793773f58c"
RAYDIUM_CPMM_SWAP_BASE_INPUT_DISCRIMINATOR = hashlib.sha256(b"global:swap_base_input").digest()[:8]
CLASSIC_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
RAYDIUM_CPMM_AUTH_SEED = b"vault_and_lp_mint_auth_seed"
RAYDIUM_CPMM_SWAP_ACCOUNT_COUNT = 13


class RaydiumCpmmCpiSemanticError(ValueError):
    """Raised when Raydium CPMM CPI bytes or accounts differ from the approved route."""


@dataclass(frozen=True, slots=True)
class DecodedRaydiumCpmmSwap:
    amount_in_raw: int
    minimum_amount_out_raw: int


@dataclass(frozen=True, slots=True)
class RaydiumCpmmCpiPolicy:
    allowed_program_ids: tuple[str, ...] = (RAYDIUM_CPMM_PROGRAM_ID,)
    allowed_auxiliary_program_ids: tuple[str, ...] = (CLASSIC_TOKEN_PROGRAM_ID,)
    max_cpi_count: int = 12

    def __post_init__(self) -> None:
        for field in ("allowed_program_ids", "allowed_auxiliary_program_ids"):
            values = getattr(self, field)
            if not isinstance(values, tuple) or not values:
                raise RaydiumCpmmCpiSemanticError(f"{field} must be a non-empty immutable tuple")
            for value in values:
                try:
                    if str(Pubkey.from_string(value)) != value:
                        raise ValueError("non-canonical")
                except Exception as exc:
                    raise RaydiumCpmmCpiSemanticError(f"{field} contains an invalid public key") from exc
            if len(set(values)) != len(values):
                raise RaydiumCpmmCpiSemanticError(f"{field} must not contain duplicates")
        if set(self.allowed_program_ids) & set(self.allowed_auxiliary_program_ids):
            raise RaydiumCpmmCpiSemanticError("DEX and auxiliary program allowlists must not overlap")
        if isinstance(self.max_cpi_count, bool) or not isinstance(self.max_cpi_count, int) or not 1 <= self.max_cpi_count <= 64:
            raise RaydiumCpmmCpiSemanticError("max_cpi_count is outside its permitted range")


@dataclass(frozen=True, slots=True)
class RaydiumCpmmCpiSemanticResult:
    passed: bool
    message_sha256: str
    outer_instruction_index: int
    cpi_count: int
    pool_ids: tuple[str, ...]
    input_amounts_raw: tuple[int, ...]
    minimum_output_amounts_raw: tuple[int, ...]
    source_commit: str = RAYDIUM_CPMM_SOURCE_COMMIT


def decode_raydium_cpmm_swap_base_input(data: bytes) -> DecodedRaydiumCpmmSwap:
    if not isinstance(data, bytes) or len(data) != 24:
        raise RaydiumCpmmCpiSemanticError("Raydium CPMM swap_base_input data must be exactly 24 bytes")
    if data[:8] != RAYDIUM_CPMM_SWAP_BASE_INPUT_DISCRIMINATOR:
        raise RaydiumCpmmCpiSemanticError("Raydium CPMM CPI is not the pinned swap_base_input instruction")
    amount_in, minimum_out = struct.unpack("<QQ", data[8:24])
    if amount_in <= 0 or minimum_out <= 0:
        raise RaydiumCpmmCpiSemanticError("Raydium CPMM input amount and minimum output must be positive")
    return DecodedRaydiumCpmmSwap(amount_in, minimum_out)


def _canonical_account(value: str, field: str) -> str:
    try:
        canonical = str(Pubkey.from_string(value))
    except Exception as exc:
        raise RaydiumCpmmCpiSemanticError(f"{field} is not a valid public key") from exc
    if canonical != value:
        raise RaydiumCpmmCpiSemanticError(f"{field} is not canonical")
    return canonical


def _authority(program_id: str) -> str:
    try:
        address, _ = Pubkey.find_program_address(
            [RAYDIUM_CPMM_AUTH_SEED],
            Pubkey.from_string(program_id),
        )
        return str(address)
    except Exception as exc:
        raise RaydiumCpmmCpiSemanticError("could not derive Raydium CPMM vault authority PDA") from exc


def evaluate_raydium_cpmm_cpis(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    simulation: SimulationEvidence,
    simulation_gate: SimulationGateResult,
    program_gate: JupiterRouteProgramGateResult,
    route_semantics: JupiterRouteSemanticResult,
    policy: RaydiumCpmmCpiPolicy,
) -> RaydiumCpmmCpiSemanticResult:
    """Decode every Raydium CPMM CPI and bind its pool/amount/accounts to the quote."""
    if not isinstance(order, JupiterOrderTransaction):
        raise RaydiumCpmmCpiSemanticError("Jupiter order is required")
    if not isinstance(validation, TransactionValidationResult):
        raise RaydiumCpmmCpiSemanticError("validated transaction is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumCpmmCpiSemanticError("detailed simulation evidence is required")
    if not isinstance(simulation_gate, SimulationGateResult) or simulation_gate.approved is not True:
        raise RaydiumCpmmCpiSemanticError("successful simulation gate is required")
    if not isinstance(program_gate, JupiterRouteProgramGateResult) or program_gate.passed is not True:
        raise RaydiumCpmmCpiSemanticError("successful Jupiter program trace gate is required")
    if not isinstance(route_semantics, JupiterRouteSemanticResult) or route_semantics.passed is not True:
        raise RaydiumCpmmCpiSemanticError("successful Jupiter route semantic gate is required")
    if not isinstance(policy, RaydiumCpmmCpiPolicy):
        raise RaydiumCpmmCpiSemanticError("Raydium CPMM CPI policy is required")

    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if any(value != digest for value in (
        validation.message_sha256, simulation.message_sha256, simulation_gate.message_sha256,
        program_gate.message_sha256, route_semantics.message_sha256,
    )):
        raise RaydiumCpmmCpiSemanticError("order, validation, simulation and semantic evidence digests differ")
    if simulation.simulation_error is not None:
        raise RaydiumCpmmCpiSemanticError("simulation reported an execution error")
    if (
        simulation.recent_blockhash != order.recent_blockhash
        or simulation.last_valid_block_height != order.last_valid_block_height
    ):
        raise RaydiumCpmmCpiSemanticError("simulation blockhash or last-valid height differs from the Jupiter order")
    if simulation.provider_id != simulation_gate.provider_id or simulation.response_id != simulation_gate.response_id:
        raise RaydiumCpmmCpiSemanticError("simulation gate and detailed evidence refer to different RPC responses")
    if simulation.context_slot != simulation_gate.context_slot:
        raise RaydiumCpmmCpiSemanticError("simulation context slots differ")
    if (
        tuple(simulation.top_level_program_ids) != tuple(validation.program_ids)
        or tuple(program_gate.top_level_program_ids) != tuple(validation.program_ids)
    ):
        raise RaydiumCpmmCpiSemanticError("top-level program trace differs from the validated transaction")
    if tuple(item.program_id for item in simulation.inner_instructions) != simulation.inner_program_ids:
        raise RaydiumCpmmCpiSemanticError("detailed inner CPI evidence is missing or inconsistent")
    if tuple(program_gate.inner_program_ids) != simulation.inner_program_ids:
        raise RaydiumCpmmCpiSemanticError("Jupiter program trace differs from detailed simulation CPI evidence")
    if tuple(program_gate.route_labels) != tuple(dict.fromkeys(step.label for step in order.route_plan)):
        raise RaydiumCpmmCpiSemanticError("Jupiter program trace labels differ from the quote route plan")
    if not set(policy.allowed_program_ids).intersection(program_gate.dex_program_ids):
        raise RaydiumCpmmCpiSemanticError("Raydium CPMM program is not present in the approved DEX program trace")
    if len(simulation.inner_instructions) != simulation.inner_instruction_count or not simulation.inner_instructions:
        raise RaydiumCpmmCpiSemanticError("complete detailed inner CPI instruction evidence is required")
    if not order.route_plan or len(order.route_plan) > policy.max_cpi_count:
        raise RaydiumCpmmCpiSemanticError("Jupiter route plan is empty or exceeds the CPI count policy")
    if len(route_semantics.route_steps) != len(order.route_plan):
        raise RaydiumCpmmCpiSemanticError("decoded Jupiter route-step count differs from the quote")
    if any(step.label != "Raydium CPMM" for step in order.route_plan):
        raise RaydiumCpmmCpiSemanticError("this DEX semantic gate supports only all-Raydium-CPMM routes")
    if any(step.swap_variant_index != 46 for step in route_semantics.route_steps):
        raise RaydiumCpmmCpiSemanticError("decoded Jupiter route contains a non-CPMM swap enum")

    dex_cpis = []
    for inner in simulation.inner_instructions:
        if inner.outer_instruction_index != route_semantics.instruction_index:
            continue
        if inner.program_id in policy.allowed_program_ids:
            dex_cpis.append(inner)
        elif inner.program_id not in policy.allowed_auxiliary_program_ids:
            raise RaydiumCpmmCpiSemanticError("simulation contains an unapproved inner CPI program")
    if len(dex_cpis) != len(order.route_plan):
        raise RaydiumCpmmCpiSemanticError("Raydium CPMM CPI count differs from the quoted route step count")

    pool_ids = []
    amounts = []
    minimum_outputs = []
    seen_pools = set()
    token_index_accounts: dict[int, str] = {}
    for step, inner, decoded_route_step in zip(order.route_plan, dex_cpis, route_semantics.route_steps):
        if inner.stack_height != 2:
            raise RaydiumCpmmCpiSemanticError("Raydium CPMM CPI must be a direct depth-2 child of the Jupiter route instruction")
        if len(inner.account_keys) != RAYDIUM_CPMM_SWAP_ACCOUNT_COUNT:
            raise RaydiumCpmmCpiSemanticError("Raydium CPMM swap CPI must contain exactly 13 accounts")
        accounts = tuple(_canonical_account(value, f"Raydium CPMM CPI account {position}") for position, value in enumerate(inner.account_keys))
        unique_accounts = tuple(accounts[index] for index in (0, 1, 2, 3, 4, 5, 6, 7, 10, 11, 12))
        if len(set(unique_accounts)) != len(unique_accounts):
            raise RaydiumCpmmCpiSemanticError("Raydium CPMM swap CPI contains aliased state or token accounts")
        if accounts[0] != order.taker:
            raise RaydiumCpmmCpiSemanticError("Raydium CPMM payer differs from the Jupiter taker")
        if accounts[1] != _authority(inner.program_id):
            raise RaydiumCpmmCpiSemanticError("Raydium CPMM vault authority PDA is invalid")
        if accounts[3] != step.amm_key:
            raise RaydiumCpmmCpiSemanticError("Raydium CPMM pool state differs from the quoted AMM key")
        if accounts[4] == accounts[5] or accounts[6] == accounts[7]:
            raise RaydiumCpmmCpiSemanticError("Raydium CPMM user token accounts or pool vaults unexpectedly alias")
        if accounts[8] != CLASSIC_TOKEN_PROGRAM_ID or accounts[9] != CLASSIC_TOKEN_PROGRAM_ID:
            raise RaydiumCpmmCpiSemanticError("Raydium CPMM route must use classic SPL Token for both assets")
        if accounts[10] != step.input_mint or accounts[11] != step.output_mint:
            raise RaydiumCpmmCpiSemanticError("Raydium CPMM input/output mint accounts differ from the quoted route step")
        decoded = decode_raydium_cpmm_swap_base_input(inner.data)
        if decoded.amount_in_raw != step.input_amount_raw:
            raise RaydiumCpmmCpiSemanticError("Raydium CPMM input amount differs from the quoted route-step amount")
        if decoded.minimum_amount_out_raw > step.output_amount_raw:
            raise RaydiumCpmmCpiSemanticError("Raydium CPMM minimum output threshold exceeds the quoted route-step output")
        for token_index, account in (
            (decoded_route_step.input_index, accounts[4]),
            (decoded_route_step.output_index, accounts[5]),
        ):
            previous_account = token_index_accounts.setdefault(token_index, account)
            if previous_account != account:
                raise RaydiumCpmmCpiSemanticError("Raydium CPMM token accounts do not preserve Jupiter route token-index flow")
        if accounts[3] in seen_pools:
            raise RaydiumCpmmCpiSemanticError("duplicate Raydium CPMM pool state appears in the same route")
        seen_pools.add(accounts[3])
        pool_ids.append(accounts[3])
        amounts.append(decoded.amount_in_raw)
        minimum_outputs.append(decoded.minimum_amount_out_raw)

    expected_input_ata = derive_associated_token_address(order.taker, order.input_mint)
    expected_output_ata = derive_associated_token_address(order.taker, order.output_mint)
    output_indexes = tuple(index for index, mint in route_semantics.token_index_mints if mint == order.output_mint)
    if len(output_indexes) != 1:
        raise RaydiumCpmmCpiSemanticError("decoded Jupiter route does not identify exactly one final output token index")
    if token_index_accounts.get(0) != expected_input_ata:
        raise RaydiumCpmmCpiSemanticError("Raydium CPMM route input token account is not the taker's classic SPL input ATA")
    if token_index_accounts.get(output_indexes[0]) != expected_output_ata:
        raise RaydiumCpmmCpiSemanticError("Raydium CPMM route output token account is not the taker's classic SPL output ATA")

    return RaydiumCpmmCpiSemanticResult(
        passed=True,
        message_sha256=digest,
        outer_instruction_index=route_semantics.instruction_index,
        cpi_count=len(dex_cpis),
        pool_ids=tuple(pool_ids),
        input_amounts_raw=tuple(amounts),
        minimum_output_amounts_raw=tuple(minimum_outputs),
    )
