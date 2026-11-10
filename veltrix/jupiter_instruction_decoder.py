"""Strict Borsh decoder for supported Jupiter v6 exact-in route instructions.

IDL source: jup-ag/instruction-parser, pinned upstream revision
e6f77951377847c579112e6a16d8c17c5c092485. Only `route` and
`sharedAccountsRoute` are supported. Token-ledger, exact-out, route-v2,
unknown and unreviewed variants fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

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
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
ROUTE_DISCRIMINATOR = hashlib.sha256(b"global:route").digest()[:8]
SHARED_ACCOUNTS_ROUTE_DISCRIMINATOR = hashlib.sha256(b"global:shared_accounts_route").digest()[:8]


class JupiterInstructionDecodeError(ValueError):
    """Raised when the Jupiter instruction is malformed or outside local policy."""


# Enum order and Borsh field layouts are taken from the pinned official Jupiter v6 IDL.
SWAP_VARIANTS: tuple[tuple[str, str], ...] = (
    ("Saber", ""),
    ("SaberAddDecimalsDeposit", ""),
    ("SaberAddDecimalsWithdraw", ""),
    ("TokenSwap", ""),
    ("Sencha", ""),
    ("Step", ""),
    ("Cropper", ""),
    ("Raydium", ""),
    ("Crema", "bool"),
    ("Lifinity", ""),
    ("Mercurial", ""),
    ("Cykura", ""),
    ("Serum", "side"),
    ("MarinadeDeposit", ""),
    ("MarinadeUnstake", ""),
    ("Aldrin", "side"),
    ("AldrinV2", "side"),
    ("Whirlpool", "bool"),
    ("Invariant", "bool"),
    ("Meteora", ""),
    ("GooseFX", ""),
    ("DeltaFi", "bool"),
    ("Balansol", ""),
    ("MarcoPolo", "bool"),
    ("Dradex", "side"),
    ("LifinityV2", ""),
    ("RaydiumClmm", ""),
    ("Openbook", "side"),
    ("Phoenix", "side"),
    ("Symmetry", "u64_u64"),
    ("TokenSwapV2", ""),
    ("HeliumTreasuryManagementRedeemV0", ""),
    ("StakeDexStakeWrappedSol", ""),
    ("StakeDexSwapViaStake", "u32"),
    ("GooseFXV2", ""),
    ("Perps", ""),
    ("PerpsAddLiquidity", ""),
    ("PerpsRemoveLiquidity", ""),
    ("MeteoraDlmm", ""),
    ("OpenBookV2", "side"),
    ("RaydiumClmmV2", ""),
    ("StakeDexPrefundWithdrawStakeAndDepositStake", "u32"),
    ("Clone", "u8_bool_bool"),
    ("SanctumS", "u8_u8_u32_u32"),
    ("SanctumSAddLiquidity", "u8_u32"),
    ("SanctumSRemoveLiquidity", "u8_u32"),
    ("RaydiumCP", ""),
    ("WhirlpoolSwapV2", "bool_option_remaining"),
    ("OneIntro", ""),
    ("PumpdotfunWrappedBuy", ""),
    ("PumpdotfunWrappedSell", ""),
    ("PerpsV2", ""),
    ("PerpsV2AddLiquidity", ""),
    ("PerpsV2RemoveLiquidity", ""),
    ("MoonshotWrappedBuy", ""),
    ("MoonshotWrappedSell", ""),
    ("StabbleStableSwap", ""),
    ("StabbleWeightedSwap", ""),
    ("Obric", "bool"),
    ("FoxBuyFromEstimatedCost", ""),
    ("FoxClaimPartial", "bool"),
    ("SolFi", "bool"),
)

DEFAULT_VARIANT_TO_LABEL: tuple[tuple[str, str], ...] = (
    ("Raydium", "Raydium"),
    ("RaydiumClmm", "Raydium CLMM"),
    ("RaydiumClmmV2", "Raydium CLMM"),
    ("RaydiumCP", "Raydium CP"),
    ("Meteora", "Meteora"),
    ("MeteoraDlmm", "Meteora DLMM"),
    ("Whirlpool", "Whirlpool"),
    ("WhirlpoolSwapV2", "Whirlpool"),
    ("PumpdotfunWrappedBuy", "Pump.Fun"),
    ("PumpdotfunWrappedSell", "Pump.Fun"),
    ("MoonshotWrappedBuy", "Moonshot"),
    ("MoonshotWrappedSell", "Moonshot"),
    ("SolFi", "SolFi"),
)


class _BorshReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def read(self, size: int) -> bytes:
        if size < 0 or self.offset + size > len(self.data):
            raise JupiterInstructionDecodeError("Jupiter instruction data is truncated")
        value = self.data[self.offset:self.offset + size]
        self.offset += size
        return value

    def u8(self) -> int:
        return int.from_bytes(self.read(1), "little")

    def u16(self) -> int:
        return int.from_bytes(self.read(2), "little")

    def u32(self) -> int:
        return int.from_bytes(self.read(4), "little")

    def u64(self) -> int:
        return int.from_bytes(self.read(8), "little")

    def boolean(self) -> bool:
        value = self.u8()
        if value not in (0, 1):
            raise JupiterInstructionDecodeError("Borsh boolean is not encoded as 0 or 1")
        return bool(value)

    def remaining_accounts_info(self) -> tuple[tuple[int, int], ...]:
        option_tag = self.u8()
        if option_tag == 0:
            return ()
        if option_tag != 1:
            raise JupiterInstructionDecodeError("remaining accounts option tag is invalid")
        count = self.u32()
        if count > 16:
            raise JupiterInstructionDecodeError("remaining accounts slice count exceeds policy")
        slices = []
        for _ in range(count):
            accounts_type = self.u8()
            length = self.u8()
            if accounts_type not in (0, 1) or length == 0:
                raise JupiterInstructionDecodeError("remaining accounts slice is invalid")
            slices.append((accounts_type, length))
        return tuple(slices)

    def swap_variant(self) -> tuple[str, tuple[object, ...]]:
        tag = self.u8()
        if tag >= len(SWAP_VARIANTS):
            raise JupiterInstructionDecodeError("Jupiter swap enum tag is unknown")
        name, layout = SWAP_VARIANTS[tag]
        fields: list[object] = []
        if layout == "bool":
            fields.append(self.boolean())
        elif layout == "side":
            side = self.u8()
            if side not in (0, 1):
                raise JupiterInstructionDecodeError("Jupiter side enum tag is invalid")
            fields.append(side)
        elif layout == "u64_u64":
            fields.extend((self.u64(), self.u64()))
        elif layout == "u32":
            fields.append(self.u32())
        elif layout == "u8_bool_bool":
            fields.extend((self.u8(), self.boolean(), self.boolean()))
        elif layout == "u8_u8_u32_u32":
            fields.extend((self.u8(), self.u8(), self.u32(), self.u32()))
        elif layout == "u8_u32":
            fields.extend((self.u8(), self.u32()))
        elif layout == "bool_option_remaining":
            fields.extend((self.boolean(), self.remaining_accounts_info()))
        elif layout:
            raise JupiterInstructionDecodeError("Jupiter swap variant layout is not implemented")
        return name, tuple(fields)


@dataclass(frozen=True, slots=True)
class JupiterSwapInstructionPolicy:
    max_route_steps: int = 12
    max_slippage_bps: int = 500
    allowed_swap_variants: tuple[str, ...] = tuple(name for name, _ in DEFAULT_VARIANT_TO_LABEL)
    variant_to_label: tuple[tuple[str, str], ...] = DEFAULT_VARIANT_TO_LABEL
    require_zero_platform_fee: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.max_route_steps, bool) or not isinstance(self.max_route_steps, int) or not 1 <= self.max_route_steps <= 64:
            raise JupiterInstructionDecodeError("max_route_steps must be between 1 and 64")
        if isinstance(self.max_slippage_bps, bool) or not isinstance(self.max_slippage_bps, int) or not 0 <= self.max_slippage_bps <= 10_000:
            raise JupiterInstructionDecodeError("max_slippage_bps must be between 0 and 10000")
        if not isinstance(self.allowed_swap_variants, tuple) or not self.allowed_swap_variants:
            raise JupiterInstructionDecodeError("allowed_swap_variants must be a non-empty immutable tuple")
        known = {name for name, _ in SWAP_VARIANTS}
        if len(set(self.allowed_swap_variants)) != len(self.allowed_swap_variants) or any(name not in known for name in self.allowed_swap_variants):
            raise JupiterInstructionDecodeError("allowed_swap_variants contains an unknown or duplicate variant")
        if not isinstance(self.variant_to_label, tuple) or not self.variant_to_label:
            raise JupiterInstructionDecodeError("variant_to_label must be a non-empty immutable tuple")
        names = []
        for pair in self.variant_to_label:
            if not isinstance(pair, tuple) or len(pair) != 2 or pair[0] not in known or not isinstance(pair[1], str) or not pair[1]:
                raise JupiterInstructionDecodeError("variant_to_label contains an invalid mapping")
            names.append(pair[0])
        if len(names) != len(set(names)):
            raise JupiterInstructionDecodeError("variant_to_label contains duplicate variant names")
        if any(name not in names for name in self.allowed_swap_variants):
            raise JupiterInstructionDecodeError("every allowed swap variant must have an explicit DEX label mapping")
        if not isinstance(self.require_zero_platform_fee, bool):
            raise JupiterInstructionDecodeError("require_zero_platform_fee must be a boolean")


@dataclass(frozen=True, slots=True)
class DecodedJupiterRouteStep:
    swap_variant: str
    dex_label: str
    variant_fields: tuple[object, ...]
    percent: int
    input_index: int
    output_index: int


@dataclass(frozen=True, slots=True)
class JupiterSwapInstructionResult:
    message_sha256: str
    instruction_name: str
    program_id: str
    transfer_authority: str
    input_token_account: str
    output_token_account: str
    input_mint: str
    output_mint: str
    input_amount_raw: int
    quoted_output_amount_raw: int
    slippage_bps: int
    platform_fee_bps: int
    shared_route_id: int | None
    route_plan: tuple[DecodedJupiterRouteStep, ...]


def _decode_route_data(data: bytes, *, instruction_name: str, policy: JupiterSwapInstructionPolicy) -> tuple[tuple[DecodedJupiterRouteStep, ...], int, int, int, int, int | None]:
    reader = _BorshReader(data)
    discriminator = reader.read(8)
    if instruction_name == "route":
        if discriminator != ROUTE_DISCRIMINATOR:
            raise JupiterInstructionDecodeError("route instruction discriminator mismatch")
        shared_route_id = None
    elif instruction_name == "sharedAccountsRoute":
        if discriminator != SHARED_ACCOUNTS_ROUTE_DISCRIMINATOR:
            raise JupiterInstructionDecodeError("sharedAccountsRoute discriminator mismatch")
        shared_route_id = reader.u8()
    else:
        raise JupiterInstructionDecodeError("Jupiter instruction variant is not an approved exact-in route")

    count = reader.u32()
    if count <= 0 or count > policy.max_route_steps:
        raise JupiterInstructionDecodeError("Jupiter route plan is empty or exceeds the configured step limit")
    labels = dict(policy.variant_to_label)
    steps = []
    for _ in range(count):
        variant, fields = reader.swap_variant()
        if variant not in policy.allowed_swap_variants:
            raise JupiterInstructionDecodeError(f"Jupiter DEX swap variant {variant} is not allowlisted")
        percent = reader.u8()
        input_index = reader.u8()
        output_index = reader.u8()
        if percent <= 0 or percent > 100:
            raise JupiterInstructionDecodeError("Jupiter route step percent is invalid")
        if input_index >= output_index or output_index > count:
            raise JupiterInstructionDecodeError("Jupiter route plan is not a valid topologically sorted DAG")
        steps.append(DecodedJupiterRouteStep(
            swap_variant=variant,
            dex_label=labels[variant],
            variant_fields=fields,
            percent=percent,
            input_index=input_index,
            output_index=output_index,
        ))
    if not any(step.input_index == 0 for step in steps) or not any(step.output_index == count for step in steps):
        raise JupiterInstructionDecodeError("Jupiter route plan does not connect the input token to the final output")
    outgoing_percentages: dict[int, int] = {}
    reachable = {0}
    for step in steps:
        outgoing_percentages[step.input_index] = outgoing_percentages.get(step.input_index, 0) + step.percent
        if step.input_index in reachable:
            reachable.add(step.output_index)
    if any(total != 100 for total in outgoing_percentages.values()):
        raise JupiterInstructionDecodeError("Jupiter route split percentages do not total 100 for each input node")
    if count not in reachable:
        raise JupiterInstructionDecodeError("Jupiter route DAG does not reach the final output node")
    in_amount = reader.u64()
    quoted_out_amount = reader.u64()
    slippage_bps = reader.u16()
    platform_fee_bps = reader.u8()
    if reader.offset != len(data):
        raise JupiterInstructionDecodeError("Jupiter route instruction contains trailing or unknown data")
    return tuple(steps), in_amount, quoted_out_amount, slippage_bps, platform_fee_bps, shared_route_id


def _assert_privileges(
    validation: TransactionValidationResult,
    index: int,
    *,
    signer: bool,
    writable: bool,
    role: str,
) -> None:
    if index < 0 or index >= len(validation.account_keys):
        raise JupiterInstructionDecodeError(f"{role} account index is out of range")
    if len(validation.account_signers) != len(validation.account_keys) or len(validation.account_writable) != len(validation.account_keys):
        raise JupiterInstructionDecodeError("validated account privilege evidence is incomplete")
    if validation.account_signers[index] is not signer or validation.account_writable[index] is not writable:
        raise JupiterInstructionDecodeError(f"{role} signer/writable privilege differs from the Jupiter IDL")
def _assert_account(
    validation: TransactionValidationResult,
    index: int,
    expected_key: str,
    *,
    signer: bool,
    writable: bool,
    role: str,
) -> None:
    if index < 0 or index >= len(validation.account_keys):
        raise JupiterInstructionDecodeError(f"{role} account index is out of range")
    if validation.account_keys[index] != expected_key:
        raise JupiterInstructionDecodeError(f"{role} account key differs from the approved taker/mint")
    if len(validation.account_signers) != len(validation.account_keys) or len(validation.account_writable) != len(validation.account_keys):
        raise JupiterInstructionDecodeError("validated account privilege evidence is incomplete")
    if validation.account_signers[index] is not signer or validation.account_writable[index] is not writable:
        raise JupiterInstructionDecodeError(f"{role} signer/writable privilege differs from the Jupiter IDL")


def decode_jupiter_swap_instruction(
    order: JupiterOrderTransaction,
    *,
    validation: TransactionValidationResult,
    policy: JupiterSwapInstructionPolicy,
) -> JupiterSwapInstructionResult:
    """Decode and bind one supported Jupiter v6 exact-in instruction to its order."""
    if not isinstance(order, JupiterOrderTransaction):
        raise JupiterInstructionDecodeError("Jupiter v0 order is required")
    if not isinstance(validation, TransactionValidationResult):
        raise JupiterInstructionDecodeError("structural transaction validation is required")
    if not isinstance(policy, JupiterSwapInstructionPolicy):
        raise JupiterInstructionDecodeError("Jupiter instruction policy is required")
    if order.taker != order.fee_payer or validation.fee_payer != order.taker:
        raise JupiterInstructionDecodeError("Jupiter taker must be the validated transaction fee payer")
    digest = hashlib.sha256(order.message_bytes).hexdigest()
    if digest != validation.message_sha256:
        raise JupiterInstructionDecodeError("Jupiter order and structural validation message digests differ")
    if order.transaction_version != 0:
        raise JupiterInstructionDecodeError("only Jupiter v0 order transactions are supported")
    if not validation.account_keys or validation.account_keys[0] != order.taker:
        raise JupiterInstructionDecodeError("validated account key zero is not the taker fee payer")
    if len(validation.account_signers) != len(validation.account_keys) or len(validation.account_writable) != len(validation.account_keys):
        raise JupiterInstructionDecodeError("validated account privilege evidence is incomplete")

    try:
        message = from_bytes_versioned(order.message_bytes)
        if not isinstance(message, MessageV0):
            raise JupiterInstructionDecodeError("only v0 Jupiter order messages are supported")
        message.sanitize()
        if str(message.recent_blockhash) != order.recent_blockhash:
            raise JupiterInstructionDecodeError("Jupiter order blockhash differs from the decoded message")
    except JupiterInstructionDecodeError:
        raise
    except Exception as exc:
        raise JupiterInstructionDecodeError("Jupiter order message could not be decoded") from exc

    jupiter_instructions = []
    for instruction in message.instructions:
        program_index = int(instruction.program_id_index)
        if program_index >= len(validation.account_keys):
            raise JupiterInstructionDecodeError("Jupiter instruction program index is out of range")
        if validation.account_keys[program_index] == JUPITER_V6_PROGRAM_ID:
            jupiter_instructions.append((instruction, program_index))
    if len(jupiter_instructions) != 1 or validation.program_ids.count(JUPITER_V6_PROGRAM_ID) != 1:
        raise JupiterInstructionDecodeError("transaction must contain exactly one top-level Jupiter v6 instruction")

    instruction, program_index = jupiter_instructions[0]
    program_id = validation.account_keys[program_index]
    account_indexes = tuple(int(index) for index in bytes(instruction.accounts))
    account_keys = tuple(validation.account_keys[index] if 0 <= index < len(validation.account_keys) else "" for index in account_indexes)
    data = bytes(instruction.data)
    if data[:8] == ROUTE_DISCRIMINATOR:
        instruction_name = "route"
        if len(account_indexes) != 7:
            raise JupiterInstructionDecodeError("route instruction must contain the exact seven-account IDL layout")
        authority_position, source_position, destination_position, destination_mint_position = 1, 2, 3, 5
        optional_positions = (4, 6)
    elif data[:8] == SHARED_ACCOUNTS_ROUTE_DISCRIMINATOR:
        instruction_name = "sharedAccountsRoute"
        if len(account_indexes) != 11:
            raise JupiterInstructionDecodeError("sharedAccountsRoute instruction must contain the exact eleven-account IDL layout")
        authority_position, source_position, destination_position, destination_mint_position = 2, 3, 6, 8
        optional_positions = (9, 10)
    else:
        raise JupiterInstructionDecodeError("Jupiter instruction discriminator is not a supported exact-in route")

    if account_keys[0] != CLASSIC_TOKEN_PROGRAM_ID:
        raise JupiterInstructionDecodeError("Jupiter route must use the classic SPL Token program")
    input_ata = derive_associated_token_address(order.taker, order.input_mint)
    output_ata = derive_associated_token_address(order.taker, order.output_mint)
    _assert_account(validation, account_indexes[0], CLASSIC_TOKEN_PROGRAM_ID, signer=False, writable=False, role="token program")
    _assert_account(validation, account_indexes[authority_position], order.taker, signer=True, writable=False, role="transfer authority")
    _assert_account(validation, account_indexes[source_position], input_ata, signer=False, writable=True, role="user source token account")
    _assert_account(validation, account_indexes[destination_position], output_ata, signer=False, writable=True, role="user destination token account")
    _assert_account(validation, account_indexes[destination_mint_position], order.output_mint, signer=False, writable=False, role="destination mint")
    _assert_privileges(validation, account_indexes[0], signer=False, writable=False, role="token program")
    _assert_privileges(validation, account_indexes[authority_position], signer=True, writable=False, role="transfer authority")
    _assert_privileges(validation, account_indexes[source_position], signer=False, writable=True, role="user source token account")
    _assert_privileges(validation, account_indexes[destination_position], signer=False, writable=True, role="user destination token account")
    _assert_privileges(validation, account_indexes[destination_mint_position], signer=False, writable=False, role="destination mint")
    if instruction_name == "sharedAccountsRoute":
        source_mint_position = 7
        _assert_account(validation, account_indexes[source_mint_position], order.input_mint, signer=False, writable=False, role="source mint")
        _assert_privileges(validation, account_indexes[1], signer=False, writable=False, role="Jupiter program authority")
        _assert_privileges(validation, account_indexes[4], signer=False, writable=True, role="program source token account")
        _assert_privileges(validation, account_indexes[5], signer=False, writable=True, role="program destination token account")
        if account_keys[1] == SYSTEM_PROGRAM_ID:
            raise JupiterInstructionDecodeError("shared route program authority cannot be the system program")
        if account_keys[4] in (input_ata, output_ata, SYSTEM_PROGRAM_ID) or account_keys[5] in (input_ata, output_ata, SYSTEM_PROGRAM_ID):
            raise JupiterInstructionDecodeError("shared route program token accounts cannot alias user ATAs or default placeholders")
        if account_keys[4] == account_keys[5]:
            raise JupiterInstructionDecodeError("shared route program source and destination token accounts must differ")
    for position in optional_positions:
        _assert_account(validation, account_indexes[position], SYSTEM_PROGRAM_ID, signer=False, writable=False, role="unsupported optional fee/Token-2022 account")

    route_plan, in_amount, quoted_out_amount, slippage_bps, platform_fee_bps, shared_route_id = _decode_route_data(
        data,
        instruction_name=instruction_name,
        policy=policy,
    )
    if in_amount != order.input_amount_raw or quoted_out_amount != order.output_amount_raw:
        raise JupiterInstructionDecodeError("Jupiter route amounts differ from the API order")
    if slippage_bps > policy.max_slippage_bps:
        raise JupiterInstructionDecodeError("Jupiter route slippage exceeds local policy")
    if order.slippage_bps is not None and slippage_bps != order.slippage_bps:
        raise JupiterInstructionDecodeError("Jupiter route slippage differs from the API order")
    if policy.require_zero_platform_fee and platform_fee_bps != 0:
        raise JupiterInstructionDecodeError("Jupiter platform fee is not permitted by local policy")
    if len(route_plan) != len(order.route_plan):
        raise JupiterInstructionDecodeError("Jupiter instruction route step count differs from the API route plan")
    for decoded, quoted in zip(route_plan, order.route_plan):
        if decoded.dex_label != quoted.label:
            raise JupiterInstructionDecodeError("Jupiter instruction DEX variant differs from the API route label")
        if quoted.percent != decoded.percent or quoted.bps != decoded.percent * 100:
            raise JupiterInstructionDecodeError("Jupiter instruction split percentage differs from the API route plan")
    first_layer_input = sum(quoted.input_amount_raw for decoded, quoted in zip(route_plan, order.route_plan) if decoded.input_index == 0)
    final_layer_output = sum(quoted.output_amount_raw for decoded, quoted in zip(route_plan, order.route_plan) if decoded.output_index == len(route_plan))
    if first_layer_input != order.input_amount_raw or final_layer_output != order.output_amount_raw:
        raise JupiterInstructionDecodeError("Jupiter route-plan raw amounts do not reconcile to the order")

    return JupiterSwapInstructionResult(
        message_sha256=digest,
        instruction_name=instruction_name,
        program_id=program_id,
        transfer_authority=order.taker,
        input_token_account=input_ata,
        output_token_account=output_ata,
        input_mint=order.input_mint,
        output_mint=order.output_mint,
        input_amount_raw=in_amount,
        quoted_output_amount_raw=quoted_out_amount,
        slippage_bps=slippage_bps,
        platform_fee_bps=platform_fee_bps,
        shared_route_id=shared_route_id,
        route_plan=route_plan,
    )
