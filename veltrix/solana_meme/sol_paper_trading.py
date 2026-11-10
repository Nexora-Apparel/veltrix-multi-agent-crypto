"""Consensus-gated native SOL paper execution; never submits Solana transactions."""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path

from .paper_trading import PaperTradingBatchResult, PaperTradingWorker
from .sol_paper_quote import SolanaPaperQuoteAdapter
from .sol_paper_wallet import SolanaPaperWallet


class SolanaNativePaperTradingWorker:
    """Reuse Phase 06 approval validation with a lamport-native virtual wallet."""

    def __init__(
        self, validator: PaperTradingWorker, wallet: SolanaPaperWallet,
        quote_adapter: SolanaPaperQuoteAdapter,
    ) -> None:
        paths = {
            str(Path(p).expanduser().resolve())
            for p in (
                validator.consensus_store.database_path,
                validator.market_store.database_path,
                validator.risk_store.database_path,
                validator.broker.db_path,
                wallet.database_path,
                quote_adapter.market_store.database_path,
            )
        }
        if len(paths) != 1:
            raise ValueError("native SOL paper worker components must share one persistent database")
        self.validator = validator
        self.wallet = wallet
        self.quote_adapter = quote_adapter
        self.policy = validator.policy
        self.consensus_store = validator.consensus_store
        self.market_store = validator.market_store

    @staticmethod
    def _provenance(quote) -> dict:
        return {
            "mint": quote.mint,
            "market_snapshot_id": quote.market_snapshot_id,
            "token_pair_address": quote.token_pair_address,
            "sol_usd_pair_address": quote.sol_usd_pair_address,
            "token_price_usd": str(quote.token_price_usd),
            "sol_price_usd": str(quote.sol_price_usd),
            "token_observed_at_ms": quote.token_observed_at_ms,
            "sol_observed_at_ms": quote.sol_observed_at_ms,
            "token_source": quote.token_source,
            "sol_source": quote.sol_source,
        }

    def _process_exits(self, now_ms: int, context=None) -> tuple[int, int]:
        fills = errors = 0
        positions = self.wallet.snapshot().get("open_positions", [])
        for position in positions:
            mint = position["mint"]
            try:
                quote = self.quote_adapter.quote(mint)
                latest = self.market_store.latest(mint)
                if latest is None or latest.get("id") != quote.market_snapshot_id:
                    errors += 1
                    continue
                actions = self.wallet.process_market_price(
                    mint=mint,
                    price_lamports=quote.price_lamports,
                    market_snapshot_id=quote.market_snapshot_id,
                    fee_bps=self.policy.fee_bps,
                    slippage_bps=self.policy.slippage_bps,
                    created_at_ms=now_ms,
                    context=context,
                    quote_provenance=self._provenance(quote),
                )
                fills += len(actions)
            except Exception as exc:
                from ..orchestration import LeaseBusy
                if isinstance(exc, LeaseBusy):
                    raise
                errors += 1
        return fills, errors

    def run_once(self, *, limit: int = 25, context=None) -> PaperTradingBatchResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 250:
            raise ValueError("limit must be between 1 and 250")
        now_ms = self.validator.clock_ms()
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms <= 0:
            raise ValueError("clock_ms must return a positive integer")
        exit_fills, errors = self._process_exits(now_ms, context)
        opened = skipped = blocked = duplicates = 0
        decisions = self.consensus_store.latest_all(limit=limit)
        for decision in decisions:
            decision_id = decision.get("decision_id")
            if not isinstance(decision_id, str) or not decision_id.strip():
                errors += 1
                continue
            if self.wallet.has_fill_key(decision_id):
                duplicates += 1
                continue
            if decision.get("status") != "CONSENSUS_APPROVED":
                skipped += 1
                continue
            market, reason = self.validator._validate_approval(decision, now_ms)
            if reason is not None or market is None:
                blocked += 1
                continue
            try:
                quote = self.quote_adapter.quote(decision["mint"])
                if quote.market_snapshot_id != market["id"]:
                    blocked += 1
                    continue
                if quote.token_observed_at_ms != market["retrieved_at_ms"]:
                    blocked += 1
                    continue
                # Preserve the configured USD risk budget while settling the
                # simulated position entirely in SOL lamports.
                quantity = Decimal(str(self.policy.position_usd)) / quote.token_price_usd
                buy_slippage = Decimal(str(self.policy.slippage_bps)) / Decimal(10_000)
                fill_price = int(
                    (Decimal(quote.price_lamports) * (1 + buy_slippage)).to_integral_value(
                        rounding=ROUND_HALF_UP
                    )
                )
                notional = int((quantity * fill_price).to_integral_value(rounding=ROUND_DOWN))
                fee = int(
                    (Decimal(notional) * Decimal(str(self.policy.fee_bps)) / Decimal(10_000))
                    .to_integral_value(rounding=ROUND_DOWN)
                )
                result = self.wallet.open_position(
                    idempotency_key=decision_id,
                    mint=decision["mint"],
                    quantity=quantity,
                    price_lamports=fill_price,
                    fee_lamports=fee,
                    created_at_ms=now_ms,
                    context=context,
                    quote_provenance=self._provenance(quote),
                )
                if result["status"] == "OPENED":
                    opened += 1
                elif result["status"] == "DUPLICATE":
                    duplicates += 1
                else:
                    blocked += 1
            except Exception as exc:
                from ..orchestration import LeaseBusy
                if isinstance(exc, LeaseBusy):
                    raise
                if isinstance(exc, ValueError) and any(
                    phrase in str(exc) for phrase in (
                        "insufficient simulated SOL", "exposure cap", "maximum SOL paper positions",
                        "already exists",
                    )
                ):
                    blocked += 1
                else:
                    errors += 1
        return PaperTradingBatchResult(
            scanned_decisions=len(decisions),
            opened_positions=opened,
            skipped_decisions=skipped,
            blocked_entries=blocked,
            duplicate_decisions=duplicates,
            exit_fills=exit_fills,
            errors=errors,
        )

    def status(self) -> dict:
        marks = {}
        unmarked = []
        for position in self.wallet.snapshot().get("open_positions", []):
            mint = position["mint"]
            try:
                quote = self.quote_adapter.quote(mint)
                marks[mint] = quote.price_lamports
            except Exception:
                unmarked.append(mint)
        account = self.wallet.snapshot(marks)
        account["unmarked_positions"] = sorted(set(account["unmarked_positions"]) | set(unmarked))
        return {
            "account": account,
            "unit": "SOL",
            "execution_mode": "paper_only",
            "live_execution": False,
        }
