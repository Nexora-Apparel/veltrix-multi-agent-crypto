# Phase 09 — Legacy/canonical differential regression status

**Snapshot date:** 2026-11-14  
**Scope:** Historical Python transition compatibility against the canonical Phase 09 replay primitives.  
**Execution status:** Live execution remains disabled.

## CI-verified regression chain

All counts below are GitHub Actions results for the corresponding PR head commits. Each run completed the Python compilation and full `pytest -q` suite successfully. The three live Solana smoke steps were skipped in each run and are not counted as passing on-chain checks.

| PR | Comparison | Tests passed | Skipped | CI |
|---|---|---:|---:|---|
| [#86](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/86) | PR #75 dynamic-fee API → canonical PR #77 state API | 1,321 | 5 | Passed |
| [#87](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/87) | PR #75 ObservationState update → canonical PR #80 state model | 1,328 | 5 | Passed |
| [#88](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/88) | PR #76 combined tick crossing → canonical PR #78 composition | 1,339 | 5 | Passed |
| [#89](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/89) | PR #76 FIFO matcher → canonical PR #79 matcher | 1,363 | 5 | Passed |

The regression PRs form a draft dependency chain: **#86 → #87 → #88 → #89**. They are test-only additions; no production trading code was modified. All remain unmerged.

## Pinned Rust/Python oracle — PR #137

Against pinned Raydium source commit `6d7a0f82d2f293c553c69aaf943377367c6c4cc6`, the dedicated Rust differential workflow completed successfully on 2026-11-14:

- [Rust oracle run #43609398854](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/43609398854): **17 passed, 0 failed**.
- [Python CI run #43609398855](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/43609398855): passed.
- Cross-language coverage includes tick/swap math, dynamic-fee transitions and rates, FIFO limit-order matching, tick crossing/reward/liquidity updates, pool post-swap finalization, default and extension tick-array bitmaps, ObservationState transitions, and four account serialization layouts.
- Eight Python replay fixtures now execute the pinned upstream Rust `swap_internal`: exact-input in both directions, exact-output, dynamic-fee-active, FIFO matching with liquidity crossing, two-array default-bitmap traversal, default-bitmap empty-array removal, and positive bitmap-extension traversal plus mutation. They compare returned token amounts, trade fees, tick, sqrt price, liquidity, and final PoolState, all TickArrayState accounts, ObservationState, and bitmap-extension bytes.
- The final account-byte comparison normalizes only the two `recent_epoch` fields that upstream refreshes using its `cfg(test)` system-time epoch helper; all remaining serialized bytes are compared exactly.

This is one controlled end-to-end scenario, not exhaustive swap-loop parity. Multi-array traversal, dynamic-fee-active swaps, FIFO boundary orders, exact-output, empty-array bitmap flips, initialized reward accrual, vault reconciliation, and live-chain validation remain unproven. PR #137 remains draft and unmerged.

## What the comparisons establish

### Dynamic fees

- Validates mapping between the historical public-call interface and canonical state-object interface.
- Covers both directions, skipped/non-skipped spacing, equal/unequal sqrt-price branches, negative ticks, aggregate fee composition/cap, and spacing-bound behavior.
- The compatibility adapter deliberately reuses canonical tick-index, volatility-accumulator, and dynamic-fee primitive helpers. It therefore does **not** independently prove those primitives' arithmetic.

### ObservationState

- The historical Python compatibility regression compares the canonical decoder's 32-byte zero-padding representation with the historical four-u64 shape.
- PR #137 adds Python-generated transition fixtures consumed by the pinned Raydium Rust `ObservationState::update`: first-write initialization, 14/15-second cadence, ring wrap, i64 cumulative wrapping, backward timestamps, and the maximum valid timestamp/tick product.
- PR #137 also compares nontrivial Python-encoded ObservationState, PoolState, TickArrayState, and TickArrayBitmapExtension account images against the pinned Rust discriminator plus zero-copy struct bytes; all four fixtures match byte-for-byte.
- This does **not** certify every possible account byte pattern, prove exact transaction-level oracle sequencing, or validate TWAP sufficiency.

### Tick crossing

- Compares the historical combined crossing operation with canonical crossing plus directional liquidity application.
- Covers both directions, positive/negative/zero liquidity net, exact subtraction to zero, wrapping u128 fee/reward growth, and post-crossing tick convention.
- The canonical composition explicitly rejects outstanding FIFO orders. Comparisons use valid common-domain states; the historical helper's missing caller-level guard is not treated as equivalent behavior.

### FIFO matching

- Uses a frozen test-only transcription of the historical matcher and compares exact tick mutation and amount/fee results with the canonical matcher.
- Covers both directions, exact-input/output, fee-on-input/output, partially filled cohort priority, phase progression, and exhausted cohorts.
- These comparisons are still Python-to-Python compatibility tests, not an independent Rust execution oracle.

## Remaining release blockers

1. **Pinned Rust/Python differential harness:** extend the passing 17-test oracle beyond the first single-array swap fixture to multi-array traversal, dynamic-fee and FIFO cases, exact-output, reverse direction, empty-array bitmap flips, and active reward accrual.
2. **Complete transaction-level swap replay:** the Python orchestrator now composes pre-swap reward/epoch updates, tick-array selection, dynamic-fee-aware inner iterations, boundary FIFO/tick mutation, crossing, observation updates, settlement, and PoolState finalization. Both swap directions, exact-output, active dynamic fees, FIFO orders, default-bitmap traversal/empty-array flips, and positive bitmap-extension traversal/mutation now have full-loop differential fixtures. Initialized reward accrual and vault settlement remain open.
3. **Exact account serialization:** passing fixtures cover ObservationState, PoolState, TickArrayState, and TickArrayBitmapExtension; the single-array replay also compares final PoolState, TickArrayState, and ObservationState bytes after normalizing only the upstream test-epoch fields. Remaining coverage includes vault/token account representations, broader mutated-state combinations, and exhaustive reserved-byte, owner, PDA, counter, and hash invariants.
4. **Balance reconciliation and quote parity:** reconcile token/vault deltas, all fee destinations, and independent quote calculations across exact-input/output and both directions.
5. **Live Solana validation:** provider discovery, market-data, finalized mint/account, and transaction replay smoke tests remain skipped. No live-chain parity claim is made.
6. **Host isolation proof:** validate process, filesystem, mount namespace, service, and key isolation on a real supported Linux host. Android Termux alone cannot prove systemd or mount-namespace isolation.
7. **Independent review and canonical merge order:** reconcile historical PR #75/#76 behavior with the canonical dependency chain before merging any implementation PR. Keep the canonical chain and differential PRs unmerged until source review, full CI, and all dependent checks pass.

## Safety state

- No signing, wallet approval, Jupiter `/execute`, transaction submission, broadcast, or live trading was introduced by this work.
- Value/risk gates and live execution remain locked.
- CI success is software regression evidence only. It is not certification, on-chain parity, or authorization to enable execution.
