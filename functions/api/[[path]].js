import { createClient } from "@libsql/client/web";

const REQUIRED_PAPER_TABLES = [
  "account",
  "positions",
  "trades",
  "paper_consensus_decisions",
];

const COUNT_TABLES = {
  candidates: "solana_launch_candidates",
  market_snapshots: "solana_market_snapshots",
  risk_reports: "solana_risk_evidence_reports",
  consensus_decisions: "solana_consensus_decisions",
  paper_decisions: "paper_consensus_decisions",
};

const ROUTES = new Set([
  "/api/health",
  "/api/overview",
  "/api/positions",
  "/api/trades",
  "/api/decisions",
  "/api/consensus",
  "/api/markets",
  "/api/equity-curve",
  "/api/costs",
]);

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store, max-age=0",
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "no-referrer",
      "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    },
  });
}

function equalSecret(left, right) {
  if (typeof left !== "string" || typeof right !== "string" || left.length !== right.length) {
    return false;
  }
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function finiteNumber(value, fallback = 0) {
  if (typeof value === "boolean" || value === null || value === undefined || value === "") {
    return fallback;
  }
  const result = Number(value);
  return Number.isFinite(result) ? result : fallback;
}

function isoSeconds(value) {
  const seconds = finiteNumber(value, -1);
  return seconds < 0 ? null : new Date(seconds * 1000).toISOString();
}

function isoMilliseconds(value) {
  const milliseconds = finiteNumber(value, -1);
  return milliseconds < 0 ? null : new Date(milliseconds).toISOString();
}

function rowObject(result, row) {
  if (!row || typeof row !== "object") return {};
  const output = {};
  const columns = Array.isArray(result.columns) ? result.columns : [];
  if (columns.length) {
    columns.forEach((column, index) => {
      const name = typeof column === "string" ? column : column?.name;
      if (name) {
        const value = row[name] !== undefined ? row[name] : row[index];
        output[name] = value;
      }
    });
    return output;
  }
  return { ...row };
}

async function rows(client, sql, args = []) {
  const result = await client.execute({ sql, args });
  return (result.rows || []).map((row) => rowObject(result, row));
}

async function first(client, sql, args = []) {
  const result = await client.execute({ sql, args });
  const row = result.rows?.[0];
  return row ? rowObject(result, row) : null;
}

async function tableNames(client) {
  const found = await rows(
    client,
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
  );
  return new Set(found.map((row) => String(row.name)));
}

function decodeQualityFlags(row) {
  if (!Object.prototype.hasOwnProperty.call(row, "quality_flags_json")) {
    return ["quality_flags_unavailable"];
  }
  try {
    const flags = JSON.parse(row.quality_flags_json);
    if (!Array.isArray(flags) || flags.some((flag) => typeof flag !== "string" || !flag.trim())) {
      return ["malformed_quality_flags"];
    }
    return [...new Set(flags.map((flag) => flag.trim()))].sort();
  } catch {
    return ["malformed_quality_flags"];
  }
}

async function latestMarket(client, mint, tables) {
  if (!tables.has("solana_market_snapshots")) return null;
  const row = await first(
    client,
    `SELECT * FROM solana_market_snapshots
     WHERE mint = ? ORDER BY retrieved_at_ms DESC, id DESC LIMIT 1`,
    [mint],
  );
  if (!row) return null;
  row.quality_flags = decodeQualityFlags(row);
  return row;
}

function positionMark(market, entryPrice, nowMs, maxMarketAgeMs) {
  if (!market) return { price: entryPrice, status: "MISSING", age: null, flags: [] };
  const flags = market.quality_flags;
  const observed = market.retrieved_at_ms;
  const age = Number.isSafeInteger(Number(observed)) && Number(observed) > 0
    ? nowMs - Number(observed) : null;
  if (!Array.isArray(flags) || flags.length) {
    return {
      price: entryPrice,
      status: "QUALITY_FLAGGED",
      age,
      flags: Array.isArray(flags) ? flags : ["malformed_quality_flags"],
    };
  }
  if (age === null) return { price: entryPrice, status: "INVALID_TIME", age: null, flags: [] };
  if (age < 0) return { price: entryPrice, status: "FUTURE", age, flags: [] };
  if (age > maxMarketAgeMs) return { price: entryPrice, status: "STALE", age, flags: [] };
  const price = finiteNumber(market.price_usd, 0);
  if (price <= 0) return { price: entryPrice, status: "INVALID_PRICE", age, flags: [] };
  return { price, status: "FRESH", age, flags: [] };
}

async function health(client, tables) {
  return {
    ok: true,
    database: "connected",
    database_read_only: true,
    paper_ledger_initialized: tables.has("account"),
    available_tables: [...tables].sort(),
    missing_paper_tables: REQUIRED_PAPER_TABLES.filter((table) => !tables.has(table)),
    live_execution: false,
    checked_at: new Date().toISOString(),
  };
}

async function overview(client, tables, maxMarketAgeMs) {
  const nowMs = Date.now();
  const account = tables.has("account")
    ? await first(client, "SELECT * FROM account WHERE id = 1 LIMIT 1") : null;
  const positions = tables.has("positions")
    ? await rows(client, "SELECT * FROM positions ORDER BY opened_at") : [];
  const sells = tables.has("trades")
    ? await rows(client, "SELECT realized_pnl, created_at FROM trades WHERE side='SELL' ORDER BY created_at")
    : [];
  const buys = tables.has("trades")
    ? await first(client, "SELECT COUNT(*) AS count FROM trades WHERE side='BUY'") : { count: 0 };
  const costs = tables.has("paper_execution_costs")
    ? await first(client, "SELECT COALESCE(SUM(fee_usd),0) AS fees, COALESCE(SUM(slippage_cost_usd),0) AS slippage FROM paper_execution_costs")
    : { fees: 0, slippage: 0 };

  const startingBalance = finiteNumber(account?.starting_balance);
  const cash = finiteNumber(account?.cash);
  let marketValue = 0;
  let unrealizedPnl = 0;
  let exposure = 0;
  let stalePositions = 0;
  let qualityFlaggedPositions = 0;
  for (const position of positions) {
    const market = await latestMarket(client, position.token, tables);
    const entry = finiteNumber(position.entry_price);
    const mark = positionMark(market, entry, nowMs, maxMarketAgeMs);
    if (mark.status !== "FRESH") stalePositions += 1;
    if (mark.status === "QUALITY_FLAGGED") qualityFlaggedPositions += 1;
    const value = finiteNumber(position.quantity) * mark.price;
    marketValue += value;
    unrealizedPnl += value - finiteNumber(position.cost_basis);
    exposure += finiteNumber(position.cost_basis);
  }

  const realizedPnl = sells.reduce((sum, row) => sum + finiteNumber(row.realized_pnl), 0);
  const wins = sells.filter((row) => finiteNumber(row.realized_pnl) > 0).length;
  const losses = sells.filter((row) => finiteNumber(row.realized_pnl) < 0).length;
  const closedFills = sells.length;
  const equity = cash + marketValue;
  const counts = {};
  for (const [key, table] of Object.entries(COUNT_TABLES)) {
    if (tables.has(table)) {
      const count = await first(client, `SELECT COUNT(*) AS count FROM ${table}`);
      counts[key] = Number(count?.count || 0);
    } else {
      counts[key] = 0;
    }
  }
  const latest = {};
  for (const [key, table, column] of [
    ["market", "solana_market_snapshots", "retrieved_at_ms"],
    ["risk", "solana_risk_evidence_reports", "observed_at_ms"],
    ["consensus", "solana_consensus_decisions", "created_at_ms"],
    ["paper", "paper_consensus_decisions", "processed_at_ms"],
  ]) {
    if (tables.has(table)) {
      const row = await first(client, `SELECT MAX(${column}) AS latest FROM ${table}`);
      latest[key] = isoMilliseconds(row?.latest);
    } else {
      latest[key] = null;
    }
  }

  return {
    account: {
      starting_balance_usd: startingBalance,
      cash_usd: cash,
      equity_usd: equity,
      realized_pnl_usd: realizedPnl,
      unrealized_pnl_usd: unrealizedPnl,
      total_return_pct: startingBalance ? (equity - startingBalance) / startingBalance * 100 : 0,
      open_positions: positions.length,
      portfolio_exposure_usd: exposure,
      portfolio_exposure_pct: startingBalance ? exposure / startingBalance * 100 : 0,
      stale_or_unmarked_positions: stalePositions,
      quality_flagged_positions: qualityFlaggedPositions,
    },
    performance: {
      exit_fills: closedFills,
      winning_exit_fills: wins,
      losing_exit_fills: losses,
      win_rate_pct: closedFills ? wins / closedFills * 100 : 0,
      realized_pnl_usd: realizedPnl,
      fees_usd: finiteNumber(costs?.fees),
      slippage_cost_usd: finiteNumber(costs?.slippage),
      buy_fills: Number(buys?.count || 0),
    },
    pipeline: counts,
    latest_updates: latest,
    read_only: true,
    live_execution: false,
  };
}

async function positions(client, tables, maxMarketAgeMs) {
  if (!tables.has("positions")) return [];
  const nowMs = Date.now();
  const stored = await rows(client, "SELECT * FROM positions ORDER BY opened_at");
  const output = [];
  for (const row of stored) {
    const market = await latestMarket(client, row.token, tables);
    const entry = finiteNumber(row.entry_price);
    const mark = positionMark(market, entry, nowMs, maxMarketAgeMs);
    const marketValue = finiteNumber(row.quantity) * mark.price;
    const basis = finiteNumber(row.cost_basis);
    const pnl = marketValue - basis;
    output.push({
      token: row.token,
      entry_price: entry,
      mark_price: mark.price,
      quantity: finiteNumber(row.quantity),
      cost_basis_usd: basis,
      market_value_usd: marketValue,
      unrealized_pnl_usd: pnl,
      unrealized_pnl_pct: basis ? pnl / basis * 100 : 0,
      opened_at: isoSeconds(row.opened_at),
      mark_status: mark.status,
      market_age_ms: mark.age,
      quality_flags: mark.flags,
      market_observed_at: isoMilliseconds(market?.retrieved_at_ms),
      symbol: market?.symbol ?? null,
      name: market?.name ?? null,
    });
  }
  return output;
}

async function trades(client, tables) {
  if (!tables.has("trades")) return [];
  const selected = tables.has("paper_execution_costs")
    ? await rows(client, `SELECT t.*, c.mark_price, c.fee_usd, c.slippage_cost_usd
       FROM trades t LEFT JOIN paper_execution_costs c ON c.trade_id=t.id
       ORDER BY t.created_at DESC LIMIT 100`)
    : await rows(client, `SELECT t.*, NULL AS mark_price, 0 AS fee_usd, 0 AS slippage_cost_usd
       FROM trades t ORDER BY t.created_at DESC LIMIT 100`);
  return selected.map((row) => ({
    ...row,
    created_at_iso: isoSeconds(row.created_at),
    price: finiteNumber(row.price),
    quantity: finiteNumber(row.quantity),
    notional: finiteNumber(row.notional),
    realized_pnl: finiteNumber(row.realized_pnl),
    fee_usd: finiteNumber(row.fee_usd),
    slippage_cost_usd: finiteNumber(row.slippage_cost_usd),
  }));
}

async function decisions(client, tables) {
  if (!tables.has("paper_consensus_decisions")) return [];
  const selected = await rows(client, `SELECT * FROM paper_consensus_decisions
    ORDER BY processed_at_ms DESC, decision_id DESC LIMIT 100`);
  return selected.map((row) => ({
    ...row,
    created_at_iso: isoMilliseconds(row.created_at_ms),
    processed_at_iso: isoMilliseconds(row.processed_at_ms),
  }));
}

async function consensus(client, tables) {
  if (!tables.has("solana_consensus_decisions")) return [];
  const selected = await rows(client, `SELECT mint, created_at_ms, decision_id, payload_json
    FROM solana_consensus_decisions
    ORDER BY created_at_ms DESC, id DESC LIMIT 50`);
  return selected.map((row) => {
    let payload = {};
    try {
      payload = JSON.parse(row.payload_json);
    } catch {
      payload = {};
    }
    return {
      mint: row.mint,
      decision_id: row.decision_id,
      created_at: isoMilliseconds(row.created_at_ms),
      status: payload.status ?? null,
      support_count: payload.support_count ?? null,
      supporting_agents: Array.isArray(payload.supporting_agents) ? payload.supporting_agents : [],
      paper_review_eligible: payload.paper_review_eligible === true,
      execution_authorized: payload.execution_authorized === true,
      reasons: Array.isArray(payload.reasons) ? payload.reasons : [],
    };
  });
}

async function markets(client, tables) {
  if (!tables.has("solana_market_snapshots")) return [];
  const selected = await rows(client, `SELECT m.* FROM solana_market_snapshots m
    JOIN (
      SELECT mint, MAX(id) AS latest_id FROM solana_market_snapshots GROUP BY mint
    ) latest ON latest.latest_id=m.id
    ORDER BY m.retrieved_at_ms DESC LIMIT 50`);
  return selected.map((row) => ({
    ...row,
    quality_flags: decodeQualityFlags(row),
    retrieved_at: isoMilliseconds(row.retrieved_at_ms),
  }));
}

async function equityCurve(client, tables) {
  const account = tables.has("account")
    ? await first(client, "SELECT starting_balance FROM account WHERE id=1") : null;
  const starting = finiteNumber(account?.starting_balance);
  if (!tables.has("trades")) {
    return {
      basis: "realized exit fills only",
      points: [{ time: null, equity_usd: starting, realized_pnl_usd: 0 }],
    };
  }
  const selected = await rows(client, `SELECT created_at, realized_pnl FROM trades
    WHERE side='SELL' ORDER BY created_at ASC LIMIT 250`);
  let cumulative = 0;
  const points = [{ time: null, equity_usd: starting, realized_pnl_usd: 0 }];
  for (const row of selected) {
    cumulative += finiteNumber(row.realized_pnl);
    points.push({
      time: isoSeconds(row.created_at),
      equity_usd: starting + cumulative,
      realized_pnl_usd: cumulative,
    });
  }
  return {
    basis: "realized exit fills only; excludes open-position mark-to-market",
    points,
  };
}

async function costs(client, tables) {
  if (!tables.has("paper_execution_costs")) {
    return { fees_usd: 0, slippage_cost_usd: 0, by_side: [] };
  }
  const totals = await first(client, `SELECT COALESCE(SUM(fee_usd),0) AS fees,
    COALESCE(SUM(slippage_cost_usd),0) AS slippage FROM paper_execution_costs`);
  const sides = await rows(client, `SELECT side, COALESCE(SUM(fee_usd),0) AS fees,
    COALESCE(SUM(slippage_cost_usd),0) AS slippage, COUNT(*) AS fills
    FROM paper_execution_costs GROUP BY side ORDER BY side`);
  return {
    fees_usd: finiteNumber(totals?.fees),
    slippage_cost_usd: finiteNumber(totals?.slippage),
    by_side: sides,
  };
}

function validateEnvironment(env) {
  const url = String(env.TURSO_DATABASE_URL || "").trim();
  const token = String(env.TURSO_AUTH_TOKEN || "").trim();
  const dashboardToken = String(env.DASHBOARD_API_TOKEN || "").trim();
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error("TURSO_DATABASE_URL is missing or invalid");
  }
  if (parsed.protocol !== "libsql:" || !parsed.hostname.endsWith(".turso.io") ||
      parsed.username || parsed.password || parsed.search || parsed.hash || !["", "/"].includes(parsed.pathname)) {
    throw new Error("TURSO_DATABASE_URL must be a credential-free libsql://*.turso.io endpoint");
  }
  if (!token) throw new Error("TURSO_AUTH_TOKEN is missing");
  if (dashboardToken.length < 32) throw new Error("DASHBOARD_API_TOKEN must contain at least 32 characters");
  return { url, token, dashboardToken };
}

