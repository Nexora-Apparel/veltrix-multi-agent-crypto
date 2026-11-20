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



test("agent roster is sourced from persisted runtime telemetry, not consensus support alone", async () => {
  const js = await readFile(new URL("dashboard.js", root), "utf8");
  const renderAgents = js.match(/function renderAgents\(\)[\s\S]*?\n  function renderRuntime/);
  assert.ok(renderAgents, "renderAgents implementation exists");
  assert.match(renderAgents[0], /runtimeSnapshot\?\.agents\?\.items/);
  assert.match(renderAgents[0], /supporting_agents/);
  assert.match(renderAgents[0], /CONSENSUS-ONLY/);
  assert.doesNotMatch(renderAgents[0], /No agent identities recorded in consensus history/);
});

test("agent roster exposes runtime status and heartbeat age", async () => {
  const js = await readFile(new URL("dashboard.js", root), "utf8");
  const renderAgents = js.match(/function renderAgents\(\)[\s\S]*?\n  function renderRuntime/);
  assert.ok(renderAgents);
  assert.match(renderAgents[0], /runtime\?\.status/);
  assert.match(renderAgents[0], /age\(runtime\.last_heartbeat\)/);
  assert.match(js, /Last evaluation/);
});

test("provider probes and chain observations expose source-data age separately from page refresh", async () => {
  const js = await readFile(new URL("dashboard.js", root), "utf8");
  assert.match(js, /Probe "\+age\(item\.last_probe\)/);
  assert.match(js, /Last observation "\+age\(data\.latest_observed_at\)/);
  assert.match(js, /const age = \(value\) =>/);
});

test("consensus records distinguish missing attribution from missing agent identities", async () => {
  const js = await readFile(new URL("dashboard.js", root), "utf8");
  assert.match(js, /Agent attribution missing in this record/);
});


test("dashboard displays last bounded run independently from agent evaluation heartbeats", async () => {
  const html = await readFile(new URL("index.html", root), "utf8");
  const js = await readFile(new URL("dashboard.js", root), "utf8");
  assert.match(html, /id="worker-runtime"/);
  assert.match(html, /id="worker-runtime-summary"/);
  assert.match(js, /runtime\?\.workers\?\.items/);
  assert.match(js, /Agent evaluation heartbeats are tracked separately/);
  assert.match(js, /Last run "\+age\(item\.last_heartbeat\)/);
  assert.match(js, /item\.status==="SUCCESS"/);
  assert.match(js, /Workers "\+workerStatus/);
});
