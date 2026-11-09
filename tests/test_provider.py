import math

import pytest

from veltrix.providers.dexscreener import DexScreenerProvider
from veltrix.providers.base import ProviderError


def pair(**overrides):
    payload = {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": "pool-1",
        "baseToken": {"address": "So11111111111111111111111111111111111111112",
                      "name": "Wrapped SOL", "symbol": "SOL"},
        "quoteToken": {"address": "USD", "symbol": "USDC"},
        "priceUsd": "142.50",
        "liquidity": {"usd": 250000},
        "volume": {"m5": 1000, "h1": 50000, "h24": 900000},
        "priceChange": {"m5": 2.5, "h1": -1.2, "h24": 8.4},
        "txns": {
            "m5": {"buys": 12, "sells": 7},
            "h1": {"buys": 180, "sells": 90},
            "h24": {"buys": 1500, "sells": 900},
        },
        "fdv": 1_000_000_000,
        "marketCap": 950_000_000,
        "pairCreatedAt": 1_700_000_000_000,
    }
    payload.update(overrides)
    return payload


class Response:
    def __init__(self, payload, error=None):
        self.payload = payload
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


class Session:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.urls = []

    def get(self, url, timeout):
        self.urls.append((url, timeout))
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return Response(payload)


def test_normalizes_solana_market_intelligence_fields():
    mint = "So11111111111111111111111111111111111111112"
    provider = DexScreenerProvider(
        session=Session([[pair()]]),
        clock_ms=lambda: 1_800_000_000_000,
    )

    event = provider.fetch_token_pairs("solana", [mint])[0]

    assert event.token_address == mint
    assert event.price_usd == 142.5
    assert event.liquidity_usd == 250000
    assert event.volume_h1_usd == 50000
    assert event.volume_m5_usd == 1000
    assert event.volume_h24_usd == 900000
    assert event.price_change_m5_pct == 2.5
    assert event.price_change_h1_pct == -1.2
    assert event.price_change_h24_pct == 8.4
    assert (event.txns_h1_buys, event.txns_h1_sells) == (180, 90)
    assert event.fdv_usd == 1_000_000_000
    assert event.market_cap_usd == 950_000_000
    assert event.name == "Wrapped SOL"
    assert event.dex_id == "raydium"
    assert event.quote_token_symbol == "USDC"
    assert event.observed_at_ms == 1_800_000_000_000


def test_rejects_unsupported_chain():
    provider = DexScreenerProvider(session=Session([[]]))
    with pytest.raises(ProviderError, match="Unsupported chain"):
        provider.fetch_token_pairs("unknown", ["x"])


def test_ignores_other_chain_pairs_and_unrequested_tokens():
    mint = "So11111111111111111111111111111111111111112"
    other = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    provider = DexScreenerProvider(session=Session([[
        pair(chainId="base"),
        pair(baseToken={"address": other, "symbol": "OTHER"}),
    ]]))
    assert provider.fetch_token_pairs("solana", [mint]) == []


def test_batches_at_most_thirty_addresses_and_deduplicates_inputs():
    addresses = [f"mint-{index}" for index in range(31)]
    session = Session([[], []])
    provider = DexScreenerProvider(session=session)
    provider.fetch_token_pairs("solana", addresses + [addresses[0]])

    assert len(session.urls) == 2
    first = session.urls[0][0].rsplit("/", 1)[1].split(",")
    second = session.urls[1][0].rsplit("/", 1)[1].split(",")
    assert len(first) == 30
    assert len(second) == 1


def test_empty_address_batch_makes_no_request():
    session = Session([])
    assert DexScreenerProvider(session=session).fetch_token_pairs("solana", []) == []
    assert session.urls == []


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "bad"])
def test_skips_individual_pairs_with_invalid_numeric_values(value):
    mint = "So11111111111111111111111111111111111111112"
    valid = pair()
    invalid = pair(liquidity={"usd": value})
    provider = DexScreenerProvider(session=Session([[invalid, valid]]))
    events = provider.fetch_token_pairs("solana", [mint])
    assert len(events) == 1
    assert events[0].liquidity_usd == 250000


def test_rejects_invalid_transaction_counts_without_discarding_other_pairs():
    mint = "So11111111111111111111111111111111111111112"
    invalid = pair(txns={"h1": {"buys": 1.5, "sells": 3}})
    valid = pair(pairAddress="pool-valid")
    events = DexScreenerProvider(session=Session([[invalid, valid]])).fetch_token_pairs(
        "solana", [mint]
    )
    assert [event.pair_address for event in events] == ["pool-valid"]


def test_provider_fails_closed_on_bad_response_envelope():
    with pytest.raises(ProviderError, match="response shape"):
        DexScreenerProvider(session=Session([{"pairs": []}])).fetch_token_pairs(
            "solana", ["So11111111111111111111111111111111111111112"]
        )


def test_provider_wraps_transport_failures():
    with pytest.raises(ProviderError, match="request failed"):
        DexScreenerProvider(session=Session([OSError("offline")])).fetch_token_pairs(
            "solana", ["So11111111111111111111111111111111111111112"]
        )


def test_market_event_freshness_rejects_future_and_invalid_timestamps():
    from veltrix.providers.base import MarketEvent

    event = DexScreenerProvider._normalize(
        pair(), "solana", 1000
    )
    assert event is not None
    assert event.is_fresh(1000)
    assert not event.is_fresh(999)
    assert not event.is_fresh(True)
