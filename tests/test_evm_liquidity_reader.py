import time

import pytest
from eth_hash.auto import keccak

from veltrix.liquidity_evidence import LiquidityEvidenceAnalyzer
from veltrix.providers.evm_liquidity_reader import (
    EvmFactorySpec, EvmLockerSpec, EvmV2LiquiditySnapshotReader,
    BSC_DEPOSIT_TOPIC, BSC_WITHDRAW_TOPIC, PAIR_CREATED_TOPIC,
)


TOKEN = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"
PAIR = "0x3333333333333333333333333333333333333333"
FACTORY = "0x4444444444444444444444444444444444444444"
LOCKER = "0x5555555555555555555555555555555555555555"
OWNER = "0x6666666666666666666666666666666666666666"
CODE = "0x60006000"
CODE_HASH = keccak(bytes.fromhex(CODE[2:])).hex()
BLOCK_HASH = "0x" + "ab" * 32
BLOCK_TIME = 2_000_000_000


def word(value):
    return int(value).to_bytes(32, "big").hex()


def address_topic(address):
    return "0x" + address[2:].lower().rjust(64, "0")


def pair_created_log(pair=PAIR, pair_count=1, log_index=0):
    return {
        "address": FACTORY, "removed": False, "topics": [
            PAIR_CREATED_TOPIC, address_topic(TOKEN), address_topic(OTHER),
        ],
        "data": "0x" + address_topic(pair)[2:] + word(pair_count),
        "blockNumber": "0x1", "logIndex": hex(log_index),
        "transactionHash": "0x" + "cd" * 32,
    }


def bsc_deposit_log():
    return {
        "address": LOCKER, "removed": False, "topics": [BSC_DEPOSIT_TOPIC],
        "data": "0x" + address_topic(PAIR)[2:] + address_topic(OWNER)[2:]
        + word(100) + word(BLOCK_TIME - 100) + word(BLOCK_TIME + 100_000),
        "blockNumber": "0x1", "logIndex": "0x0",
        "transactionHash": "0x" + "ef" * 32,
    }


class Response:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return {"jsonrpc": "2.0", "id": 1, "result": self.value}


class RpcSession:
    def __init__(self, chain, *, block_hash=BLOCK_HASH, omit_bsc_event=False, extra_bsc_relock=False):
        self.chain = chain
        self.block_hash = block_hash
        self.omit_bsc_event = omit_bsc_event
        self.extra_bsc_relock = extra_bsc_relock

    def post(self, url, json, timeout):
        method, params = json["method"], json["params"]
        if method == "eth_chainId":
            return Response(hex(8453 if self.chain == "base" else 56))
        if method == "eth_getBlockByNumber":
            return Response({
                "number": "0x64", "hash": self.block_hash,
                "timestamp": hex(BLOCK_TIME),
            })
        if method == "eth_getCode":
            return Response(CODE)
        if method == "eth_getLogs":
            address = params[0]["address"].lower()
            if address == FACTORY.lower():
                return Response([pair_created_log()] if params[0]["fromBlock"] == "0x1" else [])
            if (address == LOCKER.lower() and params[0]["topics"][0] == BSC_DEPOSIT_TOPIC
                    and params[0]["fromBlock"] == "0x1"):
                return Response([] if self.omit_bsc_event else [bsc_deposit_log()] + ([bsc_deposit_log()] if self.extra_bsc_relock else []))
            if address == LOCKER.lower() and params[0]["topics"][0] == BSC_WITHDRAW_TOPIC:
                return Response([])
            return Response([])
        if method == "eth_call":
            call = params[0]
            data = call["data"]
            selector = data[:10]
            if selector == "0x" + keccak(b"allPairsLength()")[:4].hex():
                return Response("0x" + word(1))
            if selector == "0x" + keccak(b"token0()")[:4].hex():
                return Response("0x" + address_topic(TOKEN)[2:])
            if selector == "0x" + keccak(b"token1()")[:4].hex():
                return Response("0x" + address_topic(OTHER)[2:])
            if selector == "0x" + keccak(b"factory()")[:4].hex():
                return Response("0x" + address_topic(FACTORY)[2:])
            if selector == "0x" + keccak(b"totalSupply()")[:4].hex():
                return Response("0x" + word(100))
            if selector == "0x" + keccak(b"uniswapFactory()")[:4].hex():
                return Response("0x" + address_topic(FACTORY)[2:])
            if selector == "0x" + keccak(b"getNumLocksForToken(address)")[:4].hex():
                return Response("0x" + word(1))
            if selector == "0x" + keccak(b"tokenLocks(address,uint256)")[:4].hex():
                legacy_lock = (
                    word(BLOCK_TIME - 100) + word(100) + word(100)
                    + word(BLOCK_TIME + 100_000) + word(0)
                    + address_topic(OWNER)[2:]
                )
                return Response("0x" + legacy_lock)
            if selector == "0x" + keccak(b"NONCE()")[:4].hex():
                return Response("0x" + word(1))
            if selector == "0x" + keccak(b"LOCKS(uint256)")[:4].hex():
                lock = (
                    address_topic(PAIR)[2:] + word(BLOCK_TIME - 100)
                    + word(100) + word(100) + word(BLOCK_TIME + 100_000)
                    + word(0) + address_topic(OWNER)[2:] + word(0)
                )
                return Response("0x" + lock)
            if selector == "0x" + keccak(b"balanceOf(address)")[:4].hex():
                return Response("0x" + word(100))
            raise AssertionError(f"unexpected eth_call selector {selector}")
        raise AssertionError(f"unexpected RPC method {method}")


