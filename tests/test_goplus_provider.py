import requests

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


def test_goplus_base_uses_documented_chain_id_and_creator_percent():
    token = "0xabc"
    provider = GoPlusTokenSafetyProvider(
        session=Session(payload(token, {"creator_percent": "0.03"})),
        clock_ms=lambda: 1234,
    )
    result = provider.fetch_safety_observation("base", token)
    assert result.dev_hold_pct is None
    assert result.creator_hold_pct == 3.0
    assert result.bundled_supply_pct is None
    assert provider.session.calls[0][0].endswith("/token_security/8453")


def test_goplus_rejects_api_error_response():
    provider = GoPlusTokenSafetyProvider(session=Session({"code": 0, "result": {}}))
    try:
        provider.fetch_safety_observation("bsc", "0xabc")
    except ProviderError:
        pass
    else:
        raise AssertionError("Expected provider error")


def test_goplus_bsc_maps_creator_fraction_to_percentage_points():
    token = "0xabc"
    provider = GoPlusTokenSafetyProvider(
        session=Session(payload(token, {"creator_percent": "0.049"})),
        clock_ms=lambda: 1234,
    )
    result = provider.fetch_safety_observation("bsc", token)
    assert result.dev_hold_pct is None
    assert result.creator_hold_pct == 4.9
    assert result.bundled_supply_pct is None


def test_goplus_bsc_rejects_invalid_creator_fraction_as_unknown():
    token = "0xabc"
    for value in ("5", "-0.1", "nan", "inf", None, "unknown"):
        provider = GoPlusTokenSafetyProvider(
            session=Session(payload(token, {"creator_percent": value}))
        )
        result = provider.fetch_safety_observation("bsc", token)
        assert result.dev_hold_pct is None


def test_goplus_sol_creator_percentage_remains_unknown_without_documented_field():
    token = "MintAddress"
    provider = GoPlusTokenSafetyProvider(
        access_token="secret",
        session=Session(payload(token, {"creator_percent": "0.01"})),
    )
    result = provider.fetch_safety_observation("solana", token)
    assert result.dev_hold_pct is None



def test_goplus_transport_errors_do_not_echo_endpoint_or_access_token():
    secret_url = "https://user:password@goplus.test/private?api-key=private-secret"

    class BrokenSession:
        def get(self, url, **kwargs):
            raise requests.ConnectionError(f"connection failed for {secret_url}")

    provider = GoPlusTokenSafetyProvider(
        access_token="private-secret",
        base_url=secret_url,
        session=BrokenSession(),
    )
    try:
        provider.fetch_safety_observation("bsc", "0xabc")
    except ProviderError as error:
        assert "private-secret" not in str(error)
        assert "password" not in str(error)
        assert "ConnectionError" in str(error)
    else:
        raise AssertionError("Expected transport failure")
