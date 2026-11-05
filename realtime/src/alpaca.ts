// alpaca.ts: the only file that talks to Alpaca. The paper server is written here as a constant,
// so nothing can point this engine at a live account, and live keys are refused before any call.

import type { Bar, Order, Position } from "./engine.ts";

export const PAPER_URL = "https://paper-api.alpaca.markets";
export const DATA_URL = "https://data.alpaca.markets";
const BAR_LIMIT = { "1Min": 1400, "5Min": 300 } as Record<string, number>;

export type Keys = { key: string; secret: string };
export type Fetcher = typeof fetch;
// buyingPower is what Alpaca will lend against for a trade right now (4x equity for day trades on a
// margin account over $25,000). NaN if Alpaca didn't say, which the sizing treats as no borrowing.
export type Account = { equity: number; lastEquity: number; number: string; buyingPower: number };

// Alpaca's live keys start with AK, and its paper accounts have numbers starting with PA.
export function requirePaperKey(key: string): void {
  if (!key || key.startsWith("AK")) throw new Error("Paper only: this is not a paper key, so the engine will not run.");
}

export function requirePaperAccount(number: string): void {
  if (!number.startsWith("PA")) throw new Error("Paper only: this is not a paper account, so the engine will not run.");
}

function headers(keys: Keys, json = false): Record<string, string> {
  const base: Record<string, string> = { "APCA-API-KEY-ID": keys.key, "APCA-API-SECRET-KEY": keys.secret };
  if (json) base["content-type"] = "application/json";
  return base;
}

async function read(call: Fetcher, url: string, keys: Keys): Promise<any> {
  const response = await call(url, { headers: headers(keys) });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`Alpaca said ${response.status} for ${new URL(url).pathname}`);
  return response.json();
}

export async function clock(call: Fetcher, keys: Keys): Promise<{ isOpen: boolean; nextOpen: string }> {
  const body = await read(call, `${PAPER_URL}/v2/clock`, keys);
  return { isOpen: Boolean(body.is_open), nextOpen: String(body.next_open) };
}

export async function account(call: Fetcher, keys: Keys): Promise<Account> {
  const body = await read(call, `${PAPER_URL}/v2/account`, keys);
  requirePaperAccount(String(body.account_number));
  return {
    equity: Number(body.equity), lastEquity: Number(body.last_equity), number: String(body.account_number),
    buyingPower: body.buying_power == null ? NaN : Number(body.buying_power),
  };
}

export async function position(call: Fetcher, keys: Keys, symbol: string): Promise<Position> {
  const body = await read(call, `${PAPER_URL}/v2/positions/${symbol}`, keys);
  return body ? { qty: Number(body.qty), avgEntry: Number(body.avg_entry_price) } : null;
}

export function toOrder(raw: any): Order {
  return {
    id: String(raw.id), clientId: String(raw.client_order_id ?? ""), side: String(raw.side), type: String(raw.type),
    status: String(raw.status), qty: Number(raw.qty), stopPrice: Number(raw.stop_price ?? 0),
    filledAt: String(raw.filled_at ?? ""), filledPrice: Number(raw.filled_avg_price ?? 0),
  };
}

export async function ordersSince(call: Fetcher, keys: Keys, after: number): Promise<Order[]> {
  const stamp = encodeURIComponent(new Date(after).toISOString());
  const body = await read(call, `${PAPER_URL}/v2/orders?status=all&after=${stamp}&direction=asc&limit=200`, keys);
  return (body ?? []).map(toOrder);
}

// The newest bars first, so a run reads only what it needs. The free real-time feed is IEX.
export async function bars(call: Fetcher, keys: Keys, symbol: string, timeframe: string, now: number): Promise<Bar[]> {
  const start = new Date(now - 6 * 86400000).toISOString();
  const url = `${DATA_URL}/v2/stocks/${symbol}/bars?timeframe=${timeframe}&feed=iex&sort=desc`
    + `&limit=${BAR_LIMIT[timeframe] ?? 300}&start=${encodeURIComponent(start)}`;
  const body = await read(call, url, keys);
  return ((body?.bars ?? []) as any[]).reverse().map((bar) => ({ t: Date.parse(bar.t), o: bar.o, h: bar.h, l: bar.l, c: bar.c, v: bar.v }));
}

