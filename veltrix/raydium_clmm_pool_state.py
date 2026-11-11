"""Read-only Raydium CLMM PoolState decoder pinned to an upstream source commit.

This validates the fixed PoolState header, mint pair, vault identities, swap
status and canonical PDA. It does not decode tick arrays, vault token balances,
fee configuration, liquidity depth, oracle state, or DEX CPI account semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct

from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolAccountEvidence, DexPoolEvidenceSnapshot
from veltrix.jupiter_order import JupiterOrderTransaction
from veltrix.jupiter_v6_route import JupiterV6RouteInspection


RAYDIUM_CLMM_PROGRAM_ID = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
RAYDIUM_CLMM_SOURCE_COMMIT = "6d7a0f82d2f293c553c69aaf943377367c6c4cc6"
RAYDIUM_POOL_STATE_DISCRIMINATOR = hashlib.sha256(b"account:PoolState").digest()[:8]
RAYDIUM_POOL_STATE_LENGTH = 1576
RAYDIUM_POOL_HEADER_LENGTH = 397
RAYDIUM_POOL_SEED = b"pool"
RAYDIUM_MIN_TICK = -443636
RAYDIUM_MAX_TICK = 443636
RAYDIUM_SWAP_DISABLED_BIT = 1 << 4
RAYDIUM_FEE_ON_MAX = 2


class RaydiumClmmPoolStateError(ValueError):
    """Raised when a Raydium CLMM pool account is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmPoolState:
    pool_address: str
    bump: int
    amm_config: str
    pool_creator: str
    token_mint_0: str
    token_mint_1: str
    token_vault_0: str
    token_vault_1: str
    observation_key: str
    mint_decimals_0: int
    mint_decimals_1: int
    tick_spacing: int
    liquidity: int
    sqrt_price_x64: int
    tick_current: int
    status: int
    fee_on: int
    seed_index: int
    account_data_sha256: str
    source_commit: str = RAYDIUM_CLMM_SOURCE_COMMIT

    @property
    def swaps_enabled(self) -> bool:
        return not bool(self.status & RAYDIUM_SWAP_DISABLED_BIT)


@dataclass(frozen=True, slots=True)
class RaydiumClmmPoolRouteCheck:
    passed: bool
    pool: RaydiumClmmPoolState
    route_input_mint: str
    route_output_mint: str


def _pubkey(data: bytes, start: int, field: str) -> str:
    raw = data[start:start + 32]
    if len(raw) != 32:
        raise RaydiumClmmPoolStateError(f"pool account is truncated at {field}")
    try:
        key = Pubkey.from_bytes(raw)
    except Exception as exc:
        raise RaydiumClmmPoolStateError(f"pool account has an invalid {field} public key") from exc
    return str(key)


def derive_raydium_clmm_pool_address(
    *,
    amm_config: str,
    token_mint_0: str,
    token_mint_1: str,
    bump: int,
    seed_index: int = 0,
) -> str:
    """Re-derive Raydium PoolState PDA using the exact upstream PoolState::seeds order."""
    if isinstance(bump, bool) or not isinstance(bump, int) or not 0 <= bump <= 255:
        raise RaydiumClmmPoolStateError("Raydium pool bump must be a byte")
    if isinstance(seed_index, bool) or not isinstance(seed_index, int) or not 0 <= seed_index <= 65535:
        raise RaydiumClmmPoolStateError("Raydium pool seed index must be a u16")
    try:
        config_key = Pubkey.from_string(amm_config)
        mint0_key = Pubkey.from_string(token_mint_0)
        mint1_key = Pubkey.from_string(token_mint_1)
        program_key = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
        seeds = [
            RAYDIUM_POOL_SEED,
            bytes(config_key),
            bytes(mint0_key),
            bytes(mint1_key),
            b"" if seed_index == 0 else seed_index.to_bytes(2, "little"),
            bytes((bump,)),
        ]
        address, derived_bump = Pubkey.find_program_address(seeds[:-1], program_key)
        if derived_bump != bump:
            raise RaydiumClmmPoolStateError("Raydium pool PDA bump does not match the canonical derivation")
        return str(address)
    except RaydiumClmmPoolStateError:
        raise
    except Exception as exc:
        raise RaydiumClmmPoolStateError(f"Raydium pool PDA derivation failed: {exc}") from exc


