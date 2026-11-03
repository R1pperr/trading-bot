// Whole Worker runs with a fake Alpaca, so no test can reach a real service or place a real order.
import { test } from "node:test";
import assert from "node:assert/strict";
import * as alpaca from "../src/alpaca.ts";
import { clearStatusCache, handle, runOnce } from "../src/index.ts";
import type { Env } from "../src/index.ts";
import { completedBars, crossovers, decide, warmedUp } from "../src/engine.ts";
import type { Bar } from "../src/engine.ts";

const MINUTE = 60000;
const open = (day: string) => Date.parse(`${day}T14:30:00Z`);
const ORIGIN = "https://r1pperr.github.io";
const CODE = "open-sesame-123";

function bars(): Bar[] {
  const rows: Bar[] = [];
  for (let i = 0; i < 78; i += 1) rows.push({ t: open("2026-11-02") + i * 5 * MINUTE, o: 100, h: 100.1, l: 99.9, c: 100 });
  const today = [...Array.from({ length: 12 }, (_, i) => 100 - i * 0.2), ...Array.from({ length: 14 }, (_, i) => 98 + i * 0.5)];
  today.forEach((c, i) => rows.push({ t: open("2026-11-03") + i * 5 * MINUTE, o: c, h: c + 0.1, l: c - 0.1, c }));
  return rows;
}

function signalEnd(): number {
  const late = open("2026-11-03") + 6.4 * 3600000;
  const { bars: warmed, firstToday } = warmedUp(completedBars(bars(), late, 5), late);
  return crossovers(warmed, firstToday, 5).find((signal) => signal.side === "buy")!.end;
}

type World = { isOpen?: boolean; position?: unknown; orders?: unknown[]; accountNumber?: string };

function fakeAlpaca(world: World = {}) {
  const calls: { url: string; method: string; body: string }[] = [];
  const call = (async (input: string, init: RequestInit = {}) => {
    const url = String(input);
    const method = init.method ?? "GET";
    calls.push({ url, method, body: String(init.body ?? "") });
    const path = new URL(url).pathname;
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
    if (url.includes("telegram")) return json({ ok: true });
    if (path === "/v2/clock") return json({ is_open: world.isOpen ?? true, next_open: "2026-11-04T14:30:00Z" });
    if (path === "/v2/account") return json({ account_number: world.accountNumber ?? "PA123", equity: "100000", last_equity: "100000" });
    if (path.startsWith("/v2/positions")) return world.position ? json(world.position) : json({ message: "none" }, 404);
    if (path === "/v2/orders" && method === "GET") return json(world.orders ?? []);
    if (path === "/v2/orders" && method === "POST") {
      return json({ id: "new-order", client_order_id: "x", side: "buy", type: "market", status: "filled", qty: "35",
        filled_at: "2026-11-03T15:30:00Z", filled_avg_price: "100" });
    }
    if (path.startsWith("/v2/orders/") && method === "DELETE") return new Response(null, { status: 204 });
    if (path.startsWith("/v2/orders/")) {
      return json({ id: "new-order", side: "buy", type: "market", status: "filled", qty: "35", filled_at: "2026-11-03T15:30:00Z", filled_avg_price: "100" });
    }
    if (path.includes("/bars")) {
      return json({ bars: [...bars()].reverse().map((b) => ({ t: new Date(b.t).toISOString(), o: b.o, h: b.h, l: b.l, c: b.c })) });
    }
    return json({ message: "unknown" }, 500);
  }) as unknown as typeof fetch;
  return { call, calls };
}

function env(changes: Partial<Env> = {}): Env {
  return {
    RT_MODE: "dry", RT_TIMEFRAME: "5Min", PAGES_ORIGIN: ORIGIN, ALPACA_KEY: "PKFAKEKEY", ALPACA_SECRET: "fake-secret-value",
    TELEGRAM_BOT_TOKEN: "fake-token", TELEGRAM_CHAT_ID: "42", VIEWER_CODE: CODE, ...changes,
  };
}

