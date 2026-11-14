"""Provenance-bearing same-context Solana account snapshots.

Read-only RPC acquisition for transaction preflight. Every requested account is
returned from one getMultipleAccounts context slot; the RPC cluster is checked
against an operator-supplied genesis hash. This is transport/provider provenance,
not a cryptographic proof that an RPC operator is honest.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import math
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import requests
from solders.pubkey import Pubkey


class SolanaAccountSnapshotError(ValueError):
    """Raised when RPC account-state evidence is incomplete or inconsistent."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class SolanaAccountSnapshot:
    address: str
    owner: str
    lamports: int
    executable: bool
    rent_epoch: int
    space: int
    data: bytes
    data_sha256: str


@dataclass(frozen=True, slots=True)
class SolanaAccountStateSnapshot:
    provider_id: str
    endpoint_origin: str
    genesis_hash: str
    commitment: str
    context_slot: int
    min_context_slot: int | None
    observed_at_ms: int
    requested_addresses: tuple[str, ...]
    accounts: tuple[SolanaAccountSnapshot | None, ...]
    account_state_hash: str

    def account(self, address: str) -> SolanaAccountSnapshot | None:
        try:
            canonical = str(Pubkey.from_string(address))
        except Exception as exc:
            raise SolanaAccountSnapshotError("invalid_account_address") from exc
        try:
            index = self.requested_addresses.index(canonical)
        except ValueError:
            raise SolanaAccountSnapshotError("account_not_in_snapshot") from None
        return self.accounts[index]

    def lookup_table_snapshots(self, addresses: tuple[str, ...] | list[str]):
        """Adapt ALT account bytes from this exact context into decoder inputs."""
        from .solana_message_decoder import AddressLookupTableSnapshot

        if not isinstance(addresses, (tuple, list)) or not addresses:
            raise SolanaAccountSnapshotError("lookup_table_address_list_invalid")
        snapshots = {}
        for address in addresses:
            account = self.account(address)
            if account is None:
                raise SolanaAccountSnapshotError("lookup_table_account_missing")
            snapshots[account.address] = AddressLookupTableSnapshot(
                address=account.address,
                owner=account.owner,
                account_data=account.data,
                context_slot=self.context_slot,
            )
        if len(snapshots) != len(addresses):
            raise SolanaAccountSnapshotError("duplicate_lookup_table_address")
        return snapshots