export async function handleDashboardRequest(request, env, injectedClient = null) {
  if (request.method !== "GET") {
    return json({ error: "read-only dashboard; write methods are disabled" }, 405);
  }
  const path = new URL(request.url).pathname;
  if (!ROUTES.has(path)) return json({ error: "not found" }, 404);

  let config;
  try {
    config = validateEnvironment(env);
  } catch {
    return json({ error: "dashboard service is not configured" }, 503);
  }
  const authorization = request.headers.get("Authorization") || "";
  const supplied = authorization.startsWith("Bearer ") ? authorization.slice(7) : "";
  if (!equalSecret(supplied, config.dashboardToken)) {
    return json({ error: "unauthorized" }, 401);
  }

  let client = injectedClient;
  let ownsClient = false;
  try {
    if (!client) {
      client = createClient({ url: config.url, authToken: config.token });
      ownsClient = true;
    }
    const tables = await tableNames(client);
    const maxAge = Math.max(1, Math.min(
      900_000,
      Number.parseInt(String(env.VELTRIX_DASHBOARD_MARKET_MAX_AGE_MS || "120000"), 10) || 120_000,
    ));
    let payload;
    switch (path) {
      case "/api/health":
        payload = {
          ...(await health(client, tables)),
          database_read_only: true,
          live_execution: false,
        };
        break;
      case "/api/overview":
        payload = await overview(client, tables, maxAge);
        break;
      case "/api/positions":
        payload = await positions(client, tables, maxAge);
        break;
      case "/api/trades":
        payload = await trades(client, tables);
        break;
      case "/api/decisions":
        payload = await decisions(client, tables);
        break;
      case "/api/consensus":
        payload = await consensus(client, tables);
        break;
      case "/api/markets":
        payload = await markets(client, tables);
        break;
      case "/api/equity-curve":
        payload = await equityCurve(client, tables);
        break;
      case "/api/costs":
        payload = await costs(client, tables);
        break;
      default:
        return json({ error: "not found" }, 404);
    }
    return json(payload);
  } catch {
    return json({ error: "dashboard data unavailable" }, 503);
  } finally {
    if (ownsClient && client && typeof client.close === "function") {
      try { await client.close(); } catch { /* close failures do not expose internals */ }
    }
  }
}

export async function onRequest(context) {
  return handleDashboardRequest(context.request, context.env);
}
