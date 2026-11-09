from veltrix.whales import WalletBuyEvent, WhaleFlowAggregator

def test_only_allowlisted_fresh_wallets_form_cluster():
    events=[
        WalletBuyEvent("solana","mint","w1",9900,20,"rpc"),
        WalletBuyEvent("solana","mint","w2",9950,30,"rpc"),
        WalletBuyEvent("solana","mint","unknown",9950,999,"rpc"),
        WalletBuyEvent("solana","old", "w1",1,100,"rpc"),
    ]
    clusters=WhaleFlowAggregator(["w1","w2"],window_ms=1000,min_buy_usd=10).aggregate(events,10000)
    assert len(clusters)==1
    assert clusters[0].token_address=="mint"
    assert clusters[0].total_notional_usd==50

def test_single_wallet_does_not_count_as_cluster():
    event=WalletBuyEvent("base","token","w1",100,25,"indexer")
    assert WhaleFlowAggregator(["w1"]).aggregate([event],100)==[]
