"""Decode and reconcile the pinned Raydium CLMM SwapEvent emitted after token transfers."""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
from dataclasses import dataclass

from solders.pubkey import Pubkey

from veltrix.raydium_clmm_cpi import (
    RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
    RAYDIUM_CLMM_SOURCE_COMMIT,
    RaydiumClmmCpiResult,
)
from veltrix.raydium_clmm_swap_loop_settlement import RaydiumSwapLoopSettlementResult


RAYDIUM_SWAP_EVENT_DISCRIMINATOR = hashlib.sha256(b"event:SwapEvent").digest()[:8]
RAYDIUM_SWAP_EVENT_LENGTH = 221
_PROGRAM_INVOKE = re.compile(r"^Program ([1-9A-HJ-NP-Za-km-z]+) invoke \[\d+\]$")
_PROGRAM_RETURN = re.compile(r"^Program ([1-9A-HJ-NP-Za-km-z]+) (?:success|failed:.*)$")


class RaydiumSwapEventError(ValueError):
    """Raised when a Raydium SwapEvent is missing, malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmSwapEvent:
    pool_state: str
    sender: str
    token_account_0: str
    token_account_1: str
    amount_0: int
    transfer_fee_0: int
    amount_1: int
    transfer_fee_1: int
    zero_for_one: bool
    sqrt_price_x64: int
    liquidity: int
    tick: int
    trade_fee_0: int
    trade_fee_1: int
    source_commit: str = RAYDIUM_CLMM_SOURCE_COMMIT


@dataclass(frozen=True, slots=True)
class RaydiumSwapEventReconciliationResult:
    event: RaydiumClmmSwapEvent
    cpi: RaydiumClmmCpiResult
    loop_settlement: RaydiumSwapLoopSettlementResult
    matched: bool


def decode_raydium_clmm_swap_event(data: bytes) -> RaydiumClmmSwapEvent:
    """Decode the exact Anchor/Borsh SwapEvent layout in the pinned pool.rs."""
    if not isinstance(data, bytes) or len(data) != RAYDIUM_SWAP_EVENT_LENGTH:
        raise RaydiumSwapEventError("Raydium SwapEvent payload must be exactly 221 bytes")
    if data[:8] != RAYDIUM_SWAP_EVENT_DISCRIMINATOR:
        raise RaydiumSwapEventError("Anchor SwapEvent discriminator is invalid")

    try:
        pool_state = str(Pubkey.from_bytes(data[8:40]))
        sender = str(Pubkey.from_bytes(data[40:72]))
        token_account_0 = str(Pubkey.from_bytes(data[72:104]))
        token_account_1 = str(Pubkey.from_bytes(data[104:136]))
    except Exception as exc:
        raise RaydiumSwapEventError("SwapEvent contains an invalid public key") from exc

    amount_0, transfer_fee_0, amount_1, transfer_fee_1 = struct.unpack_from("<QQQQ", data, 136)
    direction = data[168]
    if direction not in (0, 1):
        raise RaydiumSwapEventError("SwapEvent zero_for_one field is not a canonical Borsh boolean")
    sqrt_price_x64 = int.from_bytes(data[169:185], "little")
    liquidity = int.from_bytes(data[185:201], "little")
    tick = struct.unpack_from("<i", data, 201)[0]
    trade_fee_0, trade_fee_1 = struct.unpack_from("<QQ", data, 205)
    if amount_0 == 0 or amount_1 == 0:
        raise RaydiumSwapEventError("SwapEvent input and output amounts must both be non-zero")
    if sqrt_price_x64 == 0:
        raise RaydiumSwapEventError("SwapEvent sqrt price must be positive")

    return RaydiumClmmSwapEvent(
        pool_state=pool_state,
        sender=sender,
        token_account_0=token_account_0,
        token_account_1=token_account_1,
        amount_0=amount_0,
        transfer_fee_0=transfer_fee_0,
        amount_1=amount_1,
        transfer_fee_1=transfer_fee_1,
        zero_for_one=bool(direction),
        sqrt_price_x64=sqrt_price_x64,
        liquidity=liquidity,
        tick=tick,
        trade_fee_0=trade_fee_0,
        trade_fee_1=trade_fee_1,
    )


def extract_raydium_clmm_swap_event(
    logs: tuple[str, ...],
    *,
    program_id: str = RAYDIUM_CLMM_MAINNET_PROGRAM_ID,
) -> RaydiumClmmSwapEvent:
    """Extract exactly one SwapEvent from Program data emitted inside the Raydium invocation."""
    if not isinstance(logs, tuple) or not logs or any(not isinstance(line, str) for line in logs):
        raise RaydiumSwapEventError("complete ordered transaction log lines are required")
    if program_id != RAYDIUM_CLMM_MAINNET_PROGRAM_ID:
        raise RaydiumSwapEventError("only the pinned Raydium CLMM program ID is supported")
    stack: list[str] = []
    events: list[RaydiumClmmSwapEvent] = []

    for line in logs:
        invoke = _PROGRAM_INVOKE.match(line)
        if invoke:
            stack.append(invoke.group(1))
            continue
        returned = _PROGRAM_RETURN.match(line)
        if returned:
            returned_program = returned.group(1)
            if not stack or stack[-1] != returned_program:
                raise RaydiumSwapEventError("program invocation log stack is unbalanced")
            stack.pop()
            continue
        if line.startswith("Program data: ") and stack and stack[-1] == program_id:
            encoded = line[len("Program data: "):].strip()
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise RaydiumSwapEventError("Raydium Program data log is not valid base64") from exc
            if raw[:8] == RAYDIUM_SWAP_EVENT_DISCRIMINATOR:
                events.append(decode_raydium_clmm_swap_event(raw))

    if stack:
        raise RaydiumSwapEventError("transaction logs ended with an unclosed program invocation")
    if len(events) != 1:
        raise RaydiumSwapEventError("transaction logs must contain exactly one Raydium CLMM SwapEvent")
    return events[0]


def reconcile_raydium_clmm_swap_event(
    event: RaydiumClmmSwapEvent,
    cpi: RaydiumClmmCpiResult,
    loop_settlement: RaydiumSwapLoopSettlementResult,
) -> RaydiumSwapEventReconciliationResult:
    """Require event amounts and final state to match the exact simulated CPI and replay."""
    if not isinstance(event, RaydiumClmmSwapEvent):
        raise RaydiumSwapEventError("decoded Raydium CLMM SwapEvent is required")
    if not isinstance(cpi, RaydiumClmmCpiResult) or cpi.passed is not True:
        raise RaydiumSwapEventError("successful Raydium CLMM CPI evidence is required")
    if not isinstance(loop_settlement, RaydiumSwapLoopSettlementResult):
        raise RaydiumSwapEventError("completed outer-loop settlement is required")
    loop = loop_settlement.loop
    settlement = loop_settlement.settlement
    pool_after = settlement.state.pool

    token_account_0 = cpi.input_token_account if loop.zero_for_one else cpi.output_token_account
    token_account_1 = cpi.output_token_account if loop.zero_for_one else cpi.input_token_account
    if (
        event.pool_state != cpi.pool_state
        or event.sender != cpi.payer
        or event.token_account_0 != token_account_0
        or event.token_account_1 != token_account_1
    ):
        raise RaydiumSwapEventError("SwapEvent pool, sender or token-account roles differ from the simulated CPI")
    if (
        event.amount_0 != settlement.amounts.amount_0
        or event.amount_1 != settlement.amounts.amount_1
        or event.trade_fee_0 != settlement.amounts.trade_fee_0
        or event.trade_fee_1 != settlement.amounts.trade_fee_1
    ):
        raise RaydiumSwapEventError("SwapEvent amounts or trade fees differ from deterministic settlement")
    if (
        event.zero_for_one is not loop.zero_for_one
        or event.sqrt_price_x64 != pool_after.sqrt_price_x64
        or event.liquidity != pool_after.liquidity
        or event.tick != pool_after.tick_current
    ):
        raise RaydiumSwapEventError("SwapEvent direction or final price/liquidity/tick differs from PoolState")
    if (
        event.transfer_fee_0 != 0
        or event.transfer_fee_1 != 0
        or settlement.transfer_fee_0 != 0
        or settlement.transfer_fee_1 != 0
    ):
        raise RaydiumSwapEventError("non-zero transfer fees are unsupported by the pinned classic-token swap path")
    if (
        cpi.message_sha256 != loop.tick_array_snapshot.route_message_sha256
        or cpi.amount_raw != loop.amount_specified
        or cpi.sqrt_price_limit_x64 != loop.sqrt_price_limit_x64
    ):
        raise RaydiumSwapEventError("SwapEvent reconciliation inputs are not bound to the same simulated CPI")
    return RaydiumSwapEventReconciliationResult(
        event=event,
        cpi=cpi,
        loop_settlement=loop_settlement,
        matched=True,
    )
