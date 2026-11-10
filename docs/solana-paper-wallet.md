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
- Marks must be supplied in lamports per token. No USD/SOL conversion is
  inferred; the caller must later provide a fresh, provenance-checked SOL
  quote conversion before this wallet is connected to the consensus worker.

## Explicit phase boundary

This is a virtual accounting component only. It does not contain a keypair,
wallet signer, Solana RPC transaction builder, or transaction broadcast
method. It is not yet wired into consensus-approved paper execution. That
integration is gated on a verified, timestamped SOL/USD conversion and
market quote adapter, plus fenced transaction integration.

## Tests

`pytest -q tests/test_solana_meme_sol_paper_wallet.py`


# SOL quote conversion boundary

The native SOL paper wallet accepts only integer lamports per token. This
adapter converts an existing token USD snapshot using a separate, fresh SOL/USD
market observation from a stable-quoted Solana pool.

Acceptance requirements:
- Token USD snapshot and SOL/USD observation must both be no older than the
  configured maximum age and cannot be future-dated.
- SOL/USD observations must be WSOL token pairs quoted in USD, USDC or USDT,
  with a non-empty provider source, sufficient liquidity and hourly volume.
- The most liquid eligible stable-quoted WSOL pool is selected.
- Conversion uses Decimal arithmetic and rounds to the nearest lamport.
- Missing, stale, malformed or illiquid conversion data blocks SOL paper entry;
  there is no cached, fixed, synthetic or inferred SOL/USD fallback.

This adapter is a quote primitive only. It is not yet wired into the consensus
paper worker. The next integration step must preserve Phase 06 approval checks,
use the existing orchestration RunContext fence for all SOL wallet writes,
and persist quote provenance with each fill.


# Native SOL consensus paper worker

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
inside the same transaction. This worker is not yet enabled in the default
pipeline runtime; explicit runtime integration and full end-to-end acceptance
remain required.
