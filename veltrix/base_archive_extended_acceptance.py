"""Extended read-only Base archive acceptance for Alchemy vs SolidRPC.

This is a qualification test, not automatic provider certification. It checks
multiple historical states, finalized-tag consistency, contract calls, and
factory event-log retrieval at exact block heights.
"""
import json
import os
import sys

from eth_hash.auto import keccak

from veltrix.base_archive_preflight import (
    ALL_PAIRS_LENGTH_SELECTOR,
    CHAIN_ID,
    DEPLOYMENT_BLOCK_DEFAULT,
    FACTORY_DEFAULT,
    PreflightError,
    Rpc,
    TX_DEFAULT,
    quantity,
)

PAIR_CREATED_TOPIC = "0x" + keccak(
    b"PairCreated(address,address,address,uint256)"
).hex()


def block_by_tag(rpc, tag):
    block = rpc.call("eth_getBlockByNumber", [tag, False])
    if not isinstance(block, dict) or not block.get("number") or not block.get("hash"):
        raise PreflightError(f"{rpc.label}: {tag} block unavailable")
    return {
        "number": quantity(block["number"], f"{rpc.label} {tag} block number"),
        "hash": block["hash"].lower(),
    }


def block_by_number(rpc, number):
    block = rpc.call("eth_getBlockByNumber", [hex(number), False])
    if not isinstance(block, dict) or not block.get("hash"):
        raise PreflightError(f"{rpc.label}: historical block {number} unavailable")
    return block["hash"].lower()


def normalized_logs(rpc, factory, number):
    rows = rpc.call("eth_getLogs", [{
        "address": factory,
        "fromBlock": hex(number),
        "toBlock": hex(number),
        "topics": [PAIR_CREATED_TOPIC],
    }])
    if not isinstance(rows, list):
        raise PreflightError(f"{rpc.label}: eth_getLogs returned malformed data")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise PreflightError(f"{rpc.label}: malformed log row")
        normalized.append({
            "blockHash": str(row.get("blockHash", "")).lower(),
            "transactionHash": str(row.get("transactionHash", "")).lower(),
            "logIndex": quantity(row.get("logIndex"), f"{rpc.label} log index"),
            "topics": [str(x).lower() for x in row.get("topics", [])],
            "data": str(row.get("data", "")).lower(),
        })
    return sorted(normalized, key=lambda x: (x["transactionHash"], x["logIndex"]))


def inspect_provider(rpc, factory, creation_tx, deployment_block, anchors):
    chain_id = quantity(rpc.call("eth_chainId", []), f"{rpc.label} chain ID")
    if chain_id != CHAIN_ID:
        raise PreflightError(f"{rpc.label}: expected Base chain ID 8453, received {chain_id}")

    receipt = rpc.call("eth_getTransactionReceipt", [creation_tx])
    if not isinstance(receipt, dict) or receipt.get("status") != "0x1":
        raise PreflightError(f"{rpc.label}: factory creation receipt missing or unsuccessful")
    if str(receipt.get("contractAddress", "")).lower() != factory.lower():
        raise PreflightError(f"{rpc.label}: factory creation address mismatch")
    if quantity(receipt.get("blockNumber"), f"{rpc.label} receipt block") != deployment_block:
        raise PreflightError(f"{rpc.label}: factory deployment block mismatch")

    finalized = block_by_tag(rpc, "finalized")
    results = {}
    for number in anchors:
        block_hash = block_by_number(rpc, number)
        code = rpc.call("eth_getCode", [factory, hex(number)])
        if not isinstance(code, str) or not code.startswith("0x"):
            raise PreflightError(f"{rpc.label}: malformed eth_getCode at {number}")
        try:
            code_bytes = bytes.fromhex(code[2:])
        except ValueError as exc:
            raise PreflightError(f"{rpc.label}: invalid bytecode at {number}") from exc

        # The factory exists at and after its deployment block.
        if number >= deployment_block and not code_bytes:
            raise PreflightError(f"{rpc.label}: factory bytecode missing at {number}")
        call_count = None
        if number >= deployment_block:
            call_count = quantity(
                rpc.call("eth_call", [
                    {"to": factory, "data": ALL_PAIRS_LENGTH_SELECTOR},
                    hex(number),
                ]),
                f"{rpc.label} allPairsLength at {number}",
            )
        logs = normalized_logs(rpc, factory, number)
        results[str(number)] = {
            "block_hash": block_hash,
            "runtime_code_bytes": len(code_bytes),
            "runtime_code_keccak256": keccak(code_bytes).hex() if code_bytes else None,
            "all_pairs_length": call_count,
            "pair_created_logs": logs,
        }

    return {
        "provider": rpc.label,
        "chain_id": chain_id,
        "finalized": finalized,
        "anchors": results,
        "checks": {
            "chain_id": "passed",
            "creation_receipt": "passed",
            "finalized_tag": "passed",
            "historical_block_hashes": "passed",
            "historical_eth_getCode": "passed",
            "historical_eth_call": "passed",
            "historical_eth_getLogs": "passed",
        },
    }


