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
  public keys. Empty by default.

No wallet allowlist or wallet-buy feed is fabricated. Without both, Whale
abstains.

When explicitly configured, the runtime connects the existing Helius Parsed
Events provider to the Whale agent. Required settings are:
- `HELIUS_API_KEY`: provider credential.
- `TRUSTED_WALLET_PROVIDERS=helius`: explicit provider trust decision.
- `VELTRIX_TRUSTED_SOLANA_WALLETS`: comma-separated operator-approved wallet
  public keys.
- `VELTRIX_HELIUS_WALLET_SCAN_LIMIT`: maximum allowlisted wallets scanned per
  candidate (default 10, permitted 2–25).

The runtime refuses to enable Helius if the provider identity is not trusted,
the API key is missing, the wallet allowlist is empty, or the allowlist exceeds
the configured scan bound. Helius observations are normalized through the
existing verified-intelligence adapter before reaching Whale. USD buy notional
is estimated using the latest persisted spot price, not historical execution
price; this approximation is not treated as proof of trade profitability.
Provider request failures enter the worker retry path. Missing credentials or
trust configuration leave Whale in abstention mode.

## Phase boundary

Phase 06 ends at a persisted consensus decision. Paper-trading execution,
position sizing, entry/exit simulation, portfolio accounting and P&L remain
exclusively in Phase 07. Dashboard work remains Phase 08. Live wallet signing
and swaps remain disabled in Phase 09.
