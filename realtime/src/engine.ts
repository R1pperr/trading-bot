// engine.ts: the trading rules, as plain functions with no network calls, so they can be tested
// on small hand-made bars. index.ts does the talking to Alpaca and Telegram.
//
// The signal: EMA(9) and EMA(21) of the close on completed SPY bars. BUY when the fast line crosses
// above the slow one, SELL when it crosses back below. Long only. A bar still forming is never
// looked at, so a signal can't appear and then vanish.

export const SYMBOL = "SPY";
export const EMA_FAST = 9;
export const EMA_SLOW = 21;
// The locked rules. A fix from the menu below can make each one tighter, never looser.
export const RT_POSITION_PCT = 0.25;       // share of this account's equity per trade, in whole shares
export const RT_STOP_PCT = 0.02;           // protective stop this far below the entry
export const RT_MAX_ENTRIES = 20;          // entries per day
export const RT_DAILY_LOSS_PCT = 0.02;     // no new entries once the account is down this much today
export const NO_ENTRIES_FROM = 15 * 60 + 45;     // 15:45 New York time
export const FLAT_FROM = 15 * 60 + 55;           // 15:55 New York time
export const SESSION_OPEN = 9 * 60 + 30;
const SESSION_CLOSE = 16 * 60;
const GRACE_BARS = 2;                      // a signal stays good for two bars, in case one run was missed

// The fix menu's standard settings. They are never fine-tuned.
export const TIGHT_STOP_PCT = 0.01;                    // tighter stop: 1% instead of 2%
export const TIME_EXIT_NO_ENTRIES_FROM = 15 * 60 + 20; // earlier time exit: no entries from 15:20
export const TIME_EXIT_FLAT_FROM = 15 * 60 + 30;       // and flat by 15:30
export const GAP_LIMIT = 0.02;                         // gap filter: skip after an overnight gap over 2%
export const VOL_BAND = 0.2;                           // volatility filter: top or bottom 20% of the last year
export const VOL_DAYS = 20;                            // 20-day volatility
export const VOL_YEAR = 252;                           // compared with the last 252 trading days
export const TREND_DAYS = 200;                         // trend filter: the 200-day average
export const LIQUIDITY_BARS = 20;                      // liquidity filter: the signal bar's volume against the 20 before it
export const LIQUIDITY_SHARE = 0.5;                    // under half their average is too thin
export const MAX_SPREAD_PCT = 0.0005;                  // a spread over 0.05% of the price is too wide

export type Bar = { t: number; o: number; h: number; l: number; c: number; v?: number };
export type Signal = { t: number; end: number; side: "buy" | "sell"; close: number };
export type Position = { qty: number; avgEntry: number } | null;
export type Order = {
  id: string; clientId: string; side: string; type: string; status: string; qty: number;
  stopPrice: number; filledAt: string; filledPrice: number;
};
export type Action =
  | { kind: "buy"; qty: number; clientId: string; why: string }
  | { kind: "sell"; qty: number; clientId: string; cancel: string[]; why: string }
  | { kind: "stop"; qty: number; stopPrice: number; clientId: string; why: string }
  | { kind: "move_stop"; id: string; stopPrice: number; why: string }
  | { kind: "cancel"; id: string; why: string };

// Every switch is off unless the daily job turns it on. See docs/data/control.json.
export type FixSet = {
  trend_filter: "off" | "above" | "below"; vol_filter: boolean; time_filter: 0 | 15 | 30; gap_filter: boolean;
  liquidity_filter: boolean; confirmation: boolean; time_exit: boolean; trailing_stop: boolean; tight_stop: boolean;
  vol_sizing: boolean;
};
export const NO_FIXES: FixSet = Object.freeze({
  trend_filter: "off", vol_filter: false, time_filter: 0, gap_filter: false, liquidity_filter: false,
  confirmation: false, time_exit: false, trailing_stop: false, tight_stop: false, vol_sizing: false,
}) as FixSet;

