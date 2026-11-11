# Phase 09 — Pinned Raydium CLMM swap-loop source-order matrix

**Source repository:** `raydium-io/raydium-clmm`  
**Pinned commit:** `6d7a0f82d2f293c553c69aaf943377367c6c4cc6`  
**Primary sources:** `programs/amm/src/instructions/swap.rs` and `swap_v2.rs`  
**Snapshot:** 2026-11-11  
**Execution:** disabled; no signing, wallet approval, or transaction submission.

This document maps the actual pinned Rust control flow to the current Python primitives. It is a source-order specification, not a claim that the Python implementation already performs a complete swap replay.

## 1. Exact `swap_internal` order

| Rust source lines | Required operation and order | Current Python coverage | Replay status |
|---|---|---|---|
| `swap.rs:513-525` | Reject zero amount, disabled swap status, and invalid direction-specific sqrt-price limit | PoolState decoder/status checks; swap-step math domain checks | Partial; no single entrypoint gate |
| `swap.rs:528-536` | Update reward infos at block timestamp; validate observation belongs to pool; resolve first valid tick-array start using bitmap extension | `raydium_clmm_rewards.update_raydium_reward_infos`; bitmap traversal verifier; observation decoder | Primitives exist; orchestration absent |
| `swap.rs:537-559` | Consume the ordered tick-array queue, locate the first valid array, and validate pool/start index | Tick-array snapshots, PDA validation and CPI-order binding | No mutable `VecDeque` replay cursor |
| `swap.rs:560-568` | Determine fee-on-input and initialize swap state, including dynamic-fee reference update | `raydium_clmm_dynamic_fee.initialize_dynamic_fee_swap_state`; fee accounting state | Primitive coverage; not integrated into one swap state |
| `swap.rs:571-612` | Outer loop: find next initialized tick; if necessary move through the exact next bitmap-selected array and consume the next supplied account | Tick-array decoding and bitmap traversal verification | No complete source-order cursor |
| `swap.rs:621-625` | Derive target price from next initialized tick, direction and price limit | Tick math and spacing-bounded-price helpers | Primitive coverage |
| `swap.rs:633-638` | Begin inner loop: update volatility accumulator, compute total fee rate, and apply spacing-bounded price | `update_volatility_accumulator`, `compute_total_fee_rate`, `get_spacing_bounded_price` | Primitive coverage |
| `swap.rs:640-667` | If price changes, compute swap amounts and apply them, splitting LP/protocol/fund fees | `compute_swap_step`; `apply_raydium_swap_amounts` | Primitive coverage; sequencing not composed |
| `swap.rs:672-708` | At the tick boundary, match limit orders using the pre-advance fee rate and apply their amounts/fees | `match_raydium_limit_order`; fee accounting | Primitive coverage; exact loop coupling absent |
| `swap.rs:709-717` | If tick becomes uninitialized, decrement initialized count and flip bitmap only when array becomes empty | `apply_raydium_tick_array_mutation` | Local mutation primitive covered |
| `swap.rs:719-742` | For liquidity-bearing non-limit-order tick, cross fee/reward growth and apply direction-sensitive liquidity delta | `cross_tick_state`; `apply_crossed_tick_liquidity` | Primitive coverage; exact order with order matching not replayed |
| `swap.rs:744-761` | Persist crossed tick state and apply the four-way direction/limit-order tick assignment rule | Tick-crossing and tick-array mutation primitives | Composition not replayed |
| `swap.rs:763-778` | For non-crossing movement, update tick from bounded tick or sqrt price; set price and advance dynamic-fee index | Tick math and `advance_dynamic_fee_index` | Primitive coverage |
| `swap.rs:779-793` | Enforce limit-order residual invariant, break inner loop, then apply `liquidity_next` to active state | FIFO order matching and liquidity arithmetic | Invariant is not yet enforced by one orchestrator |
| `swap.rs:798-821` | If final tick changed, update observation using the **previous pool tick**; settle amounts; update PoolState with final tick/price/liquidity/fees/dynamic-fee state | Observation update, `settle_raydium_swap_amounts`, `apply_raydium_pool_after_swap`, reward update | Separate primitives; exact ordered composition absent |

## 2. Exact V2 account and token-transfer order

