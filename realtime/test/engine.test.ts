// The engine's rules on small hand-made bars. No network here.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  FLAT_FROM, NO_ENTRIES_FROM, RT_MAX_ENTRIES, clientOrderId, completedBars, crossovers, dayChange, decide, ema, entriesToday, newYorkDay, spreadTooWide,
  newYorkMidnight, newYorkMinute, sharesFor, stopPriceFor, warmedUp,
} from "../src/engine.ts";
import type { Bar, Input, Order } from "../src/engine.ts";

const MINUTE = 60000;
const open = (day: string) => Date.parse(`${day}T14:30:00Z`);          // 09:30 in New York, in winter

function bar(t: number, close: number): Bar {
  return { t, o: close, h: close + 0.1, l: close - 0.1, c: close };
}

// Yesterday flat, today down then up, in 5-minute bars. EMA(9) crosses above EMA(21) on the way up.
function dipThenRise(extra = 0): Bar[] {
  const bars: Bar[] = [];
  for (let i = 0; i < 78; i += 1) bars.push(bar(open("2026-11-02") + i * 5 * MINUTE, 100));
  const today = [...Array.from({ length: 12 }, (_, i) => 100 - i * 0.2), ...Array.from({ length: 14 + extra }, (_, i) => 98 + i * 0.5)];
  today.forEach((close, i) => bars.push(bar(open("2026-11-03") + i * 5 * MINUTE, close)));
  return bars;
}

function firstBuy(bars: Bar[]) {
  const late = open("2026-11-03") + 6.4 * 3600000;
  const { bars: warmed, firstToday } = warmedUp(completedBars(bars, late, 5), late);
  return crossovers(warmed, firstToday, 5).find((signal) => signal.side === "buy")!;
}

function input(now: number, changes: Partial<Input> = {}): Input {
  return { now, bars: dipThenRise(), minutes: 5, position: null, orders: [], equity: 100000, lastEquity: 100000, ...changes };
}

function order(changes: Partial<Order>): Order {
  return { id: "x", clientId: "", side: "buy", type: "market", status: "filled", qty: 35, stopPrice: 0, filledAt: "", filledPrice: 0, ...changes };
}

test("EMA matches a worked example", () => {
  assert.deepEqual(ema([1, 2, 3], 3), [1, 1.5, 2.25]);
});

test("an EMA of a flat line stays flat", () => {
  assert.deepEqual(ema([5, 5, 5, 5], 9), [5, 5, 5, 5]);
});

test("a rise through the slow average is a BUY and a fall back is a SELL", () => {
  const closes = [...Array(40).fill(100), ...Array.from({ length: 30 }, (_, i) => 100 + i), ...Array.from({ length: 30 }, (_, i) => 129 - i)];
  const bars = closes.map((close, i) => bar(open("2026-11-03") + i * MINUTE, close));
  assert.deepEqual(crossovers(bars, 0, 1).map((signal) => signal.side), ["buy", "sell"]);
});

test("a signal is only taken from finished bars", () => {
  const signal = firstBuy(dipThenRise());
  assert.equal(decide(input(signal.end - 1000)).actions.length, 0);
  assert.equal(decide(input(signal.end)).actions[0].kind, "buy");
});

test("signals never repaint: later bars can't change or remove an earlier one", () => {
  const signal = firstBuy(dipThenRise());
  const atTheTime = decide(input(signal.end)).signals;
  const later = decide(input(signal.end + 25 * MINUTE)).signals.filter((item) => item.t <= signal.t);
  assert.deepEqual(later, atTheTime);
});

test("a bar still forming changes nothing, however wild it is", () => {
  const signal = firstBuy(dipThenRise());
  const bars = dipThenRise().filter((b) => b.t <= signal.t);
  const wild = [...bars, bar(signal.t + 5 * MINUTE, 1)];
  assert.deepEqual(decide(input(signal.end + MINUTE, { bars: wild })).signals, decide(input(signal.end + MINUTE, { bars })).signals);
});

test("yesterday only warms the averages up, so it can't make a signal today", () => {
  assert.equal(newYorkDay(firstBuy(dipThenRise()).t), "2026-11-03");
});

