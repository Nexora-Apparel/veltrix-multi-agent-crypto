"""Read-only qualification of provisional Base V2 factories against two archives.

The operator-supplied manifest must contain independently sourced creation
transaction hashes and deployment blocks. Missing metadata fails closed.
"""
import json
import os
import re
import sys

from eth_hash.auto import keccak
from veltrix.base_archive_preflight import Rpc, PreflightError, quantity

CHAIN_ID = 8453
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")


def selector(signature):
    return "0x" + keccak(signature.encode())[:4].hex()


def decode_uint(value, label):
    if not isinstance(value, str) or not value.startswith("0x"):
        raise PreflightError(f"{label}: malformed ABI result")
    raw = bytes.fromhex(value[2:])
    if len(raw) != 32:
        raise PreflightError(f"{label}: expected one ABI word")
    return int.from_bytes(raw, "big")


def decode_address(value, label):
    if not isinstance(value, str) or not value.startswith("0x"):
        raise PreflightError(f"{label}: malformed ABI result")
    raw = bytes.fromhex(value[2:])
    if len(raw) != 32 or raw[:12] != b"\x00" * 12:
        raise PreflightError(f"{label}: malformed ABI address")
    return "0x" + raw[12:].hex()


def call(rpc, to, signature, args, block):
    data = selector(signature) + "".join(args)
    return rpc.call("eth_call", [{"to": to, "data": data}, hex(block)])


def address_word(address):
    if not ADDRESS.fullmatch(address or ""):
        raise PreflightError("manifest contains malformed address")
    return address[2:].lower().rjust(64, "0")


def inspect(rpc, candidate, finalized):
    address = candidate["address"]
    tx = candidate["creation_tx"]
    deployment = candidate["deployment_block"]
    chain_id = quantity(rpc.call("eth_chainId", []), f"{rpc.label} chain id")
    if chain_id != CHAIN_ID:
        raise PreflightError(f"{rpc.label}: expected Base chain id 8453")

    receipt = rpc.call("eth_getTransactionReceipt", [tx])
    if not isinstance(receipt, dict) or quantity(receipt.get("status"), "receipt status") != 1:
        raise PreflightError(f"{rpc.label}: creation receipt missing or unsuccessful")
    if str(receipt.get("transactionHash", "")).lower() != tx.lower():
        raise PreflightError(f"{rpc.label}: receipt transaction hash mismatch")
    if str(receipt.get("contractAddress", "")).lower() != address.lower():
        raise PreflightError(f"{rpc.label}: receipt contract address mismatch")
    if quantity(receipt.get("blockNumber"), "receipt block") != deployment:
        raise PreflightError(f"{rpc.label}: receipt deployment block mismatch")

    dep_block = rpc.call("eth_getBlockByNumber", [hex(deployment), False])
    final_block = rpc.call("eth_getBlockByNumber", [hex(finalized), False])
    if not isinstance(dep_block, dict) or not dep_block.get("hash"):
        raise PreflightError(f"{rpc.label}: deployment block unavailable")
    if not isinstance(final_block, dict) or not final_block.get("hash"):
        raise PreflightError(f"{rpc.label}: finalized block unavailable")

    code = rpc.call("eth_getCode", [address, hex(finalized)])
    if not isinstance(code, str) or not code.startswith("0x") or len(code) <= 2:
        raise PreflightError(f"{rpc.label}: factory runtime code missing")
    code_hash = keccak(bytes.fromhex(code[2:])).hex()

    length = decode_uint(call(rpc, address, "allPairsLength()", [], finalized),
                         f"{rpc.label} allPairsLength")
    if length < 1:
        raise PreflightError(f"{rpc.label}: factory has no indexed pairs")

    pair0 = decode_address(call(
        rpc, address, "allPairs(uint256)", [f"{0:064x}"], finalized
    ), f"{rpc.label} allPairs(0)")
    token0 = decode_address(call(rpc, pair0, "token0()", [], finalized),
                            f"{rpc.label} pair token0")
    token1 = decode_address(call(rpc, pair0, "token1()", [], finalized),
                            f"{rpc.label} pair token1")
    pair_factory = decode_address(call(rpc, pair0, "factory()", [], finalized),
                                  f"{rpc.label} pair factory")
    mapped_pair = decode_address(call(
        rpc, address, "getPair(address,address)",
        [address_word(token0), address_word(token1)], finalized
    ), f"{rpc.label} getPair")

    if token0.lower() == token1.lower() or token0.lower() == "0x" + "00" * 20 or token1.lower() == "0x" + "00" * 20:
        raise PreflightError(f"{rpc.label}: invalid pair token addresses")
    if pair_factory.lower() != address.lower():
        raise PreflightError(f"{rpc.label}: pair factory() mismatch")
    if mapped_pair.lower() != pair0.lower():
        raise PreflightError(f"{rpc.label}: getPair does not map pair tokens to allPairs(0)")

    # Validate that the V2 PairCreated event signature is queryable at the
    # deployment block; full history/inventory is a later milestone.
    topic = "0x" + keccak(b"PairCreated(address,address,address,uint256)").hex()
    logs = rpc.call("eth_getLogs", [{
        "address": address, "fromBlock": hex(max(deployment, finalized - 9)),
        "toBlock": hex(finalized), "topics": [topic],
    }])
    if not isinstance(logs, list):
        raise PreflightError(f"{rpc.label}: PairCreated query returned malformed result")

    return {
        "provider": rpc.label,
        "chain_id": chain_id,
        "deployment_block_hash": dep_block["hash"].lower(),
        "finalized_block_hash": final_block["hash"].lower(),
        "runtime_code_bytes": len(bytes.fromhex(code[2:])),
        "runtime_code_keccak256": code_hash,
        "all_pairs_length": length,
        "sample_pair": pair0.lower(),
        "sample_token0": token0.lower(),
        "sample_token1": token1.lower(),
        "sample_pair_factory": pair_factory.lower(),
        "get_pair_matches_all_pairs": True,
        "pair_created_events_returned": len(logs),
    }


