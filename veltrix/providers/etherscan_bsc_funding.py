"""Etherscan V2 BSC first-funder graph from external, internal and ERC-20 history.

Every history category is paginated in ascending order to a finalized BSC
block. The explorer's 10,000-record per-category ceiling is treated as an
incompleteness condition, never as complete coverage.
"""
import hashlib
import json
import time

import requests

from ..developer_concentration import FundingGraphSnapshot, FundingRelationship
from .base import ProviderError


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ACTIONS = ("txlist", "txlistinternal", "tokentx")


def _number(value, label):
    if isinstance(value, bool):
        raise ProviderError(f"BSC {label} is malformed")
    try:
        return int(str(value), 10)
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"BSC {label} is malformed") from exc


def _sub_order(action, row):
    if action == "txlistinternal":
        trace = row.get("traceId", "0")
        if not isinstance(trace, str):
            raise ProviderError("BSC internal trace ID is malformed")
        try:
            return tuple(int(part or "0") for part in trace.split("_"))
        except ValueError as exc:
            raise ProviderError("BSC internal trace ID is malformed") from exc
    field = "logIndex" if action == "tokentx" else "transactionIndex"
    return (_number(row.get(field, "0"), field),)


class EtherscanBscFundingGraphProvider:
    provider_id = "etherscan-v2-bsc-funding-graph"
    chain = "bsc"
    CHAIN_ID = "56"
    MAX_RECORDS_PER_ACTION = 10_000

    def __init__(self, api_key, rpc_url, *, session=None, timeout=20.0,
                 clock_ms=None, max_pages=100, max_wallets=2_000,
                 max_total_pages=1_000, page_size=1_000,
                 api_url="https://api.etherscan.io/v2/api"):
        if not api_key or not rpc_url:
            raise ValueError("Etherscan API key and BSC RPC URL are required")
        if not 1 <= max_pages <= 1_000 or not 1 <= max_wallets <= 10_000:
            raise ValueError("Invalid BSC funding graph limits")
        if not 1 <= page_size <= 1_000 or max_total_pages < 1:
            raise ValueError("Invalid Etherscan page or total-page bound")
        self.api_key = api_key
        self.rpc_url = rpc_url
        self.session = session or requests.Session()
        self.timeout = timeout
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.max_pages = max_pages
        self.max_wallets = max_wallets
        self.max_total_pages = max_total_pages
        self.page_size = page_size
        self.api_url = api_url
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
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"BSC {method} request failed") from exc
        if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
            raise ProviderError(f"BSC {method} returned an RPC error")
        return payload["result"]

    def _api_page(self, wallet, action, end_block, page):
        params = {
            "chainid": self.CHAIN_ID,
            "module": "account",
            "action": action,
            "address": wallet,
            "startblock": "0",
            "endblock": str(end_block),
            "page": str(page),
            "offset": str(self.page_size),
            "sort": "asc",
            "apikey": self.api_key,
        }
        try:
            response = self.session.get(self.api_url, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"Etherscan {action} history request failed") from exc
        if not isinstance(payload, dict):
            raise ProviderError(f"Etherscan {action} response is malformed")
        rows = payload.get("result")
        if str(payload.get("status")) == "0":
            if isinstance(rows, str) and "no transactions found" in rows.lower():
                return []
            if rows == [] and "no transactions found" in str(payload.get("message", "")).lower():
                return []
            raise ProviderError(f"Etherscan {action} history is unavailable or incomplete")
        if str(payload.get("status")) != "1" or not isinstance(rows, list):
            raise ProviderError(f"Etherscan {action} history response is malformed")
        if len(rows) > self.page_size:
            raise ProviderError(f"Etherscan {action} exceeded the requested page size")
        return rows

    def _history(self, wallet, end_block, page_budget):
        rows_by_action = {}
        total_pages = 0
        for action in ACTIONS:
            action_rows = []
            page = 1
            while page <= self.max_pages and total_pages < page_budget:
                rows = self._api_page(wallet, action, end_block, page)
                total_pages += 1
                action_rows.extend(rows)
                if len(action_rows) >= self.MAX_RECORDS_PER_ACTION:
                    raise ProviderError(
                        f"BSC {action} history reached the explorer's 10,000-record ceiling"
                    )
                if len(rows) < self.page_size:
                    break
                page += 1
            else:
                raise ProviderError("BSC funding graph exceeded page budget")
            rows_by_action[action] = action_rows
        candidates = []
        for action, rows in rows_by_action.items():
            for row in rows:
                if not isinstance(row, dict):
                    raise ProviderError(f"BSC {action} contains a malformed row")
                recipient = row.get("to")
                if not isinstance(recipient, str) or recipient.lower() != wallet.lower():
                    continue
                sender = row.get("from")
                if not isinstance(sender, str) or not sender:
                    raise ProviderError(f"BSC {action} inbound transfer has no sender")
                if sender.lower() == ZERO_ADDRESS:
                    continue
                if action in {"txlist", "txlistinternal"} and str(row.get("isError", "")) != "0":
                    continue
                if action == "txlist" and str(row.get("txreceipt_status", "1")) != "1":
                    continue
                value = _number(row.get("value"), "transfer value")
                if value <= 0:
                    continue
                block = _number(row.get("blockNumber"), "transfer block")
                stamp = _number(row.get("timeStamp"), "transfer timestamp")
                tx_hash = row.get("hash")
                if block < 0 or block > end_block or stamp < 0 or not isinstance(tx_hash, str) or not tx_hash:
                    raise ProviderError(f"BSC {action} transfer provenance is invalid")
                tx_index = _number(row.get("transactionIndex", "0"), "transaction index")
                sub_index = _sub_order(action, row)
                priority = ACTIONS.index(action)
                candidates.append((
                    (block, tx_index, sub_index, priority), sender, tx_hash, stamp, action,
                ))
        return candidates, total_pages

    def fetch_snapshot(self, token_address, holder_wallets):
        if not isinstance(token_address, str) or not token_address:
            raise ValueError("Token address is required")
        initial = tuple(dict.fromkeys(
            x.lower() for x in holder_wallets
            if isinstance(x, str) and x
        ))
        if not initial:
            raise ValueError("At least one holder wallet is required")
        if len(initial) > self.max_wallets:
            raise ProviderError("Holder set exceeds BSC funding graph wallet cap")
        finalized = self._rpc("eth_getBlockByNumber", ["finalized", False])
        if not isinstance(finalized, dict) or not isinstance(finalized.get("number"), str):
            raise ProviderError("BSC RPC did not return a finalized block")
        try:
            end_block = int(finalized["number"], 16)
        except ValueError as exc:
            raise ProviderError("BSC finalized block number is malformed") from exc
        if end_block < 0:
            raise ProviderError("BSC finalized block number is invalid")
        now = self.clock_ms()
        queue = list(initial)
        queried = set()
        relationships = []
        total_pages = 0
        while queue:
            wallet = queue.pop(0)
            key = wallet.lower()
            if key in queried:
                continue
            if len(queried) >= self.max_wallets:
                raise ProviderError("BSC funding ancestry exceeded wallet cap")
            queried.add(key)
            remaining = self.max_total_pages - total_pages
            if remaining <= 0:
                raise ProviderError("BSC funding graph exceeded total page budget")
            candidates, pages = self._history(wallet, end_block, remaining)
            total_pages += pages
            if candidates:
                order, funder, tx_hash, _, source = min(candidates, key=lambda row: row[0])
                evidence = hashlib.sha256(
                    f"{wallet}:{funder}:{tx_hash}:{order}:{source}".encode()
                ).hexdigest()
                relationships.append(FundingRelationship(
                    wallet, funder, tx_hash, now, f"etherscan-bsc-funding:{evidence}"
                ))
                if funder.lower() not in queried:
                    queue.append(funder.lower())
            else:
                evidence = hashlib.sha256(
                    f"{wallet}:no-inbound:{end_block}".encode()
                ).hexdigest()
                relationships.append(FundingRelationship(
                    wallet, None, None, now, f"etherscan-bsc-funding:{evidence}"
                ))
        covered = tuple(sorted(queried))
        if {row.wallet_address.lower() for row in relationships} != set(covered):
            raise ProviderError("BSC funding graph does not cover every queried wallet")
        digest = hashlib.sha256(json.dumps(
            [(row.wallet_address.lower(),
              row.funder_address.lower() if row.funder_address else None,
              row.transaction_id, row.evidence_id) for row in relationships],
            sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        return FundingGraphSnapshot(
            provider=self.provider_id, chain="bsc", token_address=token_address,
            observed_at_ms=now, evidence_id=f"etherscan-bsc-funding-graph:{digest}",
            block_ref=f"finalized-block:{end_block};pages:{total_pages}",
            coverage_start_ms=0, coverage_end_ms=now, covered_wallets=covered,
            relationships=tuple(relationships), coverage_complete=True,
        )
