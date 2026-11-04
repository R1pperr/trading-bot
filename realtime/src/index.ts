// index.ts: the Cloudflare Worker. A cron trigger calls scheduled() every minute of the trading day,
// and each run rebuilds everything from Alpaca, so the Worker keeps no memory between runs.
//
// RT_MODE "off" does nothing, "dry" logs what it would do and never places an order, and "live"
// places paper orders. This engine trades its own Alpaca paper account, never the main bot's.
//
// GET /status is read-only and holds no secrets. Your positions and fills are always in it.
// Prices are only in it for a visitor with your viewer code, because Alpaca's data can't be
// passed on publicly. GET /prices is the same, and always needs the code.

import * as alpaca from "./alpaca.ts";
import type { Fetcher, Keys } from "./alpaca.ts";
import {
  EMA_FAST, EMA_SLOW, SYMBOL, RT_POSITION_PCT, RT_STOP_PCT, RT_MAX_ENTRIES, RT_DAILY_LOSS_PCT, clientOrderId, completedBars,
  crossovers, decide, ema, newYorkMidnight, stopPriceFor, warmedUp,
} from "./engine.ts";
import type { Action, Order, Signal } from "./engine.ts";

export type Env = {
  RT_MODE?: string; RT_TIMEFRAME?: string; RT_EXPERIMENTAL?: string; PAGES_ORIGIN?: string;
  ALPACA_KEY: string; ALPACA_SECRET: string; TELEGRAM_BOT_TOKEN?: string; TELEGRAM_CHAT_ID?: string; VIEWER_CODE?: string;
};
export type Report = { mode: string; ran: boolean; reason: string; would: string[]; placed: string[]; messages: string[] };

const FILL_WAIT_TRIES = 3;
const STATUS_CACHE_MS = 20000;
const minutesOf = (timeframe: string) => (timeframe === "1Min" ? 1 : 5);
const pause = (ms: number) => new Promise((done) => setTimeout(done, ms));

function modeOf(env: Env): string {
  return ["off", "dry", "live"].includes(env.RT_MODE ?? "") ? String(env.RT_MODE) : "dry";
}

function labelFor(env: Env, mode: string): string {
  const parts = ["[REALTIME]"];
  if (mode === "dry") parts.push("[DRY]");
  if (env.RT_EXPERIMENTAL === "true") parts.push("[Experimental: failed backtest]");
  return parts.join(" ");
}

async function telegram(call: Fetcher, env: Env, text: string): Promise<void> {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return;
  try {
    await call(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
    });
  } catch {
    console.log("Telegram message not sent");
  }
}

// One run for each signal and fill: each is reported only in the minute it happens.
export function justHappened(when: number, now: number): boolean {
  return when <= now && now - when < 60000;
}

async function act(call: Fetcher, keys: Keys, action: Action, report: Report): Promise<void> {
  if (action.kind === "cancel") {
    await alpaca.cancel(call, keys, action.id);
  } else if (action.kind === "stop") {
    await alpaca.stopOrder(call, keys, SYMBOL, action.qty, action.stopPrice, action.clientId);
  } else if (action.kind === "sell") {
    for (const id of action.cancel) await alpaca.cancel(call, keys, id);
    for (let attempt = 0; attempt < FILL_WAIT_TRIES; attempt += 1) {
      try {
        await alpaca.marketOrder(call, keys, SYMBOL, "sell", action.qty, action.clientId);
        break;
      } catch (error) {
        if (attempt === FILL_WAIT_TRIES - 1) throw error;     // a cancelled stop can take a moment to free the shares
        await pause(1000);
      }
    }
  } else {
    const order = await alpaca.marketOrder(call, keys, SYMBOL, "buy", action.qty, action.clientId);
    if (order) await placeStopSoon(call, keys, order.id, report);
  }
  report.placed.push(`${action.kind}: ${action.why}`);
}

// The stop goes in as soon as the entry fills. If the fill is slow, the next run places it.
async function placeStopSoon(call: Fetcher, keys: Keys, orderId: string, report: Report): Promise<void> {
  for (let attempt = 0; attempt < FILL_WAIT_TRIES; attempt += 1) {
    await pause(1000);
    const order = await alpaca.orderById(call, keys, orderId);
    if (order && order.status === "filled") {
      await alpaca.stopOrder(call, keys, SYMBOL, order.qty, stopPriceFor(order.filledPrice),
        clientOrderId(SYMBOL, Date.parse(order.filledAt), "stop"));
      report.placed.push("stop: protective stop 2% below the entry");
      return;
    }
  }
}

