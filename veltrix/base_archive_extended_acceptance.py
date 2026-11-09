"""Extended read-only Base archive acceptance for Alchemy vs SolidRPC.

This is a qualification test, not automatic provider certification. It checks
multiple historical states, finalized-tag consistency, contract calls, and
factory event-log retrieval over a non-empty historical range.
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


def normalized_logs(rpc, factory, from_block, to_block, stop_after=None, max_block_span=10, coverage=None):
    """Fetch historical logs in provider-compatible chunks.

    Alchemy Free permits at most a 10-block eth_getLogs range. Scan backward
    and stop once the known allPairsLength delta is reached: because each
    PairCreated event increments that counter exactly once, this proves the
    complete event set for the interval without making unnecessary requests.
    """
    if from_block > to_block:
        raise PreflightError(f"{rpc.label}: invalid historical log range")
    if max_block_span < 1 or max_block_span > 10:
        raise PreflightError(f"{rpc.label}: eth_getLogs chunk size must be 1-10 blocks")

    normalized = []
    chunk_to = to_block
    while chunk_to >= from_block:
        chunk_from = max(from_block, chunk_to - max_block_span + 1)
        if coverage is not None:
            coverage["queried_from_block"] = chunk_from
            coverage["queried_to_block"] = to_block
        rows = rpc.call("eth_getLogs", [{
            "address": factory,
            "fromBlock": hex(chunk_from),
            "toBlock": hex(chunk_to),
            "topics": [PAIR_CREATED_TOPIC],
        }])
        if not isinstance(rows, list):
            raise PreflightError(f"{rpc.label}: eth_getLogs returned malformed data")
        for row in rows:
            if not isinstance(row, dict):
                raise PreflightError(f"{rpc.label}: malformed log row")
            topics = row.get("topics", [])
            if not isinstance(topics, list) or not topics or str(topics[0]).lower() != PAIR_CREATED_TOPIC:
                raise PreflightError(f"{rpc.label}: eth_getLogs returned a non-PairCreated event")
            normalized.append({
                "blockHash": str(row.get("blockHash", "")).lower(),
                "blockNumber": quantity(row.get("blockNumber"), f"{rpc.label} log block number"),
                "transactionHash": str(row.get("transactionHash", "")).lower(),
                "logIndex": quantity(row.get("logIndex"), f"{rpc.label} log index"),
                "topics": [str(x).lower() for x in topics],
                "data": str(row.get("data", "")).lower(),
            })
        if stop_after is not None and len(normalized) >= stop_after:
            break
        chunk_to = chunk_from - 1

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
        results[str(number)] = {
            "block_hash": block_hash,
            "runtime_code_bytes": len(code_bytes),
            "runtime_code_keccak256": keccak(code_bytes).hex() if code_bytes else None,
            "all_pairs_length": call_count,
        }

    # Query a range known to contain PairCreated events: the final two
    # historical anchors show allPairsLength increasing. This tests actual
    # historical log retrieval rather than only empty single-block queries.
    log_from, log_to = anchors[-2], anchors[-1]
    expected_delta = (
        results[str(log_to)]["all_pairs_length"]
        - results[str(log_from)]["all_pairs_length"]
    )
    if expected_delta <= 0:
        raise PreflightError(
            f"{rpc.label}: selected historical log range has no pair-count increase"
        )
    log_coverage = {}
    logs = normalized_logs(
        rpc, factory, log_from + 1, log_to,
        stop_after=expected_delta, coverage=log_coverage,
    )
    if len(logs) != expected_delta:
        raise PreflightError(
            f"{rpc.label}: PairCreated log count {len(logs)} does not match "
            f"allPairsLength delta {expected_delta} in blocks {log_from + 1}-{log_to}"
        )
    for log in logs:
        if log["blockNumber"] < log_from + 1 or log["blockNumber"] > log_to:
            raise PreflightError(f"{rpc.label}: PairCreated log outside requested range")

    return {
        "provider": rpc.label,
        "chain_id": chain_id,
        "finalized": finalized,
        "anchors": results,
        "pair_created_log_range": {
            "from_block": log_from + 1,
            "to_block": log_to,
            "expected_pair_count_delta": expected_delta,
            "retrieved_log_count": len(logs),
            "queried_from_block": log_coverage["queried_from_block"],
            "queried_to_block": log_coverage["queried_to_block"],
            "logs": logs,
        },
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
            "all_pairs_length",
        ):
            if a[field] != b[field]:
                raise PreflightError(
                    f"providers disagree at block {number} for {field}"
                )

    if first["pair_created_log_range"] != second["pair_created_log_range"]:
        raise PreflightError("providers disagree on historical PairCreated log range")

    result = {
        "status": "passed",
        "purpose": "extended read-only archive qualification; not automatic certification",
        "factory": factory,
        "creation_tx": creation_tx,
        "deployment_block": deployment_block,
        "finalized_block": first_finalized,
        "historical_anchor_blocks": anchors,
        "providers": [first, second],
        "comparison": "matching finalized head and historical block, code, call, plus non-empty PairCreated log range reconciled to pair-count delta",
    }
    with open("base-archive-extended-acceptance.json", "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps({
        "status": "passed",
        "finalized_block": first_finalized,
        "historical_anchor_count": len(anchors),
        "historical_checks": [
            "eth_getBlockByNumber", "eth_getCode", "eth_call",
            "eth_getLogs (non-empty historical range; count reconciled to allPairsLength delta)"
        ],
        "report_file": "base-archive-extended-acceptance.json",
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (PreflightError, ValueError) as exc:
        print(f"BASE_ARCHIVE_EXTENDED_ACCEPTANCE_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
