from veltrix.providers.goplus import GoPlusTokenSafetyProvider
from veltrix.providers.base import ProviderError


class Response:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


class Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.payload)


def payload(token, item):
    return {"code": 1, "message": "OK", "result": {token: item}}


def test_goplus_connector_maps_complete_locked_lp_holders_but_leaves_unknowns():
    token = "0xabc"
    session = Session(payload(token, {
        "lp_holder_count": "2",
        "lp_holders": [{"is_locked": 1}, {"is_locked": "1"}],
        "holders": [{"tag": "deployer", "percent": 0.25}],
    }))
    provider = GoPlusTokenSafetyProvider(session=session, clock_ms=lambda: 1234)
    result = provider.fetch_safety_observation("bsc", token)
    assert result.provider == "goplus"
    assert result.liquidity_locked is True
    assert result.dev_hold_pct is None
    assert result.bundled_supply_pct is None
    assert result.observed_at_ms == 1234
    assert session.calls[0][0].endswith("/token_security/56")


def test_goplus_connector_does_not_claim_lock_from_partial_top_ten():
    token = "0xabc"
    session = Session(payload(token, {
        "lp_holder_count": "18",
        "lp_holders": [{"is_locked": 1}, {"is_locked": 1}],
    }))
    result = GoPlusTokenSafetyProvider(session=session).fetch_safety_observation("bsc", token)
    assert result.liquidity_locked is None


def test_goplus_connector_marks_unlocked_lp_holder_false():
    token = "0xabc"
    session = Session(payload(token, {
        "lp_holder_count": "2",
        "lp_holders": [{"is_locked": 1}, {"is_locked": 0}],
    }))
    result = GoPlusTokenSafetyProvider(session=session).fetch_safety_observation("bsc", token)
    assert result.liquidity_locked is False


def test_goplus_solana_requires_bearer_access_token():
    provider = GoPlusTokenSafetyProvider(session=Session({}))
    try:
        provider.fetch_safety_observation("solana", "mint")
    except ProviderError as exc:
        assert "requires an access token" in str(exc)
    else:
        raise AssertionError("Expected missing GoPlus token to fail closed")


def test_goplus_rejects_unsupported_base_until_documented():
    provider = GoPlusTokenSafetyProvider(session=Session({}))
    try:
        provider.fetch_safety_observation("base", "0xabc")
    except ProviderError as exc:
        assert "does not support" in str(exc)
    else:
        raise AssertionError("Undocumented chain support must not be assumed")


def test_goplus_rejects_api_error_response():
    provider = GoPlusTokenSafetyProvider(session=Session({"code": 0, "result": {}}))
    try:
        provider.fetch_safety_observation("bsc", "0xabc")
    except ProviderError:
        pass
    else:
        raise AssertionError("Expected provider error")
