(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const tokenKey = "veltrix_dashboard_api_token";
  const money = (value) => new Intl.NumberFormat(undefined, {
    style: "currency", currency: "USD", maximumFractionDigits: 2
  }).format(Number(value || 0));
  const number = (value, digits = 2) => Number(value || 0).toLocaleString(undefined, {
    maximumFractionDigits: digits
  });
  const date = (value) => value ? new Date(value).toLocaleString() : "—";
  const short = (value) => value && value.length > 18
    ? value.slice(0, 8) + "…" + value.slice(-7) : (value || "—");
  const signed = (value) => (Number(value) > 0 ? "+" : "") + money(value);
  let refreshing = false;
  const chainRequests = new Map();

  const pageMeta = {
    overview: ["System overview", "Shared ledger, portfolio state and recent intelligence activity."],
    solana: ["Solana intelligence", "Persisted Solana market observations and data quality."],
    base: ["Base intelligence", "Chain-specific dashboard integration status."],
    bsc: ["BSC intelligence", "BNB Smart Chain dashboard integration status."],
    history: ["Decision and ledger history", "Persisted consensus decisions, paper outcomes and fills."],
    diagnostics: ["System diagnostics", "Authenticated health and read-only safety checks."]
  };

  function setPage(page) {
    const selected = Object.hasOwn(pageMeta, page) ? page : "overview";
    for (const view of document.querySelectorAll("[data-page]")) {
      view.hidden = view.dataset.page !== selected;
    }
    for (const link of document.querySelectorAll("[data-route]")) {
      if (link.dataset.route === selected) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    $("page-title").textContent = pageMeta[selected][0];
    $("page-description").textContent = pageMeta[selected][1];
    if (location.hash !== "#" + selected) history.replaceState(null, "", "#" + selected);
    if (selected === "base" || selected === "bsc") loadChainMarkets(selected);
  }

  function renderDiagnostics(health, overview, runtime) {
    $("diag-database").textContent = health.database || "UNKNOWN";
    $("diag-readonly").textContent = health.database_read_only === true ? "READ-ONLY" :
      health.database_read_only === false ? "WRITE ACCESS REPORTED" : "UNKNOWN";
    $("diag-paper").textContent = health.paper_ledger_initialized === true ? "INITIALIZED" :
      health.paper_ledger_initialized === false ? "INCOMPLETE" : "UNKNOWN";
    $("diag-live").textContent = health.live_execution === false ? "DISABLED" :
      health.live_execution === true ? "ENABLED — INVESTIGATE" : "UNKNOWN";
    $("diag-missing").textContent = (health.missing_paper_tables || []).join(", ") || "None reported";
    const latest = overview.latest_updates || {};
    $("diag-last-market").textContent = date(latest.market);
    $("diag-last-risk").textContent = date(latest.risk);
    $("diag-last-consensus").textContent = date(latest.consensus);
    $("diag-last-paper").textContent = date(latest.paper);
    const pipeline = overview.pipeline || {};
    $("diag-pipeline").textContent = Object.entries(pipeline)
      .map(([key, value]) => key + ": " + number(value, 0)).join(" · ") || "No counts returned";
    const orchestration = runtime.orchestration || {};
    $("diag-worker-lease").textContent = orchestration.lease_status || "UNKNOWN";
    const job = orchestration.latest_job;
    $("diag-worker-job").textContent = job
      ? [job.job_type, job.status, "attempt " + number(job.attempt, 0), date(job.updated_at)].join(" · ")
      : "No persisted orchestration job";
    const agents = runtime.agents || {};
    const agentItems = Array.isArray(agents.items) ? agents.items : [];
    $("diag-agent-heartbeat").textContent = agentItems.length
      ? agentItems.map((item) => item.name + ": " + (item.status || "UNKNOWN") +
        (item.last_stance ? " (" + item.last_stance + ")" : "")).join(" · ")
      : (agents.status || "UNKNOWN");
    const providers = runtime.providers || {};
    const providerItems = Array.isArray(providers.items) ? providers.items : [];
    $("diag-provider-connectivity").textContent = providerItems.length
      ? providerItems.map((item) => item.provider + "/" + item.chain + ": " +
        (item.status || "UNKNOWN") + " · " + number(item.latency_ms, 0) + " ms").join(" · ")
      : (providers.status || "UNKNOWN");
    $("diag-runtime-checked").textContent = date(runtime.checked_at);
  }


  async function api(path) {
    const token = sessionStorage.getItem(tokenKey) || "";
    const headers = token ? { Authorization: "Bearer " + token } : {};
    const response = await fetch(path, { headers, cache: "no-store" });
    if (response.status === 401) {
      sessionStorage.removeItem(tokenKey);
      throw new Error("Dashboard token expired or was revoked. Enter a current token.");
    }
    if (!response.ok) throw new Error("Dashboard request failed (" + response.status + ").");
    return response.json();
  }

  function textCell(row, value, className) {
    const cell = document.createElement("td");
    cell.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
    if (className) cell.className = className;
    row.appendChild(cell);
    return cell;
  }

  function badgeCell(row, value, goodValues, badValues) {
    const cell = textCell(row, value);
    const badge = document.createElement("span");
    badge.className = "badge";
    if (goodValues.includes(value)) badge.classList.add("good");
    else if (badValues.includes(value)) badge.classList.add("bad");
    else badge.classList.add("warn");
    badge.textContent = value || "UNKNOWN";
    cell.replaceChildren(badge);
    return cell;
  }

  function renderRows(id, emptyId, rows, render, emptyText) {
    const body = $(id);
    body.replaceChildren();
    $(emptyId).hidden = rows.length > 0;
    if (!rows.length) {
      $(emptyId).textContent = emptyText;
      return;
    }
    for (const item of rows) {
      const row = body.insertRow();
      render(row, item);
    }
  }

  function renderMetrics(overview) {
    const a = overview.account || {};
    const p = overview.performance || {};
    const values = [
      ["equity", money(a.equity_usd)],
      ["cash", money(a.cash_usd)],
      ["realized", signed(a.realized_pnl_usd)],
      ["unrealized", signed(a.unrealized_pnl_usd)],
      ["open-count", number(a.open_positions, 0)],
      ["exposure", money(a.portfolio_exposure_usd)]
    ];
    for (const [id, value] of values) $(id).textContent = value;
    $("return").textContent = number(a.total_return_pct) + "% total return";
    $("closed-fills").textContent = number(p.exit_fills, 0) + " exit fills";
    $("stale-count").textContent = number(a.stale_or_unmarked_positions, 0) + " stale / unmarked";
    $("exposure-pct").textContent = number(a.portfolio_exposure_pct) + "% of starting balance";
    $("realized").className = Number(a.realized_pnl_usd) >= 0 ? "positive" : "negative";
    $("unrealized").className = Number(a.unrealized_pnl_usd) >= 0 ? "positive" : "negative";
    $("position-count").textContent = number(a.open_positions, 0);
    $("health-label").textContent = overview.read_only && !overview.live_execution
      ? "Shared ledger connected · read-only" : "Dashboard safety state requires review";
    $("health-detail").textContent = "Live execution: disabled · Pipeline records: " +
      Object.entries(overview.pipeline || {}).map(([key, value]) => key + " " + value).join(" · ");
  }

  function renderPositions(rows) {
    renderRows("positions-body", "positions-empty", rows, (row, item) => {
      textCell(row, (item.symbol ? item.symbol + " · " : "") + short(item.token));
      textCell(row, number(item.entry_price, 8));
      textCell(row, number(item.mark_price, 8));
      textCell(row, money(item.market_value_usd));
      textCell(row, signed(item.unrealized_pnl_usd), Number(item.unrealized_pnl_usd) >= 0 ? "positive" : "negative");
      badgeCell(row, item.mark_status, ["FRESH"], ["STALE", "MISSING"]);
    }, "No open paper positions.");
  }

  function renderTrades(rows) {
    renderRows("trades-body", "trades-empty", rows, (row, item) => {
      textCell(row, date(item.created_at_iso));
      textCell(row, short(item.token));
      badgeCell(row, item.side, ["BUY"], ["SELL"]);
      textCell(row, number(item.price, 8));
      textCell(row, money(item.notional));
      textCell(row, item.side === "SELL" ? signed(item.realized_pnl) : "—",
        Number(item.realized_pnl) >= 0 ? "positive" : "negative");
      textCell(row, money(item.fee_usd));
      textCell(row, money(item.slippage_cost_usd));
    }, "No paper fills recorded.");
  }

  function renderDecisions(rows) {
    renderRows("decisions-body", "decisions-empty", rows, (row, item) => {
      textCell(row, date(item.processed_at_iso));
      textCell(row, short(item.token));
      badgeCell(row, item.consensus_status, ["CONSENSUS_APPROVED"], ["REJECTED"]);
      badgeCell(row, item.execution_status, ["OPENED"], ["BLOCKED"]);
      textCell(row, item.notional_usd ? money(item.notional_usd) : "—");
      textCell(row, item.reason, "reason");
    }, "No paper decisions recorded.");
  }

  function renderConsensus(rows) {
    renderRows("consensus-body", "consensus-empty", rows, (row, item) => {
      textCell(row, date(item.created_at));
      textCell(row, short(item.mint));
      badgeCell(row, item.status, ["CONSENSUS_APPROVED"], ["REJECTED"]);
      textCell(row, number(item.support_count, 0) + " · " + (item.supporting_agents || []).join(", "));
      badgeCell(row, item.paper_review_eligible ? "ELIGIBLE" : "NOT ELIGIBLE",
        ["ELIGIBLE"], ["NOT ELIGIBLE"]);
      textCell(row, (item.reasons || []).join("; "), "reason");
    }, "No consensus decisions recorded.");
  }

  function renderMarkets(rows) {
    renderRows("markets-body", "markets-empty", rows, (row, item) => {
      textCell(row, (item.symbol ? item.symbol + " · " : "") + short(item.mint));
      textCell(row, number(item.price_usd, 10));
      textCell(row, money(item.selected_pool_liquidity_usd));
      textCell(row, money(item.selected_pool_volume_h1_usd));
      textCell(row, number(item.price_change_m5_pct) + "%",
        Number(item.price_change_m5_pct) >= 0 ? "positive" : "negative");
      badgeCell(row, (item.quality_flags || []).length ? "REVIEW" : "CLEAN",
        ["CLEAN"], ["REVIEW"]);
      textCell(row, date(item.retrieved_at));
    }, "No market snapshots available.");
  }


  function renderChainMarkets(chain, data) {
    const observations = Array.isArray(data.observations) ? data.observations : [];
    const status = $(chain + "-data-status");
    if (!data.available) {
      status.textContent = "Generic events ledger is not available in this database.";
      renderRows(chain + "-markets-body", chain + "-markets-empty", [],
        () => {}, "The persisted events table is not initialized.");
      return;
    }
    status.textContent = "Read-only events ledger available · " +
      number(data.observation_count, 0) + " persisted " + chain.toUpperCase() + " observations";
    renderRows(chain + "-markets-body", chain + "-markets-empty", observations, (row, item) => {
      textCell(row, (item.symbol ? item.symbol + " · " : "") + short(item.token));
      textCell(row, number(item.price_usd, 10));
      textCell(row, money(item.liquidity_usd));
      textCell(row, money(item.volume_h1_usd));
      textCell(row, item.price_change_m5_pct === null ? "—" :
        number(item.price_change_m5_pct) + "%",
        Number(item.price_change_m5_pct) >= 0 ? "positive" : "negative");
      textCell(row, item.source);
      textCell(row, date(item.observed_at));
    }, "No persisted " + chain.toUpperCase() + " market observations have been recorded.");
  }

  async function loadChainMarkets(chain) {
    if (chainRequests.has(chain)) return chainRequests.get(chain);
    const task = (async () => {
      const status = $(chain + "-data-status");
      status.textContent = "Loading persisted " + chain.toUpperCase() + " observations...";
      try {
        const data = await api("/api/chain-markets?chain=" + encodeURIComponent(chain));
        renderChainMarkets(chain, data);
      } catch (error) {
        status.textContent = "Chain market history query failed.";
        renderRows(chain + "-markets-body", chain + "-markets-empty", [],
          () => {}, error.message);
      }
    })();
    chainRequests.set(chain, task);
    try { await task; } finally { chainRequests.delete(chain); }
  }

  function renderEquityCurve(data) {
    const svg = $("equity-chart");
    svg.replaceChildren();
    const points = data.points || [];
    if (points.length < 2) {
      $("chart-empty").hidden = false;
      return;
    }
    $("chart-empty").hidden = true;
    const ns = "http://www.w3.org/2000/svg";
    const values = points.map((point) => Number(point.equity_usd || 0));
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = Math.max(1, max - min);
    const x0 = 42, x1 = 884, y0 = 16, y1 = 204;
    for (let i = 0; i < 4; i++) {
      const y = y0 + (y1 - y0) * i / 3;
      const line = document.createElementNS(ns, "line");
      line.setAttribute("x1", x0); line.setAttribute("x2", x1);
      line.setAttribute("y1", y); line.setAttribute("y2", y);
      line.setAttribute("class", "chart-grid"); svg.appendChild(line);
    }
    const coords = values.map((value, index) => {
      const x = x0 + (x1 - x0) * index / (values.length - 1);
      const y = y1 - ((value - min) / range) * (y1 - y0);
      return [x, y];
    });
    const area = document.createElementNS(ns, "polygon");
    area.setAttribute("points", x0 + "," + y1 + " " +
      coords.map((point) => point.join(",")).join(" ") + " " + x1 + "," + y1);
    area.setAttribute("class", "chart-area"); svg.appendChild(area);
    const line = document.createElementNS(ns, "polyline");
    line.setAttribute("points", coords.map((point) => point.join(",")).join(" "));
    line.setAttribute("class", "chart-line"); svg.appendChild(line);
    const last = coords[coords.length - 1];
    const dot = document.createElementNS(ns, "circle");
    dot.setAttribute("cx", last[0]); dot.setAttribute("cy", last[1]);
    dot.setAttribute("r", "5"); dot.setAttribute("class", "chart-point"); svg.appendChild(dot);
    const top = document.createElementNS(ns, "text");
    top.setAttribute("x", x0); top.setAttribute("y", 12); top.setAttribute("class", "chart-axis");
    top.textContent = money(max); svg.appendChild(top);
    const bottom = document.createElementNS(ns, "text");
    bottom.setAttribute("x", x0); bottom.setAttribute("y", 222); bottom.setAttribute("class", "chart-axis");
    bottom.textContent = money(min); svg.appendChild(bottom);
  }

  function renderCosts(costs) {
    $("fees").textContent = money(costs.fees_usd);
    $("slippage").textContent = money(costs.slippage_cost_usd);
    $("total-costs").textContent = money(Number(costs.fees_usd || 0) + Number(costs.slippage_cost_usd || 0));
    const bySide = Object.fromEntries((costs.by_side || []).map((row) => [row.side, row]));
    const entry = bySide.BUY || { fees: 0, slippage: 0 };
    const exit = bySide.SELL || { fees: 0, slippage: 0 };
    const max = Math.max(0.01, Number(entry.fees) + Number(entry.slippage),
      Number(exit.fees) + Number(exit.slippage));
    $("entry-cost").textContent = money(Number(entry.fees) + Number(entry.slippage));
    $("exit-cost").textContent = money(Number(exit.fees) + Number(exit.slippage));
    $("entry-cost-bar").style.setProperty("--fill",
      ((Number(entry.fees) + Number(entry.slippage)) / max * 100) + "%");
    $("exit-cost-bar").style.setProperty("--fill",
      ((Number(exit.fees) + Number(exit.slippage)) / max * 100) + "%");
  }

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const results = await Promise.allSettled([
        api("/api/health"), api("/api/overview"), api("/api/positions"),
        api("/api/trades"), api("/api/decisions"), api("/api/consensus"),
        api("/api/markets"), api("/api/equity-curve"), api("/api/costs"),
        api("/api/runtime-status")
      ]);
      const names = ["health", "overview", "positions", "trades", "decisions",
        "consensus", "markets", "equity curve", "costs", "runtime status"];
      const errors = [];
      const failures = [];
      const value = (index) => {
        const result = results[index];
        if (result.status === "fulfilled") return result.value;
        failures[index] = result.reason?.message || "request failed";
        errors.push(names[index] + ": " + failures[index]);
        return null;
      };
      const health = value(0);
      const overview = value(1);
      const positions = value(2);
      const trades = value(3);
      const decisions = value(4);
      const consensus = value(5);
      const markets = value(6);
      const curve = value(7);
      const costs = value(8);
      const runtime = value(9);

      if (overview) renderMetrics(overview);
      else {
        ["equity", "cash", "realized", "unrealized", "open-count", "exposure",
          "position-count"].forEach((id) => $(id).textContent = "—");
        $("return").textContent = "Overview unavailable";
        $("closed-fills").textContent = "Unavailable";
        $("stale-count").textContent = "Unavailable";
        $("exposure-pct").textContent = "Unavailable";
      }
      if (positions) renderPositions(positions);
      else renderRows("positions-body", "positions-empty", [], () => {}, "Positions unavailable: " + failures[2]);
      if (trades) renderTrades(trades);
      else renderRows("trades-body", "trades-empty", [], () => {}, "Trade history unavailable: " + failures[3]);
      if (decisions) renderDecisions(decisions);
      else renderRows("decisions-body", "decisions-empty", [], () => {}, "Decision history unavailable: " + failures[4]);
      if (consensus) renderConsensus(consensus);
      else renderRows("consensus-body", "consensus-empty", [], () => {}, "Consensus history unavailable: " + failures[5]);
      if (markets) {
        renderMarkets(markets);
        $("market-count").textContent = number(markets.length, 0);
      } else {
        renderRows("markets-body", "markets-empty", [], () => {}, "Market snapshots unavailable: " + failures[6]);
        $("market-count").textContent = "—";
      }
      if (curve) renderEquityCurve(curve);
      else {
        renderEquityCurve({ points: [] });
        $("chart-empty").textContent = "Equity curve unavailable: " + failures[7];
      }
      if (costs) renderCosts(costs);
      else {
        ["fees", "slippage", "total-costs", "entry-cost", "exit-cost"]
          .forEach((id) => $(id).textContent = "Unavailable");
        $("entry-cost-bar").style.setProperty("--fill", "0%");
        $("exit-cost-bar").style.setProperty("--fill", "0%");
      }

      renderDiagnostics(health || {}, overview || {}, runtime || {});
      if (health) {
        $("health-led").className = health.database === "connected"
          ? "status-led" : "status-led bad";
        $("health-label").textContent = health.database === "connected"
          ? (errors.length ? "Database connected · partial dashboard data" : "Database connected · read-only")
          : "Database health requires review";
        $("health-detail").textContent = "Live execution: " +
          (health.live_execution === false ? "disabled" : "state unverified") +
          " · Missing paper tables: " + ((health.missing_paper_tables || []).join(", ") || "none") +
          (errors.length ? " · Section errors: " + errors.length : "");
      } else {
        $("health-led").className = "status-led bad";
        $("health-label").textContent = "Health check unavailable";
        $("health-detail").textContent = errors[0] || "Health endpoint failed.";
      }
      $("last-updated").textContent = errors.length
        ? "Partial update " + new Date().toLocaleTimeString()
        : "Updated " + new Date().toLocaleTimeString();
    } finally {
      refreshing = false;
    }
  }

  for (const link of document.querySelectorAll("[data-route]")) {
    link.addEventListener("click", () => setPage(link.dataset.route));
  }
  window.addEventListener("hashchange", () => setPage(location.hash.slice(1)));
  setPage(location.hash.slice(1) || "overview");

  $("refresh").addEventListener("click", () => {
    refresh();
    const selected = location.hash.slice(1);
    if (selected === "base" || selected === "bsc") loadChainMarkets(selected);
  });
  $("save-token").addEventListener("click", () => {
    const value = $("api-token").value.trim();
    if (value) sessionStorage.setItem(tokenKey, value);
    else sessionStorage.removeItem(tokenKey);
    refresh();
  });
  $("clear-token").addEventListener("click", () => {
    sessionStorage.removeItem(tokenKey);
    $("api-token").value = "";
    refresh();
  });
  $("api-token").value = sessionStorage.getItem(tokenKey) || "";
  refresh();
  window.setInterval(refresh, 15_000);
})();
