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

  async function api(path) {
    const token = sessionStorage.getItem(tokenKey) || "";
    const headers = token ? { Authorization: "Bearer " + token } : {};
    const response = await fetch(path, { headers, cache: "no-store" });
    if (response.status === 401) throw new Error("Dashboard access token required.");
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

  function renderSolWallet(data) {
    const status = $("sol-wallet-status");
    if (!data || data.configured !== true) {
      status.textContent = "NOT INITIALIZED";
      status.className = "count-pill";
      $("sol-starting").textContent = "—";
      $("sol-cash").textContent = "—";
      $("sol-realized").textContent = "—";
      $("sol-open").textContent = "—";
      $("sol-fills").textContent = "— fills";
      $("sol-equity").textContent = "No SOL marks";
      $("sol-wallet-note").textContent = data && data.reason
        ? data.reason + " · read-only panel"
        : "Native SOL ledger is not initialized.";
      return;
    }
    status.textContent = "PAPER LEDGER CONNECTED";
    status.className = "count-pill positive";
    $("sol-starting").textContent = number(data.starting_balance_sol, 6) + " SOL";
    $("sol-cash").textContent = number(data.cash_sol, 6) + " SOL";
    $("sol-realized").textContent = (Number(data.realized_pnl_lamports) > 0 ? "+" : "") +
      number(data.realized_pnl_sol, 6) + " SOL";
    $("sol-realized").className = Number(data.realized_pnl_lamports) >= 0 ? "positive" : "negative";
    $("sol-open").textContent = number(data.open_position_count, 0);
    $("sol-fills").textContent = number(data.entry_fills, 0) + " entries · " +
      number(data.exit_fills, 0) + " exits";
    $("sol-equity").textContent = data.equity_complete
      ? "Equity " + number(data.equity_sol, 6) + " SOL"
      : number((data.unmarked_positions || []).length, 0) + " unmarked · equity incomplete";
    $("sol-wallet-note").textContent =
      "All balances and P&L are integer lamports · " +
      number(data.fill_count, 0) + " persisted fills · live execution disabled";
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
      row.classList.add("market-row-selectable");
      row.tabIndex = 0;
      row.setAttribute("aria-label", "Open live chart for " + (item.symbol || item.mint));
      const openChart = () => {
        $("chart-mint").value = item.mint;
        $("chart-pair").value = "";
        loadCandles();
        $("token-terminal").scrollIntoView({behavior: "smooth", block: "center"});
      };
      row.addEventListener("click", openChart);
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openChart();
        }
      });
    }, "No market snapshots available.");
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

  function renderCandles(data) {
    const svg = $("token-candle-chart");
    svg.replaceChildren();
    const candles = data.candles || [];
    $("candle-empty").hidden = candles.length > 0;
    $("candle-freshness").textContent = (data.freshness || "unavailable").toUpperCase();
    $("candle-freshness").className = "count-pill " +
      (data.freshness === "fresh" ? "positive" : "negative");
    $("candle-source").textContent = data.source ? "Source: " + data.source : "No provider observations";
    $("candle-updated").textContent = data.observed_at_ms
      ? "Observed " + date(new Date(data.observed_at_ms).toISOString()) : "No observation timestamp";
    if (!candles.length) return;
    const ns = "http://www.w3.org/2000/svg";
    const highs = candles.map(c => Number(c.high));
    const lows = candles.map(c => Number(c.low));
    const max = Math.max(...highs), min = Math.min(...lows);
    const span = Math.max(max - min, max * 0.000001);
    const left = 42, right = 880, top = 16, bottom = 220;
    const volumeTop = 238, volumeBottom = 282;
    const step = (right - left) / candles.length;
    const bodyWidth = Math.max(2, Math.min(12, step * 0.62));
    const y = value => bottom - ((value - min) / span) * (bottom - top);
    [0,1,2,3].forEach(i => {
      const gy = top + (bottom-top)*i/3;
      const grid = document.createElementNS(ns,"line");
      grid.setAttribute("x1",left); grid.setAttribute("x2",right);
      grid.setAttribute("y1",gy); grid.setAttribute("y2",gy);
      grid.setAttribute("class","chart-grid"); svg.appendChild(grid);
    });
    const maxVol = Math.max(1,...candles.map(c=>Number(c.volume)||0));
    candles.forEach((c,i) => {
      const x = left + step*(i+0.5);
      const up = Number(c.close) >= Number(c.open);
      const color = up ? "#31d68b" : "#ff557f";
      const wick = document.createElementNS(ns,"line");
      wick.setAttribute("x1",x); wick.setAttribute("x2",x);
      wick.setAttribute("y1",y(Number(c.high))); wick.setAttribute("y2",y(Number(c.low)));
      wick.setAttribute("stroke",color); wick.setAttribute("stroke-width","1.4");
      svg.appendChild(wick);
      const body = document.createElementNS(ns,"rect");
      body.setAttribute("x",x-bodyWidth/2);
      body.setAttribute("y",Math.min(y(Number(c.open)),y(Number(c.close))));
      body.setAttribute("width",bodyWidth);
      body.setAttribute("height",Math.max(1,Math.abs(y(Number(c.open))-y(Number(c.close)))));
      body.setAttribute("rx","1"); body.setAttribute("fill",color);
      svg.appendChild(body);
      const vol = document.createElementNS(ns,"rect");
      vol.setAttribute("x",x-bodyWidth/2);
      vol.setAttribute("y",volumeBottom-(Number(c.volume)||0)/maxVol*(volumeBottom-volumeTop));
      vol.setAttribute("width",bodyWidth);
      vol.setAttribute("height",Math.max(1,(Number(c.volume)||0)/maxVol*(volumeBottom-volumeTop)));
      vol.setAttribute("fill",color); vol.setAttribute("opacity",".55");
      svg.appendChild(vol);
    });
    const label = document.createElementNS(ns,"text");
    label.setAttribute("x",left); label.setAttribute("y",12);
    label.setAttribute("class","chart-axis"); label.textContent="OHLC · USD";
    svg.appendChild(label);
  }

  let candleLoading = false;
  async function loadCandles() {
    if (candleLoading) return;
    const mint = $("chart-mint").value.trim();
    let pair = $("chart-pair").value.trim();
    const interval = $("chart-interval").value;
    if (!mint) {
      $("candle-empty").hidden = false;
      $("candle-empty").textContent = "Enter a Solana token mint to resolve its market pool.";
      return;
    }
    candleLoading = true;
    try {
      if (!pair) {
        $("candle-source").textContent = "Resolving deepest eligible Solana pool…";
        const resolved = await api("/api/pool?" + new URLSearchParams({mint}).toString());
        pair = resolved.pair_address;
        $("chart-pair").value = pair;
        $("candle-source").textContent = "Pool: " + (resolved.dex_id || "DEX") +
          " · Liquidity " + money(resolved.liquidity_usd) +
          " · 1h volume " + money(resolved.volume_h1_usd);
      }
      const query = new URLSearchParams({mint, pair, interval, limit:"180"});
      const data = await api("/api/candles?" + query.toString());
      renderCandles(data);
    } catch (error) {
      $("candle-freshness").textContent = "UNAVAILABLE";
      $("candle-source").textContent = error.message;
      $("candle-empty").hidden = false;
      $("candle-empty").textContent = "Candle data unavailable. The chart will not fabricate price movement.";
    } finally {
      candleLoading = false;
    }
  }

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const [health, overview, positions, trades, decisions, consensus, markets, curve, costs, solWallet] =
        await Promise.all([
          api("/api/health"), api("/api/overview"), api("/api/positions"),
          api("/api/trades"), api("/api/decisions"), api("/api/consensus"),
          api("/api/markets"), api("/api/equity-curve"), api("/api/costs"),
          api("/api/sol-paper-wallet")
        ]);
      renderMetrics(overview);
      renderPositions(positions);
      renderTrades(trades);
      renderDecisions(decisions);
      renderConsensus(consensus);
      renderMarkets(markets);
      if (!$("chart-mint").value.trim() && Array.isArray(markets)) {
        const initialMarket = markets.find((item) =>
          Number(item.selected_pool_liquidity_usd) >= 1000 &&
          Number(item.selected_pool_volume_h1_usd) > 0 &&
          (!Array.isArray(item.quality_flags) || item.quality_flags.length === 0)
        );
        if (initialMarket) {
          $("chart-mint").value = initialMarket.mint;
          loadCandles();
        }
      }
      renderEquityCurve(curve);
      renderCosts(costs);
      renderSolWallet(solWallet);
      const worker = health.worker || { status: "not_configured", healthy: false };
      const candleHealth = health.candle_refresh || { status: "not_configured", total_jobs: 0 };
      const workerHealthy = worker.healthy === true;
      const candleHealthy = candleHealth.status !== "degraded";
      const workerState = worker.status || "unknown";
      $("health-led").className = "status-led" + (workerHealthy && candleHealthy ? "" : " bad");
      $("health-label").textContent = "Database connected · worker " + workerState +
        " · candles " + (candleHealth.status || "unknown");
      const workerMeta = worker.last_success_at
        ? " · Last success " + date(worker.last_success_at)
        : " · No successful worker cycle recorded";
      const workerError = worker.last_error ? " · Last error " + worker.last_error : "";
      $("health-detail").textContent =
        "Live execution: disabled · Worker failures: " +
        number(worker.consecutive_failures || 0, 0) +
        " · Last duration: " + number(worker.last_duration_ms || 0, 0) + " ms" +
        workerMeta + workerError +
        " · Candle pools: " + number(candleHealth.total_jobs || 0, 0) +
        " · Candle retries: " + number(candleHealth.retry_jobs || 0, 0) +
        " · Last candle success: " + date(candleHealth.last_success_at) +
        " · Missing paper tables: " + ((health.missing_paper_tables || []).join(", ") || "none");
      $("last-updated").textContent = "Updated " + new Date().toLocaleTimeString();
    } catch (error) {
      $("health-led").className = "status-led bad";
      $("health-label").textContent = "Dashboard data unavailable";
      $("health-detail").textContent = error.message;
      $("last-updated").textContent = "Refresh failed";
    } finally {
      refreshing = false;
    }
  }

  $("refresh").addEventListener("click", refresh);
  $("load-candles").addEventListener("click", loadCandles);
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
  window.setInterval(() => { if ($("chart-mint").value.trim() && $("chart-pair").value.trim()) loadCandles(); }, 15_000);
})();
