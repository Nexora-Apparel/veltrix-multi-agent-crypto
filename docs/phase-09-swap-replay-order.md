# Phase 09 — Pinned Raydium swap-loop composition order

**Source repository:** `raydium-io/raydium-clmm`  
**Pinned source commit:** `6d7a0f82d2f293c553c69aaf943377367c6c4cc6`  
**Primary source:** `programs/amm/src/instructions/swap.rs`, `swap_internal`  
**Reference range:** lines 501–830 at the pinned commit  
**Status:** specification only; no transaction-level replay is implemented by this document.

## Required execution order

The replay orchestrator must preserve this ordering. Individual arithmetic primitives are not sufficient if called in a different sequence.

1. **Preconditions and bounds** — reject zero amount, disabled swap status, and directionally invalid sqrt-price limits.
2. **Reward snapshot update** — call `PoolState::update_reward_infos(block_timestamp)` before the loop; retain the returned reward infos for tick crossing.
3. **Observation ownership** — verify the ObservationState belongs to the pool.
4. **Tick-array start and account selection** — derive the first valid array using the bitmap extension when required; verify pool association and expected start index.
5. **Fee mode and initial swap state** — compute `is_fee_on_input` once; initialize remaining/calculated amounts, price, tick, fee-growth side, liquidity, dynamic-fee state, and initial dynamic-fee reference.
6. **Find the next initialized tick** — search the current array from the current tick; handle the special first-array case; otherwise traverse bitmap-selected arrays and validate every consumed array.
7. **Set target price** — derive the next tick price and clamp it to the user sqrt-price limit.
8. **Begin inner spacing loop** — initialize `liquidity_next` from current liquidity for the outer tick iteration.
9. **Dynamic-fee pre-step** — update volatility accumulator, compute total fee rate, then compute the spacing-bounded price and whether spacing was skipped. This order is source-significant.
10. **Swap-step calculation** — when price changes, run exact-input/output swap math with the current liquidity, remaining amount, total fee, direction, and fee mode; immediately apply amount/fee accounting and split LP/protocol/fund fees.
11. **Boundary FIFO match** — when the computed next sqrt price equals the initialized tick price, match that tick's limit orders using the **pre-dynamic-index-advance total fee rate**. Apply non-zero match amounts through the same amount/fee accounting transition.
12. **Tick-array mutation** — if the tick becomes uninitialized after order matching, decrement the initialized count; flip the bitmap bit only when the array becomes empty.
13. **Tick crossing** — only when the tick has liquidity and no remaining limit orders: invert fee-growth-outside for both tokens, invert initialized reward-growth slots using the start-of-swap reward snapshot, apply direction-signed liquidity net, and stage the resulting liquidity in `liquidity_next`.
14. **Persist tick state and current tick convention** — write the mutated tick to the current array; set current tick according to direction and whether limit orders remain. This is not always simply `tick_next - 1`.
15. **Non-boundary price/tick update** — if the price moved without reaching the initialized tick, derive current tick from the new sqrt price, except when the bounded-tick result can be reused.
16. **Advance price and dynamic-fee index** — set the new sqrt price, then update the dynamic-fee spacing index. If amount is exhausted or the target price is reached, validate the post-match unfilled-order invariant before leaving the inner loop.
17. **Commit staged liquidity to loop state** — after the inner loop, assign `state.liquidity = liquidity_next` before searching the next initialized tick.

## Post-loop settlement order

18. **Observation update** — only if final tick differs from the pool's stored current tick, update the observation using the **previous pool tick**, before changing PoolState.
19. **Token amount settlement** — derive final token0/token1 amounts and trade-fee amounts from the original specified amount, direction, base-input flag, and fee-token side.
20. **PoolState post-swap mutation** — update current tick, sqrt price, liquidity, LP/protocol/fund fee totals, fee growth, and dynamic-fee state.
21. **Return replay result** — expose amounts, fees, final sqrt price, liquidity, and tick only after all preceding transitions succeed.

## Ordering hazards that require explicit tests

- Dynamic fee is updated before each price step, while the boundary FIFO match uses the already-computed fee for that spacing group. The upstream source comment explicitly describes this as last-traversed-group semantics.
- The dynamic-fee index advances after the price/tick transition, not before the boundary order match.
- FIFO orders are matched before tick crossing. A tick with remaining limit orders does not run the liquidity-crossing branch.
- Initialized count and bitmap mutation occur after matching and before tick-state persistence.
- Tick crossing uses reward growth values captured before the loop, not a reward state recalculated per tick.
- `liquidity_next` is staged during the inner loop and assigned after the inner loop exits.
- ObservationState receives the old PoolState tick and is updated before `PoolState::update_after_swap`.
- Fee growth for the two tokens depends on the configured fee-on side; only the fee-earning token's global growth comes from mutable swap state.

## Implementation acceptance criteria

A future Python replay implementation must provide:
- one immutable replay state with explicit remaining/calculated amount, current/next tick, sqrt prices, liquidity, fee growth, LP/protocol/fund totals, and dynamic-fee state;
- deterministic tick-array/bitmap cursor state with exact account-consumption order;
- one loop iteration trace recording each intermediate transition and source-equivalent values;
- golden fixtures from the pinned Rust program for both directions, exact-input/output, fee-on-input/output, skipped spacing, empty-array bitmap flips, FIFO partial fills, reward growth, observation cadence, and amount/price-limit exits;
- atomic failure semantics: no partially mutated account state escapes when any transition fails;
- byte-for-byte post-state serialization and vault/token delta reconciliation.

Until those criteria and the blockers in `phase-09-differential-regression-status.md` are satisfied, this ordering document is not execution authorization.


## Executable wrapper validator status

PR #96 implements a pure validator for the wrapper's pool-vault reload and exact-fill checks. It does not invoke SPL CPI transfers or model user token accounts. The complete loop and actual transfer execution remain unimplemented; the source-order specification is not itself a replay engine.
