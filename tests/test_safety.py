from veltrix.safety import SafetyEvidence, TokenSafetyValidator

def evidence(**overrides):
    data = dict(token_address="mintA", provider="audit-provider", evidence_id="audit-123",
                observed_at_ms=1_000_000, liquidity_locked=True, dev_hold_pct=4.99, bundled_supply_pct=0.0)
    data.update(overrides)
    return SafetyEvidence(**data)

def test_safety_passes_only_with_all_verified_checks():
    validator = TokenSafetyValidator(["audit-provider"])
    report = validator.validate("mintA", evidence(), 1_000_001)
    assert report.passed
    assert len(report.checks) == 3
    assert not report.reasons

def test_missing_evidence_fails_closed():
    report = TokenSafetyValidator(["audit-provider"]).validate("mintA", None, 1_000_000)
    assert not report.passed

def test_untrusted_provider_fails_closed():
    report = TokenSafetyValidator().validate("mintA", evidence(), 1_000_001)
    assert not report.passed
    assert "evidence provider is not trusted" in report.reasons

def test_token_mismatch_and_stale_evidence_fail():
    validator = TokenSafetyValidator(["audit-provider"], max_age_ms=100)
    report = validator.validate("other-mint", evidence(), 1_000_500)
    assert not report.passed
    assert "evidence token address mismatch" in report.reasons
    assert "safety evidence is stale or timestamp is invalid" in report.reasons

def test_thresholds_are_strict():
    validator = TokenSafetyValidator(["audit-provider"])
    assert not validator.validate("mintA", evidence(dev_hold_pct=5), 1_000_001).passed
    assert not validator.validate("mintA", evidence(bundled_supply_pct=0.01), 1_000_001).passed
    assert not validator.validate("mintA", evidence(liquidity_locked=False), 1_000_001).passed
