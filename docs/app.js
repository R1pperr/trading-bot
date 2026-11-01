// Dashboard logic. It reads the JSON files in data/ that the bot writes after each run.
// It talks to nothing else, and all text goes in through textContent, so words from the
// trade log or the AI can only ever appear as plain text on the page.
"use strict";

(function () {
  const RUN_STATES = {
    ok: ["good", "✓ Ran normally"],
    market_closed: ["", "Market closed"],
    dry_run: ["", "Dry run"],
    error: ["critical", "✕ Error"],
  };

  const byId = (id) => document.getElementById(id);
  const color = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function load(name) {
    return fetch(`data/${name}.json?t=${Date.now()}`, { cache: "no-store" }).then((response) => {
      if (!response.ok) throw new Error(`${name}: ${response.status}`);
      return response.json();
    });
  }

  function setState(id, text, tone) {
    const element = byId(id);
    element.textContent = text;
    element.className = tone ? `state ${tone}` : "state";
  }

  const hasNumber = (value) => typeof value === "number" && Number.isFinite(value);

  function money(value) {
    if (!hasNumber(value)) return "–";
    return value.toLocaleString("en-US", { style: "currency", currency: "USD" });
  }

  function signed(text, value) {
    return value > 0 ? `+${text}` : text;
  }

  function percent(value) {
    if (!hasNumber(value)) return "–";
    return signed(`${(value * 100).toFixed(2)}%`, value);
  }

  const shares = (value) => (hasNumber(value) ? value.toLocaleString("en-US") : "–");
  const tone = (value) => (value > 0 ? "good" : value < 0 ? "critical" : "");

  // The log stores UTC as "2026-11-02 15:00:00"; status.json uses "2026-11-02T15:00:00Z".
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

  // columns: [{ title, value(row), numeric?, tone?(row) }]
  function buildTable(columns, rows) {
    const table = document.createElement("table");
    const headRow = table.createTHead().insertRow();
    for (const column of columns) {
      const heading = document.createElement("th");
      heading.textContent = column.title;
      heading.scope = "col";
      if (column.numeric) heading.className = "num";
      headRow.append(heading);
    }
    const body = table.createTBody();
    for (const row of rows) {
      const tableRow = body.insertRow();
      for (const column of columns) {
        const cell = tableRow.insertCell();
        cell.textContent = column.value(row);
        cell.dataset.label = column.title;
        const classes = [column.numeric ? "num" : "", column.tone ? column.tone(row) : ""];
        cell.className = classes.filter(Boolean).join(" ");
      }
    }
    return table;
  }

  function showIn(id, content) {
    const holder = byId(id);
    if (typeof content === "string") {
      const note = document.createElement("p");
      note.className = "note";
      note.textContent = content;
      holder.replaceChildren(note);
    } else {
      holder.replaceChildren(content);
    }
  }

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
    if (latestExpectedUpdate(Date.now()) - toDate(lastUpdate).getTime() <= ALLOWED_GAP) return;
    byId("stale-text").textContent =
      `⚠ The last update was ${agoText(lastUpdate)}, but the bot should have run since then. It may have stopped.`;
    byId("stale-warning").hidden = false;
  }

  function renderStatus(status) {
    const run = status.last_run;
    byId("updated").textContent = `Last update ${timeText(run.time_utc)} (${agoText(run.time_utc)})`;
    showStaleWarning(run.time_utc);
    byId("equity").textContent = money(status.equity);
    const change = byId("day-change");
    if (hasNumber(status.day_change_pct)) {
      change.textContent = `${percent(status.day_change_pct)} since yesterday's close`;
      change.className = `delta ${tone(status.day_change_pct)}`;
    }

    const [runTone, runLabel] = RUN_STATES[run.status] || ["", run.status];
    setState("run-status", runLabel, runTone);
    byId("run-message").textContent = run.message;

    const market = status.market_open;
    setState("market", market === true ? "Open" : market === false ? "Closed" : "Unknown");

    const kill = status.kill_switch;
    setState("kill-switch", kill.on ? "✕ ON" : "✓ Off", kill.on ? "critical" : "good");
    byId("kill-reason").textContent = kill.on ? kill.reason || "No new trades today." : "";

    setState("ai-analyst", status.ai_analyst_on ? "On" : "Off");
  }

  function renderPositions(positions) {
    if (!positions.length) return showIn("positions", "No open positions.");
    showIn("positions", buildTable([
      { title: "Symbol", value: (p) => p.symbol },
      { title: "Shares", value: (p) => shares(p.qty), numeric: true },
      { title: "Bought at", value: (p) => money(p.avg_entry_price), numeric: true },
      { title: "Now", value: (p) => money(p.current_price), numeric: true },
      { title: "Value", value: (p) => money(p.market_value), numeric: true },
      {
        title: "Unrealized P/L",
        value: (p) => `${signed(money(p.unrealized_pl), p.unrealized_pl)} (${percent(p.unrealized_pl_pct)})`,
        numeric: true,
        tone: (p) => tone(p.unrealized_pl),
      },
      { title: "Stop-loss at", value: (p) => money(p.stop_price), numeric: true },
    ], positions));
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

  function renderTrades(trades) {
    if (!trades.length) return showIn("trades", "No trades yet.");
    showIn("trades", buildTable([
      { title: "Time", value: (t) => timeText(t.time_utc) },
      { title: "Action", value: actionText },
      { title: "Symbol", value: (t) => t.symbol },
      { title: "Shares", value: (t) => shares(t.qty), numeric: true },
      { title: "Price", value: (t) => money(t.price), numeric: true },
      { title: "Status", value: (t) => t.order_status || "–" },
      { title: "Reason", value: (t) => t.reason || "–" },
      { title: "AI decision", value: aiText },
    ], trades));
  }

  // Draws the vertical line that follows the pointer and snaps to the nearest point.
  const crosshair = {
    id: "crosshair",
    afterDatasetsDraw(chart) {
      const active = chart.tooltip && chart.tooltip.getActiveElements()[0];
      if (!active) return;
      const { ctx, chartArea } = chart;
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

  function drawChart(history) {
    if (typeof window.Chart === "undefined") {
      byId("equity-details").open = true;
      const note = byId("chart-note");
      note.textContent = "The chart library could not load, so here are the numbers instead.";
      note.hidden = false;
      return;
    }
    const last = history.length - 1;
    new window.Chart(byId("equity-chart"), {
      type: "line",
      plugins: [crosshair],
      data: {
        labels: history.map((point) => timeText(point.time_utc)),
        datasets: [{
          data: history.map((point) => point.equity),
          borderColor: color("--series"),
          borderWidth: 2,
          borderCapStyle: "round",
          borderJoinStyle: "round",
          tension: 0,
          pointRadius: history.map((_, index) => (index === last ? 4 : 0)),
          pointHoverRadius: 5,
          pointBackgroundColor: color("--series"),
          pointBorderColor: color("--surface"),
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
            border: { color: color("--grid") },
            ticks: { color: color("--axis"), maxRotation: 0, autoSkipPadding: 24, maxTicksLimit: 6 },
          },
          y: {
            grid: { color: color("--grid") },
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
            backgroundColor: color("--ink"),
            titleColor: color("--surface"),
            bodyColor: color("--surface"),
            callbacks: { label: (item) => money(item.parsed.y) },
          },
        },
      },
    });
  }

  function renderHistory(history) {
    const note = byId("chart-note");
    if (!history.length) {
      note.textContent = "The chart fills in as the bot runs.";
      note.hidden = false;
      byId("equity-details").hidden = true;
      return;
    }
    if (history.length < 2) {
      note.textContent = "One data point so far. The line appears once the account value changes.";
      note.hidden = false;
    }
    showIn("equity-table", buildTable([
      { title: "Time", value: (point) => timeText(point.time_utc) },
      { title: "Account value", value: (point) => money(point.equity), numeric: true },
    ], history.slice(-50).reverse()));
    drawChart(history);
  }

  function run(name, render, fallback) {
    load(name).then(render).catch(fallback);
  }

  run("status", renderStatus, () => {
    byId("updated").textContent = "No data yet. The dashboard fills in after the bot's next run.";
  });
  run("positions", renderPositions, () => showIn("positions", "No data yet."));
  run("trades", renderTrades, () => showIn("trades", "No data yet."));
  run("equity_history", renderHistory, () => {
    const note = byId("chart-note");
    note.textContent = "No data yet.";
    note.hidden = false;
    byId("equity-details").hidden = true;
  });
})();