test("pre-market bars are ignored", () => {
  const premarket = bar(open("2026-11-03") - 30 * MINUTE, 999);
  assert.equal(completedBars([premarket], open("2026-11-03") + 3600000, 5).length, 0);
});

test("New York time is right in winter and in summer", () => {
  assert.equal(newYorkMinute(Date.parse("2026-11-03T14:30:00Z")), 9 * 60 + 30);
  assert.equal(newYorkMinute(Date.parse("2026-07-07T13:30:00Z")), 9 * 60 + 30);
});

test("the clock change is handled on the right days", () => {
  assert.equal(newYorkMinute(Date.parse("2026-03-06T14:30:00Z")), 9 * 60 + 30);     // the Friday before: still winter time
  assert.equal(newYorkMinute(Date.parse("2026-03-09T13:30:00Z")), 9 * 60 + 30);     // the Monday after: summer time
  assert.equal(newYorkMinute(Date.parse("2026-10-30T13:30:00Z")), 9 * 60 + 30);     // still summer time
  assert.equal(newYorkMinute(Date.parse("2026-11-02T14:30:00Z")), 9 * 60 + 30);     // winter time again
});

test("the New York day starts at midnight there", () => {
  assert.equal(new Date(newYorkMidnight(Date.parse("2026-11-03T14:30:00Z"))).toISOString(), "2026-11-03T05:00:00.000Z");
});

test("no new entries from 15:45", () => {
  const signal = firstBuy(dipThenRise());
  const late = open("2026-11-03") + (NO_ENTRIES_FROM - 570) * MINUTE;
  const moved = dipThenRise().map((b) => (b.t >= open("2026-11-03") ? { ...b, t: b.t + (late - signal.end) } : b));
  const plan = decide(input(late, { bars: moved }));
  assert.equal(plan.actions.length, 0);
  assert.deepEqual(plan.notes, ["no new entries after 15:45"]);
  const stillOk = moved.map((b) => (b.t >= open("2026-11-03") ? { ...b, t: b.t - MINUTE } : b));
  assert.equal(decide(input(late - MINUTE, { bars: stillOk })).actions[0].kind, "buy");
});

test("flat by 15:55: the stop is cancelled and the position sold", () => {
  const closing = open("2026-11-03") + (FLAT_FROM - 570) * MINUTE;
  const stop = order({ id: "stop-1", side: "sell", type: "stop", status: "new", clientId: "rt-SPY-a-stop" });
  const plan = decide(input(closing, { position: { qty: 35, avgEntry: 700 }, orders: [stop] }));
  assert.equal(plan.actions.length, 1);
  const action = plan.actions[0];
  assert.equal(action.kind, "sell");
  if (action.kind === "sell") assert.deepEqual(action.cancel, ["stop-1"]);
});

test("at 15:55 an unfilled entry is cancelled", () => {
  const closing = open("2026-11-03") + (FLAT_FROM - 570) * MINUTE;
  const plan = decide(input(closing, { orders: [order({ id: "buy-1", status: "new" })] }));
  assert.deepEqual(plan.actions.map((action) => action.kind), ["cancel"]);
});

test("a flat account does nothing at the close", () => {
  assert.equal(decide(input(open("2026-11-03") + (FLAT_FROM - 570 + 2) * MINUTE)).actions.length, 0);
});

test("the position is 25% of equity in whole shares", () => {
  assert.equal(sharesFor(100000, 700), 35);
  assert.equal(sharesFor(100000, 703.5), 35);
  assert.equal(sharesFor(100, 700), 0);
});

test("the entry uses that size", () => {
  const signal = firstBuy(dipThenRise());
  const action = decide(input(signal.end)).actions[0];
  assert.equal(action.kind, "buy");
  if (action.kind === "buy") assert.equal(action.qty, Math.floor(25000 / signal.close));
});

test("the stop sits 2% below the entry", () => {
  assert.equal(stopPriceFor(700), 686);
});

test("a position with no stop gets one", () => {
  const signal = firstBuy(dipThenRise());
  const action = decide(input(signal.end + 5 * MINUTE, { position: { qty: 35, avgEntry: 100 } })).actions[0];
  assert.equal(action.kind, "stop");
  if (action.kind === "stop") assert.equal(action.stopPrice, 98);
});

