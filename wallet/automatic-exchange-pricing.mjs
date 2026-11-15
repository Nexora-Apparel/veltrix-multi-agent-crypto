// Deterministic quote protection and fee accounting for the Veltrix Devnet swap flow.
// This module is deliberately side-effect free: it never signs, submits, or retries.
const BPS = 10_000n;
const U64_MAX = (1n << 64n) - 1n;

function fail(message) { throw new Error("QUOTE_POLICY_REJECTED: " + message); }
function asBigInt(value, label) {
  if (typeof value === "bigint") return value;
  if (typeof value !== "string" || !/^(0|[1-9][0-9]*)$/.test(value)) fail(label + " must be an unsigned integer string");
  return BigInt(value);
}
function asFinite(value, label) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) fail(label + " must be finite and non-negative");
  return n;
}
function ceilDiv(n, d) { return (n + d - 1n) / d; }
function clamp(n, min, max) { return Math.max(min, Math.min(max, n)); }

export function computeProtectionBps({
  quoteAgeMs, quoteTtlMs, liquidityUsd, tradeUsd, volatilityBps, routeHops
}) {
  const age = asFinite(quoteAgeMs, "quoteAgeMs");
  const ttl = asFinite(quoteTtlMs, "quoteTtlMs");
  const liquidity = asFinite(liquidityUsd, "liquidityUsd");
  const trade = asFinite(tradeUsd, "tradeUsd");
  const volatility = asFinite(volatilityBps, "volatilityBps");
  const hops = Number(routeHops);
  if (ttl <= 0 || liquidity <= 0 || trade <= 0 || !Number.isInteger(hops) || hops < 1 || hops > 4) {
    fail("invalid quote TTL, liquidity, trade size or route hop count");
  }
  if (age > ttl) fail("quote is expired and must be refreshed before review");
  const freshnessRisk = Math.ceil((age / ttl) * 40);
  const liquidityRatioBps = Math.ceil((trade / liquidity) * 10_000);
  if (liquidityRatioBps > 2_000) fail("trade size exceeds 20% of reported route liquidity");
  const liquidityRisk = Math.ceil(liquidityRatioBps / 4);
  const volatilityRisk = Math.ceil(volatility / 2);
  const routeRisk = (hops - 1) * 12;
  return clamp(15 + freshnessRisk + liquidityRisk + volatilityRisk + routeRisk, 15, 300);
}

export function calculateMinimumOutput(quotedOutputRaw, protectionBps) {
  const quoted = asBigInt(quotedOutputRaw, "quotedOutputRaw");
  const bps = Number(protectionBps);
  if (quoted <= 0n || quoted > U64_MAX || !Number.isInteger(bps) || bps < 0 || bps >= 10_000) {
    fail("invalid quoted output or protection threshold");
  }
  const minimum = (quoted * (BPS - BigInt(bps))) / BPS;
  if (minimum <= 0n) fail("protection threshold produces a zero minimum output");
  return minimum.toString();
}

export function calculateFeeBreakdown({
  dexFee, creatorFee, networkFeeLamports, routeFee, platformFeeConfig, outputRaw
}) {
  const output = asBigInt(outputRaw, "outputRaw");
  if (output <= 0n) fail("outputRaw must be positive");
  const normalize = (fee, label) => {
    if (!fee) fail(label + " fee must be explicitly itemized, including an explicit zero");
    const amount = asBigInt(fee.amountRaw, label + ".amountRaw");
    if (typeof fee.currency !== "string" || !fee.currency.trim()) fail(label + " currency is missing");
    return { category: label, amountRaw: amount.toString(), currency: fee.currency, disclosure: fee.disclosure || "explicit" };
  };
  const dex = normalize(dexFee, "dex");
  const creator = normalize(creatorFee, "creator");
  const route = normalize(routeFee, "route");
  const network = asBigInt(networkFeeLamports, "networkFeeLamports");
  const platform = { category: "platform", amountRaw: "0", currency: "output-token", recipient: null, enabled: false };
  if (platformFeeConfig?.enabled === true) {
    const rate = Number(platformFeeConfig.rateBps);
    const recipient = platformFeeConfig.recipient;
    if (!Number.isInteger(rate) || rate < 1 || rate > 500 ||
        typeof recipient !== "string" || recipient.trim().length < 32 ||
        platformFeeConfig.instructionVerified !== true) {
      fail("platform fee requires an explicit bounded rate, recipient and independently verified transaction instruction");
    }
    const amount = (output * BigInt(rate)) / BPS;
    if (amount <= 0n || amount >= output) fail("platform fee rounds to zero or consumes the output");
    platform.amountRaw = amount.toString();
    platform.recipient = recipient;
    platform.rateBps = rate;
    platform.enabled = true;
  }
  return Object.freeze({
    dex: Object.freeze(dex),
    creator: Object.freeze(creator),
    network: Object.freeze({ category: "network", amountRaw: network.toString(), currency: "SOL" }),
    route: Object.freeze(route),
    platform: Object.freeze(platform),
    totalNetworkFeeLamports: network.toString(),
    platformFeeRaw: platform.amountRaw
  });
}

