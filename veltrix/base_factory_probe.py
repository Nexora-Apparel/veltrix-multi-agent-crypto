"""Read-only Base factory metadata probe using two independent RPC endpoints.

Confirms the creation receipt, deployment block, finalized block agreement,
runtime bytecode equality/Keccak hash, and the Uniswap V2 allPairsLength view.
Never prints RPC URLs or credentials.
"""
import argparse
import json
import os
import sys
from urllib.parse import urlparse

import requests
from eth_hash.auto import keccak


FACTORY_DEFAULT = "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"
TX_DEFAULT = "0x3c94031f81d9afe3beeb8fbcf4dcf1bd5b5688b86081d94e3d0231514dc00d31"
DEPLOYMENT_BLOCK_DEFAULT = 6601915
CHAIN_ID = 8453
ALL_PAIRS_LENGTH_SELECTOR = "0x574f2ba3"


class ProbeError(RuntimeError):
    pass


class Rpc:
    def __init__(self, url, label):
        parsed = urlparse(url or "")
        if parsed.scheme != "https" or not parsed.netloc:
            raise ProbeError(f"{label} endpoint is missing or is not HTTPS")
        self.url = url
        self.label = label
        self.session = requests.Session()
        self.counter = 0

    def call(self, method, params):
        self.counter += 1
        try:
            response = self.session.post(
                self.url,
                json={"jsonrpc": "2.0", "id": self.counter,
                      "method": method, "params": params},
                timeout=25,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as exc:
            response = exc.response
            status = response.status_code if response is not None else "unknown"
            # Emit actionable HTTP diagnostics without exposing the RPC URL,
            # authorization headers, cookies, or endpoint credentials.
            detail = {
                "provider": self.label,
                "method": method,
                "params": params,
                "http_status": status,
            }
            if response is not None:
                body = (response.text or "").strip()
                if body:
                    import re
                    body = re.sub(r"https?://[^\\s\\\"'<>]+", "[REDACTED_URL]", body)
                    body = re.sub(
                        r"(?i)(token|api[_-]?key|authorization|password|secret)(\\s*[=:]\\s*)[^,\\s\\\"']+",
                        r"\\1\\2[REDACTED]",
                        body,
                    )
                    detail["response_body"] = body[:600]
                safe_headers = ("content-type", "server", "x-request-id", "x-correlation-id", "cf-ray")
                detail["response_headers"] = {
                    key: response.headers[key]
                    for key in safe_headers
                    if key in response.headers
                }
            print("BASE_RPC_HTTP_DIAGNOSTIC: " + json.dumps(detail, sort_keys=True), file=sys.stderr)
            raise ProbeError(
                f"{self.label}: {method} params={json.dumps(params)} HTTP {status}"
            ) from exc
        except requests.Timeout as exc:
            raise ProbeError(f"{self.label}: {method} timed out after 25 seconds") from exc
        except requests.ConnectionError as exc:
            raise ProbeError(f"{self.label}: {method} connection failed ({type(exc).__name__})") from exc
        except requests.RequestException as exc:
            raise ProbeError(f"{self.label}: {method} transport failed ({type(exc).__name__})") from exc
        except ValueError as exc:
            raise ProbeError(f"{self.label}: {method} returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
            raise ProbeError(f"{self.label}: {method} returned an RPC error")
        return payload["result"]


def quantity(value, label):
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ProbeError(f"{label} is malformed")
    try:
        return int(value, 16)
    except ValueError as exc:
        raise ProbeError(f"{label} is malformed") from exc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--factory", default=FACTORY_DEFAULT)
    parser.add_argument("--creation-tx", default=TX_DEFAULT)
    parser.add_argument("--deployment-block", type=int, default=DEPLOYMENT_BLOCK_DEFAULT)
    parser.add_argument("--output", default="base-factory-probe.json")
    args = parser.parse_args()

    if len(args.factory) != 42 or not args.factory.startswith("0x"):
        raise ProbeError("factory address is malformed")
    if len(args.creation_tx) != 66 or not args.creation_tx.startswith("0x"):
        raise ProbeError("creation transaction hash is malformed")
    if args.deployment_block < 0:
        raise ProbeError("deployment block cannot be negative")

    pool = Rpc(os.getenv("BASE_LIQUIDITY_POOL_RPC_URL"), "pool/Alchemy")
    locker = Rpc(os.getenv("BASE_LIQUIDITY_LOCKER_RPC_URL"), "locker/Chainstack")

    # First validate both independent providers and obtain the finalized anchor
    # from the primary Alchemy endpoint. Chainstack may reject the symbolic
    # safe/finalized tags with HTTP 403 while still serving explicit block reads.
    contexts = []
    for rpc in (pool, locker):
        if quantity(rpc.call("eth_chainId", []), f"{rpc.label} chain ID") != CHAIN_ID:
            raise ProbeError(f"{rpc.label}: endpoint is not Base Mainnet")
        # Capture a simple head-read capability before receipt/tag checks. If
        # this passes but a later block lookup is denied, the failure is
        # method/parameter-specific rather than total endpoint inaccessibility.
        rpc.call("eth_blockNumber", [])
        receipt = rpc.call("eth_getTransactionReceipt", [args.creation_tx])
        if not isinstance(receipt, dict) or receipt.get("status") != "0x1":
            raise ProbeError(f"{rpc.label}: factory creation transaction is not successful")
        if (receipt.get("contractAddress", "").lower() != args.factory.lower()
                or quantity(receipt.get("blockNumber"), "receipt block") != args.deployment_block):
            raise ProbeError(f"{rpc.label}: creation receipt does not match factory/deployment block")

        tag_results = {}
        finalized = None
        for tag in ("latest", "safe", "finalized"):
            try:
                block = rpc.call("eth_getBlockByNumber", [tag, False])
                if not isinstance(block, dict) or not block.get("hash"):
                    raise ProbeError(f"{rpc.label}: {tag} returned no block/hash")
                tag_results[tag] = {
                    "status": "ok",
                    "number": quantity(block.get("number"), f"{rpc.label} {tag} block number"),
                    "hash": block["hash"],
                }
                if tag == "finalized":
                    finalized = block
            except ProbeError as exc:
                tag_results[tag] = {"status": "error", "error": str(exc)}
        contexts.append({"rpc": rpc, "tag_results": tag_results, "finalized": finalized})

    # Finality authority is the primary independent provider. Require its
    # finalized tag; never downgrade the acceptance anchor to latest or safe.
    anchor = contexts[0]["finalized"]
    if anchor is None:
        compact = "; ".join(
            f"{tag}={item['status']}" + (f" ({item['error']})" if item["status"] == "error" else "")
            for tag, item in contexts[0]["tag_results"].items()
        )
        raise ProbeError(f"{pool.label}: finalized anchor unavailable; tag diagnostics: {compact}")
    final_number = quantity(anchor.get("number"), "primary finalized block number")
    if final_number < args.deployment_block:
        raise ProbeError(f"{pool.label}: finalized head precedes factory deployment")

    reports = []
    for context in contexts:
        rpc = context["rpc"]
        tag_results = context["tag_results"]
        # Pin BOTH providers to the exact block selected by Alchemy's finalized
        # tag. This permits an explicit-height read on Chainstack when its
        # symbolic safe/finalized tags are access-denied, without weakening
        # finality or accepting a different block.
        pinned = rpc.call("eth_getBlockByNumber", [hex(final_number), False])
        if not isinstance(pinned, dict) or not pinned.get("hash"):
            raise ProbeError(f"{rpc.label}: cannot retrieve finalized anchor block {final_number}")
        if pinned["hash"].lower() != anchor["hash"].lower():
            raise ProbeError(f"{rpc.label}: block hash disagrees with primary finalized anchor")
        code = rpc.call("eth_getCode", [args.factory, hex(final_number)])
        if not isinstance(code, str) or not code.startswith("0x") or len(code) <= 2:
            raise ProbeError(f"{rpc.label}: factory runtime bytecode is empty")
        try:
            raw_code = bytes.fromhex(code[2:])
        except ValueError as exc:
            raise ProbeError(f"{rpc.label}: factory runtime bytecode is malformed") from exc
        code_hash = keccak(raw_code).hex()
        length_result = rpc.call(
            "eth_call",
            [{"to": args.factory, "data": ALL_PAIRS_LENGTH_SELECTOR}, hex(final_number)],
        )
        pair_count = quantity(length_result, f"{rpc.label} allPairsLength")
        reports.append({
            "provider": rpc.label,
            "chain_id": CHAIN_ID,
            "block_tag_checks": tag_results,
            "creation_tx": args.creation_tx,
            "deployment_block": args.deployment_block,
            "finalized_block": final_number,
            "finalized_block_hash": pinned["hash"],
            "finality_anchor_provider": pool.label,
            "runtime_code_bytes": len(raw_code),
            "runtime_code_hash": code_hash,
            "all_pairs_length": pair_count,
        })

    if reports[0]["finalized_block_hash"].lower() != reports[1]["finalized_block_hash"].lower():
        raise ProbeError("independent RPC endpoints disagree on finalized block hash")
    if reports[0]["runtime_code_hash"] != reports[1]["runtime_code_hash"]:
        raise ProbeError("independent RPC endpoints disagree on factory runtime code hash")
    if reports[0]["all_pairs_length"] != reports[1]["all_pairs_length"]:
        raise ProbeError("independent RPC endpoints disagree on allPairsLength")

    result = {
        "status": "passed",
        "factory": args.factory,
        "deployment_block": args.deployment_block,
        "expected_code_hash": reports[0]["runtime_code_hash"],
        "all_pairs_length": reports[0]["all_pairs_length"],
        "finalized_block": reports[0]["finalized_block"],
        "finalized_block_hash": reports[0]["finalized_block_hash"],
        "providers": reports,
        "registry_entry_candidate": {
            "amm_id": "uniswap-v2",
            "address": args.factory,
            "deployment_block": args.deployment_block,
            "expected_code_hash": reports[0]["runtime_code_hash"],
        },
        "scope_note": "This verifies one factory only; it does not certify complete Base V2 AMM coverage.",
    }
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps({
        "status": result["status"],
        "factory": result["factory"],
        "deployment_block": result["deployment_block"],
        "expected_code_hash": result["expected_code_hash"],
        "all_pairs_length": result["all_pairs_length"],
        "finalized_block": result["finalized_block"],
        "report_file": args.output,
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except ProbeError as exc:
        print(f"BASE_FACTORY_PROBE_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
