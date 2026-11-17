import test from "node:test";
import assert from "node:assert/strict";
import { handleDashboardRequest } from "../functions/api/[[path]].js";

const TOKEN = "veltrix-chain-dashboard-test-token-012345";

function request(chain = "base", options = {}) {
  return new Request("https://veltrix.example/api/chain-markets?chain=" + encodeURIComponent(chain), {
    method: options.method || "GET",
    headers: options.auth === false ? {} : { Authorization: "Bearer " + TOKEN },
  });
}

function environment() {
  return {
    TURSO_DATABASE_URL: "libsql://veltrix-test.aws-eu-central-1.turso.io",
    TURSO_AUTH_TOKEN: "read-only-test-token",
    DASHBOARD_API_TOKEN: TOKEN,
  };
}

function clientWithEvents(events) {
  const calls = [];
  return {
    calls,
    async execute(statement) {
      calls.push(statement);
      if (statement.sql.includes("sqlite_master")) {
        return { columns: ["name"], rows: [{ name: "events" }] };
      }
      if (statement.sql.includes("FROM events")) {
        return { columns: ["payload", "created_at"], rows: events };
      }
      throw new Error("unexpected query");
    },
  };
}

test("chain market history requires authentication before database access", async () => {
  const client = clientWithEvents([]);
  const response = await handleDashboardRequest(request("base", { auth: false }), environment(), client);
  assert.equal(response.status, 401);
  assert.equal(client.calls.length, 0);
});

test("chain market history rejects unsupported chain selectors", async () => {
  const client = clientWithEvents([]);
  const response = await handleDashboardRequest(request("ethereum"), environment(), client);
  assert.equal(response.status, 400);
  assert.equal(client.calls.length, 0);
});

test("chain market history filters persisted ledger events by exact chain", async () => {
  const client = clientWithEvents([
    { payload: JSON.stringify({ chain: "base", token: "", price_usd: 3 }), created_at: 1760000004 },
    { payload: "{malformed", created_at: 1760000003 },
    { payload: JSON.stringify({
      chain: "base", token: "0xBaseToken", pair: "0xBasePair", symbol: "BASE",
      price_usd: "1.50", liquidity_usd: "27000", volume_h1_usd: "9000",
      price_change_m5_pct: "-2.5", observed_at_ms: 1760000002000, source: "dexscreener",
    }), created_at: 1760000002 },
    { payload: JSON.stringify({
      chain: "bsc", token: "0xBscToken", price_usd: 9, liquidity_usd: 1000,
      volume_h1_usd: 500, observed_at_ms: 1760000001000,
    }), created_at: 1760000001 },
    { payload: JSON.stringify({
      chain: "base", token: "0xBaseToken", pair: "0xBasePair", symbol: "BASE",
      price_usd: "1.25", liquidity_usd: "25000", volume_h1_usd: "8000",
      price_change_m5_pct: "-2.5", observed_at_ms: 1760000000000, source: "dexscreener",
    }), created_at: 1760000000 },
  ]);
  const response = await handleDashboardRequest(request("base"), environment(), client);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.chain, "base");
  assert.equal(body.available, true);
  assert.equal(body.status, "AVAILABLE");
  assert.equal(body.data_source, "events.MARKET_OBSERVATION");
  assert.equal(body.observation_count, 1);
  assert.equal(body.observations[0].token, "0xBaseToken");
  assert.equal(body.observations[0].price_usd, 1.5);
  assert.equal(body.observations[0].price_change_m5_pct, -2.5);
  assert.equal(body.observations[0].source, "dexscreener");
  assert.equal(body.latest_observed_at, "2025-10-09T08:53:22.000Z");
  assert.match(client.calls[1].sql, /^SELECT payload, created_at FROM events/);
  assert.deepEqual(client.calls[1].args, [1000]);
});

test("chain market history distinguishes a missing events table from an empty chain", async () => {
  const client = {
    calls: [],
    async execute(statement) {
      this.calls.push(statement);
      return { columns: ["name"], rows: [{ name: "account" }] };
    },
  };
  const response = await handleDashboardRequest(request("bsc"), environment(), client);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.available, false);
  assert.equal(body.observation_count, 0);
  assert.deepEqual(body.observations, []);
  assert.equal(client.calls.length, 1);
});

test("chain market history remains GET-only", async () => {
  const client = clientWithEvents([]);
  const response = await handleDashboardRequest(request("solana", { method: "POST" }), environment(), client);
  assert.equal(response.status, 405);
  assert.equal(client.calls.length, 0);
});

test("chain market history reports EMPTY distinctly and does not duplicate the same pool", async () => {
  const payload = JSON.stringify({
    chain: "bsc", token: "0xToken", pair: "0xPool", price_usd: 2,
    observed_at_ms: 1760000000000,
  });
  const client = clientWithEvents([
    { payload: payload.replace('"price_usd":2', '"price_usd":3'), created_at: 1760000001 },
    { payload, created_at: 1760000000 },
  ]);
  const response = await handleDashboardRequest(request("bsc"), environment(), client);
  const body = await response.json();
  assert.equal(body.status, "AVAILABLE");
  assert.equal(body.observation_count, 1);
  assert.equal(body.observations[0].price_usd, 3);
});
