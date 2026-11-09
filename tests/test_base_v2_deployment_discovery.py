import pytest

from veltrix.base_archive_preflight import PreflightError
from veltrix.base_v2_deployment_discovery import discover_one


class FakeRpc:
    label = "fake-archive"

    def __init__(self, address, code_start=7, top_level=True):
        self.address = address
        self.code_start = code_start
        self.top_level = top_level
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_getCode":
            block = int(params[1], 16)
            return "0x60016000" if block >= self.code_start else "0x"
        if method == "eth_getBlockByNumber":
            assert params == [hex(self.code_start), True]
            tx = {"hash": "0x" + "11" * 32, "to": None if self.top_level else "0x" + "22" * 20}
            return {"hash": "0x" + "ab" * 32, "transactions": [tx]}
        if method == "eth_getTransactionReceipt":
            return {
                "status": "0x1",
                "contractAddress": self.address if self.top_level else None,
                "blockNumber": hex(self.code_start),
                "blockHash": "0x" + "ab" * 32,
            }
        raise AssertionError(f"unexpected RPC method: {method}")


def test_discovers_first_code_block_and_top_level_creation_receipt():
    address = "0x" + "33" * 20
    rpc = FakeRpc(address, code_start=7)
    result = discover_one(rpc, "test-v2", address, finalized=31)

    assert result["deployment_block"] == 7
    assert result["creation_tx"] == "0x" + "11" * 32
    assert result["deployment_block_hash"] == "0x" + "ab" * 32
    assert result["runtime_code_bytes"] == 4
    assert result["creation_method"] == "top_level_contract_creation_receipt"


def test_rejects_candidate_without_code_at_finalized_height():
    address = "0x" + "33" * 20
    rpc = FakeRpc(address, code_start=99)
    with pytest.raises(PreflightError, match="no runtime code"):
        discover_one(rpc, "test-v2", address, finalized=31)


def test_fails_closed_when_deployment_was_not_top_level_creation():
    address = "0x" + "33" * 20
    rpc = FakeRpc(address, code_start=7, top_level=False)
    with pytest.raises(PreflightError, match="Internal CREATE/CREATE2"):
        discover_one(rpc, "test-v2", address, finalized=31)
