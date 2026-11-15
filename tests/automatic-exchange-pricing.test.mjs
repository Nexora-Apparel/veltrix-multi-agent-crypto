import assert from "node:assert/strict";
import {
  computeProtectionBps, calculateMinimumOutput, calculateFeeBreakdown,
  createQuoteSnapshot, evaluateFreshQuote, reconcileWalletBalanceChanges
} from "../wallet/automatic-exchange-pricing.mjs";
import * as browserPricingEntry from "../automatic-exchange-pricing.mjs";
assert.equal(browserPricingEntry.calculateMinimumOutput("10904", 250), "10631");

const base = {
  quoteId: "devnet-quote-001", quotedAtMs: 1_000_000, quoteTtlMs: 8_000,
  inputMint: "SoFQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U111111112",
  outputMint: "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
  inputAmountRaw: "5000000", inputDecimals: 9, outputDecimals: 6, quotedOutputRaw: "10904",
  liquidityUsd: 50_000, tradeUsd: 5, volatilityBps: 20, routeHops: 1,
  dexFee: { amountRaw: "15000", currency: "SOL", disclosure: "trade fee from on-chain CPMM quote" },
  creatorFee: { amountRaw: "0", currency: "SOL", disclosure: "creator fee from on-chain CPMM quote" },
  routeFee: { amountRaw: "0", currency: "USDC", disclosure: "no separate route fee" },
  networkFeeLamports: "5000"
};

assert.ok(computeProtectionBps({
  quoteAgeMs: 7000, quoteTtlMs: 8000, liquidityUsd: 50000,
  tradeUsd: 5, volatilityBps: 80, routeHops: 3
}) > computeProtectionBps({
  quoteAgeMs: 0, quoteTtlMs: 8000, liquidityUsd: 50000,
  tradeUsd: 5, volatilityBps: 0, routeHops: 1
}), "older, more volatile multi-hop quotes must receive a wider protection buffer");
assert.throws(() => computeProtectionBps({
  quoteAgeMs: 9000, quoteTtlMs: 8000, liquidityUsd: 50000,
  tradeUsd: 5, volatilityBps: 10, routeHops: 1
}), /expired/);
assert.throws(() => computeProtectionBps({
  quoteAgeMs: 0, quoteTtlMs: 8000, liquidityUsd: 100,
  tradeUsd: 30, volatilityBps: 10, routeHops: 1
}), /20%/);
assert.equal(calculateMinimumOutput("10904", 250), "10631");
assert.throws(() => calculateMinimumOutput("1", 300), /zero minimum/);

const fees = calculateFeeBreakdown({
  dexFee: base.dexFee, creatorFee: base.creatorFee, routeFee: base.routeFee, networkFeeLamports: "5000",
  outputRaw: "10904"
});
assert.equal(fees.dex.amountRaw, "15000");
assert.equal(fees.creator.amountRaw, "0");
assert.equal(fees.route.amountRaw, "0");
assert.equal(fees.network.amountRaw, "5000");
assert.equal(fees.platform.enabled, false);
assert.equal(fees.platform.amountRaw, "0");
const enabledPlatformFee = calculateFeeBreakdown({
  dexFee: base.dexFee, creatorFee: base.creatorFee, routeFee: base.routeFee,
  networkFeeLamports: "5000", outputRaw: "10904",
  platformFeeConfig: {
    enabled: true, rateBps: 50,
    recipient: "FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U",
    instructionVerified: true
  }
});
assert.equal(enabledPlatformFee.platformFeeRaw, "54", "platform fee must use deterministic floor rounding");
assert.throws(() => calculateFeeBreakdown({
  dexFee: base.dexFee, creatorFee: base.creatorFee, routeFee: base.routeFee,
  networkFeeLamports: "5000", outputRaw: "10904",
  platformFeeConfig: { enabled: true, rateBps: 50, recipient: "", instructionVerified: true }
}), /explicit bounded rate, recipient/);
assert.throws(() => calculateFeeBreakdown({
  dexFee: base.dexFee, creatorFee: base.creatorFee, routeFee: base.routeFee,
  networkFeeLamports: "5000", outputRaw: "1",
  platformFeeConfig: {
    enabled: true, rateBps: 50,
    recipient: "FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U",
    instructionVerified: true
  }
}), /rounds to zero/);
assert.throws(() => calculateFeeBreakdown({
  dexFee: base.dexFee, creatorFee: base.creatorFee, routeFee: base.routeFee, networkFeeLamports: "5000",
  outputRaw: "10904",
  platformFeeConfig: { enabled: true, rateBps: 50, recipient: "FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U", instructionVerified: false }
}), /verified transaction instruction/);

const snapshot = await createQuoteSnapshot(base, 1_002_000);
const sealedWithExistingProtection = await createQuoteSnapshot({
  ...base, quotedAtMs: 1_010_000, protectionBps: snapshot.protectionBps
}, 1_010_000);
assert.equal(sealedWithExistingProtection.protectionBps, snapshot.protectionBps,
  "sealing the review package must preserve the protection already applied to the transaction");
assert.equal(sealedWithExistingProtection.expiresAtMs, 1_018_000,
  "quote expiry begins when the completed review package is sealed");
