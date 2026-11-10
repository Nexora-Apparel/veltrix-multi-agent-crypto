"""Provider-constrained JSON-RPC adapter for exact Solana simulation evidence.

The adapter only calls read-only RPC methods. It creates an unsigned wire
transaction with placeholder signatures and uses simulateTransaction with
sigVerify=false and replaceRecentBlockhash=false. It never broadcasts.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import time
from typing import Any
from urllib.parse import urlsplit

import requests
from solders.message import MessageV0, from_bytes_versioned, to_bytes_versioned
from solders.address_lookup_table_account import AddressLookupTable, ID as ADDRESS_LOOKUP_TABLE_ID
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from veltrix.simulation_gate import (
    ExpectedTokenDelta,
    SimulatedAccountSnapshot,
    SimulatedInnerInstruction,
    SimulatedTokenBalance,
    SimulationEvidence,
    SimulationGateError,
)
from veltrix.transaction_validator import (
    ResolvedLookupTable,
    TransactionIntent,
    TransactionValidationPolicy,
    TransactionValidationResult,
    validate_transaction_message,
)


TOKEN_PROGRAM_IDS = frozenset({
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
})
TOKEN_ACCOUNT_BASE_SIZE = 165
MAX_TRACKED_TOKEN_ACCOUNTS = 100


class SolanaRpcAdapterError(RuntimeError):
    pass


_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {character: index for index, character in enumerate(_BASE58_ALPHABET)}


def _decode_base58(value: str) -> bytes:
    if not isinstance(value, str):
        raise SolanaRpcAdapterError("inner instruction data must be a base58 string")
    if value == "":
        return b""
    number = 0
    for character in value:
        digit = _BASE58_INDEX.get(character)
        if digit is None:
            raise SolanaRpcAdapterError("inner instruction data contains an invalid base58 character")
        number = number * 58 + digit
    payload = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return bytes(leading_zeroes) + payload
    """Raised when the configured RPC provider cannot supply complete evidence."""


@dataclass(frozen=True, slots=True)
class RpcSimulationResult:
    validation: TransactionValidationResult
    evidence: SimulationEvidence


class SolanaRpcSimulationAdapter:
    """Read-only RPC adapter with exact-host pinning and strict response parsing."""

    def __init__(
        self,
        *,
        endpoint: str,
        provider_id: str,
        cluster: str,
        expected_genesis_hash: str,
        allowed_hosts: tuple[str, ...],
        timeout_seconds: float = 8.0,
        session: Any | None = None,
        clock_ms: Any | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.query
            or parsed.hostname.lower() not in {h.lower() for h in allowed_hosts}
        ):
            raise SolanaRpcAdapterError("RPC endpoint must be HTTPS on an explicitly allowlisted host")
        if not isinstance(provider_id, str) or not provider_id or provider_id.strip() != provider_id:
            raise SolanaRpcAdapterError("provider_id must be a canonical non-empty string")
        if not isinstance(cluster, str) or not cluster or cluster.strip() != cluster:
            raise SolanaRpcAdapterError("cluster must be a canonical non-empty string")
        if not isinstance(expected_genesis_hash, str) or not expected_genesis_hash:
            raise SolanaRpcAdapterError("expected genesis hash is required")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 30:
            raise SolanaRpcAdapterError("timeout_seconds must be greater than zero and at most 30")
        self._endpoint = endpoint
        self.provider_id = provider_id
        self.cluster = cluster
        self.expected_genesis_hash = expected_genesis_hash
        self.timeout_seconds = float(timeout_seconds)
        self._session = session or requests.Session()
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._request_id = 0

    def _rpc(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        request_id = self._request_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            response = self._session.post(
                self._endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise SolanaRpcAdapterError(f"RPC {method} returned HTTP {response.status_code}")
            raw = response.json()
        except SolanaRpcAdapterError:
            raise
        except Exception as exc:
            raise SolanaRpcAdapterError(f"RPC {method} transport or JSON decoding failed") from exc
        if not isinstance(raw, dict) or raw.get("jsonrpc") != "2.0" or raw.get("id") != request_id:
            raise SolanaRpcAdapterError(f"RPC {method} returned an invalid JSON-RPC envelope")
        if raw.get("error") is not None:
            raise SolanaRpcAdapterError(f"RPC {method} returned a JSON-RPC error")
        if "result" not in raw:
            raise SolanaRpcAdapterError(f"RPC {method} omitted its result")
        return raw["result"]

    @staticmethod
    def _context_slot(result: Any, method: str) -> int:
        if not isinstance(result, dict) or not isinstance(result.get("context"), dict):
            raise SolanaRpcAdapterError(f"RPC {method} omitted context")
        slot = result["context"].get("slot")
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
            raise SolanaRpcAdapterError(f"RPC {method} returned an invalid context slot")
        return slot

    def _resolve_lookup_tables(
        self,
        message: object,
        *,
        minimum_context_slot: int,
    ) -> tuple[tuple[ResolvedLookupTable, ...], int | None]:
        if not isinstance(message, MessageV0) or not message.address_table_lookups:
            return (), None
        table_addresses = [str(item.account_key) for item in message.address_table_lookups]
        if len(table_addresses) != len(set(table_addresses)):
            raise SolanaRpcAdapterError("message contains duplicate address lookup table references")
        result = self._rpc(
            "getMultipleAccounts",
            [table_addresses, {
                "encoding": "base64",
                "commitment": "confirmed",
                "minContextSlot": minimum_context_slot,
            }],
        )
        context_slot = self._context_slot(result, "getMultipleAccounts(lookup tables)")
        if context_slot < minimum_context_slot:
            raise SolanaRpcAdapterError("lookup table response did not reach the required minimum context slot")
        values = result.get("value")
        if not isinstance(values, list) or len(values) != len(table_addresses):
            raise SolanaRpcAdapterError("lookup table RPC response is incomplete")
        resolved: list[ResolvedLookupTable] = []
        for address, info in zip(table_addresses, values):
            if not isinstance(info, dict):
                raise SolanaRpcAdapterError("address lookup table account is missing")
            if info.get("owner") != str(ADDRESS_LOOKUP_TABLE_ID):
                raise SolanaRpcAdapterError("lookup table account owner is not the canonical address lookup table program")
            data = info.get("data")
            if not isinstance(data, list) or len(data) != 2 or data[1] != "base64" or not isinstance(data[0], str):
                raise SolanaRpcAdapterError("lookup table account has unsupported data encoding")
            try:
                raw = base64.b64decode(data[0], validate=True)
                table = AddressLookupTable.deserialize(raw)
                meta = table.meta
                addresses = tuple(str(key) for key in table.addresses)
                resolved.append(ResolvedLookupTable(
                    table_address=address,
                    owner_program_id=str(ADDRESS_LOOKUP_TABLE_ID),
                    addresses=addresses,
                    deactivation_slot=int(meta.deactivation_slot),
                    last_extended_slot=int(meta.last_extended_slot),
                    context_slot=context_slot,
                ))
            except Exception as exc:
                raise SolanaRpcAdapterError("address lookup table account data could not be safely decoded") from exc
        return tuple(resolved), context_slot

    @staticmethod
    def _token_balance(account_info: Any, address: str) -> tuple[str, str, str, int]:
        if not isinstance(account_info, dict):
            raise SolanaRpcAdapterError(f"token account {address} is missing")
        program_owner = account_info.get("owner")
        if program_owner not in TOKEN_PROGRAM_IDS:
            raise SolanaRpcAdapterError(f"token account {address} is not owned by a supported SPL token program")
        data_field = account_info.get("data")
        if (
            not isinstance(data_field, list)
            or len(data_field) != 2
            or data_field[1] != "base64"
            or not isinstance(data_field[0], str)
        ):
            raise SolanaRpcAdapterError(f"token account {address} has an unsupported data encoding")
        try:
            raw = base64.b64decode(data_field[0], validate=True)
        except Exception as exc:
            raise SolanaRpcAdapterError(f"token account {address} has malformed base64 data") from exc
        if len(raw) < TOKEN_ACCOUNT_BASE_SIZE:
            raise SolanaRpcAdapterError(f"token account {address} data is shorter than the SPL base layout")
        from solders.pubkey import Pubkey
        mint = str(Pubkey.from_bytes(raw[0:32]))
        owner = str(Pubkey.from_bytes(raw[32:64]))
        amount = int.from_bytes(raw[64:72], "little", signed=False)
        return program_owner, mint, owner, amount

    @staticmethod
    def _account_snapshot(info: object, address: str, context_slot: int) -> SimulatedAccountSnapshot:
        if not isinstance(info, dict):
            raise SolanaRpcAdapterError("requested account snapshot is missing")
        owner = info.get("owner")
        lamports = info.get("lamports")
        executable = info.get("executable")
        rent_epoch = info.get("rentEpoch")
        raw_data = info.get("data")
        if not isinstance(owner, str) or not isinstance(executable, bool):
            raise SolanaRpcAdapterError("account snapshot owner or executable flag is malformed")
        if isinstance(lamports, bool) or not isinstance(lamports, int) or lamports < 0:
            raise SolanaRpcAdapterError("account snapshot lamports are malformed")
        if isinstance(rent_epoch, bool) or not isinstance(rent_epoch, int) or rent_epoch < 0:
            raise SolanaRpcAdapterError("account snapshot rentEpoch is malformed")
        if not isinstance(raw_data, list) or len(raw_data) != 2 or raw_data[1] != "base64" or not isinstance(raw_data[0], str):
            raise SolanaRpcAdapterError("account snapshot data is not base64 encoded")
        try:
            data = base64.b64decode(raw_data[0], validate=True)
        except Exception as exc:
            raise SolanaRpcAdapterError("account snapshot data contains invalid base64") from exc
        return SimulatedAccountSnapshot(
            address=address,
            owner=owner,
            lamports=lamports,
            executable=executable,
            rent_epoch=rent_epoch,
            data=data,
            context_slot=context_slot,
        )

    def simulate(
        self,
        message_bytes: bytes,
        *,
        intent: TransactionIntent,
        transaction_policy: TransactionValidationPolicy,
        expected_token_deltas: tuple[ExpectedTokenDelta, ...],
        minimum_context_slot: int,
        last_valid_block_height: int,
        additional_account_snapshots: tuple[str, ...] = (),
    ) -> RpcSimulationResult:
        """Validate and simulate the exact message; return normalized evidence.

        Pre-state and simulated post-state account snapshots must come from the
        same confirmed context slot. This intentionally rejects non-atomic
        snapshots rather than comparing balances from different banks.
        """
        if not isinstance(expected_token_deltas, tuple) or not expected_token_deltas:
            raise SolanaRpcAdapterError("explicit expected token-account deltas are required")
        if len(expected_token_deltas) > MAX_TRACKED_TOKEN_ACCOUNTS:
            raise SolanaRpcAdapterError("too many token accounts requested for one simulation")
        if any(not isinstance(x, ExpectedTokenDelta) for x in expected_token_deltas):
            raise SolanaRpcAdapterError("expected token-account deltas contain invalid records")
        for name, value in (
            ("minimum_context_slot", minimum_context_slot),
            ("last_valid_block_height", last_valid_block_height),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SolanaRpcAdapterError(f"{name} must be a non-negative integer")

        token_addresses = [item.token_account for item in expected_token_deltas]
        if len(token_addresses) != len(set(token_addresses)):
            raise SolanaRpcAdapterError("expected token-account addresses must be unique")
        if not isinstance(additional_account_snapshots, tuple) or len(additional_account_snapshots) > MAX_ADDITIONAL_ACCOUNT_SNAPSHOTS:
            raise SolanaRpcAdapterError("additional account snapshots must be a bounded immutable tuple")
        if any(not isinstance(address, str) for address in additional_account_snapshots):
            raise SolanaRpcAdapterError("additional account snapshot addresses must be strings")
        snapshot_addresses = list(additional_account_snapshots)
        if len(snapshot_addresses) != len(set(snapshot_addresses)) or set(snapshot_addresses) & set(token_addresses):
            raise SolanaRpcAdapterError("additional account snapshot addresses must be unique and separate from token accounts")
        addresses = token_addresses + snapshot_addresses

        from solders.pubkey import Pubkey
        for address in addresses:
            try:
                Pubkey.from_string(address)
            except Exception as exc:
                raise SolanaRpcAdapterError("requested account address is not a valid Solana public key") from exc

        genesis = self._rpc("getGenesisHash", [])
        if genesis != self.expected_genesis_hash:
            raise SolanaRpcAdapterError("RPC endpoint genesis hash does not match configured cluster")

        blockhash_valid = self._rpc(
            "isBlockhashValid",
            [intent.recent_blockhash, {"commitment": "confirmed"}],
        )
        if not isinstance(blockhash_valid, dict):
            raise SolanaRpcAdapterError("isBlockhashValid response is malformed")
        if blockhash_valid.get("value") is not True:
            raise SolanaRpcAdapterError("approved recent blockhash is not valid on the configured cluster")
        blockhash_slot = self._context_slot(blockhash_valid, "isBlockhashValid")

        block_height = self._rpc("getBlockHeight", [{"commitment": "confirmed"}])
        if isinstance(block_height, bool) or not isinstance(block_height, int) or block_height < 0:
            raise SolanaRpcAdapterError("getBlockHeight returned an invalid block height")
        if block_height > last_valid_block_height:
            raise SolanaRpcAdapterError("approved recent blockhash has expired")

        try:
            message = from_bytes_versioned(message_bytes)
            if isinstance(message, MessageV0):
                message.sanitize()
        except Exception as exc:
            raise SolanaRpcAdapterError("transaction message could not be decoded before lookup resolution") from exc
        resolved_lookup_tables, lookup_context_slot = self._resolve_lookup_tables(
            message, minimum_context_slot=minimum_context_slot
        )
        validation = validate_transaction_message(
            message_bytes,
            intent=intent,
            policy=transaction_policy,
            resolved_lookup_tables=resolved_lookup_tables,
            current_slot=lookup_context_slot,
            minimum_context_slot=minimum_context_slot,
        )

        pre_result = self._rpc(
            "getMultipleAccounts",
            [addresses, {
                "encoding": "base64",
                "commitment": "confirmed",
                "minContextSlot": max(minimum_context_slot, blockhash_slot, lookup_context_slot or 0),
            }],
        )
        pre_slot = self._context_slot(pre_result, "getMultipleAccounts")
        if pre_slot < max(minimum_context_slot, blockhash_slot, lookup_context_slot or 0):
            raise SolanaRpcAdapterError("pre-state response did not reach the required minimum context slot")
        if lookup_context_slot is not None and pre_slot != lookup_context_slot:
            raise SolanaRpcAdapterError("lookup table and token-account snapshots were not observed at the same context slot")
        pre_accounts = pre_result.get("value")
        if not isinstance(pre_accounts, list) or len(pre_accounts) != len(addresses):
            raise SolanaRpcAdapterError("getMultipleAccounts returned an incomplete account list")
        token_count = len(token_addresses)
        pre_balances = {
            address: self._token_balance(info, address)
            for address, info in zip(token_addresses, pre_accounts[:token_count])
        }
        pre_snapshots = tuple(
            self._account_snapshot(info, address, pre_slot)
            for address, info in zip(snapshot_addresses, pre_accounts[token_count:])
        )

        try:
            message = from_bytes_versioned(message_bytes)
            if isinstance(message, MessageV0):
                message.sanitize()
            required_signatures = int(message.header.num_required_signatures)
            if required_signatures < 1:
                raise SolanaRpcAdapterError("transaction message has no required fee-payer signature")
            unsigned = VersionedTransaction.populate(
                message,
                [Signature.default() for _ in range(required_signatures)],
            )
            wire_b64 = base64.b64encode(bytes(unsigned)).decode("ascii")
            message_b64 = base64.b64encode(to_bytes_versioned(message)).decode("ascii")
        except SolanaRpcAdapterError:
            raise
        except Exception as exc:
            raise SolanaRpcAdapterError("could not construct unsigned simulation payload") from exc

        simulation = self._rpc(
            "simulateTransaction",
            [wire_b64, {
                "encoding": "base64",
                "commitment": "confirmed",
                "sigVerify": False,
                "replaceRecentBlockhash": False,
                "minContextSlot": pre_slot,
                "accounts": {"encoding": "base64", "addresses": addresses},
                "innerInstructions": True,
            }],
        )
        simulation_slot = self._context_slot(simulation, "simulateTransaction")
        if simulation_slot != pre_slot:
            raise SolanaRpcAdapterError("pre-state and simulation post-state were not observed at the same context slot")
        value = simulation.get("value")
        if not isinstance(value, dict):
            raise SolanaRpcAdapterError("simulateTransaction omitted its value object")
        inner_groups = value.get("innerInstructions") or []
        if not isinstance(inner_groups, list):
            raise SolanaRpcAdapterError("simulateTransaction returned malformed inner-instruction trace")
        inner_instructions: list[SimulatedInnerInstruction] = []
        for group in inner_groups:
            if not isinstance(group, dict) or not isinstance(group.get("instructions"), list):
                raise SolanaRpcAdapterError("simulateTransaction returned malformed inner-instruction group")
            group_index = group.get("index")
            if isinstance(group_index, bool) or not isinstance(group_index, int) or not 0 <= group_index < validation.instruction_count:
                raise SolanaRpcAdapterError("inner instruction group index is outside the top-level message")
            for inner in group["instructions"]:
                if not isinstance(inner, dict):
                    raise SolanaRpcAdapterError("simulateTransaction returned a malformed inner instruction")
                program_index = inner.get("programIdIndex")
                if isinstance(program_index, bool) or not isinstance(program_index, int) or not 0 <= program_index < len(validation.account_keys):
                    raise SolanaRpcAdapterError("inner instruction program index is outside the resolved account list")
                raw_account_indexes = inner.get("accounts")
                if not isinstance(raw_account_indexes, list):
                    raise SolanaRpcAdapterError("inner instruction account indexes are missing or malformed")
                account_keys = []
                for account_index in raw_account_indexes:
                    if isinstance(account_index, bool) or not isinstance(account_index, int) or not 0 <= account_index < len(validation.account_keys):
                        raise SolanaRpcAdapterError("inner instruction account index is outside the resolved account list")
                    account_keys.append(validation.account_keys[account_index])
                stack_height = inner.get("stackHeight")
                if stack_height is not None and (isinstance(stack_height, bool) or not isinstance(stack_height, int) or stack_height < 1):
                    raise SolanaRpcAdapterError("inner instruction stackHeight is invalid")
                inner_instructions.append(SimulatedInnerInstruction(
                    top_level_instruction_index=group_index,
                    program_id=validation.account_keys[program_index],
                    account_keys=tuple(account_keys),
                    data=_decode_base58(inner.get("data")),
                    stack_height=stack_height,
                ))
        inner_program_ids = tuple(item.program_id for item in inner_instructions)
        post_accounts = value.get("accounts")
        if not isinstance(post_accounts, list) or len(post_accounts) != len(addresses):
            raise SolanaRpcAdapterError("simulateTransaction omitted requested post-state token accounts")

        token_balances = []
        for address, post_info in zip(token_addresses, post_accounts[:token_count]):
            pre_program, pre_mint, pre_owner, pre_amount = pre_balances[address]
            post_program, post_mint, post_owner, post_amount = self._token_balance(post_info, address)
            if (pre_program, pre_mint, pre_owner) != (post_program, post_mint, post_owner):
                raise SolanaRpcAdapterError("simulated token-account mint or authority changed")
            token_balances.append(SimulatedTokenBalance(
                token_account=address,
                mint=pre_mint,
                owner=pre_owner,
                pre_amount_raw=pre_amount,
                post_amount_raw=post_amount,
                token_program_id=pre_program,
            ))
        post_snapshots = tuple(
            self._account_snapshot(info, address, simulation_slot)
            for address, info in zip(snapshot_addresses, post_accounts[token_count:])
        )
        if pre_snapshots != post_snapshots:
            raise SolanaRpcAdapterError("account snapshot changed between pre-state and simulated post-state")

        fee_result = self._rpc(
            "getFeeForMessage",
            [message_b64, {"commitment": "confirmed", "minContextSlot": simulation_slot}],
        )
        fee_slot = self._context_slot(fee_result, "getFeeForMessage")
        if fee_slot != simulation_slot:
            raise SolanaRpcAdapterError("fee quote and simulation were evaluated at different context slots")
        fee_lamports = fee_result.get("value")
        if isinstance(fee_lamports, bool) or not isinstance(fee_lamports, int) or fee_lamports < 0:
            raise SolanaRpcAdapterError("getFeeForMessage returned a missing or invalid fee")

        units = value.get("unitsConsumed")
        if isinstance(units, bool) or not isinstance(units, int) or units < 0:
            raise SolanaRpcAdapterError("simulateTransaction omitted valid unitsConsumed")
        error = value.get("err")
        if error is not None and not isinstance(error, str):
            error = str(error)

        evidence = SimulationEvidence(
            message_sha256=validation.message_sha256,
            recent_blockhash=intent.recent_blockhash,
            cluster=self.cluster,
            genesis_hash=genesis,
            provider_id=self.provider_id,
            response_id=f"simulate-slot-{simulation_slot}-request-{self._request_id}",
            observed_at_epoch_ms=int(self._clock_ms()),
            context_slot=simulation_slot,
            current_block_height=block_height,
            last_valid_block_height=last_valid_block_height,
            simulation_error=error,
            units_consumed=units,
            fee_lamports=fee_lamports,
            token_balances=tuple(token_balances),
            top_level_program_ids=validation.program_ids,
            inner_program_ids=inner_program_ids,
            inner_instruction_count=len(inner_program_ids),
            inner_instructions=tuple(inner_instructions),
            account_snapshots=post_snapshots,
        )
        return RpcSimulationResult(validation=validation, evidence=evidence)
