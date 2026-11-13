"""Claim-separated Solana security evidence and deterministic risk flags.

This collector records facts and evidence gaps. It does not certify a token as
safe, produce a buy/sell signal, or replace independent provider due diligence.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable

from ..developer_concentration import HolderSnapshot
from ..providers.base import ProviderError
from ..providers.goplus import GoPlusTokenSafetyProvider
from ..providers.holder_inventory import SolanaHolderInventoryProvider
from ..providers.intelligence import TokenSafetyObservation
from ..providers.solana_mint import (
    SolanaMintAuthorityProvider,
    SolanaMintAuthoritySnapshot,
    TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
)
from .candidates import canonical_mint


@dataclass(frozen=True)
class RiskFlag:
    code: str
    severity: str
    description: str
    evidence_source: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("risk flag code is required")
        if self.severity not in {"info", "low", "medium", "high", "critical"}:
            raise ValueError("risk flag severity is invalid")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("risk flag description is required")
        if not isinstance(self.evidence_source, str) or not self.evidence_source.strip():
            raise ValueError("risk flag evidence source is required")


@dataclass(frozen=True)
class SolanaRiskEvidenceReport:
    mint: str
    observed_at_ms: int
    goplus_provider: str | None
    goplus_evidence_id: str | None
    goplus_observed_at_ms: int | None
    liquidity_locked: bool | None
    goplus_dev_hold_pct: float | None
    goplus_bundled_supply_pct: float | None
    goplus_creator_hold_pct: float | None
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
    mint_supply_raw: int | None = None
    mint_decimals: int | None = None
    mint_supply_reconciled: bool = False
    risk_flags: tuple[RiskFlag, ...] = ()

    def __post_init__(self) -> None:
        if canonical_mint(self.mint) != self.mint:
            raise ValueError("risk report mint must be canonical")
        if isinstance(self.observed_at_ms, bool) or not isinstance(self.observed_at_ms, int) or self.observed_at_ms <= 0:
            raise ValueError("risk report timestamp must be a positive integer")
        for field in ("goplus_provider", "goplus_evidence_id", "holder_provider", "holder_evidence_id", "holder_block_ref", "mint_evidence_id", "token_program_id"):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field} must be a non-empty string or None")
        if self.token_program_id is not None and self.token_program_id not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
            raise ValueError("risk report token program is unsupported")
        for field in ("mint_authority", "freeze_authority"):
            value = getattr(self, field)
            if value is not None and canonical_mint(value) != value:
                raise ValueError(f"{field} must be a canonical Solana public key")
        if self.goplus_observed_at_ms is not None and (
            isinstance(self.goplus_observed_at_ms, bool)
            or not isinstance(self.goplus_observed_at_ms, int)
            or self.goplus_observed_at_ms <= 0
        ):
            raise ValueError("goplus_observed_at_ms must be a positive integer or None")
        for field in ("holder_count", "mint_finalized_slot"):
            value = getattr(self, field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{field} must be a non-negative integer or None")
        for field in ("total_supply_raw", "mint_supply_raw"):
            value = getattr(self, field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{field} must be a positive integer or None")
        if self.observed_holder_balance_raw is not None and (
            isinstance(self.observed_holder_balance_raw, bool)
            or not isinstance(self.observed_holder_balance_raw, int)
            or self.observed_holder_balance_raw < 0
        ):
            raise ValueError("observed_holder_balance_raw must be a non-negative integer or None")
        if self.mint_decimals is not None and (
            isinstance(self.mint_decimals, bool)
            or not isinstance(self.mint_decimals, int)
            or not 0 <= self.mint_decimals <= 38
        ):
            raise ValueError("mint_decimals must be between 0 and 38 or None")
        for field in ("holder_coverage_complete", "token_2022_extension_review_complete", "mint_supply_reconciled"):
            if not isinstance(getattr(self, field), bool):
                raise ValueError(f"{field} must be boolean")
        if self.liquidity_locked is not None and not isinstance(self.liquidity_locked, bool):
            raise ValueError("liquidity_locked must be boolean or None")
        for field in ("goplus_dev_hold_pct", "goplus_bundled_supply_pct", "goplus_creator_hold_pct", "top_holder_pct", "top5_holder_pct", "top10_holder_pct"):
            value = getattr(self, field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or not 0 <= float(value) <= 100
            ):
                raise ValueError(f"{field} must be a finite percentage or None")
        for field in ("missing_claims", "errors", "token_2022_extension_types", "risk_flags"):
            if not isinstance(getattr(self, field), tuple):
                raise ValueError(f"{field} must be a tuple")
        if any(not isinstance(item, str) or not item.strip() for item in self.missing_claims + self.errors + self.token_2022_extension_types):
            raise ValueError("risk report text collections must contain non-empty strings")
        checked_flags = []
        for flag in self.risk_flags:
            if type(flag) is not RiskFlag:
                raise ValueError("risk_flags must contain RiskFlag values")
            checked_flags.append(RiskFlag(flag.code, flag.severity, flag.description, flag.evidence_source))
        if len({flag.code for flag in checked_flags}) != len(checked_flags):
            raise ValueError("risk flag codes must be unique")
        object.__setattr__(self, "missing_claims", tuple(sorted(set(self.missing_claims))))
        object.__setattr__(self, "token_2022_extension_types", tuple(sorted(set(self.token_2022_extension_types))))
        object.__setattr__(self, "risk_flags", tuple(sorted(checked_flags, key=lambda flag: flag.code)))
        if self.holder_coverage_complete and (
            self.total_supply_raw is None or self.observed_holder_balance_raw != self.total_supply_raw
        ):
            raise ValueError("complete holder coverage must reconcile to total supply")
        if self.mint_supply_reconciled and (
            self.mint_supply_raw is None or self.total_supply_raw is None
            or self.mint_supply_raw != self.total_supply_raw
        ):
            raise ValueError("mint supply reconciliation must match reported supplies")
        if any(value is not None for value in (self.top_holder_pct, self.top5_holder_pct, self.top10_holder_pct)) and (
            not self.holder_coverage_complete or not self.mint_supply_reconciled
        ):
            raise ValueError("concentration percentages require complete, mint-reconciled inventory")

    @property
    def risk_state(self) -> str:
        """Evidence readiness only; this property never returns SAFE."""
        if self.missing_claims or self.errors:
            return "REQUIRES_MORE_EVIDENCE"
        return "EVIDENCE_COLLECTED_REVIEW_REQUIRED"


def validate_risk_report(report: object) -> SolanaRiskEvidenceReport:
    """Reconstruct a report and nested risk flags before persistent storage."""
    if type(report) is not SolanaRiskEvidenceReport:
        raise TypeError("report must be a normalized SolanaRiskEvidenceReport")
    try:
        values = {field.name: getattr(report, field.name) for field in fields(SolanaRiskEvidenceReport)}
        return SolanaRiskEvidenceReport(**values)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("risk report failed canonical validation") from None




class SolanaRiskEvidenceCollector:
    """Collect independent mint, holder and supplemental token-security evidence."""

    REQUIRED_UNKNOWN_CLAIMS = (
        "liquidity_lock_complete_inventory",
        "developer_wallet_linkage",
        "bundled_supply",
        "creator_wallet_provenance",
        "executable_sellability",
    )

    EXTENSION_RISK = {
        "transfer_fee_config": ("token2022_transfer_fee_config", "medium",
            "Token-2022 transfer-fee configuration is present; inspect fee parameters and authorities"),
        "mint_close_authority": ("token2022_mint_close_authority", "medium",
            "Token-2022 mint close authority is present"),
        "confidential_transfer_mint": ("token2022_confidential_transfer", "medium",
            "Confidential transfer functionality requires specialist review"),
        "default_account_state": ("token2022_default_account_state", "high",
            "Token-2022 default account state can affect whether holders can transfer"),
        "non_transferable": ("token2022_non_transferable", "critical",
            "Token-2022 NonTransferable extension is present"),
        "interest_bearing_config": ("token2022_interest_bearing", "info",
            "Token-2022 interest-bearing configuration is present"),
        "permanent_delegate": ("token2022_permanent_delegate", "critical",
            "Token-2022 permanent delegate can retain privileged token-control powers"),
        "transfer_hook": ("token2022_transfer_hook", "high",
            "Token-2022 transfer hook executes external transfer logic"),
        "confidential_transfer_fee_config": ("token2022_confidential_transfer_fee", "high",
            "Confidential transfer-fee configuration requires specialist review"),
        "metadata_pointer": ("token2022_metadata_pointer", "info",
            "Token-2022 metadata pointer is present"),
        "token_metadata": ("token2022_token_metadata", "info",
            "Token-2022 on-chain metadata extension is present"),
        "group_pointer": ("token2022_group_pointer", "info",
            "Token-2022 group pointer is present"),
        "token_group": ("token2022_token_group", "info",
            "Token-2022 token-group extension is present"),
        "group_member_pointer": ("token2022_group_member_pointer", "info",
            "Token-2022 group-member pointer is present"),
        "token_group_member": ("token2022_token_group_member", "info",
            "Token-2022 token-group member extension is present"),
        "confidential_mint_burn": ("token2022_confidential_mint_burn", "high",
            "Confidential mint/burn extension requires specialist review"),
        "scaled_ui_amount": ("token2022_scaled_ui_amount", "medium",
            "Token-2022 scaled UI amount can change displayed token quantities"),
        "pausable": ("token2022_pausable", "high",
            "Token-2022 pausable extension can pause token operations"),
    }

    def __init__(
        self,
        goplus_provider: GoPlusTokenSafetyProvider | None,
        holder_provider: SolanaHolderInventoryProvider | None,
        *,
        mint_provider: SolanaMintAuthorityProvider | None = None,
        max_age_ms: int = 300_000,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if isinstance(max_age_ms, bool) or not isinstance(max_age_ms, int) or max_age_ms <= 0:
            raise ValueError("max_age_ms must be a positive integer")
        self.goplus_provider = goplus_provider
        self.holder_provider = holder_provider
        self.mint_provider = mint_provider
        self.max_age_ms = max_age_ms
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    @staticmethod
    def _valid_optional_pct(value: object) -> bool:
        if value is None:
            return True
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        number = float(value)
        return math.isfinite(number) and 0 <= number <= 100

    def _fresh(self, observed: object, now: int) -> bool:
        return (
            isinstance(observed, int)
            and not isinstance(observed, bool)
            and 0 < observed <= now
            and now - observed <= self.max_age_ms
        )

    def collect(self, mint: str, *, now_ms: int | None = None) -> SolanaRiskEvidenceReport:
        canonical = canonical_mint(mint)
        now = self.clock_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise ValueError("now_ms must be a positive integer")

        errors: list[str] = []
        missing = set(self.REQUIRED_UNKNOWN_CLAIMS)
        missing.update({
            "mint_account_state",
            "mint_authority",
            "freeze_authority",
            "token_2022_extension_review",
            "complete_holder_inventory",
            "mint_holder_supply_reconciliation",
        })

        goplus_observation = None
        if self.goplus_provider is None:
            missing.add("goplus_token_security_observation")
        else:
            try:
                candidate = self.goplus_provider.fetch_safety_observation("solana", canonical)
                if not isinstance(candidate, TokenSafetyObservation):
                    raise ProviderError("GoPlus returned an untyped token-safety observation")
                if candidate.provider != getattr(self.goplus_provider, "provider_id", None):
                    raise ProviderError("GoPlus provider identity mismatch")
                if candidate.token_address != canonical:
                    raise ProviderError("GoPlus returned evidence for another mint")
                if not self._fresh(candidate.observed_at_ms, now):
                    raise ProviderError("GoPlus evidence is stale or future-dated")
                if not isinstance(candidate.evidence_id, str) or not candidate.evidence_id.strip():
                    raise ProviderError("GoPlus evidence identifier is missing")
                if candidate.liquidity_locked is not None and not isinstance(candidate.liquidity_locked, bool):
                    raise ProviderError("GoPlus liquidity status is malformed")
                for field in ("dev_hold_pct", "bundled_supply_pct", "creator_hold_pct"):
                    if not self._valid_optional_pct(getattr(candidate, field)):
                        raise ProviderError(f"GoPlus {field} is malformed")
                goplus_observation = candidate
                missing.discard("goplus_token_security_observation")
            except Exception as exc:
                errors.append(f"goplus: {type(exc).__name__}")
                missing.add("goplus_token_security_observation")

        holder_snapshot = None
        if self.holder_provider is None:
            missing.add("complete_holder_inventory")
        else:
            try:
                candidate = self.holder_provider.fetch_snapshot(canonical)
                if not isinstance(candidate, HolderSnapshot):
                    raise ProviderError("holder provider returned an untyped snapshot")
                if candidate.provider != getattr(self.holder_provider, "provider_id", None):
                    raise ProviderError("holder provider identity mismatch")
                if candidate.chain != "solana" or candidate.token_address != canonical:
                    raise ProviderError("holder provider returned a mismatched chain or mint")
                if not self._fresh(candidate.observed_at_ms, now):
                    raise ProviderError("holder snapshot is stale or future-dated")
                if not isinstance(candidate.evidence_id, str) or not candidate.evidence_id.strip():
                    raise ProviderError("holder evidence identifier is missing")
                if not isinstance(candidate.block_ref, str) or not candidate.block_ref.strip():
                    raise ProviderError("holder block reference is missing")
                if (
                    isinstance(candidate.total_supply_raw, bool)
                    or not isinstance(candidate.total_supply_raw, int)
                    or candidate.total_supply_raw <= 0
                ):
                    raise ProviderError("holder total supply is invalid")
                if (
                    isinstance(candidate.decimals, bool)
                    or not isinstance(candidate.decimals, int)
                    or not 0 <= candidate.decimals <= 38
                ):
                    raise ProviderError("holder token decimals are invalid")
                if not isinstance(candidate.holders, tuple):
                    raise ProviderError("holder inventory is not a tuple")
                seen = set()
                for holder in candidate.holders:
                    if (
                        not isinstance(holder.wallet_address, str)
                        or canonical_mint(holder.wallet_address) != holder.wallet_address
                        or isinstance(holder.balance_raw, bool)
                        or not isinstance(holder.balance_raw, int)
                        or holder.balance_raw < 0
                    ):
                        raise ProviderError("holder balance row is malformed")
                    if holder.wallet_address in seen:
                        raise ProviderError("holder inventory contains duplicate wallet rows")
                    seen.add(holder.wallet_address)
                if not isinstance(candidate.coverage_complete, bool):
                    raise ProviderError("holder coverage declaration is malformed")
                holder_snapshot = candidate
            except Exception as exc:
                errors.append(f"holders: {type(exc).__name__}")
                missing.add("complete_holder_inventory")

        mint_snapshot = None
        if self.mint_provider is None:
            missing.add("mint_account_state")
        else:
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
                if not self._fresh(candidate.observed_at_ms, now):
                    raise ProviderError("mint evidence is stale or future-dated")
                if (
                    isinstance(candidate.finalized_slot, bool)
                    or not isinstance(candidate.finalized_slot, int)
                    or candidate.finalized_slot < 0
                ):
                    raise ProviderError("mint finalized slot is invalid")
                if candidate.token_program_id not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
                    raise ProviderError("mint token-program identity is unsupported")
                if candidate.extension_tlv_structure_valid is not True:
                    raise ProviderError("Token-2022 extension TLV structure is invalid")
                if (
                    isinstance(candidate.supply_raw, bool)
                    or not isinstance(candidate.supply_raw, int)
                    or candidate.supply_raw <= 0
                    or isinstance(candidate.decimals, bool)
                    or not isinstance(candidate.decimals, int)
                    or not 0 <= candidate.decimals <= 38
                ):
                    raise ProviderError("mint supply or decimals are invalid")
                if not isinstance(candidate.extension_types, tuple) or any(
                    not isinstance(name, str) or not name for name in candidate.extension_types
                ):
                    raise ProviderError("mint extension type inventory is malformed")
                if not isinstance(candidate.unknown_extension_ids, tuple) or any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in candidate.unknown_extension_ids
                ):
                    raise ProviderError("mint unknown extension inventory is malformed")
                for authority in (candidate.mint_authority, candidate.freeze_authority):
                    if authority is not None and canonical_mint(authority) != authority:
                        raise ProviderError("mint authority public key is malformed")
                if (
                    not isinstance(candidate.account_data_sha256, str)
                    or len(candidate.account_data_sha256) != 64
                    or any(char not in "0123456789abcdef" for char in candidate.account_data_sha256)
                ):
                    raise ProviderError("mint account digest is malformed")
                mint_snapshot = candidate
                missing.discard("mint_account_state")
                missing.discard("mint_authority")
                missing.discard("freeze_authority")
                extension_review_complete = (
                    not candidate.unknown_extension_ids
                    and all(name in self.EXTENSION_RISK for name in candidate.extension_types)
                )
                if extension_review_complete:
                    missing.discard("token_2022_extension_review")
            except Exception as exc:
                errors.append(f"mint_account: {type(exc).__name__}")
                missing.update({
                    "mint_account_state",
                    "mint_authority",
                    "freeze_authority",
                    "token_2022_extension_review",
                })

        mint_supply_reconciled = bool(
            mint_snapshot is not None
            and holder_snapshot is not None
            and mint_snapshot.supply_raw == holder_snapshot.total_supply_raw
            and mint_snapshot.decimals == holder_snapshot.decimals
        )
        if mint_snapshot is not None and holder_snapshot is not None:
            if mint_supply_reconciled:
                missing.discard("mint_holder_supply_reconciliation")
            else:
                missing.add("mint_holder_supply_reconciliation")
                errors.append("mint_account: finalized supply or decimals disagree with holder inventory")

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
                missing.discard("complete_holder_inventory")
                if mint_supply_reconciled:
                    denominator = float(total_supply)
                    top1 = balances[0] / denominator * 100.0 if balances else 0.0
                    top5 = sum(balances[:5]) / denominator * 100.0
                    top10 = sum(balances[:10]) / denominator * 100.0
            else:
                missing.add("complete_holder_inventory")

        flags: dict[str, RiskFlag] = {}

        def add_flag(code: str, severity: str, description: str, source: str) -> None:
            flags[code] = RiskFlag(code, severity, description, source)

        if mint_snapshot is not None:
            if mint_snapshot.mint_authority is not None:
                add_flag(
                    "mint_authority_active", "high",
                    "Mint authority remains active and may permit additional token issuance",
                    "solana-finalized-mint-account",
                )
            if mint_snapshot.freeze_authority is not None:
                add_flag(
                    "freeze_authority_active", "critical",
                    "Freeze authority remains active and may freeze token accounts",
                    "solana-finalized-mint-account",
                )
            for extension in mint_snapshot.extension_types:
                if extension in self.EXTENSION_RISK:
                    code, severity, description = self.EXTENSION_RISK[extension]
                else:
                    code, severity, description = (
                        f"token2022_unknown_extension_{extension.removeprefix('unknown_')}",
                        "high",
                        f"Unrecognized Token-2022 extension {extension} requires manual review",
                    )
                add_flag(code, severity, description, "solana-finalized-mint-account")
            if mint_snapshot.unknown_extension_ids:
                add_flag(
                    "token2022_unknown_extension_ids", "high",
                    "Mint contains extension IDs not recognized by the local parser",
                    "solana-finalized-mint-account",
                )

        if coverage_complete and top1 is not None and top5 is not None and top10 is not None:
            if top1 >= 50:
                add_flag(
                    "top_holder_concentration_critical", "high",
                    "One owner controls at least 50% of supply; classify pool, burn and custodial wallets",
                    "solana-rpc-holder-inventory",
                )
            elif top1 >= 20:
                add_flag(
                    "top_holder_concentration_elevated", "medium",
                    "One owner controls at least 20% of supply; wallet purpose is not yet classified",
                    "solana-rpc-holder-inventory",
                )
            if top5 >= 80:
                add_flag(
                    "top5_concentration_elevated", "high",
                    "Top five owners control at least 80% of supply; classify pool and custodial wallets",
                    "solana-rpc-holder-inventory",
                )
            elif top5 >= 50:
                add_flag(
                    "top5_concentration_moderate", "medium",
                    "Top five owners control at least 50% of supply",
                    "solana-rpc-holder-inventory",
                )
            if top10 >= 90:
                add_flag(
                    "top10_concentration_elevated", "high",
                    "Top ten owners control at least 90% of supply",
                    "solana-rpc-holder-inventory",
                )
        elif holder_snapshot is not None:
            add_flag(
                "holder_inventory_incomplete", "high",
                "Holder inventory does not reconcile exactly to total supply",
                "solana-rpc-holder-inventory",
            )

        if mint_snapshot is not None and holder_snapshot is not None and not mint_supply_reconciled:
            add_flag(
                "mint_holder_supply_mismatch", "critical",
                "Finalized mint supply/decimals do not reconcile with the holder inventory",
                "solana-finalized-mint-account+solana-rpc-holder-inventory",
            )

        if goplus_observation is not None:
            for field, medium_code, high_code, label in (
                ("dev_hold_pct", "goplus_reports_elevated_dev_holdings",
                 "goplus_reports_high_dev_holdings", "developer holdings"),
                ("bundled_supply_pct", "goplus_reports_elevated_bundle_supply",
                 "goplus_reports_high_bundle_supply", "bundled supply"),
                ("creator_hold_pct", "goplus_reports_elevated_creator_holdings",
                 "goplus_reports_high_creator_holdings", "creator holdings"),
            ):
                value = getattr(goplus_observation, field)
                if value is not None and value >= 50:
                    add_flag(
                        high_code, "high",
                        f"GoPlus reports {value:.2f}% {label}; this is supplemental provider data",
                        "goplus",
                    )
                elif value is not None and value >= 20:
                    add_flag(
                        medium_code, "medium",
                        f"GoPlus reports {value:.2f}% {label}; independent linkage is not established",
                        "goplus",
                    )
            if goplus_observation.liquidity_locked is False:
                add_flag(
                    "goplus_reports_unlocked_liquidity", "medium",
                    "GoPlus reports unlocked LP; this is supplemental and not an exhaustive all-pool lock proof",
                    "goplus",
                )
            elif goplus_observation.liquidity_locked is True:
                add_flag(
                    "goplus_reports_locked_liquidity", "info",
                    "GoPlus reports locked LP; independent all-pool verification remains missing",
                    "goplus",
                )

        ordered_flags = tuple(flags[key] for key in sorted(flags))
        missing_claims = tuple(sorted(missing))
        return SolanaRiskEvidenceReport(
            mint=canonical,
            observed_at_ms=now,
            goplus_provider=(goplus_observation.provider if goplus_observation else None),
            goplus_evidence_id=(goplus_observation.evidence_id if goplus_observation else None),
            goplus_observed_at_ms=(goplus_observation.observed_at_ms if goplus_observation else None),
            liquidity_locked=(goplus_observation.liquidity_locked if goplus_observation else None),
            goplus_dev_hold_pct=(goplus_observation.dev_hold_pct if goplus_observation else None),
            goplus_bundled_supply_pct=(goplus_observation.bundled_supply_pct if goplus_observation else None),
            goplus_creator_hold_pct=(goplus_observation.creator_hold_pct if goplus_observation else None),
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
            missing_claims=missing_claims,
            errors=tuple(errors),
            mint_evidence_id=(mint_snapshot.evidence_id if mint_snapshot else None),
            mint_finalized_slot=(mint_snapshot.finalized_slot if mint_snapshot else None),
            token_program_id=(mint_snapshot.token_program_id if mint_snapshot else None),
            mint_authority=(mint_snapshot.mint_authority if mint_snapshot else None),
            freeze_authority=(mint_snapshot.freeze_authority if mint_snapshot else None),
            token_2022_extension_types=(mint_snapshot.extension_types if mint_snapshot else ()),
            token_2022_extension_review_complete=(
                mint_snapshot is not None
                and not mint_snapshot.unknown_extension_ids
                and all(name in self.EXTENSION_RISK for name in mint_snapshot.extension_types)
            ),
            mint_supply_raw=(mint_snapshot.supply_raw if mint_snapshot else None),
            mint_decimals=(mint_snapshot.decimals if mint_snapshot else None),
            mint_supply_reconciled=mint_supply_reconciled,
            risk_flags=ordered_flags,
        )
