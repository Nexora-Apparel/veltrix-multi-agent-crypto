import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const root = new URL("../veltrix/dashboard_assets/", import.meta.url);

test("mobile dashboard preserves the supplied VELTRIX navigation and visual identity", async () => {
  const html = await readFile(new URL("index.html", root), "utf8");
  const css = await readFile(new URL("dashboard.css", root), "utf8");
  for (const screen of ["home", "trades", "agents", "wallet"]) {
    assert.match(html, new RegExp('data-screen="' + screen + '"'));
    assert.match(html, new RegExp('id="screen-' + screen + '"'));
  }
  assert.match(html, /id="search-open"/);
  assert.match(html, /id="status-toggle"/);
  assert.match(html, /name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/);
  assert.match(css, /--bg:#090A0B/);
  assert.match(css, /--surface:#121417/);
  assert.match(css, /\.ambient\{display:block/);
  assert.match(css, /\.bottom-nav/);
});

test("mobile dashboard connects all three chains to persisted market observations", async () => {
  const html = await readFile(new URL("index.html", root), "utf8");
  const js = await readFile(new URL("dashboard.js", root), "utf8");
  for (const chain of ["solana", "base", "bsc"]) {
    assert.match(html, new RegExp('id="' + chain + '-count"'));
    assert.match(js, new RegExp('/api/chain-markets\\?chain=' + chain));
  }
  assert.match(js, /function renderNetworks\(\)/);
  assert.match(js, /observation_count/);
  assert.match(js, /latest_observed_at/);
});

test("agent and provider status is sourced from persisted runtime telemetry", async () => {
  const html = await readFile(new URL("index.html", root), "utf8");
  const js = await readFile(new URL("dashboard.js", root), "utf8");
  assert.match(html, /id="agent-runtime"/);
  assert.match(html, /id="provider-runtime"/);
  assert.match(js, /api\("\/api\/runtime-status"\)/);
  assert.match(js, /runtime\?\.agents\?\.items/);
  assert.match(js, /runtime\?\.providers\?\.items/);
  assert.match(js, /UNKNOWN/);
});

test("dashboard keeps tab-scoped authentication and clears rejected tokens", async () => {
  const js = await readFile(new URL("dashboard.js", root), "utf8");
  assert.match(js, /sessionStorage\.setItem\(tokenKey,token\)/);
  assert.match(js, /sessionStorage\.removeItem\(tokenKey\)/);
  assert.match(js, /expired or was revoked/);
});

test("dashboard isolates failed API sections without discarding successful data", async () => {
  const js = await readFile(new URL("dashboard.js", root), "utf8");
  assert.match(js, /Promise\.allSettled\(/);
  assert.match(js, /Partial data:/);
  assert.match(js, /Partially updated/);
});

test("dashboard contains no wallet signing, transfer, submission, broadcast or scheduler controls", async () => {
  const html = await readFile(new URL("index.html", root), "utf8");
  assert.doesNotMatch(html, /data-action=["'](trade|execute|sign|submit|broadcast|schedule)/i);
  assert.doesNotMatch(html, /name=["'](private_key|seed_phrase|wallet_secret)["']/i);
});
