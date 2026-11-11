# Phantom Devnet swap page

This static page is the wallet-owner signing boundary for the isolated Raydium Devnet swap test.

## Operating constraints

- Open the page inside Phantom's in-app browser on Android.
- Connect the intended Phantom wallet and verify it is on Solana Devnet.
- The page independently checks the canonical Devnet genesis hash.
- The only supported pair is wrapped SOL to the official Devnet USDC mint.
- Input is capped at 0.005 SOL and the page retains a 0.05 SOL balance reserve.
- A quote with price impact above 2% is rejected before transaction construction.
- The unsigned transaction bundle is inspected for the connected fee payer, exactly one required signer, and allowlisted program IDs; each transaction is simulated before it is offered for approval.
- The user must press the approval button and confirm the browser prompt, then review the Phantom approval screen.
- The page never accepts, derives, exports, or stores a seed phrase or private key.

## Local preview

Serve the repository root over HTTPS or localhost, then open `/wallet/phantom-devnet-swap.html` inside Phantom's in-app browser. A static file opened outside Phantom will not have an injected Phantom provider.

The page currently uses the public Solana Devnet RPC and Raydium Devnet Trade API. Public Devnet faucet access is not required because the wallet owner already funds the Devnet wallet.

## Evidence boundary

A quote or successful simulation is not a swap. A swap is counted as successful only after Phantom returns transaction signatures, Devnet confirms every transaction, and the resulting Devnet USDC balance is independently checked. This interface does not enable Mainnet or unattended trading.

## Release gate

Before public hosting, run the browser flow against Devnet with a disposable test wallet first. Verify the Phantom provider behavior, transaction bundle, output-token account creation, and confirmation UX. Do not use a Mainnet wallet or approve a Mainnet transaction during this test.
