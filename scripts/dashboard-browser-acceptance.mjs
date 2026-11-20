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

  // Exercise the new pipeline controls against a fully mocked API. No real GitHub
  // dispatch or production endpoint is contacted by this browser acceptance.
  const control = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  await control.addInitScript(() => {
    sessionStorage.setItem("veltrix_dashboard_api_token", "test-dashboard-token-for-control-ui-only");
  });
  let controlRequestId = null;
  let controlRunStatus = "DISPATCH_PENDING";
  const controlRunId = 246810;
  await control.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    let payload = {};
    if (url.pathname === "/api/health") payload = { database: "connected", missing_paper_tables: [], live_execution: false, database_read_only: true };
    else if (url.pathname === "/api/overview") payload = { account: {}, performance: {} };
    else if (url.pathname === "/api/equity-curve") payload = { points: [] };
    else if (url.pathname === "/api/runtime-status") payload = { workers: { status: "UNKNOWN", items: [] }, agents: { status: "UNKNOWN", items: [] }, providers: { status: "UNKNOWN", items: [] }, live_execution: false };
    else if (url.pathname === "/api/chain-markets") payload = { available: true, observation_count: 0, observations: [] };
    else payload = [];
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(payload) });
  });
  await control.route("**/api/pipeline/status**", async (route) => {
    const url = new URL(route.request().url());
    const requestId = url.searchParams.get("request_id");
    const matching = Boolean(controlRequestId && requestId === controlRequestId);
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify(matching
        ? { status: controlRunStatus, request_id: requestId, run_id: controlRunId, conclusion: controlRunStatus === "COMPLETED" ? "cancelled" : null, created_at: "2026-11-20T16:00:00Z" }
        : { status: "DISPATCH_PENDING", request_id: requestId }),
    });
  });
  await control.route("**/api/pipeline/activate", async (route) => {
    const body = route.request().postDataJSON();
    assert.match(body.request_id, /^[0-9a-f-]{36}$/i);
    controlRequestId = body.request_id;
    controlRunStatus = "QUEUED";
    await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ status: "DISPATCH_ACCEPTED", request_id: controlRequestId }) });
  });
  await control.route("**/api/pipeline/cancel", async (route) => {
    const body = route.request().postDataJSON();
    assert.equal(body.request_id, controlRequestId);
    assert.equal(body.run_id, controlRunId);
    controlRunStatus = "COMPLETED";
    await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ status: "CANCEL_REQUESTED", request_id: controlRequestId, run_id: controlRunId }) });
  });
  control.on("dialog", (dialog) => dialog.accept());
  await control.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await control.waitForFunction(() => document.querySelector("#pipeline-control-status").textContent === "READY");
  assert.equal(await control.locator("#pipeline-activate").isEnabled(), true);
  await control.locator("#pipeline-activate").click();
  await control.waitForFunction(() => document.querySelector("#pipeline-control-status").textContent === "QUEUED");
  assert.equal(await control.locator("#pipeline-cancel").isEnabled(), true);
  await control.locator("#pipeline-cancel").click();
  await control.waitForFunction(() => document.querySelector("#pipeline-control-status").textContent === "COMPLETED");
  assert.equal(await control.locator("#pipeline-cancel").isDisabled(), true);
  assert.equal(await control.evaluate(() => sessionStorage.getItem("veltrix_pipeline_request_id")), controlRequestId);
  await control.close();

  console.log("Mobile dashboard acceptance passed: four screens, 390/360px layouts, Solana/Base/BSC data, runtime telemetry, revoked-token handling, HTTP 503/offline isolation, and mocked pipeline readiness/activation/exact-run cancellation.");
} finally {
  await browser.close();
}
