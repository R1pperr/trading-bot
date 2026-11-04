// index.ts: the Cloudflare Worker. A cron trigger calls scheduled() every minute of the trading day.
// Trading decisions are rebuilt from Alpaca on every run. A small saved state (Cloudflare KV, one
// write per run) keeps only the activity feed, the AI's answers, alert times and a daily cache.
//
// RT_MODE "off" does nothing, "dry" logs what it would do and never places an order, and "live"
// places paper orders. This engine trades its own Alpaca paper account, never the main bot's.
//
// The AI (Gemini, free tier) is asked only when a fresh buy crossover is about to be acted on,
// never every minute, and its answer is remembered for that signal. In "enforce" a VETO, an error
// or a timeout means no trade. Exits, stops and the close-out never wait for the AI.
//
// GET /status is read-only and holds no secrets. Your positions, fills and the activity feed are
// always in it. Prices are only in it for a visitor with your viewer code, because Alpaca's data
// can't be passed on publicly. GET /prices is the same, and always needs the code.

import * as alpaca from "./alpaca.ts";
import type { Fetcher, Keys } from "./alpaca.ts";
import {
  EMA_FAST, EMA_SLOW, SYMBOL, RT_POSITION_PCT, RT_STOP_PCT, RT_MAX_ENTRIES, RT_DAILY_LOSS_PCT, anyFix, cleanFixes, clientOrderId,
  completedBars, crossovers, dailyContext, dayChange, decide, ema, fixLabel, needsDaily, newYorkDay, newYorkMidnight,
  newYorkMinute, spreadTooWide, stopPriceFor, warmedUp,
} from "./engine.ts";
import type { Action, Bar, Daily, FixSet, Order, Plan, Signal } from "./engine.ts";

export type Kv = { get(key: string): Promise<string | null>; put(key: string, value: string): Promise<void> };
export type Env = {
  RT_MODE?: string; RT_TIMEFRAME?: string; RT_EXPERIMENTAL?: string; PAGES_ORIGIN?: string;
  ALPACA_KEY: string; ALPACA_SECRET: string; TELEGRAM_BOT_TOKEN?: string; TELEGRAM_CHAT_ID?: string; VIEWER_CODE?: string;
  GEMINI_API_KEY?: string; GEMINI_MODEL?: string; AI_VETO_MODE?: string;
  MAIN_ALPACA_KEY?: string; MAIN_ALPACA_SECRET?: string; WATCHDOG_TEST?: string; RT_STATE?: Kv;
};
export type Report = { mode: string; ran: boolean; reason: string; would: string[]; placed: string[]; messages: string[] };

export const CONTROL_URL = "https://raw.githubusercontent.com/R1pperr/trading-bot/main/docs/data/control.json";
export const BOT1_STATUS_URL = "https://raw.githubusercontent.com/R1pperr/trading-bot/main/docs/data/status.json";
export const GEMINI_HOST = "https://generativelanguage.googleapis.com";
export const START_CASH = 100000;
export const GEMINI_TIMEOUT_MS = 8000;
const FILL_WAIT_TRIES = 3;
const STATUS_CACHE_MS = 20000;
const MINUTE = 60000;
const HALF_HOUR = 30 * MINUTE;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const FEED_CAP = 300;
const FEED_SHOWN = 100;
const DECISIONS_CAP = 50;
const DAILY_BAR_DAYS = 420;                // calendar days, enough for 252 + 20 trading days
export const BOT1_LATE_MINUTES = 60;
const BOT1_CHECK_FROM = 9 * 60 + 50;       // the main bot's first market-hours run is around 09:05-09:20
export const TEST_MISSED_MINUTES = 120;
const NO_STOP_RUNS = 2;
const GEMINI_FAILURE_ALERT = 3;
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

// Blanks out every secret, in case one ever ends up inside an error message.
function scrub(env: Env, text: string): string {
  let result = text;
  for (const secret of [env.ALPACA_KEY, env.ALPACA_SECRET, env.MAIN_ALPACA_KEY, env.MAIN_ALPACA_SECRET, env.GEMINI_API_KEY,
    env.TELEGRAM_BOT_TOKEN, env.VIEWER_CODE]) {
    if (secret) result = result.split(secret).join("[key]");
  }
  return result;
}

