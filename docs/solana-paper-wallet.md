# SOL-denominated paper wallet

Phase 07 currently includes a legacy USD paper broker. This module adds a
separate SOL-denominated virtual wallet foundation without silently converting
USD marks or mixing currencies.

## Accounting contract

- The virtual account balance, trade notional, fees and realized/unrealized
  P&L are integer lamports (1 SOL = 1,000,000,000 lamports).
- Token quantities are stored as decimal strings and calculations use
  `Decimal`, never binary floating point.
- A token position is keyed by canonical Solana mint; only one open position
  per mint is allowed in this initial implementation.
- Every fill has a unique idempotency key. Account debit/credit, position
  mutation and fill insertion are committed atomically.
- Restarting the process preserves the initial balance, cash, positions,
  fills and realized P&L. Supplying a different initial balance on restart
  never resets an existing account.
- The integrated quote adapter derives lamports per token from a fresh token
  USD mark and a separately verified SOL/USD observation. It persists the
  complete source, pool, timestamp and market-snapshot provenance with each
  fill; missing or stale conversion evidence blocks entry.

## Explicit phase boundary

This is a virtual accounting component only. It does not contain a keypair,
wallet signer, Solana RPC transaction builder, or transaction broadcast
method. The native SOL paper worker is integrated with consensus-approved
paper execution and the orchestration RunContext fence. It is disabled by
default and requires the explicit `SOL_PAPER_TRADING_ENABLED=true` runtime
flag. Legacy USD paper trading and native SOL paper trading cannot be enabled
at the same time.

## Tests

`pytest -q tests/test_solana_meme_sol_paper_wallet.py`


# SOL quote conversion boundary

The native SOL paper wallet accepts only integer lamports per token. The quote
adapter converts a fresh token USD snapshot using either a fresh WSOL/stablecoin
pool or a fresh token/WSOL pool. For the latter it derives SOL/USD as
token-price-USD divided by token-price-SOL from the same provider observation.

Acceptance requirements:
- Token USD snapshot and SOL/USD observation must both be no older than the
  configured maximum age and cannot be future-dated.
- SOL/USD observations must be WSOL token pairs quoted in the canonical
  Solana USDC or USDT mint (not a symbol-only match), with a non-empty provider
  source, sufficient liquidity and hourly volume. Unknown tokens labelled
  `USD`, spoofed stablecoin symbols and unverified quote mints are rejected.
- The most liquid eligible stable-quoted WSOL pool is preferred. If that
  endpoint has no eligible pool, a token/WSOL pair may be used only when its
  quote mint is the canonical WSOL mint and the provider supplies a valid
  native token price; the conversion method is persisted with the fill.
- Conversion uses Decimal arithmetic and rounds to the nearest lamport.
- Missing, stale, malformed or illiquid conversion data blocks SOL paper entry;
  there is no cached, fixed, synthetic or inferred SOL/USD fallback.

This adapter is a quote primitive only. It is not yet wired into the consensus
paper worker. The next integration step must preserve Phase 06 approval checks,
use the existing orchestration RunContext fence for all SOL wallet writes,
and persist quote provenance with each fill.


# Native SOL consensus paper worker

The quote audit record validates both token and SOL sources, pool addresses,
positive USD marks, observation timestamps and market snapshot ID before a
fill can be committed.

The native SOL paper worker reuses the Phase 06 approval validator and persists
fills in the separate lamport-native wallet. It refuses to proceed unless the
consensus, market, risk, legacy validation broker, quote adapter and SOL wallet
share one persistent database.

Entries preserve the configured USD risk budget but are settled in SOL:
token quantity is derived from the token USD mark, while the token's lamport
price is derived from the same persisted market snapshot and a separate fresh
SOL/USD pool observation. Fill provenance stores both sources, timestamps,
pool addresses and snapshot ID. Any changed snapshot, stale conversion, or
missing provider evidence blocks entry.

Exits use the wallet's native 6% initial stop, 2x and 3x one-time 50% partial
take-profit milestones, and 6% trailing stop after the 3x milestone. Every
wallet write accepts the global orchestration RunContext and checks its fence
inside the same transaction. The worker is integrated into the default pipeline builder but remains
disabled unless `SOL_PAPER_TRADING_ENABLED=true`. The scheduled GitHub
workflow keeps it disabled unless the repository variable is explicitly
enabled. Full end-to-end provider-to-ledger acceptance remains outstanding.
