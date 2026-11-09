"""Read-only, finalized EVM V2 pool and UNCX locker snapshot reader.

The factory registry is operator-supplied and must enumerate every supported
AMM factory. Runtime code hashes, chain IDs, event coverage and cross-RPC block
hash agreement are checked before a complete inventory can be returned.
"""
from dataclasses import dataclass
import hashlib
import json
import re
import time
from urllib.parse import urlparse

import requests
from eth_hash.auto import keccak

from ..liquidity_evidence import (
    LiquidityInventoryBatch, LockerDeployment, LockerPositionSnapshot,
    PoolLiquiditySnapshot,
)
from .base import ProviderError


ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
HASH = re.compile(r"^[0-9a-fA-F]{64}$")
ZERO = "0x" + "00" * 20
PAIR_CREATED_TOPIC = "0x" + keccak(b"PairCreated(address,address,address,uint256)").hex()
BSC_DEPOSIT_TOPIC = "0x" + keccak(
    b"onDeposit(address,address,uint256,uint256,uint256)"
).hex()
BSC_WITHDRAW_TOPIC = "0x" + keccak(b"onWithdraw(address,uint256)").hex()


def _word(value):
    return value.to_bytes(32, "big")


def _selector(signature):
    return keccak(signature.encode())[:4]


def _address_word(address):
    if not ADDRESS.fullmatch(address or ""):
        raise ProviderError("EVM address is malformed")
    return bytes.fromhex(address[2:]).rjust(32, b"\x00")


def _decode_address(word):
    if len(word) != 32 or word[:12] != b"\x00" * 12:
        raise ProviderError("ABI address word is malformed")
    return "0x" + word[12:].hex()


def _decode_uint(word):
    if len(word) != 32:
        raise ProviderError("ABI uint256 word is malformed")
    return int.from_bytes(word, "big")


def _hex_int(value, label):
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ProviderError(f"{label} is malformed")
    try:
        result = int(value, 16)
    except ValueError as exc:
        raise ProviderError(f"{label} is malformed") from exc
    if result < 0:
        raise ProviderError(f"{label} is negative")
    return result


def _code_hash(code):
    if not isinstance(code, str) or not code.startswith("0x") or len(code) <= 2:
        raise ProviderError("contract code is empty or malformed")
    try:
        raw = bytes.fromhex(code[2:])
    except ValueError as exc:
        raise ProviderError("contract code is malformed") from exc
    return keccak(raw).hex()


def _call_data(signature, *words):
    return "0x" + (_selector(signature) + b"".join(words)).hex()


def _decode_lock(data):
    if not isinstance(data, str) or not data.startswith("0x"):
        raise ProviderError("locker mapping returned malformed ABI data")
    raw = bytes.fromhex(data[2:])
    if len(raw) != 8 * 32:
        raise ProviderError("locker mapping ABI tuple has unexpected size")
    words = [raw[i:i + 32] for i in range(0, len(raw), 32)]
    return {
        "lp_token": _decode_address(words[0]),
        "lock_date": _decode_uint(words[1]),
        "amount": _decode_uint(words[2]),
        "initial_amount": _decode_uint(words[3]),
        "unlock_date": _decode_uint(words[4]),
        "lock_id": _decode_uint(words[5]),
        "owner": _decode_address(words[6]),
        "country_code": _decode_uint(words[7]),
    }


@dataclass(frozen=True)
class EvmFactorySpec:
    amm_id: str
    address: str
    deployment_block: int
    expected_code_hash: str


@dataclass(frozen=True)
class EvmLockerSpec:
    amm_id: str
    address: str
    deployment_block: int
    expected_code_hash: str
    abi_profile: str


class _Rpc:
    def __init__(self, url, *, session=None, timeout=20, provider_id):
        parsed = urlparse(url or "")
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("liquidity RPC endpoints must use HTTPS")
        self.url = url
        self.session = session or requests.Session()
        self.timeout = timeout
        self.provider_id = provider_id
        self._counter = 0

    def call(self, method, params):
        self._counter += 1
        try:
            response = self.session.post(
                self.url,
                json={"jsonrpc": "2.0", "id": self._counter,
                      "method": method, "params": params},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"{method} RPC request failed") from exc
        if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
            raise ProviderError(f"{method} returned an RPC error or omitted result")
        return payload["result"]


