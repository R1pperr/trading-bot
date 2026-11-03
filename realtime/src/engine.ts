// engine.ts: the trading rules, as plain functions with no network calls, so they can be tested
// on small hand-made bars. index.ts does the talking to Alpaca and Telegram.
//
// The signal: EMA(9) and EMA(21) of the close on completed SPY bars. BUY when the fast line crosses
// above the slow one, SELL when it crosses back below. Long only. A bar still forming is never
// looked at, so a signal can't appear and then vanish.

export const SYMBOL = "SPY";
export const EMA_FAST = 9;
export const EMA_SLOW = 21;
export const RT_POSITION_PCT = 0.25;       // share of this account's equity per trade, in whole shares
export const RT_STOP_PCT = 0.02;           // protective stop this far below the entry
export const RT_MAX_ENTRIES = 20;          // entries per day
export const RT_DAILY_LOSS_PCT = 0.02;     // no new entries once the account is down this much today
export const NO_ENTRIES_FROM = 15 * 60 + 45;     // 15:45 New York time
export const FLAT_FROM = 15 * 60 + 55;           // 15:55 New York time
const SESSION_OPEN = 9 * 60 + 30;
const SESSION_CLOSE = 16 * 60;
const GRACE_BARS = 2;                      // a signal stays good for two bars, in case one run was missed

export type Bar = { t: number; o: number; h: number; l: number; c: number };
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
  | { kind: "cancel"; id: string; why: string };
export type Input = {
  now: number; bars: Bar[]; minutes: number; position: Position; orders: Order[];
  equity: number; lastEquity: number;
};

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
export function sharesFor(equity: number, price: number): number {
  return price > 0 ? Math.max(0, Math.floor((equity * RT_POSITION_PCT) / price)) : 0;
}

export function stopPriceFor(entry: number): number {
  return Math.round(entry * (1 - RT_STOP_PCT) * 100) / 100;
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
export function decide(input: Input): { actions: Action[]; signals: Signal[]; notes: string[] } {
  const { now, minutes, position, orders } = input;
  const finished = completedBars(input.bars, now, minutes);
  const { bars, firstToday } = warmedUp(finished, now);
  const signals = crossovers(bars, firstToday, minutes);
  const actions: Action[] = [];
  const notes: string[] = [];
  const minute = newYorkMinute(now);
  const stops = orders.filter((order) => order.type === "stop" && isLive(order));
  const openBuys = orders.filter((order) => order.side === "buy" && isLive(order));
  const openSells = orders.filter((order) => order.side === "sell" && order.type !== "stop" && isLive(order));
  const used = new Set(orders.map((order) => order.clientId));
  const latest = bars[bars.length - 1];
  const stopIds = stops.map((order) => order.id);

  if (minute >= FLAT_FROM) {
    for (const order of openBuys) actions.push({ kind: "cancel", id: order.id, why: "close-out time" });
    if (position && !openSells.length) {
      actions.push({
        kind: "sell", qty: position.qty, clientId: clientOrderId(SYMBOL, newYorkMidnight(now), "close"),
        cancel: stopIds, why: "flat by 15:55",
      });
    }
    return { actions, signals, notes };
  }
  if (!latest) return { actions, signals, notes: ["no finished bars yet"] };

  const lastSignal = signals[signals.length - 1];
  const fresh = lastSignal !== undefined && latest.t - lastSignal.t < GRACE_BARS * minutes * 60000;

  if (position) {
    const closes = bars.map((bar) => bar.c);
    const fast = ema(closes, EMA_FAST);
    const slow = ema(closes, EMA_SLOW);
    const down = fast[fast.length - 1] < slow[slow.length - 1];
    if (down && !openSells.length) {
      const barTime = lastSignal?.side === "sell" ? lastSignal.t : latest.t;
      actions.push({
        kind: "sell", qty: position.qty, clientId: clientOrderId(SYMBOL, barTime, "sell"), cancel: stopIds,
        why: "EMA(9) crossed below EMA(21)",
      });
      return { actions, signals, notes };
    }
    if (!stops.length && !openSells.length) {
      const level = stopPriceFor(position.avgEntry);
      if (latest.c <= level) {
        actions.push({
          kind: "sell", qty: position.qty, clientId: clientOrderId(SYMBOL, latest.t, "sell"), cancel: [],
          why: "already under the stop level",
        });
      } else {
        const entry = orders.filter((order) => order.side === "buy" && order.filledAt).pop();
        const stamp = entry ? Date.parse(entry.filledAt) : latest.t;
        actions.push({
          kind: "stop", qty: position.qty, stopPrice: level, clientId: clientOrderId(SYMBOL, stamp, "stop"),
          why: "protective stop 2% below the entry",
        });
      }
    }
    return { actions, signals, notes };
  }

  if (lastSignal?.side !== "buy" || !fresh || openBuys.length) return { actions, signals, notes };
  const id = clientOrderId(SYMBOL, lastSignal.t, "buy");
  if (used.has(id)) return { actions, signals, notes };           // already taken, or stopped out since
  if (minute >= NO_ENTRIES_FROM) return { actions, signals, notes: ["no new entries after 15:45"] };
  if (entriesToday(orders) >= RT_MAX_ENTRIES) return { actions, signals, notes: ["daily entry limit reached"] };
  if (dayChange(input.equity, input.lastEquity) <= -RT_DAILY_LOSS_PCT) {
    return { actions, signals, notes: ["down 2% today, so no new entries"] };
  }
  const qty = sharesFor(input.equity, latest.c);
  if (qty < 1) return { actions, signals, notes: ["not enough equity for one share"] };
  actions.push({ kind: "buy", qty, clientId: id, why: "EMA(9) crossed above EMA(21)" });
  return { actions, signals, notes };
}