const posted = (calls: { url: string; method: string }[]) => calls.filter((c) => c.method === "POST" && c.url.includes("/v2/orders"));
const quiet = async (work: () => Promise<unknown>) => {
  const log = console.log;
  console.log = () => {};
  try { return await work(); } finally { console.log = log; }
};

test("a closed market means one look at the clock and nothing else", async () => {
  const { call, calls } = fakeAlpaca({ isOpen: false });
  const report = await runOnce(env({ RT_MODE: "live" }), signalEnd(), call);
  assert.equal(report.ran, false);
  assert.equal(report.reason, "market closed");
  assert.equal(calls.length, 1);
  assert.ok(calls[0].url.endsWith("/v2/clock"));
});

test('"off" does nothing at all', async () => {
  const { call, calls } = fakeAlpaca();
  await runOnce(env({ RT_MODE: "off" }), signalEnd(), call);
  assert.equal(calls.length, 0);
});

test('"dry" logs what it would do and never places an order', async () => {
  const { call, calls } = fakeAlpaca();
  const report = (await quiet(() => runOnce(env({ RT_MODE: "dry" }), signalEnd(), call))) as { would: string[]; placed: string[] };
  assert.equal(report.would.length, 1);
  assert.match(report.would[0], /would buy/);
  assert.deepEqual(report.placed, []);
  assert.equal(posted(calls).length, 0);
});

test('"dry" cancels nothing either, even at the close', async () => {
  const stop = { id: "s", client_order_id: "rt-s", side: "sell", type: "stop", status: "new", qty: "35", stop_price: "98" };
  const { call, calls } = fakeAlpaca({ position: { qty: "35", avg_entry_price: "100" }, orders: [stop] });
  await quiet(() => runOnce(env({ RT_MODE: "dry" }), Date.parse("2026-11-03T20:56:00Z"), call));
  assert.equal(calls.filter((c) => c.method === "DELETE" || c.method === "POST").filter((c) => !c.url.includes("telegram")).length, 0);
});

test("an unknown mode falls back to dry, never to live", async () => {
  const { call, calls } = fakeAlpaca();
  await quiet(() => runOnce(env({ RT_MODE: "LIVE!" }), signalEnd(), call));
  assert.equal(posted(calls).length, 0);
});

test('"live" places the entry, then its stop once it has filled', async () => {
  const { call, calls } = fakeAlpaca();
  await quiet(() => runOnce(env({ RT_MODE: "live" }), signalEnd(), call));
  const orders = posted(calls).map((c) => JSON.parse(c.body));
  assert.equal(orders[0].side, "buy");
  assert.match(orders[0].client_order_id, /^rt-SPY-\d{8}T\d{4}-buy$/);
  assert.equal(orders[1].type, "stop");
  assert.equal(orders[1].stop_price, "98");
});

test("every order goes to the paper server", async () => {
  const { call, calls } = fakeAlpaca();
  await quiet(() => runOnce(env({ RT_MODE: "live" }), signalEnd(), call));
  for (const item of calls.filter((c) => !c.url.includes("telegram"))) {
    assert.ok(item.url.startsWith(alpaca.PAPER_URL) || item.url.startsWith(alpaca.DATA_URL), item.url);
  }
  assert.equal(alpaca.PAPER_URL, "https://paper-api.alpaca.markets");
});

test("a live key is refused before any call is made", async () => {
  const { call, calls } = fakeAlpaca();
  await assert.rejects(runOnce(env({ ALPACA_KEY: "AKLIVEKEY" }), signalEnd(), call), /Paper only/);
  assert.equal(calls.length, 0);
});

test("a live account is refused", async () => {
  const { call, calls } = fakeAlpaca({ accountNumber: "123456789" });
  await assert.rejects(runOnce(env({ RT_MODE: "live" }), signalEnd(), call), /Paper only/);
  assert.equal(posted(calls).length, 0);
});