function canonical(value) {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort().map(k => JSON.stringify(k) + ":" + canonical(value[k])).join(",") + "}";
  }
  return JSON.stringify(value);
}

export async function createQuoteSnapshot(input, nowMs = Date.now()) {
  if (!input || typeof input !== "object") fail("quote input is missing");
  const timestamp = Number(input.quotedAtMs);
  const ttl = Number(input.quoteTtlMs);
  if (!Number.isSafeInteger(timestamp) || !Number.isSafeInteger(ttl) || ttl <= 0 ||
      !Number.isSafeInteger(nowMs) || nowMs < timestamp || nowMs - timestamp > ttl) {
    fail("quote timestamp or expiry is invalid");
  }
  const protectionBps = computeProtectionBps({
    quoteAgeMs: nowMs - timestamp, quoteTtlMs: ttl, liquidityUsd: input.liquidityUsd,
    tradeUsd: input.tradeUsd, volatilityBps: input.volatilityBps, routeHops: input.routeHops
  });
  const outputRaw = asBigInt(input.quotedOutputRaw, "quotedOutputRaw");
  const fees = calculateFeeBreakdown({
    dexFee: input.dexFee, creatorFee: input.creatorFee, networkFeeLamports: input.networkFeeLamports,
    routeFee: input.routeFee, platformFeeConfig: input.platformFeeConfig, outputRaw
  });
  const platformFee = BigInt(fees.platformFeeRaw);
  const netOutputRaw = outputRaw - platformFee;
  const minimumOutputRaw = calculateMinimumOutput(netOutputRaw.toString(), protectionBps);
  const inputAmount = asBigInt(input.inputAmountRaw, "inputAmountRaw");
  if (inputAmount <= 0n || inputAmount > U64_MAX) fail("input amount is outside the positive u64 range");
  const inputDecimals = Number(input.inputDecimals);
  const outputDecimals = Number(input.outputDecimals);
  if (!Number.isInteger(inputDecimals) || inputDecimals < 0 || inputDecimals > 18 ||
      !Number.isInteger(outputDecimals) || outputDecimals < 0 || outputDecimals > 18) {
    fail("input/output token decimals are missing or invalid");
  }
  const tradeSizeRatio = Number(input.tradeUsd);
  if (!Number.isFinite(tradeSizeRatio) || tradeSizeRatio <= 0) fail("trade-size/liquidity ratio is invalid");
  const snapshot = {
    schema: "veltrix.quote.v1",
    network: "solana-devnet",
    quoteId: String(input.quoteId || ""),
    quotedAtMs: timestamp,
    expiresAtMs: timestamp + ttl,
    inputMint: String(input.inputMint || ""),
    outputMint: String(input.outputMint || ""),
    inputAmountRaw: inputAmount.toString(),
    inputDecimals, outputDecimals, tradeSizeRatio,
    quotedOutputRaw: outputRaw.toString(),
    netOutputRaw: netOutputRaw.toString(),
    minimumOutputRaw,
    protectionBps,
    routeHops: Number(input.routeHops),
    poolLiquidityUsd: Number(input.liquidityUsd),
    volatilityBps: Number(input.volatilityBps),
    fees
  };
  if (!snapshot.quoteId || !snapshot.inputMint || !snapshot.outputMint || snapshot.inputMint === snapshot.outputMint) {
    fail("quote identity or mint pair is invalid");
  }
  const bytes = new TextEncoder().encode(canonical(snapshot));
  if (!globalThis.crypto?.subtle) fail("Web Crypto SHA-256 is unavailable; immutable quote snapshot cannot be sealed");
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  const hash = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
  return Object.freeze({ ...snapshot, snapshotSha256: hash });
}

