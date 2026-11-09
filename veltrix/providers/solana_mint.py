"""Read-only finalized Solana mint-account and Token-2022 extension evidence."""
from __future__ import annotations

import base64
import hashlib
import math
import struct
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests
from solders.pubkey import Pubkey

from .base import ProviderError
from ..solana_meme.candidates import canonical_mint

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Token-2022 ExtensionType discriminants. Unknown IDs are preserved and cause
# an incomplete review, but a structurally valid TLV stream remains parseable.
MINT_EXTENSION_TYPES = {
    1: "transfer_fee_config",
    3: "mint_close_authority",
    4: "confidential_transfer_mint",
    6: "default_account_state",
    9: "non_transferable",
    10: "interest_bearing_config",
    12: "permanent_delegate",
    14: "transfer_hook",
    16: "confidential_transfer_fee_config",
    18: "metadata_pointer",
    19: "token_metadata",
    20: "group_pointer",
    21: "token_group",
    22: "group_member_pointer",
    23: "token_group_member",
    24: "confidential_mint_burn",
    25: "scaled_ui_amount",
    26: "pausable",
}
ACCOUNT_ONLY_EXTENSION_TYPES = {2, 5, 7, 8, 11, 13, 15, 17, 27}
ACCOUNT_TYPE_OFFSET = 165
TLV_OFFSET = 166
ACCOUNT_TYPE_MINT = 1


@dataclass(frozen=True)
class SolanaMintAuthoritySnapshot:
    mint: str
    provider: str
    evidence_id: str
    observed_at_ms: int
    finalized_slot: int
    token_program_id: str
    mint_authority: str | None
    freeze_authority: str | None
    supply_raw: int
    decimals: int
    extension_types: tuple[str, ...]
    unknown_extension_ids: tuple[int, ...]
    extension_tlv_structure_valid: bool
    account_data_sha256: str