test("a position with a stop gets no second one", () => {
  const signal = firstBuy(dipThenRise());
  const stop = order({ id: "s", side: "sell", type: "stop", status: "new" });
  assert.equal(decide(input(signal.end + 5 * MINUTE, { position: { qty: 35, avgEntry: 100 }, orders: [stop] })).actions.length, 0);
});

test("a normal exit cancels the stop first", () => {
  const bars = dipThenRise().map((b) => (b.t > open("2026-11-03") + 20 * 5 * MINUTE ? { ...b, c: 90 } : b));
  const stop = order({ id: "s", side: "sell", type: "stop", status: "new" });
  const now = open("2026-11-03") + 26 * 5 * MINUTE;
  const action = decide(input(now, { bars, position: { qty: 35, avgEntry: 100 }, orders: [stop] })).actions[0];
  assert.equal(action.kind, "sell");
  if (action.kind === "sell") assert.deepEqual(action.cancel, ["s"]);
});

test("at most 20 entries a day", () => {
  const signal = firstBuy(dipThenRise());
  const taken = Array.from({ length: RT_MAX_ENTRIES }, (_, i) => order({ id: `b${i}`, clientId: `rt-SPY-${i}-buy` }));
  assert.equal(entriesToday(taken), 20);
  const plan = decide(input(signal.end, { orders: taken }));
  assert.equal(plan.actions.length, 0);
  assert.deepEqual(plan.notes, ["daily entry limit reached"]);
});

test("cancelled and rejected orders don't count as entries", () => {
  assert.equal(entriesToday([order({ clientId: "rt-a-buy", status: "canceled" }), order({ clientId: "rt-b-buy", status: "rejected" })]), 0);
});

test("no new entries once the account is down 2% today", () => {
  const signal = firstBuy(dipThenRise());
  assert.equal(dayChange(98000, 100000).toFixed(4), "-0.0200");
  const plan = decide(input(signal.end, { equity: 98000 }));
  assert.equal(plan.actions.length, 0);
  assert.deepEqual(plan.notes, ["down 2% today, so no new entries"]);
  assert.equal(decide(input(signal.end, { equity: 98100 })).actions[0].kind, "buy");
});

test("the order id comes from the symbol, the bar and the side", () => {
  const t = Date.parse("2026-11-03T15:05:00Z");
  assert.equal(clientOrderId("SPY", t, "buy"), "rt-SPY-20261103T1505-buy");
  assert.equal(clientOrderId("SPY", t, "buy"), clientOrderId("SPY", t, "buy"));
  assert.notEqual(clientOrderId("SPY", t, "buy"), clientOrderId("SPY", t, "sell"));
});

test("a signal already acted on isn't acted on again, even after a stop-out", () => {
  const signal = firstBuy(dipThenRise());
  const id = clientOrderId("SPY", signal.t, "buy");
  assert.equal(decide(input(signal.end, { orders: [order({ clientId: id })] })).actions.length, 0);
});

test("an order already waiting blocks a second entry", () => {
  const signal = firstBuy(dipThenRise());
  assert.equal(decide(input(signal.end, { orders: [order({ clientId: "rt-other-buy", status: "new" })] })).actions.length, 0);
});

test("one missed run doesn't lose a signal, but an old one is dropped", () => {
  const signal = firstBuy(dipThenRise());
  assert.equal(decide(input(signal.end + 5 * MINUTE)).actions[0].kind, "buy");
  assert.equal(decide(input(signal.end + 15 * MINUTE)).actions.length, 0);
});

// ---------------------------------------------------------------------------
// The fix menu. Every switch is off unless the daily job turns it on.
// ---------------------------------------------------------------------------
import {
  FLAT_FROM as LOCKED_FLAT, NO_ENTRIES_FROM as LOCKED_NO_ENTRIES, NO_FIXES, RT_DAILY_LOSS_PCT, RT_POSITION_PCT, RT_STOP_PCT,
  cleanFixes, dailyContext, fixLabel, limitsFor,
} from "../src/engine.ts";
import type { Daily, FixSet } from "../src/engine.ts";

