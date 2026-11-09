# VELTRIX Solana Meme-Coin Architecture

Status: Architecture baseline — implementation in progress.
Scope: Solana meme coins only. Base and BSC are legacy/isolated and are not part of the active trading path.
Trading mode: paper-only. Live swaps and wallet signing remain disabled.

## Product objective

Continuously discover Solana meme coins, enrich candidates with market and on-chain evidence, reject unsafe or untradeable tokens, rank the remaining opportunities, simulate entries and exits, and record auditable performance. The system must return NO TRADE when evidence is incomplete, liquidity is inadequate, execution costs erase expected edge, or risk limits are reached. No profit is guaranteed.

## Wallet and terminal model

- Phantom is the user-controlled funding wallet and optional interactive approval/monitoring interface. Never request, store, or transmit its recovery phrase or private key.
- A future dedicated VELTRIX trading wallet must be isolated and deliberately funded. It is not created or used in this paper-only milestone.
- Axiom and Photon are optional human-facing terminals. Neither is a required dependency for discovery, strategy, or the paper broker.
- Jupiter is the preferred future swap-routing integration. Its current Swap API V2 offers Meta-Aggregator /order + /execute and Router /build + /submit. An unsigned transaction still requires a wallet signer. No signing key belongs in an AI agent or ordinary application configuration.
- Solana RPC and indexers provide read-only chain data, transaction confirmation, account state and historical evidence.

## Logical architecture

Solana launch / token / market feeds -> discovery adapters -> candidate normalization and deduplication -> evidence collection and provenance -> independent safety gates -> Core multi-agent decision -> Shield risk veto -> SQLite paper broker -> stateful Exit manager -> audit reports and Android dashboard.

Future-only boundary (disabled): approved paper strategy -> execution policy -> isolated signer -> Jupiter -> Solana.

## Agent responsibilities

- Core: orchestration, token identity binding, evidence freshness, same-token consensus and decision audit.
- Scout: launch and market-momentum discovery. A signal is not proof of safety.
- Whale: independently sourced smart-wallet activity and cluster quality. Ignore caller-supplied wallet claims.
- Sniper: strict token-safety gate requiring claim-separated, independently trusted evidence. Missing or unknown evidence means reject.
- Shield: deterministic exposure, liquidity, slippage, concentration, loss and concurrency controls. It can veto every agent.
- Exit: stateful paper position management, partial exits, stop/trailing-stop logic and durable recovery.
- Agents cannot sign transactions or override Shield.

## Discovery and evidence rules

1. Solana is the only active chain.
2. Discovery feeds produce leads, not trade approvals.
3. Normalize and deduplicate by canonical mint; validate base58/public-key form.
4. Record provider identity, observation time, slot/signature, coverage boundary and evidence digest.
5. Reject stale, future-dated, malformed, mismatched-mint or incomplete evidence.
6. Distinguish unknown from zero. Missing safety evidence is never interpreted as safe.
7. Evaluate mint/freeze authority, Token-2022 extensions, holder concentration, creator exposure, bundled launch supply, liquidity depth, pool age, volume quality, wallet clusters and sellability.
8. A buy quote alone does not prove a token can be sold. Validate sell route feasibility where supported.
9. Recheck price, liquidity and risk evidence immediately before each paper fill.
10. Persist rejected candidates and reasons to measure coverage and false negatives.

## Paper-trading baseline

- Starting virtual equity: $500.
- Maximum position notional: $50.
- Maximum concurrent open positions: 10.
- Initial stop: 6% below entry.
- Entry requires at least two distinct BUY agents agreeing on the same mint.
- Persist decisions, quotes, simulated fills, fees, slippage assumptions, exits and equity snapshots.
- Model fees, priority fees, slippage, price impact, latency and failed execution. Never assume an optimistic fill.
- Run a continuous 24-hour paper session only after discovery and evidence gates are configured. This is an operational smoke test, not proof of profitability.
- Existing code has 2x/3x partial-exit and trailing-stop behavior. Reconcile this explicitly with the agreed baseline before approving it as strategy policy.

## Execution security boundary

No live execution in this milestone. A later execution phase requires a separate review: dedicated wallet only; isolated signer/KMS or hardware-backed custody; no secret exposure to prompts, agents, logs or source; decode and inspect transactions before signing; enforce token and quote policies, slippage, price impact, fee ceilings, daily loss and position caps; simulate and confirm final chain state; implement a global kill switch and define open-position handling; require explicit enablement after prospective paper validation.

## Service boundaries

- discovery: launch, profile and pool feeds; normalized candidates.
- market_data: price, volume, liquidity, trades and quote snapshots.
- chain_data: RPC, token accounts, authorities, transactions and slots.
- evidence: provider provenance, freshness, coverage and trust policies.
- agents: Scout, Whale, Sniper and Core.
- risk: deterministic Shield checks.
- paper: fill model, broker, positions and durable ledger.
- execution: future adapter interface only; no signing implementation in paper phase.
- api/dashboard: read-only candidates, decisions, positions, status and performance.

## Acceptance gates

1. Repository audited; legacy EVM modules isolated from the active Solana path.
2. Solana-only discovery and candidate normalization tested.
3. Market-data and on-chain adapters tested against stale, error and partial-data cases.
4. Safety and risk gates fail closed under adversarial fixtures.
5. Agent consensus and Shield decisions tested.
6. Paper fill model and restart-safe position lifecycle tested.
7. A 24-hour paper session completed with a machine-readable report.
8. Performance evaluated net of fees, slippage, priority fees and failed fills over a meaningful sample; no profitability claim from a short smoke test.
9. Separate execution security review before any live capability.

## Explicit exclusions

- No Base or BSC meme-coin scanning in the active product.
- No real-money trading, wallet signing, private-key loading or automatic deposits.
- No dependency on Axiom or Photon private endpoints.
- No token safety claim based solely on a third-party score.
- No automatic strategy tuning from a tiny or unverified sample.