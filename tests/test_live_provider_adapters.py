from veltrix.providers.goplus import GoPlusTokenSafetyProvider
from veltrix.providers.helius import HeliusWalletProvider
from veltrix.providers.intelligence import VerifiedIntelligenceAdapters


class Response:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


class HttpSession:
    def __init__(self, payload):
        self.payload = payload
    def get(self, url, **kwargs):
        return Response(self.payload)
    def post(self, url, **kwargs):
        return Response(self.payload)


def test_helius_observations_enter_whale_adapter_only_when_helius_is_allowlisted():
    tx = {
        "signature": "sig",
        "blockTime": 1000,
        "summary": {"type": "swap"},
        "tokenTransfers": [{"mint": "mint", "toUserAccount": "wallet",
                            "fromUserAccount": "pool", "tokenAmount": "20"}],
    }
    provider = HeliusWalletProvider(
        "key", ["wallet"], lambda mint: 1.5,
        session=HttpSession({"data": [tx]}), clock_ms=lambda: 1_001_000,
    )
    allowed = VerifiedIntelligenceAdapters(trusted_wallet_providers=["helius"])
    batch = allowed.collect_wallet_observations(provider, "solana", ["mint"], 900_000, 1_001_000)
    assert len(batch.accepted) == 1
    denied = VerifiedIntelligenceAdapters().collect_wallet_observations(
        provider, "solana", ["mint"], 900_000, 1_001_000
    )
    assert denied.accepted == ()
    assert "whale: provider identity is not trusted" in denied.rejected


def test_goplus_provider_cannot_pass_sniper_without_dev_and_bundle_evidence():
    token = "0xabc"
    response = {"code": 1, "result": {token: {
        "lp_holder_count": "1", "lp_holders": [{"is_locked": 1}],
        "holders": [{"tag": "deployer", "percent": 0.001}],
    }}}
    provider = GoPlusTokenSafetyProvider(
        session=HttpSession(response), clock_ms=lambda: 1_000_000
    )
    adapters = VerifiedIntelligenceAdapters(trusted_safety_providers=["goplus"])
    report = adapters.collect_safety_observation(provider, "bsc", token, 1_000_100)
    decision = adapters.sniper_decision(report)
    assert not decision.accepted
    assert report.checks == ("liquidity_locked",)
    assert "developer holdings are missing or not below 5%" in report.reasons
    assert "bundled supply is missing or non-zero" in report.reasons


def test_creator_concentration_is_preserved_but_does_not_satisfy_dev_gate():
    token = "0xabc"
    response = {"code": 1, "result": {token: {
        "lp_holder_count": "1", "lp_holders": [{"is_locked": 1}],
        "creator_percent": "0.02",
    }}}
    provider = GoPlusTokenSafetyProvider(
        session=HttpSession(response), clock_ms=lambda: 1_000_000
    )
    adapters = VerifiedIntelligenceAdapters(trusted_safety_providers=["goplus"])
    report = adapters.collect_safety_observation(provider, "bsc", token, 1_000_100)
    assert not report.passed
    assert "developer holdings are missing or not below 5%" in report.reasons
