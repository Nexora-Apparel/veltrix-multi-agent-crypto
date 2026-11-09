# Controlled sell-simulation worker

The simulation worker is separate from broad candidate discovery. It accepts only
an explicit operator-configured mapping of candidate mint to raw token amount.
It never attempts to sell every discovered token and does not infer wallet
balances from market data.

Configuration requires:
- `SOLANA_SELL_SIMULATION_ENABLED=true`
- `SOLANA_SELL_SIMULATION_TAKER`: dedicated test-wallet public key
- `SOLANA_SELL_SIMULATION_CASES_JSON`: JSON object mapping mint addresses to
  exact raw token amounts already held by that test wallet
- Existing `SOLANA_RPC_URL`; optional existing `JUPITER_API_KEY`

The public key is not a signing credential. No private key or seed phrase is
accepted by configuration. The test wallet must actually hold the configured
token amount and have required token accounts. Missing balances/accounts are
recorded as simulation failures, not automatically labelled honeypots.

Before each Jupiter order request, the worker reads finalized SPL Token and
Token-2022 balances for the exact mint and configured test wallet. If the exact
raw balance is below the requested amount, it stores a preflight observation,
skips Jupiter transaction assembly and defers the case. Native SOL balance is
recorded for diagnosis without inventing a universal minimum fee threshold.

The worker stores append-only preflight and simulation observations, uses bounded
batches and retry backoff, and refreshes only configured cases. It is disabled by default and
does not feed an entry approval or certified safety claim. A successful
simulation is a point-in-time RPC result, not a guarantee of transaction landing,
future liquidity, or profitability.
