"""Evidence-aware market signal context builder.

Scout volume anomalies are comparative heuristics over persisted snapshots,
not proof of organic demand or a trading recommendation.
"""
from statistics import median


class SignalContextBuilder:
    def __init__(self, min_history=5, volume_multiple=2.0, max_age_ms=120_000):
        if min_history < 3 or volume_multiple <= 1 or max_age_ms <= 0:
            raise ValueError("Invalid signal context thresholds")
        self.min_history = min_history
        self.volume_multiple = volume_multiple
        self.max_age_ms = max_age_ms

    def build(self, event, history, now_ms):
        """Return Scout-compatible context, or None when evidence is insufficient."""
        if not event.is_fresh(now_ms, self.max_age_ms):
            return None
        if event.price_usd <= 0 or event.liquidity_usd <= 0 or event.volume_h1_usd <= 0:
            return None
        samples = []
        for item in history:
            if item.get("source") != event.source or item.get("chain") != event.chain:
                continue
            if item.get("token") != event.token_address:
                continue
            try:
                value = float(item.get("volume_h1_usd"))
                observed = int(item.get("observed_at_ms"))
            except (TypeError, ValueError):
                continue
            age = now_ms - observed
            if 0 <= age <= 24 * 60 * 60 * 1000 and value > 0:
                samples.append(value)
        if len(samples) < self.min_history:
            return None
        baseline = median(samples)
        if baseline <= 0:
            return None
        ratio = event.volume_h1_usd / baseline
        return {
            "address": event.token_address,
            "chain": event.chain,
            "price": event.price_usd,
            "price_change_5m": event.price_change_m5_pct,
            "volume_spike": ratio >= self.volume_multiple,
            "volume_h1_usd": event.volume_h1_usd,
            "historical_median_h1_usd": baseline,
            "volume_multiple": ratio,
            "signal_source": event.source,
            "observed_at_ms": event.observed_at_ms,
            "signal_quality": "heuristic" if ratio >= self.volume_multiple else "no_anomaly",
        }
