"""Complete read-only Base V2 PairCreated/allPairs reconciliation.

This deliberately fails closed if either archive cannot scan the entire event
history in bounded chunks or if any allPairs(index), pair token, factory, or
getPair relationship disagrees with the factory's emitted inventory.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

from eth_hash.auto import keccak
from veltrix.base_archive_preflight import CHAIN_ID, PreflightError, Rpc, quantity

ZERO_ADDRESS = "0x" + "00" * 20
PAIR_CREATED_TOPIC = "0x" + keccak(
    b"PairCreated(address,address,address,uint256)"
).hex()
DEFAULT_LOG_CHUNK_SPAN = 10_000
MAX_LOG_CHUNK_SPAN = 50_000
BATCH_SIZE = 50


def selector(signature):
    return "0x" + keccak(signature.encode())[:4].hex()


def word_uint(value):
    return f"{value:064x}"


def word_address(value):
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        raise PreflightError("malformed address for ABI encoding")
    try:
        raw = bytes.fromhex(value[2:])
    except ValueError as exc:
        raise PreflightError("malformed address for ABI encoding") from exc
    return raw.rjust(32, b"\x00").hex()


def decode_address_word(value, label):
    if not isinstance(value, str) or not value.startswith("0x"):
        raise PreflightError(f"{label}: malformed ABI result")
    try:
        raw = bytes.fromhex(value[2:])
    except ValueError as exc:
        raise PreflightError(f"{label}: invalid ABI hex") from exc
    if len(raw) != 32 or raw[:12] != b"\x00" * 12:
        raise PreflightError(f"{label}: non-canonical ABI address")
    address = "0x" + raw[12:].hex()
    if address == ZERO_ADDRESS:
        raise PreflightError(f"{label}: zero address")
    return address


def decode_pair_created(log, label="PairCreated"):
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) != 3:
        raise PreflightError(f"{label}: expected signature plus two indexed token topics")
    if str(topics[0]).lower() != PAIR_CREATED_TOPIC:
        raise PreflightError(f"{label}: wrong event signature")
    token0 = decode_address_word(topics[1], f"{label} token0")
    token1 = decode_address_word(topics[2], f"{label} token1")
    data = log.get("data")
    if not isinstance(data, str) or not data.startswith("0x"):
        raise PreflightError(f"{label}: malformed event data")
    try:
        raw = bytes.fromhex(data[2:])
    except ValueError as exc:
        raise PreflightError(f"{label}: invalid event data hex") from exc
    if len(raw) != 64 or raw[:12] != b"\x00" * 12:
        raise PreflightError(f"{label}: expected canonical pair address and uint256 index")
    pair = "0x" + raw[12:32].hex()
    index = int.from_bytes(raw[32:], "big")
    if pair == ZERO_ADDRESS or token0.lower() >= token1.lower():
        raise PreflightError(f"{label}: invalid pair address or unordered token addresses")
    if index < 1:
        raise PreflightError(f"{label}: pair index must be one-based and positive")
    return {"pair_index": index, "pair": pair, "token0": token0, "token1": token1}


def normalize_event_logs(rows, factory, start, end, label):
    if not isinstance(rows, list):
        raise PreflightError(f"{label}: eth_getLogs returned a non-list")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise PreflightError(f"{label}: malformed log row")
        if str(row.get("address", "")).lower() != factory.lower():
            raise PreflightError(f"{label}: log address does not match factory")
        topics = row.get("topics")
        if not isinstance(topics, list) or not topics or str(topics[0]).lower() != PAIR_CREATED_TOPIC:
            raise PreflightError(f"{label}: non-PairCreated log returned")
        block_number = quantity(row.get("blockNumber"), f"{label} log block")
        if block_number < start or block_number > end:
            raise PreflightError(f"{label}: log outside requested block interval")
        parsed = decode_pair_created(row, label)
        normalized.append({
            **parsed,
            "block_number": block_number,
            "block_hash": str(row.get("blockHash", "")).lower(),
            "transaction_hash": str(row.get("transactionHash", "")).lower(),
            "transaction_index": quantity(row.get("transactionIndex"), f"{label} tx index"),
            "log_index": quantity(row.get("logIndex"), f"{label} log index"),
        })
    normalized.sort(key=lambda x: (
        x["block_number"], x["transaction_index"], x["log_index"]
    ))
    return normalized


def validate_event_sequence(events, expected_count, label):
    indexes = [event["pair_index"] for event in events]
    if len(indexes) != expected_count:
        raise PreflightError(
            f"{label}: event count {len(indexes)} does not equal allPairsLength {expected_count}"
        )
    expected = list(range(1, expected_count + 1))
    if indexes != expected:
        raise PreflightError(f"{label}: PairCreated indices are missing, duplicated, or out of order")
    pairs = [event["pair"].lower() for event in events]
    if len(set(pairs)) != len(pairs):
        raise PreflightError(f"{label}: duplicate pair address in event history")


def scan_event_history(rpc, factory, deployment_block, finalized_block, label,
                       chunk_span=DEFAULT_LOG_CHUNK_SPAN):
    if chunk_span < 1 or chunk_span > MAX_LOG_CHUNK_SPAN:
        raise PreflightError(f"{label}: log chunk span must be 1-{MAX_LOG_CHUNK_SPAN}")
    all_events = []
    start = deployment_block
    queried_blocks = 0
    chunk_count = 0
    while start <= finalized_block:
        end = min(finalized_block, start + chunk_span - 1)
        rows = rpc.call("eth_getLogs", [{
            "address": factory,
            "fromBlock": hex(start),
            "toBlock": hex(end),
            "topics": [PAIR_CREATED_TOPIC],
        }])
        all_events.extend(normalize_event_logs(rows, factory, start, end, label))
        queried_blocks += end - start + 1
        chunk_count += 1
        start = end + 1
        if chunk_count % 100 == 0:
            print(
                f"BASE_V2_HISTORY_PROGRESS provider={rpc.label} factory={factory} "
                f"chunks={chunk_count} blocks={queried_blocks} events={len(all_events)}",
                flush=True,
            )
    all_events.sort(key=lambda x: (
        x["block_number"], x["transaction_index"], x["log_index"]
    ))
    return all_events, {
        "from_block": deployment_block,
        "to_block": finalized_block,
        "queried_block_count": queried_blocks,
        "chunk_span": chunk_span,
        "chunk_count": chunk_count,
        "event_count": len(all_events),
        "block_coverage_complete": queried_blocks == finalized_block - deployment_block + 1,
    }


def batch_rpc(rpc, requests, label):
    """Execute a bounded JSON-RPC batch and require one valid result per ID."""
    if not requests:
        return []
    results = {}
    for offset in range(0, len(requests), BATCH_SIZE):
        group = requests[offset:offset + BATCH_SIZE]
        payload = [
            {"jsonrpc": "2.0", "id": offset + i + 1, "method": method, "params": params}
            for i, (method, params) in enumerate(group)
        ]
        body = json.dumps(payload).encode()
        response_data = None
        for attempt in range(4):
            if hasattr(rpc, "_last_request_at"):
                elapsed = time.monotonic() - rpc._last_request_at
                if elapsed < 0.25:
                    time.sleep(0.25 - elapsed)
            request = urllib.request.Request(
                rpc.url, data=body, headers={"Content-Type": "application/json"}, method="POST"
            )
            rpc._last_request_at = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    response_data = json.loads(response.read(8_000_000).decode("utf-8", "replace"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 502, 503, 504) and attempt < 3:
                    time.sleep(min(2 ** (attempt + 1), 15))
                    continue
                raise PreflightError(f"{label}: JSON-RPC batch HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if attempt < 3:
                    time.sleep(min(2 ** (attempt + 1), 15))
                    continue
                raise PreflightError(f"{label}: JSON-RPC batch transport/JSON failure") from exc
        if not isinstance(response_data, list):
            raise PreflightError(f"{label}: provider does not return JSON-RPC batch arrays")
        for item in response_data:
            if not isinstance(item, dict) or "id" not in item:
                raise PreflightError(f"{label}: malformed JSON-RPC batch response")
            if "error" in item:
                error = item.get("error", {})
                code = error.get("code") if isinstance(error, dict) else None
                message = error.get("message", "RPC batch error") if isinstance(error, dict) else "RPC batch error"
                raise PreflightError(f"{label}: batch RPC error code={code}: {str(message)[:200]}")
            if "result" not in item:
                raise PreflightError(f"{label}: batch response missing result")
            results[item["id"]] = item["result"]
    expected_ids = set(range(1, len(requests) + 1))
    if set(results) != expected_ids:
        raise PreflightError(f"{label}: JSON-RPC batch response omitted or duplicated IDs")
    return [results[i] for i in range(1, len(requests) + 1)]


def eth_call_requests(to, data, block):
    return [("eth_call", [{"to": to, "data": data}, hex(block)])]


def verify_full_inventory(rpc, factory, events, finalized, label):
    """Compare every allPairs(index), token0/1, factory(), and getPair result."""
    requests = []
    descriptors = []
    for event in events:
        index = event["pair_index"] - 1
        requests.extend([
            ("eth_call", [{"to": factory, "data": selector("allPairs(uint256)") + word_uint(index)}, hex(finalized)]),
            ("eth_call", [{"to": event["pair"], "data": selector("token0()")}, hex(finalized)]),
            ("eth_call", [{"to": event["pair"], "data": selector("token1()")}, hex(finalized)]),
        ])
        descriptors.append(event)
    values = batch_rpc(rpc, requests, label)
    inventory_rows = []
    for i, event in enumerate(descriptors):
        base = i * 3
        pair_at_index = decode_address_word(values[base], f"{label} allPairs({i})")
        token0 = decode_address_word(values[base + 1], f"{label} pair token0")
        token1 = decode_address_word(values[base + 2], f"{label} pair token1")
        expected = event["pair"].lower()
        if pair_at_index.lower() != expected:
            raise PreflightError(f"{label}: allPairs({i}) does not match PairCreated event")
        if token0.lower() != event["token0"].lower() or token1.lower() != event["token1"].lower():
            raise PreflightError(f"{label}: pair token0/token1 disagree with PairCreated event at index {i}")
        inventory_rows.append({
            "index": i,
            "pair": expected,
            "token0": token0.lower(),
            "token1": token1.lower(),
        })
        if (i + 1) % 1000 == 0:
            print(
                f"BASE_V2_INVENTORY_PROGRESS provider={rpc.label} factory={factory} "
                f"verified={i + 1}/{len(descriptors)}",
                flush=True,
            )
    encoded = json.dumps(inventory_rows, sort_keys=True, separators=(",", ":")).encode()
    return {
        "pair_count": len(inventory_rows),
        "inventory_keccak256": keccak(encoded).hex(),
        "all_pairs_index_coverage": len(inventory_rows) == len(events),
        "relationship_checks_per_pair": [
            "allPairs(index)", "token0()", "token1()"
        ],
    }


def inspect_candidate(rpc, candidate, finalized, finalized_hash, events, coverage):
    chain_id = quantity(rpc.call("eth_chainId", []), f"{rpc.label} chain ID")
    if chain_id != CHAIN_ID:
        raise PreflightError(f"{rpc.label}: expected Base chain ID 8453")
    factory = candidate["address"]
    receipt = rpc.call("eth_getTransactionReceipt", [candidate["creation_tx"]])
    if not isinstance(receipt, dict) or quantity(receipt.get("status"), "receipt status") != 1:
        raise PreflightError(f"{rpc.label}: candidate creation receipt missing or unsuccessful")
    if str(receipt.get("transactionHash", "")).lower() != candidate["creation_tx"].lower():
        raise PreflightError(f"{rpc.label}: creation transaction hash mismatch")
    if str(receipt.get("contractAddress", "")).lower() != factory.lower():
        raise PreflightError(f"{rpc.label}: creation contract address mismatch")
    if quantity(receipt.get("blockNumber"), "receipt block") != candidate["deployment_block"]:
        raise PreflightError(f"{rpc.label}: deployment block mismatch")
    deployment = rpc.call("eth_getBlockByNumber", [hex(candidate["deployment_block"]), False])
    if not isinstance(deployment, dict) or str(deployment.get("hash", "")).lower() != str(receipt.get("blockHash", "")).lower():
        raise PreflightError(f"{rpc.label}: receipt/deployment block hash mismatch")
    finalized_block = rpc.call("eth_getBlockByNumber", [hex(finalized), False])
    if not isinstance(finalized_block, dict) or str(finalized_block.get("hash", "")).lower() != finalized_hash:
        raise PreflightError(f"{rpc.label}: finalized checkpoint hash mismatch")
    code = rpc.call("eth_getCode", [factory, hex(finalized)])
    if not isinstance(code, str) or not code.startswith("0x") or len(code) <= 2:
        raise PreflightError(f"{rpc.label}: factory runtime code missing")
    code_hash = keccak(bytes.fromhex(code[2:])).hex()
    initial_count = quantity(
        rpc.call("eth_call", [{"to": factory, "data": selector("allPairsLength()")}, hex(candidate["deployment_block"])]),
        f"{rpc.label} allPairsLength at deployment",
    )
    if initial_count != 0:
        raise PreflightError(f"{rpc.label}: factory inventory was non-empty at its deployment block")
    length = quantity(
        rpc.call("eth_call", [{"to": factory, "data": selector("allPairsLength()")}, hex(finalized)]),
        f"{rpc.label} allPairsLength",
    )
    validate_event_sequence(events, length, f"{rpc.label}/{candidate['amm_id']}")
    inventory = verify_full_inventory(
        rpc, factory, events, finalized, f"{rpc.label}/{candidate['amm_id']}"
    )
    event_rows = [{
        "pair_index": e["pair_index"], "pair": e["pair"].lower(),
        "token0": e["token0"].lower(), "token1": e["token1"].lower(),
        "block_number": e["block_number"], "block_hash": e["block_hash"],
        "transaction_hash": e["transaction_hash"], "transaction_index": e["transaction_index"],
        "log_index": e["log_index"],
    } for e in events]
    event_bytes = json.dumps(event_rows, sort_keys=True, separators=(",", ":")).encode()
    return {
        "provider": rpc.label,
        "chain_id": CHAIN_ID,
        "deployment_block_hash": deployment["hash"].lower(),
        "runtime_code_bytes": len(bytes.fromhex(code[2:])),
        "runtime_code_keccak256": code_hash,
        "all_pairs_length_at_deployment": initial_count,
        "all_pairs_length": length,
        "event_coverage": coverage,
        "event_history_keccak256": keccak(event_bytes).hex(),
        "inventory": inventory,
    }


def main():
    raw = os.getenv("BASE_V2_CANDIDATE_MANIFEST", "")
    if not raw:
        raise PreflightError("BASE_V2_CANDIDATE_MANIFEST is required")
    candidates = json.loads(raw)
    if not isinstance(candidates, list) or len(candidates) != 4:
        raise PreflightError("manifest must contain exactly four candidate factories")
    required = {"sushiswap-v2", "baseswap-v2", "quickswap-v2", "swapbased-v2"}
    if {item.get("amm_id") for item in candidates if isinstance(item, dict)} != required:
        raise PreflightError("manifest does not contain the four expected candidate IDs")
    for item in candidates:
        if (not isinstance(item, dict) or not isinstance(item.get("address"), str)
                or len(item["address"]) != 42 or not item["address"].startswith("0x")
                or not isinstance(item.get("creation_tx"), str) or len(item["creation_tx"]) != 66
                or not item["creation_tx"].startswith("0x")
                or not isinstance(item.get("deployment_block"), int)
                or item["deployment_block"] <= 0):
            raise PreflightError("malformed candidate deployment metadata")

    primary = Rpc(os.getenv("BASE_LIQUIDITY_POOL_RPC_URL"), "state/Alchemy")
    secondary = Rpc(os.getenv("SOLIDRPC_BASE_ARCHIVE_RPC_URL"), "events/SolidRPC")
    publicnode = Rpc(os.getenv("BASE_PUBLIC_LOGS_RPC_URL", "https://base-rpc.publicnode.com"),
                     "state-and-events/PublicNode")
    if len({primary.url, secondary.url, publicnode.url}) != 3:
        raise PreflightError("Alchemy, SolidRPC, and PublicNode endpoints must be independent")

    provider_heads = {}
    for rpc in (primary, secondary, publicnode):
        head = rpc.call("eth_getBlockByNumber", ["finalized", False])
        if not isinstance(head, dict):
            raise PreflightError(f"{rpc.label}: finalized head unavailable")
        provider_heads[rpc.label] = quantity(head.get("number"), f"{rpc.label} finalized height")
    height = min(provider_heads.values())
    checkpoint_blocks = {
        rpc.label: rpc.call("eth_getBlockByNumber", [hex(height), False])
        for rpc in (primary, secondary, publicnode)
    }
    hashes = {
        label: str(block.get("hash", "")).lower() if isinstance(block, dict) else ""
        for label, block in checkpoint_blocks.items()
    }
    if not hashes or len(set(hashes.values())) != 1 or not next(iter(hashes.values())):
        raise PreflightError("providers disagree at common finalized checkpoint")
    checkpoint_hash = next(iter(hashes.values()))

    solid_span = int(os.getenv("BASE_V2_SOLIDRPC_LOG_CHUNK_SPAN", "30000"))
    public_span = int(os.getenv("BASE_V2_PUBLICNODE_LOG_CHUNK_SPAN", "50000"))
    if not 1 <= solid_span <= 30000:
        raise PreflightError("SolidRPC log chunk span must be 1-30000")
    if not 1 <= public_span <= 50000:
        raise PreflightError("PublicNode log chunk span must be 1-50000")

    reports = []
    for candidate in candidates:
        solid_events, solid_coverage = scan_event_history(
            secondary, candidate["address"], candidate["deployment_block"], height,
            f"SolidRPC/{candidate['amm_id']}", solid_span,
        )
        public_events, public_coverage = scan_event_history(
            publicnode, candidate["address"], candidate["deployment_block"], height,
            f"PublicNode/{candidate['amm_id']}", public_span,
        )
        solid_event_digest = keccak(json.dumps(solid_events, sort_keys=True, separators=(",", ":")).encode()).hex()
        public_event_digest = keccak(json.dumps(public_events, sort_keys=True, separators=(",", ":")).encode()).hex()
        if solid_event_digest != public_event_digest:
            raise PreflightError(f"{candidate['amm_id']}: SolidRPC and PublicNode event histories disagree")
        if len(solid_events) != len(public_events):
            raise PreflightError(f"{candidate['amm_id']}: independent event providers returned different counts")

        left = inspect_candidate(primary, candidate, height, checkpoint_hash,
                                 solid_events, solid_coverage)
        right = inspect_candidate(publicnode, candidate, height, checkpoint_hash,
                                  public_events, public_coverage)
        for field in ("chain_id", "deployment_block_hash", "runtime_code_bytes", "runtime_code_keccak256",
                      "all_pairs_length_at_deployment", "all_pairs_length",
                      "event_history_keccak256"):
            if left[field] != right[field]:
                raise PreflightError(f"{candidate['amm_id']}: state providers disagree on {field}")
        if left["inventory"] != right["inventory"]:
            raise PreflightError(f"{candidate['amm_id']}: providers disagree on complete inventory")
        reports.append({
            "amm_id": candidate["amm_id"], "address": candidate["address"],
            "deployment_block": candidate["deployment_block"],
            "status": "passed_complete_event_and_inventory_reconciliation",
            "event_providers": [
                {"provider": secondary.label, "coverage": solid_coverage, "event_digest": solid_event_digest},
                {"provider": publicnode.label, "coverage": public_coverage, "event_digest": public_event_digest},
            ],
            "state_providers": [left, right],
        })

    result = {
        "schema_version": 1,
        "status": "passed_complete_event_and_inventory_reconciliation",
        "chain_id": CHAIN_ID,
        "finalized_checkpoint": {"number": height, "hash": checkpoint_hash},
        "provider_finalized_heads": provider_heads,
        "candidates": reports,
        "limitations": [
            "Certifies only the four explicitly listed candidate factories.",
            "Does not establish that every Base V2 AMM factory has been discovered.",
            "Does not admit factories to the production registry or enable trading.",
        ],
    }
    with open("base-v2-history-inventory-acceptance.json", "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps({
        "status": result["status"], "finalized_block": height,
        "candidate_count": len(reports),
        "event_sources": ["SolidRPC", "PublicNode"],
        "inventory_state_sources": ["Alchemy", "PublicNode"],
        "report": "base-v2-history-inventory-acceptance.json",
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (PreflightError, ValueError, KeyError, TypeError) as exc:
        print(f"BASE_V2_HISTORY_INVENTORY_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