class SolanaAccountSnapshotProvider:
    """Fetch a bounded account set atomically from one RPC context slot."""

    provider_id = "solana-get-multiple-accounts-v1"
    _COMMITMENTS = frozenset({"confirmed", "finalized"})

    def __init__(
        self,
        rpc_url: str,
        *,
        expected_genesis_hash: str,
        commitment: str = "finalized",
        timeout_seconds: float = 10.0,
        session: Any | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(rpc_url, str) or not rpc_url.strip():
            raise SolanaAccountSnapshotError("rpc_url_required")
        parsed = urlsplit(rpc_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise SolanaAccountSnapshotError("rpc_url_must_use_https")
        if parsed.username or parsed.password:
            raise SolanaAccountSnapshotError("rpc_url_credentials_not_allowed")
        try:
            expected_genesis_hash = str(Pubkey.from_string(expected_genesis_hash))
        except Exception:
            # Genesis hashes are base58 32-byte values and solders parses them
            # using the same canonical public-key encoding.
            raise SolanaAccountSnapshotError("expected_genesis_hash_invalid") from None
        if commitment not in self._COMMITMENTS:
            raise SolanaAccountSnapshotError("unsupported_commitment")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 30
        ):
            raise SolanaAccountSnapshotError("timeout_seconds_invalid")
        self.rpc_url = rpc_url
        self.expected_genesis_hash = expected_genesis_hash
        self.commitment = commitment
        self.timeout_seconds = float(timeout_seconds)
        self.session = session or requests.Session()
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.endpoint_origin = f"{parsed.scheme}://{parsed.netloc}"

    def _rpc(self, method: str, params: list[Any]) -> Any:
        try:
            response = self.session.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                raise SolanaAccountSnapshotError("rpc_redirect_not_allowed")
            response.raise_for_status()
            body = response.json()
        except SolanaAccountSnapshotError:
            raise
        except Exception:
            raise SolanaAccountSnapshotError("rpc_transport_or_json_failure") from None
        if not isinstance(body, dict) or body.get("error") is not None or "result" not in body:
            raise SolanaAccountSnapshotError("rpc_response_invalid")
        return body["result"]

    @staticmethod
    def _parse_account(address: str, value: Any) -> SolanaAccountSnapshot:
        if not isinstance(value, dict):
            raise SolanaAccountSnapshotError("account_value_invalid")
        owner_value = value.get("owner")
        try:
            owner = str(Pubkey.from_string(owner_value))
        except Exception:
            raise SolanaAccountSnapshotError("account_owner_invalid") from None
        for field in ("lamports", "rentEpoch", "space"):
            item = value.get(field)
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise SolanaAccountSnapshotError(f"account_{field}_invalid")
        if not isinstance(value.get("executable"), bool):
            raise SolanaAccountSnapshotError("account_executable_invalid")
        data_field = value.get("data")
        if (
            not isinstance(data_field, list)
            or len(data_field) != 2
            or data_field[1] != "base64"
            or not isinstance(data_field[0], str)
        ):
            raise SolanaAccountSnapshotError("account_data_encoding_invalid")
        try:
            data = base64.b64decode(data_field[0], validate=True)
        except Exception:
            raise SolanaAccountSnapshotError("account_data_base64_invalid") from None
        if len(data) != value["space"]:
            raise SolanaAccountSnapshotError("account_space_mismatch")
        return SolanaAccountSnapshot(
            address=address,
            owner=owner,
            lamports=value["lamports"],
            executable=value["executable"],
            rent_epoch=value["rentEpoch"],
            space=value["space"],
            data=data,
            data_sha256=hashlib.sha256(data).hexdigest(),
        )

    def fetch_snapshot(
        self,
        addresses: tuple[str, ...] | list[str],
        *,
        min_context_slot: int | None = None,
        require_all_accounts: bool = True,
    ) -> SolanaAccountStateSnapshot:
        if not isinstance(addresses, (tuple, list)) or not 1 <= len(addresses) <= 100:
            raise SolanaAccountSnapshotError("account_count_out_of_range")
        if not isinstance(require_all_accounts, bool):
            raise SolanaAccountSnapshotError("require_all_accounts_invalid")
        if min_context_slot is not None and (
            isinstance(min_context_slot, bool)
            or not isinstance(min_context_slot, int)
            or min_context_slot < 0
        ):
            raise SolanaAccountSnapshotError("min_context_slot_invalid")
        canonical_addresses = []
        for address in addresses:
            try:
                canonical_addresses.append(str(Pubkey.from_string(address)))
            except Exception:
                raise SolanaAccountSnapshotError("invalid_account_address") from None
        if len(set(canonical_addresses)) != len(canonical_addresses):
            raise SolanaAccountSnapshotError("duplicate_account_address")

        genesis = self._rpc("getGenesisHash", [])
        if genesis != self.expected_genesis_hash:
            raise SolanaAccountSnapshotError("rpc_genesis_hash_mismatch")

        config: dict[str, Any] = {
            "encoding": "base64",
            "commitment": self.commitment,
        }
        if min_context_slot is not None:
            config["minContextSlot"] = min_context_slot
        result = self._rpc("getMultipleAccounts", [canonical_addresses, config])
        if not isinstance(result, dict) or not isinstance(result.get("context"), dict):
            raise SolanaAccountSnapshotError("multiple_accounts_context_missing")
        slot = result["context"].get("slot")
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 1:
            raise SolanaAccountSnapshotError("multiple_accounts_slot_invalid")
        if min_context_slot is not None and slot < min_context_slot:
            raise SolanaAccountSnapshotError("multiple_accounts_slot_below_minimum")
        values = result.get("value")
        if not isinstance(values, list) or len(values) != len(canonical_addresses):
            raise SolanaAccountSnapshotError("multiple_accounts_result_count_mismatch")

        parsed_accounts: list[SolanaAccountSnapshot | None] = []
        for address, value in zip(canonical_addresses, values):
            if value is None:
                if require_all_accounts:
                    raise SolanaAccountSnapshotError("required_account_missing")
                parsed_accounts.append(None)
            else:
                parsed_accounts.append(self._parse_account(address, value))

        observed_at = self.clock_ms()
        if isinstance(observed_at, bool) or not isinstance(observed_at, int) or observed_at <= 0:
            raise SolanaAccountSnapshotError("snapshot_clock_invalid")
        hash_payload = {
            "provider_id": self.provider_id,
            "endpoint_origin": self.endpoint_origin,
            "genesis_hash": genesis,
            "commitment": self.commitment,
            "context_slot": slot,
            "min_context_slot": min_context_slot,
            "requested_addresses": canonical_addresses,
            "accounts": [
                None if account is None else {
                    "address": account.address,
                    "owner": account.owner,
                    "lamports": account.lamports,
                    "executable": account.executable,
                    "rent_epoch": account.rent_epoch,
                    "space": account.space,
                    "data_sha256": account.data_sha256,
                }
                for account in parsed_accounts
            ],
        }
        canonical = json.dumps(
            hash_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        state_hash = hashlib.sha256(canonical).hexdigest()
        return SolanaAccountStateSnapshot(
            provider_id=self.provider_id,
            endpoint_origin=self.endpoint_origin,
            genesis_hash=genesis,
            commitment=self.commitment,
            context_slot=slot,
            min_context_slot=min_context_slot,
            observed_at_ms=observed_at,
            requested_addresses=tuple(canonical_addresses),
            accounts=tuple(parsed_accounts),
            account_state_hash=state_hash,
        )
