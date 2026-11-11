"""Read-only semantic inspection of Jupiter V6 route instructions.

This validates the route payload, quote binding, fixed user/program account roles,
and AMM keys. DEX-specific trailing account layouts remain explicitly incomplete;
this module is not a signer authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct

from solders.message import MessageV0, from_bytes_versioned
from solders.pubkey import Pubkey

from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_swap_effects import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    CLASSIC_TOKEN_PROGRAM_ID,
    derive_associated_token_address,
)
from veltrix.transaction_validator import TransactionValidationResult


JUPITER_V6_PROGRAM_ID = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
ROUTE_DISCRIMINATOR = bytes.fromhex("e517cb977ae3ad2a")
SHARED_ACCOUNTS_ROUTE_DISCRIMINATOR = bytes.fromhex("c1209b3341d69c81")
EVENT_AUTHORITY_SEED = b"__event_authority"
SHARED_AUTHORITY_SEED = b"authority"


class JupiterV6RouteError(ValueError):
    """Raised when Jupiter V6 route data or fixed account roles disagree with the order."""


@dataclass(frozen=True, slots=True)
class JupiterV6RoutePolicy:
    allowed_jupiter_program_ids: tuple[str, ...]
    allowed_dex_labels: tuple[str, ...]
    max_route_steps: int = 12
    max_slippage_bps: int = 500
    max_platform_fee_bps: int = 0

    def __post_init__(self) -> None:
        for field in ("allowed_jupiter_program_ids", "allowed_dex_labels"):
            value = getattr(self, field)
            if not isinstance(value, tuple) or not value:
                raise JupiterV6RouteError(f"{field} must be a non-empty immutable tuple")
            if any(not isinstance(item, str) or not item for item in value) or len(set(value)) != len(value):
                raise JupiterV6RouteError(f"{field} contains invalid or duplicate values")
        for program_id in self.allowed_jupiter_program_ids:
            try:
                if str(Pubkey.from_string(program_id)) != program_id:
                    raise ValueError("non-canonical")
            except Exception as exc:
                raise JupiterV6RouteError("allowed Jupiter program ID is invalid") from exc
        if isinstance(self.max_route_steps, bool) or not isinstance(self.max_route_steps, int) or not 1 <= self.max_route_steps <= 64:
            raise JupiterV6RouteError("max_route_steps must be between 1 and 64")
        for field in ("max_slippage_bps", "max_platform_fee_bps"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
                raise JupiterV6RouteError(f"{field} must be between 0 and 10000")


@dataclass(frozen=True, slots=True)
class DecodedJupiterRouteStep:
    swap_variant: int
    dex_label: str
    percent: int
    input_index: int
    output_index: int
    amm_key: str = ""


@dataclass(frozen=True, slots=True)
class JupiterV6RouteInspection:
    message_sha256: str
    instruction_name: str
    program_id: str
    instruction_index: int
    shared_authority_id: int | None
    route_steps: tuple[DecodedJupiterRouteStep, ...]
    input_amount_raw: int
    quoted_output_amount_raw: int
    slippage_bps: int
    platform_fee_bps: int
    remaining_account_count: int
    fully_decoded: bool
    blocking_reasons: tuple[str, ...]


# Jupiter V6 IDL Swap enum variant index -> (public route label, payload encoding).
# Only variants with a reviewed payload layout are accepted here.
_SWAP_VARIANTS: dict[int, tuple[str, str]] = {
    7: ("Raydium", ""),
    17: ("Orca Whirlpool", "bool"),
    18: ("Invariant", "bool"),
    19: ("Meteora", ""),
    26: ("Raydium CLMM", ""),
    38: ("Meteora DLMM", ""),
    40: ("Raydium CLMM", ""),
    46: ("Raydium CPMM", ""),
}


class _Cursor:
    def __init__(self, data: bytes, offset: int) -> None:
        self.data = data
        self.offset = offset

    def read(self, size: int) -> bytes:
        if size < 0 or self.offset + size > len(self.data):
            raise JupiterV6RouteError("Jupiter route instruction data is truncated")
        value = self.data[self.offset:self.offset + size]
        self.offset += size
        return value

    def u8(self) -> int:
        return self.read(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.read(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.read(8))[0]


def _pda(program_id: str, seeds: tuple[bytes, ...]) -> str:
    try:
        address, _ = Pubkey.find_program_address(list(seeds), Pubkey.from_string(program_id))
        return str(address)
    except Exception as exc:
        raise JupiterV6RouteError("Jupiter PDA derivation failed") from exc


def _runtime_privileges(message: MessageV0, account_index: int) -> tuple[bool, bool]:
    static_count = len(message.account_keys)
    required = int(message.header.num_required_signatures)
    readonly_signed = int(message.header.num_readonly_signed_accounts)
    readonly_unsigned = int(message.header.num_readonly_unsigned_accounts)
    if (
        required < 1
        or required > static_count
        or readonly_signed > required
        or readonly_unsigned > static_count - required
    ):
        raise JupiterV6RouteError("Solana message header contains impossible privilege counts")
    if account_index < 0:
        raise JupiterV6RouteError("Jupiter account index is negative")
    if account_index < static_count:
        signer = account_index < required
        if signer:
            writable = account_index < required - readonly_signed
        else:
            writable = account_index < static_count - readonly_unsigned
        return signer, writable
    loaded_writable_count = sum(len(lookup.writable_indexes) for lookup in message.address_table_lookups)
    loaded_readonly_count = sum(len(lookup.readonly_indexes) for lookup in message.address_table_lookups)
    if account_index >= static_count + loaded_writable_count + loaded_readonly_count:
        raise JupiterV6RouteError("Jupiter account index is outside the resolved privilege map")
    return False, account_index < static_count + loaded_writable_count


def _validate_fixed_privileges(
    message: MessageV0,
    account_indices: tuple[int, ...],
    *,
    shared: bool,
) -> None:
    if shared:
        expected = (
            (False, False), (False, False), (True, True),
            (False, True), (False, True), (False, True), (False, True),
            (False, False), (False, False), (False, False), (False, False),
            (False, False), (False, False),
        )
    else:
        expected = (
            (False, False), (True, True), (False, True),
            (False, True), (False, False), (False, False),
            (False, False), (False, False), (False, False),
        )
    for position, (expected_signer, expected_writable) in enumerate(expected):
        actual = _runtime_privileges(message, account_indices[position])
        if actual != (expected_signer, expected_writable):
            raise JupiterV6RouteError(
                f"Jupiter fixed account privilege mismatch at position {position}"
            )


def _decode_route_payload(data: bytes, *, shared: bool, policy: JupiterV6RoutePolicy):
    cursor = _Cursor(data, 8)
    authority_id = cursor.u8() if shared else None
    if authority_id is not None and authority_id > 7:
        raise JupiterV6RouteError("shared route authority index is outside the supported 0..7 range")
    count = cursor.u32()
    if count < 1 or count > policy.max_route_steps:
        raise JupiterV6RouteError("on-chain route step count is empty or exceeds policy")

    steps = []
    for _ in range(count):
        variant = cursor.u8()
        spec = _SWAP_VARIANTS.get(variant)
        if spec is None:
            raise JupiterV6RouteError(f"Jupiter Swap enum variant {variant} has no reviewed decoder")
        label, payload = spec
        if payload == "bool":
            direction = cursor.u8()
            if direction not in (0, 1):
                raise JupiterV6RouteError("Jupiter Swap enum boolean payload is invalid")
        percent = cursor.u8()
        input_index = cursor.u8()
        output_index = cursor.u8()
        if not 1 <= percent <= 100:
            raise JupiterV6RouteError("Jupiter route step percent must be between 1 and 100")
        if output_index <= input_index or output_index > count:
            raise JupiterV6RouteError("Jupiter route token indexes are not topologically ordered")
        steps.append(DecodedJupiterRouteStep(
            swap_variant=variant,
            dex_label=label,
            percent=percent,
            input_index=input_index,
            output_index=output_index,
        ))

    input_amount = cursor.u64()
    quoted_output = cursor.u64()
    slippage_bps = cursor.u16()
    platform_fee_bps = cursor.u8()
    if cursor.offset != len(data):
        raise JupiterV6RouteError("Jupiter route instruction contains trailing or unparsed data")
    if input_amount <= 0 or quoted_output <= 0:
        raise JupiterV6RouteError("Jupiter route instruction amounts must be positive")
    if slippage_bps > policy.max_slippage_bps:
        raise JupiterV6RouteError("Jupiter route slippage exceeds policy")
    if platform_fee_bps > policy.max_platform_fee_bps:
        raise JupiterV6RouteError("Jupiter platform fee is not approved")
    return authority_id, tuple(steps), input_amount, quoted_output, slippage_bps, platform_fee_bps


def inspect_jupiter_v6_route(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    policy: JupiterV6RoutePolicy,
) -> JupiterV6RouteInspection:
    """Inspect one Metis/Jupiter V6 route instruction without authorizing execution."""
    if not isinstance(order, JupiterOrderTransaction):
        raise JupiterV6RouteError("Jupiter order is required")
    if not isinstance(validation, TransactionValidationResult):
        raise JupiterV6RouteError("structural transaction validation is required")
    if not isinstance(policy, JupiterV6RoutePolicy):
        raise JupiterV6RouteError("Jupiter V6 route policy is required")
    if order.router != "metis":
        raise JupiterV6RouteError("only Metis/Jupiter V6 route instructions are supported")
    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if digest != validation.message_sha256:
        raise JupiterV6RouteError("order message digest differs from structural validation")
    if order.taker != order.fee_payer or validation.fee_payer != order.taker:
        raise JupiterV6RouteError("Jupiter taker must be the validated fee payer")
    if order.taker not in validation.required_signers:
        raise JupiterV6RouteError("Jupiter taker must be a required transaction signer")

    try:
        message = from_bytes_versioned(order.message_bytes)
        if not isinstance(message, MessageV0):
            raise JupiterV6RouteError("only v0 messages are supported")
        message.sanitize()
        static_keys = tuple(str(key) for key in message.account_keys)
        runtime_keys = validation.account_keys
        if not isinstance(runtime_keys, tuple) or len(runtime_keys) < len(static_keys):
            raise JupiterV6RouteError("resolved account-key evidence is incomplete")
        if tuple(runtime_keys[:len(static_keys)]) != static_keys:
            raise JupiterV6RouteError("static account keys differ from structural validation")
        program_ids = tuple(runtime_keys[int(ix.program_id_index)] for ix in message.instructions)
        if program_ids != validation.program_ids or len(program_ids) != validation.instruction_count:
            raise JupiterV6RouteError("top-level instructions differ from structural validation")
    except JupiterV6RouteError:
        raise
    except Exception as exc:
        raise JupiterV6RouteError("serialized Jupiter order message could not be decoded") from exc

    matches = [
        (index, ix, program_ids[index])
        for index, ix in enumerate(message.instructions)
        if program_ids[index] in policy.allowed_jupiter_program_ids
    ]
    if len(matches) != 1:
        raise JupiterV6RouteError("transaction must contain exactly one locally pinned Jupiter V6 instruction")
    instruction_index, instruction, program_id = matches[0]
    data = bytes(instruction.data)
    if data.startswith(ROUTE_DISCRIMINATOR):
        instruction_name = "route"
        shared = False
        fixed_count = 9
    elif data.startswith(SHARED_ACCOUNTS_ROUTE_DISCRIMINATOR):
        instruction_name = "sharedAccountsRoute"
        shared = True
        fixed_count = 13
    else:
        raise JupiterV6RouteError("Jupiter instruction discriminator is not an approved exact-in route")

    authority_id, decoded_steps, input_amount, quoted_output, slippage_bps, platform_fee_bps = _decode_route_payload(
        data,
        shared=shared,
        policy=policy,
    )
    if len(decoded_steps) != len(order.route_plan):
        raise JupiterV6RouteError("on-chain route step count differs from Jupiter order routePlan")

    grouped_percent: dict[int, int] = {}
    grouped_bps: dict[int, int] = {}
    token_mints: dict[int, str] = {0: order.input_mint, len(decoded_steps): order.output_mint}
    for decoded, quoted in zip(decoded_steps, order.route_plan):
        if decoded.dex_label not in policy.allowed_dex_labels or decoded.dex_label != quoted.label:
            raise JupiterV6RouteError("on-chain DEX enum differs from the quoted route label")
        if (
            isinstance(quoted.percent, bool)
            or not isinstance(quoted.percent, (int, float))
            or not float(quoted.percent).is_integer()
            or int(quoted.percent) != decoded.percent
            or isinstance(quoted.bps, bool)
            or not isinstance(quoted.bps, int)
            or quoted.bps != decoded.percent * 100
        ):
            raise JupiterV6RouteError("on-chain route split differs from the quoted percent/bps")
        for token_index, mint in ((decoded.input_index, quoted.input_mint), (decoded.output_index, quoted.output_mint)):
            if token_index in token_mints and token_mints[token_index] != mint:
                raise JupiterV6RouteError("quoted route mint graph differs from the on-chain token indexes")
            token_mints[token_index] = mint
        grouped_percent[decoded.input_index] = grouped_percent.get(decoded.input_index, 0) + decoded.percent
        grouped_bps[decoded.input_index] = grouped_bps.get(decoded.input_index, 0) + quoted.bps
    if token_mints.get(0) != order.input_mint or token_mints.get(len(decoded_steps)) != order.output_mint:
        raise JupiterV6RouteError("quoted route endpoints differ from the order input/output mints")
    if any(value != 100 for value in grouped_percent.values()) or any(value != 10_000 for value in grouped_bps.values()):
        raise JupiterV6RouteError("route branches do not allocate exactly 100 percent / 10000 bps")

    if input_amount != order.input_amount_raw or quoted_output != order.output_amount_raw:
        raise JupiterV6RouteError("on-chain route amounts differ from the Jupiter order quote")
    minimum_from_slippage = (quoted_output * (10_000 - slippage_bps)) // 10_000
    if minimum_from_slippage != order.minimum_output_amount_raw:
        raise JupiterV6RouteError("on-chain slippage does not produce Jupiter's quoted minimum output")

    try:
        account_indices = tuple(int(value) for value in bytes(instruction.accounts))
    except Exception as exc:
        raise JupiterV6RouteError("Jupiter route account indexes are malformed") from exc
    if len(account_indices) < fixed_count:
        raise JupiterV6RouteError("Jupiter route instruction is missing fixed account roles")
    if any(index < 0 or index >= len(runtime_keys) for index in account_indices):
        raise JupiterV6RouteError("Jupiter route account index is outside the resolved account list")
    accounts = tuple(runtime_keys[index] for index in account_indices)
    _validate_fixed_privileges(message, account_indices, shared=shared)

    expected_input_ata = derive_associated_token_address(order.taker, order.input_mint)
    expected_output_ata = derive_associated_token_address(order.taker, order.output_mint)
    event_authority = _pda(program_id, (EVENT_AUTHORITY_SEED,))

    if accounts[0] != CLASSIC_TOKEN_PROGRAM_ID:
        raise JupiterV6RouteError("Jupiter route token program must be classic SPL Token")
    if not shared:
        expected = (
            CLASSIC_TOKEN_PROGRAM_ID,
            order.taker,
            expected_input_ata,
            expected_output_ata,
            program_id,
            order.output_mint,
            program_id,
            event_authority,
            program_id,
        )
    else:
        assert authority_id is not None
        program_authority = _pda(program_id, (SHARED_AUTHORITY_SEED, bytes([authority_id])))
        program_source_ata = derive_associated_token_address(program_authority, order.input_mint)
        program_destination_ata = derive_associated_token_address(program_authority, order.output_mint)
        expected = (
            CLASSIC_TOKEN_PROGRAM_ID,
            program_authority,
            order.taker,
            expected_input_ata,
            program_source_ata,
            program_destination_ata,
            expected_output_ata,
            order.input_mint,
            order.output_mint,
            program_id,
            program_id,
            event_authority,
            program_id,
        )
    if accounts[:fixed_count] != expected:
        raise JupiterV6RouteError("Jupiter fixed account roles differ from the taker, mint, token-account or PDA policy")

    remaining = accounts[fixed_count:]
    remaining_set = set(remaining)
    missing_amm_keys = [step.amm_key for step in order.route_plan if step.amm_key not in remaining_set]
    if missing_amm_keys:
        raise JupiterV6RouteError("one or more quoted AMM keys are absent from Jupiter remaining accounts")

    blocking = ("DEX-specific remaining account roles are not yet decoded",) if remaining else ()
    bound_steps = tuple(
        DecodedJupiterRouteStep(
            amm_key=quoted.amm_key,
            swap_variant=decoded.swap_variant,
            dex_label=decoded.dex_label,
            percent=decoded.percent,
            input_index=decoded.input_index,
            output_index=decoded.output_index,
        )
        for decoded, quoted in zip(decoded_steps, order.route_plan)
    )
    return JupiterV6RouteInspection(
        message_sha256=digest,
        instruction_name=instruction_name,
        program_id=program_id,
        instruction_index=instruction_index,
        shared_authority_id=authority_id,
        route_steps=bound_steps,
        input_amount_raw=input_amount,
        quoted_output_amount_raw=quoted_output,
        slippage_bps=slippage_bps,
        platform_fee_bps=platform_fee_bps,
        remaining_account_count=len(remaining),
        fully_decoded=not blocking,
        blocking_reasons=blocking,
    )
