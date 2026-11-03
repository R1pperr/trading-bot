// The engine's rules on small hand-made bars. No network here.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  FLAT_FROM, NO_ENTRIES_FROM, RT_MAX_ENTRIES, clientOrderId, completedBars, crossovers, dayChange, decide, ema, entriesToday, newYorkDay,
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
