"""Same-provider, same-or-newer-slot Raydium CLMM vault token-account verification."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib

from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolEvidenceSnapshot
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RaydiumClmmPoolRouteCheck,
)
from veltrix.simulation_gate import SimulationEvidence
from veltrix.solana_rpc_simulation import SolanaRpcSimulationAdapter


CLASSIC_SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
RAYDIUM_POOL_VAULT_SEED = b"pool_vault"
CLASSIC_SPL_TOKEN_ACCOUNT_LENGTH = 165
TOKEN_ACCOUNT_INITIALIZED = 1


class RaydiumClmmVaultError(ValueError):
    """Raised when a Raydium pool vault is not the expected initialized token account."""


@dataclass(frozen=True, slots=True)
class RaydiumClmmVaultAccount:
    address: str
    pool_address: str
    mint: str
    token_program_id: str
    token_authority: str
    amount_raw: int
    state: int
    lamports: int
    data_sha256: str
    account_data: bytes = b""


@dataclass(frozen=True, slots=True)
class RaydiumClmmVaultSnapshot:
    route_message_sha256: str
    simulation_response_id: str
    simulation_context_slot: int
    provider_id: str
    cluster: str
    genesis_hash: str
    context_slot: int
    vaults: tuple[RaydiumClmmVaultAccount, ...]
    execution_ready: bool = False


def derive_raydium_clmm_vault_address(*, pool_address: str, mint: str) -> str:
    try:
        pool = Pubkey.from_string(pool_address)
        mint_key = Pubkey.from_string(mint)
        program = Pubkey.from_string(RAYDIUM_CLMM_PROGRAM_ID)
        address, _ = Pubkey.find_program_address(
            [RAYDIUM_POOL_VAULT_SEED, bytes(pool), bytes(mint_key)],
            program,
        )
        return str(address)
    except Exception as exc:
        raise RaydiumClmmVaultError("Raydium vault PDA derivation failed") from exc


def _decode_token_account(data: bytes, *, address: str, pool_address: str, mint: str, lamports: int, data_sha256: str) -> RaydiumClmmVaultAccount:
    if not isinstance(data, bytes) or len(data) != CLASSIC_SPL_TOKEN_ACCOUNT_LENGTH:
        raise RaydiumClmmVaultError("Raydium vault is not an exact 165-byte classic SPL token account")
    if hashlib.sha256(data).hexdigest() != data_sha256:
        raise RaydiumClmmVaultError("Raydium vault data hash differs from the fetched account evidence")
    observed_mint = str(Pubkey.from_bytes(data[0:32]))
    token_authority = str(Pubkey.from_bytes(data[32:64]))
    amount_raw = int.from_bytes(data[64:72], "little")
    delegate_option = int.from_bytes(data[72:76], "little")
    state = data[108]
    native_option = int.from_bytes(data[109:113], "little")
    close_authority_option = int.from_bytes(data[129:133], "little")
    if observed_mint != mint:
        raise RaydiumClmmVaultError("Raydium vault token mint differs from the pool's expected mint")
    if token_authority != pool_address:
        raise RaydiumClmmVaultError("Raydium vault token authority is not the pool PDA")
    if state != TOKEN_ACCOUNT_INITIALIZED:
        raise RaydiumClmmVaultError("Raydium vault token account is not initialized or is frozen")
    if delegate_option not in (0, 1) or native_option not in (0, 1) or close_authority_option not in (0, 1):
        raise RaydiumClmmVaultError("Raydium vault contains an invalid SPL token COption tag")
    if delegate_option != 0:
        raise RaydiumClmmVaultError("Raydium vault has an unexpected delegate authority")
    if native_option != 0:
        raise RaydiumClmmVaultError("wrapped-native vaults are outside the supported classic SPL route policy")
    if close_authority_option != 0:
        raise RaydiumClmmVaultError("Raydium vault has an unexpected close authority")
    if isinstance(lamports, bool) or not isinstance(lamports, int) or lamports <= 0:
        raise RaydiumClmmVaultError("Raydium vault account must have positive lamports")
    return RaydiumClmmVaultAccount(
        address=address,
        pool_address=pool_address,
        mint=mint,
        token_program_id=CLASSIC_SPL_TOKEN_PROGRAM_ID,
        token_authority=token_authority,
        amount_raw=amount_raw,
        state=state,
        lamports=lamports,
        data_sha256=data_sha256,
        account_data=data,
    )


def encode_raydium_clmm_vault_token_account(
    vault: RaydiumClmmVaultAccount,
) -> bytes:
    """Serialize the supported classic SPL vault account without altering authorities."""
    if not isinstance(vault, RaydiumClmmVaultAccount):
        raise RaydiumClmmVaultError("verified Raydium vault account is required")
    if not isinstance(vault.account_data, bytes) or len(vault.account_data) != CLASSIC_SPL_TOKEN_ACCOUNT_LENGTH:
        raise RaydiumClmmVaultError("original verified 165-byte SPL token-account data is required")
    if vault.token_program_id != CLASSIC_SPL_TOKEN_PROGRAM_ID:
        raise RaydiumClmmVaultError("vault token program is not the classic SPL Token program")
    try:
        mint = str(Pubkey.from_bytes(vault.account_data[0:32]))
        authority = str(Pubkey.from_bytes(vault.account_data[32:64]))
        if mint != vault.mint or authority != vault.token_authority:
            raise ValueError("vault account identity differs from its verified model")
    except Exception as exc:
        raise RaydiumClmmVaultError("vault token-account identity bytes are inconsistent") from exc
    if vault.state != TOKEN_ACCOUNT_INITIALIZED or vault.account_data[108] != TOKEN_ACCOUNT_INITIALIZED:
        raise RaydiumClmmVaultError("vault token account must remain initialized")
    if (
        int.from_bytes(vault.account_data[72:76], "little") != 0
        or int.from_bytes(vault.account_data[109:113], "little") != 0
        or int.from_bytes(vault.account_data[129:133], "little") != 0
    ):
        raise RaydiumClmmVaultError("vault delegate, native or close-authority options are unsupported")
    if isinstance(vault.amount_raw, bool) or not isinstance(vault.amount_raw, int) or not 0 <= vault.amount_raw <= (1 << 64) - 1:
        raise RaydiumClmmVaultError("vault amount must fit u64")
    data = bytearray(vault.account_data)
    data[64:72] = vault.amount_raw.to_bytes(8, "little")
    return bytes(data)


def apply_raydium_clmm_vault_balance_delta(
    vault: RaydiumClmmVaultAccount,
    *,
    delta_raw: int,
) -> RaydiumClmmVaultAccount:
    """Apply one checked signed token-balance delta and commit exact account bytes."""
    if isinstance(delta_raw, bool) or not isinstance(delta_raw, int):
        raise RaydiumClmmVaultError("vault balance delta must be a signed integer")
    if not isinstance(vault, RaydiumClmmVaultAccount):
        raise RaydiumClmmVaultError("verified Raydium vault account is required")
    next_amount = vault.amount_raw + delta_raw
    if not 0 <= next_amount <= (1 << 64) - 1:
        raise RaydiumClmmVaultError("vault balance delta underflows or overflows u64")
    updated = replace(vault, amount_raw=next_amount)
    encoded = encode_raydium_clmm_vault_token_account(updated)
    return replace(
        updated,
        account_data=encoded,
        data_sha256=hashlib.sha256(encoded).hexdigest(),
    )



def fetch_raydium_clmm_vault_snapshot(
    pool_evidence: DexPoolEvidenceSnapshot,
    *,
    pool_checks: tuple[RaydiumClmmPoolRouteCheck, ...],
    simulation: SimulationEvidence,
    rpc_adapter: SolanaRpcSimulationAdapter,
) -> RaydiumClmmVaultSnapshot:
    """Fetch both pool vaults and verify their token-account mint/authority/PDA roles."""
    if not isinstance(pool_evidence, DexPoolEvidenceSnapshot):
        raise RaydiumClmmVaultError("provenance-bound DEX pool evidence is required")
    if not isinstance(pool_checks, tuple) or not pool_checks or any(
        not isinstance(item, RaydiumClmmPoolRouteCheck) or item.passed is not True
        for item in pool_checks
    ):
        raise RaydiumClmmVaultError("successful Raydium CLMM pool route checks are required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumClmmVaultError("Solana RPC simulation evidence is required")
    if not isinstance(rpc_adapter, SolanaRpcSimulationAdapter):
        raise RaydiumClmmVaultError("provider-pinned Solana RPC adapter is required")
    if (
        simulation.message_sha256 != pool_evidence.route_message_sha256
        or simulation.response_id != pool_evidence.simulation_response_id
        or simulation.context_slot != pool_evidence.simulation_context_slot
    ):
        raise RaydiumClmmVaultError("pool evidence and simulation response/slot/digest do not match")
    if (
        pool_evidence.provider_id != simulation.provider_id
        or pool_evidence.cluster != simulation.cluster
        or pool_evidence.genesis_hash != simulation.genesis_hash
        or rpc_adapter.provider_id != simulation.provider_id
        or rpc_adapter.cluster != simulation.cluster
        or rpc_adapter.expected_genesis_hash != simulation.genesis_hash
    ):
        raise RaydiumClmmVaultError("vault, pool and simulation RPC provenance must use the same provider and cluster")

    expected: dict[str, tuple[str, str]] = {}
    for check in pool_checks:
        pool = check.pool
        if pool.source_commit == "":
            raise RaydiumClmmVaultError("Raydium pool source layout provenance is missing")
        for address, mint in (
            (pool.token_vault_0, pool.token_mint_0),
            (pool.token_vault_1, pool.token_mint_1),
        ):
            derived = derive_raydium_clmm_vault_address(pool_address=pool.pool_address, mint=mint)
            if derived != address:
                raise RaydiumClmmVaultError("pool vault address does not match the canonical Raydium vault PDA")
            prior = expected.get(address)
            if prior is not None and prior != (pool.pool_address, mint):
                raise RaydiumClmmVaultError("one vault address is assigned to conflicting pool/mint roles")
            expected[address] = (pool.pool_address, mint)
    if not expected:
        raise RaydiumClmmVaultError("Raydium pool checks contain no vault accounts")

    minimum_slot = max(simulation.context_slot, pool_evidence.context_slot)
    snapshot = rpc_adapter.fetch_account_snapshot(tuple(sorted(expected)), min_context_slot=minimum_slot)
    if (
        snapshot.provider_id != simulation.provider_id
        or snapshot.cluster != simulation.cluster
        or snapshot.genesis_hash != simulation.genesis_hash
        or snapshot.context_slot < minimum_slot
    ):
        raise RaydiumClmmVaultError("vault snapshot provider, cluster or context slot differs from the approved evidence")
    if len(snapshot.accounts) != len(expected):
        raise RaydiumClmmVaultError("vault RPC snapshot account count differs from the expected pool vault set")

    vaults = []
    seen: set[str] = set()
    for account in snapshot.accounts:
        if account.address in seen:
            raise RaydiumClmmVaultError("vault snapshot contains duplicate account addresses")
        seen.add(account.address)
        role = expected.get(account.address)
        if role is None:
            raise RaydiumClmmVaultError("vault RPC snapshot returned an unexpected account")
        pool_address, mint = role
        if account.owner != CLASSIC_SPL_TOKEN_PROGRAM_ID:
            raise RaydiumClmmVaultError("Raydium vault is not owned by the classic SPL Token program")
        if account.executable:
            raise RaydiumClmmVaultError("Raydium vault token account must not be executable")
        if not isinstance(account.data, bytes):
            raise RaydiumClmmVaultError("Raydium vault account data is missing")
        vaults.append(_decode_token_account(
            account.data,
            address=account.address,
            pool_address=pool_address,
            mint=mint,
            lamports=account.lamports,
            data_sha256=hashlib.sha256(account.data).hexdigest(),
        ))
    if seen != set(expected):
        raise RaydiumClmmVaultError("vault RPC snapshot omitted one or more pool vaults")

    return RaydiumClmmVaultSnapshot(
        route_message_sha256=simulation.message_sha256,
        simulation_response_id=simulation.response_id,
        simulation_context_slot=simulation.context_slot,
        provider_id=snapshot.provider_id,
        cluster=snapshot.cluster,
        genesis_hash=snapshot.genesis_hash,
        context_slot=snapshot.context_slot,
        vaults=tuple(vaults),
        execution_ready=False,
    )