class EvmV2LiquiditySnapshotReader:
    """Exhaustive configured V2 factory scan plus chain-specific locker reads.

    Base profile: NONCE + LOCKS(index), V2.1 tuple.
    BSC profile: complete onNewLock event history cross-check + NONCE + LOCKS.
    Pool discovery and locker state use separate RPC endpoints and must agree
    on the exact finalized block hash.
    """
    CHAIN_IDS = {"base": 8453, "bsc": 56}
    ABI_PROFILES = {
        "base": "uncx-v2.1-locks-nonce",
        "bsc": "uncx-v1-token-lock-index-events",
    }

    def __init__(
        self, chain, pool_rpc_url, locker_rpc_url, factories, lockers, *,
        registry_complete=False, pool_independence_group="amm-factory-rpc",
        locker_independence_group="locker-state-rpc", session=None,
        block_chunk_size=2_000, max_logs=200_000, max_pools=50_000,
        max_locks=100_000,
    ):
        if chain not in self.CHAIN_IDS:
            raise ValueError("unsupported EVM liquidity chain")
        if not pool_rpc_url or not locker_rpc_url or pool_rpc_url == locker_rpc_url:
            raise ValueError("independent pool and locker RPC endpoints are required")
        if pool_independence_group == locker_independence_group:
            raise ValueError("pool and locker RPC independence groups must differ")
        if not registry_complete:
            raise ValueError("AMM factory registry must be explicitly certified complete")
        if not factories or not lockers:
            raise ValueError("configured factory and locker registries are required")
        if not 1 <= block_chunk_size <= 10_000 or max_logs < 1 or max_pools < 1 or max_locks < 1:
            raise ValueError("invalid EVM liquidity reader bounds")
        self.chain = chain
        self.pool_rpc = _Rpc(pool_rpc_url, session=session, provider_id=f"{chain}-amm-factory-rpc")
        self.locker_rpc = _Rpc(locker_rpc_url, session=session, provider_id=f"{chain}-locker-state-rpc")
        self.factories = tuple(factories)
        self.lockers = tuple(lockers)
        self.pool_group = pool_independence_group
        self.locker_group = locker_independence_group
        self.block_chunk_size = block_chunk_size
        self.max_logs = max_logs
        self.max_pools = max_pools
        self.max_locks = max_locks
        self.provider_id = f"{chain}-finalized-all-factory-liquidity"

        expected_profile = self.ABI_PROFILES[chain]
        for factory in self.factories:
            if (not isinstance(factory, EvmFactorySpec) or not factory.amm_id
                    or not ADDRESS.fullmatch(factory.address)
                    or not HASH.fullmatch(factory.expected_code_hash)
                    or factory.deployment_block < 0):
                raise ValueError("malformed EVM AMM factory registry entry")
        for locker in self.lockers:
            if (not isinstance(locker, EvmLockerSpec) or not locker.amm_id
                    or not ADDRESS.fullmatch(locker.address)
                    or not HASH.fullmatch(locker.expected_code_hash)
                    or locker.deployment_block < 0
                    or locker.abi_profile != expected_profile):
                raise ValueError("unsupported or malformed chain-specific UNCX locker entry")

    def _chain_id(self, rpc):
        actual = _hex_int(rpc.call("eth_chainId", []), "eth_chainId")
        if actual != self.CHAIN_IDS[self.chain]:
            raise ProviderError("RPC chain ID does not match configured chain")

    def _block(self, rpc, tag):
        block = rpc.call("eth_getBlockByNumber", [tag, False])
        if not isinstance(block, dict) or not block.get("hash"):
            raise ProviderError("RPC did not return a finalized block")
        return block

    def _call(self, rpc, address, data, block_hex):
        result = rpc.call("eth_call", [{"to": address, "data": data}, block_hex])
        if not isinstance(result, str) or not result.startswith("0x"):
            raise ProviderError("eth_call response is malformed")
        return result

    def _code(self, rpc, address, block_hex, expected_hash):
        code = rpc.call("eth_getCode", [address, block_hex])
        actual = _code_hash(code)
        if actual.lower() != expected_hash.lower().removeprefix("0x"):
            raise ProviderError(f"runtime code hash mismatch for {address}")
        return actual

    def _scan_factory(self, factory, final_block):
        if factory.deployment_block > final_block:
            raise ProviderError("configured factory deployment block is after finalized head")
        self._code(self.pool_rpc, factory.address, hex(final_block), factory.expected_code_hash)
        found = []
        count = 0
        event_lengths = []
        for start in range(factory.deployment_block, final_block + 1, self.block_chunk_size):
            end = min(final_block, start + self.block_chunk_size - 1)
            logs = self.pool_rpc.call("eth_getLogs", [{
                "address": factory.address, "fromBlock": hex(start),
                "toBlock": hex(end), "topics": [PAIR_CREATED_TOPIC],
            }])
            if not isinstance(logs, list):
                raise ProviderError("factory event scan returned malformed logs")
            count += len(logs)
            if len(logs) >= 10_000:
                raise ProviderError("factory log chunk reached common RPC result cap")
            if count >= self.max_logs:
                raise ProviderError("factory event history exceeds configured complete-scan limit")
            for log in logs:
                if (not isinstance(log, dict) or log.get("removed") is True
                        or log.get("address", "").lower() != factory.address.lower()):
                    raise ProviderError("factory history contains malformed, removed or misattributed logs")
                block_number = _hex_int(log.get("blockNumber"), "factory log block number")
                log_index = _hex_int(log.get("logIndex"), "factory log index")
                if not start <= block_number <= end or not log.get("transactionHash"):
                    raise ProviderError("factory log provenance is outside the requested block range")
                topics = log.get("topics")
                data = log.get("data")
                if not isinstance(topics, list) or len(topics) != 3 or topics[0].lower() != PAIR_CREATED_TOPIC.lower():
                    raise ProviderError("factory PairCreated log has unexpected topics")
                if not isinstance(data, str) or not data.startswith("0x") or len(data) < 66:
                    raise ProviderError("factory PairCreated log data is malformed")
                token0 = _decode_address(bytes.fromhex(topics[1][2:]).rjust(32, b"\x00"))
                token1 = _decode_address(bytes.fromhex(topics[2][2:]).rjust(32, b"\x00"))
                pair = _decode_address(bytes.fromhex(data[2:66]))
                event_pair_count = _decode_uint(bytes.fromhex(data[66:130]))
                event_lengths.append((block_number, log_index, event_pair_count))
                if not ADDRESS.fullmatch(pair):
                    raise ProviderError("factory emitted malformed pair address")
                if token0.lower() != self._token.lower() and token1.lower() != self._token.lower():
                    continue
                found.append((pair, token0, token1, factory))
        length_data = self._call(
            self.pool_rpc, factory.address,
            "0x" + _selector("allPairsLength()").hex(), hex(final_block),
        )
        all_pairs_length = _decode_uint(
            bytes.fromhex(length_data[2:]).rjust(32, b"\\x00")
        )
        event_lengths.sort()
        if (count != all_pairs_length
                or [item[2] for item in event_lengths] != list(range(1, all_pairs_length + 1))):
            raise ProviderError("factory PairCreated history does not reconcile to allPairsLength")
        return found

    def _read_pair(self, pair, token0_event, token1_event, factory, block_hex):
        code = self.pool_rpc.call("eth_getCode", [pair, block_hex])
        if not isinstance(code, str) or code == "0x":
            raise ProviderError("factory-created pair has no runtime code")
        token0 = _decode_address(bytes.fromhex(self._call(
            self.pool_rpc, pair, "0x" + _selector("token0()").hex(), block_hex
        )[2:]).rjust(32, b"\x00"))
        token1 = _decode_address(bytes.fromhex(self._call(
            self.pool_rpc, pair, "0x" + _selector("token1()").hex(), block_hex
        )[2:]).rjust(32, b"\x00"))
        pair_factory = _decode_address(bytes.fromhex(self._call(
            self.pool_rpc, pair, "0x" + _selector("factory()").hex(), block_hex
        )[2:]).rjust(32, b"\x00"))
        if (token0.lower(), token1.lower()) != (token0_event.lower(), token1_event.lower()):
            raise ProviderError("pair token state does not match factory creation event")
        if pair_factory.lower() != factory.address.lower():
            raise ProviderError("pair factory() does not match configured factory")
        if self._token.lower() not in {token0.lower(), token1.lower()}:
            raise ProviderError("discovered pair does not contain requested token")
        return token0, token1

    def _read_base_v21_positions(self, locker, pools, block_hex):
        nonce_data = self._call(
            self.locker_rpc, locker.address, "0x" + _selector("NONCE()").hex(), block_hex
        )
        nonce = _decode_uint(bytes.fromhex(nonce_data[2:]).rjust(32, b"\\x00"))
        if nonce >= self.max_locks:
            raise ProviderError("Base locker nonce exceeds configured exhaustive scan limit")
        positions = []
        pool_keys = {p[0].lower() for p in pools}
        for lock_id in range(nonce):
            data = self._call(
                self.locker_rpc, locker.address,
                _call_data("LOCKS(uint256)", _word(lock_id)), block_hex,
            )
            record = _decode_lock(data)
            if record["lock_id"] != lock_id:
                raise ProviderError("Base LOCKS index does not match stored lock ID")
            lp = record["lp_token"]
            if lp.lower() not in pool_keys or record["amount"] == 0:
                continue
            if record["unlock_date"] <= 0 or record["owner"] == ZERO:
                raise ProviderError("Base active lock has invalid owner or unlock time")
            positions.append(LockerPositionSnapshot(
                chain="base", pool_address=lp, lp_token_address=lp,
                locker_address=locker.address, position_id=f"{locker.address.lower()}:{lock_id}",
                amount=record["amount"], unlock_at_ms=record["unlock_date"] * 1000,
                observed_at_ms=self._observed_ms, owner=record["owner"],
                evidence_ref=f"{self._finalized_ref}:lock:{lock_id}",
                finalized_ref=self._finalized_ref,
            ))
        return positions

    def _read_bsc_v1_positions(self, locker, pools, block_hex, final_block):
        """Enumerate legacy BSC locks by per-LP index and cross-check event history."""
        deposits_by_lp = {}
        event_count = 0
        for start in range(locker.deployment_block, final_block + 1, self.block_chunk_size):
            end = min(final_block, start + self.block_chunk_size - 1)
            for topic in (BSC_DEPOSIT_TOPIC, BSC_WITHDRAW_TOPIC):
                logs = self.locker_rpc.call("eth_getLogs", [{
                    "address": locker.address, "fromBlock": hex(start),
                    "toBlock": hex(end), "topics": [topic],
                }])
                if not isinstance(logs, list):
                    raise ProviderError("BSC locker event history returned malformed logs")
                event_count += len(logs)
                if event_count >= self.max_logs or len(logs) >= 10_000:
                    raise ProviderError("BSC locker event history reached a provider or configured cap")
                for log in logs:
                    if (not isinstance(log, dict) or log.get("removed") is True
                            or log.get("address", "").lower() != locker.address.lower()):
                        raise ProviderError("BSC locker history contains removed or misattributed logs")
                    data = log.get("data", "")
                    if not isinstance(data, str) or not data.startswith("0x"):
                        raise ProviderError("BSC locker event data is malformed")
                    raw = bytes.fromhex(data[2:])
                    if topic == BSC_DEPOSIT_TOPIC:
                        if len(raw) != 5 * 32:
                            raise ProviderError("BSC onDeposit event has unexpected ABI size")
                        lp = _decode_address(raw[0:32])
                        user = _decode_address(raw[32:64])
                        amount = _decode_uint(raw[64:96])
                        lock_date = _decode_uint(raw[96:128])
                        unlock_date = _decode_uint(raw[128:160])
                        if user == ZERO or amount <= 0 or lock_date <= 0 or unlock_date <= 0:
                            raise ProviderError("BSC onDeposit event contains invalid lock fields")
                        deposits_by_lp.setdefault(lp.lower(), 0)
                        deposits_by_lp[lp.lower()] += 1
                    else:
                        if len(raw) != 2 * 32:
                            raise ProviderError("BSC onWithdraw event has unexpected ABI size")
                        lp = _decode_address(raw[0:32])
                        amount = _decode_uint(raw[32:64])
                        if amount <= 0:
                            raise ProviderError("BSC onWithdraw event has invalid amount")

        positions = []
        for pair, *_ in pools:
            count_data = self._call(
                self.locker_rpc, locker.address,
                _call_data("getNumLocksForToken(address)", _address_word(pair)),
                block_hex,
            )
            lock_count = _decode_uint(bytes.fromhex(count_data[2:]).rjust(32, b"\\x00"))
            if lock_count >= self.max_locks:
                raise ProviderError("BSC per-LP lock index exceeds configured exhaustive scan limit")
            if deposits_by_lp.get(pair.lower(), 0) != lock_count:
                raise ProviderError("BSC onDeposit event count does not reconcile to per-LP lock index")
            for index in range(lock_count):
                data = self._call(
                    self.locker_rpc, locker.address,
                    _call_data(
                        "tokenLocks(address,uint256)", _address_word(pair), _word(index)
                    ),
                    block_hex,
                )
                raw = bytes.fromhex(data[2:])
                if len(raw) != 6 * 32:
                    raise ProviderError("BSC tokenLocks index returned unexpected ABI tuple")
                lock_date = _decode_uint(raw[0:32])
                amount = _decode_uint(raw[32:64])
                initial_amount = _decode_uint(raw[64:96])
                unlock_date = _decode_uint(raw[96:128])
                lock_id = _decode_uint(raw[128:160])
                owner = _decode_address(raw[160:192])
                if amount == 0:
                    continue
                if (lock_date <= 0 or unlock_date <= 0 or owner == ZERO
                        or amount > initial_amount):
                    raise ProviderError("BSC indexed lock has invalid current state")
                positions.append(LockerPositionSnapshot(
                    chain="bsc", pool_address=pair, lp_token_address=pair,
                    locker_address=locker.address,
                    position_id=f"{locker.address.lower()}:{pair.lower()}:{index}:{lock_id}",
                    amount=amount, unlock_at_ms=unlock_date * 1000,
                    observed_at_ms=self._observed_ms, owner=owner,
                    evidence_ref=f"{self._finalized_ref}:tokenLocks:{pair}:{index}",
                    finalized_ref=self._finalized_ref,
                ))
        return positions

    def _read_locker_positions(self, locker, pools, block_hex, final_block):
        self._code(self.locker_rpc, locker.address, block_hex, locker.expected_code_hash)
        factory_data = self._call(
            self.locker_rpc, locker.address, "0x" + _selector("uniswapFactory()").hex(), block_hex
        )
        factory_address = _decode_address(bytes.fromhex(factory_data[2:]).rjust(32, b"\\x00"))
        matching_factory = next(
            (f for f in self.factories if f.address.lower() == factory_address.lower()
             and f.amm_id == locker.amm_id), None
        )
        if matching_factory is None:
            raise ProviderError("locker constructor factory is not in the certified AMM registry")

        if self.chain == "base":
            positions = self._read_base_v21_positions(locker, pools, block_hex)
        else:
            positions = self._read_bsc_v1_positions(
                locker, pools, block_hex, final_block
            )
        deployment = LockerDeployment(
            chain=self.chain, provider=self.locker_rpc.provider_id,
            address=locker.address, abi_profile=locker.abi_profile,
            expected_code_hash=locker.expected_code_hash,
            observed_code_hash=locker.expected_code_hash,
            source_ref="https://docs.uncx.network/guides/for-developers/liquidity-lockers/lockers-v2/contracts",
            independence_group=self.locker_group,
            chain_id=self.CHAIN_IDS[self.chain],
        )
        return positions, deployment

    def fetch_inventory(self, token_address):
        if not ADDRESS.fullmatch(token_address or ""):
            raise ValueError("EVM token address is malformed")
        self._token = token_address
        self._chain_id(self.pool_rpc)
        self._chain_id(self.locker_rpc)
        final_pool = self._block(self.pool_rpc, "finalized")
        block_number = _hex_int(final_pool.get("number"), "finalized block number")
        block_hex = hex(block_number)
        final_locker = self._block(self.locker_rpc, block_hex)
        if final_locker.get("hash", "").lower() != final_pool["hash"].lower():
            raise ProviderError("independent RPC endpoints disagree on finalized block hash")
        self._finalized_ref = f"block:{block_number}:{final_pool['hash']}"
        self._observed_ms = _hex_int(final_pool.get("timestamp"), "block timestamp") * 1000

        discovered = []
        for factory in self.factories:
            discovered.extend(self._scan_factory(factory, block_number))
        if len(discovered) >= self.max_pools:
            raise ProviderError("AMM pool inventory exceeds configured limit")
        unique = {}
        for pair, token0, token1, factory in discovered:
            key = pair.lower()
            if key in unique:
                raise ProviderError("duplicate pool address emitted by configured AMM factories")
            unique[key] = (pair, token0, token1, factory)

        pool_rows = []
        for pair, token0_event, token1_event, factory in unique.values():
            token0, token1 = self._read_pair(pair, token0_event, token1_event, factory, block_hex)
            supply_data = self._call(
                self.locker_rpc, pair, "0x" + _selector("totalSupply()").hex(), block_hex
            )
            supply = _decode_uint(bytes.fromhex(supply_data[2:]).rjust(32, b"\x00"))
            if supply <= 0:
                raise ProviderError("discovered pool LP total supply is zero or unavailable")
            pool_rows.append((pair, token0, token1, factory, supply))

        positions = []
        deployments = []
        locker_balances = {row[0].lower(): 0 for row in pool_rows}
        for locker in self.lockers:
            new_positions, deployment = self._read_locker_positions(
                locker, [(row[0], row[1], row[2], row[3]) for row in pool_rows],
                block_hex, block_number,
            )
            positions.extend(new_positions)
            deployments.append(deployment)
            for pair, *_ in pool_rows:
                result = self._call(
                    self.locker_rpc, pair,
                    _call_data("balanceOf(address)", _address_word(locker.address)),
                    block_hex,
                )
                locker_balances[pair.lower()] += _decode_uint(
                    bytes.fromhex(result[2:]).rjust(32, b"\x00")
                )

        pools = tuple(PoolLiquiditySnapshot(
            chain=self.chain, token_address=token_address, pool_address=pair,
            lp_token_address=pair, total_lp_supply=supply,
            observed_at_ms=self._observed_ms, finalized_ref=self._finalized_ref,
            inventory_provider=self.provider_id, inventory_complete=True,
            pool_set_id=f"{self.chain}:configured-factories:{','.join(sorted(f.amm_id for f in self.factories))}",
            locker_custody_amount=locker_balances[pair.lower()],
        ) for pair, _t0, _t1, _factory, supply in pool_rows)

        # All configured factories were scanned from their deployment blocks;
        # an empty result is complete discovery, but cannot certify a lock.
        return LiquidityInventoryBatch(
            chain=self.chain, token_address=token_address,
            provider=self.provider_id, independence_group=self.pool_group,
            observed_at_ms=self._observed_ms, finalized_ref=self._finalized_ref,
            pool_set_id=f"{self.chain}:configured-factories:{','.join(sorted(f.amm_id for f in self.factories))}",
            inventory_complete=True, pools=pools, positions=tuple(positions),
            deployments=tuple(deployments),
        )
