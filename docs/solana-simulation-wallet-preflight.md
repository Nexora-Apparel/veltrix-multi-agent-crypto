# Simulation-wallet preflight

Before asking Jupiter to assemble a sell transaction, the optional preflight
reader can query finalized Solana RPC state for the dedicated test wallet.

It reads:
- SPL Token and Token-2022 accounts for the exact mint and wallet owner
- Raw token balances summed across all matching accounts
- Native SOL balance in lamports
- Finalized context slots for both reads

Only an exact raw token balance at or above the configured simulation amount
produces `BALANCE_READY`. Otherwise the result is
`INSUFFICIENT_TOKEN_BALANCE` and the transaction-order request should be
skipped. SOL balance is recorded for diagnosis but is not given an invented
minimum threshold because fees and rent requirements vary by route.

The reader is read-only, does not sign or send transactions, and does not
interpret insufficient balance as a token restriction or honeypot verdict.
