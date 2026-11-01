// Tach Trading Bot dashboard. It reads the JSON files in data/ that the bot writes after each
// run, and shows stock price charts from TradingView. It talks to nothing else. All text goes
// in through textContent, so words from the trade log or the AI can only appear as plain text.
"use strict";

(function () {
  const STOCK_NAMES = {
    SPY: "S&P 500 fund",
    QQQ: "Nasdaq-100 fund",
    AAPL: "Apple",
    MSFT: "Microsoft",
    JPM: "JPMorgan Chase",
    "BTC/USD": "Bitcoin",
    "ETH/USD": "Ether",
  };
  const MARKETS = {
    stock: {
      title: "Stocks",
      page: "stocks",
      list: "stocks",
      back: "All stocks",
      intro: "The stocks the bot follows and how it sees each one. Open a stock to see its price history and your trades.",
    },
    crypto: {
      title: "Crypto",
      page: "crypto",
      list: "crypto",
      back: "All crypto",
      intro: "The crypto pairs the bot follows and how it sees each one. Open a pair to see its price history and your trades.",
    },
  };
  const RUN_STATES = {
    ok: ["good", "✓ Ran normally"],
    market_closed: ["", "Market closed"],
    dry_run: ["", "Test run"],
    error: ["critical", "✕ Error"],
  };
  const FILES = {
    status: "status",
    positions: "positions",
    trades: "trades",
    history: "equity_history",
    runs: "runs",
    watchlist: "watchlist",
  };

  const data = { status: null, positions: [], trades: [], history: [], runs: [], watchlist: { stocks: [], crypto: [] } };
  const view = document.getElementById("view");
  let chart = null;
  let activeMarket = "stock";

  // ------------------------------------------------------------------------
  // Small helpers
  // ------------------------------------------------------------------------
  const byId = (id) => document.getElementById(id);
  const color = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const hasNumber = (value) => typeof value === "number" && Number.isFinite(value);
  const tone = (value) => (value > 0 ? "good" : value < 0 ? "critical" : "");
  const shares = (value) => (hasNumber(value) ? value.toLocaleString("en-US", { maximumFractionDigits: 8 }) : "–");
  const marketOf = (symbol) => (symbol.includes("/") ? "crypto" : "stock");
  const held = (position) => `${shares(position.qty)}${marketOf(position.symbol) === "crypto" ? "" : " shares"}`;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function money(value) {
    if (!hasNumber(value)) return "–";
    return value.toLocaleString("en-US", { style: "currency", currency: "USD" }).replace("-", "−");
  }

  function signed(text, value) {
    return value > 0 ? `+${text}` : text;
  }

  function percent(value) {
    if (!hasNumber(value)) return "–";
    return signed(`${(value * 100).toFixed(2)}%`, value).replace("-", "−");
  }

  // The log stores UTC as "2026-11-02 15:00:00"; the other files use "2026-11-02T15:00:00Z".
  function toDate(text) {
    return new Date(text.includes("T") ? text : `${text.replace(" ", "T")}Z`);
  }

  function timeText(text) {
    return toDate(text).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  }

  function agoText(text) {
    const minutes = Math.max(0, Math.round((Date.now() - toDate(text).getTime()) / 60000));
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours} h ago`;
    return `${Math.round(hours / 24)} days ago`;
  }

  function setText(id, text, className) {
    const node = byId(id);
    if (!node) return;
    node.textContent = text;
    if (className !== undefined) {
      const base = node.dataset.base || node.className;
      node.dataset.base = base;
      node.className = className ? `${base} ${className}` : base;
    }
  }

  // columns: [{ title, value(row), numeric?, tone?(row), link?(row) }]
  function buildTable(columns, rows) {
    const table = element("table");
    const headRow = table.createTHead().insertRow();
    for (const column of columns) {
      const heading = element("th", column.numeric ? "num" : "", column.title);
      heading.scope = "col";
      headRow.append(heading);
    }
    const body = table.createTBody();
    for (const row of rows) {
      const tableRow = body.insertRow();
      for (const column of columns) {
        const cell = tableRow.insertCell();
        const text = column.value(row);
        if (column.link && column.link(row)) {
          const link = element("a", "", text);
          link.href = column.link(row);
          cell.append(link);
        } else {
          cell.textContent = text;
        }
        cell.dataset.label = column.title;
        cell.className = [column.numeric ? "num" : "", column.tone ? column.tone(row) : ""]
          .filter(Boolean).join(" ");
      }
    }
    return table;
  }

  function show(holder, content) {
    if (!holder) return;
    holder.replaceChildren(typeof content === "string" ? element("p", "note", content) : content);
  }

  function stockLink(symbol) {
    if (marketOf(symbol) === "crypto") return `#/crypto/${encodeURIComponent(symbol.replace("/", "-"))}`;
    return `#/stocks/${encodeURIComponent(symbol)}`;
  }

  function watchlistOf(market) {
    return data.watchlist[MARKETS[market].list] || [];
  }

  // Everything the bot follows in one market, plus anything it holds or traded there.
  function knownSymbols(market) {
    const symbols = watchlistOf(market).map((stock) => stock.symbol);
    if (market === "crypto" && data.status) {
      for (const symbol of data.status.crypto_symbols || []) {
        if (!symbols.includes(symbol)) symbols.push(symbol);
      }
    }
    for (const row of [...data.positions, ...data.trades]) {
      if (row.symbol && marketOf(row.symbol) === market && !symbols.includes(row.symbol)) symbols.push(row.symbol);
    }
    return symbols;
  }

  // ------------------------------------------------------------------------
  // The warning shown on every page when the bot goes quiet
  // ------------------------------------------------------------------------
  // The bot runs on weekdays from 13:37 to 20:52 UTC, even on market holidays. This finds the
  // newest moment it should have updated by. A day's first hour is skipped, so a late first
  // run raises no alarm.
  function latestExpectedUpdate(now) {
    const MINUTE = 60000;
    const DAY = 1440 * MINUTE;
    const FIRST_RUN = (13 * 60 + 37) * MINUTE;
    const LAST_RUN = (20 * 60 + 52) * MINUTE;
    const midnight = now - (now % DAY);
    for (let back = 0; back < 7; back += 1) {
      const day = midnight - back * DAY;
      const weekday = new Date(day).getUTCDay();
      if (weekday === 0 || weekday === 6 || now < day + FIRST_RUN + 60 * MINUTE) continue;
      return Math.min(now, day + LAST_RUN);
    }
    return 0;
  }

  function showStaleWarning(lastUpdate) {
    const ALLOWED_GAP = 90 * 60000;   // GitHub can start scheduled runs late
    const now = Date.now();
    const expected = data.status.crypto_enabled ? now : latestExpectedUpdate(now);   // crypto runs 24/7
    if (expected - toDate(lastUpdate).getTime() <= ALLOWED_GAP) return;
    byId("stale-text").textContent =
      `⚠ The last update was ${agoText(lastUpdate)}, but the bot should have run since then. It may have stopped.`;
    byId("stale-warning").hidden = false;
  }

  // ------------------------------------------------------------------------
  // The tachometer: the needle is today's change, the red zone is the kill switch
  // ------------------------------------------------------------------------
  function drawDial(change, killPct) {
    const svg = byId("dial");
    const NS = "http://www.w3.org/2000/svg";
    const CENTER_X = 160;
    const CENTER_Y = 150;
    const RADIUS = 132;
    const limit = killPct * 100;
    const end = Math.ceil(limit) + 2;

    const add = (tag, attributes, text) => {
      const node = document.createElementNS(NS, tag);
      for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
      if (text !== undefined) node.textContent = text;
      svg.append(node);
      return node;
    };
    const turn = (percentage) => ((percentage + end) / (2 * end)) * 240 - 120;   // 0 points up
    const point = (percentage, distance) => {
      const radians = (turn(percentage) * Math.PI) / 180;
      return [CENTER_X + distance * Math.sin(radians), CENTER_Y - distance * Math.cos(radians)];
    };
    const arc = (from, to) => {
      const [startX, startY] = point(from, RADIUS);
      const [endX, endY] = point(to, RADIUS);
      const large = turn(to) - turn(from) > 180 ? 1 : 0;
      return `M ${startX} ${startY} A ${RADIUS} ${RADIUS} 0 ${large} 1 ${endX} ${endY}`;
    };

    svg.replaceChildren();
    add("path", { class: "dial-track", d: arc(-end, end) });
    add("path", { class: "dial-red", d: arc(-end, -limit) });
    for (let tick = -end; tick <= end; tick += 0.5) {
      const major = Number.isInteger(tick);
      const [outerX, outerY] = point(tick, RADIUS - 7);
      const [innerX, innerY] = point(tick, RADIUS - (major ? 20 : 13));
      const classes = ["dial-tick", major ? "dial-tick-major" : "", tick <= -limit ? "dial-tick-red" : ""];
      add("line", { class: classes.filter(Boolean).join(" "), x1: outerX, y1: outerY, x2: innerX, y2: innerY });
      if (major) {
        const [textX, textY] = point(tick, RADIUS - 36);
        add("text", { class: "dial-number", x: textX, y: textY }, tick < 0 ? `−${-tick}` : `${tick}`);
      }
    }

    const needle = add("line", {
      class: "dial-needle", x1: CENTER_X, y1: CENTER_Y + 14, x2: CENTER_X, y2: CENTER_Y - RADIUS + 26,
    });
    needle.style.transform = `rotate(${turn(-end)}deg)`;
    add("circle", { class: "dial-hub", cx: CENTER_X, cy: CENTER_Y, r: 9 });
    add("text", { class: "dial-value", x: CENTER_X, y: 212 }, hasNumber(change) ? percent(change) : "–");
    add("text", { class: "dial-unit", x: CENTER_X, y: 232 }, "today");

    const value = hasNumber(change) ? change * 100 : 0;
    const resting = Math.max(-end, Math.min(end, value));
    requestAnimationFrame(() => requestAnimationFrame(() => {
      needle.style.transform = `rotate(${turn(resting)}deg)`;
    }));
    svg.setAttribute("aria-label", `Today's change is ${hasNumber(change) ? percent(change) : "not known"}. `
      + `The kill switch trips at −${limit}%, where the dial turns red.`);
  }

  // ------------------------------------------------------------------------
  // Overview page
  // ------------------------------------------------------------------------
  function renderSummary() {
    const status = data.status;
    if (!status) {
      setText("updated", "No data yet. This fills in after the bot's next run.");
      drawDial(null, 0.03);
      return;
    }
    const run = status.last_run;
    const limits = status.limits || { kill_switch_pct: 0.03, max_trades_per_day: 3 };
    setText("updated", `Last update ${timeText(run.time_utc)} (${agoText(run.time_utc)})`);
    setText("equity", money(status.equity));
    setText("cash", money(status.cash));
    setText("invested", money(status.invested));
    setText("open-pl", signed(money(status.open_pl), status.open_pl), tone(status.open_pl));
    setText("trades-today", `${status.trades_today ?? "–"} of ${limits.max_trades_per_day}`);
    drawDial(status.day_change_pct, limits.kill_switch_pct);

    const [runTone, runLabel] = RUN_STATES[run.status] || ["", run.status];
    setText("run-status", runLabel, runTone);
    setText("run-message", run.status === "error" ? run.message : "");
    const market = status.market_open;
    setText("market", market === true ? "● Open" : market === false ? "Closed" : "Unknown", market ? "good" : "");
    const kill = status.kill_switch;
    setText("kill-switch", kill.on ? "✕ On" : "✓ Off", kill.on ? "critical" : "good");
    setText("kill-reason", kill.on ? kill.reason || "No new trades today."
      : `Trips at −${limits.kill_switch_pct * 100}%, the red zone on the dial.`);
    setText("ai-analyst", status.ai_analyst_on ? "On" : "Off");
  }

  function renderRuns() {
    const strip = byId("run-strip");
    const runs = data.runs.slice(-30);
    if (!runs.length) {
      setText("run-summary", "Each run the bot makes appears here.");
      return;
    }
    strip.replaceChildren(...runs.map((run) => {
      const classes = [`is-${run.status}`, run.orders ? "has-orders" : ""];
      const tick = element("li", classes.filter(Boolean).join(" "));
      const [, label] = RUN_STATES[run.status] || ["", run.status];
      tick.title = `${timeText(run.time_utc)}: ${label}${run.orders ? `, ${run.orders} order(s)` : ""}`;
      return tick;
    }));
    const errors = runs.filter((run) => run.status === "error").length;
    const orders = runs.reduce((total, run) => total + run.orders, 0);
    const count = (number, word) => `${number} ${word}${number === 1 ? "" : "s"}`;
    setText("run-summary", `Last ${count(runs.length, "run")}: ${count(errors, "error")}, ${count(orders, "order")} placed. Tall green ticks placed orders and red ones had errors.`);
  }

  function drawEquityChart(days) {
    if (chart) chart.destroy();
    chart = null;
    const note = byId("chart-note");
    note.hidden = true;
    if (typeof window.Chart === "undefined") {
      byId("equity-details").open = true;
      note.textContent = "The chart library didn't load, so the numbers are in the table below.";
      note.hidden = false;
      return;
    }
    const newest = Date.now();
    const recent = days === "all" ? data.history
      : data.history.filter((point) => newest - toDate(point.time_utc).getTime() <= days * 86400000);
    const points = recent.length ? recent : data.history;
    if (points.length < 2) {
      note.textContent = "One data point so far. The line appears once the account value changes.";
      note.hidden = false;
    }
    const last = points.length - 1;
    const crosshair = {
      id: "crosshair",
      afterDatasetsDraw(target) {
        const active = target.tooltip && target.tooltip.getActiveElements()[0];
        if (!active) return;
        const { ctx, chartArea } = target;
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(active.element.x, chartArea.top);
        ctx.lineTo(active.element.x, chartArea.bottom);
        ctx.lineWidth = 1;
        ctx.strokeStyle = color("--axis");
        ctx.stroke();
        ctx.restore();
      },
    };
    chart = new window.Chart(byId("equity-chart"), {
      type: "line",
      plugins: [crosshair],
      data: {
        labels: points.map((point) => timeText(point.time_utc)),
        datasets: [{
          data: points.map((point) => point.equity),
          borderColor: color("--series"),
          borderWidth: 2,
          borderCapStyle: "round",
          borderJoinStyle: "round",
          tension: 0,
          pointRadius: points.map((_, index) => (index === last ? 4 : 0)),
          pointHoverRadius: 5,
          pointBackgroundColor: color("--series"),
          pointBorderColor: color("--plane"),
          pointBorderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: {
            grid: { display: false },
            border: { color: color("--rule") },
            ticks: {
              color: color("--axis"),
              maxRotation: 0,
              autoSkipPadding: 24,
              maxTicksLimit: 6,
              callback: (value) => toDate(points[value].time_utc).toLocaleDateString(undefined, {
                month: "short", day: "numeric",
              }),
            },
          },
          y: {
            grid: { color: color("--rule") },
            border: { display: false },
            ticks: {
              color: color("--axis"),
              maxTicksLimit: 6,
              callback: (value) => value.toLocaleString("en-US", {
                style: "currency", currency: "USD", maximumFractionDigits: 0,
              }),
            },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            displayColors: false,
            backgroundColor: color("--dash"),
            titleColor: color("--dash-ink-2"),
            bodyColor: color("--dash-ink"),
            callbacks: { label: (item) => money(item.parsed.y) },
          },
        },
      },
    });
  }

  function renderEquity() {
    if (!data.history.length) {
      setText("chart-note", "The chart fills in as the bot runs.");
      byId("chart-note").hidden = false;
      byId("equity-details").hidden = true;
      byId("equity-ranges").hidden = true;
      return;
    }
    show(byId("equity-table"), buildTable([
      { title: "Time", value: (point) => timeText(point.time_utc) },
      { title: "Account value", value: (point) => money(point.equity), numeric: true },
    ], data.history.slice(-50).reverse()));
    drawEquityChart("all");
    byId("equity-ranges").addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button) return;
      pressOnly(button);
      drawEquityChart(button.dataset.range === "all" ? "all" : Number(button.dataset.range));
    });
  }

  function pressOnly(button) {
    for (const other of button.parentElement.querySelectorAll("button")) {
      other.setAttribute("aria-pressed", String(other === button));
    }
  }

  function renderBreakdown() {
    const status = data.status;
    if (!status) return;
    const parts = [
      { name: "Stocks", value: status.stock_value, className: "is-stock" },
      { name: "Crypto", value: status.crypto_value, className: "is-crypto" },
      { name: "Cash", value: status.cash, className: "is-cash" },
    ].map((part) => ({ ...part, value: hasNumber(part.value) ? Math.max(0, part.value) : 0 }));
    const total = parts.reduce((sum, part) => sum + part.value, 0);
    const share = (part) => (total ? Math.round((part.value / total) * 100) : 0);

    const bar = byId("breakdown");
    bar.replaceChildren(...parts.filter((part) => part.value > 0).map((part) => {
      const block = element("div", `breakdown-part ${part.className}`, share(part) >= 12 ? `${share(part)}%` : "");
      block.style.flexGrow = part.value;
      block.title = `${part.name}: ${money(part.value)}`;
      return block;
    }));
    bar.setAttribute("aria-label", parts.map((part) => `${part.name} ${money(part.value)}, ${share(part)}%`).join("; "));

    byId("breakdown-legend").replaceChildren(...parts.map((part) => {
      const item = element("li");
      item.append(element("span", `swatch ${part.className}`), element("span", "", part.name),
        element("span", "", `${money(part.value)} (${share(part)}%)`));
      return item;
    }));
  }

  function renderMarketTab() {
    for (const tab of document.querySelectorAll('[role="tab"]')) {
      const selected = tab.dataset.market === activeMarket;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      if (selected) byId("market-panel").setAttribute("aria-labelledby", tab.id);
    }
    const status = data.status || {};
    const limit = status.limits ? status.limits.max_trades_per_day : 3;
    const isMarket = (row) => (row.asset_class || "stock") === activeMarket;
    const name = activeMarket === "crypto" ? "crypto" : "stock";
    let note = `Stock trades today: ${status.trades_today ?? "–"} of ${limit}.`;
    if (activeMarket === "crypto") {
      note = status.crypto_enabled
        ? `Crypto trades today: ${status.crypto_trades_today ?? "–"} of ${limit}, counted by UTC day.`
        : "Crypto trading is off. Trades appear here once it's turned on.";
    }
    setText("market-note", note);

    const positions = data.positions.filter(isMarket);
    const trades = data.trades.filter(isMarket);
    show(byId("positions"), positions.length
      ? buildTable(positionColumns(activeMarket), positions)
      : `No open ${name} positions.`);
    show(byId("latest-trades"), trades.length
      ? tradeTable(trades.slice(0, 10), true, activeMarket)
      : `No ${name} trades yet.`);
  }

  function renderTabs() {
    renderMarketTab();
    const tabs = [...document.querySelectorAll('[role="tab"]')];
    const choose = (tab) => {
      activeMarket = tab.dataset.market;
      renderMarketTab();
      tab.focus();
    };
    for (const tab of tabs) {
      tab.addEventListener("click", () => choose(tab));
      tab.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
        event.preventDefault();
        choose(tabs[(tabs.indexOf(tab) + 1) % tabs.length]);
      });
    }
  }

  function positionColumns(market) {
    const columns = [
      { title: "Stock", value: (p) => p.symbol, link: (p) => stockLink(p.symbol) },
      { title: market === "crypto" ? "Amount" : "Shares", value: (p) => shares(p.qty), numeric: true },
      { title: "Bought at", value: (p) => money(p.avg_entry_price), numeric: true },
      { title: "Now", value: (p) => money(p.current_price), numeric: true },
      { title: "Value", value: (p) => money(p.market_value), numeric: true },
      {
        title: "Profit or loss",
        value: (p) => `${signed(money(p.unrealized_pl), p.unrealized_pl)} (${percent(p.unrealized_pl_pct)})`,
        numeric: true,
        tone: (p) => tone(p.unrealized_pl),
      },
      { title: "Stop-loss at", value: (p) => money(p.stop_price), numeric: true },
    ];
    if (market === "crypto") columns[0].title = "Pair";
    return columns;
  }

  function aiText(trade) {
    if (!trade.ai_decision) return "Not asked";
    if (trade.ai_decision === "OFF") return "Off";
    return trade.ai_reason ? `${trade.ai_decision}: ${trade.ai_reason}` : trade.ai_decision;
  }

  function actionText(trade) {
    const side = trade.side.toUpperCase();
    return trade.event === "AI_DECISION" ? `Vetoed ${side}` : side;
  }

  function tradeTable(trades, withSymbol, market) {
    const columns = [
      { title: "Time", value: (t) => timeText(t.time_utc) },
      { title: "Action", value: actionText },
      { title: market === "stock" ? "Shares" : "Amount", value: (t) => shares(t.qty), numeric: true },
      { title: "Price", value: (t) => money(t.price), numeric: true },
      { title: "Status", value: (t) => t.order_status || "–" },
      { title: "Reason", value: (t) => t.reason || "–" },
      { title: "AI decision", value: aiText },
    ];
    const symbolTitle = market === "crypto" ? "Pair" : market === "stock" ? "Stock" : "Symbol";
    if (withSymbol) columns.splice(2, 0, { title: symbolTitle, value: (t) => t.symbol, link: (t) => stockLink(t.symbol) });
    return buildTable(columns, trades);
  }

  function showOverview() {
    renderSummary();
    renderEquity();
    renderBreakdown();
    renderTabs();
    renderRuns();
  }

  // ------------------------------------------------------------------------
  // Stocks pages
  // ------------------------------------------------------------------------
  function trendText(stock) {
    if (stock.trend === "UP") return "Trend up";
    if (stock.trend === "DOWN") return "Trend down";
    return "Trend not known yet";
  }

  function signalText(stock) {
    if (stock.signal === "BUY") return "Buy signal";
    if (stock.signal === "SELL") return "Sell signal";
    if (stock.signal === "HOLD") return "No signal";
    return "–";
  }

  function viewOf(symbol) {
    return watchlistOf(marketOf(symbol)).find((stock) => stock.symbol === symbol)
      || { symbol, trend: "", signal: "", signal_date: "" };
  }

  function stockRow(symbol) {
    const stock = viewOf(symbol);
    const position = data.positions.find((p) => p.symbol === symbol);
    const row = element("a", "stock-row");
    row.href = stockLink(symbol);

    const title = element("div", "stock-title");
    title.append(element("div", "stock-symbol", symbol), element("div", "stock-name", STOCK_NAMES[symbol] || "Stock"));

    const trend = element("div", "stock-cell", trendText(stock));
    const signal = element("div", "stock-cell", signalText(stock));
    if (stock.signal_date) {
      const day = new Date(`${stock.signal_date}T12:00:00Z`);
      const label = day.toLocaleDateString(undefined, { month: "short", day: "numeric" });
      signal.append(element("span", "", `from the ${label} close`));
    }

    const holding = element("div", "stock-cell", position ? `${held(position)} held` : "Not held");
    if (position) {
      holding.append(element("span", tone(position.unrealized_pl),
        `${signed(money(position.unrealized_pl), position.unrealized_pl)} (${percent(position.unrealized_pl_pct)})`));
    }
    row.append(title, trend, signal, holding);
    return row;
  }

  function showMarket(market) {
    setText("market-title", MARKETS[market].title);
    setText("market-intro", market === "crypto" && data.status && !data.status.crypto_enabled
      ? `${MARKETS.crypto.intro} Crypto trading is off for now.`
      : MARKETS[market].intro);
    const symbols = knownSymbols(market);
    const list = byId("stock-list");
    if (!symbols.length) return show(list, "This list fills in after the bot's next run.");
    list.replaceChildren(...symbols.map(stockRow));
  }

  function fact(list, term, detail, className) {
    const group = element("div");
    group.append(element("dt", "", term), element("dd", className || "", detail));
    list.append(group);
  }

  function mountPriceChart(holder, symbol) {
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    const box = element("div", "tradingview-widget-container");
    const inner = element("div", "tradingview-widget-container__widget");
    const script = element("script");
    script.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
    script.async = true;
    script.textContent = JSON.stringify({
      autosize: true,
      symbol: marketOf(symbol) === "crypto" ? `COINBASE:${symbol.replace("/", "")}` : symbol,
      interval: "D",
      range: "12M",
      timezone: "exchange",
      theme: dark ? "dark" : "light",
      style: "1",
      locale: "en",
      allow_symbol_change: false,
      save_image: false,
      calendar: false,
      hide_volume: false,
      support_host: "https://www.tradingview.com",
    });
    box.append(inner, script);
    holder.replaceChildren(box);

    const credit = element("p", "chart-credit", "Price chart ");
    const link = element("a", "", "by TradingView");
    link.href = "https://www.tradingview.com/";
    link.rel = "noopener nofollow";
    link.target = "_blank";
    credit.append(link);
    holder.after(credit);
  }

  function showStock(symbol) {
    const market = marketOf(symbol);
    const back = byId("stock-back");
    back.textContent = MARKETS[market].back;
    back.href = `#/${MARKETS[market].page}`;
    if (!knownSymbols(market).includes(symbol)) {
      setText("stock-symbol", symbol);
      setText("stock-name", "");
      show(byId("stock-trades"), "The bot doesn't follow this.");
      byId("stock-chart").hidden = true;
      return;
    }
    document.title = `${symbol}, Tach Trading Bot`;
    setText("stock-symbol", symbol);
    setText("stock-name", STOCK_NAMES[symbol] || "");

    const stock = viewOf(symbol);
    const position = data.positions.find((p) => p.symbol === symbol);
    const facts = byId("stock-facts");
    fact(facts, "The bot's view", trendText(stock));
    fact(facts, "Latest signal", signalText(stock));
    fact(facts, "Your position", position ? `${held(position)} at ${money(position.avg_entry_price)}` : "Not held");
    if (position) {
      fact(facts, "Profit or loss",
        `${signed(money(position.unrealized_pl), position.unrealized_pl)} (${percent(position.unrealized_pl_pct)})`,
        tone(position.unrealized_pl));
      fact(facts, "Stop-loss sells at", money(position.stop_price));
    }

    mountPriceChart(byId("stock-chart"), symbol);
    const trades = data.trades.filter((trade) => trade.symbol === symbol);
    show(byId("stock-trades"), trades.length ? tradeTable(trades, false, market) : "The bot hasn't traded this yet.");
  }

  // ------------------------------------------------------------------------
  // Trades page
  // ------------------------------------------------------------------------
  function matchesFilter(trade, filter) {
    if (filter === "veto") return trade.event === "AI_DECISION";
    if (filter === "buy" || filter === "sell") return trade.event === "ORDER" && trade.side === filter;
    if (filter === "stock" || filter === "crypto") return (trade.asset_class || "stock") === filter;
    return true;
  }

  function showTrades() {
    const holder = byId("all-trades");
    const render = (filter) => {
      const trades = data.trades.filter((trade) => matchesFilter(trade, filter));
      show(holder, trades.length ? tradeTable(trades, true, "all") : "Nothing to show here yet.");
    };
    render("all");
    byId("trade-filters").addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button) return;
      pressOnly(button);
      render(button.dataset.filter);
    });
  }

  // ------------------------------------------------------------------------
  // Moving between pages
  // ------------------------------------------------------------------------
  function open(templateId, page) {
    if (chart) chart.destroy();
    chart = null;
    document.title = "Tach Trading Bot";
    for (const credit of document.querySelectorAll(".chart-credit")) credit.remove();
    view.replaceChildren(document.importNode(byId(templateId).content, true));
    for (const link of document.querySelectorAll(".pages a")) {
      if (link.dataset.page === page) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    scrollTo(0, 0);
  }

  function route(moved) {
    const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
    if ((parts[0] === "stocks" || parts[0] === "crypto") && parts[1]) {
      let symbol = "";
      try {
        symbol = decodeURIComponent(parts[1]).toUpperCase();
      } catch {
        location.hash = `#/${parts[0]}`;
        return;
      }
      open("stock-page", parts[0]);
      showStock(parts[0] === "crypto" ? symbol.replace("-", "/") : symbol);
    } else if (parts[0] === "stocks") {
      open("stocks-page", "stocks");
      showMarket("stock");
    } else if (parts[0] === "crypto") {
      open("stocks-page", "crypto");
      showMarket("crypto");
    } else if (parts[0] === "trades") {
      open("trades-page", "trades");
      showTrades();
    } else {
      open("overview-page", "overview");
      showOverview();
    }
    if (moved) view.focus({ preventScroll: true });
  }

  async function start() {
    const names = Object.keys(FILES);
    const results = await Promise.allSettled(names.map((name) => (
      fetch(`data/${FILES[name]}.json?t=${Date.now()}`, { cache: "no-store" }).then((response) => {
        if (!response.ok) throw new Error(`${name}: ${response.status}`);
        return response.json();
      })
    )));
    names.forEach((name, index) => {
      if (results[index].status === "fulfilled") data[name] = results[index].value;
    });
    if (data.status) showStaleWarning(data.status.last_run.time_utc);
    route(false);
    addEventListener("hashchange", () => route(true));
  }

  start();
})();