// What yesterday's daily bars say. null means not known, and a fix that needs it then blocks the entry.
export type Daily = {
  day: string; aboveSma200: boolean | null; volRank: number | null; volScale: number | null; prevClose: number | null;
};
export type Limits = {
  positionPct: number; stopPct: number; maxEntries: number; dailyLossPct: number; noEntriesFrom: number; flatFrom: number;
};
export type Input = {
  now: number; bars: Bar[]; minutes: number; position: Position; orders: Order[];
  equity: number; lastEquity: number; fixes?: FixSet; daily?: Daily | null;
};

// Anything strange in the control file counts as off: only the menu's own values get through.
export function cleanFixes(raw: unknown): FixSet {
  const given = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const flag = (key: string) => given[key] === true;
  return {
    trend_filter: given.trend_filter === "above" || given.trend_filter === "below" ? given.trend_filter : "off",
    vol_filter: flag("vol_filter"),
    time_filter: given.time_filter === 15 || given.time_filter === 30 ? given.time_filter : 0,
    gap_filter: flag("gap_filter"), liquidity_filter: flag("liquidity_filter"), confirmation: flag("confirmation"),
    time_exit: flag("time_exit"), trailing_stop: flag("trailing_stop"), tight_stop: flag("tight_stop"), vol_sizing: flag("vol_sizing"),
  };
}

export function anyFix(fixes: FixSet): boolean {
  return fixLabel(fixes) !== "no fixes";
}

export function fixLabel(fixes: FixSet): string {
  const names: string[] = [];
  if (fixes.trend_filter !== "off") names.push(`trend filter (only ${fixes.trend_filter} the 200-day average)`);
  if (fixes.vol_filter) names.push("volatility filter");
  if (fixes.time_filter) names.push(`no entries in the first ${fixes.time_filter} minutes`);
  if (fixes.gap_filter) names.push("gap filter");
  if (fixes.liquidity_filter) names.push("liquidity filter");
  if (fixes.confirmation) names.push("confirmation bar");
  if (fixes.time_exit) names.push("earlier time exit");
  if (fixes.trailing_stop) names.push("trailing stop");
  if (fixes.tight_stop) names.push("tighter stop");
  if (fixes.vol_sizing) names.push("volatility sizing");
  return names.length ? names.join(", ") : "no fixes";
}

export function needsDaily(fixes: FixSet): boolean {
  return fixes.trend_filter !== "off" || fixes.vol_filter || fixes.gap_filter || fixes.vol_sizing;
}

// The locked rules with any fixes applied. Each one is clamped, so a fix can only tighten it.
export function limitsFor(fixes: FixSet = NO_FIXES, daily: Daily | null = null): Limits {
  const scale = fixes.vol_sizing && daily?.volScale ? Math.min(1, Math.max(0, daily.volScale)) : 1;
  return {
    positionPct: Math.min(RT_POSITION_PCT, RT_POSITION_PCT * scale),
    stopPct: Math.min(RT_STOP_PCT, fixes.tight_stop ? TIGHT_STOP_PCT : RT_STOP_PCT),
    maxEntries: RT_MAX_ENTRIES,
    dailyLossPct: RT_DAILY_LOSS_PCT,
    noEntriesFrom: Math.min(NO_ENTRIES_FROM, fixes.time_exit ? TIME_EXIT_NO_ENTRIES_FROM : NO_ENTRIES_FROM),
    flatFrom: Math.min(FLAT_FROM, fixes.time_exit ? TIME_EXIT_FLAT_FROM : FLAT_FROM),
  };
}

