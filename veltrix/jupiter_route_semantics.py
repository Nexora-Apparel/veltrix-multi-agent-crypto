"""Fail-closed semantic decoder for Jupiter v6 exact-in route instructions.

This module supports only the Anchor `route` instruction and a deliberately
small set of unit-payload DEX enum variants. All other Jupiter instructions,
DEX enum variants, optional destination/fee accounts, and token programs fail
closed. It never signs, submits, or broadcasts transactions.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct

from solders.message import from_bytes_versioned
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_swap_effects import (
    CLASSIC_TOKEN_PROGRAM_ID,
    derive_associated_token_address,
)
from veltrix.jupiter_program_trace import JupiterRouteProgramGateResult
from veltrix.jupiter_swap_effects import JupiterSwapEffectResult
from veltrix.simulation_gate import SimulationEvidence, SimulationGateResult
from veltrix.transaction_validator import TransactionValidationResult


JUPITER_V6_ROUTE_IDL_COMMIT = "e6f77951377847c579112e6a16d8c17c5c092485"
JUPITER_ROUTE_DISCRIMINATOR = hashlib.sha256(b"global:route").digest()[:8]
MAX_ROUTE_PLAN_STEPS = 12
MAX_ROUTE_TOKEN_INDEX = 15
JUPITER_ROUTE_ACCOUNT_COUNT = 7


class JupiterRouteSemanticError(ValueError):
    """Raised when encoded Jupiter route semantics do not match the order."""


# Variant indices are pinned to the Jupiter v6 IDL commit above.
# Only no-payload enum variants are enabled in this first conservative slice.
SUPPORTED_SWAP_VARIANTS = (
    ("Raydium", 7),
    ("Meteora", 19),
    ("Raydium CLMM", 26),
    ("Meteora DLMM", 38),
    ("Raydium CPMM", 46),
)


@dataclass(frozen=True, slots=True)
class JupiterRouteSemanticPolicy:
    jupiter_program_ids: tuple[str, ...]
    label_to_variant_index: tuple[tuple[str, int], ...] = SUPPORTED_SWAP_VARIANTS
    supported_routers: tuple[str, ...] = ("metis",)
    max_slippage_bps: int = 100
    max_platform_fee_bps: int = 0
    max_route_steps: int = MAX_ROUTE_PLAN_STEPS

    def __post_init__(self) -> None:
        for field in ("jupiter_program_ids",):
            value = getattr(self, field)
            if not isinstance(value, tuple) or not value or any(not isinstance(x, str) or not x for x in value):
                raise JupiterRouteSemanticError(f"{field} must be a non-empty immutable tuple")
            if len(set(value)) != len(value):
                raise JupiterRouteSemanticError(f"{field} must not contain duplicates")
        for program_id in self.jupiter_program_ids:
            try:
                if str(Pubkey.from_string(program_id)) != program_id:
                    raise ValueError("non-canonical")
            except Exception as exc:
                raise JupiterRouteSemanticError("jupiter_program_ids contains an invalid public key") from exc
        if not isinstance(self.supported_routers, tuple) or not self.supported_routers:
            raise JupiterRouteSemanticError("supported_routers must be a non-empty immutable tuple")
        if any(router != "metis" for router in self.supported_routers) or len(set(self.supported_routers)) != len(self.supported_routers):
            raise JupiterRouteSemanticError("only the Metis Jupiter v6 route format is supported")
        if not isinstance(self.label_to_variant_index, tuple) or not self.label_to_variant_index:
            raise JupiterRouteSemanticError("label_to_variant_index must be a non-empty immutable tuple")
        labels = []
        variants = []
        for item in self.label_to_variant_index:
            if not isinstance(item, tuple) or len(item) != 2:
                raise JupiterRouteSemanticError("DEX label mappings must be (label, variant-index) tuples")
            label, variant = item
            if not isinstance(label, str) or not label:
                raise JupiterRouteSemanticError("DEX label mapping contains an invalid label")
            if isinstance(variant, bool) or not isinstance(variant, int) or variant not in {v for _, v in SUPPORTED_SWAP_VARIANTS}:
                raise JupiterRouteSemanticError("DEX label mapping references an unsupported Jupiter swap variant")
            labels.append(label)
            variants.append(variant)
        if len(set(labels)) != len(labels) or len(set(variants)) != len(variants):
            raise JupiterRouteSemanticError("DEX label mappings must be one-to-one")
        for field, minimum, maximum in (
            ("max_slippage_bps", 0, 10_000),
            ("max_platform_fee_bps", 0, 10_000),
            ("max_route_steps", 1, MAX_ROUTE_PLAN_STEPS),
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise JupiterRouteSemanticError(f"{field} is outside its permitted range")


@dataclass(frozen=True, slots=True)
class DecodedJupiterRouteStep:
    swap_variant_index: int
    percent: int
    input_index: int
    output_index: int


@dataclass(frozen=True, slots=True)
class DecodedJupiterRoute:
    steps: tuple[DecodedJupiterRouteStep, ...]
    input_amount_raw: int
    quoted_output_amount_raw: int
    slippage_bps: int
    platform_fee_bps: int


@dataclass(frozen=True, slots=True)
class JupiterRouteSemanticResult:
    passed: bool
    message_sha256: str
    jupiter_program_id: str
    instruction_index: int
    route_steps: tuple[DecodedJupiterRouteStep, ...]
    input_amount_raw: int
    quoted_output_amount_raw: int
    slippage_bps: int
    platform_fee_bps: int
    token_index_mints: tuple[tuple[int, str], ...]
    idl_commit: str = JUPITER_V6_ROUTE_IDL_COMMIT


def decode_jupiter_route_instruction_data(data: bytes, *, max_route_steps: int = MAX_ROUTE_PLAN_STEPS) -> DecodedJupiterRoute:
    """Decode the pinned Jupiter v6 Anchor `route` instruction Borsh payload."""
    if not isinstance(data, bytes) or len(data) < 8:
        raise JupiterRouteSemanticError("Jupiter route instruction data is missing or truncated")
    if data[:8] != JUPITER_ROUTE_DISCRIMINATOR:
        raise JupiterRouteSemanticError("Jupiter instruction discriminator is not the supported route variant")
    if isinstance(max_route_steps, bool) or not isinstance(max_route_steps, int) or not 1 <= max_route_steps <= MAX_ROUTE_PLAN_STEPS:
        raise JupiterRouteSemanticError("max_route_steps is outside its permitted range")
    offset = 8

    def take(size: int, field: str) -> bytes:
        nonlocal offset
        if offset + size > len(data):
            raise JupiterRouteSemanticError(f"Jupiter route payload is truncated at {field}")
        value = data[offset:offset + size]
        offset += size
        return value

    count = struct.unpack("<I", take(4, "route-plan length"))[0]
    if not 1 <= count <= max_route_steps:
        raise JupiterRouteSemanticError("Jupiter route-plan length is outside policy")
    steps = []
    for index in range(count):
        variant = take(1, f"route-plan step {index} swap enum")[0]
        if variant not in {value for _, value in SUPPORTED_SWAP_VARIANTS}:
            raise JupiterRouteSemanticError(f"route-plan step {index} uses an unsupported or payload-bearing DEX enum variant")
        percent, input_index, output_index = take(3, f"route-plan step {index} fields")
        if not 1 <= percent <= 100:
            raise JupiterRouteSemanticError(f"route-plan step {index} percent is outside 1..100")
        if input_index > MAX_ROUTE_TOKEN_INDEX or output_index > MAX_ROUTE_TOKEN_INDEX or input_index == output_index:
            raise JupiterRouteSemanticError(f"route-plan step {index} token indexes are invalid")
        steps.append(DecodedJupiterRouteStep(variant, percent, input_index, output_index))

    input_amount, quoted_output = struct.unpack("<QQ", take(16, "route amounts"))
    slippage_bps = struct.unpack("<H", take(2, "slippage basis points"))[0]
    platform_fee_bps = take(1, "platform fee basis points")[0]
    if offset != len(data):
        raise JupiterRouteSemanticError("Jupiter route payload contains trailing bytes")
    if input_amount <= 0 or quoted_output <= 0:
        raise JupiterRouteSemanticError("Jupiter route amounts must be positive")
    if slippage_bps > 10_000:
        raise JupiterRouteSemanticError("Jupiter route slippage basis points exceed 10000")
    return DecodedJupiterRoute(
        steps=tuple(steps),
        input_amount_raw=input_amount,
        quoted_output_amount_raw=quoted_output,
        slippage_bps=slippage_bps,
        platform_fee_bps=platform_fee_bps,
    )


def _bind_route_mints(order: JupiterOrderTransaction, decoded: DecodedJupiterRoute, policy: JupiterRouteSemanticPolicy) -> tuple[tuple[int, str], ...]:
    if len(order.route_plan) != len(decoded.steps):
        raise JupiterRouteSemanticError("encoded Jupiter route step count differs from the quote route plan")
    label_map = dict(policy.label_to_variant_index)
    token_index_mints: dict[int, str] = {0: order.input_mint}
    produced_indexes = {0}
    input_percent_totals: dict[int, int] = {}
    for index, (quoted, encoded) in enumerate(zip(order.route_plan, decoded.steps)):
        expected_variant = label_map.get(quoted.label)
        if expected_variant is None:
            raise JupiterRouteSemanticError(f"quote route uses unsupported DEX label: {quoted.label}")
        if encoded.swap_variant_index != expected_variant:
            raise JupiterRouteSemanticError(f"route-plan step {index} DEX enum does not match quote label")
        if not float(quoted.percent).is_integer() or encoded.percent != int(quoted.percent):
            raise JupiterRouteSemanticError(f"route-plan step {index} split percent differs from quote")
        if encoded.input_index not in produced_indexes:
            raise JupiterRouteSemanticError(f"route-plan step {index} consumes a token index before it is produced")
        input_percent_totals[encoded.input_index] = input_percent_totals.get(encoded.input_index, 0) + encoded.percent
        for token_index, mint in ((encoded.input_index, quoted.input_mint), (encoded.output_index, quoted.output_mint)):
            previous = token_index_mints.setdefault(token_index, mint)
            if previous != mint:
                raise JupiterRouteSemanticError("encoded route token index maps to conflicting quote mints")
        produced_indexes.add(encoded.output_index)
    if any(total != 100 for total in input_percent_totals.values()):
        raise JupiterRouteSemanticError("route split percentages for each input token index must total exactly 100")
    if token_index_mints.get(0) != order.input_mint:
        raise JupiterRouteSemanticError("Jupiter route token index zero does not map to the order input mint")
    if order.output_mint not in token_index_mints.values():
        raise JupiterRouteSemanticError("encoded Jupiter route does not map the order output mint")
    terminal_indexes = produced_indexes - set(input_percent_totals)
    if len(terminal_indexes) != 1 or token_index_mints.get(next(iter(terminal_indexes), -1)) != order.output_mint:
        raise JupiterRouteSemanticError("Jupiter route DAG must terminate at exactly one output-mint token index")
    if len(set(token_index_mints.values())) != len(token_index_mints):
        raise JupiterRouteSemanticError("one mint is mapped to multiple Jupiter route token indexes")
    indexes = sorted(token_index_mints)
    if indexes != list(range(len(indexes))):
        raise JupiterRouteSemanticError("Jupiter route token indexes are not contiguous from zero")
    return tuple(sorted(token_index_mints.items()))


def evaluate_jupiter_route_semantics(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    simulation: SimulationEvidence,
    simulation_gate: SimulationGateResult,
    program_gate: JupiterRouteProgramGateResult,
    swap_effects: JupiterSwapEffectResult,
    policy: JupiterRouteSemanticPolicy,
) -> JupiterRouteSemanticResult:
    """Require the exact serialized Jupiter route instruction to encode the approved quote."""
    if not isinstance(order, JupiterOrderTransaction):
        raise JupiterRouteSemanticError("Jupiter order is required")
    if not isinstance(validation, TransactionValidationResult):
        raise JupiterRouteSemanticError("structural transaction validation is required")
    if not isinstance(simulation, SimulationEvidence):
        raise JupiterRouteSemanticError("simulation evidence is required")
    if not isinstance(simulation_gate, SimulationGateResult) or simulation_gate.approved is not True:
        raise JupiterRouteSemanticError("successful SimulationGate approval is required")
    if not isinstance(program_gate, JupiterRouteProgramGateResult) or program_gate.passed is not True:
        raise JupiterRouteSemanticError("successful Jupiter program trace gate is required")
    if not isinstance(swap_effects, JupiterSwapEffectResult) or swap_effects.passed is not True:
        raise JupiterRouteSemanticError("successful Jupiter quote-to-balance effect gate is required")
    if not isinstance(policy, JupiterRouteSemanticPolicy):
        raise JupiterRouteSemanticError("Jupiter route semantic policy is required")

    if not isinstance(order.message_bytes, bytes) or not order.message_bytes:
        raise JupiterRouteSemanticError("serialized Jupiter message bytes are missing")
    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if any(value != digest for value in (
        validation.message_sha256, simulation.message_sha256,
        simulation_gate.message_sha256, program_gate.message_sha256,
        swap_effects.message_sha256,
    )):
        raise JupiterRouteSemanticError("order, validation, simulation and program trace digests differ")
    if simulation.provider_id != simulation_gate.provider_id or simulation.response_id != simulation_gate.response_id:
        raise JupiterRouteSemanticError("simulation evidence and gate result refer to different RPC responses")
    if simulation.context_slot != simulation_gate.context_slot:
        raise JupiterRouteSemanticError("simulation evidence and gate result context slots differ")
    if simulation.simulation_error is not None:
        raise JupiterRouteSemanticError("simulation reported an execution error")
    if (
        simulation.recent_blockhash != order.recent_blockhash
        or simulation.last_valid_block_height != order.last_valid_block_height
    ):
        raise JupiterRouteSemanticError("simulation blockhash or last-valid height differs from the Jupiter order")
    if tuple(program_gate.inner_program_ids) != tuple(simulation.inner_program_ids):
        raise JupiterRouteSemanticError("program trace inner IDs differ from the simulation evidence")
    expected_input_ata = derive_associated_token_address(order.taker, order.input_mint)
    expected_output_ata = derive_associated_token_address(order.taker, order.output_mint)
    effect_mismatches = []
    if swap_effects.input_token_account != expected_input_ata:
        effect_mismatches.append("input ATA")
    if swap_effects.output_token_account != expected_output_ata:
        effect_mismatches.append("output ATA")
    if swap_effects.input_delta_raw != -order.input_amount_raw:
        effect_mismatches.append("input delta")
    if swap_effects.output_delta_raw != order.output_amount_raw:
        effect_mismatches.append("output delta")
    if swap_effects.minimum_output_amount_raw != order.minimum_output_amount_raw:
        effect_mismatches.append("minimum output")
    if swap_effects.exact_output_amount_raw != order.output_amount_raw:
        effect_mismatches.append("exact output")
    if swap_effects.simulation_context_slot != simulation.context_slot:
        effect_mismatches.append("simulation slot")
    if effect_mismatches:
        raise JupiterRouteSemanticError(
            "quote-to-balance effect evidence differs from the Jupiter order or simulation: "
            + ", ".join(effect_mismatches)
        )
    if order.taker != order.fee_payer or validation.fee_payer != order.taker:
        raise JupiterRouteSemanticError("order taker is not the validated fee payer")
    if order.router not in policy.supported_routers:
        raise JupiterRouteSemanticError("Jupiter router is outside supported semantic policy")
    if program_gate.router != order.router:
        raise JupiterRouteSemanticError("program trace router differs from the order router")
    if (
        tuple(program_gate.top_level_program_ids) != tuple(validation.program_ids)
        or tuple(simulation.top_level_program_ids) != tuple(validation.program_ids)
    ):
        raise JupiterRouteSemanticError("program trace top-level IDs differ from the validated message")
    if tuple(program_gate.route_labels) != tuple(dict.fromkeys(step.label for step in order.route_plan)):
        raise JupiterRouteSemanticError("program trace route labels differ from the Jupiter order")
    if not set(policy.jupiter_program_ids).intersection(program_gate.top_level_program_ids):
        raise JupiterRouteSemanticError("program trace does not identify a locally pinned Jupiter aggregator")
    if order.input_amount_raw <= 0 or order.output_amount_raw <= 0:
        raise JupiterRouteSemanticError("order amounts must be positive")
    if sum(step.input_amount_raw for step in order.route_plan if step.input_mint == order.input_mint) != order.input_amount_raw:
        raise JupiterRouteSemanticError("quoted first-hop input amounts do not sum to the Jupiter order input")
    if sum(step.output_amount_raw for step in order.route_plan if step.output_mint == order.output_mint) != order.output_amount_raw:
        raise JupiterRouteSemanticError("quoted final-hop output amounts do not sum to the Jupiter order output")
    if len(order.message_bytes) == 0:
        raise JupiterRouteSemanticError("serialized message is empty")

    try:
        message = from_bytes_versioned(order.message_bytes)
        instructions = tuple(message.instructions)
    except Exception as exc:
        raise JupiterRouteSemanticError("validated Jupiter message cannot be decoded") from exc
    if tuple(validation.account_keys) == ():
        raise JupiterRouteSemanticError("resolved transaction account-key evidence is missing")

    candidates = []
    for instruction_index, instruction in enumerate(instructions):
        program_index = int(instruction.program_id_index)
        if program_index >= len(validation.account_keys):
            raise JupiterRouteSemanticError("Jupiter instruction program index is outside resolved account keys")
        program_id = validation.account_keys[program_index]
        if program_id in policy.jupiter_program_ids:
            candidates.append((instruction_index, instruction, program_id))
    if len(candidates) != 1:
        raise JupiterRouteSemanticError("transaction must contain exactly one locally pinned Jupiter instruction")

    instruction_index, instruction, jupiter_program_id = candidates[0]
    account_indexes = tuple(bytes(instruction.accounts))
    if len(account_indexes) != JUPITER_ROUTE_ACCOUNT_COUNT:
        raise JupiterRouteSemanticError("only the seven-account Jupiter v6 route instruction is supported")
    static_keys = tuple(str(key) for key in message.account_keys)
    if tuple(validation.account_keys[:len(static_keys)]) != static_keys:
        raise JupiterRouteSemanticError("validated static account-key order differs from the serialized message")
    if any(index >= len(static_keys) for index in account_indexes):
        raise JupiterRouteSemanticError("Jupiter route accounts loaded through address lookup tables are not supported")
    if program_index >= len(static_keys):
        raise JupiterRouteSemanticError("Jupiter program ID must be a static message account")
    accounts = tuple(validation.account_keys[index] for index in account_indexes)

    header = message.header
    required_signatures = int(header.num_required_signatures)
    readonly_signed = int(header.num_readonly_signed_accounts)
    readonly_unsigned = int(header.num_readonly_unsigned_accounts)
    static_count = len(static_keys)
    if (
        required_signatures < 1 or required_signatures > static_count
        or readonly_signed > required_signatures
        or readonly_unsigned > static_count - required_signatures
    ):
        raise JupiterRouteSemanticError("serialized message header has invalid account privileges")

    def account_privileges(index: int) -> tuple[bool, bool]:
        signer = index < required_signatures
        writable = (
            index < required_signatures - readonly_signed
            if signer else index < static_count - readonly_unsigned
        )
        return signer, writable

    # Jupiter route requires a signer authority and writable user token ATAs.
    # The fee payer may elevate the authority's global writable bit, so only
    # its signer bit is constrained here.
    if not account_privileges(account_indexes[1])[0]:
        raise JupiterRouteSemanticError("Jupiter route transfer authority is not a required signer")
    for position in (2, 3):
        signer, writable = account_privileges(account_indexes[position])
        if signer or not writable:
            raise JupiterRouteSemanticError("Jupiter source and destination token accounts must be non-signer writable accounts")
    for position in (0, 5, 4, 6):
        signer, writable = account_privileges(account_indexes[position])
        if signer or writable:
            raise JupiterRouteSemanticError("Jupiter token program, mint and omitted optional accounts must be read-only non-signers")
    expected_source = derive_associated_token_address(order.taker, order.input_mint)
    expected_destination = derive_associated_token_address(order.taker, order.output_mint)
    if accounts[0] != CLASSIC_TOKEN_PROGRAM_ID:
        raise JupiterRouteSemanticError("Jupiter route must use the classic SPL Token program")
    if accounts[1] != order.taker:
        raise JupiterRouteSemanticError("Jupiter route transfer authority differs from the taker")
    if accounts[2] != expected_source or accounts[3] != expected_destination:
        raise JupiterRouteSemanticError("Jupiter route source/destination accounts are not the taker's classic SPL ATAs")
    if accounts[4] != jupiter_program_id or accounts[6] != jupiter_program_id:
        raise JupiterRouteSemanticError("optional destination or platform-fee account is not omitted using the Jupiter program-ID sentinel")
    if accounts[5] != order.output_mint:
        raise JupiterRouteSemanticError("Jupiter route destination mint differs from the order output mint")

    decoded = decode_jupiter_route_instruction_data(bytes(instruction.data), max_route_steps=policy.max_route_steps)
    if decoded.input_amount_raw != order.input_amount_raw:
        raise JupiterRouteSemanticError("encoded Jupiter input amount differs from the requested order amount")
    if decoded.quoted_output_amount_raw != order.output_amount_raw:
        raise JupiterRouteSemanticError("encoded Jupiter quoted output differs from the order quote")
    if decoded.slippage_bps > policy.max_slippage_bps:
        raise JupiterRouteSemanticError("encoded Jupiter slippage exceeds local policy")
    if decoded.platform_fee_bps > policy.max_platform_fee_bps:
        raise JupiterRouteSemanticError("encoded Jupiter platform fee exceeds local policy")
    token_index_mints = _bind_route_mints(order, decoded, policy)
    return JupiterRouteSemanticResult(
        passed=True,
        message_sha256=digest,
        jupiter_program_id=jupiter_program_id,
        instruction_index=instruction_index,
        route_steps=decoded.steps,
        input_amount_raw=decoded.input_amount_raw,
        quoted_output_amount_raw=decoded.quoted_output_amount_raw,
        slippage_bps=decoded.slippage_bps,
        platform_fee_bps=decoded.platform_fee_bps,
        token_index_mints=token_index_mints,
    )
