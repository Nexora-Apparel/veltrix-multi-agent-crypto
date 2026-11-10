import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../cloudflare/worker.js", import.meta.url), "utf8");
const { default: worker } = await import(
  "data:text/javascript;base64," + Buffer.from(source).toString("base64")
);
const TOKEN = "a".repeat(48);
const ORIGIN = "https://api.veltrix.example";

function request(path = "/api/health", method = "GET") {
  return new Request("https://dashboard.veltrix.example" + path, { method });
}
function env(overrides = {}) {
  return {
    VELTRIX_API_ORIGIN: ORIGIN,
    VELTRIX_DASHBOARD_TOKEN: TOKEN,
    CF_ACCESS_CLIENT_ID: "veltrix-origin-token.access",
    CF_ACCESS_CLIENT_SECRET: "cloudflare-access-secret",
    ASSETS: { fetch: async () => new Response("static dashboard") },
    ...overrides,
  };
}
async function withFetch(mock, run) {
  const original = globalThis.fetch;
  globalThis.fetch = mock;
  try { return await run(); }
  finally { globalThis.fetch = original; }
}

test("serves static dashboard with security headers", async () => {
  const response = await worker.fetch(request("/"), env());
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "static dashboard");
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.equal(response.headers.get("x-frame-options"), "DENY");
  assert.match(response.headers.get("content-security-policy"), /default-src 'self'/);
  assert.equal(response.headers.get("cache-control"), "no-store");
});

test("proxies an allowlisted GET with server-side bearer token", async () => {
  let captured;
  const response = await withFetch(async (url, options) => {
    captured = { url: String(url), options };
    return Response.json({ candles: [], synthetic: false });
  }, () => worker.fetch(request("/api/candles?mint=abc&interval=1m"), env()));
  assert.equal(response.status, 200);
  assert.equal(captured.url, ORIGIN + "/api/candles?mint=abc&interval=1m");
  assert.equal(captured.options.method, "GET");
  assert.equal(captured.options.headers.get("authorization"), "Bearer " + TOKEN);
  assert.equal(captured.options.headers.get("cf-access-client-id"), "veltrix-origin-token.access");
  assert.equal(captured.options.headers.get("cf-access-client-secret"), "cloudflare-access-secret");
  assert.equal(captured.options.redirect, "manual");
  assert.equal(response.headers.get("cache-control"), "no-store");
});

test("rejects non-GET methods before upstream fetch", async () => {
  let called = false;
  const response = await withFetch(async () => {
    called = true;
    return new Response("unexpected");
  }, () => worker.fetch(request("/api/health", "POST"), env()));
  assert.equal(response.status, 405);
  assert.equal(called, false);
});

test("rejects unlisted API paths", async () => {
  let called = false;
  const response = await withFetch(async () => {
    called = true;
    return new Response("unexpected");
  }, () => worker.fetch(request("/api/admin/login"), env()));
  assert.equal(response.status, 404);
  assert.equal(called, false);
});

test("fails closed when proxy secrets are absent or weak", async () => {
  assert.equal((await worker.fetch(request(), env({ VELTRIX_API_ORIGIN: "" }))).status, 503);
  assert.equal((await worker.fetch(request(), env({ VELTRIX_DASHBOARD_TOKEN: "short" }))).status, 503);
  assert.equal((await worker.fetch(request(), env({ VELTRIX_DASHBOARD_TOKEN: TOKEN + " " }))).status, 503);
  assert.equal((await worker.fetch(request(), env({ CF_ACCESS_CLIENT_ID: "" }))).status, 503);
  assert.equal((await worker.fetch(request(), env({ CF_ACCESS_CLIENT_SECRET: "" }))).status, 503);
});

test("rejects API origins that could redirect or leak credentials", async () => {
  for (const invalid of [
    "http://api.veltrix.example",
    "https://user:pass@api.veltrix.example",
    "https://api.veltrix.example/private",
    "https://api.veltrix.example?token=secret",
    "https://api.veltrix.example#fragment",
  ]) {
    const response = await worker.fetch(request(), env({ VELTRIX_API_ORIGIN: invalid }));
    assert.equal(response.status, 503, invalid);
  }
});

test("rejects upstream redirects and never follows them", async () => {
  let options;
  const response = await withFetch(async (_url, opts) => {
    options = opts;
    return new Response(null, { status: 302, headers: { location: "https://evil.example" } });
  }, () => worker.fetch(request(), env()));
  assert.equal(options.redirect, "manual");
  assert.equal(response.status, 502);
  assert.equal(response.headers.get("location"), null);
});

test("does not forward upstream Set-Cookie headers", async () => {
  const response = await withFetch(async () => new Response("ok", {
    headers: { "set-cookie": "session=private; HttpOnly" },
  }), () => worker.fetch(request(), env()));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("set-cookie"), null);
});

test("returns generic 502 when origin request fails", async () => {
  const response = await withFetch(async () => {
    throw new Error("private upstream details");
  }, () => worker.fetch(request(), env()));
  assert.equal(response.status, 502);
  assert.deepEqual(await response.json(), { error: "API origin request failed" });
});