const sameSnapshot = await createQuoteSnapshot(base, 1_002_000);
assert.equal(snapshot.snapshotSha256, sameSnapshot.snapshotSha256, "canonical quote snapshots must hash deterministically");
assert.equal(snapshot.minimumOutputRaw, calculateMinimumOutput(snapshot.quotedOutputRaw, snapshot.protectionBps));
assert.equal(snapshot.network, "solana-devnet");
assert.equal((await evaluateFreshQuote(snapshot, {
  quotedAtMs: 1_003_000, quoteTtlMs: 8000, quotedOutputRaw: "10900",
  inputAmountRaw: base.inputAmountRaw, inputMint: base.inputMint, outputMint: base.outputMint
}, 1_004_000)).action, "continue_exact_review");
assert.equal((await evaluateFreshQuote(snapshot, {
  quotedAtMs: 1_003_000, quoteTtlMs: 8000, quotedOutputRaw: (BigInt(snapshot.minimumOutputRaw) - 1n).toString(),
  inputAmountRaw: base.inputAmountRaw, inputMint: base.inputMint, outputMint: base.outputMint
}, 1_004_000)).action, "refresh_and_re_review");
assert.equal((await evaluateFreshQuote(snapshot, {
  quotedAtMs: 1_003_000, quoteTtlMs: 8000, quotedOutputRaw: "10904",
  inputAmountRaw: "4000000", inputMint: base.inputMint, outputMint: base.outputMint
}, 1_004_000)).action, "refresh_and_re_review");
assert.equal((await evaluateFreshQuote(snapshot, {
  quotedAtMs: 1_003_000, quoteTtlMs: 8000, quotedOutputRaw: "10904",
  inputAmountRaw: base.inputAmountRaw, inputMint: base.inputMint, outputMint: base.outputMint
}, 1_009_000)).action, "refresh_and_re_review");
const completedDuringFreshnessAudit = await evaluateFreshQuote(sealedWithExistingProtection, {
  quotedAtMs: 1_019_000, quoteTtlMs: 8000, quotedOutputRaw: "10900",
  inputAmountRaw: base.inputAmountRaw, inputMint: base.inputMint, outputMint: base.outputMint
}, 1_020_000, 1_017_000);
assert.equal(completedDuringFreshnessAudit.action, "continue_exact_review",
  "a signing review initiated before expiry may finish its live freshness audit after the wall-clock expiry");
assert.equal((await evaluateFreshQuote(sealedWithExistingProtection, {
  quotedAtMs: 1_019_000, quoteTtlMs: 8000, quotedOutputRaw: "10900",
  inputAmountRaw: base.inputAmountRaw, inputMint: base.inputMint, outputMint: base.outputMint
}, 1_020_000, 1_018_001)).action, "refresh_and_re_review",
  "a signing review initiated after expiry must still be refreshed");
assert.equal((await evaluateFreshQuote(snapshot, {
  quotedAtMs: 1_003_000, quoteTtlMs: 8000, quotedOutputRaw: "10904",
  inputAmountRaw: base.inputAmountRaw, inputMint: base.inputMint, outputMint: base.outputMint,
  liquidityUsd: 1, tradeUsd: 0.03, volatilityBps: 80, routeHops: 1
}, 1_004_000)).action, "refresh_and_re_review", "liquidity/volatility deterioration must force a wider protection and fresh review");
const tamperedSnapshot = {...snapshot, minimumOutputRaw: "1"};
await assert.rejects(() => evaluateFreshQuote(tamperedSnapshot, {
  quotedAtMs: 1_003_000, quoteTtlMs: 8000, quotedOutputRaw: "10904",
  inputAmountRaw: base.inputAmountRaw, inputMint: base.inputMint, outputMint: base.outputMint
}, 1_004_000), /snapshot hash mismatch/);
await assert.rejects(() => createQuoteSnapshot({...base, outputDecimals: 99}, 1_002_000), /decimals/);
await assert.rejects(() => createQuoteSnapshot({...base, outputDecimals: 8}, 1_002_000), /Devnet USDC output mint must use 6 decimals/);
assert.throws(() => calculateFeeBreakdown({
  dexFee: null, creatorFee: base.creatorFee, routeFee: base.routeFee,
  networkFeeLamports: "5000", outputRaw: "10904"
}), /must be explicitly itemized/);

const reconciled = reconcileWalletBalanceChanges({
  inputSolBefore: "100000000", inputSolAfter: "94990000",
  outputTokenBefore: "200", outputTokenAfter: "11104",
  expectedInputLamports: "5000000", actualNetworkFeeLamports: "5000",
  persistentRentLamports: "5000", minimumOutputRaw: "10000"
});
assert.equal(reconciled.reconciled, true);
assert.equal(reconciled.solDebitLamports, "5010000");
assert.equal(reconciled.outputTokenCreditRaw, "10904");
assert.throws(() => reconcileWalletBalanceChanges({
  inputSolBefore: "100000000", inputSolAfter: "94991000",
  outputTokenBefore: "200", outputTokenAfter: "11104",
  expectedInputLamports: "5000000", actualNetworkFeeLamports: "5000",
  persistentRentLamports: "5000", minimumOutputRaw: "10000"
}), /SOL debit/);
assert.throws(() => reconcileWalletBalanceChanges({
  inputSolBefore: "100000000", inputSolAfter: "94990000",
  outputTokenBefore: "200", outputTokenAfter: "10199",
  expectedInputLamports: "5000000", actualNetworkFeeLamports: "5000",
  persistentRentLamports: "5000", minimumOutputRaw: "10000"
}), /below the reviewed minimum output/);

console.log("Automatic quote protection, expiry, fee transparency and balance reconciliation tests passed.");
