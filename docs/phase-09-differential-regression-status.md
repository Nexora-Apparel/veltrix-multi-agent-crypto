# Phase 09 — Legacy/canonical differential regression status

**Snapshot date:** 2026-11-11  
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

## What the comparisons establish

### Dynamic fees

- Validates mapping between the historical public-call interface and canonical state-object interface.
- Covers both directions, skipped/non-skipped spacing, equal/unequal sqrt-price branches, negative ticks, aggregate fee composition/cap, and spacing-bound behavior.
- The compatibility adapter deliberately reuses canonical tick-index, volatility-accumulator, and dynamic-fee primitive helpers. It therefore does **not** independently prove those primitives' arithmetic.

### ObservationState

- Decodes the same verified account bytes with the canonical decoder and maps the 32-byte zero-padding representation to the historical four-u64 shape.
- Compares first-write initialization, 14/15-second cadence, ring wrap, i64 cumulative wrapping, timestamp regression, and i64 delta saturation.
- It does **not** execute two independently imported decoder implementations, prove exact post-swap oracle sequencing, or validate TWAP sufficiency.

### Tick crossing

- Compares the historical combined crossing operation with canonical crossing plus directional liquidity application.
- Covers both directions, positive/negative/zero liquidity net, exact subtraction to zero, wrapping u128 fee/reward growth, and post-crossing tick convention.
- The canonical composition explicitly rejects outstanding FIFO orders. Comparisons use valid common-domain states; the historical helper's missing caller-level guard is not treated as equivalent behavior.

### FIFO matching

- Uses a frozen test-only transcription of the historical matcher and compares exact tick mutation and amount/fee results with the canonical matcher.
- Covers both directions, exact-input/output, fee-on-input/output, partially filled cohort priority, phase progression, and exhausted cohorts.
- These comparisons are still Python-to-Python compatibility tests, not an independent Rust execution oracle.

## Remaining release blockers

1. **Pinned Rust/Python differential harness:** execute identical account/state fixtures through the pinned Raydium Rust source and Python ports; compare exact intermediate state, amounts, fees, and serialized bytes.
2. **Complete transaction-level swap replay:** compose dynamic-fee updates, bitmap traversal, tick crossing, FIFO orders, input/output steps, fee growth, protocol/fund fee splitting, reward emissions, observation updates, and vault settlement in the exact on-chain order.
3. **Exact account serialization:** verify every mutated PoolState, TickArrayState, bitmap-extension, observation, and vault representation byte-for-byte, including discriminators, owners, PDAs, reserved bytes, counters, and hashes.
4. **Balance reconciliation and quote parity:** reconcile token/vault deltas, all fee destinations, and independent quote calculations across exact-input/output and both directions.
5. **Live Solana validation:** provider discovery, market-data, finalized mint/account, and transaction replay smoke tests remain skipped. No live-chain parity claim is made.
6. **Host isolation proof:** validate process, filesystem, mount namespace, service, and key isolation on a real supported Linux host. Android Termux alone cannot prove systemd or mount-namespace isolation.
7. **Independent review and canonical merge order:** reconcile historical PR #75/#76 behavior with the canonical dependency chain before merging any implementation PR. Keep the canonical chain and differential PRs unmerged until source review, full CI, and all dependent checks pass.

## Safety state

- No signing, wallet approval, Jupiter `/execute`, transaction submission, broadcast, or live trading was introduced by this work.
- Value/risk gates and live execution remain locked.
- CI success is software regression evidence only. It is not certification, on-chain parity, or authorization to enable execution.


## Serialization and settlement progress (2026-11-11)

The following draft PRs extend the test-only reconciliation into byte-preserving account serializers and immutable balance transitions. CI counts are full Python suites; all listed runs skipped the live Solana smoke steps.

| PR | Added capability | Passed | Skipped | CI |
|---|---|---:|---:|---|
| [#90](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/90) | Swap-loop source-order specification and blocker record | 1,363 | 5 | Passed |
| [#91](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/91) | TickArrayState and bitmap-extension serializers; post-mutation hashes | 1,364 | 5 | Passed |
| [#92](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/92) | Lossless PoolState serializer; bitmap and post-swap hash commits | 1,367 | 5 | Passed |
| [#93](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/93) | ObservationState serializer and mutation hash commits | 1,369 | 5 | Passed |
| [#94](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/94) | Classic SPL vault serializer and direction-aware pool-vault settlement | 1,376 | 5 | Passed |

The serialization work preserves source account images and opaque bytes where the Python state model does not explicitly represent them. Encoders validate their output through the canonical decoders where applicable. Vault settlement is bound to PoolState token-0/token-1 mint and vault identities and follows the pinned `exact_internal` transfer direction.

## Current release blockers after PR #94

1. **Executable swap-loop orchestrator:** the source-order specification exists, but the Python components are not yet composed into one deterministic, atomic replay of the full pinned `swap_internal` loop.
2. **Independent Rust/Python parity:** compatibility regressions compare historical Python behavior with canonical Python transitions. No executable differential harness against the pinned Rust program has passed.
3. **User token-account and transfer reconciliation:** pool vault deltas are modeled; user token account bytes, CPI transfer effects, actual before/after balance reloads, and exact-in/exact-out wrapper constraints are not yet replayed end-to-end.
4. **Complete account post-state:** PoolState, TickArrayState, bitmap-extension, ObservationState, and classic SPL pool vaults have serializers. Exact replay still needs atomic coordination of all post-state accounts and reconciliation of every token delta/fee destination.
5. **Live Solana validation:** discovery, market-data, finalized-account, and transaction replay smoke checks remain skipped. No on-chain behavior is certified.
6. **Host isolation and independent review:** supported Linux process/filesystem/mount isolation and independent source review remain unverified.

All PRs #86–#94 remain draft/unmerged. Do not merge a partial dependency chain or enable live execution based on green Python CI. Signing, wallet access, submission, broadcast, and real-money execution remain disabled.
