# VELTRIX Multi-Agent Crypto

Paper-only research harness for Solana, Base and BSC. No wallet signing, private keys, swaps or live execution.

Agents: Scout (momentum), Whale (distinct smart-wallet buys), Sniper (fail-closed safety checks), Shield (risk limits), Exit (profit/stop decisions), Core (2-agent consensus).

Python 3.11+: `pip install -r requirements.txt`; copy `.env.example` to `.env`; run `python -m veltrix.main --hours 24`.

The current runner is a safe 24-hour harness, not a connected trading system. It does not fabricate market data or trades. Provider adapters and data semantics must be verified before paper performance is meaningful. `LIVE_TRADING=true` is rejected.