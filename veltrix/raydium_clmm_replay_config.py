"""Pinned Raydium CLMM AMM-config and dynamic-fee account decoders."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolAccountEvidence
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RAYDIUM_FEE_RATE_DENOMINATOR,
)


RAYDIUM_AMM_CONFIG_SEED = b"amm_config"
RAYDIUM_AMM_CONFIG_DISCRIMINATOR = hashlib.sha256(b"account:AmmConfig").digest()[:8]
RAYDIUM_AMM_CONFIG_LENGTH = 117
RAYDIUM_MAX_TICK_SPACING = 1000


class RaydiumClmmReplayConfigError(ValueError):
    """Raised when pinned Raydium fee/config account evidence is invalid."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmAmmConfig:
    address: str
    bump: int
    index: int
    owner: str
    protocol_fee_rate: int
    trade_fee_rate: int
    tick_spacing: int
    fund_fee_rate: int
    fund_owner: str
    data_sha256: str


def derive_raydium_clmm_amm_config_address(index: int) -> tuple[str, int]:
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 65535:
        raise RaydiumClmmReplayConfigError("AMM config index must fit u16")
    try:
        address, bump = Pubkey.find_program_address(
            [RAYDIUM_AMM_CONFIG_SEED, index.to_bytes(2, "big")],
            Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID),
        )
        return str(address), bump
    except Exception as exc:
        raise RaydiumClmmReplayConfigError("Raydium AMM config PDA derivation failed") from exc


def decode_raydium_clmm_amm_config(
    account: DexPoolAccountEvidence,
    *,
    expected_address: str,
    expected_tick_spacing: int,
) -> RaydiumClmmAmmConfig:
    if not isinstance(account, DexPoolAccountEvidence):
        raise RaydiumClmmReplayConfigError("owner-verified AMM config account evidence is required")
    if account.dex_label not in ("Raydium CLMM", "Raydium CLMM AMM Config"):
        raise RaydiumClmmReplayConfigError("account evidence is not labelled as Raydium CLMM")
    if (
        account.expected_program_id != RAYDIUM_CLMM_PROGRAM_ID
        or account.owner_program_id != RAYDIUM_CLMM_PROGRAM_ID
        or account.executable is not False
        or isinstance(account.lamports, bool)
        or not isinstance(account.lamports, int)
        or account.lamports <= 0
    ):
        raise RaydiumClmmReplayConfigError("AMM config owner, executable flag or lamports are invalid")
    try:
        address = str(Pubkey.from_string(account.amm_key))
        expected = str(Pubkey.from_string(expected_address))
    except Exception as exc:
        raise RaydiumClmmReplayConfigError("AMM config account address is invalid") from exc
    if address != expected:
        raise RaydiumClmmReplayConfigError("AMM config address differs from PoolState.amm_config")
    data = account.data_bytes
    if (
        not isinstance(data, bytes)
        or len(data) != RAYDIUM_AMM_CONFIG_LENGTH
        or account.data_length != RAYDIUM_AMM_CONFIG_LENGTH
        or hashlib.sha256(data).hexdigest() != account.data_sha256
        or data[:8] != RAYDIUM_AMM_CONFIG_DISCRIMINATOR
    ):
        raise RaydiumClmmReplayConfigError("AmmConfig discriminator, length or account hash is invalid")

    bump = data[8]
    index = int.from_bytes(data[9:11], "little")
    owner = str(Pubkey.from_bytes(data[11:43]))
    protocol_fee_rate = int.from_bytes(data[43:47], "little")
    trade_fee_rate = int.from_bytes(data[47:51], "little")
    tick_spacing = int.from_bytes(data[51:53], "little")
    fund_fee_rate = int.from_bytes(data[53:57], "little")
    padding_u32 = int.from_bytes(data[57:61], "little")
    fund_owner = str(Pubkey.from_bytes(data[61:93]))
    padding = data[93:117]

    derived_address, derived_bump = derive_raydium_clmm_amm_config_address(index)
    if derived_address != address or derived_bump != bump:
        raise RaydiumClmmReplayConfigError("AmmConfig PDA does not match its index and bump")
    if (
        owner == str(Pubkey.default())
        or fund_owner == str(Pubkey.default())
        or not 0 < tick_spacing <= RAYDIUM_MAX_TICK_SPACING
        or tick_spacing != expected_tick_spacing
    ):
        raise RaydiumClmmReplayConfigError("AmmConfig owner or tick spacing is invalid for this pool")
    if (
        trade_fee_rate >= RAYDIUM_FEE_RATE_DENOMINATOR
        or protocol_fee_rate > RAYDIUM_FEE_RATE_DENOMINATOR
        or fund_fee_rate > RAYDIUM_FEE_RATE_DENOMINATOR
        or protocol_fee_rate + fund_fee_rate > RAYDIUM_FEE_RATE_DENOMINATOR
    ):
        raise RaydiumClmmReplayConfigError("AmmConfig fee rates exceed the pinned Raydium limits")
    if padding_u32 != 0 or any(padding):
        raise RaydiumClmmReplayConfigError("AmmConfig contains unsupported non-zero reserved padding")

    return RaydiumClmmAmmConfig(
        address=address,
        bump=bump,
        index=index,
        owner=owner,
        protocol_fee_rate=protocol_fee_rate,
        trade_fee_rate=trade_fee_rate,
        tick_spacing=tick_spacing,
        fund_fee_rate=fund_fee_rate,
        fund_owner=fund_owner,
        data_sha256=account.data_sha256,
    )
