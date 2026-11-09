from veltrix.solana_meme.dexscreener import DexScreenerDiscovery

MINT_A = "So11111111111111111111111111111111111111112"
MINT_B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class FakeResponse:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.status_checked = False

    def raise_for_status(self):
        self.status_checked = True
        if self.error:
            raise self.error

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    def get(self, url, *, timeout):
        self.calls.append((url, timeout))
        path = url.split("dexscreener.test", 1)[-1]
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response


def test_fetches_latest_profiles_and_boosts_and_filters_to_solana():
    session = FakeSession({
        "/token-profiles/latest/v1": FakeResponse([
            {"chainId": "solana", "tokenAddress": MINT_A},
            {"chainId": "base", "tokenAddress": MINT_B},
            {"chainId": "solana", "tokenAddress": "not-a-mint"},
            {"tokenAddress": MINT_B},
        ]),
        "/token-boosts/latest/v1": FakeResponse({
            "chainId": "solana", "tokenAddress": MINT_B, "amount": 10,
        }),
    })
    adapter = DexScreenerDiscovery(
        session=session,
        base_url="https://dexscreener.test",
        clock_ms=lambda: 123456,
    )

    batch = adapter.fetch_candidates()

    assert len(session.calls) == 2
    assert all(timeout == 10 for _, timeout in session.calls)
    assert batch.successful_sources == (
        "dexscreener-latest-profile",
        "dexscreener-latest-boost",
    )
    assert batch.errors == ()
    assert len(batch.candidates) == 2
    first = next(item for item in batch.candidates if item.mint == MINT_A)
    second = next(item for item in batch.candidates if item.mint == MINT_B)
    assert first.source == "dexscreener-latest-profile"
    assert first.discovered_at_ms == 123456
    assert second.sources == (
        "dexscreener-latest-boost",
        "dexscreener-latest-profile",
    )


def test_supports_single_object_response_shape():
    session = FakeSession({
        "/token-profiles/latest/v1": FakeResponse(
            {"chainId": "solana", "tokenAddress": MINT_A}
        ),
        "/token-boosts/latest/v1": FakeResponse([]),
    })
    batch = DexScreenerDiscovery(
        session=session, base_url="https://dexscreener.test",
        clock_ms=lambda: 123456,
    ).fetch_candidates()
    assert len(batch.candidates) == 1
    assert batch.candidates[0].mint == MINT_A


def test_partial_endpoint_failure_does_not_discard_successful_candidates():
    session = FakeSession({
        "/token-profiles/latest/v1": OSError("profile endpoint offline"),
        "/token-boosts/latest/v1": FakeResponse([
            {"chainId": "solana", "tokenAddress": MINT_A}
        ]),
    })
    batch = DexScreenerDiscovery(
        session=session, base_url="https://dexscreener.test",
        clock_ms=lambda: 123456,
    ).fetch_candidates()

    assert len(batch.candidates) == 1
    assert batch.successful_sources == ("dexscreener-latest-boost",)
    assert batch.errors[0][0] == "dexscreener-latest-profile"
    assert "offline" in batch.errors[0][1]


def test_invalid_response_shape_is_reported_as_source_error():
    session = FakeSession({
        "/token-profiles/latest/v1": FakeResponse("unexpected"),
        "/token-boosts/latest/v1": FakeResponse([]),
    })
    batch = DexScreenerDiscovery(
        session=session, base_url="https://dexscreener.test",
        clock_ms=lambda: 123456,
    ).fetch_candidates()
    assert batch.successful_sources == ("dexscreener-latest-boost",)
    assert batch.errors[0][0] == "dexscreener-latest-profile"
    assert "response shape" in batch.errors[0][1]


def test_rejects_invalid_timeout():
    import pytest

    with pytest.raises(ValueError):
        DexScreenerDiscovery(timeout_seconds=0)

def test_rejects_object_response_without_token_fields():
    session = FakeSession({
        "/token-profiles/latest/v1": FakeResponse({"message": "upstream error"}),
        "/token-boosts/latest/v1": FakeResponse([]),
    })
    batch = DexScreenerDiscovery(
        session=session, base_url="https://dexscreener.test",
        clock_ms=lambda: 123456,
    ).fetch_candidates()
    assert batch.errors[0][0] == "dexscreener-latest-profile"
    assert "token fields" in batch.errors[0][1]
