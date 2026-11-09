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