| Rust source lines | Required operation | Current Python coverage / gap |
|---|---|---|
| `swap_v2.rs:92-109` | Read clock; capture user balances; calculate Token-2022 input transfer fee or inverse output fee before swap | Not implemented. Current vault serializer supports classic SPL Token accounts only |
| `swap_v2.rs:111-162` | Validate pool open time and input/output vault direction; parse remaining accounts in supplied order; call `swap_internal` | Pool/vault identity and CPI evidence checks exist; no exact V2 transaction orchestrator |
| `swap_v2.rs:176-242` | Normalize token0/token1; compute both transfer fees, net amounts and transfer amounts with checked arithmetic | Not implemented for Token-2022 transfer-fee extensions |
| `swap_v2.rs:253-268` | Emit swap event values including transfer fees, final price/liquidity/tick and trade fees | Event-equivalent replay record not yet implemented |
| `swap_v2.rs:270-310` | Transfer input from user to pool vault, then output from pool vault to user, in direction-specific order | Local vault amount-delta primitive exists; no two-vault atomic settlement |
| `swap_v2.rs:311-345` | Reload balances, enforce price direction, and enforce exact fill when no explicit price limit was supplied | No complete transaction-level reconciliation gate |

## 3. Current primitive modules

- Dynamic fees: `veltrix/raydium_clmm_dynamic_fee.py`
- Swap-step arithmetic: `veltrix/raydium_clmm_swap_step_math.py`
- Fee splitting and settlement: `veltrix/raydium_clmm_fee_accounting.py`
- FIFO limit orders: `veltrix/raydium_clmm_limit_orders.py`
- Tick crossing/liquidity: `veltrix/raydium_clmm_tick_crossing.py`
- Tick-array and bitmap mutation: `veltrix/raydium_clmm_tick_array_mutation.py`
- Reward emissions: `veltrix/raydium_clmm_rewards.py`
- Observation state: `veltrix/raydium_clmm_observation.py`
- Pool post-swap mutation: `veltrix/raydium_clmm_pool_post_swap.py`
- Classic SPL vault byte mutation: `veltrix/raydium_clmm_vaults.py`
- Simulation account provenance and state binding: `veltrix/raydium_clmm_simulation_state.py`

These modules are intentionally not presented as a complete replay engine. Their individual CI tests do not establish correct end-to-end operation ordering.

## 4. Non-negotiable replay implementation sequence

1. **Replay input gate:** bind one pinned pool/config/observation, both vaults, all supplied tick arrays and optional bitmap extension to one simulation response, context slot, cluster and source revision.
2. **Mutable replay state:** introduce one immutable transaction-local state containing PoolState, ObservationState, both vaults, tick arrays, bitmap extension, remaining amount, active liquidity, price/tick, fee state, reward state and consumed-account cursor.
3. **Outer array loop:** implement first-array selection, next-array lookup, ordered account consumption and insufficient-account errors exactly as `swap.rs:532-612`.
4. **Inner swap loop:** compose dynamic fee, spacing-boundary calculation, swap math, fee splitting, FIFO order matching, tick initialization/bitmap mutation, tick crossing, tick assignment, price update and residual-order invariant in the exact sequence at `swap.rs:621-793`.
5. **Post-loop order:** update observation with the previous pool tick, settle amount0/amount1 and trade fees, then update PoolState exactly as `swap.rs:798-821`.
6. **V2 transfer layer:** implement Token-2022 transfer-fee and inverse-fee math, transfer amount normalization, direction-specific vault transfers, balance reload, price-direction and exact-fill checks as `swap_v2.rs:92-345`.
7. **Atomic result:** serialize every changed account into one staged result; reject all changes if any account, fee, token conservation or hash invariant fails.
8. **Differential oracle:** run identical deterministic fixtures against the pinned Rust implementation and Python replay, comparing every intermediate step and final account bytes, not just final amounts.
9. **Only then:** independent review, read-only live RPC smoke checks and certification. Signing and live execution remain separately locked.

## 5. Hard blockers

- There is no end-to-end Python swap-loop orchestrator.
- There is no Rust-generated golden fixture suite covering each intermediate loop iteration and all post-state account bytes.
- Token-2022 transfer-fee logic in `swap_v2.rs` is not implemented. Until it is, the replay gate must reject Token-2022 pools rather than silently treat them as classic SPL.
- The existing local vault delta helper is not atomic two-vault settlement.
- Python CI skips the live Solana discovery, market-data and finalized-account smoke steps.
- Independent source review and full transaction replay are still required.

**Safety invariant:** structural post-state validation and passing Python CI do not prove swap correctness or authorize execution. Live trading remains disabled.