test("a repeated order is treated as already done", async () => {
  const call = (async () => new Response('{"message":"client_order_id must be unique"}', { status: 422 })) as unknown as typeof fetch;
  assert.equal(await alpaca.marketOrder(call, { key: "PK", secret: "s" }, "SPY", "buy", 1, "rt-1"), null);
});

test("any other refusal is reported", async () => {
  const call = (async () => new Response('{"message":"insufficient buying power"}', { status: 422 })) as unknown as typeof fetch;
  await assert.rejects(alpaca.marketOrder(call, { key: "PK", secret: "s" }, "SPY", "buy", 1, "rt-1"), /refused/);
});

test("a signal sends one Telegram message, in the minute it happens", async () => {
  const { call, calls } = fakeAlpaca();
  await quiet(() => runOnce(env(), signalEnd() + 5000, call));
  assert.equal(calls.filter((c) => c.url.includes("telegram")).length, 1);
  const later = fakeAlpaca();
  await quiet(() => runOnce(env(), signalEnd() + 2 * MINUTE, later.call));
  assert.equal(later.calls.filter((c) => c.url.includes("telegram")).length, 0);
});

test("a fill sends a Telegram message", async () => {
  const when = "2026-11-03T15:30:20Z";
  const fill = { id: "f", client_order_id: "rt-a-buy", side: "buy", type: "market", status: "filled", qty: "35", filled_at: when, filled_avg_price: "100" };
  const { call, calls } = fakeAlpaca({ orders: [fill], position: { qty: "35", avg_entry_price: "100" } });
  await quiet(() => runOnce(env(), Date.parse(when) + 30000, call));
  assert.ok(calls.some((c) => c.url.includes("telegram") && c.body.includes("Filled")));
});

test("a failed backtest is labelled in every message", async () => {
  const { call, calls } = fakeAlpaca();
  await quiet(() => runOnce(env({ RT_EXPERIMENTAL: "true" }), signalEnd(), call));
  assert.ok(calls.find((c) => c.url.includes("telegram"))!.body.includes("Experimental: failed backtest"));
});

test("a run reads only what it needs, well under Cloudflare's limit of 50 calls", async () => {
  const { call, calls } = fakeAlpaca();
  await quiet(() => runOnce(env({ RT_MODE: "live" }), signalEnd(), call));
  assert.ok(calls.length < 15, String(calls.length));
});

test("the decision on a full 1-minute day fits well inside 10 ms of CPU", () => {
  const rows: Bar[] = [];
  for (let i = 0; i < 780; i += 1) rows.push({ t: open("2026-11-02") + (i % 390) * MINUTE + (i >= 390 ? 86400000 : 0), o: 100, h: 101, l: 99, c: 100 + Math.sin(i / 7) });
  const started = performance.now();
  for (let run = 0; run < 20; run += 1) {
    decide({ now: open("2026-11-03") + 390 * MINUTE - 6 * MINUTE, bars: rows, minutes: 1, position: null, orders: [], equity: 100000, lastEquity: 100000 });
  }
  assert.ok((performance.now() - started) / 20 < 10);
});

function get(path: string, code?: string, origin = ORIGIN) {
  const headers: Record<string, string> = { origin };
  if (code) headers["x-viewer-code"] = code;
  return new Request(`https://rt.example.workers.dev${path}`, { headers });
}

test("/status without the code has your account but no prices", async () => {
  clearStatusCache();
  const { call } = fakeAlpaca({ position: { qty: "35", avg_entry_price: "100" } });
  const response = await handle(get("/status"), env(), open("2026-11-03") + 3600000, call);
  const body = await response.json() as Record<string, unknown>;
  assert.equal(body.locked, true);
  assert.equal(body.bars, undefined);
  assert.equal(body.ema_fast, undefined);
  assert.deepEqual(body.position, { qty: 35, avgEntry: 100 });
  assert.equal(body.mode, "dry");
});

