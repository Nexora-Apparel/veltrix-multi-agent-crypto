# Phase 09 — Future Operator Safety Checklist

This checklist is a design/review aid for a separately approved future release. It does not authorize a live transaction. The current repository has no wallet signer or submission adapter; the execution coordinator stops at AUTHORIZED.

## Identity and wallet isolation

- [ ] Confirm the intended cluster and wallet public key through a trusted wallet UI.
- [ ] Use a dedicated, isolated wallet for any future live pilot; keep it separate from personal holdings and paper-trading state.
- [ ] Never provide a seed phrase, mnemonic, raw private key, exported wallet file or signing secret to Veltrix, ChatGPT, logs, GitHub, or support personnel.
- [ ] Confirm the application contains no key-import flow and no signer implementation.
- [ ] Confirm the wallet public key, chain and policy hash match the exact operator authorization.

## Policy and evidence

- [ ] Review per-trade, per-token, portfolio, daily-loss, slippage and priority-fee ceilings in integer base units.
- [ ] Verify the intent is bound to the exact input/output mints, spend, minimum output, allowed programs and account set.
- [ ] Verify finalized token-risk evidence is complete, provenance-checked and explicitly approved by a separate future live-risk review. The current Stage 05 collector does not emit LIVE_RISK_APPROVED.
- [ ] Verify market, RPC, risk and simulation timestamps are within policy freshness limits.
- [ ] Verify simulation message hash, account-state hash, output, fee and slot match the exact unsigned transaction manifest.
- [ ] Verify authorization is one-use, unexpired and bound to the wallet, chain, policy hash and intent ID.

## Emergency stop and audit

- [ ] Verify emergency stop is ACTIVE by default on an uninitialized store.
- [ ] Verify activation persists across process restarts and storage failures block the gate.
- [ ] Test activation from an independent operator path before any future live release.
- [ ] Verify audit events are append-only and contain only intent IDs, reason codes, state names and cryptographic hashes.
- [ ] Confirm no secrets, raw transaction bytes or private signing material appear in logs or audit records.

## Release control

- [ ] Require independent security review and full adversarial CI on the exact release commit.
- [ ] Keep production deployment, Telegram activation and scheduling disabled until their separate release gates pass.
- [ ] Require a separately approved signer adapter, raw Solana message decoder, simulation/RPC adapter, and submission/settlement reconciliation before any live execution can be considered.
- [ ] Do not interpret an AUTHORIZED preparation state as a signed, submitted, or confirmed transaction.