const DAILY: Daily = { day: "2026-11-03", aboveSma200: true, volRank: 0.5, volScale: 1, prevClose: 100 };
const fix = (changes: Partial<FixSet>): FixSet => ({ ...NO_FIXES, ...changes });
const withVolume = (bars: Bar[], volume = 1000) => bars.map((b) => ({ ...b, v: volume }));

// Down 2 bars, then up fast: the buy crossover comes at 09:45, so its entry would be at 09:50.
function earlySignal(): Bar[] {
  const bars: Bar[] = [];
  for (let i = 0; i < 78; i += 1) bars.push(bar(open("2026-11-02") + i * 5 * MINUTE, 100));
  [99.8, 99.6, ...Array.from({ length: 11 }, (_, i) => 99.6 + (i + 1) * 0.7)].forEach((close, i) => bars.push(bar(open("2026-11-03") + i * 5 * MINUTE, close)));
  return bars;
}

test("with every fix off the engine decides exactly as before", () => {
  const signal = firstBuy(dipThenRise());
  const stop = order({ id: "s", side: "sell", type: "stop", status: "new", stopPrice: 98 });
  const cases: Partial<Input>[] = [{}, { position: { qty: 35, avgEntry: 100 } }, { position: { qty: 35, avgEntry: 100 }, orders: [stop] }, { equity: 97000 }];
  for (const offset of [-1000, 0, 5 * MINUTE, 15 * MINUTE, 60 * MINUTE, 6 * 3600000]) {
    for (const changes of cases) {
      const now = signal.end + offset;
      const plain = decide(input(now, changes));
      const off = decide(input(now, { ...changes, fixes: NO_FIXES, daily: DAILY }));
      assert.deepEqual(off.actions, plain.actions);
      assert.deepEqual(off.notes, plain.notes);
    }
  }
});

test("junk in the control file counts as off", () => {
  assert.deepEqual(cleanFixes({ trend_filter: "sideways", vol_filter: "yes", time_filter: 5, tight_stop: 1, vol_sizing: [true] }), NO_FIXES);
  assert.deepEqual(cleanFixes(null), NO_FIXES);
  assert.equal(cleanFixes({ time_filter: 30 }).time_filter, 30);
  assert.equal(fixLabel(NO_FIXES), "no fixes");
  assert.equal(fixLabel(fix({ gap_filter: true, tight_stop: true })), "gap filter, tighter stop");
});

test("no fix can loosen a locked rule", () => {
  const flags = ["vol_filter", "gap_filter", "liquidity_filter", "confirmation", "time_exit", "trailing_stop", "tight_stop", "vol_sizing"];
  for (const trend of ["off", "above", "below"] as const) {
    for (const time of [0, 15, 30] as const) {
      for (let mask = 0; mask < 1 << flags.length; mask += 1) {
        const fixes = fix({ trend_filter: trend, time_filter: time });
        flags.forEach((name, i) => { (fixes as Record<string, unknown>)[name] = Boolean(mask & (1 << i)); });
        for (const volScale of [null, 0.3, 1, 5]) {
          const limits = limitsFor(fixes, { ...DAILY, volScale });
          assert.ok(limits.positionPct <= RT_POSITION_PCT);
          assert.ok(limits.stopPct <= RT_STOP_PCT);
          assert.ok(limits.maxEntries <= RT_MAX_ENTRIES);
          assert.ok(limits.dailyLossPct <= RT_DAILY_LOSS_PCT);
          assert.ok(limits.noEntriesFrom <= LOCKED_NO_ENTRIES);
          assert.ok(limits.flatFrom <= LOCKED_FLAT);
        }
      }
    }
  }
  assert.equal(sharesFor(100000, 100, 0.9), 250);          // asking for more than 25% still gets 25%
  assert.equal(stopPriceFor(100, 0.07), 98);               // asking for a looser stop still gets 2%
});

test("time filter: no entries in the first 15 or 30 minutes", () => {
  const bars = earlySignal();
  const signal = firstBuy(bars);
  assert.equal(newYorkMinute(signal.end), 9 * 60 + 50);
  assert.equal(decide(input(signal.end, { bars, fixes: fix({ time_filter: 15 }) })).actions[0].kind, "buy");
  assert.deepEqual(decide(input(signal.end, { bars, fixes: fix({ time_filter: 30 }) })).notes,
    ["Fix filter: first 30 minutes of the session → no entry"]);
});