def decode_raydium_clmm_pool_state(
    account: DexPoolAccountEvidence,
    *,
    expected_pool_address: str,
) -> RaydiumClmmPoolState:
    """Decode the fixed 397-byte PoolState header and verify its PDA and owner evidence."""
    if not isinstance(account, DexPoolAccountEvidence):
        raise RaydiumClmmPoolStateError("verified DEX pool account evidence is required")
    try:
        expected_key = str(Pubkey.from_string(expected_pool_address))
        actual_key = str(Pubkey.from_string(account.amm_key))
    except Exception as exc:
        raise RaydiumClmmPoolStateError("expected or observed pool address is invalid") from exc
    if actual_key != expected_key:
        raise RaydiumClmmPoolStateError("pool account evidence address differs from the route AMM key")
    if account.dex_label != "Raydium CLMM":
        raise RaydiumClmmPoolStateError("pool evidence is not labelled Raydium CLMM")
    if account.expected_program_id != RAYDIUM_CLMM_PROGRAM_ID or account.owner_program_id != RAYDIUM_CLMM_PROGRAM_ID:
        raise RaydiumClmmPoolStateError("pool account owner is not the pinned Raydium CLMM program")
    data = account.data_bytes
    if account.executable is not False:
        raise RaydiumClmmPoolStateError("Raydium pool account must be non-executable")
    if isinstance(account.lamports, bool) or not isinstance(account.lamports, int) or account.lamports <= 0:
        raise RaydiumClmmPoolStateError("Raydium pool account must have positive lamports")
    if not isinstance(data, bytes) or account.data_length != len(data) or len(data) != RAYDIUM_POOL_STATE_LENGTH:
        raise RaydiumClmmPoolStateError("Raydium PoolState account length differs from the pinned 1576-byte layout")
    if hashlib.sha256(data).hexdigest() != account.data_sha256:
        raise RaydiumClmmPoolStateError("pool account data hash differs from owner evidence")
    if data[:8] != RAYDIUM_POOL_STATE_DISCRIMINATOR:
        raise RaydiumClmmPoolStateError("Raydium PoolState Anchor discriminator is invalid")

    # Offsets are byte-packed exactly as the upstream #[repr(C, packed)] struct.
    bump = data[8]
    amm_config = _pubkey(data, 9, "amm_config")
    pool_creator = _pubkey(data, 41, "owner")
    mint_0 = _pubkey(data, 73, "token_mint_0")
    mint_1 = _pubkey(data, 105, "token_mint_1")
    vault_0 = _pubkey(data, 137, "token_vault_0")
    vault_1 = _pubkey(data, 169, "token_vault_1")
    observation = _pubkey(data, 201, "observation_key")
    decimals_0 = data[233]
    decimals_1 = data[234]
    tick_spacing = struct.unpack_from("<H", data, 235)[0]
    liquidity = int.from_bytes(data[237:253], "little")
    sqrt_price_x64 = int.from_bytes(data[253:269], "little")
    tick_current = struct.unpack_from("<i", data, 269)[0]
    status = data[389]
    fee_on = data[390]
    seed_index = int.from_bytes(data[391:393], "little")

    default_key = str(Pubkey.default())
    if default_key in {amm_config, pool_creator, mint_0, mint_1, vault_0, vault_1, observation}:
        raise RaydiumClmmPoolStateError("Raydium pool header contains a default public key")
    if mint_0 == mint_1:
        raise RaydiumClmmPoolStateError("Raydium pool token mints must be distinct")
    if bytes(Pubkey.from_string(mint_0)) >= bytes(Pubkey.from_string(mint_1)):
        raise RaydiumClmmPoolStateError("Raydium pool mint order is not canonical")
    if vault_0 == vault_1 or vault_0 in {mint_0, mint_1} or vault_1 in {mint_0, mint_1}:
        raise RaydiumClmmPoolStateError("Raydium pool vault identities are invalid")
    if observation in {mint_0, mint_1, vault_0, vault_1}:
        raise RaydiumClmmPoolStateError("Raydium observation account aliases a mint or vault")
    if tick_spacing <= 0:
        raise RaydiumClmmPoolStateError("Raydium pool tick spacing must be positive")
    if liquidity <= 0 or sqrt_price_x64 <= 0:
        raise RaydiumClmmPoolStateError("Raydium pool liquidity and sqrt price must be positive")
    if not RAYDIUM_MIN_TICK <= tick_current <= RAYDIUM_MAX_TICK:
        raise RaydiumClmmPoolStateError("Raydium current tick is outside the protocol boundary")
    if status & RAYDIUM_SWAP_DISABLED_BIT:
        raise RaydiumClmmPoolStateError("Raydium pool has swaps disabled")
    if fee_on > RAYDIUM_FEE_ON_MAX:
        raise RaydiumClmmPoolStateError("Raydium pool fee_on mode is unknown")

    derived = derive_raydium_clmm_pool_address(
        amm_config=amm_config,
        token_mint_0=mint_0,
        token_mint_1=mint_1,
        bump=bump,
        seed_index=seed_index,
    )
    if derived != actual_key:
        raise RaydiumClmmPoolStateError("Raydium PoolState PDA does not match its config, mint pair, seed index and bump")

    return RaydiumClmmPoolState(
        pool_address=actual_key,
        bump=bump,
        amm_config=amm_config,
        pool_creator=pool_creator,
        token_mint_0=mint_0,
        token_mint_1=mint_1,
        token_vault_0=vault_0,
        token_vault_1=vault_1,
        observation_key=observation,
        mint_decimals_0=decimals_0,
        mint_decimals_1=decimals_1,
        tick_spacing=tick_spacing,
        liquidity=liquidity,
        sqrt_price_x64=sqrt_price_x64,
        tick_current=tick_current,
        status=status,
        fee_on=fee_on,
        seed_index=seed_index,
        account_data_sha256=account.data_sha256,
    )