def reader(chain, session=None, **kwargs):
    amm_id = "uniswap-v2" if chain == "base" else "pancakeswap-v2"
    profile = "uncx-v2.1-locks-nonce" if chain == "base" else "uncx-v1-token-lock-index-events"
    session = session or RpcSession(chain)
    return EvmV2LiquiditySnapshotReader(
        chain, "https://pool-rpc.example", "https://locker-rpc.example",
        (EvmFactorySpec(amm_id, FACTORY, 1, CODE_HASH),),
        (EvmLockerSpec(amm_id, LOCKER, 1, CODE_HASH, profile),),
        registry_complete=True, session=session, block_chunk_size=50, **kwargs,
    )


@pytest.mark.parametrize("chain", ["base", "bsc"])
def test_evm_reader_discovers_pool_and_reconciles_lock_at_one_finalized_block(chain):
    batch = reader(chain).fetch_inventory(TOKEN)
    assert batch.inventory_complete
    assert batch.finalized_ref == f"block:100:{BLOCK_HASH}"
    assert len(batch.pools) == 1
    assert batch.pools[0].pool_address == PAIR
    assert batch.pools[0].total_lp_supply == 100
    assert batch.pools[0].locker_custody_amount == 100
    assert len(batch.positions) == 1
    assert batch.positions[0].amount == 100
    assert batch.deployments[0].observed_code_hash == CODE_HASH
    assert batch.provider != batch.deployments[0].provider
    report = LiquidityEvidenceAnalyzer().evaluate(
        batch, chain=chain, token_address=TOKEN, now_ms=BLOCK_TIME * 1000,
    )
    assert report.passed


def test_bsc_reader_requires_event_history_to_reconcile_every_nonce():
    with pytest.raises(Exception, match="history is incomplete"):
        reader("bsc", RpcSession("bsc", omit_bsc_event=True)).fetch_inventory(TOKEN)


def test_evm_reader_rejects_rpc_block_hash_disagreement():
    class SplitSession(RpcSession):
        def post(self, url, json, timeout):
            if url.startswith("https://locker") and json["method"] == "eth_getBlockByNumber":
                old = self.block_hash
                self.block_hash = "0x" + "99" * 32
                result = super().post(url, json, timeout)
                self.block_hash = old
                return result
            return super().post(url, json, timeout)

    with pytest.raises(Exception, match="disagree on finalized block hash"):
        reader("base", SplitSession("base")).fetch_inventory(TOKEN)


