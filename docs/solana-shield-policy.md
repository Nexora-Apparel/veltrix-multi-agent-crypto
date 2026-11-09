# Solana Shield Decision Gate

The Solana Shield policy is the final veto before the paper broker. It is not
an agent vote and cannot be overridden by Scout, Whale, Sniper, or another
agent.

## Current decision contract

- `BLOCK`: any required evidence, safety, freshness, consensus, or sizing
  condition fails. Unknown and missing evidence are blocked, not scored.
- `APPROVE_PAPER_ENTRY`: all required checks pass. This is a paper-only
  decision and is not a live execution authorisation.

## Entry requirements

- A typed risk-evidence report for the exact canonical Solana mint.
- No provider/validation errors and no unknown mandatory risk claims.
- A finalized mint-account snapshot with source identity and slot provenance.
- Mint and freeze authorities must both be explicitly revoked; an active
  authority vetoes entry.
- Token-2022 extensions must have a separate policy certification. The current
  implementation deliberately blocks all mints with extensions because their
  full payload semantics and extension authorities are not yet decoded.
- A complete holder inventory whose balances reconcile exactly to total supply.
- Fresh evidence and a fresh, positively verified composite safety certificate.
- At least two distinct BUY agents, all targeting the same mint.
- A fresh sell-side route quote for the exact intended raw-token exit size.
- Position count below 10 and paper notional in (0, $50].

There is no confidence-score averaging, fallback prior, or agent override.

The `executable_sellability` item is an execution-only unknown, not a token-risk
claim. It remains unresolved even when a paper route is observed. The route state
must remain `ROUTE_OBSERVED_NOT_EXECUTION_PROVEN`; Core preserves that state and
the route evidence ID in the paper trade result and broker audit reason. It does
not satisfy the live gate. Every other unknown mandatory token-risk claim
remains a paper-entry veto.

## Execution boundary

The only positive decision is `APPROVE_PAPER_ENTRY`. The module has no
wallet, key, signer, swap, transaction-submission, or live-trading interface.
The existing live-trading guard remains authoritative. A paper approval is not
evidence of expected profitability.

Synthetic complete reports in unit tests verify control flow only. They do not
certify provider coverage or prove real-world token safety. Provider-backed
validation and paper-broker integration remain separate acceptance gates.
