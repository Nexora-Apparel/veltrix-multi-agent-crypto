"""Adversarial tests for the read-only Jupiter Swap API V2 quote adapter."""
import copy
import pytest
from solders.pubkey import Pubkey

from veltrix.jupiter_quote import (
    JUPITER_ORDER_URL,
    JupiterQuoteError,
    JupiterQuotePolicy,
    JupiterSwapV2QuoteAdapter,
)


class FakeResponse:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def get(self, url, *, params, headers, timeout, allow_redirects):
        self.calls.append((url, params, headers, timeout, allow_redirects))
        assert url == JUPITER_ORDER_URL
        assert "taker" not in params
        assert params["swapMode"] == "ExactIn"
        assert allow_redirects is False
        return FakeResponse(self.body)


def quote_body():
    input_mint = str(Pubkey.new_unique())
    output_mint = str(Pubkey.new_unique())
    amm = str(Pubkey.new_unique())
    return {
        "requestId": "request-123",
        "quoteId": "quote-456",
        "mode": "ultra",
        "inputMint": input_mint,
        "outputMint": output_mint,
        "inAmount": "1000000",
        "outAmount": "2500000",
        "otherAmountThreshold": "2475000",
        "priceImpact": 0.35,
        "feeBps": 50,
        "feeMint": output_mint,
        "signatureFeeLamports": 5000,
        "prioritizationFeeLamports": 10000,
        "rentFeeLamports": 2039280,
        "gasless": False,
        "router": "metis",
        "transactionVersion": 0,
        "transaction": None,
        "taker": None,
        "slippageBps": 100,
        "routePlan": [
            {
                "swapInfo": {
                    "ammKey": amm,
                    "label": "Raydium CLMM",
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "inAmount": "1000000",
                    "outAmount": "2500000",
                },
                "percent": 100,
                "bps": 10000,
            }
        ],
    }


def adapter(body, *, api_key="test-jupiter-key"):
    return JupiterSwapV2QuoteAdapter(
        api_key=api_key,
        timeout_seconds=5.0,
        session=FakeSession(body),
        clock_ms=lambda: 1_005_000,
    )


def test_returns_quote_only_order_with_exact_mints_and_raw_amounts():
    body = quote_body()
    session = FakeSession(body)
    client = JupiterSwapV2QuoteAdapter(
        api_key="test-jupiter-key",
        timeout_seconds=5.0,
        session=session,
        clock_ms=lambda: 1_005_000,
    )
    quote = client.quote_exact_in(
        body["inputMint"], body["outputMint"], 1_000_000,
        policy=JupiterQuotePolicy(),
    )
    assert quote.provider_id == "jupiter-swap-v2"
    assert quote.request_id == "request-123"
    assert quote.input_amount_raw == 1_000_000
    assert quote.output_amount_raw == 2_500_000
    assert quote.minimum_output_amount_raw == 2_475_000
    assert quote.router == "metis"
    assert quote.route_plan[0].label == "Raydium CLMM"
    assert quote.observed_at_epoch_ms == 1_005_000
    assert session.calls[0][2]["x-api-key"] == "test-jupiter-key"
    assert session.calls[0][1]["amount"] == "1000000"
    assert "taker" not in session.calls[0][1]


def test_supports_keyless_quote_mode_without_api_key_header():
    body = quote_body()
    session = FakeSession(body)
    client = JupiterSwapV2QuoteAdapter(
        session=session,
        clock_ms=lambda: 1_005_000,
    )
    quote = client.quote_exact_in(
        body["inputMint"], body["outputMint"], 1_000_000,
        policy=JupiterQuotePolicy(),
    )
    assert quote.request_id == "request-123"
    assert "x-api-key" not in session.calls[0][2]


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda b: b.update(transaction="dGVzdA=="), "unexpectedly returned a transaction"),
        (lambda b: b.update(taker=str(Pubkey.new_unique())), "binds a taker"),
        (lambda b: b.update(router="unknown-router"), "outside policy"),
        (lambda b: b.update(inputMint=str(Pubkey.new_unique())), "mint pair differs"),
        (lambda b: b.update(inAmount="999"), "input amount differs"),
        (lambda b: b.update(otherAmountThreshold="2600000"), "threshold exceeds"),
        (lambda b: b.update(priceImpact=6.0), "price impact exceeds"),
        (lambda b: b.update(feeBps=301), "fee basis points exceed"),
        (lambda b: b.update(routePlan=[]), "routePlan is missing"),
        (lambda b: b.update(errorCode=2), "could not produce"),
        (lambda b: b.update(requestId=""), "omitted requestId"),
    ],
)
def test_rejects_invalid_or_unsafe_order_response(mutator, message):
    body = quote_body()
    mutator(body)
    client = adapter(body)
    with pytest.raises(JupiterQuoteError, match=message):
        client.quote_exact_in(
            quote_body()["inputMint"], quote_body()["outputMint"], 1_000_000,
            policy=JupiterQuotePolicy(),
        )