// The daily inputs, from daily bars that ended before today. Same sums as rt_backtest.daily_contexts.
export function dailyContext(daily: Bar[], today: string): Daily {
  const closes = daily.filter((bar) => newYorkDay(bar.t) < today).map((bar) => bar.c);
  const result: Daily = { day: today, aboveSma200: null, volRank: null, volScale: null, prevClose: null };
  if (!closes.length) return result;
  const last = closes[closes.length - 1];
  result.prevClose = last;
  if (closes.length >= TREND_DAYS) {
    let sum = 0;
    for (let i = closes.length - TREND_DAYS; i < closes.length; i += 1) sum += closes[i];
    result.aboveSma200 = last > sum / TREND_DAYS;
  }
  const vols: number[] = [];
  for (let end = closes.length - 1; end >= VOL_DAYS && vols.length < VOL_YEAR; end -= 1) {
    const returns: number[] = [];
    for (let i = end - VOL_DAYS + 1; i <= end; i += 1) returns.push(closes[i] / closes[i - 1] - 1);
    const mean = returns.reduce((a, b) => a + b, 0) / returns.length;
    const variance = returns.reduce((a, b) => a + (b - mean) ** 2, 0) / (returns.length - 1);
    vols.push(Math.sqrt(variance));
  }
  if (vols.length === VOL_YEAR && vols[0] > 0) {
    const current = vols[0];
    result.volRank = vols.filter((value) => value < current).length / VOL_YEAR;
    const sorted = [...vols].sort((a, b) => a - b);
    const median = (sorted[VOL_YEAR / 2 - 1] + sorted[VOL_YEAR / 2]) / 2;
    result.volScale = median / current;
  }
  return result;
}

// Why an entry is blocked by a fix, in words with no prices, or null if it may go ahead.
export function entryBlock(
  fixes: FixSet, daily: Daily | null, bars: Bar[], firstToday: number, signalIndex: number, minute: number,
): string | null {
  if (fixes.time_filter && minute < SESSION_OPEN + fixes.time_filter) {
    return `Fix filter: first ${fixes.time_filter} minutes of the session → no entry`;
  }
  if (needsDaily(fixes) && !daily) return "Fix filter: no daily data → no entry";
  if (fixes.trend_filter !== "off") {
    if (daily!.aboveSma200 === null) return "Fix filter: no 200-day average yet → no entry";
    if (fixes.trend_filter === "above" && !daily!.aboveSma200) return "Fix filter: SPY below its 200-day average → no entry";
    if (fixes.trend_filter === "below" && daily!.aboveSma200) return "Fix filter: SPY above its 200-day average → no entry";
  }
  if (fixes.vol_filter) {
    if (daily!.volRank === null) return "Fix filter: no volatility history yet → no entry";
    if (daily!.volRank >= 1 - VOL_BAND) return "Fix filter: volatility in the top 20% of the year → no entry";
    if (daily!.volRank < VOL_BAND) return "Fix filter: volatility in the bottom 20% of the year → no entry";
  }
  if (fixes.gap_filter) {
    const todayOpen = bars[firstToday]?.o;
    if (!daily!.prevClose || !todayOpen) return "Fix filter: no gap data → no entry";
    if (Math.abs(todayOpen / daily!.prevClose - 1) > GAP_LIMIT) return "Fix filter: gap over 2% → no entry";
  }
  if (fixes.vol_sizing && !daily!.volScale) return "Fix filter: no volatility history yet → no entry";
  if (fixes.liquidity_filter) {
    const before = bars.slice(Math.max(0, signalIndex - LIQUIDITY_BARS), signalIndex);
    const volume = bars[signalIndex]?.v;
    if (before.length < LIQUIDITY_BARS || volume === undefined || before.some((bar) => bar.v === undefined)) {
      return "Fix filter: no volume data → no entry";
    }
    const average = before.reduce((sum, bar) => sum + (bar.v ?? 0), 0) / before.length;
    if (volume < LIQUIDITY_SHARE * average) return "Fix filter: thin volume → no entry";
  }
  return null;
}

export function spreadTooWide(bid: number, ask: number): boolean {
  const mid = (bid + ask) / 2;
  return !(mid > 0) || ask < bid || (ask - bid) / mid > MAX_SPREAD_PCT;
}

