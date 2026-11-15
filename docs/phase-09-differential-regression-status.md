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

- [Rust oracle run #43623708702](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/43623708702): **19 passed, 0 failed**.
- [Python CI run #43623708823](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/43623708823): **1,513 passed, 5 skipped**.
- Cross-language coverage includes tick/swap math, dynamic-fee transitions and rates, FIFO limit-order matching, tick crossing/reward/liquidity updates, direct PoolState reward-update transitions across nine edge cases plus a simultaneous three-slot fixture, pool post-swap finalization, default and extension tick-array bitmaps, ObservationState transitions, and account serialization.
- Fourteen Python replay fixtures execute pinned upstream Rust `swap_internal`: exact-input in both directions, exact-output, dynamic-fee-active, FIFO matching with liquidity crossing, two-array default-bitmap traversal, default-bitmap empty-array removal, positive and negative bitmap-extension traversal/mutation, initialized reward accrual with all three reward slots initialized, and all three PoolState `fee_on` modes across both directions.
- Replay comparisons cover returned token amounts, trade fees, final tick, sqrt price, liquidity, final PoolState, all TickArrayState accounts, ObservationState, bitmap-extension bytes, and both vault token-account byte images.
- The Rust replay decodes and re-encodes classic SPL Token's 165-byte vault account format. Actual pinned Rust `swap_internal` amount deltas are applied directionally and the resulting account bytes match the Python vault-delta model.
- A Python integration test runs the full outer swap loop, settles the completed swap, builds provenance-bound before/after vault snapshots, and reconciles direction-specific vault deltas and account hashes.
- Vault RPC reads accept an explicit minimum context slot. Swap settlement now rejects a pre-swap vault snapshot older than the CPI tick-array snapshot, preventing cross-slot state mixing.
- Only PoolState and mutated TickArrayState `recent_epoch` values refreshed by the upstream `cfg(test)` system-time helper are normalized; all remaining serialized bytes are compared exactly.

This is deterministic software differential evidence, not exhaustive swap-loop parity. The five skipped Python CI checks include live Solana smoke coverage; skipped checks are not passes.

## What the comparisons establish

### Dynamic fees

- Validates mapping between the historical public-call interface and canonical state-object interface.
- Covers both directions, skipped/non-skipped spacing, equal/unequal sqrt-price branches, negative ticks, aggregate fee composition/cap, and spacing-bound behavior.
- The compatibility adapter deliberately reuses canonical tick-index, volatility-accumulator, and dynamic-fee primitive helpers. It therefore does **not** independently prove those primitives' arithmetic.

### ObservationState

- The historical Python compatibility regression compares the canonical decoder's 32-byte zero-padding representation with the historical four-u64 shape.
- PR #137 adds Python-generated transition fixtures consumed by pinned Raydium Rust `ObservationState::update`: first-write initialization, 14/15-second cadence, ring wrap, i64 cumulative wrapping, backward timestamps, and maximum valid timestamp/tick product.
- Python-encoded ObservationState, PoolState, TickArrayState, and TickArrayBitmapExtension account images match the pinned Rust discriminator plus zero-copy struct bytes byte-for-byte.
- This does **not** certify every possible account byte pattern, prove exact transaction-level oracle sequencing, invoke the Token Program CPI, or validate TWAP sufficiency.

### Tick crossing

- Compares the historical combined crossing operation with canonical crossing plus directional liquidity application.
- Covers both directions, positive/negative/zero liquidity net, exact subtraction to zero, wrapping u128 fee/reward growth, and post-crossing tick convention.
- The canonical composition explicitly rejects outstanding FIFO orders. Comparisons use valid common-domain states; the historical helper's missing caller-level guard is not treated as equivalent behavior.

### FIFO matching

- Uses a frozen test-only transcription of the historical matcher and compares exact tick mutation and amount/fee results with the canonical matcher.
- Covers both directions, exact-input/output, fee-on-input/output, partially filled cohort priority, phase progression, and exhausted cohorts.
- These comparisons are still Python-to-Python compatibility tests, not an independent Rust execution oracle; the separate Rust replay fixtures independently exercise FIFO through upstream `swap_internal`.

## Remaining release blockers

1. **Broader differential fuzzing:** add seeded randomized and adversarial state combinations around tick boundaries, liquidity exhaustion, fee rounding, reward caps, and malformed account-state transitions. Current fixtures are deterministic and bounded.
2. **Full transaction replay expansion:** fourteen fixtures now exercise both directions, exact-output, dynamic fees, FIFO, multi-array traversal, positive/negative bitmap extensions, multi-slot rewards, and fee-on modes. Expand to combinations of these features rather than only one feature family per fixture.
3. **Exhaustive account serialization:** passing fixtures cover ObservationState, PoolState, TickArrayState, TickArrayBitmapExtension, and classic SPL Token vault accounts. Remaining coverage includes broader mutated-state combinations and exhaustive reserved-byte, owner, PDA, counter, and hash invariants.
4. **Provider-backed vault reconciliation and CPI:** before/after snapshot models are tested and slot coherence is enforced. Still fetch actual same-provider, same-genesis, same-or-newer-slot post-CPI vault snapshots; exercise the Token Program CPI in a controlled validator; and reconcile the resulting actual balances.
5. **Independent quote parity:** compare exact-input/output and both directions against an independent, pinned quote implementation; do not reuse the same Python swap-loop calculation as its own oracle.
6. **Live Solana validation:** provider discovery, market-data, finalized mint/account, and transaction replay smoke tests remain skipped. No live-chain parity claim is made.
7. **Host isolation proof:** validate process, filesystem, mount namespace, service, and key isolation on a real supported Linux host. Android Termux alone cannot prove systemd or mount-namespace isolation.
8. **Independent review and canonical merge order:** reconcile historical PR #75/#76 behavior with the canonical dependency chain before merging any implementation PR. Keep PR #137 and its dependency chain unmerged until source review, full CI, and all dependent checks pass.

## Phase 09.74 — finalized Devnet settlement inspection

- Added a read-only inspector entry point that accepts an existing finalized transaction signature together with the exact typed pool evidence, route checks, simulation evidence, and expected signed vault deltas.
- The inspector is Devnet-only and requires explicit endpoint, provider ID, independently pinned Devnet genesis hash, and exact hostname allowlist settings:
  `VELTRIX_CLMM_DEVNET_RPC_URL`,
  `VELTRIX_CLMM_DEVNET_RPC_PROVIDER_ID`,
  `VELTRIX_CLMM_DEVNET_GENESIS_HASH`, and
  `VELTRIX_CLMM_DEVNET_RPC_ALLOWED_HOSTS`.
- The RPC adapter now fetches both decoded transaction metadata and canonical base64 wire bytes at finalized commitment. It validates the wire signature and canonical message serialization, then records the SHA-256 of the exact serialized message.
- Settlement verification requires that finalized message hash to equal the message hash in the original simulation evidence, in addition to the existing same-provider/genesis, Raydium invocation, Token Program CPI, and exact vault-delta checks.
- The expected signed vault deltas are now independently bound to the original simulation's raw pre/post account-state bytes for both pool vaults. Each simulated account must be the classic SPL Token account with matching mint, pool authority, initialized state, and verified SHA-256 commitments; caller-supplied expected deltas that do not equal those simulated byte-level changes are rejected. The simulation must capture all non-program message accounts so the pool vaults are present in `SimulationEvidence.account_states`.
- This closes a critical evidence-pairing gap: an unrelated finalized transaction can no longer be combined with an otherwise valid simulation record merely because it touched the same pool and produced matching vault deltas.
- The inspector has no signing, wallet, transaction-submission, or broadcast path. Its result remains `execution_ready=False`.
- [Python CI run #43689173575](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/43689173575): **1,530 passed, 5 skipped**; compilation passed. The five skipped checks are live-provider smoke tests and are not counted as on-chain validation.
- [Pinned Rust differential run #43689173636](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/actions/runs/43689173636): **19 passed, 0 failed** against the pinned upstream Raydium source.
- CI confirms software regression behavior only. A real finalized Devnet signature and matching original simulation/pool evidence are still required before any live-chain settlement claim.

## Safety state

- No signing, wallet approval, Jupiter `/execute`, transaction submission, broadcast, or live trading was introduced by this work.
- Value/risk gates and live execution remain locked.
- CI success is software regression evidence only. It is not certification, on-chain parity, or authorization to enable execution.
