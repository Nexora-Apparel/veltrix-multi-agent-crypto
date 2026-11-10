import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../cloudflare/worker.js", import.meta.url), "utf8");
const { default: worker } = await import(
  "data:text/javascript;base64," + Buffer.from(source).toString("base64")
);
const ACCESS = {
  CF_ACCESS_CLIENT_ID: "veltrix-origin-token.access",
  CF_ACCESS_CLIENT_SECRET: "cloudflare-access-secret"
};

test("static assets are served with no-store cache policy", async () => {
  const response = await worker.fetch(new Request("https://terminal.example/"), {
    ASSETS: { fetch: async () => new Response("dashboard", { headers: { "cache-control": "public" } }) }
  });
  assert.equal(await response.text(), "dashboard");
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(response.headers.get("x-frame-options"), "DENY");
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.match(response.headers.get("content-security-policy"), /frame-ancestors 'none'/);
});

test("only GET requests and allowlisted API paths are accepted", async () => {
  const env = { VELTRIX_API_ORIGIN: "https://api.example", VELTRIX_DASHBOARD_TOKEN: "x".repeat(32) };
  const post = await worker.fetch(new Request("https://terminal.example/api/health", { method: "POST" }), env);
  assert.equal(post.status, 405);
  const unknown = await worker.fetch(new Request("https://terminal.example/api/admin/delete"), env);
  assert.equal(unknown.status, 404);
});

test("missing bindings and non-HTTPS origins fail closed", async () => {
  const missing = await worker.fetch(new Request("https://terminal.example/api/health"), {});
  assert.equal(missing.status, 503);
  const insecure = await worker.fetch(new Request("https://terminal.example/api/health"), {
    VELTRIX_API_ORIGIN: "http://api.example",
    VELTRIX_DASHBOARD_TOKEN: "x".repeat(32)
  });
  assert.equal(insecure.status, 503);
});

test("allowlisted requests receive server-side auth and never follow redirects", async () => {
  const originalFetch = globalThis.fetch;
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url: String(url), init };
    return new Response("ok", { status: 200, headers: { "set-cookie": "secret=1" } });
  };
  try {
    const response = await worker.fetch(
      new Request("https://terminal.example/api/sol-paper-wallet?view=summary"),
      {
        ...ACCESS,
        VELTRIX_API_ORIGIN: "https://api.example",
        VELTRIX_DASHBOARD_TOKEN: "x".repeat(32)
      }
    );
    assert.equal(response.status, 200);
    assert.equal(captured.url, "https://api.example/api/sol-paper-wallet?view=summary");
    assert.equal(captured.init.headers.get("authorization"), "Bearer " + "x".repeat(32));
    assert.equal(captured.init.headers.get("cf-access-client-id"), ACCESS.CF_ACCESS_CLIENT_ID);
    assert.equal(captured.init.headers.get("cf-access-client-secret"), ACCESS.CF_ACCESS_CLIENT_SECRET);
    assert.equal(captured.init.redirect, "manual");
    assert.equal(response.headers.get("set-cookie"), null);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("upstream redirects are rejected", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(null, { status: 302, headers: { location: "https://evil.example" } });
  try {
    const response = await worker.fetch(new Request("https://terminal.example/api/health"), {
      VELTRIX_API_ORIGIN: "https://api.example",
      VELTRIX_DASHBOARD_TOKEN: "x".repeat(32)
    });
    assert.equal(response.status, 502);
  } finally {
    globalThis.fetch = originalFetch;
  }
});


test("API origin rejects embedded paths and credentials", async () => {
  for (const origin of [
    "https://api.example/internal",
    "https://user:pass@api.example"
  ]) {
    const response = await worker.fetch(new Request("https://terminal.example/api/health"), {
      ...ACCESS,
      VELTRIX_API_ORIGIN: origin,
      VELTRIX_DASHBOARD_TOKEN: "x".repeat(32)
    });
    assert.equal(response.status, 503);
  }
});

test("upstream transport failures return a bounded 502 response", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error("network detail must not leak"); };
  try {
    const response = await worker.fetch(new Request("https://terminal.example/api/health"), {
      VELTRIX_API_ORIGIN: "https://api.example",
      VELTRIX_DASHBOARD_TOKEN: "x".repeat(32)
    });
    assert.equal(response.status, 502);
    assert.deepEqual(await response.json(), { error: "API origin request failed" });
  } finally {
    globalThis.fetch = originalFetch;
  }
});


test("short dashboard bearer tokens fail closed", async () => {
  const response = await worker.fetch(new Request("https://terminal.example/api/health"), {
    VELTRIX_API_ORIGIN: "https://api.example",
    VELTRIX_DASHBOARD_TOKEN: "too-short"
  });
  assert.equal(response.status, 503);
});