// ---------------------------------------------------------------------------
// New York time, by arithmetic: Intl is slow, and each run only gets 10 ms of CPU.
// ---------------------------------------------------------------------------
function nthSunday(year: number, month: number, nth: number): number {
  const first = new Date(Date.UTC(year, month, 1)).getUTCDay();
  return 1 + ((7 - first) % 7) + (nth - 1) * 7;
}

// US clocks change at 02:00 local time, on the 2nd Sunday of March and the 1st Sunday of November.
export function newYorkOffsetMinutes(ms: number): number {
  const year = new Date(ms).getUTCFullYear();
  const start = Date.UTC(year, 2, nthSunday(year, 2, 2), 7);
  const end = Date.UTC(year, 10, nthSunday(year, 10, 1), 6);
  return ms >= start && ms < end ? -240 : -300;
}

export function newYorkMinute(ms: number): number {
  const local = ms + newYorkOffsetMinutes(ms) * 60000;
  return Math.floor(local / 60000) % 1440;
}

export function newYorkDay(ms: number): string {
  return new Date(ms + newYorkOffsetMinutes(ms) * 60000).toISOString().slice(0, 10);
}

export function newYorkMidnight(ms: number): number {
  const local = ms + newYorkOffsetMinutes(ms) * 60000;
  return local - (local % 86400000) - newYorkOffsetMinutes(ms) * 60000;
}

// ---------------------------------------------------------------------------
// Indicators and signals
// ---------------------------------------------------------------------------
export function ema(values: number[], period: number): number[] {
  const weight = 2 / (period + 1);
  const result: number[] = [];
  for (let i = 0; i < values.length; i += 1) {
    result.push(i === 0 ? values[0] : values[i] * weight + result[i - 1] * (1 - weight));
  }
  return result;
}

// Regular-session bars that have finished. Alpaca's free feed also has pre-market bars.
export function completedBars(bars: Bar[], now: number, minutes: number): Bar[] {
  return bars.filter((bar) => {
    const minute = newYorkMinute(bar.t);
    return bar.t + minutes * 60000 <= now && minute >= SESSION_OPEN && minute < SESSION_CLOSE;
  });
}

// Today's bars and the session before them. The older bars only warm the averages up.
export function warmedUp(bars: Bar[], now: number): { bars: Bar[]; firstToday: number } {
  const today = newYorkDay(now);
  const days: string[] = [];
  for (const bar of bars) {
    const day = newYorkDay(bar.t);
    if (days[days.length - 1] !== day) days.push(day);
  }
  const past = days.filter((day) => day < today);
  const start = past[past.length - 1] ?? today;
  const kept = bars.filter((bar) => newYorkDay(bar.t) >= start);
  const first = kept.findIndex((bar) => newYorkDay(bar.t) === today);
  return { bars: kept, firstToday: first < 0 ? kept.length : first };
}

export function crossovers(bars: Bar[], firstToday: number, minutes: number): Signal[] {
  const closes = bars.map((bar) => bar.c);
  const fast = ema(closes, EMA_FAST);
  const slow = ema(closes, EMA_SLOW);
  const signals: Signal[] = [];
  for (let i = Math.max(firstToday, 1); i < bars.length; i += 1) {
    const was = fast[i - 1] - slow[i - 1];
    const is = fast[i] - slow[i];
    if (was <= 0 && is > 0) signals.push({ t: bars[i].t, end: bars[i].t + minutes * 60000, side: "buy", close: bars[i].c });
    if (was >= 0 && is < 0) signals.push({ t: bars[i].t, end: bars[i].t + minutes * 60000, side: "sell", close: bars[i].c });
  }
  return signals;
}

// ---------------------------------------------------------------------------
// Sizing and ids
// ---------------------------------------------------------------------------
export function sharesFor(equity: number, price: number, pct = RT_POSITION_PCT): number {
  const share = Math.min(RT_POSITION_PCT, pct);
  return price > 0 ? Math.max(0, Math.floor((equity * share) / price)) : 0;
}

