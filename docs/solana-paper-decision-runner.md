# Solana paper decision runner

## Implemented component

`veltrix/solana_meme/decision_runner.py` now contains
`SolanaPaperDecisionRunner`. It joins persisted launch, market, risk,
developer-ancestry, launch-bundle, sell-route and composite-safety evidence,
reconstructs typed reports, and calls the existing paper-only Core.

The runner requires every evidence store and the durable PaperBroker to use
the same persistent SQLite database. Each candidate/evidence packet is claimed
with a deterministic key before Core is called. The audit stores source record
IDs, provider evidence IDs, the decision state, reasons and the Core result.
A repeated packet is skipped rather than evaluated again.

## Entry requirements

- Fresh market snapshot (maximum age 120 seconds), positive USD price.
- Fresh risk, developer, bundle and composite-safety evidence (maximum age
  300 seconds).
- Fresh Jupiter sell route (maximum age 15 seconds), unexpired quote, and
  `ROUTE_OBSERVED_NOT_EXECUTION_PROVEN` state.
- Finalized mint supply and decimals reconciled against the complete holder
  inventory.
- No unknown mandatory token-risk claims. The sole tolerated unknown is
  `executable_sellability`; it remains in the original report and is never
  re-labelled as proven.
- Complete developer ancestry and launch-bundle reports with valid provenance.
- A typed, passed, composite-verified `SafetyReport` for the same mint.
- A separately configured agent-context provider whose provider ID is
  explicitly allowlisted. Context must be typed, fresh and mint-matched.
- Core must independently produce at least two distinct BUY agents for the
  same mint. Its Shield recomputes the raw sell amount from the $50 paper
  notional, current market price, finalized decimals and supply.
- The PaperBroker enforces the $500 starting balance, $50 maximum position,
  ten-position cap, duplicate-open-position rule and existing 6% stop profile.

## Failure and crash behaviour

Missing, malformed, stale, future-dated, mismatched or untrusted evidence is
recorded as BLOCKED/FAILED_CLOSED. The runner does not invent evidence or turn
provider metadata into a BUY signal.

The deterministic claim is persisted before Core is invoked. If the process
crashes after claiming a packet, that exact packet is not replayed
automatically. `reconcile_stale_claims()` inspects stale claims against the
paper broker: a matching position opened after the claim is marked
`PAPER_ENTRY_OPENED_RECONCILED`; otherwise it is marked
`INDETERMINATE_NO_REPLAY` for manual reconciliation. Neither outcome triggers
automatic re-entry. A new evidence packet has a new key and can be evaluated
independently. This deliberately favours preventing duplicate entries over
silently retrying an uncertain broker outcome.

## Current activation blockers

The runner is implemented and unit-tested but is **not wired into the live
read-only runtime**. It has no default safety or agent-context provider.

- `SolanaSafetyReportStore` only persists typed reports; it does not create
  certificates. The current capability catalogue marks GoPlus Solana
  liquidity-lock evidence unsupported and the available developer-cluster
  evidence partial. No production Solana composite-safety report can currently
  pass the default capability validator.
- Added SolanaMarketAgentContextProvider, which derives a Scout volume-spike
  flag from at least five prior observations of the same selected pool and
  attaches a content-addressed evidence ID. It never invents smart-wallet BUYs;
  the Whale agent remains inactive unless a separate wallet-evidence source is
  supplied. The market-context provider still requires explicit allowlisting
  and has not been wired into runtime settings.

These are evidence-provider gaps, not reasons to weaken Shield. Runtime
activation is withheld until both provider paths are independently verified
and their acceptance tests pass.

## Safety boundary

The runner has no wallet, private key, signer, RPC-write, Jupiter `/execute`,
or transaction-submission interface. A paper route quote is not a transfer
test or proof of live sellability. No live trading is enabled.


## Crash-recovery verification

- Added stale-claim reconciliation against the durable PaperBroker ledger.
- A stale claim with a matching newly opened position is annotated as reconciled.
- A stale claim without a matching new position is marked indeterminate and remains non-replayable.
- Both outcomes are audit-only; no automatic retry or second position is created.


## Market-derived agent context

SolanaMarketAgentContextProvider derives momentum from the latest persisted
market snapshot and compares hourly volume with the median of five prior
observations for the same selected pool. A pool change, stale snapshot,
insufficient baseline or zero baseline volume cannot produce a volume-spike
signal. Smart-wallet BUY evidence is always empty in this adapter; it does not
claim to replace an independent wallet-intelligence provider.

The provider is suitable for the Scout signal only. The second BUY agent must
still be independently satisfied (for example, Sniper after a separately
certified composite safety report). The provider ID must be explicitly
allowlisted by the runner.
