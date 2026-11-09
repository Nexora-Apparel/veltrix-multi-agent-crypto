"""Complete-coverage developer-linked holder concentration analysis.

This is a deterministic analyzer, not a chain data provider. It only emits a
concentration percentage when an upstream provider supplies an exhaustive
holder snapshot and a complete, auditable funding graph for every path.
"""
from dataclasses import dataclass
import math
from typing import Iterable

from .safety import SafetyEvidence


CHAINS = frozenset({"solana", "base", "bsc"})


def _key(chain: str, address: str) -> str:
    return address.lower() if chain in {"base", "bsc"} else address


@dataclass(frozen=True)
class HolderBalance:
    wallet_address: str
    balance_raw: int


@dataclass(frozen=True)
class HolderSnapshot:
    provider: str
    chain: str
    token_address: str
    observed_at_ms: int
    evidence_id: str
    block_ref: str
    decimals: int
    total_supply_raw: int
    holders: tuple[HolderBalance, ...]
    coverage_complete: bool


@dataclass(frozen=True)
class FundingRelationship:
    wallet_address: str
    funder_address: str | None
    transaction_id: str | None
    observed_at_ms: int
    evidence_id: str


@dataclass(frozen=True)
class FundingGraphSnapshot:
    provider: str
    chain: str
    token_address: str
    observed_at_ms: int
    evidence_id: str
    block_ref: str
    coverage_start_ms: int
    coverage_end_ms: int
    covered_wallets: tuple[str, ...]
    relationships: tuple[FundingRelationship, ...]
    coverage_complete: bool


@dataclass(frozen=True)
class DeveloperConcentrationReport:
    chain: str
    token_address: str
    provider: str | None
    linked_wallets: tuple[str, ...]
    linked_supply_raw: int | None
    total_supply_raw: int | None
    dev_hold_pct: float | None
    coverage_complete: bool
    observed_at_ms: int
    evidence_id: str | None
    source_ref: str | None
    method: str
    independence_group: str | None
    holder_provider: str | None
    funding_provider: str | None
    root_provider: str | None
    reasons: tuple[str, ...]

    def to_safety_evidence(self) -> SafetyEvidence | None:
        """Return typed claim evidence only when every completeness check passed."""
        if not self.coverage_complete or self.dev_hold_pct is None or not self.provider:
            return None
        return SafetyEvidence(
            token_address=self.token_address,
            provider=self.provider,
            evidence_id=self.evidence_id or "",
            observed_at_ms=self.observed_at_ms,
            liquidity_locked=None,
            dev_hold_pct=self.dev_hold_pct,
            bundled_supply_pct=None,
            claim="developer_holdings",
            chain=self.chain,
            coverage_complete=True,
            source_ref=self.source_ref,
            method=self.method,
            independence_group=self.independence_group,
        )


