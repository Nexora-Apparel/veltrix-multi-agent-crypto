# Phase 09 — Legacy / Canonical Raydium Reconciliation Audit

**Audit date:** 2026-11-11  
**Audit base:** `89cdaa7b2f90e6409833043888de62e55f89fe51` (Phase 09.42 head)  
**Status:** Static comparison completed; behavioral parity NOT certified.  
**Execution policy:** Paper-only. No signing, transaction submission, or live trading is authorized by this audit.

## Scope

Compared the historical Phase 09.35–09.36 implementations in PRs #75 and #76 with the later canonical modules and their tests:

- Dynamic fees: #75 vs #77
- Observation state: #75 vs #80
- Tick crossing: #76 vs #78
- FIFO limit orders: #76 vs #79

This is a source/API/test-coverage comparison, not an execution of the test suite and not a Rust-vs-Python equivalence proof.

## Findings

### Dynamic fees — #75 vs #77

The implementations share core operations for tick-spacing indexing, reference updates, volatility accumulation, fee calculation, and spacing-bounded prices. Their public helper surfaces differ:

- Historical-only: `_u32`, `get_total_fee_rate`, `update_dynamic_fee_index`
- Canonical-only: `_integer`, `compute_total_fee_rate`, `initialize_dynamic_fee_swap_state`, `advance_dynamic_fee_index`

The canonical test module contains source-vector coverage for fee calculation, reference filter/decay/reset windows, backwards timestamps, volatility caps, skipped tick spacing, direction, and spacing-bounded price. No evidence from this static audit establishes that every historical `update_dynamic_fee_index` state transition is represented by an equivalent canonical test.

**Required before closure:** map every historical state transition to a canonical operation and add differential vectors for both swap directions, skipped/non-skipped spacing, boundary timestamps, and fee-on-input/output behavior where applicable.

### Observation state — #75 vs #80

Both modules expose an observation update transition, but the decoder and PDA helper names changed:

- Historical: `derive_raydium_observation_address`, `decode_raydium_observation_state`
- Canonical: `derive_raydium_clmm_observation_address`, `decode_raydium_clmm_observation_state`

Canonical tests cover exact account layout, initialized/uninitialized state, ring-index bounds, pool association, PDA, owner/hash/padding validation, first write, 15-second boundary, ring wrap, and cumulative arithmetic.

**Required before closure:** compare historical and canonical decoder acceptance/rejection conditions and update behavior over identical account byte fixtures. Confirm no caller/import compatibility is required by downstream code.

### Tick crossing — #76 vs #78

Historical #76 combines tick crossing with limit-order helpers and a `cross_raydium_tick` transition. Canonical #78 separates fee/reward-growth inversion and liquidity arithmetic into `cross_tick_state`, `apply_crossed_tick_liquidity`, and `cross_tick_and_apply_liquidity`.

Canonical tests cover wrapping fee growth, initialized reward growth inversion, direction-sensitive liquidity net, zero/negative liquidity net, atomic composition, u128/i128 boundaries, outstanding-order rejection, reward slot validation, and initialized-state consistency.

**Required before closure:** differential-test the historical combined crossing path against the canonical composed path. Verify call ordering and mutation semantics at the swap-loop boundary; unit-level arithmetic parity alone is insufficient.

### FIFO limit orders — #76 vs #79

The canonical module retains the matching and amount-conversion concepts but adds explicit u64/u128 domain validation and a token0 price helper. Historical #76 also contains `cross_raydium_tick`, which belongs to the separately scoped canonical crossing module.

Canonical tests cover directional floor/ceiling, all four base-input/fee-on-input combinations, existing partially filled cohort priority, FIFO phase advancement, amount caps, no-op behavior, invalid state, fee/amount domain errors, and invalid square-root price.

**Required before closure:** compare exact rounding and state transitions for both swap directions and all fee modes, including partially filled cohorts and exhausted cohorts. Confirm the caller consumes the returned input/output/fee amounts in the same order as the pinned on-chain instruction.

## Historical branch handling

- PR #75 and #76 remain historical comparison sources; do not merge them wholesale into the canonical chain.
- Do not close or delete them until the parity checklist above is completed and all unique behavior is either covered by canonical code/tests or explicitly documented as obsolete.
- No source files were changed by this audit.

## Integration gate

Before starting Phase 09.43:

1. Complete the differential parity tests above.
2. Reconcile all unique historical tests and callers.
3. Verify canonical PR dependency bases and heads are current.
4. Obtain independent review of the canonical chain.
5. Run the full repository suite on the integrated chain.
6. Keep live-chain smoke tests and transaction-level replay as separate outstanding gates; unit CI cannot substitute for them.

Phase 09.43 must not be treated as complete until exact on-chain operation order is established from the pinned Raydium source and validated against finalized transaction/account evidence.

## Safety conclusion

This audit does not certify quote parity, serialized account parity, balance reconciliation, or live execution. Keep live execution disabled and paper-only until the complete Phase 09 release gates are independently satisfied.
