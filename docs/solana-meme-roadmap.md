# VELTRIX Solana Meme-Coin Delivery Roadmap

All stages are sequential. A stage is not complete until code, tests and evidence are recorded. Live trading stays disabled throughout these stages.

| Stage | Deliverable | Exit evidence |
|---|---|---|
| 0 | Repository and provider audit | Inventory of active Solana code, legacy EVM code, existing secrets by name only, CI and known gaps |
| 1 | Solana-only product boundary | Runtime configuration rejects Base/BSC in the active product path; legacy modules remain isolated, not deleted |
| 2 | Candidate discovery | Multi-source Solana launches/pairs, canonical mint validation, deduplication, freshness and provenance |
| 3 | Market intelligence | Price, liquidity, volume, trade flow, pool age, quote impact and sell-route feasibility with timestamps |
| 4 | Token risk intelligence | Mint/freeze authority, Token-2022 extensions, holder distribution, creator/developer exposure, launch bundles and liquidity evidence; unknown fails closed |
| 5 | Multi-agent decision engine | Scout, Whale and Sniper produce independent evidence-bound signals; Core requires two distinct same-mint BUY votes |
| 6 | Deterministic risk manager | Shield enforces max $50 position, max 10 positions, 6% initial stop, liquidity/impact limits and global paper kill switch |
| 7 | Paper execution simulator | Realistic quote-based fills, fees, priority fees, slippage, price impact, latency, failed fills and restart-safe exits |
| 8 | 24-hour paper operation | Continuous run using $500 virtual equity; machine-readable report, event log and failure summary |
| 9 | Strategy evaluation | Net-of-cost performance, drawdown, expectancy, sample size, win/loss distribution and benchmark comparison |
| 10 | Execution security design | Separate threat model and review for dedicated wallet, isolated signer, transaction inspection, limits and kill switch; no automatic enablement |

## Previously agreed paper profile

- Virtual starting balance: $500.
- Maximum position: $50.
- Maximum open positions: 10.
- Initial stop loss: 6%.
- Entry consensus: two distinct agents on the same token.
- Initial continuous paper run: 24 hours.

## Current implementation baseline

The existing repository already contains a multi-agent Core, Scout/Whale/Sniper/Shield/Exit logic, a SQLite paper ledger, DexScreener read-only snapshots, Helius/Alchemy wallet intelligence, GoPlus safety connectors, launch-intelligence and composite-safety gates, plus a large Base V2 archive/factory validation subsystem. The latter is legacy scope for the Solana-only product and must be isolated rather than allowed to control Solana acceptance.

## Current verified state at roadmap creation

- Repository: Nexora-Apparel/veltrix-multi-agent-crypto.
- Base branch: main.
- Main commit audited: 77c0d63c12f38d909d266494df9b24fe3d5cc72f.
- CI run 42664670151 completed successfully (compile and pytest job).
- Existing live-trading guard rejects LIVE_TRADING=true.
- No live signing or swap execution is implemented in the current paper-only architecture.

## Stage 0 unresolved audit items

- Enumerate every active Solana discovery adapter and its actual coverage/limitations.
- Review the full discovery monitor to determine whether candidate feeds create paper positions or only persist observations.
- Inventory required provider secrets by name, never values; reuse configured credentials before requesting any new one.
- Establish which providers are genuinely available for Solana launches, holder inventory, token authorities and swap quotes.
- Inspect all current tests and CI workflow triggers before changing project-wide configuration.

## Release discipline

- Work on the dedicated solana-meme-first branch; do not overwrite main during the rebuild.
- Preserve useful existing evidence, provider adapters and paper ledger unless tests prove them unsafe or irrelevant.
- Do not add credentials to source, commit history, issue comments or chat.
- Do not call an API integration complete until a real read-only provider check succeeds.
- Do not call a strategy profitable without out-of-sample, net-of-cost evidence.
- Never enable live trading as a side effect of configuration or deployment.