test("trend filter: only above, or only below, the 200-day average", () => {
  const signal = firstBuy(dipThenRise());
  const go = (fixes: FixSet, daily: Daily | null) => decide(input(signal.end, { fixes, daily }));
  assert.equal(go(fix({ trend_filter: "above" }), DAILY).actions[0].kind, "buy");
  assert.deepEqual(go(fix({ trend_filter: "above" }), { ...DAILY, aboveSma200: false }).notes, ["Fix filter: SPY below its 200-day average → no entry"]);
  assert.deepEqual(go(fix({ trend_filter: "below" }), DAILY).notes, ["Fix filter: SPY above its 200-day average → no entry"]);
  assert.deepEqual(go(fix({ trend_filter: "above" }), null).notes, ["Fix filter: no daily data → no entry"]);
});

test("volatility filter: skip the top and bottom 20% of the year", () => {
  const signal = firstBuy(dipThenRise());
  const go = (volRank: number) => decide(input(signal.end, { fixes: fix({ vol_filter: true }), daily: { ...DAILY, volRank } }));
  for (const [rank, allowed] of [[0.85, false], [0.8, false], [0.1, false], [0.2, true], [0.5, true], [0.79, true]] as const) {
    assert.equal(go(rank).actions.length === 1, allowed, String(rank));
  }
});

test("gap filter: skip after an overnight gap over 2%", () => {
  const signal = firstBuy(dipThenRise());
  const go = (prevClose: number) => decide(input(signal.end, { fixes: fix({ gap_filter: true }), daily: { ...DAILY, prevClose } }));
  assert.deepEqual(go(97).notes, ["Fix filter: gap over 2% → no entry"]);       // opened 3.1% above yesterday's close
  assert.equal(go(99).actions[0].kind, "buy");
});

test("liquidity filter: skip a thin signal bar, or bars with no volume", () => {
  const signal = firstBuy(dipThenRise());
  const thin = withVolume(dipThenRise()).map((b) => (b.t === signal.t ? { ...b, v: 400 } : b));
  const go = (bars: Bar[]) => decide(input(signal.end, { bars, fixes: fix({ liquidity_filter: true }) }));
  assert.deepEqual(go(thin).notes, ["Fix filter: thin volume → no entry"]);
  assert.equal(go(withVolume(dipThenRise())).actions[0].kind, "buy");
  assert.deepEqual(go(dipThenRise()).notes, ["Fix filter: no volume data → no entry"]);
  assert.equal(spreadTooWide(100, 100.04), false);
  assert.equal(spreadTooWide(99, 101), true);
});

test("confirmation: wait one more bar, and only if the averages stay crossed", () => {
  const signal = firstBuy(dipThenRise());
  const fixes = fix({ confirmation: true });
  assert.deepEqual(decide(input(signal.end, { fixes })).notes, ["waiting one more bar to confirm the crossover"]);
  assert.equal(decide(input(signal.end + 5 * MINUTE, { fixes })).actions[0].kind, "buy");
  const fellBack = dipThenRise().map((b) => (b.t === signal.t + 5 * MINUTE ? { ...b, c: 90, o: 90, l: 89.9 } : b));
  assert.equal(decide(input(signal.end + 5 * MINUTE, { bars: fellBack, fixes })).actions.length, 0);
  assert.equal(decide(input(signal.end + 15 * MINUTE, { fixes })).actions.length, 0);       // too old by then
});

test("earlier time exit: no entries from 15:20, flat by 15:30", () => {
  const fixes = fix({ time_exit: true });
  const at1530 = open("2026-11-03") + 6 * 3600000;
  const plan = decide(input(at1530, { position: { qty: 35, avgEntry: 100 }, fixes }));
  assert.equal(plan.actions[0].kind, "sell");
  assert.equal(plan.actions[0].why, "flat by 15:30");
  const signal = firstBuy(dipThenRise());
  const late = open("2026-11-03") + (15 * 60 + 20 - 570) * MINUTE;
  const moved = dipThenRise().map((b) => (b.t >= open("2026-11-03") ? { ...b, t: b.t + (late - signal.end) } : b));
  assert.deepEqual(decide(input(late, { bars: moved, fixes })).notes, ["no new entries after 15:20"]);
  assert.equal(decide(input(late, { bars: moved })).actions[0].kind, "buy");
});