def test_evm_reader_requires_explicit_complete_factory_registry():
    with pytest.raises(ValueError, match="explicitly certified complete"):
        EvmV2LiquiditySnapshotReader(
            "base", "https://pool.example", "https://locker.example",
            (EvmFactorySpec("uni", FACTORY, 1, CODE_HASH),),
            (EvmLockerSpec("uni", LOCKER, 1, CODE_HASH, "uncx-v2.1-locks-nonce"),),
            registry_complete=False,
        )


def test_factory_scan_includes_unlocked_second_pool_in_inventory():
    second = "0x7777777777777777777777777777777777777777"

    class TwoPoolSession(RpcSession):
        def post(self, url, json, timeout):
            method, params = json["method"], json["params"]
            if method == "eth_getLogs" and params[0]["address"].lower() == FACTORY.lower():
                return Response([pair_created_log(PAIR, 1, 0), pair_created_log(second, 2, 1)]
                                if params[0]["fromBlock"] == "0x1" else [])
            if (method == "eth_call" and params[0]["to"].lower() == FACTORY.lower()
                    and params[0]["data"][:10] == "0x" + keccak(b"allPairsLength()")[:4].hex()):
                return Response("0x" + word(2))
            if method == "eth_call" and params[0]["to"].lower() == second.lower():
                selector = params[0]["data"][:10]
                if selector == "0x" + keccak(b"allPairsLength()")[:4].hex():
                    return Response("0x" + word(2))
                if selector == "0x" + keccak(b"token0()")[:4].hex():
                    return Response("0x" + address_topic(TOKEN)[2:])
                if selector == "0x" + keccak(b"token1()")[:4].hex():
                    return Response("0x" + address_topic(OTHER)[2:])
                if selector == "0x" + keccak(b"factory()")[:4].hex():
                    return Response("0x" + address_topic(FACTORY)[2:])
                if selector == "0x" + keccak(b"totalSupply()")[:4].hex():
                    return Response("0x" + word(100))
                if selector == "0x" + keccak(b"balanceOf(address)")[:4].hex():
                    return Response("0x" + word(0))
            return super().post(url, json, timeout)

    batch = reader("base", TwoPoolSession("base")).fetch_inventory(TOKEN)
    assert {pool.pool_address for pool in batch.pools} == {PAIR, second}
    assert batch.pools[1].locker_custody_amount == 0
    report = LiquidityEvidenceAnalyzer().evaluate(
        batch, chain="base", token_address=TOKEN, now_ms=BLOCK_TIME * 1000,
    )
    assert not report.passed
    assert report.pool_count == 2
    assert report.active_locked_pool_count == 1


def test_bsc_legacy_event_topics_match_verified_contract_abi():
    assert BSC_DEPOSIT_TOPIC == "0x830357565da6ecfc26d8d9f69df488ed6f70361af9a07e570544aeb5c5e765e5"
    assert BSC_WITHDRAW_TOPIC == "0xccad973dcd043c7d680389db4378bd6b9775db7124092e9e0422c9e46d7985dc"


def test_factory_log_history_must_match_all_pairs_length():
    class MissingPairSession(RpcSession):
        def post(self, url, json, timeout):
            method, params = json["method"], json["params"]
            if method == "eth_getLogs" and params[0]["address"].lower() == FACTORY.lower():
                return Response([])
            if method == "eth_call" and params[0]["data"][:10] == "0x" + keccak(b"allPairsLength()")[:4].hex():
                return Response("0x" + word(1))
            return super().post(url, json, timeout)

    with pytest.raises(Exception, match="does not reconcile to allPairsLength"):
        reader("base", MissingPairSession("base")).fetch_inventory(TOKEN)


def test_bsc_reader_accepts_relock_history_without_overcounting_positions():
    batch = reader("bsc", RpcSession("bsc", extra_bsc_relock=True)).fetch_inventory(TOKEN)
    assert batch.inventory_complete
    assert len(batch.positions) == 1