def main():
    raw = os.getenv("BASE_V2_CANDIDATE_MANIFEST", "")
    if not raw:
        raise PreflightError("BASE_V2_CANDIDATE_MANIFEST is required; no metadata will be guessed")
    try:
        candidates = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PreflightError("candidate manifest is not valid JSON") from exc
    expected = {"sushiswap-v2", "baseswap-v2", "quickswap-v2", "swapbased-v2"}
    if (not isinstance(candidates, list) or len(candidates) != 4
            or any(not isinstance(item, dict) for item in candidates)
            or {item.get("amm_id") for item in candidates} != expected):
        raise PreflightError("manifest must contain exactly the four named provisional candidates")
    for item in candidates:
        if (not isinstance(item.get("amm_id"), str)
                or not ADDRESS.fullmatch(item.get("address", ""))
                or not TX_HASH.fullmatch(item.get("creation_tx", ""))
                or not isinstance(item.get("deployment_block"), int)
                or item["deployment_block"] <= 0):
            raise PreflightError("candidate manifest has missing or malformed deployment metadata")

    primary = Rpc(os.getenv("BASE_LIQUIDITY_POOL_RPC_URL"), "primary/Alchemy")
    secondary = Rpc(os.getenv("SOLIDRPC_BASE_ARCHIVE_RPC_URL"), "candidate/SolidRPC")
    if primary.url == secondary.url:
        raise PreflightError("archive endpoints must be independent")
    head_a = primary.call("eth_getBlockByNumber", ["finalized", False])
    head_b = secondary.call("eth_getBlockByNumber", ["finalized", False])
    height = min(quantity(head_a.get("number"), "Alchemy finalized height"),
                 quantity(head_b.get("number"), "SolidRPC finalized height"))
    a = primary.call("eth_getBlockByNumber", [hex(height), False])
    b = secondary.call("eth_getBlockByNumber", [hex(height), False])
    if not a or not b or a.get("hash", "").lower() != b.get("hash", "").lower():
        raise PreflightError("providers disagree at common finalized checkpoint")

    reports = []
    for item in candidates:
        left = inspect(primary, item, height)
        right = inspect(secondary, item, height)
        for key in ("deployment_block_hash", "finalized_block_hash",
                    "runtime_code_bytes", "runtime_code_keccak256",
                    "all_pairs_length", "sample_pair", "sample_token0",
                    "sample_token1", "sample_pair_factory"):
            if left[key] != right[key]:
                raise PreflightError(f"{item['amm_id']}: providers disagree on {key}")
        reports.append({
            "amm_id": item["amm_id"], "address": item["address"],
            "creation_tx": item["creation_tx"],
            "deployment_block": item["deployment_block"],
            "status": "passed_read_only_interface_probe",
            "providers": [left, right],
        })

    result = {
        "schema_version": 1,
        "status": "passed_read_only_interface_probe",
        "finalized_checkpoint": {"number": height, "hash": a["hash"].lower()},
        "candidates": reports,
        "limitations": [
            "Does not certify complete PairCreated history or full allPairs inventory.",
            "Does not establish global Base V2 registry completeness.",
            "Does not enable trading or admit candidates into production registry."
        ],
    }
    with open("base-v2-candidate-qualification.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    print(json.dumps({"status": result["status"], "finalized_block": height,
                      "candidate_count": len(reports),
                      "report": "base-v2-candidate-qualification.json"}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (PreflightError, ValueError, KeyError, TypeError) as exc:
        print(f"BASE_V2_CANDIDATE_QUALIFICATION_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