test("/status with the code has today's bars, EMA values and signals", async () => {
  clearStatusCache();
  const { call } = fakeAlpaca();
  const body = await (await handle(get("/status", CODE), env(), signalEnd() + 10 * MINUTE, call)).json() as Record<string, any>;
  assert.equal(body.locked, false);
  assert.ok(body.bars.length > 0);
  assert.equal(body.bars.length, body.ema_fast.length);
  assert.equal(body.bars.length, body.ema_slow.length);
  assert.ok(body.signals.length > 0);
});

test("a wrong code is treated like no code", async () => {
  clearStatusCache();
  const { call } = fakeAlpaca();
  const body = await (await handle(get("/status", "wrong-code-12345"), env(), signalEnd(), call)).json() as Record<string, unknown>;
  assert.equal(body.locked, true);
});

test("no secret ever appears in a response", async () => {
  clearStatusCache();
  const { call } = fakeAlpaca();
  const text = await (await handle(get("/status", CODE), env(), signalEnd(), call)).text();
  for (const secret of ["PKFAKEKEY", "fake-secret-value", "fake-token", CODE]) assert.ok(!text.includes(secret), secret);
});

test("/status only allows your GitHub Pages site", async () => {
  clearStatusCache();
  const { call } = fakeAlpaca();
  const yours = await handle(get("/status"), env(), signalEnd(), call);
  assert.equal(yours.headers.get("access-control-allow-origin"), ORIGIN);
  clearStatusCache();
  const theirs = await handle(get("/status", undefined, "https://evil.example"), env(), signalEnd(), call);
  assert.equal(theirs.headers.get("access-control-allow-origin"), null);
});

test("the preflight check is answered without calling Alpaca", async () => {
  const { call, calls } = fakeAlpaca();
  const request = new Request("https://rt.example.workers.dev/status", { method: "OPTIONS", headers: { origin: ORIGIN } });
  assert.equal((await handle(request, env(), signalEnd(), call)).status, 204);
  assert.equal(calls.length, 0);
});

test("/status is read only", async () => {
  const { call } = fakeAlpaca();
  const request = new Request("https://rt.example.workers.dev/status", { method: "POST" });
  assert.equal((await handle(request, env(), signalEnd(), call)).status, 405);
});

test("repeat visits inside 20 seconds don't call Alpaca again", async () => {
  clearStatusCache();
  const { call, calls } = fakeAlpaca();
  await handle(get("/status"), env(), signalEnd(), call);
  const first = calls.length;
  await handle(get("/status"), env(), signalEnd() + 5000, call);
  assert.equal(calls.length, first);
});

test("/prices needs the code", async () => {
  const { call, calls } = fakeAlpaca();
  assert.equal((await handle(get("/prices?symbol=SPY"), env(), signalEnd(), call)).status, 401);
  assert.equal(calls.length, 0);
});

test("/prices refuses a strange symbol", async () => {
  const { call } = fakeAlpaca();
  assert.equal((await handle(get("/prices?symbol=../../v2/account", CODE), env(), signalEnd(), call)).status, 400);
});

test("/prices with the code returns daily bars", async () => {
  const { call } = fakeAlpaca();
  const body = await (await handle(get("/prices?symbol=spy&days=200", CODE), env(), signalEnd(), call)).json() as Record<string, any>;
  assert.equal(body.symbol, "SPY");
  assert.ok(body.bars.length > 0);
});

test("a problem reaching Alpaca gives a plain error with no details", async () => {
  clearStatusCache();
  const call = (async () => { throw new Error("secret detail"); }) as unknown as typeof fetch;
  const response = await handle(get("/status"), env(), signalEnd(), call);
  assert.equal(response.status, 502);
  assert.ok(!(await response.text()).includes("secret detail"));
});
