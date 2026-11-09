"""Claim-separated Solana risk evidence collection.

This module gathers GoPlus token-security and native holder-inventory evidence.
It deliberately does not produce a safe/unsafe verdict: developer linkage,
bundled-supply provenance, mint/freeze authority, Token-2022 extension analysis
and executable sellability remain separate required claims.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable

from ..developer_concentration import HolderSnapshot
from ..providers.base import ProviderError
from ..providers.goplus import GoPlusTokenSafetyProvider
from ..providers.holder_inventory import SolanaHolderInventoryProvider
from ..providers.intelligence import TokenSafetyObservation
from ..providers.solana_mint import (
    SolanaMintAuthorityProvider, SolanaMintAuthoritySnapshot,
    TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID,
)
from .candidates import canonical_mint


@dataclass(frozen=True)
class SolanaRiskEvidenceReport:
    mint: str
    observed_at_ms: int
    goplus_provider: str | None
    goplus_evidence_id: str | None
    goplus_observed_at_ms: int | None
    liquidity_locked: bool | None
    holder_provider: str | None
    holder_evidence_id: str | None
    holder_block_ref: str | None
    holder_count: int | None
    total_supply_raw: int | None
    observed_holder_balance_raw: int | None
    holder_coverage_complete: bool
    top_holder_pct: float | None
    top5_holder_pct: float | None
    top10_holder_pct: float | None
    missing_claims: tuple[str, ...]
    errors: tuple[str, ...]
    mint_evidence_id: str | None = None
    mint_finalized_slot: int | None = None
    token_program_id: str | None = None
    mint_authority: str | None = None
    freeze_authority: str | None = None
    token_2022_extension_types: tuple[str, ...] = ()
    token_2022_extension_review_complete: bool = False

    @property
    def risk_state(self) -> str:
        # This is an evidence-readiness state, not a token safety verdict.
        if self.missing_claims or self.errors:
            return "REQUIRES_MORE_EVIDENCE"
        return "EVIDENCE_COLLECTED_REVIEW_REQUIRED"


class SolanaRiskEvidenceCollector:
    """Collect independent, timestamped Solana token-risk evidence."""

    REQUIRED_UNKNOWN_CLAIMS = (
        "developer_wallet_linkage",
        "bundled_supply",
        "mint_account_state",
        "mint_authority",
        "freeze_authority",
        "token_2022_extension_review",
        "creator_wallet_provenance",
        "executable_sellability",
    )

    def __init__(
        self,
        goplus_provider: GoPlusTokenSafetyProvider,
        holder_provider: SolanaHolderInventoryProvider,
        *,
        mint_provider: SolanaMintAuthorityProvider | None = None,
        max_age_ms: int = 300_000,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if max_age_ms <= 0:
            raise ValueError("max_age_ms must be positive")
        self.goplus_provider = goplus_provider
        self.holder_provider = holder_provider
        self.mint_provider = mint_provider
        self.max_age_ms = max_age_ms
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def collect(self, mint: str, *, now_ms: int | None = None) -> SolanaRiskEvidenceReport:
        canonical = canonical_mint(mint)
        now = self.clock_ms() if now_ms is None else now_ms
        errors: list[str] = []

        goplus_observation = None
        try:
            candidate = self.goplus_provider.fetch_safety_observation("solana", canonical)
            if not isinstance(candidate, TokenSafetyObservation):
                raise ProviderError("GoPlus returned an untyped token-safety observation")
            if candidate.provider != getattr(self.goplus_provider, "provider_id", None):
                raise ProviderError("GoPlus provider identity mismatch")
            if candidate.token_address != canonical:
                raise ProviderError("GoPlus returned evidence for another mint")
            if (not isinstance(candidate.observed_at_ms, int)
                    or isinstance(candidate.observed_at_ms, bool)
                    or candidate.observed_at_ms > now
                    or now - candidate.observed_at_ms > self.max_age_ms):
                raise ProviderError("GoPlus evidence is stale or future-dated")
            goplus_observation = candidate
        except Exception as exc:
            errors.append(f"goplus: {type(exc).__name__}: {str(exc)[:240]}")

        holder_snapshot = None
        try:
            candidate = self.holder_provider.fetch_snapshot(canonical)
            if not isinstance(candidate, HolderSnapshot):
                raise ProviderError("holder provider returned an untyped snapshot")
            if candidate.chain != "solana" or candidate.token_address != canonical:
                raise ProviderError("holder provider returned a mismatched chain or mint")
            if (not isinstance(candidate.observed_at_ms, int)
                    or isinstance(candidate.observed_at_ms, bool)
                    or candidate.observed_at_ms > now
                    or now - candidate.observed_at_ms > self.max_age_ms):
                raise ProviderError("holder snapshot is stale or future-dated")
            if not candidate.provider or not candidate.evidence_id or not candidate.block_ref:
                raise ProviderError("holder snapshot provenance is incomplete")
            if (not isinstance(candidate.total_supply_raw, int)
                    or isinstance(candidate.total_supply_raw, bool)
                    or candidate.total_supply_raw <= 0):
                raise ProviderError("holder total supply is invalid")
            if (not isinstance(candidate.decimals, int) or isinstance(candidate.decimals, bool)
                    or not 0 <= candidate.decimals <= 38):
                raise ProviderError("holder token decimals are invalid")
            seen = set()
            for holder in candidate.holders:
                if (not isinstance(holder.wallet_address, str) or not holder.wallet_address
                        or not isinstance(holder.balance_raw, int)
                        or isinstance(holder.balance_raw, bool) or holder.balance_raw < 0):
                    raise ProviderError("holder balance row is malformed")
                if holder.wallet_address in seen:
                    raise ProviderError("holder inventory contains duplicate wallet rows")
                seen.add(holder.wallet_address)
            holder_snapshot = candidate
        except Exception as exc:
            errors.append(f"holders: {type(exc).__name__}: {str(exc)[:240]}")

        mint_snapshot = None
        if self.mint_provider is not None:
            try:
                candidate = self.mint_provider.fetch_snapshot(canonical)
                if not isinstance(candidate, SolanaMintAuthoritySnapshot):
                    raise ProviderError("mint provider returned an untyped snapshot")
                if candidate.mint != canonical:
                    raise ProviderError("mint provider returned evidence for another mint")
                if candidate.provider != getattr(self.mint_provider, "provider_id", None):
                    raise ProviderError("mint provider identity mismatch")
                if not isinstance(candidate.evidence_id, str) or not candidate.evidence_id.strip():
                    raise ProviderError("mint evidence provenance is incomplete")
                if (isinstance(candidate.observed_at_ms, bool)
                        or not isinstance(candidate.observed_at_ms, int)
                        or candidate.observed_at_ms > now
                        or now - candidate.observed_at_ms > self.max_age_ms):
                    raise ProviderError("mint evidence is stale or future-dated")
                if (isinstance(candidate.finalized_slot, bool)
                        or not isinstance(candidate.finalized_slot, int)
                        or candidate.finalized_slot < 0):
                    raise ProviderError("mint finalized slot is invalid")
                if candidate.token_program_id not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
                    raise ProviderError("mint token-program identity is unsupported")
                if candidate.extension_tlv_structure_valid is not True:
                    raise ProviderError("Token-2022 extension TLV structure is incomplete")
                if not isinstance(candidate.extension_types, tuple) or any(
                    not isinstance(name, str) or not name for name in candidate.extension_types
                ):
                    raise ProviderError("mint extension type inventory is malformed")
                if not isinstance(candidate.unknown_extension_ids, tuple):
                    raise ProviderError("mint unknown-extension inventory is malformed")
                mint_snapshot = candidate
            except Exception as exc:
                errors.append(f"mint_account: {type(exc).__name__}: {str(exc)[:240]}")

        holder_count = total_supply = balance_sum = None
        coverage_complete = False
        top1 = top5 = top10 = None
        holder_provider_id = holder_evidence_id = holder_block_ref = None
        if holder_snapshot is not None:
            holder_provider_id = holder_snapshot.provider
            holder_evidence_id = holder_snapshot.evidence_id
            holder_block_ref = holder_snapshot.block_ref
            holder_count = len(holder_snapshot.holders)
            total_supply = holder_snapshot.total_supply_raw
            balances = sorted(
                (holder.balance_raw for holder in holder_snapshot.holders),
                reverse=True,
            )
            balance_sum = sum(balances)
            coverage_complete = (
                holder_snapshot.coverage_complete is True
                and balance_sum == total_supply
            )
            if coverage_complete:
                denominator = float(total_supply)
                top1 = sum(balances[:1]) / denominator * 100.0
                top5 = sum(balances[:5]) / denominator * 100.0
                top10 = sum(balances[:10]) / denominator * 100.0

        missing = list(self.REQUIRED_UNKNOWN_CLAIMS)
        if mint_snapshot is not None:
            for claim in ("mint_account_state", "mint_authority", "freeze_authority"):
                missing.remove(claim)
            if not mint_snapshot.extension_types:
                missing.remove("token_2022_extension_review")
        if mint_snapshot is None:
            missing.append("mint_account_state")
        if goplus_observation is None:
            missing.append("goplus_token_security_observation")
        if holder_snapshot is None:
            missing.append("complete_holder_inventory")
        elif not coverage_complete:
            missing.append("reconciled_complete_holder_inventory")

        return SolanaRiskEvidenceReport(
            mint=canonical,
            observed_at_ms=now,
            goplus_provider=(goplus_observation.provider if goplus_observation else None),
            goplus_evidence_id=(goplus_observation.evidence_id if goplus_observation else None),
            goplus_observed_at_ms=(goplus_observation.observed_at_ms if goplus_observation else None),
            liquidity_locked=(goplus_observation.liquidity_locked if goplus_observation else None),
            holder_provider=holder_provider_id,
            holder_evidence_id=holder_evidence_id,
            holder_block_ref=holder_block_ref,
            holder_count=holder_count,
            total_supply_raw=total_supply,
            observed_holder_balance_raw=balance_sum,
            holder_coverage_complete=coverage_complete,
            top_holder_pct=top1,
            top5_holder_pct=top5,
            top10_holder_pct=top10,
            missing_claims=tuple(missing),
            errors=tuple(errors),
            mint_evidence_id=(mint_snapshot.evidence_id if mint_snapshot else None),
            mint_finalized_slot=(mint_snapshot.finalized_slot if mint_snapshot else None),
            token_program_id=(mint_snapshot.token_program_id if mint_snapshot else None),
            mint_authority=(mint_snapshot.mint_authority if mint_snapshot else None),
            freeze_authority=(mint_snapshot.freeze_authority if mint_snapshot else None),
            token_2022_extension_types=(mint_snapshot.extension_types if mint_snapshot else ()),
            token_2022_extension_review_complete=(
                mint_snapshot is not None and not mint_snapshot.extension_types
            ),
        )