def validate_raydium_clmm_pool_for_route(
    snapshot: DexPoolEvidenceSnapshot,
    route_inspection: JupiterV6RouteInspection,
    order: JupiterOrderTransaction,
) -> tuple[RaydiumClmmPoolRouteCheck, ...]:
    """Bind each Raydium CLMM quote hop to its owner-verified pool mint pair and swap state."""
    if not isinstance(snapshot, DexPoolEvidenceSnapshot):
        raise RaydiumClmmPoolStateError("verified DEX pool evidence snapshot is required")
    if not isinstance(route_inspection, JupiterV6RouteInspection):
        raise RaydiumClmmPoolStateError("Jupiter V6 route inspection is required")
    if not isinstance(order, JupiterOrderTransaction):
        raise RaydiumClmmPoolStateError("Jupiter order quote is required to bind route-hop mints")
    order_digest = hashlib.sha256(order.message_bytes).hexdigest()
    if (
        snapshot.route_message_sha256 != route_inspection.message_sha256
        or order_digest != route_inspection.message_sha256
    ):
        raise RaydiumClmmPoolStateError("pool evidence, Jupiter order and route inspection message digests differ")
    by_address = {account.amm_key: account for account in snapshot.accounts}
    if len(by_address) != len(snapshot.accounts):
        raise RaydiumClmmPoolStateError("pool evidence contains duplicate AMM addresses")
    if len(route_inspection.route_steps) != len(order.route_plan):
        raise RaydiumClmmPoolStateError("decoded route steps differ in count from the Jupiter quote")
    checks = []
    for step, quote_step in zip(route_inspection.route_steps, order.route_plan):
        if step.amm_key != quote_step.amm_key or step.dex_label != quote_step.label:
            raise RaydiumClmmPoolStateError("decoded route step AMM key or DEX label differs from the Jupiter quote")
        if step.dex_label != "Raydium CLMM":
            continue
        account = by_address.get(step.amm_key)
        if account is None:
            raise RaydiumClmmPoolStateError("Raydium route step has no matching pool account evidence")
        pool = decode_raydium_clmm_pool_state(account, expected_pool_address=step.amm_key)
        if {quote_step.input_mint, quote_step.output_mint} != {pool.token_mint_0, pool.token_mint_1}:
            raise RaydiumClmmPoolStateError("Raydium pool token mints do not match the quoted route hop")
        checks.append(RaydiumClmmPoolRouteCheck(
            passed=True,
            pool=pool,
            route_input_mint=quote_step.input_mint,
            route_output_mint=quote_step.output_mint,
        ))
    if not checks:
        raise RaydiumClmmPoolStateError("route contains no supported Raydium CLMM hop")
    return tuple(checks)
