import test from "node:test";
import assert from "node:assert/strict";
import { handleDashboardRequest } from "../functions/api/[[path]].js";

const TOKEN = "veltrix-test-dashboard-token-0123456789";

function env(overrides = {}) {
  return {
    TURSO_DATABASE_URL: "libsql://veltrix-test.aws-eu-central-1.turso.io",
    TURSO_AUTH_TOKEN: "test-read-only-token",
    DASHBOARD_API_TOKEN: TOKEN,
    ...overrides,
  };
}

function fakeClient(tables = ["account", "positions", "trades", "paper_consensus_decisions"]) {
  const calls = [];
  return {
    calls,
    async execute(statement) {
      calls.push(statement);
      if (statement.sql.includes("sqlite_master")) {
        return { columns: ["name"], rows: tables.map((name) => ({ name })) };
      }
      throw new Error("unexpected query in security boundary test");
    },
  };
}

test("dashboard API fails closed when production bindings are absent", async () => {
  const response = await handleDashboardRequest(
    new Request("https://veltrix.example/api/health"),
    {},
  );
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: "dashboard service is not configured" });
});

test("dashboard API rejects malformed database URLs before connecting", async () => {
  const response = await handleDashboardRequest(
    new Request("https://veltrix.example/api/health", {
      headers: { Authorization: `Bearer ${TOKEN}` },
    }),
    env({ TURSO_DATABASE_URL: "https://example.com/not-turso" }),
  );
  assert.equal(response.status, 503);
});

test("dashboard API requires a bearer token and does not query the database", async () => {
  const client = fakeClient();
  const response = await handleDashboardRequest(
    new Request("https://veltrix.example/api/health"),
    env(),
    client,
  );
  assert.equal(response.status, 401);
  assert.deepEqual(await response.json(), { error: "unauthorized" });
  assert.equal(client.calls.length, 0);
});

test("dashboard API rejects short configured access tokens", async () => {
  const response = await handleDashboardRequest(
    new Request("https://veltrix.example/api/health"),
    env({ DASHBOARD_API_TOKEN: "short" }),
  );
  assert.equal(response.status, 503);
});

test("dashboard API allows only GET methods", async () => {
  const response = await handleDashboardRequest(
    new Request("https://veltrix.example/api/overview", {
      method: "POST",
      headers: { Authorization: `Bearer ${TOKEN}` },
    }),
    env(),
  );
  assert.equal(response.status, 405);
  assert.match((await response.json()).error, /read-only/);
});

test("dashboard API returns 404 for unknown routes without database access", async () => {
  const client = fakeClient();
  const response = await handleDashboardRequest(
    new Request("https://veltrix.example/api/admin"),
    env(),
    client,
  );
  assert.equal(response.status, 404);
  assert.equal(client.calls.length, 0);
});

test("health endpoint reports the shared database as read-only", async () => {
  const client = fakeClient();
  const response = await handleDashboardRequest(
    new Request("https://veltrix.example/api/health", {
      headers: { Authorization: `Bearer ${TOKEN}` },
    }),
    env(),
    client,
  );
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.database, "connected");
  assert.equal(body.database_read_only, true);
  assert.equal(body.live_execution, false);
  assert.deepEqual(body.missing_paper_tables, []);
  assert.equal(client.calls.length, 1);
  assert.match(client.calls[0].sql, /^SELECT name FROM sqlite_master/);
});

test("health endpoint identifies incomplete paper schemas without failing open", async () => {
  const client = fakeClient(["solana_market_snapshots"]);
  const response = await handleDashboardRequest(
    new Request("https://veltrix.example/api/health", {
      headers: { Authorization: `Bearer ${TOKEN}` },
    }),
    env(),
    client,
  );
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.paper_ledger_initialized, false);
  assert.deepEqual(body.missing_paper_tables, [
    "account", "positions", "trades", "paper_consensus_decisions",
  ]);
});

test("API responses never include database credentials", async () => {
  const client = fakeClient();
  const response = await handleDashboardRequest(
    new Request("https://veltrix.example/api/health", {
      headers: { Authorization: `Bearer ${TOKEN}` },
    }),
    env(),
    client,
  );
  const body = await response.text();
  assert.equal(body.includes("test-read-only-token"), false);
  assert.equal(body.includes("veltrix-test.aws-eu-central-1.turso.io"), false);
});

test("markets endpoint preserves the existing dashboard field contract", async () => {
  const client = {
    async execute(statement) {
      if (statement.sql.includes("sqlite_master")) {
        return {
          columns: ["name"],
          rows: [{ name: "solana_market_snapshots" }],
        };
      }
      if (statement.sql.includes("SELECT m.* FROM solana_market_snapshots")) {
        return {
          columns: [
            "id", "mint", "symbol", "price_usd", "liquidity_usd",
            "volume_h1_usd", "retrieved_at_ms", "quality_flags_json",
          ],
          rows: [{
            id: 7,
            mint: "So11111111111111111111111111111111111111112",
            symbol: "SOL",
            price_usd: "145.5",
            liquidity_usd: "25000",
            volume_h1_usd: "8500",
            retrieved_at_ms: 1760000000000,
            quality_flags_json: "[]",
          }],
        };
      }
      throw new Error("unexpected SQL in market response contract test");
    },
  };
  const response = await handleDashboardRequest(
    new Request("https://veltrix.example/api/markets", {
      headers: { Authorization: `Bearer ${TOKEN}` },
    }),
    env(),
    client,
  );
  assert.equal(response.status, 200);
  const [market] = await response.json();
  assert.equal(market.selected_pool_liquidity_usd, 25000);
  assert.equal(market.selected_pool_volume_h1_usd, 8500);
  assert.deepEqual(market.quality_flags, []);
  assert.ok(market.retrieved_at);
});