test("trailing stop: the stop follows the highest close up, never down", () => {
  const fixes = fix({ trailing_stop: true });
  const entryTime = new Date(open("2026-11-03") + 18 * 5 * MINUTE).toISOString();
  const entry = order({ id: "b", side: "buy", filledAt: entryTime, filledPrice: 100 });
  const now = open("2026-11-03") + 26 * 5 * MINUTE + 1000;
  const low = order({ id: "s", side: "sell", type: "stop", status: "new", stopPrice: 98 });
  const moved = decide(input(now, { position: { qty: 35, avgEntry: 100 }, orders: [entry, low], fixes })).actions;
  assert.deepEqual(moved, [{ kind: "move_stop", id: "s", stopPrice: 102.41, why: "trailing stop moved up" }]);
  const high = order({ id: "s", side: "sell", type: "stop", status: "new", stopPrice: 103 });
  assert.equal(decide(input(now, { position: { qty: 35, avgEntry: 100 }, orders: [entry, high], fixes })).actions.length, 0);
});

test("tighter stop: 1% below the entry, and a 2% stop is moved up to it", () => {
  const signal = firstBuy(dipThenRise());
  const fixes = fix({ tight_stop: true });
  const placed = decide(input(signal.end + 5 * MINUTE, { position: { qty: 35, avgEntry: 100 }, fixes })).actions[0];
  assert.equal(placed.kind, "stop");
  if (placed.kind === "stop") assert.equal(placed.stopPrice, 99);
  const old = order({ id: "s", side: "sell", type: "stop", status: "new", stopPrice: 98 });
  assert.deepEqual(decide(input(signal.end + 5 * MINUTE, { position: { qty: 35, avgEntry: 100 }, orders: [old], fixes })).actions,
    [{ kind: "move_stop", id: "s", stopPrice: 99, why: "tighter stop" }]);
});

test("volatility sizing: smaller when volatility is high, never above 25%", () => {
  const signal = firstBuy(dipThenRise());
  const go = (volScale: number | null) => decide(input(signal.end, { fixes: fix({ vol_sizing: true }), daily: { ...DAILY, volScale } }));
  const full = Math.floor(25000 / signal.close);
  const half = go(0.5).actions[0];
  assert.equal(half.kind === "buy" ? half.qty : 0, Math.floor(12500 / signal.close));
  const capped = go(2).actions[0];
  assert.equal(capped.kind === "buy" ? capped.qty : 0, full);
  assert.deepEqual(go(null).notes, ["Fix filter: no volatility history yet → no entry"]);
});

test("the daily inputs match the Python backtest's numbers", () => {
  // The same series is in tests/test_realtime_fixes.py, with the same expected numbers.
  const daily: Bar[] = [];
  let day = Date.parse("2025-01-01T05:00:00Z");
  for (let k = 0; daily.length < 301; day += 86400000) {
    const weekday = new Date(day).getUTCDay();
    if (weekday === 0 || weekday === 6) continue;
    const c = 100 + 10 * Math.sin(k / 9) + 0.05 * k + 3 * Math.sin(k / 2.3);
    daily.push({ t: day, o: c, h: c, l: c, c });
    k += 1;
  }
  const today = newYorkDay(daily[300].t);
  assert.equal(today, "2026-02-25");
  const context = dailyContext(daily, today);
  assert.ok(Math.abs(context.prevClose! - 121.88366162664722) < 1e-9);
  assert.equal(context.aboveSma200, true);
  assert.ok(Math.abs(context.volRank! - 0.4444444444444444) < 1e-9);
  assert.ok(Math.abs(context.volScale! - 1.0127883333957304) < 1e-9);
  const short = dailyContext(daily.slice(0, 150), newYorkDay(daily[149].t));
  assert.equal(short.aboveSma200, null);
  assert.equal(short.volRank, null);
});
