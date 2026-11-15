"""Same-provider, same-or-newer-slot Raydium CLMM vault token-account verification."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json

from solders.pubkey import Pubkey

from veltrix.dex_pool_evidence import DexPoolEvidenceSnapshot
from veltrix.raydium_clmm_pool_state import (
    RAYDIUM_CLMM_PROGRAM_ID,
    RaydiumClmmPoolRouteCheck,
)
from veltrix.simulation_gate import SimulationEvidence
from veltrix.solana_rpc_simulation import (
    SolanaRpcSimulationAdapter,
    ConfirmedTransactionEvidence,
    _decode_base58_instruction_data,
)


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


@dataclass(frozen=True, slots=True)
class ExpectedRaydiumVaultDelta:
    vault_address: str
    delta_raw: int

    def __post_init__(self) -> None:
        if not isinstance(self.vault_address, str) or not self.vault_address:
            raise RaydiumClmmVaultError("expected vault delta address is required")
        if isinstance(self.delta_raw, bool) or not isinstance(self.delta_raw, int) or self.delta_raw == 0:
            raise RaydiumClmmVaultError("expected vault delta must be a non-zero signed integer")


@dataclass(frozen=True, slots=True)
class RaydiumFinalizedVaultSettlement:
    signature: str
    finalized_slot: int
    provider_id: str
    cluster: str
    genesis_hash: str
    vault_deltas: tuple[ExpectedRaydiumVaultDelta, ...]
    execution_ready: bool = False


def verify_finalized_raydium_clmm_vault_settlement(
    evidence: ConfirmedTransactionEvidence,
    *,
    simulation: SimulationEvidence,
    vault_snapshot: RaydiumClmmVaultSnapshot,
    expected_deltas: tuple[ExpectedRaydiumVaultDelta, ...],
    provider_id: str,
    cluster: str,
    genesis_hash: str,
) -> RaydiumFinalizedVaultSettlement:
    """Verify finalized Token Program CPI transfers against exact pool-vault balance deltas."""
    if not isinstance(evidence, ConfirmedTransactionEvidence):
        raise RaydiumClmmVaultError("finalized transaction evidence is required")
    if not isinstance(simulation, SimulationEvidence):
        raise RaydiumClmmVaultError("the exact successful simulation evidence is required")
    if evidence.message_sha256 != simulation.message_sha256:
        raise RaydiumClmmVaultError("finalized transaction message does not match the exact simulated message")
    if (
        evidence.provider_id != simulation.provider_id
        or evidence.cluster != simulation.cluster
        or evidence.genesis_hash != simulation.genesis_hash
    ):
        raise RaydiumClmmVaultError("finalized transaction provenance differs from the exact simulation")
    if evidence.inner_instructions_present is not True:
        raise RaydiumClmmVaultError("finalized transaction does not attest to captured inner instructions")
    if not isinstance(vault_snapshot, RaydiumClmmVaultSnapshot):
        raise RaydiumClmmVaultError("provider-backed verified Raydium vault snapshot is required")
    vaults = vault_snapshot.vaults
    if not isinstance(vaults, tuple) or len(vaults) != 2 or any(
        not isinstance(item, RaydiumClmmVaultAccount) for item in vaults
    ):
        raise RaydiumClmmVaultError("exactly two previously verified Raydium pool vaults are required")
    if not isinstance(expected_deltas, tuple) or len(expected_deltas) != 2 or any(
        not isinstance(item, ExpectedRaydiumVaultDelta) for item in expected_deltas
    ):
        raise RaydiumClmmVaultError("exactly two expected signed vault deltas are required")
    if len({item.address for item in vaults}) != 2 or len({item.vault_address for item in expected_deltas}) != 2:
        raise RaydiumClmmVaultError("vault accounts and expected deltas must be unique")
    if {item.address for item in vaults} != {item.vault_address for item in expected_deltas}:
        raise RaydiumClmmVaultError("expected delta addresses do not match the verified pool vault set")
    if any(item.pool_address != vaults[0].pool_address for item in vaults):
        raise RaydiumClmmVaultError("verified vaults do not belong to the same Raydium pool")
    if (
        evidence.provider_id != provider_id
        or evidence.cluster != cluster
        or evidence.genesis_hash != genesis_hash
        or vault_snapshot.provider_id != provider_id
        or vault_snapshot.cluster != cluster
        or vault_snapshot.genesis_hash != genesis_hash
    ):
        raise RaydiumClmmVaultError("finalized transaction and vault snapshot provider, cluster or genesis differ")
    if vault_snapshot.context_slot < evidence.slot:
        raise RaydiumClmmVaultError("vault snapshot context slot is older than the finalized transaction slot")
    if RAYDIUM_CLMM_PROGRAM_ID not in evidence.top_level_program_ids:
        raise RaydiumClmmVaultError("finalized transaction did not invoke the canonical Raydium CLMM program")
    if CLASSIC_SPL_TOKEN_PROGRAM_ID not in evidence.inner_program_ids:
        raise RaydiumClmmVaultError("finalized transaction has no classic SPL Token Program CPI evidence")

    try:
        result = json.loads(evidence.raw_result_json)
        transaction = result["transaction"]
        message = transaction["message"]
        meta = result["meta"]
        inner_groups = meta["innerInstructions"]
        pre_balances = meta["preTokenBalances"]
        post_balances = meta["postTokenBalances"]
        raw_signatures = transaction["signatures"]
        raw_keys = message["accountKeys"]
        raw_keys = [item.get("pubkey") if isinstance(item, dict) else item for item in raw_keys]
        loaded = meta.get("loadedAddresses") or {"writable": [], "readonly": []}
        resolved_keys = tuple(raw_keys + loaded.get("writable", []) + loaded.get("readonly", []))
        if (
            result.get("slot") != evidence.slot
            or not isinstance(raw_signatures, list)
            or not raw_signatures
            or raw_signatures[0] != evidence.signature
            or meta.get("err") is not None
            or resolved_keys != evidence.account_keys
        ):
            raise RaydiumClmmVaultError("finalized transaction raw payload does not match its immutable evidence fields")

        def resolve_program_ids(groups: list) -> tuple[str, ...]:
            resolved = []
            for group in groups:
                instructions = group.get("instructions", []) if "index" in group else [group]
                for instruction in instructions:
                    index = instruction.get("programIdIndex")
                    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(evidence.account_keys):
                        raise RaydiumClmmVaultError("raw instruction program index is outside resolved account keys")
                    resolved.append(evidence.account_keys[index])
            return tuple(resolved)

        if (
            resolve_program_ids(message["instructions"]) != evidence.top_level_program_ids
            or resolve_program_ids(inner_groups) != evidence.inner_program_ids
            or pre_balances != json.loads(evidence.pre_token_balances_json)
            or post_balances != json.loads(evidence.post_token_balances_json)
            or tuple(meta.get("logMessages") or ()) != evidence.logs
        ):
            raise RaydiumClmmVaultError("finalized transaction raw payload differs from its immutable instruction or balance evidence")
    except Exception as exc:
        raise RaydiumClmmVaultError("finalized transaction raw evidence is incomplete or invalid") from exc

    def balance_for(records: list, vault: RaydiumClmmVaultAccount) -> int:
        matches = []
        for record in records:
            if not isinstance(record, dict):
                continue
            index = record.get("accountIndex")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(evidence.account_keys):
                raise RaydiumClmmVaultError("token balance record has an invalid account index")
            if evidence.account_keys[index] == vault.address:
                matches.append(record)
        if len(matches) != 1:
            raise RaydiumClmmVaultError("finalized transaction must contain exactly one token balance record per vault")
        record = matches[0]
        if (
            record.get("mint") != vault.mint
            or record.get("owner") != vault.pool_address
            or record.get("programId") != CLASSIC_SPL_TOKEN_PROGRAM_ID
        ):
            raise RaydiumClmmVaultError("finalized vault token balance mint, owner or token program is inconsistent")
        amount = record.get("uiTokenAmount", {}).get("amount")
        if not isinstance(amount, str) or not amount.isdigit():
            raise RaydiumClmmVaultError("finalized vault token balance amount is not a raw unsigned integer")
        parsed = int(amount)
        if parsed > (1 << 64) - 1:
            raise RaydiumClmmVaultError("finalized vault token balance exceeds u64")
        return parsed

    actual_deltas = {}
    for vault in vaults:
        before = balance_for(pre_balances, vault)
        after = balance_for(post_balances, vault)
        actual_deltas[vault.address] = after - before
    expected = {item.vault_address: item.delta_raw for item in expected_deltas}
    if actual_deltas != expected:
        raise RaydiumClmmVaultError("finalized pre/post vault balances do not equal the exact expected deltas")

    transfer_amounts: dict[str, list[int]] = {item.address: [] for item in vaults}
    vault_mints = {item.address: item.mint for item in vaults}
    for group in inner_groups:
        for instruction in group.get("instructions", []):
            program_index = instruction.get("programIdIndex")
            if isinstance(program_index, bool) or not isinstance(program_index, int) or not 0 <= program_index < len(evidence.account_keys):
                raise RaydiumClmmVaultError("CPI program index is outside resolved transaction account keys")
            if evidence.account_keys[program_index] != CLASSIC_SPL_TOKEN_PROGRAM_ID:
                continue
            data = _decode_base58_instruction_data(instruction.get("data"))
            accounts = instruction.get("accounts")
            if not isinstance(accounts, list):
                raise RaydiumClmmVaultError("Token Program CPI account indexes are malformed")
            if len(data) == 9 and data[0] == 3 and len(accounts) >= 3:
                source_index, destination_index = accounts[0], accounts[1]
                amount = int.from_bytes(data[1:9], "little")
            elif len(data) == 10 and data[0] == 12 and len(accounts) >= 4:
                source_index, destination_index = accounts[0], accounts[2]
                mint_index = accounts[1]
                if isinstance(mint_index, bool) or not isinstance(mint_index, int) or not 0 <= mint_index < len(evidence.account_keys):
                    raise RaydiumClmmVaultError("TransferChecked CPI mint index is invalid")
                amount = int.from_bytes(data[1:9], "little")
            else:
                continue
            for account_index in (source_index, destination_index):
                if isinstance(account_index, bool) or not isinstance(account_index, int) or not 0 <= account_index < len(evidence.account_keys):
                    raise RaydiumClmmVaultError("Token Program CPI account index is invalid")
            source = evidence.account_keys[source_index]
            if len(data) == 10 and data[0] == 12:
                for address in (source, evidence.account_keys[destination_index]):
                    if address in vault_mints and evidence.account_keys[mint_index] != vault_mints[address]:
                        raise RaydiumClmmVaultError("TransferChecked CPI mint differs from the verified pool vault mint")
            destination = evidence.account_keys[destination_index]
            if source in transfer_amounts:
                transfer_amounts[source].append(-amount)
            if destination in transfer_amounts:
                transfer_amounts[destination].append(amount)

    for address, delta in expected.items():
        if transfer_amounts[address] != [delta]:
            raise RaydiumClmmVaultError("Token Program CPI transfer amount/direction does not match the exact vault delta")

    return RaydiumFinalizedVaultSettlement(
        signature=evidence.signature,
        finalized_slot=evidence.slot,
        provider_id=evidence.provider_id,
        cluster=evidence.cluster,
        genesis_hash=evidence.genesis_hash,
        vault_deltas=expected_deltas,
        execution_ready=False,
    )




def verify_finalized_raydium_clmm_vault_settlement_from_rpc(
    signature: str,
    *,
    pool_evidence: DexPoolEvidenceSnapshot,
    pool_checks: tuple[RaydiumClmmPoolRouteCheck, ...],
    simulation: SimulationEvidence,
    rpc_adapter: SolanaRpcSimulationAdapter,
    expected_deltas: tuple[ExpectedRaydiumVaultDelta, ...],
) -> RaydiumFinalizedVaultSettlement:
    """Fetch finalized transaction then refetch pool vaults at/after its slot from the same RPC."""
    if not isinstance(rpc_adapter, SolanaRpcSimulationAdapter):
        raise RaydiumClmmVaultError("provider-pinned Solana RPC adapter is required")
    if (
        rpc_adapter.provider_id != simulation.provider_id
        or rpc_adapter.cluster != simulation.cluster
        or rpc_adapter.expected_genesis_hash != simulation.genesis_hash
    ):
        raise RaydiumClmmVaultError("transaction and vault verification must use the simulation's pinned RPC")
    evidence = rpc_adapter.fetch_finalized_transaction(signature)
    snapshot = fetch_raydium_clmm_vault_snapshot(
        pool_evidence,
        pool_checks=pool_checks,
        simulation=simulation,
        rpc_adapter=rpc_adapter,
        minimum_context_slot=evidence.slot,
    )
    return verify_finalized_raydium_clmm_vault_settlement(
        evidence,
        simulation=simulation,
        vault_snapshot=snapshot,
        expected_deltas=expected_deltas,
        provider_id=rpc_adapter.provider_id,
        cluster=rpc_adapter.cluster,
        genesis_hash=rpc_adapter.expected_genesis_hash,
    )


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
    if hashlib.sha256(vault.account_data).hexdigest() != vault.data_sha256:
        raise RaydiumClmmVaultError("vault account bytes differ from the stored SHA-256 commitment")
    if vault.token_program_id != CLASSIC_SPL_TOKEN_PROGRAM_ID:
        raise RaydiumClmmVaultError("vault token program is not the classic SPL Token program")
    if vault.token_authority != vault.pool_address:
        raise RaydiumClmmVaultError("vault authority differs from its Raydium pool PDA")
    if vault.address != derive_raydium_clmm_vault_address(
        pool_address=vault.pool_address, mint=vault.mint
    ):
        raise RaydiumClmmVaultError("vault address is not the canonical pool/mint PDA")
    try:
        mint = str(Pubkey.from_bytes(vault.account_data[0:32]))
        authority = str(Pubkey.from_bytes(vault.account_data[32:64]))
        if mint != vault.mint or authority != vault.token_authority:
            raise ValueError("vault account identity differs from its verified model")
    except Exception as exc:
        raise RaydiumClmmVaultError("vault token-account identity bytes are inconsistent") from exc
    if vault.state != TOKEN_ACCOUNT_INITIALIZED or vault.account_data[108] != TOKEN_ACCOUNT_INITIALIZED:
        raise RaydiumClmmVaultError("vault token account must remain initialized")
    if int.from_bytes(vault.account_data[64:72], "little") != vault.amount_raw:
        raise RaydiumClmmVaultError("decoded vault amount differs from the original account bytes")
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
    # Validate the original evidence/model binding before applying a signed delta.
    original = encode_raydium_clmm_vault_token_account(vault)
    if original != vault.account_data:
        raise RaydiumClmmVaultError("original vault bytes differ from their canonical serialization")
    next_amount = vault.amount_raw + delta_raw
    if not 0 <= next_amount <= (1 << 64) - 1:
        raise RaydiumClmmVaultError("vault balance delta underflows or overflows u64")
    data = bytearray(original)
    data[64:72] = next_amount.to_bytes(8, "little")
    encoded = bytes(data)
    updated = replace(vault, amount_raw=next_amount)
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
    minimum_context_slot: int | None = None,
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
    if minimum_context_slot is not None and (
        isinstance(minimum_context_slot, bool)
        or not isinstance(minimum_context_slot, int)
        or minimum_context_slot < 0
    ):
        raise RaydiumClmmVaultError("minimum vault context slot must be a non-negative integer")
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

    minimum_slot = max(simulation.context_slot, pool_evidence.context_slot, minimum_context_slot or 0)
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