export function stopPriceFor(entry: number, pct = RT_STOP_PCT): number {
  return Math.round(entry * (1 - Math.min(RT_STOP_PCT, pct)) * 100) / 100;
}

// The id comes from the symbol, the bar's time and the side, so a repeated run can't place the
// same order twice: Alpaca refuses a second order with an id it has already seen.
export function clientOrderId(symbol: string, barTime: number, side: string): string {
  const stamp = new Date(barTime).toISOString().slice(0, 16).replace(/[-:]/g, "");
  return `rt-${symbol}-${stamp}-${side}`;
}

const isLive = (order: Order) => ["new", "accepted", "pending_new", "partially_filled", "held"].includes(order.status);
const counts = (order: Order) => !["canceled", "rejected", "expired"].includes(order.status);

export function entriesToday(orders: Order[]): number {
  return orders.filter((order) => order.side === "buy" && order.clientId.startsWith("rt-") && counts(order)).length;
}

export function dayChange(equity: number, lastEquity: number): number {
  return lastEquity > 0 ? equity / lastEquity - 1 : 0;
}

// ---------------------------------------------------------------------------
// The decision for one run. Everything comes from Alpaca, so nothing is remembered between runs.
// ---------------------------------------------------------------------------
const clock = (minute: number) => `${Math.floor(minute / 60)}:${String(minute % 60).padStart(2, "0")}`;

export type Plan = {
  actions: Action[]; signals: Signal[]; notes: string[]; fastAbove: boolean | null; limits: Limits;
  signalIndex: number;          // where the acted-on buy signal sits in the warmed-up bars, or -1
};

