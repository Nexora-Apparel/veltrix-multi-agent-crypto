import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const root = new URL("../veltrix/dashboard_assets/", import.meta.url);

test("dashboard exposes all required navigation routes and matching page views", async () => {
  const html = await readFile(new URL("index.html", root), "utf8");
  const routes = ["overview", "solana", "base", "bsc", "history", "diagnostics"];
  for (const route of routes) {
    assert.match(html, new RegExp('data-route="' + route + '"'));
    assert.match(html, new RegExp('data-page="' + route + '"'));
  }
});

test("Base and BSC views render only persisted chain-specific market history", async () => {
  const html = await readFile(new URL("index.html", root), "utf8");
  const js = await readFile(new URL("dashboard.js", root), "utf8");
  assert.match(html, /Persisted Base observations/);
  assert.match(html, /Persisted BSC observations/);
  assert.match(html, /id="base-markets-body"/);
  assert.match(html, /id="bsc-markets-body"/);
  assert.match(js, /api\("\/api\/chain-markets\?chain="/);
  assert.match(js, /No persisted /);
});

test("diagnostics distinguish API health fields from unavailable agent and provider telemetry", async () => {
  const html = await readFile(new URL("index.html", root), "utf8");
  const js = await readFile(new URL("dashboard.js", root), "utf8");
  assert.match(html, /Agent heartbeat/);
  assert.match(html, /Provider connectivity/);
  assert.match(html, /Not exposed by current API/);
  assert.match(html, /id="diag-last-market"/);
  assert.match(html, /id="diag-last-consensus"/);
  assert.match(html, /id="diag-pipeline"/);
  assert.match(js, /function renderDiagnostics\(health, overview\)/);
  assert.match(js, /health\.database_read_only === true/);
  assert.match(js, /health\.live_execution === false/);
  assert.match(js, /overview\.latest_updates/);
  assert.match(js, /overview\.pipeline/);
});

test("dashboard navigation is client-side, supports direct hash routes and rejects unknown routes", async () => {
  const js = await readFile(new URL("dashboard.js", root), "utf8");
  assert.match(js, /window\.addEventListener\("hashchange"/);
  assert.match(js, /setPage\(location\.hash\.slice\(1\) \|\| "overview"\)/);
  assert.match(js, /Object\.hasOwn\(pageMeta, page\) \? page : "overview"/);
});

test("dashboard contains no execution, signing, transaction submission or scheduler controls", async () => {
  const html = await readFile(new URL("index.html", root), "utf8");
  assert.doesNotMatch(html, /<button[^>]*(trade|execute|sign|submit|broadcast|schedule)/i);
  assert.doesNotMatch(html, /name=["'](private_key|seed_phrase|wallet_secret)["']/i);
});
