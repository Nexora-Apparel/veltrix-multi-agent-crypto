# Solana Shield Decision Gate

The Solana Shield policy is the final veto before the paper broker. It is not
an agent vote and cannot be overridden by Scout, Whale, Sniper, or another
agent.

## Current decision contract

- `BLOCK`: any required evidence, safety, freshness, consensus, or sizing
  condition fails. Unknown and missing evidence are blocked, not scored.
- `APPROVE_PAPER_ENTRY`: all required checks pass. This is a paper-only
  decision and is not a live execution authorisation.

The current policy returns BLOCK whenever mandatory risk claims remain unknown.
The collector deliberately leaves developer-wallet linkage, bundled supply,
mint/freeze authority, Token-2022 extensions, creator provenance, and executable
sellability unresolved. Consequently, the real collector currently blocks
candidate entries.

## Entry requirements

- A typed risk-evidence report for the exact canonical Solana mint.
- No provider/validation errors and no unknown mandatory risk claims.
- A complete holder inventory whose balances reconcile exactly to total supply.
- Fresh evidence and a fresh, positively verified composite safety certificate.
- At least two distinct BUY agents, all targeting the same mint.
- Position count below 10 and paper notional in (0, $50].

There is no confidence-score averaging, fallback prior, or agent override.

## Execution boundary

The only positive decision is `APPROVE_PAPER_ENTRY`. The module has no
wallet, key, signer, swap, transaction-submission, or live-trading interface.
The existing live-trading guard remains authoritative. A paper approval is not
evidence of expected profitability.

Synthetic complete reports in unit tests verify control flow only. They do not
certify provider coverage or prove real-world token safety. Provider-backed
validation and paper-broker integration remain separate acceptance gates.
