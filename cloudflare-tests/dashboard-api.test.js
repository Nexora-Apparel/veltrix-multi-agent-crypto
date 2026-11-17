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

test("runtime status exposes worker lease evidence but never fabricates agent or provider health", async () => {
  const client = {
    async execute(statement) {
      if (statement.sql.includes("sqlite_master")) {
        return { columns: ["name"], rows: [
          { name: "orchestration_worker_lease" }, { name: "orchestration_jobs" },
        ] };
      }
      if (statement.sql.includes("FROM orchestration_worker_lease")) {
        return { columns: ["lease_name", "fencing_token", "acquired_at_ms", "expires_at_ms"], rows: [] };
      }
      if (statement.sql.includes("FROM orchestration_jobs")) {
        return { columns: ["job_type", "status", "attempt", "updated_at_ms", "started_at_ms", "finished_at_ms", "error_code"], rows: [{
          job_type: "solana_pipeline", status: "SUCCEEDED", attempt: 1,
          updated_at_ms: 1760000000000, started_at_ms: 1759999990000,
          finished_at_ms: 1760000000000, error_code: null,
        }] };
      }
      throw new Error("unexpected SQL in runtime status test");
    },
  };
  const response = await handleDashboardRequest(
    new Request("https://veltrix.example/api/runtime-status", {
      headers: { Authorization: `Bearer ${TOKEN}` },
    }), env(), client,
  );
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.orchestration.lease_status, "UNKNOWN");
  assert.equal(body.orchestration.latest_job.status, "SUCCEEDED");
  assert.equal(body.agents.status, "UNAVAILABLE");
  assert.equal(body.agents.heartbeat_source_available, false);
  assert.equal(body.agents.items.length, 4);
  assert.equal(body.providers.status, "UNAVAILABLE");
  assert.equal(body.providers.connectivity_probe_available, false);
  assert.equal(body.read_only, true);
  assert.equal(body.live_execution, false);
});

test("runtime status reports missing orchestration tables as unavailable without writes", async () => {
  const client = fakeClient();
  const response = await handleDashboardRequest(
    new Request("https://veltrix.example/api/runtime-status", {
      headers: { Authorization: `Bearer ${TOKEN}` },
    }), env(), client,
  );
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.orchestration.lease_status, "UNAVAILABLE");
  assert.equal(body.orchestration.jobs_table_available, false);
  assert.equal(body.agents.status, "UNAVAILABLE");
  assert.equal(body.providers.status, "UNAVAILABLE");
  assert.equal(client.calls.length, 1);
});

test("runtime status derives agent and provider freshness from persisted evidence", async () => {
  const now = Date.now();
  const events = [
    { id: 5, event: "AGENT_HEARTBEAT", payload: JSON.stringify({
      agent: "scout", status: "SUCCESS", stance: "SUPPORT", observed_at_ms: now - 1000,
    }) },
    { id: 4, event: "AGENT_HEARTBEAT", payload: JSON.stringify({
      agent: "whale", status: "FAILED", stance: null, error_code: "ProviderError", observed_at_ms: now - 2000,
    }) },
    { id: 3, event: "PROVIDER_PROBE", payload: JSON.stringify({
      provider: "dexscreener", chain: "base", status: "SUCCESS", latency_ms: 125, observed_at_ms: now - 500,
    }) },
  ];
  const client = {
    async execute(statement) {
      if (statement.sql.includes("sqlite_master")) {
        return { columns: ["name"], rows: [
          { name: "events" }, { name: "orchestration_worker_lease" }, { name: "orchestration_jobs" },
        ] };
      }
      if (statement.sql.includes("FROM orchestration_worker_lease")) return { columns: [], rows: [] };
      if (statement.sql.includes("FROM orchestration_jobs")) return { columns: [], rows: [] };
      if (statement.sql.includes("FROM events")) return { columns: ["id", "event", "payload"], rows: events };
      throw new Error("unexpected runtime telemetry query");
    },
  };
  const response = await handleDashboardRequest(
    new Request("https://veltrix.example/api/runtime-status", {
      headers: { Authorization: `Bearer ${TOKEN}` },
    }), env(), client,
  );
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.agents.items.find((item) => item.name === "scout").status, "FRESH");
  assert.equal(body.agents.items.find((item) => item.name === "scout").last_stance, "SUPPORT");
  assert.equal(body.agents.items.find((item) => item.name === "whale").status, "FAILED");
  assert.equal(body.agents.items.find((item) => item.name === "sniper").status, "UNKNOWN");
  assert.equal(body.providers.items[0].status, "FRESH");
  assert.equal(body.providers.items[0].provider, "dexscreener");
  assert.equal(body.providers.items[0].chain, "base");
  assert.equal(body.providers.items[0].latency_ms, 125);
});

test("runtime status distinguishes unavailable telemetry schema from no telemetry yet", async () => {
  const client = fakeClient();
  const response = await handleDashboardRequest(
    new Request("https://veltrix.example/api/runtime-status", {
      headers: { Authorization: `Bearer ${TOKEN}` },
    }), env(), client,
  );
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.agents.status, "UNAVAILABLE");
  assert.equal(body.providers.status, "UNAVAILABLE");
});
