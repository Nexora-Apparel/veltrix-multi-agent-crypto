"""Discover Base V2 factory deployment metadata using two archive RPC providers.

The script derives the first block containing runtime code, then identifies
the top-level contract-creation transaction from that block's receipts.
It is read-only and fails closed when a factory was deployed internally.
"""
import json
import os
import sys

from eth_hash.auto import keccak
from veltrix.base_archive_preflight import PreflightError, Rpc, quantity

CHAIN_ID = 8453
CANDIDATES = [
    ("sushiswap-v2", "0x71524B4f93c58fcbF659783284E38825f0622859"),
    ("baseswap-v2", "0xFDa619b6d20975be80A10332cD39b9a4b0FAa8BB"),
    ("quickswap-v2", "0xEC6540261aaaE13F236A032d454dc9287E52e56A"),
    ("swapbased-v2", "0x04C9f118d21e8B767D2e50C946f0cC9F6C367300"),
]


def runtime_code(rpc, address, block):
    value = rpc.call("eth_getCode", [address, hex(block)])
    if not isinstance(value, str) or not value.startswith("0x"):
        raise PreflightError(f"{rpc.label}: malformed eth_getCode at block {block}")
    try:
        return bytes.fromhex(value[2:])
    except ValueError as exc:
        raise PreflightError(f"{rpc.label}: invalid runtime bytecode at block {block}") from exc


def discover_one(rpc, amm_id, address, finalized):
    if runtime_code(rpc, address, 0):
        raise PreflightError(f"{amm_id}: candidate already has code at genesis; deployment boundary is ambiguous")
    if not runtime_code(rpc, address, finalized):
        raise PreflightError(f"{amm_id}: no runtime code at finalized block")

    # Invariant: low has no code, high has code. Find the first code-bearing block.
    low, high = 0, finalized
    while high - low > 1:
        middle = (low + high) // 2
        if runtime_code(rpc, address, middle):
            high = middle
        else:
            low = middle

    if runtime_code(rpc, address, high - 1):
        raise PreflightError(f"{amm_id}: deployment boundary verification failed")
    code = runtime_code(rpc, address, high)
    if not code:
        raise PreflightError(f"{amm_id}: runtime code missing at derived deployment block")

    block = rpc.call("eth_getBlockByNumber", [hex(high), True])
    if not isinstance(block, dict) or not isinstance(block.get("hash"), str):
        raise PreflightError(f"{amm_id}: deployment block unavailable")
    transactions = block.get("transactions")
    if not isinstance(transactions, list):
        raise PreflightError(f"{amm_id}: deployment block did not return full transactions")

    matches = []
    for tx in transactions:
        if not isinstance(tx, dict) or tx.get("to") is not None:
            continue
        tx_hash = tx.get("hash")
        if not isinstance(tx_hash, str) or not tx_hash.startswith("0x"):
            raise PreflightError(f"{amm_id}: malformed transaction in deployment block")
        receipt = rpc.call("eth_getTransactionReceipt", [tx_hash])
        if not isinstance(receipt, dict):
            raise PreflightError(f"{amm_id}: missing receipt for deployment-block creation transaction")
        created = receipt.get("contractAddress")
        if isinstance(created, str) and created.lower() == address.lower():
            if quantity(receipt.get("status"), f"{amm_id} creation receipt status") != 1:
                raise PreflightError(f"{amm_id}: factory creation transaction failed")
            if quantity(receipt.get("blockNumber"), f"{amm_id} creation receipt block") != high:
                raise PreflightError(f"{amm_id}: creation receipt block mismatch")
            if str(receipt.get("blockHash", "")).lower() != block["hash"].lower():
                raise PreflightError(f"{amm_id}: creation receipt block hash mismatch")
            matches.append(tx_hash.lower())

    if len(matches) != 1:
        raise PreflightError(
            f"{amm_id}: expected one top-level creation transaction for {address} "
            f"at block {high}; found {len(matches)}. Internal CREATE/CREATE2 deployments "
            "require a separately verified trace and are not guessed."
        )

    return {
        "amm_id": amm_id,
        "address": address,
        "creation_tx": matches[0],
        "deployment_block": high,
        "deployment_block_hash": block["hash"].lower(),
        "runtime_code_bytes": len(code),
        "runtime_code_keccak256": keccak(code).hex(),
        "creation_method": "top_level_contract_creation_receipt",
    }


def main():
    primary = Rpc(os.getenv("BASE_LIQUIDITY_POOL_RPC_URL"), "primary/Alchemy")
    secondary = Rpc(os.getenv("SOLIDRPC_BASE_ARCHIVE_RPC_URL"), "candidate/SolidRPC")
    if primary.url == secondary.url:
        raise PreflightError("archive endpoints must be independent")

    chain_a = quantity(primary.call("eth_chainId", []), "primary chain id")
    chain_b = quantity(secondary.call("eth_chainId", []), "secondary chain id")
    if chain_a != CHAIN_ID or chain_b != CHAIN_ID:
        raise PreflightError("both archive providers must report Base chain id 8453")

    head_a = primary.call("eth_getBlockByNumber", ["finalized", False])
    head_b = secondary.call("eth_getBlockByNumber", ["finalized", False])
    if not isinstance(head_a, dict) or not isinstance(head_b, dict):
        raise PreflightError("finalized block tag unavailable")
    height = min(quantity(head_a.get("number"), "primary finalized height"),
                 quantity(head_b.get("number"), "secondary finalized height"))
    block_a = primary.call("eth_getBlockByNumber", [hex(height), False])
    block_b = secondary.call("eth_getBlockByNumber", [hex(height), False])
    if (not isinstance(block_a, dict) or not isinstance(block_b, dict)
            or str(block_a.get("hash", "")).lower() != str(block_b.get("hash", "")).lower()):
        raise PreflightError("archive providers disagree at common finalized checkpoint")

    results = []
    for amm_id, address in CANDIDATES:
        left = discover_one(primary, amm_id, address, height)
        right = discover_one(secondary, amm_id, address, height)
        for field in ("creation_tx", "deployment_block", "deployment_block_hash",
                      "runtime_code_bytes", "runtime_code_keccak256"):
            if left[field] != right[field]:
                raise PreflightError(f"{amm_id}: archive providers disagree on {field}")
        results.append(left)

    report = {
        "schema_version": 1,
        "status": "passed_deployment_metadata_discovery",
        "chain_id": CHAIN_ID,
        "finalized_checkpoint": {"number": height, "hash": block_a["hash"].lower()},
        "candidates": results,
        "candidate_manifest": [
            {key: item[key] for key in ("amm_id", "address", "creation_tx", "deployment_block")}
            for item in results
        ],
        "limitations": [
            "Metadata discovery only; factory interface qualification is a separate gate.",
            "Only top-level contract creation receipts are accepted; internal deployments fail closed.",
            "No factory is admitted to the production registry and trading remains disabled."
        ]
    }
    with open("base-v2-deployment-discovery.json", "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\\n")
    print(json.dumps({
        "status": report["status"],
        "finalized_block": height,
        "candidate_count": len(results),
        "report_file": "base-v2-deployment-discovery.json"
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (PreflightError, ValueError, KeyError, TypeError) as exc:
        print(f"BASE_V2_DEPLOYMENT_DISCOVERY_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