def test_rejects_route_plan_with_invalid_amm_public_key():
    body = quote_body()
    body["routePlan"][0]["swapInfo"]["ammKey"] = "not-a-public-key"
    with pytest.raises(JupiterQuoteError, match="ammKey"):
        adapter(body).quote_exact_in(
            body["inputMint"], body["outputMint"], 1_000_000,
            policy=JupiterQuotePolicy(),
        )


def test_rejects_route_step_with_nonpositive_raw_amount():
    body = quote_body()
    body["routePlan"][0]["swapInfo"]["outAmount"] = "0"
    with pytest.raises(JupiterQuoteError, match="must be positive"):
        adapter(body).quote_exact_in(
            body["inputMint"], body["outputMint"], 1_000_000,
            policy=JupiterQuotePolicy(),
        )


def test_rejects_excessive_route_step_count():
    body = quote_body()
    body["routePlan"] = body["routePlan"] * 2
    with pytest.raises(JupiterQuoteError, match="step limit"):
        adapter(body).quote_exact_in(
            body["inputMint"], body["outputMint"], 1_000_000,
            policy=JupiterQuotePolicy(max_route_steps=1),
        )


def test_rejects_invalid_request_amount_and_mint_pair():
    body = quote_body()
    client = adapter(body)
    with pytest.raises(JupiterQuoteError, match="positive integer"):
        client.quote_exact_in(body["inputMint"], body["outputMint"], 0, policy=JupiterQuotePolicy())
    with pytest.raises(JupiterQuoteError, match="must differ"):
        client.quote_exact_in(body["inputMint"], body["inputMint"], 1, policy=JupiterQuotePolicy())
    with pytest.raises(JupiterQuoteError, match="valid Solana public key"):
        client.quote_exact_in("bad-mint", body["outputMint"], 1, policy=JupiterQuotePolicy())


def test_rejects_router_not_in_local_policy():
    body = quote_body()
    policy = JupiterQuotePolicy(allowed_routers=("jupiterz",))
    with pytest.raises(JupiterQuoteError, match="outside policy"):
        adapter(body).quote_exact_in(
            body["inputMint"], body["outputMint"], 1_000_000,
            policy=policy,
        )


def test_rejects_malformed_fee_and_gas_fields():
    body = quote_body()
    body["prioritizationFeeLamports"] = "5000"
    with pytest.raises(JupiterQuoteError, match="non-negative integer"):
        adapter(body).quote_exact_in(
            body["inputMint"], body["outputMint"], 1_000_000,
            policy=JupiterQuotePolicy(),
        )


def test_rejects_redirects_and_http_errors():
    class BadSession:
        def get(self, *args, **kwargs):
            return type("Response", (), {"status_code": 302, "json": lambda self: {}})()

    body = quote_body()
    client = JupiterSwapV2QuoteAdapter(session=BadSession())
    with pytest.raises(JupiterQuoteError, match="HTTP 302"):
        client.quote_exact_in(
            body["inputMint"], body["outputMint"], 1_000_000,
            policy=JupiterQuotePolicy(),
        )


def test_rejects_invalid_policy_values():
    with pytest.raises(JupiterQuoteError, match="unknown Jupiter router"):
        JupiterQuotePolicy(allowed_routers=("axiom",))
    with pytest.raises(JupiterQuoteError, match="max_route_steps"):
        JupiterQuotePolicy(max_route_steps=0)
