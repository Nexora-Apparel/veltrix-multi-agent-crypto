"""Launch-window shared-funder analysis for conservative bundle estimates.

A shared funder is a heuristic signal, not proof of insider activity. The
reported percentage is unknown unless the indexed source explicitly certifies
complete launch-window coverage and a positive total-supply denominator.
"""
from dataclasses import dataclass
import math
from typing import Iterable


SUPPORTED_CHAINS = frozenset({"solana", "base", "bsc"})


@dataclass(frozen=True)
class LaunchBuyEvidence:
    provider: str
    chain: str
    token_address: str
    wallet_address: str
    funder_address: str
    transaction_id: str
    token_amount: float
    observed_at_ms: int


@dataclass(frozen=True)
class LaunchBundleReport:
    chain: str
    token_address: str
    provider: str
    launch_at_ms: int
    window_end_ms: int
    coverage_complete: bool
    bundled_supply_pct: float | None
    shared_funder_clusters: int
    participating_wallets: int
    accepted_transactions: int
    rejected: tuple[str, ...]
    notes: tuple[str, ...]


class LaunchBundleAnalyzer:
    """Calculate a conservative shared-funder estimate from indexed launch buys."""

    def __init__(
        self,
        trusted_providers: Iterable[str] = (),
        launch_window_ms: int = 120_000,
        min_cluster_wallets: int = 2,
    ):
        if launch_window_ms <= 0 or min_cluster_wallets < 2:
            raise ValueError("launch window must be positive and cluster size at least two")
        self.trusted_providers = frozenset(trusted_providers)
        self.launch_window_ms = launch_window_ms
        self.min_cluster_wallets = min_cluster_wallets

    def analyze(
        self,
        *,
        provider: str,
        chain: str,
        token_address: str,
        launch_at_ms: int,
        total_supply: float | None,
        observations: Iterable[LaunchBuyEvidence],
        coverage_complete: bool,
        now_ms: int,
    ) -> LaunchBundleReport:
        chain_key = chain.lower() if isinstance(chain, str) else ""
        rejected: list[str] = []
        notes = [
            "Shared funding is a heuristic and does not prove developer or insider control.",
            "Only launch-window token amounts from qualifying shared-funder clusters are counted.",
        ]
        valid_scope = (
            isinstance(provider, str) and provider in self.trusted_providers
            and chain_key in SUPPORTED_CHAINS
            and isinstance(token_address, str) and bool(token_address.strip())
            and isinstance(launch_at_ms, int) and not isinstance(launch_at_ms, bool)
            and launch_at_ms <= now_ms
        )
        if not valid_scope:
            rejected.append("untrusted provider or invalid chain/token/launch scope")
            return self._report(chain_key, token_address, provider, launch_at_ms, coverage_complete,
                                None, 0, 0, 0, rejected, notes)

        window_end = launch_at_ms + self.launch_window_ms
        grouped: dict[str, dict[str, float]] = {}
        seen: set[tuple[str, str]] = set()
        accepted = 0
        for index, item in enumerate(observations):
            prefix = f"observation {index}"
            if not isinstance(item, LaunchBuyEvidence) or item.provider != provider:
                rejected.append(f"{prefix}: provider identity mismatch or malformed record")
                continue
            item_chain = item.chain.lower() if isinstance(item.chain, str) else ""
            if item_chain != chain_key or not self._same_address(chain_key, item.token_address, token_address):
                rejected.append(f"{prefix}: chain or token mismatch")
                continue
            if not all(isinstance(value, str) and value.strip() for value in (
                item.wallet_address, item.funder_address, item.transaction_id
            )):
                rejected.append(f"{prefix}: wallet, funder or transaction ID missing")
                continue
            if not isinstance(item.observed_at_ms, int) or isinstance(item.observed_at_ms, bool):
                rejected.append(f"{prefix}: invalid timestamp")
                continue
            if item.observed_at_ms < launch_at_ms or item.observed_at_ms > window_end or item.observed_at_ms > now_ms:
                rejected.append(f"{prefix}: outside launch window or future-dated")
                continue
            try:
                amount = float(item.token_amount)
            except (TypeError, ValueError):
                rejected.append(f"{prefix}: invalid token amount")
                continue
            if not math.isfinite(amount) or amount <= 0:
                rejected.append(f"{prefix}: token amount must be finite and positive")
                continue
            tx_key = (
                item_chain,
                item.transaction_id.lower() if item_chain in {"base", "bsc"} else item.transaction_id,
            )
            if tx_key in seen:
                rejected.append(f"{prefix}: duplicate transaction")
                continue
            seen.add(tx_key)
            funder = item.funder_address.lower() if chain_key in {"base", "bsc"} else item.funder_address
            wallet = item.wallet_address.lower() if chain_key in {"base", "bsc"} else item.wallet_address
            grouped.setdefault(funder, {})[wallet] = grouped.setdefault(funder, {}).get(wallet, 0.0) + amount
            accepted += 1

        clusters = [
            wallets for wallets in grouped.values()
            if len(wallets) >= self.min_cluster_wallets
        ]
        cluster_supply = sum(sum(wallets.values()) for wallets in clusters)
        participating = sum(len(wallets) for wallets in clusters)

        supply_pct: float | None = None
        try:
            supply = float(total_supply)
        except (TypeError, ValueError):
            supply = float("nan")
        if coverage_complete is True and not rejected and math.isfinite(supply) and supply > 0:
            if cluster_supply > supply:
                rejected.append("cluster token amount exceeds total supply; percentage withheld")
            else:
                supply_pct = (cluster_supply / supply) * 100.0
        else:
            rejected.append("complete launch-window coverage and valid total supply are required")
        if not clusters:
            notes.append("No qualifying multi-wallet shared-funder cluster was observed.")
        if coverage_complete is not True or rejected:
            notes.append("Coverage or evidence validation is incomplete; no bundle percentage is certified.")
        return self._report(chain_key, token_address, provider, launch_at_ms, coverage_complete,
                            supply_pct, len(clusters), participating, accepted, rejected, notes,
                            window_end)

    @staticmethod
    def _same_address(chain: str, left: str, right: str) -> bool:
        if not isinstance(left, str) or not isinstance(right, str):
            return False
        return left.lower() == right.lower() if chain in {"base", "bsc"} else left == right

    @staticmethod
    def _report(chain, token, provider, launch, coverage, pct, clusters,
                wallets, transactions, rejected, notes, window_end=None):
        return LaunchBundleReport(
            chain=chain, token_address=token, provider=provider,
            launch_at_ms=launch,
            window_end_ms=window_end if window_end is not None else launch,
            coverage_complete=coverage is True,
            bundled_supply_pct=pct,
            shared_funder_clusters=clusters,
            participating_wallets=wallets,
            accepted_transactions=transactions,
            rejected=tuple(rejected), notes=tuple(notes),
        )