// The latest bid and ask on the free feed. Only asked for when the liquidity fix is on and a buy is about to happen.
export async function latestQuote(call: Fetcher, keys: Keys, symbol: string): Promise<{ bid: number; ask: number }> {
  const body = await read(call, `${DATA_URL}/v2/stocks/${symbol}/quotes/latest?feed=iex`, keys);
  return { bid: Number(body?.quote?.bp ?? 0), ask: Number(body?.quote?.ap ?? 0) };
}

export type HeldPosition = { symbol: string; qty: number; market_value: number; unrealized_pl: number; unrealized_plpc: number };

// The main bot's positions, for the side-by-side dashboard. Read only.
export async function positions(call: Fetcher, keys: Keys): Promise<HeldPosition[]> {
  const body = await read(call, `${PAPER_URL}/v2/positions`, keys);
  return ((body ?? []) as any[]).map((item) => ({
    symbol: String(item.symbol), qty: Number(item.qty), market_value: Number(item.market_value),
    unrealized_pl: Number(item.unrealized_pl), unrealized_plpc: Number(item.unrealized_plpc),
  }));
}

// Moves a working stop to a new price in one step, so the shares are never left without a stop.
export async function replaceStop(call: Fetcher, keys: Keys, id: string, stopPrice: number): Promise<Order | null> {
  const response = await call(`${PAPER_URL}/v2/orders/${id}`, {
    method: "PATCH", headers: headers(keys, true), body: JSON.stringify({ stop_price: String(stopPrice) }),
  });
  if (response.status === 404 || response.status === 422) return null;      // filled or gone meanwhile
  if (!response.ok) throw new Error(`Alpaca said ${response.status} to a stop change`);
  return toOrder(await response.json());
}

// Daily bars for the dashboard's charts. Full-market history, leaving out the last 20 minutes.
export async function dailyBars(call: Fetcher, keys: Keys, symbol: string, days: number, now: number): Promise<Bar[]> {
  const start = new Date(now - days * 86400000).toISOString();
  const end = new Date(now - 20 * 60000).toISOString();
  const url = `${DATA_URL}/v2/stocks/${symbol}/bars?timeframe=1Day&feed=sip&adjustment=all&sort=asc&limit=4000`
    + `&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;
  const body = await read(call, url, keys);
  return ((body?.bars ?? []) as any[]).map((bar) => ({ t: Date.parse(bar.t), o: bar.o, h: bar.h, l: bar.l, c: bar.c }));
}

async function send(call: Fetcher, keys: Keys, order: Record<string, unknown>): Promise<Order | null> {
  const response = await call(`${PAPER_URL}/v2/orders`, {
    method: "POST", headers: headers(keys, true), body: JSON.stringify(order),
  });
  if (response.status === 422) {
    const text = await response.text();
    if (text.includes("client_order_id")) return null;          // this exact order already exists
    throw new Error(`Alpaca refused the order: ${text.slice(0, 160)}`);
  }
  if (!response.ok) throw new Error(`Alpaca said ${response.status} to an order`);
  return toOrder(await response.json());
}

export function marketOrder(call: Fetcher, keys: Keys, symbol: string, side: string, qty: number, clientId: string) {
  return send(call, keys, { symbol, qty: String(qty), side, type: "market", time_in_force: "day", client_order_id: clientId });
}

export function stopOrder(call: Fetcher, keys: Keys, symbol: string, qty: number, stopPrice: number, clientId: string) {
  return send(call, keys, {
    symbol, qty: String(qty), side: "sell", type: "stop", stop_price: String(stopPrice), time_in_force: "day", client_order_id: clientId,
  });
}

export async function cancel(call: Fetcher, keys: Keys, id: string): Promise<void> {
  const response = await call(`${PAPER_URL}/v2/orders/${id}`, { method: "DELETE", headers: headers(keys) });
  if (!response.ok && response.status !== 404 && response.status !== 422) throw new Error(`Alpaca said ${response.status} to a cancel`);
}

export async function orderById(call: Fetcher, keys: Keys, id: string): Promise<Order | null> {
  const body = await read(call, `${PAPER_URL}/v2/orders/${id}`, keys);
  return body ? toOrder(body) : null;
}
