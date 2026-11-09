"""Read-only native holder inventory adapters for Solana, Base and BSC.

Solana uses finalized getProgramAccounts snapshots for both SPL token programs.
EVM inventories are reconstructed from the complete ERC-20 Transfer log history
from a caller-supplied deployment block through a finalized block, then checked
against balanceOf and totalSupply at that same block.
"""
import base64
import hashlib
import json
import time
from typing import Any

import requests

from ..developer_concentration import HolderBalance, HolderSnapshot
from .base import ProviderError


SOLANA_TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)
EVM_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
UINT256_MAX = (1 << 256) - 1


def _rpc_result(response, method):
    try:
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ProviderError(f"{method} RPC request failed") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise ProviderError(f"{method} returned an RPC error")
    if "result" not in payload:
        raise ProviderError(f"{method} response omitted result")
    return payload["result"]


def _u256(value):
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ProviderError("EVM quantity is malformed")
    try:
        number = int(value, 16)
    except ValueError as exc:
        raise ProviderError("EVM quantity is malformed") from exc
    if number < 0 or number > UINT256_MAX:
        raise ProviderError("EVM quantity is out of range")
    return number


class SolanaHolderInventoryProvider:
    provider_id = "solana-rpc-holder-inventory"
    supported_chains = frozenset({"solana"})

    def __init__(self, rpc_url, *, session=None, timeout=20.0,
                 clock_ms=None, max_accounts=50_000):
        if not rpc_url:
            raise ValueError("Solana RPC URL is required")
        if not 1 <= max_accounts <= 100_000:
            raise ValueError("max_accounts must be between 1 and 100000")
        self.rpc_url = rpc_url
        self.session = session or requests.Session()
        self.timeout = timeout
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.max_accounts = max_accounts
        self._request_id = 0

    def _rpc(self, method, params):
        self._request_id += 1
        try:
            response = self.session.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": self._request_id,
                      "method": method, "params": params},
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"{method} transport failed") from exc
        return _rpc_result(response, method)

    def fetch_snapshot(self, mint: str) -> HolderSnapshot:
        if not isinstance(mint, str) or not mint:
            raise ValueError("A Solana mint address is required")
        rows = []
        context_slots = []
        for program in SOLANA_TOKEN_PROGRAMS:
            result = self._rpc("getProgramAccounts", [
                program,
                {
                    "commitment": "finalized",
                    "encoding": "jsonParsed",
                    "withContext": True,
                    "filters": [
                        {"memcmp": {"offset": 0, "bytes": mint}},
                        {"dataSize": 165} if program == SOLANA_TOKEN_PROGRAMS[0]
                        else {"memcmp": {"offset": 0, "bytes": mint}},
                    ],
                },
            ])
            if not isinstance(result, dict) or not isinstance(result.get("context"), dict):
                raise ProviderError("Solana holder response omitted snapshot context")
            slot = result["context"].get("slot")
            accounts = result.get("value")
            if not isinstance(slot, int) or isinstance(slot, bool) or slot < 0:
                raise ProviderError("Solana holder context slot is invalid")
            if not isinstance(accounts, list):
                raise ProviderError("Solana holder account list is malformed")
            context_slots.append(slot)
            for item in accounts:
                if not isinstance(item, dict):
                    raise ProviderError("Solana holder account row is malformed")
                pubkey = item.get("pubkey")
                account = item.get("account")
                data = account.get("data") if isinstance(account, dict) else None
                parsed = data.get("parsed") if isinstance(data, dict) else None
                info = parsed.get("info") if isinstance(parsed, dict) else None
                if not isinstance(pubkey, str) or not isinstance(info, dict):
                    raise ProviderError("Solana parsed token account is incomplete")
                if info.get("mint") != mint:
                    raise ProviderError("Solana RPC returned an account for another mint")
                owner = info.get("owner")
                amount = (info.get("tokenAmount") or {}).get("amount")
                if not isinstance(owner, str) or not owner or not isinstance(amount, str) or not amount.isdigit():
                    raise ProviderError("Solana token account owner or raw amount is malformed")
                rows.append((pubkey, owner, int(amount)))
        if len({row[0] for row in rows}) != len(rows):
            raise ProviderError("Solana token account inventory contains duplicates")
        if len(rows) >= self.max_accounts:
            # getProgramAccounts has no standard cursor. A result at the configured
            # ceiling cannot prove the server did not truncate the inventory.
            raise ProviderError("Solana holder result reached the safety cap; coverage is uncertified")
        if len(set(context_slots)) != 1:
            raise ProviderError("Solana token programs were not read at one common context slot")

        supply_result = self._rpc("getTokenSupply", [
            mint, {"commitment": "finalized", "minContextSlot": context_slots[0]}
        ])
        if not isinstance(supply_result, dict) or not isinstance(supply_result.get("value"), dict):
            raise ProviderError("Solana token supply response is malformed")
        supply_value = supply_result["value"]
        raw_supply = supply_value.get("amount")
        decimals = supply_value.get("decimals")
        supply_context = supply_result.get("context") or {}
        supply_slot = supply_context.get("slot")
        if (not isinstance(raw_supply, str) or not raw_supply.isdigit()
                or not isinstance(decimals, int) or isinstance(decimals, bool)
                or not 0 <= decimals <= 38):
            raise ProviderError("Solana token supply fields are malformed")
        if supply_slot != context_slots[0]:
            raise ProviderError("Solana supply and holder accounts are not from the same slot")
        balances = {}
        for _, owner, amount in rows:
            balances[owner] = balances.get(owner, 0) + amount
        holders = tuple(HolderBalance(owner, amount) for owner, amount in sorted(balances.items()))
        total = int(raw_supply)
        complete = sum(x.balance_raw for x in holders) == total
        digest = hashlib.sha256(json.dumps(
            {"mint": mint, "slot": context_slots[0],
             "accounts": sorted((a, b, c) for a, b, c in rows),
             "supply": raw_supply}, separators=(",", ":")
        ).encode()).hexdigest()
        return HolderSnapshot(
            provider=self.provider_id, chain="solana", token_address=mint,
            observed_at_ms=self.clock_ms(), evidence_id=f"solana-holder:{digest}",
            block_ref=f"finalized-slot:{context_slots[0]}",
            decimals=decimals, total_supply_raw=total, holders=holders,
            coverage_complete=complete,
        )


