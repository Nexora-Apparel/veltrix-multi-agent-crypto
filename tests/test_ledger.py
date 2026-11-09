import pytest
from veltrix.ledger import PaperBroker

def test_paper_ledger_open_partial_and_close(tmp_path):
    broker=PaperBroker(str(tmp_path/"paper.db"),500,10,50)
    opened=broker.open_position("mint1",2.0,50,"two-agent consensus")
    assert opened["quantity"]==25
    half=broker.close_fraction("mint1",4.0,0.5,"2x partial")
    assert half["realized_pnl"]==25
    assert half["remaining_quantity"]==12.5
    final=broker.close_fraction("mint1",6.0,1.0,"trailing exit")
    assert final["realized_pnl"]==50
    assert broker.snapshot()["cash"]==575
    assert broker.totals()["realized_pnl"]==75
    assert broker.snapshot()["open_positions"]==[]

def test_paper_ledger_enforces_position_cap(tmp_path):
    broker=PaperBroker(str(tmp_path/"paper.db"),500,1,50)
    broker.open_position("mint1",1,50,"test")
    with pytest.raises(ValueError): broker.open_position("mint2",1,50,"test")
    with pytest.raises(ValueError): broker.open_position("mint1",1,50,"duplicate")

def test_exit_milestones_are_persisted_and_only_trigger_once(tmp_path):
    path=str(tmp_path/"exits.db")
    broker=PaperBroker(path,500,10,50)
    broker.open_position("mint",10,50,"consensus")
    first=broker.process_market_price("mint",20)
    assert len(first)==1 and first[0]["reason"]=="take_profit_2x"
    assert broker.process_market_price("mint",20)==[]
    # Reopen broker to prove milestone state survives process restarts.
    broker=PaperBroker(path,500,10,50)
    second=broker.process_market_price("mint",30)
    assert len(second)==1 and second[0]["reason"]=="take_profit_3x"
    assert broker.process_market_price("mint",30)==[]
    # 6% trailing drawdown from the $30 high closes the remaining quarter.
    closed=broker.process_market_price("mint",28.1)
    assert len(closed)==1 and closed[0]["reason"]=="trailing_stop"
    assert broker.snapshot()["open_positions"]==[]
    assert broker.totals()["closed_sell_fills"]==3

def test_initial_stop_loss_closes_entire_position(tmp_path):
    broker=PaperBroker(str(tmp_path/"stop.db"),500,10,50)
    broker.open_position("mint",100,50,"consensus")
    result=broker.process_market_price("mint",94)
    assert len(result)==1 and result[0]["reason"]=="stop_loss"
    assert result[0]["remaining_quantity"]==0
    assert broker.snapshot()["open_positions"]==[]

def test_snapshot_marks_open_positions_to_market(tmp_path):
    broker=PaperBroker(str(tmp_path/"mark.db"),500,10,50)
    broker.open_position("mint",10,50,"consensus")
    snap=broker.snapshot({"mint":12})
    assert snap["unrealized_pnl"]==10
    assert snap["equity"]==510

def test_session_report_summarizes_closed_positions(tmp_path):
    broker=PaperBroker(str(tmp_path/"report.db"),500,10,50)
    broker.open_position("winner",10,50,"consensus")
    broker.close_fraction("winner",20,1,"take profit")
    broker.open_position("loser",10,50,"consensus")
    broker.close_fraction("loser",5,1,"stop")
    report=broker.session_report()
    assert report["closed_positions"]==2
    assert report["win_rate_pct"]==50
    assert report["best_trade"]["token"]=="winner"
    assert report["worst_trade"]["token"]=="loser"
