import assert from "node:assert/strict";
import { chromium } from "playwright";

const baseUrl = process.env.VELTRIX_PAGES_TEST_URL || "http://127.0.0.1:8788";
const browser = await chromium.launch({ headless: true });
try {
  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const requested = new Set();
  await mobile.route("**/api/**", async (route) => {
    requested.add(new URL(route.request().url()).pathname + new URL(route.request().url()).search);
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "test backend unavailable" }) });
  });
  await mobile.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await mobile.waitForSelector("#screen-home");
  await mobile.waitForFunction(() => document.querySelector("#health-label").textContent.includes("unavailable"));
  for (const screen of ["home", "trades", "agents", "wallet"]) {
    await mobile.locator('[data-screen="' + screen + '"]').click();
    assert.equal(await mobile.locator("#screen-" + screen).evaluate((node) => node.classList.contains("active")), true);
  }
  await mobile.setViewportSize({ width: 360, height: 800 });
  assert.equal(await mobile.locator(".bottom-nav").isVisible(), true);
  assert.equal(await mobile.locator("#network-grid").evaluate((node) => node.scrollWidth <= node.clientWidth), true);
  for (const chain of ["solana", "base", "bsc"]) assert.ok([...requested].some(path => path.includes("/api/chain-markets?chain=" + chain)));
  assert.ok([...requested].some(path => path.startsWith("/api/runtime-status")));
  await mobile.close();

  const auth = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await auth.route("**/api/**", (route) => route.fulfill({
    status: 401, contentType: "application/json", body: JSON.stringify({ error: "unauthorized" }),
  }));
  await auth.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await auth.waitForSelector("#access-gate:not([hidden])");
  await auth.evaluate(() => { sessionStorage.setItem("veltrix_dashboard_api_token", "revoked-token"); });
  await auth.locator("#access-token").fill("revoked-token");
  await auth.locator("#access-submit").click();
  await auth.waitForFunction(() => !sessionStorage.getItem("veltrix_dashboard_api_token"));
  await auth.waitForFunction(() => document.querySelector("#access-error").textContent.includes("revoked"));
  await auth.close();

  const offline = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await offline.route("**/api/**", (route) => route.abort());
  await offline.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await offline.waitForFunction(() => document.querySelector("#health-label").textContent.includes("unavailable"));
  assert.match(await offline.locator("#health-detail").textContent(), /Partial data|failed|network/i);
  await offline.close();

  console.log("Mobile dashboard acceptance passed: four screens, 390/360px layouts, Solana/Base/BSC data, runtime telemetry, revoked-token handling, HTTP 503 and offline isolation.");
} finally {
  await browser.close();
}
