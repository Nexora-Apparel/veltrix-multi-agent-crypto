from veltrix.agents import Sniper, Shield, Exit, Signal

def test_sniper_fails_closed_when_fields_missing():
    assert Sniper().evaluate({"address":"x"}) is None

def test_sniper_requires_clean_explicit_fields():
    t={"address":"x","liquidity_locked":True,"dev_hold_pct":4.9,"bundled_supply_pct":0}
    assert Sniper().evaluate(t).side == "BUY"
    t["bundled_supply_pct"]=0.1
    assert Sniper().evaluate(t) is None

def test_shield_limits_and_two_agent_consensus():
    signals=[Signal("scout","x","BUY","a"),Signal("whale","x","BUY","b")]
    gate=Shield()
    assert gate.approve(signals,0,50,10,50)
    assert not gate.approve(signals,10,50,10,50)
    assert not gate.approve(signals,0,50.01,10,50)
    assert not gate.approve(signals[:1],0,50,10,50)

def test_exit_rules():
    e=Exit()
    assert e.decisions(100,94)==[("stop_loss",1.0)]
    assert e.decisions(100,200)==[("take_2x",0.5)]
    assert e.decisions(100,300)==[("take_3x_and_trail",0.5)]
