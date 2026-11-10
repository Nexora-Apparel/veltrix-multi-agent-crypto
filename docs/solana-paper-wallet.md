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