class DeveloperConcentrationAnalyzer:
    """Calculate balances held by wallets with a verified funding path to roots.

    Common exchange/funder relationships do not count as developer linkage unless
    the ancestry actually reaches an independently attested developer root.
    """

    def __init__(self, max_age_ms: int = 300_000, max_ancestry_depth: int = 8):
        if max_age_ms <= 0 or max_ancestry_depth < 1:
            raise ValueError("invalid evidence freshness or ancestry bound")
        self.max_age_ms = max_age_ms
        self.max_ancestry_depth = max_ancestry_depth

    def analyze(
        self,
        chain: str,
        token_address: str,
        holder_snapshot: HolderSnapshot,
        funding_snapshot: FundingGraphSnapshot,
        developer_roots: Iterable[str],
        *,
        root_provider: str,
        root_evidence_id: str,
        now_ms: int,
        certified_provider_id: str | None = None,
        independence_group: str | None = None,
    ) -> DeveloperConcentrationReport:
        reasons: list[str] = []
        chain_key = chain.lower() if isinstance(chain, str) else ""
        if chain_key not in CHAINS:
            reasons.append("unsupported chain")
        if not isinstance(token_address, str) or not token_address.strip():
            reasons.append("missing token address")
        if not isinstance(holder_snapshot, HolderSnapshot):
            reasons.append("holder snapshot is missing or malformed")
        if not isinstance(funding_snapshot, FundingGraphSnapshot):
            reasons.append("funding graph snapshot is missing or malformed")
        if reasons:
            return self._blocked(chain_key, token_address, now_ms, reasons)

        if holder_snapshot.chain.lower() != chain_key or funding_snapshot.chain.lower() != chain_key:
            reasons.append("holder or funding snapshot chain mismatch")
        if not self._same_token(chain_key, holder_snapshot.token_address, token_address):
            reasons.append("holder snapshot token mismatch")
        if not self._same_token(chain_key, funding_snapshot.token_address, token_address):
            reasons.append("funding graph token mismatch")
        if not holder_snapshot.coverage_complete or not funding_snapshot.coverage_complete:
            reasons.append("holder or funding graph coverage is incomplete")
        if not holder_snapshot.provider or not funding_snapshot.provider:
            reasons.append("holder or funding provider identity is missing")
        if holder_snapshot.provider == funding_snapshot.provider:
            reasons.append("holder and funding graph sources must be independently identified")
        if not root_provider or not root_evidence_id:
            reasons.append("developer-root provenance is missing")
        if root_provider in {holder_snapshot.provider, funding_snapshot.provider}:
            reasons.append("developer-root identity source must be independent")
        for label, observed in (
            ("holder", holder_snapshot.observed_at_ms),
            ("funding graph", funding_snapshot.observed_at_ms),
        ):
            if not isinstance(observed, int) or isinstance(observed, bool):
                reasons.append(f"{label} timestamp is invalid")
            elif observed > now_ms or now_ms - observed > self.max_age_ms:
                reasons.append(f"{label} evidence is stale or from the future")
        if (not isinstance(holder_snapshot.evidence_id, str) or not holder_snapshot.evidence_id
                or not isinstance(holder_snapshot.block_ref, str) or not holder_snapshot.block_ref):
            reasons.append("holder snapshot provenance is incomplete")
        if (not isinstance(funding_snapshot.evidence_id, str) or not funding_snapshot.evidence_id
                or not isinstance(funding_snapshot.block_ref, str) or not funding_snapshot.block_ref):
            reasons.append("funding graph provenance is incomplete")
        if (not isinstance(funding_snapshot.coverage_start_ms, int)
                or not isinstance(funding_snapshot.coverage_end_ms, int)
                or funding_snapshot.coverage_start_ms > funding_snapshot.coverage_end_ms
                or funding_snapshot.coverage_end_ms < holder_snapshot.observed_at_ms):
            reasons.append("funding graph coverage interval is invalid or incomplete")
        if not isinstance(holder_snapshot.decimals, int) or not 0 <= holder_snapshot.decimals <= 38:
            reasons.append("token decimals are invalid")
        if (not isinstance(holder_snapshot.total_supply_raw, int)
                or isinstance(holder_snapshot.total_supply_raw, bool)
                or holder_snapshot.total_supply_raw <= 0):
            reasons.append("total supply is missing or invalid")
        if not isinstance(holder_snapshot.holders, tuple) or not holder_snapshot.holders:
            reasons.append("complete holder inventory is missing")
        if not isinstance(funding_snapshot.relationships, tuple):
            reasons.append("funding relationships are malformed")
        roots = tuple(x for x in developer_roots if isinstance(x, str) and x.strip())
        if not roots:
            reasons.append("no independently identified developer roots")
        if not isinstance(funding_snapshot.covered_wallets, tuple):
            reasons.append("funding graph covered-wallet inventory is malformed")

        if reasons:
            return self._blocked(chain_key, token_address, now_ms, reasons,
                                 holder_snapshot.provider, funding_snapshot.provider, root_provider)

        holders: dict[str, tuple[str, int]] = {}
        total = 0
        for row in holder_snapshot.holders:
            if (not isinstance(row, HolderBalance) or not isinstance(row.wallet_address, str)
                    or not row.wallet_address or not isinstance(row.balance_raw, int)
                    or isinstance(row.balance_raw, bool) or row.balance_raw < 0):
                reasons.append("holder inventory contains malformed wallet or balance")
                continue
            key = _key(chain_key, row.wallet_address)
            if key in holders:
                reasons.append("holder inventory contains duplicate wallet addresses")
                continue
            holders[key] = (row.wallet_address, row.balance_raw)
            total += row.balance_raw
        if total != holder_snapshot.total_supply_raw:
            reasons.append("holder balances do not reconcile exactly to total supply")

        links: dict[str, FundingRelationship] = {}
        for edge in funding_snapshot.relationships:
            if (not isinstance(edge, FundingRelationship) or not isinstance(edge.wallet_address, str)
                    or not edge.wallet_address or not isinstance(edge.observed_at_ms, int)
                    or isinstance(edge.observed_at_ms, bool) or not edge.evidence_id):
                reasons.append("funding graph contains malformed relationship evidence")
                continue
            key = _key(chain_key, edge.wallet_address)
            if key in links:
                reasons.append("funding graph contains duplicate wallet relationships")
                continue
            if edge.observed_at_ms > now_ms or now_ms - edge.observed_at_ms > self.max_age_ms:
                reasons.append("funding relationship is stale or from the future")
            if edge.funder_address is not None and (
                not isinstance(edge.funder_address, str) or not edge.funder_address
                or not isinstance(edge.transaction_id, str) or not edge.transaction_id
            ):
                reasons.append("funding relationship lacks transaction provenance")
            links[key] = edge

        covered = {_key(chain_key, address) for address in funding_snapshot.covered_wallets
                   if isinstance(address, str) and address}
        if covered != set(links):
            reasons.append("funding graph coverage declaration does not match relationship records")
        if not set(holders).issubset(covered):
            reasons.append("funding graph does not explicitly cover every token holder")

        root_keys = {_key(chain_key, root) for root in roots}
        linked_keys: set[str] = set()
        for holder_key in holders:
            current = holder_key
            visited: set[str] = set()
            for _ in range(self.max_ancestry_depth + 1):
                if current in root_keys:
                    linked_keys.add(holder_key)
                    break
                if current in visited:
                    reasons.append("funding graph contains an ancestry cycle")
                    break
                visited.add(current)
                edge = links.get(current)
                if edge is None:
                    reasons.append("funding ancestry is unresolved for a holder or intermediary")
                    break
                if edge.funder_address is None:
                    break
                current = _key(chain_key, edge.funder_address)
            else:
                reasons.append("funding ancestry exceeds configured depth")

        if reasons:
            return self._blocked(chain_key, token_address, now_ms, reasons,
                                 holder_snapshot.provider, funding_snapshot.provider, root_provider)

        linked_raw = sum(holders[key][1] for key in linked_keys)
        pct = linked_raw / holder_snapshot.total_supply_raw * 100.0
        evidence_id = (
            f"developer-graph:{holder_snapshot.evidence_id}:"
            f"{funding_snapshot.evidence_id}:{root_evidence_id}"
        )
        source_ref = f"{holder_snapshot.block_ref}|{funding_snapshot.block_ref}|{root_evidence_id}"
        return DeveloperConcentrationReport(
            chain=chain_key,
            token_address=token_address,
            provider=certified_provider_id,
            linked_wallets=tuple(sorted(holders[key][0] for key in linked_keys)),
            linked_supply_raw=linked_raw,
            total_supply_raw=holder_snapshot.total_supply_raw,
            dev_hold_pct=pct,
            coverage_complete=True,
            observed_at_ms=min(holder_snapshot.observed_at_ms, funding_snapshot.observed_at_ms),
            evidence_id=evidence_id,
            source_ref=source_ref,
            method="exhaustive_holder_balances_with_root_reachable_funding_ancestry",
            independence_group=independence_group,
            holder_provider=holder_snapshot.provider,
            funding_provider=funding_snapshot.provider,
            root_provider=root_provider,
            reasons=(),
        )

    @staticmethod
    def _same_token(chain: str, left: str, right: str) -> bool:
        if not isinstance(left, str) or not isinstance(right, str):
            return False
        return left.lower() == right.lower() if chain in {"base", "bsc"} else left == right

    @staticmethod
    def _blocked(chain, token, now, reasons, holder=None, funding=None, root=None):
        return DeveloperConcentrationReport(
            chain=chain, token_address=token if isinstance(token, str) else "",
            provider=None, linked_wallets=(), linked_supply_raw=None,
            total_supply_raw=None, dev_hold_pct=None, coverage_complete=False,
            observed_at_ms=now, evidence_id=None, source_ref=None,
            method="unverified_incomplete_holder_funding_graph",
            independence_group=None, holder_provider=holder,
            funding_provider=funding, root_provider=root,
            reasons=tuple(dict.fromkeys(reasons)),
        )