export async function runOnce(env: Env, now: number, call: Fetcher): Promise<Report> {
  const mode = modeOf(env);
  const report: Report = { mode, ran: false, reason: "", would: [], placed: [], messages: [] };
  if (mode === "off") return { ...report, reason: "RT_MODE is off" };
  alpaca.requirePaperKey(env.ALPACA_KEY);
  const keys = { key: env.ALPACA_KEY, secret: env.ALPACA_SECRET };
  const time = await alpaca.clock(call, keys);
  if (!time.isOpen) return { ...report, reason: "market closed" };
  report.ran = true;

  const timeframe = env.RT_TIMEFRAME === "1Min" ? "1Min" : "5Min";
  const minutes = minutesOf(timeframe);
  const [account, bars, position, orders] = await Promise.all([
    alpaca.account(call, keys), alpaca.bars(call, keys, SYMBOL, timeframe, now), alpaca.position(call, keys, SYMBOL),
    alpaca.ordersSince(call, keys, newYorkMidnight(now)),
  ]);
  const plan = decide({ now, bars, minutes, position, orders, equity: account.equity, lastEquity: account.lastEquity });
  report.reason = plan.notes.join("; ");
  const label = labelFor(env, mode);

  for (const signal of plan.signals.filter((item) => justHappened(item.end, now))) {
    const text = `${label} ${signal.side === "buy" ? "🟢 BUY" : "🔴 SELL"} signal on ${SYMBOL} at $${signal.close.toFixed(2)} (${timeframe})`;
    report.messages.push(text);
    await telegram(call, env, text);
  }
  for (const order of orders.filter((item) => item.filledAt && justHappened(Date.parse(item.filledAt), now))) {
    const text = `${label} ✅ Filled: ${order.side} ${order.qty} ${SYMBOL} at $${order.filledPrice.toFixed(2)}`;
    report.messages.push(text);
    await telegram(call, env, text);
  }
  for (const action of plan.actions) {
    if (mode === "dry") {
      report.would.push(`would ${action.kind}: ${action.why}`);
      console.log(`DRY RUN, nothing placed: ${action.kind} (${action.why})`);
      continue;
    }
    await act(call, keys, action, report);
  }
  console.log(JSON.stringify({ mode, equity: account.equity, position, actions: plan.actions.length, notes: plan.notes }));
  return report;
}

// ---------------------------------------------------------------------------
// The read-only pages for the dashboard
// ---------------------------------------------------------------------------
function codeMatches(given: string | null, wanted: string | undefined): boolean {
  if (!wanted || !given || given.length !== wanted.length) return false;
  let difference = 0;
  for (let i = 0; i < wanted.length; i += 1) difference |= given.charCodeAt(i) ^ wanted.charCodeAt(i);
  return difference === 0;
}

function corsHeaders(request: Request, env: Env): Record<string, string> {
  const origin = request.headers.get("origin");
  const allowed = env.PAGES_ORIGIN ?? "";
  const base: Record<string, string> = { "content-type": "application/json", "cache-control": "no-store", vary: "Origin" };
  if (origin === allowed) {
    base["access-control-allow-origin"] = allowed;
    base["access-control-allow-headers"] = "x-viewer-code";
    base["access-control-allow-methods"] = "GET, OPTIONS";
    base["access-control-max-age"] = "600";       // the browser asks once in 10 minutes, not on every poll
  }
  return base;
}

export function publicFill(order: Order): Record<string, unknown> {
  return { t: order.filledAt, side: order.side, qty: order.qty, price: order.filledPrice };
}

