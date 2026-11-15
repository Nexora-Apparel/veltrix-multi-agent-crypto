# VELTRIX Swap Execution Policy

Status: implementation contract for the Devnet swap path. This document does not enable mainnet or automated wallet signing.

## Product behavior

VELTRIX should not impose arbitrary business limits on otherwise valid swaps. The swap builder should accept any positive amount that the wallet can fund and that the selected route/program can encode and execute. It must not hard-code a single test amount, a universal price-impact ceiling, a fixed number of route accounts, or a fixed ALT address.

The builder should discover the route, pool state, token accounts, required program accounts, and every address lookup table from the selected quote and the actual compiled message. Supported transaction versions are selected from the connected wallet's advertised capabilities.

## Limits: remove versus retain

Remove application-imposed limits that are not required by Solana or the selected protocol:
- fixed 0.001 SOL amount and TEST_LAMPORTS assumptions;
- universal 2% price-impact ceiling;
- fixed route/pool/account/ALT counts and fixed ALT addresses;
- arbitrary one-request faucet ceilings in production funding paths;
- assumptions that compiled instruction indexes or static account ordering remain unchanged.

Do not confuse those with protocol constraints. The Solana packet-size limit, integer/account encoding bounds, actual wallet balance, rent requirements, blockhash validity, RPC/provider quotas, and protocol-defined swap constraints are real execution constraints. The builder should surface these constraints accurately and avoid inventing additional policy ceilings.

Price impact should be shown as quote information. The user-selected minimum output/slippage protection remains encoded in the transaction and shown before wallet approval; it is not replaced by a universal impact threshold.

## Automatic Veltrix fee

The application fee is calculated from the actual input or output amount using integer arithmetic and a configured basis-point rate. The fee transfer is added to the same transaction where the chosen route/protocol supports atomic fee collection. The transaction review must display:
- swap amount;
- Veltrix fee rate and exact fee amount;
- fee asset and destination;
- network fee and rent separately;
- expected net wallet debit and minimum received output.

The fee is collected automatically as part of the approved swap transaction. There is no separate manual fee step. Fee accounting is persisted with the transaction signature and reconciled against confirmed chain effects.

Required configuration:
- `VELTRIX_FEE_RECIPIENT`: public wallet address controlled by Veltrix;
- `VELTRIX_FEE_BPS`: integer fee rate in basis points;
- `VELTRIX_FEE_ASSET`: explicit fee denomination (input or output asset).

These values must be supplied by the project owner. They must never be guessed, embedded as a fabricated address, or silently defaulted to an undisclosed charge. If configuration is absent, fee collection is marked unconfigured and the swap UI must not claim a Veltrix fee was collected. Network fees charged by Solana and pool/protocol fees charged by the AMM are separate and must not be mislabeled as Veltrix revenue.

## Transaction acceptance and reconciliation

The user approves the transaction in Phantom. The browser audits the exact transaction being presented, including signer/fee payer, route program identities, writable privileges, source and destination token accounts, amount bounds, fee destination, and all asset movements. Unknown instructions are not accepted merely to avoid a failure; the route adapter must understand the instruction and its effects before it can be enabled.

After submission, reconciliation uses the confirmed transaction and token/native balance effects. It records the signature, actual input debit, actual output credit, Veltrix fee transfer, network fee, and execution error. A confirmed signature with unavailable metadata is reported as confirmed-but-unverified, never as a failed swap that should be retried.

## Implementation sequence

1. Refactor amount handling so the actual quote amount is passed through all outer and CPI audits; remove global fixed-amount assumptions.
2. Replace the universal impact ceiling with quote display plus explicit minimum-output/slippage handling.
3. Replace fixed route/account/ALT assumptions with route-adapter-driven discovery and resolved account identities.
4. Add the fee configuration contract and integer fee calculator; validate destination and denomination.
5. Add atomic fee transfer to each supported route adapter and include it in the reviewed transaction.
6. Extend post-confirmation reconciliation to prove the exact fee transfer and net asset effects.
7. Add tests for zero/large amounts within protocol encoding, fee rounding, missing configuration, fee destination substitution, unexpected transfers, multi-ALT routes, and transaction-size boundary behavior.

## Current implementation gap

The existing `wallet/phantom-devnet-swap.html` is still a narrow Devnet test path: it pins a direct CPMM pool, hard-codes 0.001 SOL in multiple instruction/CPI checks, applies a 2% price-impact ceiling and 1% minimum-output floor, and has no configured Veltrix fee recipient/rate or platform-fee transfer. Those are known implementation gaps; this policy document does not represent them as already fixed.

The next code change must refactor the transaction intent/evidence objects to carry the requested raw input amount and fee plan end-to-end. Do not perform a partial text-only removal of constants while leaving the CPI and post-confirmation audits pinned to 1,000,000 lamports.