class EvmHolderInventoryProvider:
    """Reconstruct ERC-20 holder balances from Transfer events and verify state."""

    NETWORKS = {"base": "base-mainnet", "bsc": "bnb-mainnet"}

    def __init__(self, chain, rpc_url, *, session=None, timeout=20.0,
                 clock_ms=None, block_chunk_size=2_000, max_logs=200_000,
                 max_holders=50_000):
        chain = str(chain).lower()
        if chain not in self.NETWORKS:
            raise ValueError("EVM holder provider supports Base and BSC only")
        if not rpc_url:
            raise ValueError("EVM RPC URL is required")
        if not 1 <= block_chunk_size <= 10_000 or max_logs < 1 or max_holders < 1:
            raise ValueError("Invalid EVM inventory limits")
        self.chain = chain
        self.rpc_url = rpc_url
        self.session = session or requests.Session()
        self.timeout = timeout
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.block_chunk_size = block_chunk_size
        self.max_logs = max_logs
        self.max_holders = max_holders
        self._request_id = 0

    @property
    def provider_id(self):
        return f"{self.chain}-rpc-holder-inventory"

    def _rpc(self, method, params):
        self._request_id += 1
        try:
            response = self.session.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": self._request_id,
                      "method": method, "params": params},
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"{method} transport failed") from exc
        return _rpc_result(response, method)

    def _call(self, token, signature, address=None, block_tag="finalized"):
        data = signature
        if address is not None:
            data += address.lower().removeprefix("0x").rjust(64, "0")
        result = self._rpc("eth_call", [
            {"to": token, "data": data}, block_tag
        ])
        if not isinstance(result, str) or not result.startswith("0x") or len(result) < 66:
            raise ProviderError("ERC-20 eth_call returned malformed ABI data")
        return _u256(result)

    def fetch_snapshot(self, token: str, deployment_block: int) -> HolderSnapshot:
        if (not isinstance(token, str) or len(token) != 42 or not token.startswith("0x")
                or any(c not in "0123456789abcdefABCDEF" for c in token[2:])):
            raise ValueError("A valid EVM token contract address is required")
        if not isinstance(deployment_block, int) or isinstance(deployment_block, bool) or deployment_block < 0:
            raise ValueError("deployment_block must be a non-negative integer")
        finalized = self._rpc("eth_getBlockByNumber", ["finalized", False])
        if not isinstance(finalized, dict) or not isinstance(finalized.get("number"), str):
            raise ProviderError("RPC does not expose a finalized block number")
        end_block = _u256(finalized["number"])
        if deployment_block > end_block:
            raise ProviderError("deployment block is newer than finalized chain head")

        holders = {EVM_ZERO_ADDRESS}
        logs_count = 0
        for start in range(deployment_block, end_block + 1, self.block_chunk_size):
            end = min(start + self.block_chunk_size - 1, end_block)
            logs = self._rpc("eth_getLogs", [{
                "address": token,
                "fromBlock": hex(start),
                "toBlock": hex(end),
                "topics": [TRANSFER_TOPIC],
            }])
            if not isinstance(logs, list):
                raise ProviderError("eth_getLogs returned malformed transfer history")
            logs_count += len(logs)
            if logs_count > self.max_logs:
                raise ProviderError("ERC-20 transfer history exceeded the configured log cap")
            for log in logs:
                topics = log.get("topics") if isinstance(log, dict) else None
                if (not isinstance(topics, list) or len(topics) != 3
                        or topics[0].lower() != TRANSFER_TOPIC
                        or any(not isinstance(x, str) or len(x) != 66 for x in topics[1:])):
                    raise ProviderError("ERC-20 transfer log is malformed")
                from_address = "0x" + topics[1][-40:].lower()
                to_address = "0x" + topics[2][-40:].lower()
                holders.add(from_address)
                holders.add(to_address)
                if len(holders) > self.max_holders:
                    raise ProviderError("ERC-20 holder inventory exceeded the configured cap")

        holders.discard(EVM_ZERO_ADDRESS)
        decimals = self._call(token, "0x313ce567")
        total_supply = self._call(token, "0x18160ddd", block_tag="finalized")
        if decimals > 38 or total_supply <= 0:
            raise ProviderError("ERC-20 decimals or total supply is invalid")
        # Pin all state reads to the same finalized block used for log coverage.
        block_tag = hex(end_block)
        verified_supply = self._call(token, "0x18160ddd", block_tag=block_tag)
        if verified_supply != total_supply:
            raise ProviderError("Finalized total supply changed during inventory snapshot")
        balances = []
        for holder in sorted(holders):
            amount = self._call(token, "0x70a08231", holder, block_tag)
            if amount:
                balances.append(HolderBalance(holder, amount))
        if sum(x.balance_raw for x in balances) != total_supply:
            raise ProviderError("ERC-20 holder balances do not reconcile to total supply")
        digest = hashlib.sha256(json.dumps(
            {"chain": self.chain, "token": token.lower(), "block": end_block,
             "supply": total_supply, "holders": [(x.wallet_address, x.balance_raw) for x in balances]},
            separators=(",", ":")
        ).encode()).hexdigest()
        return HolderSnapshot(
            provider=self.provider_id, chain=self.chain, token_address=token,
            observed_at_ms=self.clock_ms(), evidence_id=f"evm-holder:{digest}",
            block_ref=f"finalized-block:{end_block}", decimals=decimals,
            total_supply_raw=total_supply, holders=tuple(balances),
            coverage_complete=True,
        )
