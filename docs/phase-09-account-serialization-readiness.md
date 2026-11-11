# Phase 09 — Account serialization and replay readiness

**Snapshot:** 2026-11-11  
**Pinned Raydium CLMM source:** `6d7a0f82d2f293c553c69aaf943377367c6c4cc6`  
**Execution state:** live trading disabled; all implementation PRs remain draft/unmerged.

## CI-verified compatibility and serialization chain

| PR | Scope | Latest verified suite |
|---|---|---:|
| [#86](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/86) | Dynamic-fee legacy/canonical regressions | 1,321 passed, 5 skipped |
| [#87](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/87) | ObservationState legacy/canonical regressions | 1,328 passed, 5 skipped |
| [#88](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/88) | Tick-crossing legacy/canonical regressions | 1,339 passed, 5 skipped |
| [#89](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/89) | FIFO limit-order legacy/canonical regressions | 1,363 passed, 5 skipped |
| [#91](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/91) | TickArrayState, PoolState and bitmap-extension serializers | 1,366 passed, 5 skipped |
| [#98](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/98) | ObservationState exact serializer and update commitment | 1,368 passed, 5 skipped |
| [#99](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/99) | SPL vault amount serializer/delta, PoolState mutation commitments | 1,375 passed, 5 skipped |\n| [#101](https://github.com/Nexora-Apparel/veltrix-multi-agent-crypto/pull/101) | Replay account bundle identity and byte-commitment validator | 1,381 passed, 5 skipped |

The PRs are stacked and intentionally remain unmerged. CI compilation and Python test suite succeeded for each listed head. Three live Solana smoke steps were skipped in CI; they are not passing live-chain evidence.

## Account byte layouts now covered

| Account | Serialized length | Mutated fields / commitment |
|---|---:|---|
| Raydium TickArrayState | 10,240 bytes | 60 TickState records, initialized count, recent epoch; preserves 107 opaque account-padding bytes |
| Raydium PoolState | 1,544 bytes | modeled price/tick/liquidity, fee growth, protocol/fund fees, reward infos, dynamic-fee state, default bitmap; preserves unmodeled/reserved bytes |
| TickArrayBitmapExtension | 1,832 bytes | exact positive/negative bitmap layout |
| ObservationState | 4,483 bytes | initialized flag, epoch, ring index and 100 observation records; reserved padding remains zero |
| Classic SPL Token vault | 165 bytes | only the u64 token amount is changed; all other source bytes preserved |

For supported decoded accounts, serialization validates source-byte commitments and canonical PDA identity where applicable. Mutation paths that produce serialized state recalculate SHA-256 commitments. The SPL vault delta primitive enforces u64 bounds and does not yet constitute a two-vault atomic settlement.

## What is established

- Historical Python transitions are compared with canonical Python transition APIs across common valid domains for dynamic fees, ObservationState, tick crossing and FIFO orders.
- Account serializers round-trip exact pinned layouts and preserve opaque bytes where the model supports them.
- PoolState post-swap, reward, and tick-array bitmap mutation paths can refresh serialized PoolState bytes and commitments.
- Observation updates refresh the exact serialized ObservationState commitment.
- SPL vault amount deltas update only the token amount bytes and refresh the vault commitment.
- All these changes remain local deterministic state operations; no transaction signer or broadcaster is involved.\n- PR #101 binds PoolState, one or more TickArrays, optional bitmap extension, ObservationState and exactly two token vaults into one deterministic account-bundle commitment.

## What is not established

1. **Independent Rust/Python differential oracle:** the legacy compatibility tests are Python transcriptions/adapters. They are not executing identical fixtures against the pinned Rust program.
2. **Complete swap-loop replay:** there is no single transaction-level orchestrator yet composing array traversal, dynamic fee, swap steps, FIFO matching, tick crossing, fee splitting, reward update, observation update and settlement in the exact source order.
3. **Atomic multi-account commit:** both vaults, PoolState, all touched TickArrayState/bitmap-extension accounts and ObservationState must be staged together; any failed invariant must reject the entire replay without exposing partial state.
4. **Vault reconciliation:** token0/token1 deltas, LP/protocol/fund fees, reward transfers and account balances must reconcile exactly against the pinned Rust instruction and vault snapshots.
5. **Full account-set serialization proof:** mutated accounts need byte-for-byte comparisons against Rust-generated post-state fixtures, including account discriminators, owners, PDAs, reserved fields and hashes.
6. **Live Solana smoke tests:** provider discovery, market-data and finalized mint/account checks remain skipped.
7. **Host isolation proof:** demonstrate process, filesystem, mount namespace, service and key isolation on a supported Linux host; Android Termux does not prove systemd or mount-namespace guarantees.
8. **Independent review and merge sequence:** resolve historical PR #75/#76 behavior against the canonical chain, review the stacked PRs, and merge only after source parity, full CI and all release gates pass.

## Next engineering sequence

1. Freeze the serializer contracts and obtain CI-green status for #99.
2. Add a replay-account bundle validator that binds PoolState, TickArrays, optional bitmap extension, ObservationState and both vaults to one pinned source, pool identity and set of account commitments.
3. Build a traceable deterministic swap-loop orchestrator in pinned Rust source order, with no signing or network writes.
4. Add Rust-generated golden account fixtures and byte-for-byte intermediate/final state comparisons.
5. Add atomic settlement and conservation checks for token balances and all fee destinations.
6. Run independent review and live read-only Solana smoke checks; keep execution locked until the complete certification gate is satisfied.

**Safety invariant:** no signing, wallet approval, Jupiter `/execute`, transaction submission, broadcast, or live trading is permitted by this work. Passing Python CI is not on-chain parity or execution authorization.
