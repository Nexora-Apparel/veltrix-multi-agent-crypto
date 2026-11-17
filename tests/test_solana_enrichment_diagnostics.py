from scripts.inspect_solana_enrichment_failures import redact_error


def test_redacts_bearer_tokens_and_urls():
    value = "request failed Bearer abc.def https://host.example/path?api_key=secret"
    result = redact_error(value)
    assert "abc.def" not in result
    assert "https://host.example" not in result
    assert "[URL]" in result
    assert "Bearer [REDACTED]" in result


def test_redacts_solana_addresses():
    address = "So11111111111111111111111111111111111111112"
    result = redact_error(f"no usable pair for {address}")
    assert address not in result
    assert "[ADDRESS]" in result