export async function evaluateFreshQuote(snapshot, freshQuote, nowMs = Date.now()) {
  if (!snapshot || typeof snapshot.snapshotSha256 !== "string") fail("sealed quote snapshot is missing");
  const { snapshotSha256, ...snapshotPayload } = snapshot;
  if (!globalThis.crypto?.subtle) fail("Web Crypto SHA-256 is unavailable for quote integrity verification");
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical(snapshotPayload)));
  const actualHash = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
  if (actualHash !== snapshotSha256) fail("immutable quote snapshot hash mismatch");
  if (!Number.isSafeInteger(nowMs) || nowMs > snapshot.expiresAtMs) {
    return { action: "refresh_and_re_review", reason: "reviewed quote expired" };
  }
  if (!freshQuote || !Number.isSafeInteger(freshQuote.quotedAtMs) ||
      nowMs < freshQuote.quotedAtMs || nowMs - freshQuote.quotedAtMs > freshQuote.quoteTtlMs) {
    return { action: "refresh_and_re_review", reason: "fresh quote is missing or stale" };
  }
  const freshProtectionBps = computeProtectionBps({
    quoteAgeMs: nowMs - freshQuote.quotedAtMs,
    quoteTtlMs: freshQuote.quoteTtlMs,
    liquidityUsd: freshQuote.liquidityUsd ?? snapshot.poolLiquidityUsd,
    tradeUsd: freshQuote.tradeUsd ?? snapshot.tradeSizeRatio,
    volatilityBps: freshQuote.volatilityBps ?? snapshot.volatilityBps,
    routeHops: freshQuote.routeHops ?? snapshot.routeHops
  });
  if (freshProtectionBps > snapshot.protectionBps) {
    return { action: "refresh_and_re_review", reason: "current liquidity, volatility or route risk requires a wider protection threshold" };
  }
  const freshOutput = asBigInt(freshQuote.quotedOutputRaw, "freshQuote.quotedOutputRaw");
  if (freshOutput < BigInt(snapshot.minimumOutputRaw)) {
    return { action: "refresh_and_re_review", reason: "fresh output is below reviewed minimum" };
  }
  if (freshQuote.inputAmountRaw !== snapshot.inputAmountRaw ||
      freshQuote.inputMint !== snapshot.inputMint || freshQuote.outputMint !== snapshot.outputMint) {
    return { action: "refresh_and_re_review", reason: "fresh quote changed the reviewed trade identity" };
  }
  return { action: "continue_exact_review", reason: "fresh quote clears the immutable minimum output" };
}

export function reconcileWalletBalanceChanges({
  inputSolBefore, inputSolAfter, outputTokenBefore, outputTokenAfter,
  expectedInputLamports, actualNetworkFeeLamports, persistentRentLamports,
  minimumOutputRaw, expectedPlatformFeeRaw = "0", actualPlatformFeeRaw = "0"
}) {
  const solBefore = asBigInt(inputSolBefore, "inputSolBefore");
  const solAfter = asBigInt(inputSolAfter, "inputSolAfter");
  const tokenBefore = asBigInt(outputTokenBefore, "outputTokenBefore");
  const tokenAfter = asBigInt(outputTokenAfter, "outputTokenAfter");
  const input = asBigInt(expectedInputLamports, "expectedInputLamports");
  const network = asBigInt(actualNetworkFeeLamports, "actualNetworkFeeLamports");
  const rent = asBigInt(persistentRentLamports, "persistentRentLamports");
  const minimum = asBigInt(minimumOutputRaw, "minimumOutputRaw");
  const platform = asBigInt(expectedPlatformFeeRaw, "expectedPlatformFeeRaw");
  const actualPlatform = asBigInt(actualPlatformFeeRaw, "actualPlatformFeeRaw");
  if (solAfter > solBefore || tokenAfter < tokenBefore) fail("wallet balance directions are inconsistent with a swap");
  const solDebit = solBefore - solAfter;
  const tokenCredit = tokenAfter - tokenBefore;
  const expectedSolDebit = input + network + rent;
  if (solDebit !== expectedSolDebit) fail("SOL debit does not reconcile to input, actual network fee and persistent account rent");
  if (tokenCredit < minimum) fail("received token balance delta is below the reviewed minimum output");
  if (actualPlatform !== platform) fail("actual platform-fee transfer does not match the explicitly disclosed fee");
  return Object.freeze({
    reconciled: true,
    solDebitLamports: solDebit.toString(),
    inputLamports: input.toString(),
    networkFeeLamports: network.toString(),
    persistentRentLamports: rent.toString(),
    outputTokenCreditRaw: tokenCredit.toString(),
    minimumOutputRaw: minimum.toString(),
    platformFeeRaw: actualPlatform.toString()
  });
}

export const QUOTE_POLICY = Object.freeze({ minimumProtectionBps: 15, maximumProtectionBps: 300, platformFeeMaximumBps: 500 });