export function decide(input: Input): Plan {
  const { now, minutes, position, orders } = input;
  const fixes = input.fixes ?? NO_FIXES;
  const daily = input.daily ?? null;
  const limits = limitsFor(fixes, daily);
  const finished = completedBars(input.bars, now, minutes);
  const { bars, firstToday } = warmedUp(finished, now);
  const signals = crossovers(bars, firstToday, minutes);
  const actions: Action[] = [];
  const minute = newYorkMinute(now);
  const stops = orders.filter((order) => order.type === "stop" && isLive(order));
  const openBuys = orders.filter((order) => order.side === "buy" && isLive(order));
  const openSells = orders.filter((order) => order.side === "sell" && order.type !== "stop" && isLive(order));
  const used = new Set(orders.map((order) => order.clientId));
  const latest = bars[bars.length - 1];
  const stopIds = stops.map((order) => order.id);
  const closes = bars.map((bar) => bar.c);
  const fast = ema(closes, EMA_FAST);
  const slow = ema(closes, EMA_SLOW);
  const fastAbove = latest ? fast[fast.length - 1] > slow[slow.length - 1] : null;
  const plan = (notes: string[] = [], signalIndex = -1): Plan => ({ actions, signals, notes, fastAbove, limits, signalIndex });

  if (minute >= limits.flatFrom) {
    for (const order of openBuys) actions.push({ kind: "cancel", id: order.id, why: "close-out time" });
    if (position && !openSells.length) {
      actions.push({
        kind: "sell", qty: position.qty, clientId: clientOrderId(SYMBOL, newYorkMidnight(now), "close"),
        cancel: stopIds, why: `flat by ${clock(limits.flatFrom)}`,
      });
    }
    return plan();
  }
  if (!latest) return plan(["no finished bars yet"]);

  const lastSignal = signals[signals.length - 1];
  const age = lastSignal !== undefined ? latest.t - lastSignal.t : Infinity;
  const bar = minutes * 60000;
  // With the confirmation fix a crossover waits one more finished bar, so the window moves one bar later.
  const fresh = fixes.confirmation ? age >= bar && age < (GRACE_BARS + 1) * bar : age < GRACE_BARS * bar;

  if (position) {
    const n = fast.length;
    const below = (i: number) => i >= 0 && fast[i] < slow[i];
    const down = fixes.confirmation ? below(n - 1) && below(n - 2) : below(n - 1);
    if (down && !openSells.length) {
      const barTime = lastSignal?.side === "sell" ? lastSignal.t : latest.t;
      actions.push({
        kind: "sell", qty: position.qty, clientId: clientOrderId(SYMBOL, barTime, "sell"), cancel: stopIds,
        why: "EMA(9) crossed below EMA(21)",
      });
      return plan();
    }
    const entry = orders.filter((order) => order.side === "buy" && order.filledAt).pop();
    let level = stopPriceFor(position.avgEntry, limits.stopPct);
    if (fixes.trailing_stop) {
      const since = entry ? Date.parse(entry.filledAt) : bars[firstToday]?.t ?? latest.t;
      let high = 0;
      for (const item of bars) if (item.t + bar > since) high = Math.max(high, item.c);
      if (high > 0) level = Math.max(level, stopPriceFor(high, limits.stopPct));
    }
    const percent = `${Math.round(limits.stopPct * 100)}%`;
    if (!stops.length && !openSells.length) {
      if (latest.c <= level) {
        actions.push({
          kind: "sell", qty: position.qty, clientId: clientOrderId(SYMBOL, latest.t, "sell"), cancel: [],
          why: "already under the stop level",
        });
      } else {
        const stamp = entry ? Date.parse(entry.filledAt) : latest.t;
        actions.push({
          kind: "stop", qty: position.qty, stopPrice: level, clientId: clientOrderId(SYMBOL, stamp, "stop"),
          why: fixes.trailing_stop ? `trailing stop ${percent} below the high` : `protective stop ${percent} below the entry`,
        });
      }
    } else if ((fixes.trailing_stop || fixes.tight_stop) && stops.length && !openSells.length) {
      // A stop only ever moves up. If the price is already under the new level, sell instead.
      const highest = stops.reduce((best, order) => (order.stopPrice > best.stopPrice ? order : best));
      if (latest.c <= level) {
        actions.push({
          kind: "sell", qty: position.qty, clientId: clientOrderId(SYMBOL, latest.t, "sell"), cancel: stopIds,
          why: "already under the stop level",
        });
      } else if (highest.stopPrice < level - 0.005) {
        actions.push({ kind: "move_stop", id: highest.id, stopPrice: level, why: fixes.trailing_stop ? "trailing stop moved up" : "tighter stop" });
      }
    }
    return plan();
  }

  if (lastSignal?.side !== "buy" || !fresh || openBuys.length) {
    if (fixes.confirmation && lastSignal?.side === "buy" && age < bar && !openBuys.length) {
      return plan(["waiting one more bar to confirm the crossover"]);
    }
    return plan();
  }
  const signalIndex = bars.findIndex((item) => item.t === lastSignal.t);
  if (fixes.confirmation) {
    const next = signalIndex + 1;
    if (!(fast[next] > slow[next])) return plan(["crossover not confirmed → no entry"]);
  }
  const id = clientOrderId(SYMBOL, lastSignal.t, "buy");
  if (used.has(id)) return plan();           // already taken, or stopped out since
  if (minute >= limits.noEntriesFrom) return plan([`no new entries after ${clock(limits.noEntriesFrom)}`]);
  if (entriesToday(orders) >= limits.maxEntries) return plan(["daily entry limit reached"]);
  if (dayChange(input.equity, input.lastEquity) <= -limits.dailyLossPct) {
    return plan(["down 2% today, so no new entries"]);
  }
  const blocked = entryBlock(fixes, daily, bars, firstToday, signalIndex, minute);
  if (blocked) return plan([blocked], signalIndex);
  const qty = sharesFor(input.equity, latest.c, limits.positionPct);
  if (qty < 1) return plan(["not enough equity for one share"]);
  actions.push({ kind: "buy", qty, clientId: id, why: "EMA(9) crossed above EMA(21)" });
  return plan([], signalIndex);
}
