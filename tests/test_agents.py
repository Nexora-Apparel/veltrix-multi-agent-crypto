from veltrix.agents import Sniper, Shield, Exit, Signal
from veltrix.safety import SafetyEvidence, TokenSafetyValidator

def passing_report():
    evidence = SafetyEvidence("x", "trusted", "evidence-1", 1000, True, 4.5, 0.0)
    return TokenSafetyValidator(["trusted"]).validate("x", evidence, 1001)

def test_sniper_fails_closed_when_report_missing():
    assert Sniper().evaluate({"address":"x"}) is None

def test_sniper_requires_validator_report():
    assert Sniper().evaluate({"address":"x", "liquidity_locked":True, "dev_hold_pct":1, "bundled_supply_pct":0}) is None
    assert Sniper().evaluate({"address":"x", "safety_report":passing_report()}).side == "BUY"

def test_sniper_rejects_report_for_another_token():
    assert Sniper().evaluate({"address":"y", "safety_report":passing_report()}) is None

def test_shield_limits_and_same_token_consensus():
    signals=[Signal("scout","x","BUY","a"),Signal("whale","x","BUY","b")]
    gate=Shield()
    assert gate.approve(signals,0,50,10,50)
    assert not gate.approve(signals,10,50,10,50)
    assert not gate.approve(signals,0,50.01,10,50)
    assert not gate.approve(signals[:1],0,50,10,50)
    assert not gate.approve([signals[0],Signal("whale","different","BUY","b")],0,50,10,50)

def test_exit_rules_and_invalid_fraction():
    e=Exit()
    assert e.decisions(100,94)==[("stop_loss",1.0)]
    assert e.decisions(100,200)==[("take_2x",0.5)]
    assert e.decisions(100,300)==[("take_3x_and_trail",0.5)]
    assert e.decisions(100,200,0.5)==[("take_2x",0.25)]
    assert e.decisions(100,200,0)==[]
