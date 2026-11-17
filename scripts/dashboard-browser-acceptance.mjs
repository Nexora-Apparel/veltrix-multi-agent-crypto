import assert from "node:assert/strict";
import { chromium } from "playwright";

const baseUrl = process.env.VELTRIX_PAGES_TEST_URL || "http://127.0.0.1:8788";
const browser = await chromium.launch({ headless: true });
try {
  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  await mobile.route("**/api/**", (route) => route.fulfill({
    status: 503, contentType: "application/json", body: JSON.stringify({ error: "test backend unavailable" }),
  }));
  await mobile.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await mobile.waitForSelector("#page-title");
  assert.equal(await mobile.locator("#page-title").textContent(), "System overview");
  const nav = mobile.locator(".primary-nav");
  assert.equal(await nav.isVisible(), true);
  assert.equal(await nav.evaluate((node) => getComputedStyle(node).overflowX), "auto");

  const routes = [
    ["Solana", "solana", "Solana intelligence"],
    ["Base", "base", "Base intelligence"],
    ["BSC", "bsc", "BSC intelligence"],
    ["History", "history", "Decision and ledger history"],
    ["Diagnostics", "diagnostics", "System diagnostics"],
    ["Overview", "overview", "System overview"],
  ];
  for (const [label, hash, title] of routes) {
    await mobile.getByRole("link", { name: label, exact: true }).click();
    await mobile.waitForFunction((value) => location.hash === "#" + value, hash);
    assert.equal(await mobile.locator("#page-title").textContent(), title);
  }
  await mobile.setViewportSize({ width: 360, height: 800 });
  assert.equal(await mobile.locator(".primary-nav").isVisible(), true);
  await mobile.goto(baseUrl + "#base", { waitUntil: "domcontentloaded" });
  assert.equal(await mobile.locator("#page-title").textContent(), "Base intelligence");
  await mobile.locator("#base-data-status").waitFor();
  await mobile.close();

  const auth = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await auth.route("**/api/**", (route) => route.fulfill({
    status: 401, contentType: "application/json", body: JSON.stringify({ error: "unauthorized" }),
  }));
  await auth.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await auth.evaluate(() => sessionStorage.setItem("veltrix_dashboard_api_token", "revoked-token"));
  await auth.locator("#refresh").click();
  await auth.waitForFunction(() => !sessionStorage.getItem("veltrix_dashboard_api_token"));
  await auth.waitForFunction(() => document.querySelector("#health-detail").textContent.includes("expired or was revoked"));
  await auth.close();

  const offline = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await offline.route("**/api/**", (route) => route.abort());
  await offline.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await offline.waitForFunction(() => document.querySelector("#health-label").textContent === "Health check unavailable");
  assert.match(await offline.locator("#health-detail").textContent(), /fetch|network|failed/i);
  await offline.close();

  console.log("Dashboard browser acceptance passed: six routes, 390/360px layouts, revoked-token handling, HTTP 503 and offline failure.");
} finally {
  await browser.close();
}
