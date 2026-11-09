# Base V2 factory discovery register

Status: discovery only. No factory in this document is accepted into the production registry unless its deployment receipt, deployment block, runtime code hash, V2 ABI, common-finalized-block state, complete PairCreated history, and pool inventory have been independently verified against both archive providers.

## Accepted

| AMM | Factory | Chain ID | Deployment block | Runtime code hash | Status |
|---|---|---:|---:|---|---|
| Uniswap V2 | 0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6 | 8453 | 6601915 | bab145d02e7005f0d84c6c1639d39b799b0ea16df99ebbdaf5a14d9da820b4e0 | Pinned candidate; archive acceptance report exists, but rerun after finalized-head consistency fix before final release acceptance |

## Provisional candidates requiring independent archive verification

| AMM | Candidate factory | Discovery evidence | Missing acceptance evidence |
|---|---|---|---|
| SushiSwap V2 | 0x71524B4f93c58fcbF659783284E38825f0622859 | Trading Strategy identifies it as Sushi V2 on Base; BaseScan indexed a PairCreated log from this address | Creation transaction, deployment block, code hash, two-provider finalized-state match, full event-history reconciliation |
| BaseSwap V2 | 0xFDa619b6d20975be80A10332cD39b9a4b0FAa8BB | Trading Strategy identifies it as a Uniswap-V2-like Base DEX; BaseScan labels the address BaseSwap Factory and exposes verified source/creation activity | Canonical creation receipt/block, two-provider finalized-state match, PairCreated ABI/event completeness and full inventory |
| QuickSwap Base V2 | 0xEC6540261aaaE13F236A032d454dc9287E52e56A | QuickSwap's official contracts page lists this as its Base V2 factory | Creation transaction, deployment block, runtime hash, V2 ABI and both-provider archive verification |
| SwapBased V2 | 0x04C9f118d21e8B767D2e50C946f0cC9F6C367300 | SwapBased's official contract-address page lists this as UniswapV2Factory and publishes V2 pool addresses | Creation transaction, deployment block, runtime hash, V2 ABI and both-provider archive verification |

## Excluded from this V2 registry until separately scoped

- Concentrated-liquidity / V3 factories (including Pancake V3, Uniswap V3, Algebra and Aerodrome Slipstream) do not implement the V2 PairCreated / allPairsLength / allPairs(index) inventory profile.
- A contract merely called a pool factory is not a V2 pair factory. For example, pools.fun PartyFactory and WindSwap PoolFactory require separate ABI and event-history adapters, not forced compatibility with this reader.
- Router, locker, staking, farm, launchpad and gauge factories are not AMM pair factories.
- Do not infer PancakeSwap V2 deployment on Base from its V3 contracts or from a generic cross-chain deployment list.

## Acceptance protocol for every provisional candidate

1. Obtain the canonical factory creation transaction and deployment block from the protocol's deployment record and explorer transaction receipt.
2. Query Alchemy and SolidRPC at one Alchemy finalized anchor. Both must return the same block hash at that exact height.
3. On both providers, verify chain ID 8453, successful creation receipt, receipt contract address/block, non-empty runtime bytecode and matching Keccak-256 code hash.
4. Confirm V2 factory interface (allPairsLength, allPairs, getPair, PairCreated) and pair implementation (token0, token1, factory).
5. Scan the entire deployment-to-finalized interval in bounded log chunks. Reconcile the complete ordered PairCreated sequence and pair indices with allPairsLength; reject provider result caps, missing ranges, duplicate logs, removed logs, malformed provenance or unexplained gaps.
6. Enumerate all indexed pairs and reconcile the pool inventory. Only then add the factory to the pinned registry and reconsider the completeness flag.

## Source references

- Uniswap V2 Base deployment: https://developers.uniswap.org/docs/protocols/v2/deployments
- QuickSwap contracts and addresses: https://docs.quickswap.exchange/overview/contracts-and-addresses
- SwapBased contract addresses: https://docs.swapbased.finance/informational/contract-adresses
- BaseSwap factory explorer: https://basescan.org/address/0xFDa619b6d20975be80A10332cD39b9a4b0FAa8BB
- SushiSwap Base factory market reference: https://tradingstrategy.ai/trading-view/base/0x71524b4f93c58fcbf659783284e38825f0622859
- SushiSwap factory PairCreated explorer transaction: https://basescan.org/tx/0x765f108d41fdf48c134723a42a24baa5780f4a58e37627362d1c93a18a2f29db

This register is deliberately not imported as an accepted registry. The runtime registry remains incomplete and the Base liquidity reader remains fail-closed.
