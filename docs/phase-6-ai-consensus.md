# Phase 06 — Evidence-Gated Multi-Agent Consensus

## Scope

Phase 06 combines Phase 04 market intelligence and Phase 05 security evidence
into one deterministic multi-agent analytical disposition.

The current agents are deterministic, evidence-backed rules. No external
generative LLM is allowed to invent token facts or override a security veto.
The engine does not create paper positions, submit swaps, sign transactions or
authorize live execution.

## Agents

### Scout
Uses `SignalContextBuilder` to compare current 1-hour volume with at least
five prior persisted snapshots for the same mint and source. It supports a
candidate only when a comparative volume anomaly and positive 5-minute price
movement are both present. This is a heuristic, not proof of organic demand.

### Whale
Uses the Solana-only `SolanaWhaleFlowAggregator` over explicitly allowlisted Solana wallet-buy
observations. At least two distinct allowlisted wallets must buy the same mint
inside the configured time window. Unknown wallets, stale events, malformed
addresses, duplicate observations and non-Solana events are excluded. An empty
allowlist produces abstentions, not inferred smart-wallet activity.

### Sniper
Checks fresh market evidence, pool age, selected-pool liquidity, 1-hour volume,
market data-quality flags and complete risk evidence. It does not simulate a
swap or guarantee executable depth.

### Shield
Has veto authority:
- Critical/high risk flags produce `REJECT`.
- Stale risk evidence, provider errors, missing claims or medium flags produce
  `ABSTAIN`.
- It approves only when the configured risk evidence is complete and no
  medium/high/critical flags remain.

The existing position-limit Shield and Exit position-management rules are not
used in Phase 06; position management belongs to Phase 07.

## Consensus dispositions

- `REJECTED`: Shield vetoed on critical/high security evidence.
- `WATCH`: at least one analysis agent supports the candidate, but Shield
  cannot approve because evidence is missing, stale or requires review.
- `ABSTAIN`: insufficient fresh evidence or insufficient agent support.
- `CONSENSUS_APPROVED`: Shield approved and at least two of Scout, Whale and
  Sniper support the candidate.

A `CONSENSUS_APPROVED` result is only eligible for later paper-review. It is
not a BUY instruction and never authorizes execution. Every stored decision
has `execution_authorized=false`.

## Evidence and persistence

The worker requires:
- A Phase 04 market snapshot.
- A Phase 05 risk report.
- The same persistent SQLite database used by Phases 03–05.

Market history excludes the current snapshot from its baseline. Market and risk
evidence are freshness-checked independently. Decisions are append-only and
deduplicated by deterministic decision ID.

## Runtime

Start with:

```bash
python -m veltrix.solana_meme.consensus_runtime
```

Configuration:
- `VELTRIX_SOLANA_DISCOVERY_DB`: shared persistent SQLite database.
- `VELTRIX_CONSENSUS_POLL_SECONDS`: poll interval (default 30).
- `VELTRIX_CONSENSUS_REFRESH_INTERVAL_MS`: per-mint refresh cadence
  (default 60000).
- `VELTRIX_CONSENSUS_BATCH_SIZE`: candidates per cycle (default 10, maximum 25).
- `VELTRIX_TRUSTED_SOLANA_WALLETS`: comma-separated, operator-approved wallet
  public keys. Empty by default. `TRACKED_SMART_WALLETS` is accepted as a fallback.

No wallet allowlist or wallet-buy feed is fabricated. Without both, Whale
abstains.

When explicitly configured, the runtime connects the existing Helius Parsed
Events provider to the Whale agent. Required settings are:
- `HELIUS_API_KEY`: provider credential.
- `TRUSTED_WALLET_PROVIDERS=helius`: explicit provider trust decision.
- `VELTRIX_TRUSTED_SOLANA_WALLETS`: comma-separated operator-approved wallet
  public keys.
- `VELTRIX_HELIUS_WALLET_SCAN_LIMIT`: maximum allowlisted wallets scanned per
  consensus batch (default 10, permitted 2–25).

The runtime refuses to enable Helius if the provider identity is not trusted,
the API key is missing, the wallet allowlist is empty, or the allowlist exceeds
the configured scan bound. The scan bound is validated before runtime construction.
One bounded Helius request batch covers all claimed
candidate mints in a consensus cycle. Helius observations are normalized through
the existing verified-intelligence adapter before reaching Whale. USD buy
notional is estimated using a fresh persisted spot price, not historical
execution price; this approximation is not treated as proof of trade
profitability. Provider failures yield no Whale support for that cycle and are
retried on the next polling cycle. Missing credentials or trust configuration
leave Whale in abstention mode.

## Phase boundary

Phase 06 ends at a persisted consensus decision. Paper-trading execution,
position sizing, entry/exit simulation, portfolio accounting and P&L remain
exclusively in Phase 07. Dashboard work remains Phase 08. Live wallet signing
and swaps remain disabled in Phase 09.


## Stage 6 exit criteria

- [x] Scout, Whale, Sniper and Shield emit one typed vote each in deterministic order.
- [x] Market and risk evidence are reconstructed and revalidated before analysis; malformed or forged inputs fail closed.
- [x] Shield approval requires review-ready risk state, no missing claims/errors and no medium/high/critical flags.
- [x] Consensus persistence revalidates all votes, support counts, evidence references, disposition and eligibility, then recomputes the deterministic decision ID.
- [x] Every persisted decision has `execution_authorized=false`; only `CONSENSUS_APPROVED` can be marked eligible for a later paper review.
- [x] Provider exception text is excluded from wallet-feed logs and retry records.
- [x] Discovery, market-intelligence, risk and consensus runtime builders enforce the shared production/Telegram/scheduling/live-trading release locks.
- [x] Full repository CI and adversarial tests pass on the exact Stage 6 PR head (834 passed, 5 skipped; compileall passed).
- [ ] Post-merge CI and deterministic decision replay/store tests pass on main.
- [x] Review confirms no wallet allowlist was fabricated, no generative LLM can override Shield, and no paper-trading, Telegram, scheduling, production deployment or execution capability was activated.

Stage 6 implements deterministic specialist agents and an evidence-gated consensus policy; it does not claim a trained predictive model or generative LLM.

The Whale agent remains abstention-only until an operator explicitly configures a trusted provider and approved wallet allowlist. This is an intentional evidence boundary, not a reason to infer wallet identities.