export async function buildStatus(env: Env, now: number, call: Fetcher, unlocked: boolean): Promise<Record<string, unknown>> {
  const mode = modeOf(env);
  const timeframe = env.RT_TIMEFRAME === "1Min" ? "1Min" : "5Min";
  const minutes = minutesOf(timeframe);
  alpaca.requirePaperKey(env.ALPACA_KEY);
  const keys = { key: env.ALPACA_KEY, secret: env.ALPACA_SECRET };
  const [time, account, position, orders, bars] = await Promise.all([
    alpaca.clock(call, keys), alpaca.account(call, keys), alpaca.position(call, keys, SYMBOL),
    alpaca.ordersSince(call, keys, newYorkMidnight(now)), alpaca.bars(call, keys, SYMBOL, timeframe, now),
  ]);
  const { bars: series, firstToday } = warmedUp(completedBars(bars, now, minutes), now);
  const signals: Signal[] = crossovers(series, firstToday, minutes);
  const body: Record<string, unknown> = {
    updated_ms: now, market_open: time.isOpen, next_open: time.nextOpen, mode, timeframe, symbol: SYMBOL,
    experimental: env.RT_EXPERIMENTAL === "true", locked: !unlocked,
    position, pl_today: account.equity - account.lastEquity, pl_today_pct: account.lastEquity ? account.equity / account.lastEquity - 1 : 0,
    equity: account.equity, signals: signals.map((signal) => ({ t: signal.t, side: signal.side })),
    fills: orders.filter((order) => order.filledAt).map(publicFill),
    rules: {
      position_pct: RT_POSITION_PCT, stop_pct: RT_STOP_PCT, max_entries: RT_MAX_ENTRIES, daily_loss_pct: RT_DAILY_LOSS_PCT,
      ema_fast: EMA_FAST, ema_slow: EMA_SLOW, no_entries_from: "15:45", flat_from: "15:55",
    },
  };
  if (unlocked) {
    const closes = series.map((bar) => bar.c);
    const fast = ema(closes, EMA_FAST);
    const slow = ema(closes, EMA_SLOW);
    const today = series.slice(firstToday);
    body.bars = today;
    body.ema_fast = fast.slice(firstToday).map((value) => Math.round(value * 100) / 100);
    body.ema_slow = slow.slice(firstToday).map((value) => Math.round(value * 100) / 100);
    body.signals = signals.map((signal) => ({ t: signal.t, side: signal.side, price: signal.close }));
  }
  return body;
}

let statusCache: { key: string; until: number; body: string } | null = null;

export function clearStatusCache(): void {
  statusCache = null;
}

export async function handle(request: Request, env: Env, now: number, call: Fetcher): Promise<Response> {
  const url = new URL(request.url);
  const headers = corsHeaders(request, env);
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers });
  if (request.method !== "GET") return new Response('{"error":"read only"}', { status: 405, headers });
  const unlocked = codeMatches(request.headers.get("x-viewer-code"), env.VIEWER_CODE);
  try {
    if (url.pathname === "/status") {
      const key = unlocked ? "open" : "locked";
      if (statusCache && statusCache.key === key && statusCache.until > now) return new Response(statusCache.body, { headers });
      const body = JSON.stringify(await buildStatus(env, now, call, unlocked));
      statusCache = { key, until: now + STATUS_CACHE_MS, body };
      return new Response(body, { headers });
    }
    if (url.pathname === "/prices") {
      if (!unlocked) return new Response('{"error":"this needs your viewer code"}', { status: 401, headers });
      const symbol = (url.searchParams.get("symbol") ?? "").toUpperCase();
      const days = Math.min(4200, Math.max(1, Number(url.searchParams.get("days") ?? 200) || 200));
      if (!/^[A-Z.]{1,8}$/.test(symbol)) return new Response('{"error":"bad symbol"}', { status: 400, headers });
      alpaca.requirePaperKey(env.ALPACA_KEY);
      const bars = await alpaca.dailyBars(call, { key: env.ALPACA_KEY, secret: env.ALPACA_SECRET }, symbol, days, now);
      return new Response(JSON.stringify({ symbol, bars }), { headers });
    }
    return new Response('{"error":"not found"}', { status: 404, headers });
  } catch (error) {
    // Only the reason is logged, for `wrangler tail`, with any key text blanked out. Visitors still see the short message.
    let reason = String((error as Error)?.message ?? error);
    for (const secret of [env.ALPACA_KEY, env.ALPACA_SECRET]) if (secret) reason = reason.split(secret).join("[key]");
    console.log(`${url.pathname} failed: ${reason}`);
    return new Response('{"error":"could not reach Alpaca"}', { status: 502, headers });
  }
}

export default {
  async scheduled(event: { scheduledTime: number }, env: Env, context: { waitUntil(work: Promise<unknown>): void }) {
    context.waitUntil(runOnce(env, event.scheduledTime, fetch).catch((error) => console.log(`Run failed: ${error.message}`)));
  },
  fetch(request: Request, env: Env) {
    return handle(request, env, Date.now(), fetch);
  },
};
