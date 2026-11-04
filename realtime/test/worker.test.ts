// Whole Worker runs with a fake Alpaca, so no test can reach a real service or place a real order.
import { test } from "node:test";
import assert from "node:assert/strict";
import * as alpaca from "../src/alpaca.ts";
import {
  BOT1_STATUS_URL, CONTROL_URL, askGemini, clearStatusCache, freshState, geminiRetry, handle, maskPrices, parseControl, runOnce,
  scheduledRun,
} from "../src/index.ts";
import type { Env, Kv, State } from "../src/index.ts";
import { completedBars, crossovers, decide, warmedUp } from "../src/engine.ts";
import type { Bar } from "../src/engine.ts";

const MINUTE = 60000;
const open = (day: string) => Date.parse(`${day}T14:30:00Z`);
const ORIGIN = "https://r1pperr.github.io";
const CODE = "open-sesame-123";

function bars(): Bar[] {
  const rows: Bar[] = [];
  for (let i = 0; i < 78; i += 1) rows.push({ t: open("2026-11-02") + i * 5 * MINUTE, o: 100, h: 100.1, l: 99.9, c: 100, v: 1000 });
  const today = [...Array.from({ length: 12 }, (_, i) => 100 - i * 0.2), ...Array.from({ length: 14 }, (_, i) => 98 + i * 0.5)];
  today.forEach((c, i) => rows.push({ t: open("2026-11-03") + i * 5 * MINUTE, o: c, h: c + 0.1, l: c - 0.1, c, v: 1000 }));
  return rows;
}

// 300 rising daily closes before 2026-11-03, so SPY is above its 200-day average.
function dailyBars(): { t: string; o: number; h: number; l: number; c: number }[] {
  const rows = [];
  let day = Date.parse("2026-11-02T05:00:00Z");
  for (let i = 0; rows.length < 300; i += 1, day -= 86400000) {
    const weekday = new Date(day).getUTCDay();
    if (weekday === 0 || weekday === 6) continue;
    const c = 700 - rows.length * 0.5 + (rows.length % 3) * 0.7;
    rows.push({ t: new Date(day).toISOString(), o: c, h: c, l: c, c });
  }
  return rows.reverse();
}

function signalEnd(): number {
  const late = open("2026-11-03") + 6.4 * 3600000;
  const { bars: warmed, firstToday } = warmedUp(completedBars(bars(), late, 5), late);
  return crossovers(warmed, firstToday, 5).find((signal) => signal.side === "buy")!.end;
}

type Gemini = { decision: string; reason: string } | "error" | "garbage" | "hang";
type World = {
  isOpen?: boolean; position?: unknown; orders?: unknown[]; accountNumber?: string; equity?: string; lastEquity?: string;
  control?: unknown; controlFails?: boolean; bot1?: unknown; gemini?: Gemini; quote?: { ap: number; bp: number };
  mainPositions?: unknown[];
};