// The public feed never shows a price. Text from outside (the AI, error messages) goes through this.
export function maskPrices(text: string): string {
  return String(text).replace(/\$\s?\d[\d,]*(?:\.\d+)?/g, "…").replace(/\d+\.\d+/g, "…").replace(/\b\d{3,}\b/g, "…");
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

// ---------------------------------------------------------------------------
// The saved state: one JSON value in KV, read once and written once per run
// ---------------------------------------------------------------------------
export type FeedItem = { t: number; text: string; detail?: string };
export type Decision = {
  t: number; bar_t: number; client_id: string; decision: string; reason: string; qty: number; spread_pct: number | null;
};
export type State = {
  heartbeat: { last_run_ms: number; last_result: string; mode: string };
  feed: FeedItem[];
  ai: { failures_in_row: number; decisions: Decision[] };
  alerts: Record<string, number>;
  control: unknown;
  daily: Daily | null;
  watchdog: { bot1_last_run_utc: string; checked_ms: number };
  nostop_runs: number;
};

export function freshState(): State {
  return {
    heartbeat: { last_run_ms: 0, last_result: "", mode: "" }, feed: [], ai: { failures_in_row: 0, decisions: [] },
    alerts: {}, control: null, daily: null, watchdog: { bot1_last_run_utc: "", checked_ms: 0 }, nostop_runs: 0,
  };
}

export async function loadState(env: Env): Promise<State> {
  const base = freshState();
  if (!env.RT_STATE) return base;
  try {
    const raw = await env.RT_STATE.get("state");
    if (!raw) return base;
    const saved = JSON.parse(raw) as Partial<State>;
    return {
      ...base, ...saved,
      heartbeat: { ...base.heartbeat, ...saved.heartbeat }, feed: Array.isArray(saved.feed) ? saved.feed : [],
      ai: { failures_in_row: Number(saved.ai?.failures_in_row ?? 0), decisions: Array.isArray(saved.ai?.decisions) ? saved.ai!.decisions : [] },
      alerts: { ...(saved.alerts ?? {}) }, watchdog: { ...base.watchdog, ...saved.watchdog },
      nostop_runs: Number(saved.nostop_runs ?? 0),
    };
  } catch {
    console.log("Saved state not read, starting fresh");
    return base;
  }
}

export async function saveState(env: Env, state: State): Promise<boolean> {
  if (!env.RT_STATE) return false;
  try {
    await env.RT_STATE.put("state", JSON.stringify(state));
    return true;
  } catch {
    console.log("Saved state not written");
    return false;
  }
}

// Newest first. The same text twice in a row only moves its time, so a quiet hour stays one line.
export function addFeed(state: State, t: number, text: string, detail?: string): void {
  const top = state.feed[0];
  if (top && top.text === text) {
    top.t = t;
    if (detail !== undefined) top.detail = detail;
    return;
  }
  state.feed.unshift(detail === undefined ? { t, text } : { t, text, detail });
  if (state.feed.length > FEED_CAP) state.feed.length = FEED_CAP;
}

async function alert(call: Fetcher, env: Env, state: State, kind: string, text: string, now: number, every: number): Promise<boolean> {
  if (now - (state.alerts[kind] ?? 0) < every) return false;
  state.alerts[kind] = now;
  await telegram(call, env, text);
  return true;
}

function pruneAlerts(state: State, now: number): void {
  for (const [kind, when] of Object.entries(state.alerts)) if (now - when > 2 * DAY) delete state.alerts[kind];
}

// ---------------------------------------------------------------------------
// control.json: the fix switches and the AI mode, written by the daily job on GitHub
// ---------------------------------------------------------------------------
export type Control = { aiMode: "enforce" | "shadow" | "off"; aiRecord: string; fixes: FixSet; shadow: FixSet | null };

function aiDefault(env: Env): "enforce" | "shadow" | "off" {
  return env.AI_VETO_MODE === "shadow" || env.AI_VETO_MODE === "off" ? env.AI_VETO_MODE : "enforce";
}

export function parseControl(raw: unknown, env: Env): Control {
  const body = (raw && typeof raw === "object" ? raw : {}) as Record<string, any>;
  const chosen = body.ai_mode?.realtime;
  const shadow = body.shadow?.realtime ? cleanFixes(body.shadow.realtime) : null;
  return {
    aiMode: chosen === "enforce" || chosen === "shadow" ? chosen : aiDefault(env),
    aiRecord: typeof body.ai_record?.realtime === "string" ? body.ai_record.realtime.slice(0, 1500) : "",
    fixes: cleanFixes(body.fixes?.realtime),
    shadow: shadow && anyFix(shadow) ? shadow : null,
  };
}

async function fetchControl(call: Fetcher, state: State): Promise<unknown> {
  try {
    const response = await call(CONTROL_URL, { headers: { "cache-control": "no-cache" } });
    if (response.ok) {
      state.control = await response.json();
      return state.control;
    }
  } catch {
    console.log("control.json not read, using the saved copy");
  }
  return state.control ?? null;            // the saved copy, or all off
}

async function dailyFor(call: Fetcher, keys: Keys, state: State, now: number): Promise<Daily | null> {
  const today = newYorkDay(now);
  if (state.daily && state.daily.day === today) return state.daily;
  try {
    state.daily = dailyContext(await alpaca.dailyBars(call, keys, SYMBOL, DAILY_BAR_DAYS, now), today);
    return state.daily;
  } catch {
    console.log("Daily bars not read");
    return null;
  }
}

// ---------------------------------------------------------------------------
// The watchdog: is the main bot (GitHub Actions) still running?
// ---------------------------------------------------------------------------
export function minutesSince(iso: string, now: number): number | null {
  const t = Date.parse(iso);
  return iso && Number.isFinite(t) ? Math.floor((now - t) / MINUTE) : null;
}

export function bot1IsLate(minutes: number | null, now: number): boolean {
  return minutes !== null && minutes > BOT1_LATE_MINUTES && newYorkMinute(now) >= BOT1_CHECK_FROM;
}

export function bot1LateText(minutes: number, label: string, test = false): string {
  return `${test ? "[TEST] " : ""}${label} ⏰ Watchdog: the main bot hasn't run for ${minutes} minutes`
    + (test ? " (simulated missed run, no orders involved)" : "");
}

async function watchBot1(call: Fetcher, env: Env, state: State, now: number, label: string): Promise<void> {
  let last = "";
  try {
    const response = await call(BOT1_STATUS_URL, { headers: { "cache-control": "no-cache" } });
    if (response.ok) last = String(((await response.json()) as any)?.last_run?.time_utc ?? "");
  } catch {
    console.log("Main bot status not read");
  }
  if (!last) return;                       // can't tell, so no alarm
  state.watchdog = { bot1_last_run_utc: last, checked_ms: now };
  const minutes = minutesSince(last, now);
  if (bot1IsLate(minutes, now)) await alert(call, env, state, "bot1_late", bot1LateText(minutes!, label), now, HOUR);
}

// ---------------------------------------------------------------------------
// The AI analyst
// ---------------------------------------------------------------------------
export type Verdict = { decision: "APPROVE" | "VETO"; reason: string; failed: boolean };

const INSTRUCTIONS = `You are a cautious reviewer for a student's paper-trading (simulated money) project.
A simple intraday rule wants to make one trade. Decide only whether to APPROVE or VETO it.
VETO only if the recent prices give a clear reason the signal looks unreliable, for example
a sharp spike likely to reverse, or prices so choppy there is no real trend. Otherwise APPROVE.
You cannot change the trade size, and you must not suggest one.
Reply with JSON only: {"decision": "APPROVE" or "VETO", "reason": "<one sentence, at most 25 words>"}`;

function clockOf(t: number): string {
  const minute = newYorkMinute(t);
  return `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
}

export function aiPrompt(series: Bar[], signalIndex: number, record: string): string {
  const end = signalIndex >= 0 ? signalIndex : series.length - 1;
  const recent = series.slice(Math.max(0, end - 29), end + 1);
  const lines = recent.map((bar) => `${newYorkDay(bar.t)} ${clockOf(bar.t)}: ${bar.c.toFixed(2)}`);
  return [
    INSTRUCTIONS, "", `Proposed trade: BUY ${SYMBOL}`,
    `Strategy: EMA(${EMA_FAST}) crossed above EMA(${EMA_SLOW}) on finished 5-minute bars. Long only, `
      + `${RT_STOP_PCT * 100}% stop, flat by 15:55 New York time.`,
    `Last ${lines.length} 5-minute closes, oldest first (New York time):`, ...lines,
    ...(record ? ["", `Your recent record on this strategy: ${record}`] : []),
  ].join("\n");
}

export async function askGemini(call: Fetcher, env: Env, prompt: string, timeoutMs = GEMINI_TIMEOUT_MS): Promise<Verdict> {
  if (!env.GEMINI_API_KEY) return { decision: "VETO", reason: "AI error: no Gemini key is set", failed: true };
  const model = env.GEMINI_MODEL || "gemini-3.8-flash";
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await call(`${GEMINI_HOST}/v1beta/models/${model}:generateContent`, {
      method: "POST", signal: controller.signal,
      headers: { "content-type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY },
      body: JSON.stringify({
        contents: [{ parts: [{ text: prompt }] }],
        generationConfig: {
          temperature: 0, responseMimeType: "application/json",
          responseSchema: { type: "OBJECT", properties: { decision: { type: "STRING" }, reason: { type: "STRING" } }, required: ["decision", "reason"] },
        },
      }),
    });
    if (!response.ok) return { decision: "VETO", reason: `AI error: Gemini said ${response.status}`, failed: true };
    const body = (await response.json()) as any;
    let answer: any = null;
    try { answer = JSON.parse(String(body?.candidates?.[0]?.content?.parts?.[0]?.text ?? "")); } catch { answer = null; }
    const decision = String(answer?.decision ?? "").trim().toUpperCase();
    const reason = String(answer?.reason ?? "").split(/\s+/).join(" ").trim().slice(0, 200) || "(no reason given)";
    if (decision !== "APPROVE" && decision !== "VETO") return { decision: "VETO", reason: "AI gave an unclear answer", failed: true };
    return { decision, reason, failed: false };
  } catch (error) {
    const timedOut = (error as Error)?.name === "AbortError";
    return { decision: "VETO", reason: timedOut ? "AI error: Gemini timed out" : "AI error: Gemini not reached", failed: true };
  } finally {
    clearTimeout(timer);
  }
}

// One question per signal: the answer is saved under the buy's order id and reused for the grace bar.
async function aiVerdict(
  call: Fetcher, env: Env, state: State, control: Control, buy: Extract<Action, { kind: "buy" }>, series: Bar[],
  plan: Plan, now: number, spread: number | null, label: string,
): Promise<Verdict & { cached: boolean }> {
  const known = state.ai.decisions.find((item) => item.client_id === buy.clientId);
  if (known) {
    const decision = known.decision === "APPROVE" ? "APPROVE" : "VETO";
    return { decision, reason: known.reason, failed: known.reason.startsWith("AI error") || known.reason.startsWith("AI gave"), cached: true };
  }
  const verdict = await askGemini(call, env, aiPrompt(series, plan.signalIndex, control.aiRecord));
  state.ai.decisions.unshift({
    t: now, bar_t: series[plan.signalIndex]?.t ?? 0, client_id: buy.clientId, decision: verdict.decision,
    reason: verdict.reason, qty: buy.qty, spread_pct: spread,
  });
  if (state.ai.decisions.length > DECISIONS_CAP) state.ai.decisions.length = DECISIONS_CAP;
  state.ai.failures_in_row = verdict.failed ? state.ai.failures_in_row + 1 : 0;
  if (state.ai.failures_in_row >= GEMINI_FAILURE_ALERT) {
    await alert(call, env, state, "gemini_failures",
      `${label} ⚠️ Gemini failed ${state.ai.failures_in_row} times in a row (${verdict.reason}). In enforce mode that means no buys.`,
      now, HALF_HOUR);
  }
  return { ...verdict, cached: false };
}

// ---------------------------------------------------------------------------
// Placing orders
// ---------------------------------------------------------------------------
async function act(call: Fetcher, keys: Keys, action: Action, report: Report, stopPct: number): Promise<string> {
  if (action.kind === "cancel") {
    await alpaca.cancel(call, keys, action.id);
  } else if (action.kind === "stop") {
    await alpaca.stopOrder(call, keys, SYMBOL, action.qty, action.stopPrice, action.clientId);
  } else if (action.kind === "move_stop") {
    await alpaca.replaceStop(call, keys, action.id, action.stopPrice);
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
    report.placed.push(`${action.kind}: ${action.why}`);
    return order ? placeStopSoon(call, keys, order.id, report, stopPct) : "";
  }
  report.placed.push(`${action.kind}: ${action.why}`);
  return "";
}

// The stop goes in as soon as the entry fills. If the fill is slow, the next run places it.
async function placeStopSoon(call: Fetcher, keys: Keys, orderId: string, report: Report, stopPct: number): Promise<string> {
  for (let attempt = 0; attempt < FILL_WAIT_TRIES; attempt += 1) {
    await pause(1000);
    const order = await alpaca.orderById(call, keys, orderId);
    if (order && order.status === "filled") {
      const level = stopPriceFor(order.filledPrice, stopPct);
      await alpaca.stopOrder(call, keys, SYMBOL, order.qty, level, clientOrderId(SYMBOL, Date.parse(order.filledAt), "stop"));
      report.placed.push(`stop: protective stop ${Math.round(stopPct * 100)}% below the entry`);
      return `filled at $${order.filledPrice.toFixed(2)}, stop at $${level.toFixed(2)}`;
    }
  }
  return "";
}

const isLiveOrder = (order: Order) => ["new", "accepted", "pending_new", "partially_filled", "held"].includes(order.status);

function aiWords(verdict: Verdict): string {
  if (verdict.reason.startsWith("AI error") || verdict.reason.startsWith("AI gave")) {
    return `Gemini error (${maskPrices(verdict.reason.replace(/^AI error: /, ""))})`;
  }
  return `AI ${verdict.decision === "APPROVE" ? "approved" : "vetoed"} (${maskPrices(verdict.reason)})`;
}

function shadowLine(fixes: FixSet, live: Plan, shadow: Plan): string | null {
  const kinds = (plan: Plan) => plan.actions.map((action) => action.kind).join(" and ");
  if (kinds(live) === kinds(shadow)) return null;
  const why = shadow.notes[0] ? ` (${shadow.notes[0]})` : "";
  return `Shadow (${fixLabel(fixes)}): would ${kinds(shadow) || "skip"}${why}`;
}

// ---------------------------------------------------------------------------
// One run
// ---------------------------------------------------------------------------
export async function runOnce(env: Env, now: number, call: Fetcher, state: State = freshState()): Promise<Report> {
  const mode = modeOf(env);
  const report: Report = { mode, ran: false, reason: "", would: [], placed: [], messages: [] };
  const label = labelFor(env, mode);
  if (env.WATCHDOG_TEST === "missed_bot1") {
    // Test mode: only the watchdog, with a pretend missed run. No Alpaca call at all, so no order can be touched.
    const text = bot1LateText(TEST_MISSED_MINUTES, label, true);
    report.messages.push(text);
    await telegram(call, env, text);
    return { ...report, reason: "watchdog test" };
  }
  if (mode === "off") return { ...report, reason: "RT_MODE is off" };
  alpaca.requirePaperKey(env.ALPACA_KEY);
  const keys = { key: env.ALPACA_KEY, secret: env.ALPACA_SECRET };
  const time = await alpaca.clock(call, keys);
  if (!time.isOpen) {
    addFeed(state, now, "market closed, nothing to do");
    return { ...report, reason: "market closed" };
  }
  report.ran = true;
  const control = parseControl(await fetchControl(call, state), env);
  await watchBot1(call, env, state, now, label);

  const timeframe = env.RT_TIMEFRAME === "1Min" ? "1Min" : "5Min";
  const minutes = minutesOf(timeframe);
  const [account, bars, position, orders] = await Promise.all([
    alpaca.account(call, keys), alpaca.bars(call, keys, SYMBOL, timeframe, now), alpaca.position(call, keys, SYMBOL),
    alpaca.ordersSince(call, keys, newYorkMidnight(now)),
  ]);
  const daily = needsDaily(control.fixes) || (control.shadow && needsDaily(control.shadow)) ? await dailyFor(call, keys, state, now) : null;
  const base = { now, bars, minutes, position, orders, equity: account.equity, lastEquity: account.lastEquity, daily };
  const plan = decide({ ...base, fixes: control.fixes });
  report.reason = plan.notes.join("; ");
  const series = warmedUp(completedBars(bars, now, minutes), now).bars;
  const dry = mode === "dry";
  const lines: { text: string; detail?: string }[] = [];

  // A buy waits for the liquidity check (if on) and then the AI. Everything else goes straight through.
  let actions = plan.actions;
  let verdict: (Verdict & { cached: boolean }) | null = null;
  const buy = plan.actions.find((action): action is Extract<Action, { kind: "buy" }> => action.kind === "buy");
  if (buy) {
    let spread: number | null = null;
    let blocked = "";
    if (control.fixes.liquidity_filter) {
      try {
        const quote = await alpaca.latestQuote(call, keys, SYMBOL);
        spread = (quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2);
        if (spreadTooWide(quote.bid, quote.ask)) blocked = "Fix filter: wide spread → no entry";
      } catch {
        blocked = "Fix filter: no quote → no entry";
      }
    }
    if (blocked) {
      actions = actions.filter((action) => action !== buy);
      lines.push({ text: blocked });
    } else if (control.aiMode !== "off") {
      verdict = await aiVerdict(call, env, state, control, buy, series, plan, now, spread, label);
      if (control.aiMode === "enforce" && verdict.decision !== "APPROVE") {
        actions = actions.filter((action) => action !== buy);
        lines.push({ text: `SPY: EMA9 crossed above EMA21 → buy signal → ${aiWords(verdict)} → no trade` });
      }
    }
  }

  for (const signal of plan.signals.filter((item) => justHappened(item.end, now))) {
    let text = `${label} ${signal.side === "buy" ? "🟢 BUY" : "🔴 SELL"} signal on ${SYMBOL} at $${signal.close.toFixed(2)} (${timeframe})`;
    if (signal.side === "buy" && verdict) {
      const blocking = control.aiMode === "enforce" && verdict.decision !== "APPROVE";
      text += `\nAI: ${verdict.decision} · ${verdict.reason}${blocking ? " → no trade" : ""}`;
    }
    report.messages.push(text);
    await telegram(call, env, text);
  }
  for (const order of orders.filter((item) => item.filledAt && justHappened(Date.parse(item.filledAt), now))) {
    const text = `${label} ✅ Filled: ${order.side} ${order.qty} ${SYMBOL} at $${order.filledPrice.toFixed(2)}`;
    report.messages.push(text);
    await telegram(call, env, text);
    lines.push({ text: `SPY: ${order.side} filled, ${order.qty} shares`, detail: `at $${order.filledPrice.toFixed(2)}` });
  }

  for (const action of actions) {
    let detail = "";
    if (dry) {
      report.would.push(`would ${action.kind}: ${action.why}`);
      console.log(`DRY RUN, nothing placed: ${action.kind} (${action.why})`);
    } else {
      detail = await act(call, keys, action, report, plan.limits.stopPct);
    }
    const did = (live: string, would: string) => (dry ? `${would} (dry run)` : live);
    if (action.kind === "buy") {
      const ai = verdict ? ` → ${aiWords(verdict)}` : "";
      lines.push({ text: `SPY: EMA9 crossed above EMA21 → buy signal${ai} → ${did("bought", "would buy")} ${action.qty} shares`, detail: detail || undefined });
    } else if (action.kind === "sell") {
      const why = action.why.replace("EMA(9) crossed below EMA(21)", "EMA9 crossed below EMA21");
      lines.push({ text: `SPY: ${why} → ${did("sold", "would sell")} ${action.qty} shares` });
    } else if (action.kind === "stop") {
      lines.push({ text: `SPY: ${did("placed", "would place")} a ${action.why}`, detail: `at $${action.stopPrice.toFixed(2)}` });
    } else if (action.kind === "move_stop") {
      lines.push({ text: `SPY: ${action.why}${dry ? " (dry run)" : ""}`, detail: `to $${action.stopPrice.toFixed(2)}` });
    } else {
      lines.push({ text: `SPY: unfilled entry ${did("cancelled", "would be cancelled")} at close-out time` });
    }
  }

  if (!lines.length) {
    const trend = plan.fastAbove === null ? "" : `EMA9 ${plan.fastAbove ? "above" : "below"} EMA21`;
    if (plan.notes.length) lines.push({ text: plan.notes[0].startsWith("Fix filter") ? plan.notes[0] : `SPY: ${plan.notes[0]}` });
    else if (position) lines.push({ text: `SPY: holding ${position.qty} shares, ${trend}` });
    else lines.push({ text: `SPY: ${trend}, no signal` });
  }
  for (const line of lines) addFeed(state, now, line.text, line.detail);

  if (control.shadow) {
    const shadow = decide({ ...base, fixes: control.shadow });
    const line = shadowLine(control.shadow, plan, shadow);
    if (line) addFeed(state, now, line);
  }

  // Alerts the watchdog sends from here. Each kind at most once in its window.
  if (dayChange(account.equity, account.lastEquity) <= -RT_DAILY_LOSS_PCT) {
    await alert(call, env, state, `daily_loss_${newYorkDay(now)}`,
      `${label} 🛑 Daily loss stop: this account is down 2% today, so no new entries until tomorrow.`, now, DAY);
  }
  const stopWorking = orders.some((order) => order.type === "stop" && isLiveOrder(order));
  const stopHandled = actions.some((action) => action.kind === "stop" || action.kind === "sell");
  state.nostop_runs = mode === "live" && position && !stopWorking && !stopHandled ? state.nostop_runs + 1 : 0;
  if (state.nostop_runs > NO_STOP_RUNS) {
    await alert(call, env, state, "no_stop",
      `${label} ⚠️ SPY position has had no working stop order for ${state.nostop_runs} runs.`, now, HALF_HOUR);
  }
  console.log(JSON.stringify({ mode, equity: account.equity, position, actions: actions.length, notes: plan.notes }));
  return report;
}

// The cron entry: one state read, one run, one state write. Errors are logged, fed and alerted.
export async function scheduledRun(env: Env, now: number, call: Fetcher): Promise<{ report: Report | null; error: string; state: State; saved: boolean }> {
  const testing = env.WATCHDOG_TEST === "missed_bot1";
  const state = testing ? freshState() : await loadState(env);
  const mode = modeOf(env);
  let report: Report | null = null;
  let error = "";
  try {
    report = await runOnce(env, now, call, state);
    state.heartbeat = { last_run_ms: now, last_result: report.ran ? "ok" : report.reason || "nothing to do", mode };
  } catch (failure) {
    error = scrub(env, String((failure as Error)?.message ?? failure));
    state.heartbeat = { last_run_ms: now, last_result: "error", mode };
    addFeed(state, now, `Run failed: ${maskPrices(error)}`, error);
    await alert(call, env, state, "run_error", `${labelFor(env, mode)} ⚠️ Run failed: ${error}`, now, HALF_HOUR);
  }
  pruneAlerts(state, now);
  const saved = testing ? false : await saveState(env, state);
  return { report, error, state, saved };
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

// The main bot's account, read only, for the side-by-side dashboard. Needs MAIN_ALPACA_KEY and MAIN_ALPACA_SECRET.
async function mainSummary(env: Env, call: Fetcher, now: number): Promise<Record<string, unknown> | null> {
  if (!env.MAIN_ALPACA_KEY || !env.MAIN_ALPACA_SECRET) return null;
  try {
    alpaca.requirePaperKey(env.MAIN_ALPACA_KEY);
    const keys = { key: env.MAIN_ALPACA_KEY, secret: env.MAIN_ALPACA_SECRET };
    const [account, held] = await Promise.all([alpaca.account(call, keys), alpaca.positions(call, keys)]);
    return {
      equity: account.equity, last_equity: account.lastEquity, pl_today: account.equity - account.lastEquity,
      pl_today_pct: account.lastEquity ? account.equity / account.lastEquity - 1 : 0, pl_total: account.equity - START_CASH,
      positions: held, updated_ms: now,
    };
  } catch (error) {
    console.log(`Main account not read: ${scrub(env, String((error as Error)?.message ?? error))}`);
    return { error: "could not read the main account" };
  }
}

export async function buildStatus(env: Env, now: number, call: Fetcher, unlocked: boolean): Promise<Record<string, unknown>> {
  const mode = modeOf(env);
  const timeframe = env.RT_TIMEFRAME === "1Min" ? "1Min" : "5Min";
  const minutes = minutesOf(timeframe);
  alpaca.requirePaperKey(env.ALPACA_KEY);
  const keys = { key: env.ALPACA_KEY, secret: env.ALPACA_SECRET };
  const [time, account, position, orders, bars, state, bot1] = await Promise.all([
    alpaca.clock(call, keys), alpaca.account(call, keys), alpaca.position(call, keys, SYMBOL),
    alpaca.ordersSince(call, keys, newYorkMidnight(now)), alpaca.bars(call, keys, SYMBOL, timeframe, now), loadState(env),
    mainSummary(env, call, now),
  ]);
  const control = parseControl(state.control, env);
  const { bars: series, firstToday } = warmedUp(completedBars(bars, now, minutes), now);
  const signals: Signal[] = crossovers(series, firstToday, minutes);
  const sinceBot1 = minutesSince(state.watchdog.bot1_last_run_utc, now);
  const body: Record<string, unknown> = {
    updated_ms: now, market_open: time.isOpen, next_open: time.nextOpen, mode, timeframe, symbol: SYMBOL,
    experimental: env.RT_EXPERIMENTAL === "true", locked: !unlocked,
    position, pl_today: account.equity - account.lastEquity, pl_today_pct: account.lastEquity ? account.equity / account.lastEquity - 1 : 0,
    pl_total: account.equity - START_CASH,
    equity: account.equity, signals: signals.map((signal) => ({ t: signal.t, side: signal.side })),
    fills: orders.filter((order) => order.filledAt).map(publicFill),
    rules: {
      position_pct: RT_POSITION_PCT, stop_pct: RT_STOP_PCT, max_entries: RT_MAX_ENTRIES, daily_loss_pct: RT_DAILY_LOSS_PCT,
      ema_fast: EMA_FAST, ema_slow: EMA_SLOW, no_entries_from: "15:45", flat_from: "15:55",
    },
    heartbeat: state.heartbeat,
    feed: state.feed.slice(0, FEED_SHOWN).map((item) => (unlocked ? item : { t: item.t, text: item.text })),
    ai: {
      mode: control.aiMode, failures_in_row: state.ai.failures_in_row,
      decisions: state.ai.decisions.slice(0, 20).map((item) => ({
        t: item.t, bar_t: item.bar_t, decision: item.decision, reason: unlocked ? item.reason : maskPrices(item.reason), qty: item.qty,
      })),
    },
    fixes: { active: control.fixes, shadow: control.shadow },
    watchdog: {
      bot1_last_run_utc: state.watchdog.bot1_last_run_utc, bot1_minutes_since: sinceBot1,
      late: time.isOpen && bot1IsLate(sinceBot1, now),
    },
    bot1,
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
    console.log(`${url.pathname} failed: ${scrub(env, String((error as Error)?.message ?? error))}`);
    return new Response('{"error":"could not reach Alpaca"}', { status: 502, headers });
  }
}

export default {
  async scheduled(event: { scheduledTime: number }, env: Env, context: { waitUntil(work: Promise<unknown>): void }) {
    context.waitUntil(scheduledRun(env, event.scheduledTime, fetch).then(({ report, error }) => {
      if (error) console.log(`Run failed: ${error}`);
      else if (report && !report.ran) console.log(`Nothing to do: ${report.reason}`);
    }));
  },
  fetch(request: Request, env: Env) {
    return handle(request, env, Date.now(), fetch);
  },
};
