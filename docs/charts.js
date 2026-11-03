// The dashboard's charts. app.js decides what to show, and this file draws it. Lightweight Charts
// draws prices and equity, and Apache ECharts draws heatmaps and histograms. Both load from the CDN
// only when a chart comes near the screen, with a pinned version and a checked hash, so a page
// that doesn't need them stays light. Lightweight Charts keeps its TradingView credit.
"use strict";

window.TachCharts = (function () {
  const LIBRARIES = {
    lightweight: {
      global: "LightweightCharts",
      url: "https://cdn.jsdelivr.net/npm/lightweight-charts@5.2.1/dist/lightweight-charts.standalone.production.js",
      hash: "sha384-KkYqTZlM13Zya6fVUF3IGCBOQ1ehFeJVVgxaVvD/0MebineF9EI8Jkd3NnWNChqN",
    },
    echarts: {
      global: "echarts",
      url: "https://cdn.jsdelivr.net/npm/echarts@6.1.0/dist/echarts.min.js",
      hash: "sha384-C2iskrW/uPW46KzOjrvJIQo4YkV8lkD+QS0CrDN18IIPIpT/g2USu8bTP3nvmIAD",
    },
  };
  const loading = {};
  let live = [];              // everything drawn on the current page, so it can be cleared
  let watcher = null;
  const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;

  function load(name) {
    const library = LIBRARIES[name];
    if (window[library.global]) return Promise.resolve(window[library.global]);
    loading[name] = loading[name] || new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = library.url;
      script.integrity = library.hash;
      script.crossOrigin = "anonymous";
      script.onload = () => resolve(window[library.global]);
      script.onerror = () => reject(new Error(`${name} didn't load`));
      document.head.append(script);
    });
    return loading[name];
  }

  function palette() {
    const style = getComputedStyle(document.documentElement);
    const read = (name) => style.getPropertyValue(name).trim();
    return {
      ink: read("--ink"), ink2: read("--ink-2"), ink3: read("--ink-3"), up: read("--up"), down: read("--down"), amber: read("--amber"),
      sky: read("--sky"), stocks: read("--stocks"), line: read("--grid"), raised: read("--raised"), series: read("--series-4"),
      neutral: read("--neutral"),
    };
  }

  function fail(holder, text) {
    holder.replaceChildren();
    const note = document.createElement("p");
    note.className = "chart-empty";
    note.textContent = text;
    holder.append(note);
  }

  // Draw when the chart comes near the screen, so charts below the fold cost nothing until then.
  function whenNear(holder, draw) {
    const go = () => Promise.resolve().then(draw).catch(() => fail(holder, "The chart library didn't load. Check your connection and refresh."));
    if (!("IntersectionObserver" in window)) return go();
    if (!watcher) {
      watcher = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          watcher.unobserve(entry.target);
          entry.target.drawChart();
        }
      }, { rootMargin: "300px" });
    }
    holder.drawChart = go;
    watcher.observe(holder);
    return undefined;
  }

  function clear() {
    for (const item of live) item.remove();
    live = [];
    if (watcher) watcher.disconnect();
  }

  // ---------------------------------------------------------------------------
  // Lightweight Charts
  // ---------------------------------------------------------------------------
  function priceChart(lib, holder, extra = {}) {
    const c = palette();
    const chart = lib.createChart(holder, {
      autoSize: true,
      layout: {
        background: { type: "solid", color: "transparent" }, textColor: c.ink2, fontFamily: "Saira, system-ui, sans-serif",
        attributionLogo: true, panes: { separatorColor: c.line },
      },
      grid: { vertLines: { color: c.line }, horzLines: { color: c.line } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, rightOffset: 2, fixLeftEdge: true, fixRightEdge: true },
      crosshair: { mode: 0 },
      handleScroll: { vertTouchDrag: false },
      handleScale: { axisPressedMouseMove: false },
      ...extra,
    });
    live.push({ remove: () => chart.remove() });
    return chart;
  }

  const lineOptions = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };

  // A line under the chart that says what a tap or hover landed on.
  function readout(chart, target, describe) {
    if (!target) return;
    chart.subscribeCrosshairMove((point) => {
      target.textContent = point && point.time !== undefined ? describe(point) : target.dataset.resting || "";
    });
  }

  function newYorkShift(ms) {
    const text = new Date(ms).toLocaleString("en-US", { timeZone: "America/New_York" });
    return Math.round((new Date(`${text} UTC`).getTime() - ms) / 60000) * 60;
  }

  // Today's candles, with EMA(21) as a glowing band: green while EMA(9) is above it, red otherwise.
  async function liveChart(holder, status, legend) {
    const lib = await load("lightweight");
    holder.replaceChildren();
    const c = palette();
    const chart = priceChart(lib, holder, {
      timeScale: { borderVisible: false, rightOffset: 2, timeVisible: true, secondsVisible: false },
    });
    const bars = status.bars || [];
    const shift = newYorkShift(bars.length ? bars[0].t : status.updated_ms);
    const time = (ms) => ms / 1000 + shift;
    const colorAt = (i) => (status.ema_fast[i] > status.ema_slow[i] ? c.up : c.down);
    const glow = chart.addSeries(lib.LineSeries, { ...lineOptions, lineWidth: 12 });
    const band = chart.addSeries(lib.LineSeries, { ...lineOptions, lineWidth: 2 });
    const candles = chart.addSeries(lib.CandlestickSeries, {
      upColor: c.up, downColor: c.down, wickUpColor: c.up, wickDownColor: c.down, borderVisible: false, priceLineVisible: false,
    });
    glow.setData(bars.map((bar, i) => ({ time: time(bar.t), value: status.ema_slow[i], color: `${colorAt(i)}38` })));
    band.setData(bars.map((bar, i) => ({ time: time(bar.t), value: status.ema_slow[i], color: colorAt(i) })));
    candles.setData(bars.map((bar) => ({ time: time(bar.t), open: bar.o, high: bar.h, low: bar.l, close: bar.c })));

    const step = status.timeframe === "1Min" ? 60 : 300;
    const snap = (seconds) => Math.floor(seconds / step) * step;
    const marks = (status.signals || []).map((signal) => ({
      time: time(signal.t), position: signal.side === "buy" ? "belowBar" : "aboveBar", shape: "circle", size: 0,
      color: signal.side === "buy" ? c.up : c.down, text: signal.side === "buy" ? "BUY" : "SELL",
    }));
    for (const fill of status.fills || []) {
      marks.push({
        time: snap(time(Date.parse(fill.t))), position: fill.side === "buy" ? "belowBar" : "aboveBar",
        shape: fill.side === "buy" ? "arrowUp" : "arrowDown", color: fill.side === "buy" ? c.up : c.down, size: 1,
      });
    }
    lib.createSeriesMarkers(candles, marks.sort((a, b) => a.time - b.time));
    if (status.position) {
      candles.createPriceLine({ price: status.position.avgEntry, color: c.sky, lineStyle: 2, axisLabelVisible: true, title: "Entry" });
    }
    chart.timeScale().setVisibleLogicalRange({ from: -4, to: bars.length + 2 });      // room for the first label
    readout(chart, legend, (point) => {
      const bar = point.seriesData.get(candles);
      const index = bars.findIndex((row) => time(row.t) === point.time);
      if (!bar || index < 0) return legend.dataset.resting || "";
      return `Open ${bar.open.toFixed(2)}, close ${bar.close.toFixed(2)}, EMA(9) ${status.ema_fast[index]}, EMA(21) ${status.ema_slow[index]}`;
    });
  }

  function drawdowns(points) {
    let peak = -Infinity;
    return points.map((point) => {
      peak = Math.max(peak, point.value);
      return { time: point.time, value: (point.value / peak - 1) * 100 };
    });
  }

  function rebased(points) {
    const first = points.length ? points[0].value : 1;
    return points.map((point) => ({ time: point.time, value: (point.value / first) * 100 }));
  }

  // Account value against SPY, both from 100, with a matching chart of how far each fall went.
  async function equityVsSpy(top, bottom, legend, account, spy) {
    const lib = await load("lightweight");
    top.replaceChildren();
    bottom.replaceChildren();
    const c = palette();
    const upper = priceChart(lib, top);
    const lower = priceChart(lib, bottom, { rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.05, bottom: 0 } } });
    const mine = upper.addSeries(lib.LineSeries, { color: c.amber, lineWidth: 2, title: "Account", priceLineVisible: false });
    const market = upper.addSeries(lib.LineSeries, { color: c.sky, lineWidth: 2, title: "SPY", priceLineVisible: false });
    const under = lower.addSeries(lib.AreaSeries, {
      lineColor: c.down, topColor: `${c.down}10`, bottomColor: `${c.down}55`, lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
      priceFormat: { type: "percent" },
    });
    const days = account.filter((point) => !spy.length || point.time >= spy[0].time);
    mine.setData(rebased(days.length ? days : account));
    if (spy.length) market.setData(rebased(spy.filter((point) => point.time >= (days[0] || account[0]).time)));
    under.setData(drawdowns(days.length ? days : account));
    upper.timeScale().fitContent();
    lower.timeScale().fitContent();
    let syncing = false;
    const link = (from, to) => from.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (syncing || !range) return;
      syncing = true;
      to.timeScale().setVisibleLogicalRange(range);
      syncing = false;
    });
    link(upper, lower);
    link(lower, upper);
    readout(upper, legend, (point) => {
      const value = point.seriesData.get(mine);
      const other = point.seriesData.get(market);
      return `Account ${value ? value.value.toFixed(1) : "–"}${other ? `, SPY ${other.value.toFixed(1)}` : ""}`;
    });
  }

  function sma(values, days) {
    const result = [];
    let sum = 0;
    values.forEach((value, i) => {
      sum += value;
      if (i >= days) sum -= values[i - days];
      result.push(i >= days - 1 ? sum / days : null);
    });
    return result;
  }

  function wilderRsi(closes, days) {
    const result = closes.map(() => null);
    let gain = 0;
    let loss = 0;
    for (let i = 1; i < closes.length; i += 1) {
      const change = closes[i] - closes[i - 1];
      const up = Math.max(change, 0);
      const down = Math.max(-change, 0);
      if (i <= days) {
        gain += up / days;
        loss += down / days;
        if (i === days) result[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
      } else {
        gain = (gain * (days - 1) + up) / days;
        loss = (loss * (days - 1) + down) / days;
        result[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
      }
    }
    return result;
  }

  // Six months of candles with the buy, stop and sell, plus the indicator its sleeve trades on.
  async function positionChart(holder, legend, bars, detail) {
    const lib = await load("lightweight");
    holder.replaceChildren();
    const c = palette();
    const chart = priceChart(lib, holder);
    const candles = chart.addSeries(lib.CandlestickSeries, {
      upColor: c.up, downColor: c.down, wickUpColor: c.up, wickDownColor: c.down, borderVisible: false, priceLineVisible: false,
    });
    const day = (bar) => new Date(bar.t).toISOString().slice(0, 10);
    const closes = bars.map((bar) => bar.c);
    const shown = (series) => series.map((value, i) => ({ time: day(bars[i]), value })).slice(-126).filter((point) => point.value !== null);
    candles.setData(bars.slice(-126).map((bar) => ({ time: day(bar), open: bar.o, high: bar.h, low: bar.l, close: bar.c })));
    const trend = chart.addSeries(lib.LineSeries, { ...lineOptions, color: c.sky, lineWidth: 2 });
    trend.setData(shown(sma(closes, 200)));
    if (detail.sleeve === "rsi2_stocks") {
      const quick = chart.addSeries(lib.LineSeries, { ...lineOptions, color: c.amber, lineWidth: 1 });
      quick.setData(shown(sma(closes, 5)));
      const pane = chart.addSeries(lib.LineSeries, { ...lineOptions, color: c.amber, lineWidth: 2 }, 1);
      pane.setData(shown(wilderRsi(closes, 2)));
      pane.createPriceLine({ price: 10, color: c.up, lineStyle: 2, title: "Buy under 10" });
    } else if (detail.sleeve === "ibs_etfs") {
      const values = bars.map((bar) => (bar.h > bar.l ? (bar.c - bar.l) / (bar.h - bar.l) : null));
      const pane = chart.addSeries(lib.LineSeries, { ...lineOptions, color: c.amber, lineWidth: 2 }, 1);
      pane.setData(shown(values));
      pane.createPriceLine({ price: 0.2, color: c.up, lineStyle: 2, title: "Buy under 0.2" });
      pane.createPriceLine({ price: 0.8, color: c.down, lineStyle: 2, title: "Sell over 0.8" });
    }
    if (chart.panes().length > 1) chart.panes()[1].setHeight(110);
    if (detail.entry) candles.createPriceLine({ price: detail.entry, color: c.sky, lineStyle: 2, axisLabelVisible: true, title: "Entry" });
    if (detail.stop) candles.createPriceLine({ price: detail.stop, color: c.down, lineStyle: 2, axisLabelVisible: true, title: "Stop" });
    const first = day(bars[Math.max(0, bars.length - 126)]);
    const marks = (detail.marks || []).filter((mark) => mark.time >= first).map((mark) => ({
      time: mark.time, position: mark.side === "buy" ? "belowBar" : "aboveBar", shape: mark.side === "buy" ? "arrowUp" : "arrowDown",
      color: mark.side === "buy" ? c.up : c.down, text: mark.text,
    }));
    lib.createSeriesMarkers(candles, marks.sort((a, b) => (a.time < b.time ? -1 : 1)));
    chart.timeScale().fitContent();
    readout(chart, legend, (point) => {
      const bar = point.seriesData.get(candles);
      return bar ? `${point.time}: open ${bar.open.toFixed(2)}, close ${bar.close.toFixed(2)}` : "";
    });
  }

  // Simulated out-of-sample curves against SPY, all from 100.
  async function labCurves(holder, legend, curves, spy) {
    const lib = await load("lightweight");
    holder.replaceChildren();
    const c = palette();
    const chart = priceChart(lib, holder);
    const add = (points, color, title, width) => {
      const series = chart.addSeries(lib.LineSeries, { color, lineWidth: width, title, priceLineVisible: false, lastValueVisible: true });
      series.setData(points);
      return series;
    };
    const asPoints = (rows) => rows.map(([time, value]) => ({ time, value }));
    const mix = add(asPoints(curves.mix), c.amber, "Mix", 3);
    const old = add(asPoints(curves.live), c.up, "Old live", 2);
    const market = spy.length ? add(rebased(spy), c.sky, "SPY", 2) : null;
    chart.timeScale().fitContent();
    readout(chart, legend, (point) => {
      const parts = [["Mix", mix], ["Old live", old], ["SPY", market]].filter(([, series]) => series)
        .map(([name, series]) => {
          const value = point.seriesData.get(series);
          return value ? `${name} ${value.value.toFixed(1)}` : "";
        }).filter(Boolean);
      return `${point.time}: ${parts.join(", ")}`;
    });
  }

  // ---------------------------------------------------------------------------
  // ECharts
  // ---------------------------------------------------------------------------
  async function echart(holder, option) {
    const lib = await load("echarts");
    holder.replaceChildren();
    const c = palette();
    const chart = lib.init(holder, null, { renderer: "canvas" });
    chart.setOption({
      animation: !calm,
      textStyle: { color: c.ink2, fontFamily: "Saira, system-ui, sans-serif" },
      grid: { left: 6, right: 12, top: 34, bottom: 8, containLabel: true },
      tooltip: {
        confine: true, trigger: "axis", triggerOn: "mousemove|click", backgroundColor: c.raised, borderColor: c.line, textStyle: { color: c.ink },
      },
      ...option,
    });
    const resize = new ResizeObserver(() => chart.resize());
    resize.observe(holder);
    live.push({ remove: () => { resize.disconnect(); chart.dispose(); } });
    return chart;
  }

  const axisStyle = (c) => ({
    axisLine: { lineStyle: { color: c.line } }, axisTick: { show: false }, splitLine: { lineStyle: { color: c.line } }, axisLabel: { color: c.ink3, hideOverlap: true },
  });

  function exposure(holder, rows) {
    const c = palette();
    const share = (row, value) => (row.equity ? Math.round((value / row.equity) * 1000) / 10 : 0);
    const cash = (row) => (row.equity ? Math.max(0, Math.round((100 - share(row, row.short + row.medium + row.long)) * 10) / 10) : 0);
    const layers = [["Short", (r) => share(r, r.short), c.amber], ["Medium", (r) => share(r, r.medium), c.sky],
      ["Long", (r) => share(r, r.long), c.up], ["Cash", cash, c.neutral]];
    return echart(holder, {
      legend: { top: 0, textStyle: { color: c.ink2 } },
      tooltip: { confine: true, trigger: "axis", triggerOn: "mousemove|click", valueFormatter: (value) => `${value}%`, backgroundColor: c.raised, borderColor: c.line, textStyle: { color: c.ink } },
      xAxis: { type: "category", boundaryGap: false, data: rows.map((row) => row.date), ...axisStyle(c) },
      yAxis: { type: "value", max: 100, axisLabel: { color: c.ink3, formatter: "{value}%" }, splitLine: { lineStyle: { color: c.line } } },
      series: layers.map(([name, value, color]) => ({
        name, type: "line", stack: "all", symbol: "none", lineStyle: { width: 1, color }, itemStyle: { color }, areaStyle: { color, opacity: 0.55 }, data: rows.map(value),
      })),
    });
  }

  function sleeveCurves(holder, rows, nameOf) {
    const c = palette();
    const names = [...new Set(rows.flatMap((row) => Object.keys(row.sleeves || {})))];
    return echart(holder, {
      legend: { top: 0, type: "scroll", textStyle: { color: c.ink2 }, formatter: nameOf },
      color: [c.amber, c.sky, c.up, c.series, c.stocks],
      xAxis: { type: "category", boundaryGap: false, data: rows.map((row) => row.date), ...axisStyle(c) },
      yAxis: { type: "value", axisLabel: { color: c.ink3, formatter: "${value}" }, splitLine: { lineStyle: { color: c.line } } },
      series: names.map((name) => ({ name, type: "line", symbol: "none", lineStyle: { width: 2 }, data: rows.map((row) => (row.sleeves || {})[name] ?? null) })),
    });
  }

  function heatmap(holder, xNames, yNames, cells, limits, format) {
    const c = palette();
    return echart(holder, {
      grid: { left: 4, right: 4, top: 6, bottom: 4, containLabel: true },
      tooltip: {
        confine: true, trigger: "item", triggerOn: "mousemove|click", backgroundColor: c.raised, borderColor: c.line, textStyle: { color: c.ink },
        formatter: (item) => `${yNames[item.value[1]]}, ${xNames[item.value[0]]}: ${format(item.value[2])}`,
      },
      xAxis: { type: "category", data: xNames, splitArea: { show: false }, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: c.ink3, interval: 0, fontSize: 11 } },
      yAxis: { type: "category", data: yNames, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: c.ink3, fontSize: 11 } },
      visualMap: { show: false, min: limits[0], max: limits[1], inRange: { color: [c.down, c.neutral, c.up] } },
      series: [{
        type: "heatmap", data: cells, itemStyle: { borderColor: c.line, borderWidth: 1, borderRadius: 3 },
        label: { show: true, fontSize: 10, color: c.ink, formatter: (item) => format(item.value[2]) },
      }],
    });
  }

  function histogram(holder, values, unit) {
    const c = palette();
    const low = Math.min(...values);
    const high = Math.max(...values);
    const count = Math.min(16, Math.max(5, Math.ceil(Math.sqrt(values.length)) + 1));
    const width = (high - low || 1) / count;
    const bins = Array.from({ length: count }, () => 0);
    for (const value of values) bins[Math.min(count - 1, Math.floor((value - low) / width))] += 1;
    const labels = bins.map((_, i) => unit(low + width * (i + 0.5)));
    return echart(holder, {
      grid: { left: 6, right: 12, top: 12, bottom: 8, containLabel: true },
      xAxis: { type: "category", data: labels, ...axisStyle(c), splitLine: { show: false } },
      yAxis: { type: "value", minInterval: 1, axisLabel: { color: c.ink3 }, splitLine: { lineStyle: { color: c.line } } },
      series: [{
        name: "Trades", type: "bar", barCategoryGap: "12%",
        data: bins.map((size, i) => ({ value: size, itemStyle: { color: low + width * (i + 0.5) >= 0 ? c.up : c.down, borderRadius: [4, 4, 0, 0] } })),
      }],
    });
  }

  function lineChart(holder, days, values, name, format) {
    const c = palette();
    return echart(holder, {
      grid: { left: 6, right: 12, top: 12, bottom: 8, containLabel: true },
      tooltip: { confine: true, trigger: "axis", triggerOn: "mousemove|click", valueFormatter: format, backgroundColor: c.raised, borderColor: c.line, textStyle: { color: c.ink } },
      xAxis: { type: "category", boundaryGap: false, data: days, ...axisStyle(c) },
      yAxis: { type: "value", scale: true, axisLabel: { color: c.ink3, formatter: format }, splitLine: { lineStyle: { color: c.line } } },
      series: [{ name, type: "line", symbol: "none", lineStyle: { width: 2, color: c.amber }, itemStyle: { color: c.amber }, data: values }],
    });
  }

  return { whenNear, clear, liveChart, equityVsSpy, positionChart, labCurves, exposure, sleeveCurves, heatmap, histogram, lineChart };
})();