function fakeAlpaca(world: World = {}) {
  const calls: { url: string; method: string; body: string; headers: Record<string, string> }[] = [];
  const call = (async (input: string, init: RequestInit = {}) => {
    const url = String(input);
    const method = init.method ?? "GET";
    calls.push({ url, method, body: String(init.body ?? ""), headers: (init.headers ?? {}) as Record<string, string> });
    const path = new URL(url).pathname;
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
    if (url.includes("telegram")) return json({ ok: true });
    if (url.startsWith(CONTROL_URL)) {
      if (world.controlFails) throw new Error("offline");
      return world.control ? json(world.control) : json({ message: "404" }, 404);
    }
    if (url.startsWith(BOT1_STATUS_URL)) return world.bot1 ? json(world.bot1) : json({ message: "404" }, 404);
    if (url.includes("generativelanguage")) {
      const answer = world.gemini ?? { decision: "APPROVE", reason: "Looks fine" };
      if (answer === "error") return json({ error: "busy" }, 500);
      if (answer === "bad-key") return json({ error: "API key not valid" }, 400);
      if (answer === "busy-once" && calls.filter((c) => c.url.includes("generativelanguage")).length === 1) {
        return json({ error: { code: 503, status: "UNAVAILABLE" } }, 503);
      }
      if (answer === "busy-once") return json({ candidates: [{ content: { parts: [{ text: JSON.stringify({ decision: "APPROVE", reason: "Fine" }) }] } }] });
      if (answer === "hang") {
        return new Promise((_, reject) => init.signal?.addEventListener("abort", () => {
          const error = new Error("aborted");
          error.name = "AbortError";
          reject(error);
        }));
      }
      const text = answer === "garbage" ? "maybe?" : JSON.stringify(answer);
      return json({ candidates: [{ content: { parts: [{ text }] } }] });
    }
    if (path.includes("/quotes/latest")) return json({ quote: world.quote ?? { ap: 100.01, bp: 100 } });
    if (path === "/v2/clock") return json({ is_open: world.isOpen ?? true, next_open: "2026-11-04T14:30:00Z" });
    if (path === "/v2/account") {
      return json({ account_number: world.accountNumber ?? "PA123", equity: world.equity ?? "100000", last_equity: world.lastEquity ?? "100000" });
    }
    if (path === "/v2/positions") return json(world.mainPositions ?? []);
    if (path.startsWith("/v2/positions")) return world.position ? json(world.position) : json({ message: "none" }, 404);
    if (path === "/v2/orders" && method === "GET") return json(world.orders ?? []);
    if (path === "/v2/orders" && method === "POST") {
      return json({ id: "new-order", client_order_id: "x", side: "buy", type: "market", status: "filled", qty: "35",
        filled_at: "2026-11-03T15:30:00Z", filled_avg_price: "100" });
    }
    if (path.startsWith("/v2/orders/") && method === "DELETE") return new Response(null, { status: 204 });
    if (path.startsWith("/v2/orders/") && method === "PATCH") {
      return json({ id: "moved", side: "sell", type: "stop", status: "new", qty: "35", stop_price: JSON.parse(String(init.body)).stop_price });
    }
    if (path.startsWith("/v2/orders/")) {
      return json({ id: "new-order", side: "buy", type: "market", status: "filled", qty: "35", filled_at: "2026-11-03T15:30:00Z", filled_avg_price: "100" });
    }
    if (path.includes("/bars") && url.includes("timeframe=1Day")) return json({ bars: dailyBars() });
    if (path.includes("/bars")) {
      return json({ bars: [...bars()].reverse().map((b) => ({ t: new Date(b.t).toISOString(), o: b.o, h: b.h, l: b.l, c: b.c, v: b.v })) });
    }
    return json({ message: "unknown" }, 500);
  }) as unknown as typeof fetch;
  return { call, calls };
}

function fakeKv(saved?: Partial<State>) {
  const store = new Map<string, string>();
  if (saved) store.set("state", JSON.stringify({ ...freshState(), ...saved }));
  const counts = { puts: 0, gets: 0 };
  const kv: Kv = {
    get: async (key: string) => { counts.gets += 1; return store.get(key) ?? null; },
    put: async (key: string, value: string) => { counts.puts += 1; store.set(key, value); },
  };
  return { kv, counts, state: () => JSON.parse(store.get("state") ?? "null") as State };
}

// The old tests run with the AI off. The AI has its own tests below.
function env(changes: Partial<Env> = {}): Env {
  return {
    RT_MODE: "dry", RT_TIMEFRAME: "5Min", PAGES_ORIGIN: ORIGIN, ALPACA_KEY: "PKFAKEKEY", ALPACA_SECRET: "fake-secret-value",
    TELEGRAM_BOT_TOKEN: "fake-token", TELEGRAM_CHAT_ID: "42", VIEWER_CODE: CODE, AI_VETO_MODE: "off", ...changes,
  };
}

