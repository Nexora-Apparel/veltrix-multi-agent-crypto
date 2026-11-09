# Solana Shield Decision Gate

The Solana Shield policy is a final veto before the paper broker. It is not an
agent vote and cannot be overridden by Scout, Whale, Sniper, or any other agent.

## Entry requirements

- A typed risk-evidence report for the exact canonical Solana mint.
- No provider/validation errors and no unknown mandatory risk claims.
- A complete holder inventory whose balances reconcile exactly to supply.
- Fresh evidence and a fresh, positively verified composite safety certificate.
- At least two distinct BUY agents, all targeting the same mint.
- Position count below 10 and paper notional in (0, $50].

Any failed or unknown condition returns BLOCK. There is no confidence-score
averaging and no fallback approval.

## Execution boundary

The only positive decision is `APPROVE_PAPER_ENTRY`. This module has no
wallet, key, signer, swap, transaction-submission, or live-trading interface.
The existing live-trading guard remains authoritative. A paper approval is not
evidence of expected profitability.

## Current operational implication

The current evidence collector deliberately reports unknown developer linkage,
bundled supply, mint/freeze authority, Token-2022 extensions, creator provenance,
and executable sellability. Therefore the live implementation correctly blocks
entries until those claims are separately implemented and verified.
