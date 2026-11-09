"""Read-only Base archive capability preflight.

Compares Alchemy with SolidRPC at the factory deployment block. This proves
historical block retrieval, historical eth_getCode, and historical eth_call
without changing the production factory probe or certifying the provider.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from eth_hash.auto import keccak

CHAIN_ID = 8453
FACTORY_DEFAULT = "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"
TX_DEFAULT = "0x3c94031f81d9afe3beeb8fbcf4dcf1bd5b5688b86081d94e3d0231514dc00d31"
DEPLOYMENT_BLOCK_DEFAULT = 6601915
ALL_PAIRS_LENGTH_SELECTOR = "0x574f2ba3"


class PreflightError(RuntimeError):
    pass


def redact(value):
    value = re.sub(r"https?://[^\s\"'<>]+", "[REDACTED_URL]", value)
    value = re.sub(
        r"(?i)(token|api[_-]?key|authorization|password|secret)(\s*[=:]\s*)[^,\s\"']+",
        r"\1\2[REDACTED]",
        value,
    )
    return value[:700]


class Rpc:
    def __init__(self, url, label):
        parsed = urllib.parse.urlparse(url or "")
        if parsed.scheme != "https" or not parsed.netloc:
            raise PreflightError(f"{label}: endpoint missing or not HTTPS")
        self.url = url
        self.label = label
        self.request_id = 0

    def call(self, method, params):
        self.request_id += 1
        body = json.dumps({
            "jsonrpc": "2.0", "id": self.request_id,
            "method": method, "params": params,
        }).encode()
        request = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(2_000_000).decode("utf-8", "replace")
                payload = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read(2000).decode("utf-8", "replace")
            detail = {
                "provider": self.label, "method": method,
                "params": params, "http_status": exc.code,
                "response_body": redact(raw),
            }
            print("BASE_ARCHIVE_HTTP_DIAGNOSTIC: " + json.dumps(detail), file=sys.stderr)
            raise PreflightError(
                f"{self.label}: {method} params={json.dumps(params)} HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PreflightError(
                f"{self.label}: {method} transport failure ({type(exc).__name__})"
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PreflightError(f"{self.label}: {method} returned invalid JSON") from exc
        if not isinstance(payload, dict) or "error" in payload or "result" not in payload:
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            message = redact(str(error.get("message", "malformed JSON-RPC response")))
            raise PreflightError(f"{self.label}: {method} RPC error: {message}")
        return payload["result"]


def quantity(value, label):
    if not isinstance(value, str) or not value.startswith("0x"):
        raise PreflightError(f"{label}: malformed hex quantity")
    try:
        return int(value, 16)
    except ValueError as exc:
        raise PreflightError(f"{label}: malformed hex quantity") from exc


def run_provider(rpc, factory, creation_tx, deployment_block):
    chain_id = quantity(rpc.call("eth_chainId", []), f"{rpc.label} chain ID")
    if chain_id != CHAIN_ID:
        raise PreflightError(f"{rpc.label}: expected Base chain ID 8453, received {chain_id}")

    receipt = rpc.call("eth_getTransactionReceipt", [creation_tx])
    if not isinstance(receipt, dict) or receipt.get("status") != "0x1":
        raise PreflightError(f"{rpc.label}: creation receipt missing or unsuccessful")
    if receipt.get("contractAddress", "").lower() != factory.lower():
        raise PreflightError(f"{rpc.label}: creation receipt contract address mismatch")
    if quantity(receipt.get("blockNumber"), f"{rpc.label} receipt block") != deployment_block:
        raise PreflightError(f"{rpc.label}: creation receipt block mismatch")

    block = rpc.call("eth_getBlockByNumber", [hex(deployment_block), False])
    if not isinstance(block, dict) or not block.get("hash"):
        raise PreflightError(f"{rpc.label}: historical deployment block unavailable")

    code = rpc.call("eth_getCode", [factory, hex(deployment_block)])
    if not isinstance(code, str) or not code.startswith("0x") or len(code) <= 2:
        raise PreflightError(f"{rpc.label}: historical factory bytecode unavailable")
    try:
        code_bytes = bytes.fromhex(code[2:])
    except ValueError as exc:
        raise PreflightError(f"{rpc.label}: historical factory bytecode malformed") from exc

    pair_count = quantity(
        rpc.call("eth_call", [
            {"to": factory, "data": ALL_PAIRS_LENGTH_SELECTOR},
            hex(deployment_block),
        ]),
        f"{rpc.label} historical allPairsLength",
    )

    return {
        "provider": rpc.label,
        "chain_id": chain_id,
        "deployment_block": deployment_block,
        "deployment_block_hash": block["hash"],
        "runtime_code_bytes": len(code_bytes),
        "runtime_code_keccak256": keccak(code_bytes).hex(),
        "all_pairs_length_at_deployment": pair_count,
        "checks": {
            "chain_id": "passed",
            "creation_receipt": "passed",
            "historical_block": "passed",
            "historical_eth_getCode": "passed",
            "historical_eth_call": "passed",
        },
    }


def main():
    factory = os.getenv("BASE_FACTORY_ADDRESS", FACTORY_DEFAULT)
    creation_tx = os.getenv("BASE_FACTORY_CREATION_TX", TX_DEFAULT)
    deployment_block = int(os.getenv("BASE_FACTORY_DEPLOYMENT_BLOCK", str(DEPLOYMENT_BLOCK_DEFAULT)))
    primary = Rpc(os.getenv("BASE_LIQUIDITY_POOL_RPC_URL"), "primary/Alchemy")
    candidate = Rpc(os.getenv("SOLIDRPC_BASE_ARCHIVE_RPC_URL"), "candidate/SolidRPC")

    first = run_provider(primary, factory, creation_tx, deployment_block)
    second = run_provider(candidate, factory, creation_tx, deployment_block)

    if first["deployment_block_hash"].lower() != second["deployment_block_hash"].lower():
        raise PreflightError("providers disagree on deployment block hash")
    if first["runtime_code_bytes"] != second["runtime_code_bytes"]:
        raise PreflightError("providers disagree on historical runtime bytecode length")
    if first["all_pairs_length_at_deployment"] != second["all_pairs_length_at_deployment"]:
        raise PreflightError("providers disagree on historical allPairsLength")

    if first["runtime_code_keccak256"] != second["runtime_code_keccak256"]:
        raise PreflightError("providers disagree on historical runtime bytecode hash")

    result = {
        "status": "passed",
        "purpose": "archive capability preflight only; does not certify the provider",
        "factory": factory,
        "creation_tx": creation_tx,
        "deployment_block": deployment_block,
        "providers": [first, second],
        "comparison": "matching historical block hash, bytecode hash, and allPairsLength",
    }
    with open("base-archive-preflight.json", "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps({
        "status": result["status"],
        "deployment_block": deployment_block,
        "providers": [p["provider"] for p in result["providers"]],
        "historical_checks": "passed",
        "report_file": "base-archive-preflight.json",
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (PreflightError, ValueError) as exc:
        print(f"BASE_ARCHIVE_PREFLIGHT_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
