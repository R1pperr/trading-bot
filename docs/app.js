// Tach Trading Bot dashboard. It reads the JSON files in data/ that the bot writes after each
// run, and shows stock price charts from TradingView. It talks to nothing else. All text goes
// in through textContent, so words from the trade log or the AI can only appear as plain text.
// Everything that moves is written here or in style.css, and all of it stops for anyone who
// has asked their device to reduce motion.
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
    stock: { title: "Stocks", page: "stocks", back: "All stocks", unit: "stocks" },
    crypto: { title: "Crypto", page: "crypto", back: "All crypto", unit: "crypto pairs" },
  };
  const RUN_STATES = {
    ok: ["is-good", "Ran normally"],
    market_closed: ["is-idle", "Market closed"],
    dry_run: ["is-idle", "Test run"],
    error: ["is-bad", "Error"],
  };
  const FILES = {
    status: "status",
    positions: "positions",
    trades: "trades",
    history: "equity_history",
    runs: "runs",
    candidates: "candidates",
    sleeves: "sleeves",
  };
  const SLEEVES = {
    rsi2_stocks: {
      name: "RSI(2) stocks",
      summary: "Buys a strong stock after a sharp dip, then sells the bounce a few days later.",
      rules: [
        "Only stocks above their 200-day average.",
        "Buys when the 2-day RSI closes under 10.",
        "Sells on a close above the 5-day average, a close below the 200-day average, or after 10 trading days.",
      ],
    },
    crypto_trend: {
      name: "Crypto trend",
      summary: "Holds the most traded coins only while their trend points up.",
      rules: [
        "Looks at the 8 most traded coins, once a day.",
        "Averages three moving-average signals, adjusted for how wild each coin is.",
        "Holds while the signal is above zero, and sells when it isn't.",
      ],
    },
    momentum_stocks: {
      name: "Momentum stocks",
      summary: "Holds the 10 strongest stocks of the past year, and re-ranks them every month.",
      rules: [
        "Ranks by the 12-month return, leaving out the latest month.",
        "Buys the top 10 over several days, one slot at a time.",
        "Moves to cash whenever SPY closes below its 200-day average.",
      ],
    },
    crypto_hold: {
      name: "Crypto hold",
      summary: "Holds Bitcoin and Ether while each one is above its long-term average.",
      rules: [
        "Checks BTC and ETH once a day.",
        "Holds each one only while it closes above its 200-day average.",
      ],
    },
  };
  const PAGES = ["overview", "strategy", "stocks", "crypto", "trades"];
  const REFRESH_MS = 60000;

  const data = {
    status: null, positions: [], trades: [], history: [], runs: [], candidates: {},
    sleeves: { mode: "", sleeves: [] },
  };
  const root = document.documentElement;
  const view = document.getElementById("view");
  const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const road = { boost: 0, pace: 1, color: "#86d3f0" };
  const tach = { leds: [], marker: null, end: 4, pos: 0, spec: null, frame: 0 };
  let chart = null;
  let activeMarket = "stock";
  let currentPage = "";
  let ignited = false;
  let reveal = null;

  // ------------------------------------------------------------------------
  // Small helpers
  // ------------------------------------------------------------------------
  const byId = (id) => document.getElementById(id);
  const color = (name) => getComputedStyle(root).getPropertyValue(name).trim();
  const hasNumber = (value) => typeof value === "number" && Number.isFinite(value);
  const tone = (value) => (value > 0 ? "good" : value < 0 ? "critical" : "");
  const shares = (value) => (hasNumber(value) ? value.toLocaleString("en-US", { maximumFractionDigits: 8 }) : "–");
  const marketOf = (symbol) => (symbol.includes("/") ? "crypto" : "stock");
  const held = (position) => `${shares(position.qty)}${marketOf(position.symbol) === "crypto" ? "" : " shares"}`;
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const plural = (number, word) => `${number} ${word}${number === 1 ? "" : "s"}`;
  const wholePercent = (value) => `${Math.round(value * 100)}%`;
  const sleeveName = (name) => (SLEEVES[name] ? SLEEVES[name].name : "Old strategy");

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

  function dateText(day) {
    return new Date(`${day}T12:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric" });
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

  // columns: [{ title, value(row), numeric?, tone?(row), link?(row), chip?(row) }]
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
        } else if (column.chip) {
          cell.append(element("span", `chip ${column.chip(row)}`, text));
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

  function candidatesOf(market) {
    return (data.candidates[market] || {}).candidates || [];
  }

  function universeOf(market) {
    return ((data.status || {}).universe || {})[market] || {};
  }

  // Today's candidates in one market, plus anything the bot holds or traded there.
  function knownSymbols(market) {
    const symbols = candidatesOf(market).map((candidate) => candidate.symbol);
    for (const row of [...data.positions, ...data.trades]) {
      if (row.symbol && marketOf(row.symbol) === market && !symbols.includes(row.symbol)) symbols.push(row.symbol);
    }
    return symbols;
  }

  // ------------------------------------------------------------------------
  // Light: the page's ambient colour comes from today's result
  // ------------------------------------------------------------------------
  function setMood() {
    const status = data.status || {};
    const change = status.day_change_pct;
    let name = "--sky";
    if ((status.kill_switch || {}).on) name = "--down";
    else if (change > 0) name = "--up";
    else if (change < 0) name = "--amber";
    root.style.setProperty("--mood", color(name));
    road.color = color(name);
    road.pace = status.market_open ? 1.5 : 0.85;   // it idles while the market sleeps
  }

  // Tunnel lights rushing past. They idle on their own, and surge when the page moves.
  function startRoad() {
    const canvas = byId("road");
    const context = canvas.getContext("2d");
    if (!context) return;
    const BANDS = [0.42, 2.72, 3.56, 5.86];      // floor edges, then ceiling edges, in radians
    let width = 0;
    let height = 0;
    let centerX = 0;
    let centerY = 0;
    let reach = 0;
    let last = 0;

    const fresh = (anywhere) => ({
      angle: BANDS[Math.floor(Math.random() * BANDS.length)] + (Math.random() - 0.5) * 0.52,
      depth: anywhere ? Math.random() : 0,
      speed: 0.55 + Math.random() * 0.95,
      weight: 0.6 + Math.random() * 1.5,
    });
    const streaks = Array.from({ length: innerWidth < 760 ? 46 : 100 }, () => fresh(true));

    function resize() {
      const scale = Math.min(devicePixelRatio || 1, 1.5);
      width = innerWidth;
      height = innerHeight;
      canvas.width = width * scale;
      canvas.height = height * scale;
      context.setTransform(scale, 0, 0, scale, 0, 0);
      centerX = width / 2;
      centerY = Math.min(height * 0.36, 330);
      reach = Math.hypot(width, height) * 0.8;
    }

    function draw(moving) {
      context.clearRect(0, 0, width, height);
      context.lineCap = "round";
      for (const streak of streaks) {
        if (moving) {
          streak.depth += (0.0034 * road.pace + road.boost * 0.024) * streak.speed * (0.28 + streak.depth);
          if (streak.depth > 1) Object.assign(streak, fresh(false));
        }
        const distance = reach * streak.depth ** 2.5;
        const tail = 10 + (46 + road.boost * 300) * streak.depth ** 2;
        const cos = Math.cos(streak.angle);
        const sin = Math.sin(streak.angle);
        context.globalAlpha = Math.min(0.72, streak.depth * 0.95);
        context.strokeStyle = sin < 0 ? "#ffb238" : road.color;
        context.lineWidth = streak.weight * (0.5 + streak.depth * 1.6);
        context.beginPath();
        context.moveTo(centerX + cos * distance, centerY + sin * distance * 0.62);
        context.lineTo(centerX + cos * (distance + tail), centerY + sin * (distance + tail) * 0.62);
        context.stroke();
      }
      context.globalAlpha = 1;
      road.boost *= 0.93;
    }

    function step(time) {
      requestAnimationFrame(step);
      if (document.hidden || time - last < 30) return;    // about 30 frames a second
      last = time;
      draw(true);
    }

    resize();
    addEventListener("resize", () => {
      resize();
      if (calm) draw(false);
    });
    if (calm) draw(false);
    else requestAnimationFrame(step);
  }

  // Shift lights along the top of the page show how far down you are.
  function startShiftLights() {
    const strip = byId("shift");
    const lights = Array.from({ length: 48 }, (_, index) => {
      const light = element("i");
      if (index >= 44) light.className = "red";
      else if (index >= 34) light.className = "hot";
      return light;
    });
    strip.append(...lights);
    const scene = document.querySelector(".scene");
    let waiting = false;
    let previous = scrollY;

    function paint() {
      waiting = false;
      const room = document.documentElement.scrollHeight - innerHeight;
      const lit = room > 0 ? Math.round((scrollY / room) * lights.length) : 0;
      lights.forEach((light, index) => light.classList.toggle("lit", index < lit));
      scene.style.opacity = String(clamp(1 - scrollY / 1400, 0.45, 1));
      road.boost = Math.min(1, road.boost + Math.abs(scrollY - previous) / 2600);
      previous = scrollY;
    }

    addEventListener("scroll", () => {
      if (waiting) return;
      waiting = true;
      requestAnimationFrame(paint);
    }, { passive: true });
    paint();
  }

  function moveGearThumb() {
    const thumb = byId("gear-thumb");
    const active = document.querySelector('.gear a[aria-current="page"]');
    if (!active) return;
    thumb.style.width = `${active.offsetWidth}px`;
    thumb.style.transform = `translateX(${active.offsetLeft}px)`;
  }

  // Panels power on as they scroll into view. Panels are lit by a soft pointer light too.
  function startPanels() {
    if (!calm) root.classList.add("will-light");
    reveal = calm || !("IntersectionObserver" in window) ? null : new IntersectionObserver((entries) => {
      let order = 0;
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        entry.target.style.setProperty("--i", order);
        order += 1;
        entry.target.classList.add("lit");
        reveal.unobserve(entry.target);
      }
    }, { threshold: 0.08 });
    if (!reveal) root.classList.remove("will-light");

    view.addEventListener("pointermove", (event) => {
      const panel = event.target.closest && event.target.closest(".panel");
      if (!panel) return;
      const box = panel.getBoundingClientRect();
      panel.style.setProperty("--mx", `${event.clientX - box.left}px`);
      panel.style.setProperty("--my", `${event.clientY - box.top}px`);
    }, { passive: true });
  }

  function watchPanels(quiet) {
    for (const panel of view.querySelectorAll(".panel, .stock-row")) {
      if (quiet || !reveal) {
        panel.style.animation = "none";
        panel.classList.add("lit");
      } else {
        reveal.observe(panel);
      }
    }
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
    const late = expected - toDate(lastUpdate).getTime() > ALLOWED_GAP;
    byId("stale-warning").hidden = !late;
    if (late) {
      byId("stale-text").textContent =
        `The last update was ${agoText(lastUpdate)}, but the bot should have run since then. It may have stopped.`;
    }
  }

  // ------------------------------------------------------------------------
  // The tachometer: LEDs light outward from zero, and the red zone is the kill switch
  // ------------------------------------------------------------------------
  function tachSpec() {
    return innerWidth >= 761
      ? { width: 1000, height: 300, x: 500, y: 690, radius: 648, span: 50, length: 30, major: 46, thick: 7.4, labels: 22, marker: 15 }
      : { width: 400, height: 150, x: 200, y: 214, radius: 180, span: 60, length: 15, major: 22, thick: 3.6, labels: 13, marker: 8 };
  }

  function svgNode(parent, tag, attributes, text) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
    if (text !== undefined) node.textContent = text;
    parent.append(node);
    return node;
  }

  function buildTach(killPct) {
    const svg = byId("tach");
    const spec = tachSpec();
    const limit = killPct * 100;
    const end = Math.ceil(limit) + 1;
    const angleOf = (value) => (value / end) * spec.span;
    const polar = (value, distance) => {
      const radians = (angleOf(value) * Math.PI) / 180;
      return [spec.x + distance * Math.sin(radians), spec.y - distance * Math.cos(radians)];
    };

    svg.replaceChildren();
    svg.setAttribute("viewBox", `0 0 ${spec.width} ${spec.height}`);
    Object.assign(tach, { leds: [], end, spec, angleOf });

    const group = svgNode(svg, "g", { class: "leds" });
    const count = end * 20;
    for (let index = 0; index <= count; index += 1) {
      const value = Math.round((-end + index * 0.1) * 10) / 10;
      const major = Number.isInteger(value);
      const length = major ? spec.major : spec.length;
      const zone = value === 0 ? "led-zero" : value <= -limit ? "led-kill" : value < 0 ? "led-neg" : "led-pos";
      const led = svgNode(group, "rect", {
        class: `led ${zone}`,
        x: spec.x - spec.thick / 2,
        y: spec.y - spec.radius,
        width: spec.thick,
        height: length,
        rx: spec.thick / 3,
        transform: `rotate(${angleOf(value)} ${spec.x} ${spec.y})`,
      });
      led.style.setProperty("--n", index);
      tach.leds.push({ node: led, value, lit: false });
    }
    for (let value = 1 - end; value < end; value += 1) {
      const [x, y] = polar(value, spec.radius + spec.labels);
      const text = value > 0 ? `+${value}` : value < 0 ? `−${-value}` : "0";
      svgNode(svg, "text", {
        class: value <= -limit ? "tach-number is-kill" : "tach-number",
        x,
        y,
        style: `font-size:${spec.width > 500 ? 19 : 12.5}px`,
      }, text);
    }
    tach.marker = svgNode(svg, "path", {
      class: "tach-marker",
      d: `M ${spec.x - spec.marker * 0.7} ${spec.y - spec.radius + spec.major + spec.marker * 1.4} `
        + `L ${spec.x + spec.marker * 0.7} ${spec.y - spec.radius + spec.major + spec.marker * 1.4} `
        + `L ${spec.x} ${spec.y - spec.radius + spec.major + spec.marker * 0.3} Z`,
    });
    paintTach(tach.pos);
  }

  function paintTach(position) {
    tach.pos = position;
    for (const led of tach.leds) {
      const lit = position >= 0 ? led.value > 0 && led.value <= position + 0.04
        : led.value < 0 && led.value >= position - 0.04;
      if (lit !== led.lit) {
        led.lit = lit;
        led.node.classList.toggle("lit", lit);
      }
    }
    const { spec } = tach;
    tach.marker.setAttribute("transform", `rotate(${tach.angleOf(position)} ${spec.x} ${spec.y})`);
  }

  const EASE = {
    out: (t) => 1 - (1 - t) ** 3,
    both: (t) => (t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2),
    settle: (t) => 1 + 2.2 * (t - 1) ** 3 + 1.2 * (t - 1) ** 2,
  };

  // A real car sweeps its needle at ignition. This one swings right, then left, then settles.
  function sweepTach(target, full) {
    cancelAnimationFrame(tach.frame);
    const end = tach.end;
    const goal = clamp(target, -end, end);
    if (calm) return paintTach(goal);
    const legs = full
      ? [[end, 620, EASE.out], [-end, 860, EASE.both], [goal, 900, EASE.settle]]
      : [[goal, 760, EASE.settle]];
    let leg = 0;
    let from = tach.pos;
    let started = performance.now();

    const step = (now) => {
      const [to, duration, ease] = legs[leg];
      const progress = clamp((now - started) / duration, 0, 1);
      const before = tach.pos;
      paintTach(from + (to - from) * ease(progress));
      road.boost = Math.min(1, road.boost + Math.abs(tach.pos - before) * 0.16);
      if (progress < 1) {
        tach.frame = requestAnimationFrame(step);
      } else if (leg + 1 < legs.length) {
        leg += 1;
        from = to;
        started = now;
        tach.frame = requestAnimationFrame(step);
      }
    };
    tach.frame = requestAnimationFrame(step);
  }

  // Odometer drums. Each digit has its own strip, and the low digits spin the most.
  function rollOdometer(holder, text, first) {
    holder.style.setProperty("--chars", text.length);
    holder.setAttribute("aria-label", text);
    if (holder.dataset.text === undefined || holder.dataset.text.length !== text.length) {
      holder.replaceChildren(...[...text].map((character) => {
        if (!/\d/.test(character)) return element("span", "odo-mark", character);
        const cell = element("span", "odo-digit");
        const strip = element("span", "odo-strip");
        for (let index = 0; index < 40; index += 1) strip.append(element("span", "", String(index % 10)));
        cell.append(strip);
        return cell;
      }));
    }
    holder.dataset.text = text;
    const cells = [...holder.children];
    cells.forEach((cell, index) => {
      if (!cell.classList.contains("odo-digit")) return;
      const strip = cell.firstChild;
      const digit = Number(text[index]);
      const fromRight = cells.length - index;
      const turns = first && !calm ? Math.min(3, Math.ceil(fromRight / 3)) : 0;
      const stop = turns * 10 + digit;
      const seconds = first ? 1.5 + (cells.length - fromRight) * 0.06 : 0.8;
      strip.style.transition = calm ? "none" : `transform ${seconds}s cubic-bezier(.16,.84,.26,1) ${first ? 0.25 : 0}s`;
      requestAnimationFrame(() => requestAnimationFrame(() => {
        strip.style.transform = `translateY(calc(-${stop} * 1.08em))`;
      }));
    });
  }

  function countUp(id, value, className) {
    const node = byId(id);
    if (!node) return;
    const finish = () => setText(id, signedMoney(id, value), className);
    if (calm || !hasNumber(value) || ignited) return finish();
    const started = performance.now();
    const step = (now) => {
      const progress = clamp((now - started - 500) / 1100, 0, 1);
      if (!byId(id)) return;
      node.textContent = money(value * EASE.out(progress));
      if (progress < 1) requestAnimationFrame(step);
      else finish();
    };
    requestAnimationFrame(step);
  }

  const signedMoney = (id, value) => (id === "open-pl" ? signed(money(value), value) : money(value));

  // ------------------------------------------------------------------------
  // Overview page
  // ------------------------------------------------------------------------
  function setLamp(id, state) {
    const lamp = byId(id);
    if (lamp) lamp.className = `lamp ${state}`;
  }

  function ignite() {
    const cockpit = byId("cockpit");
    const lamps = [...cockpit.querySelectorAll(".lamp")];
    lamps.forEach((lamp, index) => lamp.style.setProperty("--lamp-delay", `${index * 0.14}s`));
    cockpit.classList.add("igniting");
    setTimeout(() => cockpit.classList.remove("igniting"), 1050);
  }

  function renderSummary() {
    const status = data.status;
    const cockpit = byId("cockpit");
    const first = !ignited;
    buildTach((status && status.limits ? status.limits.kill_switch_pct : 0.03));
    if (!status) {
      setText("updated", "No data yet. This fills in after the bot's next run.");
      setText("day-change", "");
      rollOdometer(byId("equity"), "$0.00", first);
      return;
    }
    const run = status.last_run;
    const limits = status.limits || { kill_switch_pct: 0.03, max_trades_per_day: 3 };
    setText("updated", `Last update ${timeText(run.time_utc)} (${agoText(run.time_utc)})`);
    rollOdometer(byId("equity"), money(status.equity), first);
    const change = status.day_change_pct;
    setText("day-change", hasNumber(change) ? `${percent(change)} today` : "", tone(change));
    byId("tach").setAttribute("aria-label", `Today's change is ${hasNumber(change) ? percent(change) : "not known"}. `
      + `The kill switch trips at −${limits.kill_switch_pct * 100}%, where the dial turns red.`);

    countUp("cash", status.cash);
    countUp("invested", status.invested);
    countUp("open-pl", status.open_pl, tone(status.open_pl));

    const allowed = limits.max_trades_per_day;
    const used = status.trades_today ?? 0;
    setText("buy-count", `${status.trades_today ?? "–"} of ${allowed}`);
    byId("buy-pips").replaceChildren(...Array.from({ length: allowed }, (_, index) => (
      element("i", index < used ? "lit" : "")
    )));

    const [runState, runLabel] = RUN_STATES[run.status] || ["is-idle", run.status];
    setLamp("lamp-run", runState);
    setText("run-status", runLabel);
    setText("run-message", run.status === "error" ? run.message : "");
    const market = status.market_open;
    setLamp("lamp-market", market ? "is-good" : "is-idle");
    setText("market", market === true ? "Open" : market === false ? "Closed" : "Unknown");
    const kill = status.kill_switch;
    setLamp("lamp-kill", kill.on ? "is-bad" : "is-good");
    setText("kill-switch", kill.on ? "On" : "Off");
    setText("kill-reason", kill.on ? kill.reason || "No new buys today."
      : `Trips at −${limits.kill_switch_pct * 100}%, the red zone on the dial.`);
    setLamp("lamp-ai", status.ai_analyst_on ? "is-good" : "is-idle");
    setText("ai-analyst", status.ai_analyst_on ? "On" : "Off");

    if (first) ignite();
    sweepTach(hasNumber(change) ? change * 100 : 0, first);
    cockpit.dataset.ready = "true";
    ignited = true;
  }

  function rightNowText() {
    const status = data.status;
    if (!status) return "Waiting for the bot's first run.";
    const count = data.positions.length;
    const market = status.market_open ? "The US market is open" : "The US market is closed";
    const holding = count ? `the bot holds ${plural(count, "position")} worth ${money(status.invested)}`
      : "the bot holds nothing right now";
    let next = status.market_open ? "It checks every stop on each run." : "It looks for new buys on the first run after the next open.";
    if ((status.kill_switch || {}).on) next = "The kill switch is on, so there are no new buys today.";
    return `${market} and ${holding}. ${next}`;
  }

  function renderTower() {
    const tower = byId("tower");
    const rows = [...data.positions].sort((a, b) => (b.unrealized_pl_pct ?? 0) - (a.unrealized_pl_pct ?? 0));
    if (!rows.length) {
      const empty = element("div", "tower-empty");
      const pulse = element("div", "idle-pulse");
      for (let index = 0; index < 7; index += 1) {
        const bar = element("i");
        bar.style.setProperty("--i", index);
        pulse.append(bar);
      }
      empty.append(pulse, element("strong", "", "Idling"),
        element("span", "note", "No open positions. The bot is watching for its next signal."));
      tower.replaceChildren(empty);
      return;
    }
    const widest = Math.max(...rows.map((row) => Math.abs(row.unrealized_pl_pct ?? 0)), 0.0001);
    tower.replaceChildren(...rows.map((position, index) => {
      const row = element("a", "tower-row");
      row.href = stockLink(position.symbol);
      row.style.setProperty("--i", index);
      const gain = position.unrealized_pl_pct ?? 0;
      row.style.setProperty("--bar", gain >= 0 ? "var(--up)" : "var(--down)");
      const name = element("div", "tower-symbol", position.symbol);
      name.append(element("span", "tower-sub", `${sleeveName(position.sleeve)}, stop ${money(position.stop_price)}`));
      const result = element("div", `tower-result ${tone(position.unrealized_pl)}`, percent(gain));
      result.append(element("span", "", signed(money(position.unrealized_pl), position.unrealized_pl)));
      const bar = element("span", "tower-bar");
      bar.style.width = `${(Math.abs(gain) / widest) * 100}%`;
      row.append(element("div", "tower-rank", String(index + 1)), name, result, bar);
      return row;
    }));
  }

  function renderRuns() {
    const strip = byId("run-strip");
    const runs = data.runs.slice(-40);
    if (!runs.length) {
      setText("run-summary", "Each run the bot makes appears here.");
      return;
    }
    strip.replaceChildren(...runs.map((run, index) => {
      const classes = [`is-${run.status}`, run.orders ? "has-orders" : ""];
      const tick = element("li", classes.filter(Boolean).join(" "));
      const [, label] = RUN_STATES[run.status] || ["", run.status];
      tick.title = `${timeText(run.time_utc)}: ${label}${run.orders ? `, ${plural(run.orders, "order")}` : ""}`;
      tick.style.setProperty("--i", index);
      return tick;
    }));
    const errors = runs.filter((run) => run.status === "error").length;
    const orders = runs.reduce((total, run) => total + run.orders, 0);
    setText("run-summary", `Last ${plural(runs.length, "run")}: ${plural(errors, "error")}, ${plural(orders, "order")} placed. Tall green beats placed orders and red ones had errors.`);
  }

  function drawEquityChart(days) {
    if (chart) chart.destroy();
    chart = null;
    const note = byId("chart-note");
    const head = byId("chart-head");
    note.hidden = true;
    head.hidden = true;
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
    const lineColor = road.color;
    const values = points.map((point) => point.equity);
    const low = Math.min(...values);
    const high = Math.max(...values);
    const flat = high - low < Math.max(high * 0.0005, 1);    // a flat line sits mid-chart, not on the floor
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
        ctx.strokeStyle = color("--ink-3");
        ctx.stroke();
        ctx.restore();
      },
    };
    const lineGlow = {
      id: "lineGlow",
      beforeDatasetDraw(target) {
        target.ctx.save();
        target.ctx.shadowColor = lineColor;
        target.ctx.shadowBlur = 14;
      },
      afterDatasetDraw(target) {
        target.ctx.restore();
      },
    };
    const placeHead = {
      id: "placeHead",
      afterRender(target) {
        const point = target.getDatasetMeta(0).data[last];
        if (!point || points.length < 2) return;
        head.style.left = `${point.x}px`;
        head.style.top = `${point.y}px`;
        head.hidden = false;
      },
    };
    chart = new window.Chart(byId("equity-chart"), {
      type: "line",
      plugins: [crosshair, lineGlow, placeHead],
      data: {
        labels: points.map((point) => timeText(point.time_utc)),
        datasets: [{
          data: values,
          borderColor: lineColor,
          borderWidth: 2.5,
          borderCapStyle: "round",
          borderJoinStyle: "round",
          tension: 0.18,
          pointRadius: 0,
          pointHoverRadius: 5,
          pointBackgroundColor: lineColor,
          fill: "start",
          backgroundColor(context) {
            const { ctx, chartArea } = context.chart;
            if (!chartArea) return "transparent";
            const fade = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
            fade.addColorStop(0, `${lineColor}44`);
            fade.addColorStop(1, `${lineColor}00`);
            return fade;
          },
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        layout: { padding: { right: 10, top: 8 } },
        interaction: { mode: "index", intersect: false },
        scales: {
          x: {
            grid: { display: false },
            border: { color: "rgba(134, 211, 240, .16)" },
            ticks: {
              color: color("--ink-3"),
              maxRotation: 0,
              autoSkipPadding: 24,
              maxTicksLimit: 6,
              callback: (value) => toDate(points[value].time_utc).toLocaleDateString(undefined, {
                month: "short", day: "numeric",
              }),
            },
          },
          y: {
            suggestedMin: flat ? low * 0.995 : undefined,
            suggestedMax: flat ? high * 1.005 : undefined,
            grid: { color: "rgba(134, 211, 240, .1)" },
            border: { display: false },
            ticks: {
              color: color("--ink-3"),
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
            backgroundColor: color("--raised"),
            borderColor: "rgba(134, 211, 240, .3)",
            borderWidth: 1,
            titleColor: color("--ink-2"),
            bodyColor: color("--ink"),
            padding: 10,
            cornerRadius: 8,
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

  // Where the money sits. Each part slides out to its size once the engine has started.
  function renderBreakdown() {
    const status = data.status;
    if (!status) return;
    const parts = [
      { name: "Stocks", value: status.stock_value, className: "is-stock" },
      { name: "Crypto", value: status.crypto_value, className: "is-crypto" },
      { name: "Cash", value: status.cash, className: "is-cash" },
    ].map((part) => ({ ...part, value: hasNumber(part.value) ? Math.max(0, part.value) : 0 }));
    const total = parts.reduce((sum, part) => sum + part.value, 0);
    const share = (part) => (total ? (part.value / total) * 100 : 0);

    const bar = byId("breakdown");
    const blocks = parts.filter((part) => part.value > 0).map((part) => {
      const block = element("div", `breakdown-part ${part.className}`);
      block.title = `${part.name}: ${money(part.value)}`;
      block.dataset.width = `calc(${share(part)}% - 2px)`;
      return block;
    });
    bar.replaceChildren(...blocks);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      for (const block of blocks) block.style.width = block.dataset.width;
    }));
    bar.setAttribute("aria-label", parts.map((part) => `${part.name} ${money(part.value)}, ${Math.round(share(part))}%`).join("; "));

    byId("breakdown-legend").replaceChildren(...parts.map((part) => {
      const item = element("li");
      item.append(element("span", `swatch ${part.className}`), element("span", "", part.name),
        element("span", "", `${money(part.value)} (${Math.round(share(part))}%)`));
      return item;
    }));
  }

  // The whole account as one bar, each sleeve sized by its share and filled by what it uses.
  function renderBank() {
    const holder = byId("bank");
    const rows = data.sleeves.sleeves;
    if (!holder) return;
    if (!rows.length) return show(holder, "Sleeve numbers appear after the next run.");
    const equity = (data.status || {}).equity || 0;
    const bank = element("div", "bank");
    bank.setAttribute("role", "img");
    bank.setAttribute("aria-label", rows.map((row) => (
      `${sleeveName(row.name)}: ${wholePercent(row.allocation)} of the account, ${row.enabled ? "on" : "off"}`
    )).join("; "));
    const fills = [];
    for (const row of rows) {
      const segment = element("div", `bank-seg ${row.enabled ? "is-on" : ""}`);
      segment.style.flex = `${row.allocation} 1 0`;
      segment.title = `${sleeveName(row.name)}: ${wholePercent(row.allocation)}, ${row.enabled ? "on" : "off"}`;
      const fill = element("span", "bank-fill");
      const used = equity && row.allocation ? clamp(row.value / (equity * row.allocation), 0, 1) : 0;
      fill.dataset.width = `${used * 100}%`;
      fills.push(fill);
      const narrow = innerWidth < 761;
      const label = element("span", "bank-label");
      if (row.allocation >= (narrow ? 0.3 : 0.14)) {
        label.textContent = sleeveName(row.name);
        if (row.allocation >= (narrow ? 0.5 : 0.3)) label.append(element("span", "", wholePercent(row.allocation)));
      } else {
        label.textContent = wholePercent(row.allocation);
      }
      if (used > 0) segment.append(fill);
      segment.append(label);
      bank.append(segment);
    }
    holder.replaceChildren(bank);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      for (const fill of fills) fill.style.width = fill.dataset.width;
    }));
  }

  function renderSleeves() {
    const rows = data.sleeves.sleeves;
    const count = rows.filter((row) => row.enabled).length;
    const note = data.sleeves.mode === "crossover" ? "The old crossover strategy is running."
      : rows.length ? `${plural(count, "sleeve")} on. The rest of the bar stays in cash, and a filled bar means the sleeve is fully invested.`
        : "";
    setText("sleeves-note", note);
    renderBank();
    show(byId("sleeves"), rows.length ? buildTable([
      { title: "Sleeve", value: (s) => sleeveName(s.name) },
      { title: "Term", value: (s) => (s.horizon === "SHORT" ? "Short" : "Long") },
      { title: "Status", value: (s) => (s.enabled ? "On" : "Off"), chip: (s) => (s.enabled ? "chip-on" : "") },
      { title: "Share", value: (s) => wholePercent(s.allocation), numeric: true },
      { title: "Positions", value: (s) => s.positions, numeric: true },
      { title: "Value", value: (s) => money(s.value), numeric: true },
      { title: "Open profit or loss", value: (s) => signed(money(s.open_pl), s.open_pl), numeric: true, tone: (s) => tone(s.open_pl) },
      { title: "Closed profit or loss", value: (s) => signed(money(s.closed_pl), s.closed_pl), numeric: true, tone: (s) => tone(s.closed_pl) },
    ], rows) : "");
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
    const universe = universeOf(activeMarket);
    const scanning = universe.size
      ? `Scanning ${universe.size} ${MARKETS[activeMarket].unit}${universe.refreshed ? `, list refreshed ${dateText(universe.refreshed)}` : ""}. `
      : "";
    let note = `${scanning}New stock buys today: ${status.trades_today ?? "–"} of ${limit}.`;
    if (activeMarket === "crypto") {
      note = status.crypto_enabled
        ? `${scanning}New crypto buys today: ${status.crypto_trades_today ?? "–"} of ${limit}, counted by UTC day.`
        : `${scanning}Crypto trading is off. Trades appear here once it's turned on.`;
    }
    setText("market-note", note);

    const plan = data.candidates[activeMarket] || {};
    const candidates = candidatesOf(activeMarket);
    setText("candidates-note", plan.day
      ? `${plan.found} found in the scan of ${dateText(plan.day)}. The bot keeps the top ${candidates.length}.`
      : "The first scan of the day fills this in.");
    show(byId("candidates"), candidates.length
      ? buildTable(candidateColumns(activeMarket), candidates)
      : "No candidates today.");

    const trades = data.trades.filter(isMarket);
    show(byId("latest-trades"), trades.length
      ? tradeTable(trades.slice(0, 10), true, activeMarket)
      : `No ${name} trades yet.`);
  }

  function outcomeText(candidate) {
    if (candidate.note) return candidate.note;
    const words = { waiting: "Waiting", ordered: "Order placed", vetoed: "Vetoed by the AI" };
    const text = words[candidate.status] || candidate.status;
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function candidateColumns(market) {
    return [
      { title: "Rank", value: (c) => c.rank, numeric: true },
      { title: market === "crypto" ? "Pair" : "Stock", value: (c) => c.symbol, link: (c) => stockLink(c.symbol) },
      { title: "Action", value: (c) => c.action, chip: (c) => (c.action === "BUY" ? "chip-up" : "chip-down") },
      { title: "Sleeve", value: (c) => sleeveName(c.sleeve) },
      { title: "Why", value: (c) => c.reason },
      { title: "AI decision", value: aiText },
      { title: "Result", value: outcomeText },
    ];
  }

  function renderTabs() {
    renderMarketTab();
    const tabs = [...document.querySelectorAll('[role="tab"]')];
    const panel = byId("market-panel");
    const choose = (tab) => {
      root.style.setProperty("--dir", tab.dataset.market === "crypto" ? "1" : "-1");
      activeMarket = tab.dataset.market;
      renderMarketTab();
      panel.style.animation = "none";
      void panel.offsetWidth;                // restarts the slide
      panel.style.animation = "";
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

  function aiText(trade) {
    if (!trade.ai_decision) return "Not asked";
    if (trade.ai_decision === "OFF") return "Off";
    return trade.ai_reason ? `${trade.ai_decision}: ${trade.ai_reason}` : trade.ai_decision;
  }

  function actionText(trade) {
    const side = trade.side.toUpperCase();
    return trade.event === "AI_DECISION" ? `Vetoed ${side}` : side;
  }

  function actionChip(trade) {
    if (trade.event === "AI_DECISION") return "chip-warn";
    return trade.side === "buy" ? "chip-up" : "chip-down";
  }

  function tradeTable(trades, withSymbol, market) {
    const columns = [
      { title: "Time", value: (t) => timeText(t.time_utc) },
      { title: "Action", value: actionText, chip: actionChip },
      { title: market === "stock" ? "Shares" : "Amount", value: (t) => shares(t.qty), numeric: true },
      { title: "Price", value: (t) => money(t.price), numeric: true },
      { title: "Status", value: (t) => t.order_status || "–" },
      { title: "Sleeve", value: (t) => sleeveName(t.sleeve) },
      { title: "Reason", value: (t) => t.reason || "–" },
      { title: "AI decision", value: aiText },
    ];
    const symbolTitle = market === "crypto" ? "Pair" : market === "stock" ? "Stock" : "Symbol";
    if (withSymbol) columns.splice(2, 0, { title: symbolTitle, value: (t) => t.symbol, link: (t) => stockLink(t.symbol) });
    return buildTable(columns, trades);
  }

  function showOverview() {
    renderSummary();
    setText("right-now", rightNowText());
    renderEquity();
    renderTower();
    renderBreakdown();
    renderSleeves();
    renderTabs();
    renderRuns();
  }

  // ------------------------------------------------------------------------
  // Strategy page
  // ------------------------------------------------------------------------
  function sleeveCard(row) {
    const about = SLEEVES[row.name] || { name: row.name, summary: "", rules: [] };
    const card = element("section", `panel sleeve-card ${row.enabled ? "is-on" : ""}`);
    const head = element("div", "sleeve-card-head");
    const lamp = element("span", `switch ${row.enabled ? "is-on" : ""}`);
    lamp.append(element("i"), document.createTextNode(row.enabled ? "On" : "Off"));
    head.append(element("h2", "", about.name), lamp);
    const rules = element("ul", "sleeve-rules");
    rules.append(...about.rules.map((rule) => element("li", "", rule)));
    const figures = element("dl", "sleeve-figures");
    const figure = (term, detail, className) => {
      const group = element("div");
      group.append(element("dt", "", term), element("dd", className || "", detail));
      figures.append(group);
    };
    figure(row.horizon === "SHORT" ? "Short-term share" : "Long-term share", wholePercent(row.allocation));
    figure("Positions", String(row.positions));
    figure("Open profit or loss", signed(money(row.open_pl), row.open_pl), tone(row.open_pl));
    figure("Closed profit or loss", signed(money(row.closed_pl), row.closed_pl), tone(row.closed_pl));
    card.append(head, element("p", "sleeve-summary", about.summary), rules, figures);
    return card;
  }

  function showStrategy() {
    const rows = data.sleeves.sleeves;
    const on = rows.filter((row) => row.enabled).map((row) => sleeveName(row.name));
    setText("strategy-intro", data.sleeves.mode === "crossover" ? "The old crossover strategy is running. The sleeves below are off."
      : on.length ? `${on.join(" and ")} ${on.length === 1 ? "is" : "are"} trading right now. Each sleeve has its own rules, its own share of the account, and an on or off switch.`
        : "Each sleeve has its own rules, its own share of the account, and an on or off switch.");
    renderBank();
    show(byId("sleeve-cards"), rows.length ? (() => {
      const cards = document.createDocumentFragment();
      cards.append(...rows.map(sleeveCard));
      return cards;
    })() : "Sleeve numbers appear after the next run.");
    const limits = (data.status || {}).limits || {};
    const percentOf = (value, fallback) => `${Math.round((value ?? fallback) * 100)}%`;
    byId("house-rules").replaceChildren(...[
      `No position is bigger than ${percentOf(limits.max_position_pct, 0.05)} of the account.`,
      `Every position has a stop-loss ${percentOf(limits.stop_loss_pct, 0.07)} below its buy price.`,
      `If the account falls ${percentOf(limits.kill_switch_pct, 0.03)} in a day, the kill switch stops new buys.`,
      `At most ${limits.max_trades_per_day ?? 3} new buys a day in each market, short-term sleeves first. Sells always go through.`,
      "A symbol belongs to one sleeve at a time, and never trades twice in a day.",
      "Long only, with no shorting and no borrowed money.",
    ].map((rule) => element("li", "", rule)));
  }

  // ------------------------------------------------------------------------
  // Stocks pages
  // ------------------------------------------------------------------------
  function signalText(stock) {
    if (stock.signal === "BUY") return "Buy signal";
    if (stock.signal === "SELL") return "Sell signal";
    if (stock.signal === "HOLD") return "No signal";
    return "–";
  }

  function viewOf(symbol) {
    const candidate = candidatesOf(marketOf(symbol)).find((row) => row.symbol === symbol);
    return candidate ? { ...candidate, signal: candidate.action }
      : { symbol, trend: "", signal: "HOLD", signal_date: "", name: "" };
  }

  function sleeveOf(symbol) {
    const position = data.positions.find((row) => row.symbol === symbol);
    if (position) return sleeveName(position.sleeve);
    const candidate = viewOf(symbol);
    return candidate.action ? sleeveName(candidate.sleeve) : "–";
  }

  function stockRow(symbol) {
    const stock = viewOf(symbol);
    const position = data.positions.find((p) => p.symbol === symbol);
    const row = element("a", "stock-row");
    row.href = stockLink(symbol);

    const title = element("div", "stock-title");
    title.append(element("div", "stock-symbol", symbol), element("div", "stock-name", STOCK_NAMES[symbol] || stock.name || ""));

    const sleeve = element("div", "stock-cell", sleeveOf(symbol));
    const signal = element("div", "stock-cell", signalText(stock));
    if (stock.signal_date) {
      const day = new Date(`${stock.signal_date}T12:00:00Z`);
      const label = day.toLocaleDateString(undefined, { month: "short", day: "numeric" });
      signal.append(element("span", "", `from ${label}`));
    }

    const holding = element("div", "stock-cell", position ? `${held(position)} held` : "Not held");
    if (position) {
      holding.append(element("span", tone(position.unrealized_pl),
        `${signed(money(position.unrealized_pl), position.unrealized_pl)} (${percent(position.unrealized_pl_pct)})`));
    }
    row.append(title, sleeve, signal, holding);
    return row;
  }

  function showMarket(market) {
    setText("market-title", MARKETS[market].title);
    const size = universeOf(market).size;
    const scan = size ? `The bot scans ${size} ${MARKETS[market].unit}. ` : "";
    const off = market === "crypto" && data.status && !data.status.crypto_enabled ? " Crypto trading is off for now." : "";
    setText("market-intro", `${scan}Here are your holdings and today's candidates. Open one to see its price history and your trades.${off}`);
    const symbols = knownSymbols(market);
    const list = byId("stock-list");
    if (!symbols.length) return show(list, "No holdings or candidates today.");
    list.replaceChildren(...symbols.map(stockRow));
  }

  function fact(list, term, detail, className) {
    const group = element("div", "pod");
    group.append(element("dt", "", term), element("dd", className || "", detail));
    list.append(group);
  }

  function mountPriceChart(holder, symbol) {
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
      theme: "dark",
      backgroundColor: "#0a2630",
      gridColor: "rgba(134, 211, 240, 0.08)",
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
      byId("stock-facts").hidden = true;
      return;
    }
    document.title = `${symbol}, Tach Trading Bot`;
    setText("stock-symbol", symbol);
    const stock = viewOf(symbol);
    setText("stock-name", STOCK_NAMES[symbol] || stock.name || "");

    const position = data.positions.find((p) => p.symbol === symbol);
    const facts = byId("stock-facts");
    fact(facts, "Sleeve", sleeveOf(symbol));
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
  function open(templateId, page, quiet) {
    if (chart) chart.destroy();
    chart = null;
    document.title = "Tach Trading Bot";
    for (const credit of document.querySelectorAll(".chart-credit")) credit.remove();
    view.replaceChildren(document.importNode(byId(templateId).content, true));
    for (const link of document.querySelectorAll(".gear a")) {
      if (link.dataset.page === page) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    moveGearThumb();
    if (!quiet) scrollTo({ top: 0, behavior: "instant" });
  }

  function pathParts() {
    return location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  }

  function pageOf(parts) {
    return PAGES.includes(parts[0]) ? parts[0] : "overview";
  }

  function draw(parts, quiet) {
    if ((parts[0] === "stocks" || parts[0] === "crypto") && parts[1]) {
      let symbol = "";
      try {
        symbol = decodeURIComponent(parts[1]).toUpperCase();
      } catch {
        location.hash = `#/${parts[0]}`;
        return;
      }
      open("stock-page", parts[0], quiet);
      showStock(parts[0] === "crypto" ? symbol.replace("-", "/") : symbol);
    } else if (parts[0] === "stocks") {
      open("stocks-page", "stocks", quiet);
      showMarket("stock");
    } else if (parts[0] === "crypto") {
      open("stocks-page", "crypto", quiet);
      showMarket("crypto");
    } else if (parts[0] === "trades") {
      open("trades-page", "trades", quiet);
      showTrades();
    } else if (parts[0] === "strategy") {
      open("strategy-page", "strategy", quiet);
      showStrategy();
    } else {
      open("overview-page", "overview", quiet);
      showOverview();
    }
    watchPanels(quiet);
  }

  function route(moved, quiet) {
    const parts = pathParts();
    const page = pageOf(parts);
    const direction = PAGES.indexOf(page) >= PAGES.indexOf(currentPage) ? 1 : -1;
    currentPage = page;
    const render = () => {
      draw(parts, quiet);
      if (moved) view.focus({ preventScroll: true });
    };
    if (moved && !calm && document.startViewTransition) {
      root.style.setProperty("--dir", String(direction));
      road.boost = 1;
      document.startViewTransition(render);
    } else {
      render();
    }
  }

  async function loadAll() {
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
    setMood();
    if (data.status) showStaleWarning(data.status.last_run.time_utc);
  }

  // A page left open keeps itself up to date: the odometer rolls to any new value.
  async function refresh() {
    if (document.hidden) return;
    const seen = data.status ? data.status.last_run.time_utc : "";
    try {
      const response = await fetch(`data/status.json?t=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) return;
      const status = await response.json();
      if (status.last_run.time_utc === seen) {
        showStaleWarning(seen);
        setText("updated", `Last update ${timeText(seen)} (${agoText(seen)})`);
        return;
      }
    } catch {
      return;
    }
    await loadAll();
    road.boost = 0.7;
    const page = currentPage;
    if (page === "overview" && byId("equity")) {
      const change = data.status.day_change_pct;
      rollOdometer(byId("equity"), money(data.status.equity), false);
      setText("day-change", hasNumber(change) ? `${percent(change)} today` : "", tone(change));
      setText("updated", `Last update ${timeText(data.status.last_run.time_utc)} (just now)`);
      setText("right-now", rightNowText());
      sweepTach(hasNumber(change) ? change * 100 : 0, false);
      renderTower();
      renderBreakdown();
      renderSleeves();
      renderMarketTab();
      renderRuns();
    } else {
      route(false, true);
    }
  }

  async function start() {
    await loadAll();
    startRoad();
    startPanels();
    route(false, false);
    startShiftLights();
    addEventListener("hashchange", () => route(true, false));
    addEventListener("resize", moveGearThumb);
    const wide = matchMedia("(min-width: 761px)");
    wide.addEventListener("change", () => {
      if (!byId("tach")) return;
      const change = data.status ? data.status.day_change_pct : 0;
      buildTach(data.status && data.status.limits ? data.status.limits.kill_switch_pct : 0.03);
      paintTach(clamp((hasNumber(change) ? change : 0) * 100, -tach.end, tach.end));
    });
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(moveGearThumb);
    setInterval(refresh, REFRESH_MS);
  }

  start();
})();