class SolanaMintAuthorityProvider:
    """Fetch finalized mint account bytes; never submits transactions."""

    provider_id = "solana-finalized-mint-account"

    def __init__(
        self,
        rpc_url: str,
        *,
        timeout_seconds: float = 12.0,
        session: Any | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(rpc_url, str) or not rpc_url.strip():
            raise ValueError("Solana RPC URL is required")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        self.rpc_url = rpc_url
        self.timeout_seconds = float(timeout_seconds)
        self.session = session or requests.Session()
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    @staticmethod
    def _decode_option_pubkey(data: bytes, tag_offset: int, value_offset: int) -> str | None:
        tag = struct.unpack_from("<I", data, tag_offset)[0]
        raw = data[value_offset:value_offset + 32]
        if len(raw) != 32:
            raise ProviderError("mint optional authority field is truncated")
        if tag == 0:
            # SPL COption semantics are determined by the tag. The payload bytes
            # are not authoritative when the option is None and need not be zero.
            return None
        if tag == 1:
            try:
                return str(Pubkey.from_bytes(raw))
            except Exception as exc:
                raise ProviderError("mint authority public key is malformed") from exc
        raise ProviderError("mint authority option tag is invalid")

    @staticmethod
    def _parse_extensions(data: bytes) -> tuple[tuple[str, ...], tuple[int, ...], bool]:
        if len(data) == 82:
            return (), (), True
        if len(data) < TLV_OFFSET:
            raise ProviderError("Token-2022 extended mint account is shorter than its header")
        if any(data[82:ACCOUNT_TYPE_OFFSET]):
            raise ProviderError("Token-2022 mint padding contains non-zero bytes")
        if data[ACCOUNT_TYPE_OFFSET] != ACCOUNT_TYPE_MINT:
            raise ProviderError("Token-2022 account type is not Mint")

        offset = TLV_OFFSET
        seen: set[int] = set()
        names: list[str] = []
        unknown: list[int] = []
        while offset < len(data):
            remaining = len(data) - offset
            if remaining < 4:
                if all(byte == 0 for byte in data[offset:]):
                    break
                raise ProviderError("Token-2022 extension TLV header is truncated")

            extension_type, length = struct.unpack_from("<HH", data, offset)
            offset += 4
            if extension_type == 0:
                if length != 0 or any(data[offset:]):
                    raise ProviderError("Token-2022 extension terminator is malformed")
                break
            if extension_type in seen:
                raise ProviderError("Token-2022 mint contains a duplicate extension type")
            if extension_type in ACCOUNT_ONLY_EXTENSION_TYPES:
                raise ProviderError("Token-2022 mint contains an account-only extension")
            end = offset + length
            if end > len(data):
                raise ProviderError("Token-2022 extension payload is truncated")
            seen.add(extension_type)
            name = MINT_EXTENSION_TYPES.get(extension_type)
            if name is None:
                unknown.append(extension_type)
                names.append(f"unknown_{extension_type}")
            else:
                names.append(name)
            offset = end

        # Unknown IDs are a review gap, not a malformed TLV structure.
        return tuple(names), tuple(unknown), True

    @classmethod
    def parse_account(
        cls,
        mint: str,
        owner: str,
        account_data: bytes,
        *,
        finalized_slot: int,
        observed_at_ms: int,
    ) -> SolanaMintAuthoritySnapshot:
        canonical = canonical_mint(mint)
        if owner not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
            raise ProviderError("mint account is not owned by a supported SPL Token program")
        if not isinstance(account_data, bytes) or len(account_data) < 82:
            raise ProviderError("mint account data is shorter than the 82-byte base Mint layout")
        if owner == TOKEN_PROGRAM_ID and len(account_data) != 82:
            raise ProviderError("classic SPL Token mint must have exactly 82 bytes")
        if account_data[45] != 1:
            raise ProviderError("mint account is not initialized")
        if (
            isinstance(finalized_slot, bool) or not isinstance(finalized_slot, int)
            or finalized_slot < 0
        ):
            raise ProviderError("finalized slot is invalid")
        if (
            isinstance(observed_at_ms, bool) or not isinstance(observed_at_ms, int)
            or observed_at_ms <= 0
        ):
            raise ProviderError("mint evidence timestamp is invalid")

        mint_authority = cls._decode_option_pubkey(account_data, 0, 4)
        supply = struct.unpack_from("<Q", account_data, 36)[0]
        decimals = account_data[44]
        freeze_authority = cls._decode_option_pubkey(account_data, 46, 50)
        if decimals > 38:
            raise ProviderError("mint decimals exceed the supported range")

        if owner == TOKEN_PROGRAM_ID:
            extension_types, unknown_ids, parse_valid = (), (), True
        else:
            extension_types, unknown_ids, parse_valid = cls._parse_extensions(account_data)

        data_hash = hashlib.sha256(account_data).hexdigest()
        evidence_id = hashlib.sha256(
            f"{canonical}:{owner}:{finalized_slot}:{data_hash}".encode("utf-8")
        ).hexdigest()
        return SolanaMintAuthoritySnapshot(
            mint=canonical,
            provider=cls.provider_id,
            evidence_id=evidence_id,
            observed_at_ms=observed_at_ms,
            finalized_slot=finalized_slot,
            token_program_id=owner,
            mint_authority=mint_authority,
            freeze_authority=freeze_authority,
            supply_raw=supply,
            decimals=decimals,
            extension_types=extension_types,
            unknown_extension_ids=unknown_ids,
            extension_tlv_structure_valid=parse_valid,
            account_data_sha256=data_hash,
        )

    def fetch_snapshot(self, mint: str) -> SolanaMintAuthoritySnapshot:
        canonical = canonical_mint(mint)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [
                canonical,
                {"encoding": "base64", "commitment": "finalized"},
            ],
        }
        try:
            response = self.session.post(
                self.rpc_url, json=payload, timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise ProviderError(
                f"Solana finalized mint RPC request failed: {type(exc).__name__}"
            ) from exc

        if not isinstance(body, dict) or body.get("error") is not None:
            raise ProviderError("Solana finalized mint RPC returned an error response")
        result = body.get("result")
        context = result.get("context") if isinstance(result, dict) else None
        value = result.get("value") if isinstance(result, dict) else None
        slot = context.get("slot") if isinstance(context, dict) else None
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
            raise ProviderError("Solana finalized mint RPC returned an invalid slot")
        if not isinstance(value, dict):
            raise ProviderError("Solana mint account does not exist")
        owner = value.get("owner")
        if value.get("executable") is not False:
            raise ProviderError("mint account must be non-executable")
        encoded = value.get("data")
        if (
            not isinstance(encoded, list) or len(encoded) != 2
            or encoded[1] != "base64" or not isinstance(encoded[0], str)
        ):
            raise ProviderError("Solana mint account data encoding is invalid")
        try:
            raw = base64.b64decode(encoded[0], validate=True)
        except Exception as exc:
            raise ProviderError("Solana mint account base64 data is malformed") from exc
        observed = self.clock_ms()
        if isinstance(observed, bool) or not isinstance(observed, int) or observed <= 0:
            raise ProviderError("local evidence timestamp is invalid")
        return self.parse_account(
            canonical, owner, raw, finalized_slot=slot, observed_at_ms=observed,
        )
