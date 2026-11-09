# Solana Shield Decision Gate

The Solana Shield is a final evidence veto before the paper broker. It is not
an agent vote and cannot be overridden by Scout, Whale, Sniper, or another
agent. The current implementation exposes a claim-aware evidence decision
method; the broker integration remains a separate task.

## Decision states

- `BLOCK`: the evidence object is absent/untyped or an explicit adverse
  condition is present, including a provider-confirmed liquidity-lock failure.
- `HOLD`: mandatory claims are unknown, evidence is stale or incomplete,
  holder balances do not reconcile, or provider/validation errors exist.
- `READY_FOR_AGENT_CONSENSUS`: the evidence packet is structurally complete.
  This is not a BUY signal and does not authorize a paper or live entry.

## Evidence requirements

- Evidence must be typed and tied to the exact canonical Solana mint.
- GoPlus and holder inventory must carry independent provenance and timestamps.
- Holder concentration is usable only when inventory is marked complete and
  observed balances reconcile exactly to total supply.
- Developer-wallet linkage, bundled-supply analysis, mint/freeze authority,
  Token-2022 extension review, creator provenance, and executable sellability
  must each be separately evidenced. Unknown is never treated as safe.
- Explicit liquidity-lock failure causes BLOCK.

## Paper and execution boundary

The existing generic Shield consensus/position-limit method is retained for
legacy compatibility. The new Solana evidence decision is an additional veto
and must be composed with consensus, paper sizing, concurrent-position limits,
and emergency-stop checks before a paper broker can accept an order.

This stage does not authorize live execution. Wallet custody, transaction
simulation, swap routing, and signing remain outside this module. A paper
approval, when implemented downstream, is not evidence of profitability.

## Current operational implication

The current collector deliberately reports several mandatory claims as
unknown. Therefore the expected decision for real candidates is HOLD until
separate collectors provide complete, fresh, source-provenanced evidence.
Synthetic complete reports in unit tests verify control flow only; they do not
certify providers or prove real-world token safety.