const AI = { AI_VETO_MODE: "enforce", GEMINI_API_KEY: "fake-gemini-key" };
const geminiCalls = (calls: { url: string }[]) => calls.filter((c) => c.url.includes("generativelanguage"));
const telegrams = (calls: { url: string; body: string }[]) => calls.filter((c) => c.url.includes("telegram"));

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

test("every order goes to the paper server, and nothing else is ever sent an order", async () => {
  const { call, calls } = fakeAlpaca();
  await quiet(() => runOnce(env({ RT_MODE: "live", ...AI }), signalEnd(), call));
  for (const item of calls.filter((c) => c.url.includes("alpaca"))) {
    assert.ok(item.url.startsWith(alpaca.PAPER_URL) || item.url.startsWith(alpaca.DATA_URL), item.url);
  }
  for (const item of calls.filter((c) => c.url.includes("/v2/orders") && c.method !== "GET")) {
    assert.ok(item.url.startsWith(alpaca.PAPER_URL), item.url);
  }
  for (const item of calls.filter((c) => c.url.includes("githubusercontent"))) assert.equal(item.method, "GET");
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
  const keys = { ...AI, MAIN_ALPACA_KEY: "PKMAINFAKE", MAIN_ALPACA_SECRET: "main-secret-value" };
  const text = await (await handle(get("/status", CODE), env(keys), signalEnd(), call)).text();
  for (const secret of ["PKFAKEKEY", "fake-secret-value", "fake-token", CODE, "fake-gemini-key", "PKMAINFAKE", "main-secret-value"]) {
    assert.ok(!text.includes(secret), secret);
  }
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

// ---------------------------------------------------------------------------
// The AI analyst: asked only when a fresh buy signal is about to be acted on
// ---------------------------------------------------------------------------
test("enforce: an approved buy goes ahead, and Gemini is asked once, with the key in a header", async () => {
  const { call, calls } = fakeAlpaca({ gemini: { decision: "APPROVE", reason: "Clean trend" } });
  await quiet(() => runOnce(env({ RT_MODE: "live", ...AI }), signalEnd(), call));
  assert.equal(geminiCalls(calls).length, 1);
  assert.ok(!geminiCalls(calls)[0].url.includes("fake-gemini-key"));
  assert.equal(geminiCalls(calls)[0].headers["x-goog-api-key"], "fake-gemini-key");
  assert.equal(JSON.parse(posted(calls)[0].body).side, "buy");
});

test("enforce: a veto means no trade, and it is logged", async () => {
  const { call, calls } = fakeAlpaca({ gemini: { decision: "VETO", reason: "Too choppy" } });
  const state = freshState();
  const report = (await quiet(() => runOnce(env({ RT_MODE: "live", ...AI }), signalEnd(), call, state))) as { placed: string[] };
  assert.deepEqual(report.placed, []);
  assert.equal(posted(calls).length, 0);
  assert.equal(state.ai.decisions[0].decision, "VETO");
  assert.match(state.feed[0].text, /AI vetoed \(Too choppy\) → no trade/);
});

test("enforce: a Gemini error, an unclear answer or a missing key means no trade", async () => {
  const cases: [Gemini | undefined, Partial<Env>, RegExp][] = [
    ["error", AI, /Gemini said 500/], ["garbage", AI, /unclear/], [undefined, { AI_VETO_MODE: "enforce" }, /no Gemini key/],
  ];
  for (const [gemini, extra, reason] of cases) {
    const { call, calls } = fakeAlpaca({ gemini });
    const state = freshState();
    await quiet(() => runOnce(env({ RT_MODE: "live", ...extra }), signalEnd(), call, state));
    assert.equal(posted(calls).length, 0, String(gemini));
    assert.match(state.ai.decisions[0].reason, reason);
    assert.equal(state.ai.failures_in_row, 1);
    assert.match(state.feed[0].text, /Gemini error .* → no trade/);
  }
});

test("a busy Gemini (503) is asked once more, and its answer counts", async () => {
  const { call, calls } = fakeAlpaca({ gemini: "busy-once" });
  const state = freshState();
  await quiet(() => runOnce(env({ RT_MODE: "live", ...AI }), signalEnd(), call, state));
  assert.equal(geminiCalls(calls).length, 2);
  assert.equal(state.ai.decisions[0].decision, "APPROVE");
  assert.equal(state.ai.failures_in_row, 0);
  assert.equal(posted(calls).length >= 1, true);
});

test("a Gemini that stays busy is tried twice, then it's an error and no trade", async () => {
  const { call, calls } = fakeAlpaca({ gemini: "error" });
  const state = freshState();
  await quiet(() => runOnce(env({ RT_MODE: "live", ...AI }), signalEnd(), call, state));
  assert.equal(geminiCalls(calls).length, 2);
  assert.equal(posted(calls).length, 0);
  assert.equal(state.ai.failures_in_row, 1);
});

test("a bad Gemini key is not retried", async () => {
  const { call, calls } = fakeAlpaca({ gemini: "bad-key" });
  const verdict = await askGemini(call, env(AI), "prompt");
  assert.deepEqual(verdict, { decision: "VETO", reason: "AI error: Gemini said 400", failed: true });
  assert.equal(geminiCalls(calls).length, 1);
});

test("a Gemini that hangs is cut off and counts as an error", async () => {
  const { call } = fakeAlpaca({ gemini: "hang" });
  assert.deepEqual(await askGemini(call, env(AI), "prompt", 20), { decision: "VETO", reason: "AI error: Gemini timed out", failed: true });
});

test("one question per signal: the next run reuses the saved answer", async () => {
  const { kv, counts } = fakeKv();
  const first = fakeAlpaca({ gemini: { decision: "VETO", reason: "Too choppy" } });
  await quiet(() => scheduledRun(env({ RT_MODE: "live", ...AI, RT_STATE: kv }), signalEnd(), first.call));
  const second = fakeAlpaca({ gemini: { decision: "APPROVE", reason: "Changed my mind" } });
  await quiet(() => scheduledRun(env({ RT_MODE: "live", ...AI, RT_STATE: kv }), signalEnd() + MINUTE, second.call));
  assert.equal(geminiCalls(first.calls).length + geminiCalls(second.calls).length, 1);
  assert.equal(posted(second.calls).length, 0);
  assert.equal(counts.puts, 2);
});

test("shadow: the AI's veto is logged but the trade goes ahead", async () => {
  const { call, calls } = fakeAlpaca({ gemini: { decision: "VETO", reason: "Too choppy" }, control: { ai_mode: { realtime: "shadow" } } });
  const state = freshState();
  await quiet(() => runOnce(env({ RT_MODE: "live", ...AI }), signalEnd(), call, state));
  assert.equal(JSON.parse(posted(calls)[0].body).side, "buy");
  assert.ok(state.feed.some((item) => /AI vetoed \(Too choppy\) → bought \d+ shares/.test(item.text)));
});

test("exits, stops and the close-out never ask the AI", async () => {
  const { call, calls } = fakeAlpaca({ position: { qty: "35", avg_entry_price: "100" } });
  await quiet(() => runOnce(env({ RT_MODE: "live", ...AI }), open("2026-11-03") + 60 * MINUTE, call));
  assert.equal(geminiCalls(calls).length, 0);
  assert.ok(posted(calls).some((c) => JSON.parse(c.body).side === "sell"));
});

test("three Gemini failures in a row send one alert, not one a minute", async () => {
  const { call, calls } = fakeAlpaca({ gemini: "error" });
  const state = { ...freshState(), ai: { failures_in_row: 2, decisions: [] } };
  await quiet(() => runOnce(env({ RT_MODE: "live", ...AI }), signalEnd(), call, state));
  assert.equal(state.ai.failures_in_row, 3);
  assert.equal(telegrams(calls).filter((c) => c.body.includes("failed 3 times in a row")).length, 1);
  state.ai.decisions = [];
  const again = fakeAlpaca({ gemini: "error" });
  await quiet(() => runOnce(env({ RT_MODE: "live", ...AI }), signalEnd() + MINUTE, again.call, state));
  assert.equal(telegrams(again.calls).filter((c) => c.body.includes("times in a row")).length, 0);
});

// ---------------------------------------------------------------------------
// The activity feed and the saved state
// ---------------------------------------------------------------------------
test("the public feed never shows a price; the viewer code shows the details", async () => {
  const { kv } = fakeKv();
  const world = { gemini: { decision: "APPROVE", reason: "Bounced from $683.20 to 690.5 cleanly" } };
  const keys = env({ RT_MODE: "live", ...AI, RT_STATE: kv });
  await quiet(() => scheduledRun(keys, signalEnd(), fakeAlpaca(world).call));
  clearStatusCache();
  const locked = (await (await handle(get("/status"), keys, signalEnd() + 5000, fakeAlpaca(world).call)).json()) as Record<string, any>;
  assert.ok(locked.feed.length > 0);
  for (const item of locked.feed) {
    assert.equal(item.detail, undefined);
    const text = String(item.text).replace(/\d+ shares/g, "N shares");
    assert.ok(!/\$|\d+\.\d+|\b\d{3,}\b/.test(text), text);
  }
  for (const item of locked.ai.decisions) assert.ok(!/683|690/.test(item.reason), item.reason);
  clearStatusCache();
  const unlocked = (await (await handle(get("/status", CODE), keys, signalEnd() + 6000, fakeAlpaca(world).call)).json()) as Record<string, any>;
  assert.ok(unlocked.feed.some((item: any) => String(item.detail ?? "").includes("$100.00")));
  assert.equal(maskPrices("from $683.20 to 690.5, after 9 and 21 bars"), "from … to …, after 9 and 21 bars");
});

test("a quiet hour stays one line in the feed", async () => {
  const state = freshState();
  for (let minute = 0; minute < 90; minute += 1) {
    await runOnce(env(), Date.parse("2026-11-03T13:00:00Z") + minute * MINUTE, fakeAlpaca({ isOpen: false }).call, state);
  }
  assert.equal(state.feed.length, 1);
  assert.equal(state.feed[0].text, "market closed, nothing to do");
});

test("each run writes the saved state exactly once, whatever happens", async () => {
  const cases: [World, Partial<Env>][] = [
    [{ isOpen: false }, {}],
    [{ gemini: { decision: "APPROVE", reason: "ok" } }, { RT_MODE: "live", ...AI }],
    [{ accountNumber: "123456789" }, { RT_MODE: "live" }],
  ];
  for (const [world, keys] of cases) {
    const { kv, counts } = fakeKv();
    const result = (await quiet(() => scheduledRun(env({ ...keys, RT_STATE: kv }), signalEnd(), fakeAlpaca(world).call))) as { saved: boolean };
    assert.equal(counts.puts, 1);
    assert.equal(result.saved, true);
  }
});

test("without KV the engine still runs, it just keeps nothing", async () => {
  const result = (await quiet(() => scheduledRun(env({ RT_MODE: "live" }), signalEnd(), fakeAlpaca().call))) as { saved: boolean; report: { placed: string[] } };
  assert.equal(result.saved, false);
  assert.ok(result.report.placed.length > 0);
});

test("a failed run is fed, marked in the heartbeat and alerted at most every 30 minutes", async () => {
  const { kv, state } = fakeKv();
  const first = fakeAlpaca({ accountNumber: "123456789" });
  const result = (await quiet(() => scheduledRun(env({ RT_MODE: "live", RT_STATE: kv }), signalEnd(), first.call))) as { error: string };
  assert.match(result.error, /Paper only/);
  assert.equal(state().heartbeat.last_result, "error");
  assert.match(state().feed[0].text, /^Run failed/);
  assert.equal(telegrams(first.calls).filter((c) => c.body.includes("Run failed")).length, 1);
  const second = fakeAlpaca({ accountNumber: "123456789" });
  await quiet(() => scheduledRun(env({ RT_MODE: "live", RT_STATE: kv }), signalEnd() + 10 * MINUTE, second.call));
  assert.equal(telegrams(second.calls).filter((c) => c.body.includes("Run failed")).length, 0);
});

// ---------------------------------------------------------------------------
// The watchdog
// ---------------------------------------------------------------------------
const newYork = (hour: number, minute: number) => Date.parse(`2026-11-03T${String(hour + 5).padStart(2, "0")}:${String(minute).padStart(2, "0")}:00Z`);
const ranAt = (when: number) => ({ last_run: { time_utc: new Date(when).toISOString().replace(".000", "") } });
const watchdogAlerts = (calls: { url: string; body: string }[]) => telegrams(calls).filter((c) => c.body.includes("Watchdog"));

test("watchdog: the main bot silent for over an hour sends one alert an hour", async () => {
  const { kv } = fakeKv();
  const now = newYork(11, 0);
  const first = fakeAlpaca({ bot1: ranAt(now - 90 * MINUTE) });
  await quiet(() => scheduledRun(env({ RT_STATE: kv }), now, first.call));
  const sent = watchdogAlerts(first.calls);
  assert.equal(sent.length, 1);
  assert.match(sent[0].body, /the main bot hasn't run for 90 minutes/);
  const second = fakeAlpaca({ bot1: ranAt(now - 90 * MINUTE) });
  await quiet(() => scheduledRun(env({ RT_STATE: kv }), now + 5 * MINUTE, second.call));
  assert.equal(watchdogAlerts(second.calls).length, 0);
});

test("watchdog: quiet when the main bot ran recently, before 09:50, or with the market closed", async () => {
  const recent = fakeAlpaca({ bot1: ranAt(newYork(11, 0) - 20 * MINUTE) });
  await quiet(() => runOnce(env(), newYork(11, 0), recent.call));
  assert.equal(watchdogAlerts(recent.calls).length, 0);
  const early = fakeAlpaca({ bot1: ranAt(newYork(9, 40) - 120 * MINUTE) });
  await quiet(() => runOnce(env(), newYork(9, 40), early.call));
  assert.equal(watchdogAlerts(early.calls).length, 0);
  const closed = fakeAlpaca({ isOpen: false, bot1: ranAt(newYork(11, 0) - 300 * MINUTE) });
  await runOnce(env(), newYork(11, 0), closed.call);
  assert.equal(closed.calls.length, 1);
});

test("watchdog test mode sends a [TEST] alert and touches no order, no Alpaca and no saved state", async () => {
  const { kv, counts } = fakeKv();
  const { call, calls } = fakeAlpaca();
  const result = await scheduledRun(env({ RT_MODE: "live", WATCHDOG_TEST: "missed_bot1", RT_STATE: kv }), Date.parse("2026-11-04T09:00:00Z"), call);
  const sent = telegrams(calls);
  assert.equal(sent.length, 1);
  assert.match(sent[0].body, /\[TEST\].*hasn't run for 120 minutes/);
  assert.equal(calls.filter((c) => !c.url.includes("telegram")).length, 0);
  assert.equal(counts.puts + counts.gets, 0);
  assert.equal(result.report?.reason, "watchdog test");
});

test("the daily loss stop alerts once a day", async () => {
  const { kv } = fakeKv();
  const world = { equity: "97900", lastEquity: "100000" };
  const first = fakeAlpaca(world);
  await quiet(() => scheduledRun(env({ RT_STATE: kv }), signalEnd(), first.call));
  assert.equal(telegrams(first.calls).filter((c) => c.body.includes("Daily loss stop")).length, 1);
  const second = fakeAlpaca(world);
  await quiet(() => scheduledRun(env({ RT_STATE: kv }), signalEnd() + 10 * MINUTE, second.call));
  assert.equal(telegrams(second.calls).filter((c) => c.body.includes("Daily loss stop")).length, 0);
});

test("a position with no working stop for more than 2 runs sends an alert", async () => {
  const sell = { id: "q", client_order_id: "rt-q", side: "sell", type: "market", status: "new", qty: "35" };
  const world = { position: { qty: "35", avg_entry_price: "100" }, orders: [sell] };
  const { kv } = fakeKv();
  const sent: number[] = [];
  for (let run = 0; run < 3; run += 1) {
    const { call, calls } = fakeAlpaca(world);
    await quiet(() => scheduledRun(env({ RT_MODE: "live", RT_STATE: kv }), signalEnd() + (10 + run) * MINUTE, call));
    sent.push(telegrams(calls).filter((c) => c.body.includes("no working stop")).length);
  }
  assert.deepEqual(sent, [0, 0, 1]);
});

// ---------------------------------------------------------------------------
// control.json, the fixes and the dashboard
// ---------------------------------------------------------------------------
test("control.json: falls back to the saved copy, then to the defaults", async () => {
  const { kv } = fakeKv({ control: { ai_mode: { realtime: "shadow" } } });
  const { call, calls } = fakeAlpaca({ controlFails: true, gemini: { decision: "VETO", reason: "No" } });
  await quiet(() => scheduledRun(env({ RT_MODE: "live", ...AI, RT_STATE: kv }), signalEnd(), call));
  assert.equal(JSON.parse(posted(calls)[0].body).side, "buy");               // the saved copy said shadow
  const control = parseControl(null, env({ AI_VETO_MODE: "enforce" }));
  assert.equal(control.aiMode, "enforce");
  assert.equal(control.shadow, null);
  assert.equal(control.fixes.trend_filter, "off");
  assert.equal(parseControl({ ai_mode: { realtime: "off" } }, env({ AI_VETO_MODE: "enforce" })).aiMode, "enforce");
});

test("liquidity fix: a wide spread blocks the buy before the AI is asked", async () => {
  const control = { fixes: { realtime: { liquidity_filter: true } } };
  const wide = fakeAlpaca({ control, quote: { ap: 101, bp: 99 } });
  const state = freshState();
  await quiet(() => runOnce(env({ RT_MODE: "live", ...AI }), signalEnd(), wide.call, state));
  assert.equal(posted(wide.calls).length, 0);
  assert.equal(geminiCalls(wide.calls).length, 0);
  assert.equal(state.feed[0].text, "Fix filter: wide spread → no entry");
  const narrow = fakeAlpaca({ control, quote: { ap: 100.01, bp: 100 } });
  await quiet(() => runOnce(env({ RT_MODE: "live" }), signalEnd(), narrow.call));
  assert.equal(JSON.parse(posted(narrow.calls)[0].body).side, "buy");
});

test("trailing stop fix: the working stop is moved up in one step", async () => {
  const control = { fixes: { realtime: { trailing_stop: true } } };
  const entryTime = new Date(open("2026-11-03") + 18 * 5 * MINUTE).toISOString();
  const orders = [
    { id: "b", client_order_id: "rt-b-buy", side: "buy", type: "market", status: "filled", qty: "35", filled_at: entryTime, filled_avg_price: "100" },
    { id: "s", client_order_id: "rt-s-stop", side: "sell", type: "stop", status: "new", qty: "35", stop_price: "98" },
  ];
  const { call, calls } = fakeAlpaca({ control, orders, position: { qty: "35", avg_entry_price: "100" } });
  await quiet(() => runOnce(env({ RT_MODE: "live" }), open("2026-11-03") + 26 * 5 * MINUTE + 1000, call));
  const patch = calls.find((c) => c.method === "PATCH");
  assert.ok(patch && patch.url.endsWith("/v2/orders/s"));
  assert.equal(JSON.parse(patch!.body).stop_price, String(Math.round(104.5 * 0.98 * 100) / 100));
});

test("shadow fixes are logged next to the live decision and never trade", async () => {
  const state = freshState();
  const { call, calls } = fakeAlpaca({ control: { shadow: { realtime: { trend_filter: "below" } } } });
  await quiet(() => runOnce(env({ RT_MODE: "live" }), signalEnd(), call, state));
  assert.equal(posted(calls).filter((c) => JSON.parse(c.body).side === "buy").length, 1);
  assert.ok(state.feed.some((item) => item.text.startsWith("Shadow (trend filter (only below the 200-day average)): would skip")));
  assert.ok(calls.some((c) => c.url.includes("timeframe=1Day")));
});

test("/status shows the heartbeat, feed, AI, fixes, watchdog and both accounts", async () => {
  const { kv } = fakeKv();
  const keys = env({ RT_STATE: kv, MAIN_ALPACA_KEY: "PKMAINFAKE", MAIN_ALPACA_SECRET: "main-secret-value" });
  const world = {
    bot1: { last_run: { time_utc: "2026-11-03T15:50:00Z" } }, equity: "100250",
    mainPositions: [{ symbol: "MSFT", qty: "9", market_value: "4700", unrealized_pl: "-21.5", unrealized_plpc: "-0.0045" }],
  };
  await quiet(() => scheduledRun(keys, signalEnd(), fakeAlpaca(world).call));
  clearStatusCache();
  const reading = fakeAlpaca(world);
  const body = (await (await handle(get("/status"), keys, signalEnd() + 5000, reading.call)).json()) as Record<string, any>;
  assert.ok(reading.calls.length <= 10, String(reading.calls.length));
  assert.equal(body.heartbeat.last_result, "ok");
  assert.ok(body.feed.length > 0);
  assert.equal(body.ai.mode, "off");
  assert.equal(body.fixes.active.trend_filter, "off");
  assert.equal(body.fixes.shadow, null);
  assert.equal(body.watchdog.bot1_last_run_utc, "2026-11-03T15:50:00Z");
  assert.equal(body.watchdog.bot1_minutes_since, 10);
  assert.equal(body.watchdog.late, false);
  assert.equal(body.pl_total, 250);
  assert.equal(body.bot1.pl_total, 250);
  assert.deepEqual(body.bot1.positions[0], { symbol: "MSFT", qty: 9, market_value: 4700, unrealized_pl: -21.5, unrealized_plpc: -0.0045 });
  clearStatusCache();
  const without = (await (await handle(get("/status"), env({ RT_STATE: kv }), signalEnd() + 5000, fakeAlpaca(world).call)).json()) as Record<string, any>;
  assert.equal(without.bot1, null);
  clearStatusCache();
  const live = (await (await handle(get("/status"), env({ MAIN_ALPACA_KEY: "AKLIVE", MAIN_ALPACA_SECRET: "x" }), signalEnd(), fakeAlpaca().call)).json()) as Record<string, any>;
  assert.deepEqual(live.bot1, { error: "could not read the main account" });
});

test("a busy run (AI, liquidity check, daily bars, entry, stop, alerts) stays at 15 calls or fewer", async () => {
  const control = { fixes: { realtime: { liquidity_filter: true, trend_filter: "above" } } };
  const { call, calls } = fakeAlpaca({ control, gemini: { decision: "APPROVE", reason: "ok" }, bot1: ranAt(signalEnd() - 180 * MINUTE) });
  await quiet(() => scheduledRun(env({ RT_MODE: "live", ...AI }), signalEnd(), call));
  assert.ok(posted(calls).length >= 2);
  assert.ok(calls.length <= 15, String(calls.length));
});