def main():
    factory = os.getenv("BASE_FACTORY_ADDRESS", FACTORY_DEFAULT)
    creation_tx = os.getenv("BASE_FACTORY_CREATION_TX", TX_DEFAULT)
    deployment_block = int(os.getenv(
        "BASE_FACTORY_DEPLOYMENT_BLOCK", str(DEPLOYMENT_BLOCK_DEFAULT)
    ))
    primary = Rpc(os.getenv("BASE_LIQUIDITY_POOL_RPC_URL"), "primary/Alchemy")
    candidate = Rpc(os.getenv("SOLIDRPC_BASE_ARCHIVE_RPC_URL"), "candidate/SolidRPC")

    first_finalized = block_by_tag(primary, "finalized")
    second_finalized = block_by_tag(candidate, "finalized")
    if first_finalized["number"] != second_finalized["number"]:
        raise PreflightError("providers disagree on finalized block number")
    if first_finalized["hash"] != second_finalized["hash"]:
        raise PreflightError("providers disagree on finalized block hash")
    finalized_number = first_finalized["number"]
    if finalized_number <= deployment_block + 1000:
        raise PreflightError("finalized head is too close to deployment for extended anchors")

    anchors = sorted(set([
        deployment_block,
        deployment_block + 1,
        deployment_block + 1000,
        max(deployment_block + 1001, finalized_number - 100000),
        max(deployment_block + 1001, finalized_number - 10000),
        finalized_number,
    ]))
    if anchors[-1] > finalized_number:
        raise PreflightError("computed historical anchor exceeds finalized head")

    first = inspect_provider(primary, factory, creation_tx, deployment_block, anchors)
    second = inspect_provider(candidate, factory, creation_tx, deployment_block, anchors)

    for number in anchors:
        key = str(number)
        a, b = first["anchors"][key], second["anchors"][key]
        for field in (
            "block_hash", "runtime_code_bytes", "runtime_code_keccak256",
            "all_pairs_length", "pair_created_logs",
        ):
            if a[field] != b[field]:
                raise PreflightError(
                    f"providers disagree at block {number} for {field}"
                )

    result = {
        "status": "passed",
        "purpose": "extended read-only archive qualification; not automatic certification",
        "factory": factory,
        "creation_tx": creation_tx,
        "deployment_block": deployment_block,
        "finalized_block": first_finalized,
        "historical_anchor_blocks": anchors,
        "providers": [first, second],
        "comparison": "matching finalized head and all historical block, code, call, and log results",
    }
    with open("base-archive-extended-acceptance.json", "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps({
        "status": "passed",
        "finalized_block": first_finalized,
        "historical_anchor_count": len(anchors),
        "historical_checks": [
            "eth_getBlockByNumber", "eth_getCode", "eth_call", "eth_getLogs"
        ],
        "report_file": "base-archive-extended-acceptance.json",
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (PreflightError, ValueError) as exc:
        print(f"BASE_ARCHIVE_EXTENDED_ACCEPTANCE_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
