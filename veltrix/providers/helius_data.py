"""Normalization helpers for Helius Parsed Events Scan response records."""
import math


def unwrap_parsed_transaction(record):
    """Accept the documented {signature, parserStatus, parsed} envelope."""
    if not isinstance(record, dict):
        return None
    if "parsed" not in record:
        return record
    if record.get("parserStatus") != "OK" or not isinstance(record.get("parsed"), dict):
        return None
    parsed = dict(record["parsed"])
    if not parsed.get("signature"):
        parsed["signature"] = record.get("signature")
    return parsed


def token_transfer_amount(transfer):
    """Return UI token amount from parsed Helius raw integer fields."""
    if not isinstance(transfer, dict):
        raise ValueError("token transfer must be an object")
    if transfer.get("tokenAmount") is not None:
        amount = float(transfer["tokenAmount"])
    else:
        raw = transfer.get("rawTokenAmount")
        decimals = transfer.get("decimals")
        if isinstance(raw, dict):
            decimals = raw.get("decimals", decimals)
            raw = raw.get("tokenAmount")
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise ValueError("raw token amount is missing")
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 36:
            raise ValueError("token decimals are missing or invalid")
        amount = int(raw) / (10 ** decimals)
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("token amount must be finite and positive")
    return amount
