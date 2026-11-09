# Exact Solana paper-exit sizing

`calculate_paper_exit_size` converts a USD-denominated paper position into
an exact raw-token amount for a sell-side quote. It is a deterministic sizing
utility; it does not request a quote, submit a transaction or authorise a trade.

## Inputs

- Canonical Solana mint
- Paper notional in USD
- Positive token USD price
- Finalized mint decimals
- Finalized raw total supply
- Configured supply-fraction bounds

The sizing calculation is:

`raw_amount = floor((notional_usd / token_price_usd) * 10^decimals)`

Decimal arithmetic is used and the result is rounded down, never up. The
effective notional after raw-unit rounding is returned separately.

The default size band is 0.0001% to 5% of raw total supply. A result outside
that band is rejected rather than silently widened or clipped. The quote
evaluator and Shield use the same lower/upper bounds.

## Supply provenance

Risk evidence now carries the finalized mint raw supply and decimals. It
compares both values against the holder inventory snapshot. Shield requires
that reconciliation before paper entry. A mismatch, missing mint snapshot or
unknown decimals remains a hard veto.

## Safety boundary

- This utility only calculates an amount.
- A quote for that amount remains
  `ROUTE_OBSERVED_NOT_EXECUTION_PROVEN`.
- A quote does not prove transferability, transaction landing or live
  sellability.
- The paper broker and live-execution gate remain separate.
