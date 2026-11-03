"""
sleeves.py  -  runs the strategy sleeves on the one paper account.

Used only when config.STRATEGY_MODE is "sleeves". Each sleeve has its own ENABLE
flag in config.py, and each one is switched on only after you approve it.

Every run: checks the stop on every position.
First run of the day (stocks shortly before the open, so orders fill at the open;
crypto after 00:00 UTC): works out signals, sells what should be sold, then makes
new entries.

How the sleeves share the account:
  * Each horizon's money is split equally among its enabled sleeves. That share of
    equity caps a sleeve's total exposure, and unused money stays in cash.
  * A symbol belongs to one sleeve at a time. state/sleeves.json records the owner,
    and each order's id carries the sleeve too, so the file can be rebuilt.
  * A position with no owner stays on the old rules (fixed stop, crossover sell).
  * A sleeve that is switched off makes no new entries but still sells what it holds.
  * Exits always go through. New entries wait for the daily cap, short-term first.
  * Buys never spend more cash than the account has, so margin is never used.

The state file is public, so it holds your own positions and no price history.
"""

import json
import math
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest, LimitOrderRequest, MarketOrderRequest

import ai_analyst
import config
import crypto
import notifier
import risk
import signals
import stops
import strategies
import universe
from strategy import SELL, latest_signal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state", "sleeves.json")
FLAGS = {"turn_of_month": "ENABLE_TURN_OF_MONTH", "ibs_etfs": "ENABLE_IBS_ETFS", "rsi2_stocks": "ENABLE_RSI2_STOCKS",
         "crypto_trend": "ENABLE_CRYPTO_TREND", "rotation": "ENABLE_ROTATION",
         "momentum_stocks": "ENABLE_MOMENTUM_STOCKS", "crypto_hold": "ENABLE_CRYPTO_HOLD"}
RULE_TEXT = {"turn_of_month": "hold IVV from the month's last trading day to the new month's 4th",
             "ibs_etfs": "buy a fund that closed in the bottom fifth of its daily range",
             "rsi2_stocks": "buy a stock above its 200-day average after its 2-day RSI closed under 10",
             "crypto_trend": "hold coins with a positive trend signal", "rotation": "monthly asset-class rotation",
             "momentum_stocks": "hold the 10 strongest stocks of the past year", "crypto_hold": "hold BTC and ETH in an uptrend"}
HISTORY_DAYS = 420      # calendar days: enough for the 200-day average and 12-month momentum


def is_enabled(sleeve):
    return bool(getattr(config, FLAGS[sleeve]))


def sleeves_for(market):
    """This market's sleeves that are switched on, short-term first."""
    return [name for name, sleeve in config.SLEEVES.items() if sleeve["market"] == market and is_enabled(name)]


def share_of(sleeve):
    """This sleeve's share of the account: its horizon's money over its enabled sleeves. Off means 0."""
    return strategies.split_by_horizon([name for name in config.SLEEVES if is_enabled(name)]).get(sleeve, 0.0)


def in_market(symbol, market):
    """Crypto pairs are saved with a slash, stocks never are."""
    return ("/" in symbol) == (market == "crypto")


# ---------------------------------------------------------------------------
# Who owns what
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        state = {}
    state.setdefault("positions", {})
    state.setdefault("days", {})
    state.setdefault("momentum", {"month": "", "targets": [], "stopped": []})
    state.setdefault("closed_pl", {})
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def owner_of(state, symbol):
    return state["positions"].get(symbol, {}).get("sleeve", "")


def make_order_id(sleeve, side):
    return f"tach-{sleeve}-{side}-{uuid.uuid4().hex[:12]}"


def sleeve_from_order_id(order_id):
    parts = str(order_id or "").split("-")
    return parts[1] if len(parts) >= 4 and parts[0] == "tach" and parts[1] in config.SLEEVES else ""


def tagged_buys(orders, name):
    """The newest buy a sleeve placed for each symbol. Alpaca lists the newest order first."""
    found = {}
    for order in orders:
        if crypto.text(order.side) == "buy" and sleeve_from_order_id(order.client_order_id):
            found.setdefault(name(order.symbol), order)
    return found


def reconcile(state, market, held, open_buys, closed_buys):
    """Forget what this market no longer holds, and rebuild lost owners from the order ids."""
    records = state["positions"]
    for symbol in [s for s in records if in_market(s, market)]:
        if symbol not in held and symbol not in open_buys:
            del records[symbol]
    for symbol, order in {**closed_buys, **open_buys}.items():
        if symbol not in records and (symbol in held or symbol in open_buys):
            records[symbol] = {"sleeve": sleeve_from_order_id(order.client_order_id),
                               "entry_date": str(order.filled_at or order.created_at)[:10]}


def exposure(state, positions, sleeve):
    return sum(float(p.market_value or 0) for s, p in positions.items() if owner_of(state, s) == sleeve)


def days_held(table, entry_date):
    """Finished daily bars since the buy, counting the day it was bought."""
    if not entry_date:
        return 0
    return int((table.index >= pd.Timestamp(entry_date)).sum())


# ---------------------------------------------------------------------------
# Each sleeve's rules, applied to every symbol's latest row of indicators.
# Each returns (exits as {symbol: reason}, the symbols it wants, best first).
# ---------------------------------------------------------------------------
def rsi2_rules(latest, order, mine, held_days):
    exits = {s: "RSI(2) exit rule" for s in mine
             if strategies.rsi2_exit(latest[s]["close"], latest[s]["sma_exit"], latest[s]["sma_trend"],
                                     held_days.get(s, 0))}
    wanted = [s for s in order
              if strategies.rsi2_entry(latest[s]["close"], latest[s]["sma_trend"], latest[s]["rsi"])]
    return exits, wanted


def trend_rules(latest, order, mine):
    exits = {s: "trend signal no longer positive" for s in mine
             if not pd.isna(latest[s]["trend"]) and not strategies.trend_long(latest[s]["trend"])}
    wanted = [s for s in order[:config.CRYPTO_TREND_TOP_N] if strategies.trend_long(latest[s]["trend"])]
    return exits, wanted


def basket_scale(tables, names):
    """The volatility-scaled sleeve's exposure, from the stocks it's about to hold."""
    prices = pd.DataFrame({name: tables[name]["close"] for name in names if name in tables})
    return strategies.vol_scale(prices.pct_change(fill_method=None).mean(axis=1)) if len(prices.columns) else 1.0


def momentum_rules(latest, order, mine, state, month, tables=None):
    """No SPY reading means no decision: a failed download must never sell the sleeve."""
    spy = latest.get(config.MOMENTUM_MARKET_SYMBOL)
    if spy is None:
        return {}, []
    plan = state["momentum"]
    if plan["month"] != month:
        names = strategies.top_momentum(order, [latest[s]["momentum"] for s in order])
        if names:
            plan = state["momentum"] = {"month": month, "targets": names, "stopped": []}
            if config.VOL_SCALED_MOMENTUM and tables:
                plan["scale"] = round(basket_scale(tables, names), 4)
    market_up = strategies.above_trend(spy["close"], spy["sma_trend"])
    exits = {}
    for symbol in mine:
        if not market_up:
            exits[symbol] = "SPY closed below its 200-day average"
        elif symbol not in plan["targets"]:
            exits[symbol] = "no longer in the top momentum list"
    wanted = [s for s in plan["targets"] if s in latest and s not in plan["stopped"]] if market_up else []
    return exits, wanted


def ibs_rules(latest, mine, held_days):
    exits = {s: "IBS exit rule" for s in mine
             if not pd.isna(latest[s]["ibs"]) and strategies.ibs_exit(latest[s]["ibs"], held_days.get(s, 0))
             or held_days.get(s, 0) >= config.IBS_MAX_HOLD_DAYS}
    wanted = [s for s in config.IBS_SYMBOLS if s in latest and strategies.ibs_entry(latest[s]["ibs"])]
    return exits, wanted


def turn_of_month_rules(calendar, today, mine):
    """calendar is the exchange's trading days. The days are known in advance, so no prices are needed."""
    if today not in calendar:
        return {}, []
    action = strategies.turn_of_month_order(calendar, calendar.index(today))
    if action == "sell":
        return {s: "4th trading day of the month" for s in mine}, []
    return {}, [config.TURN_OF_MONTH_SYMBOL] if action == "buy" else []


def rotation_rules(latest, mine, state, month):
    """Re-ranked once a month. Between ranks only the stop-loss can sell."""
    plan = state.setdefault("rotation", {"month": "", "targets": [], "stopped": []})
    if plan["month"] != month:
        names = [s for s in config.ROTATION_SYMBOLS if s in latest]
        if names:
            picks = strategies.rotation_picks(names, [latest[s]["rotation"] for s in names],
                                              [latest[s]["close"] for s in names],
                                              [latest[s]["sma_trend"] for s in names])
            plan = state["rotation"] = {"month": month, "targets": picks, "stopped": []}
    exits = {s: "no longer in the top of the rotation" for s in mine if s not in plan["targets"]}
    return exits, [s for s in plan["targets"] if s in latest and s not in plan["stopped"]]


def hold_rules(latest, mine):
    exits, wanted = {}, []
    for symbol in config.CRYPTO_HOLD_SYMBOLS:
        if symbol not in latest:
            continue
        if strategies.above_trend(latest[symbol]["close"], latest[symbol]["sma_trend"]):
            wanted.append(symbol)
        elif symbol in mine:
            exits[symbol] = "closed below its 200-day average"
    return exits, wanted


def daily_decisions(market, latest, state, month, held_days, tables=None, calendar=(), today=""):
    """Returns (exits as {symbol: reason}, entries as (sleeve, symbol), short-term sleeves first)."""
    exits, entries = {}, []
    order = [row["symbol"] for row in universe.rows(market)
             if row["symbol"] in latest and row["symbol"] not in config.ONLY_FOR_TURN_OF_MONTH]
    for sleeve, settings in config.SLEEVES.items():
        if settings["market"] != market:
            continue
        mine = [s for s, record in state["positions"].items() if record["sleeve"] == sleeve and s in latest]
        if sleeve in ("ibs_etfs", "turn_of_month", "rotation") and not mine and not is_enabled(sleeve):
            continue
        if sleeve == "rsi2_stocks":
            sleeve_exits, wanted = rsi2_rules(latest, order, mine, held_days)
        elif sleeve == "ibs_etfs":
            sleeve_exits, wanted = ibs_rules(latest, mine, held_days)
        elif sleeve == "turn_of_month":
            sleeve_exits, wanted = turn_of_month_rules(list(calendar), today, mine)
        elif sleeve == "rotation":
            sleeve_exits, wanted = rotation_rules(latest, mine, state, month)
        elif sleeve == "crypto_trend":
            sleeve_exits, wanted = trend_rules(latest, order, mine)
        elif sleeve == "momentum_stocks":
            sleeve_exits, wanted = momentum_rules(latest, order, mine, state, month, tables)
        else:
            sleeve_exits, wanted = hold_rules(latest, mine)
        exits.update(sleeve_exits)
        if is_enabled(sleeve):
            entries += [(sleeve, symbol) for symbol in wanted]
    return exits, entries


def stop_for(price, atr_value):
    if config.USE_ATR_STOPS and atr_value and atr_value > 0:
        return risk.atr_stop(price, atr_value)
    return risk.stop_price(price)


def stop_hit(record, entry, price):
    """Owned positions use their saved stop once ATR stops are approved. Otherwise the fixed stop."""
    if config.USE_ATR_STOPS and record and record.get("stop"):
        return price <= record["stop"]
    return risk.stop_loss_hit(entry, price)


def entry_room(sleeve, equity, already_used, scale=1.0):
    """Dollars this sleeve may still add: what's left of its share, and at most one slot of it."""
    share = equity * share_of(sleeve)
    room = share - already_used
    slots = {"momentum_stocks": config.MOMENTUM_TOP_N, "rotation": config.ROTATION_TOP_N,
             "crypto_trend": config.CRYPTO_TREND_TOP_N, "crypto_hold": len(config.CRYPTO_HOLD_SYMBOLS)}
    return min(room, share / slots[sleeve] * scale) if sleeve in slots else room


def fractionable(trading, symbol):
    """Whether Alpaca lets this stock trade in parts of a share. If it can't say, assume not."""
    if not config.FRACTIONAL_SHARES:
        return False
    try:
        return bool(trading.get_asset(symbol).fractionable)
    except Exception:
        return False


def units(amount, market, parts=False):
    """How much to order. Crypto always trades in parts. Stocks do where Alpaca allows it."""
    if market == "crypto":
        return math.floor(amount * 1e8) / 1e8
    return math.floor(amount * 1e6) / 1e6 if parts else math.floor(amount)


def trading_days(trading, today):
    """The exchange's trading days around today, from Alpaca's calendar. Empty if it can't be read."""
    try:
        middle = date.fromisoformat(today)
        request = GetCalendarRequest(start=middle - timedelta(days=45), end=middle + timedelta(days=45))
        return [str(day.date) for day in trading.get_calendar(filters=request)]
    except Exception as error:
        print("  Could not read the trading calendar:", notifier.redact(error))
        return []


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
def tagged(sleeve, text):
    """The trade log keeps the sleeve at the start of the details."""
    return f"sleeve={sleeve}; {text}" if sleeve else text


def cancel_stops(trading, working):
    """A waiting stop holds the shares, so it must go before any other sell."""
    for order in working:
        trading.cancel_order_by_id(order.id)


def sell_all(trading, market, symbol, qty, sleeve, waited_on_stop):
    """Alpaca can take a moment to release shares from a cancelled stop, so that case tries again."""
    request = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL,
                                 time_in_force=TimeInForce.GTC if market == "crypto" else TimeInForce.DAY,
                                 client_order_id=make_order_id(sleeve or "legacy", "sell"))
    for attempt in range(3 if waited_on_stop else 1):
        try:
            return trading.submit_order(order_data=request)
        except Exception:
            if attempt == (2 if waited_on_stop else 0):
                raise
            time.sleep(1)


def exit_position(trading, market, symbol, position, state, reason, equity, log_event, working=()):
    """Cancel any waiting stop, then sell the whole position with a market order, so an exit can't
    sit unfilled. A refused sell is logged and the run carries on, so it can't block the other stops."""
    sleeve = owner_of(state, symbol)
    qty, entry, price = float(position.qty), float(position.avg_entry_price), float(position.current_price or 0)
    try:
        cancel_stops(trading, working)
        order = sell_all(trading, market, symbol, qty, sleeve, bool(working))
    except Exception as error:
        print(f"  {symbol}: sell failed")
        log_event(event="ERROR", symbol=symbol, equity=equity,
                  details=" ".join(notifier.redact(f"{symbol} sell failed: {error}").split())[:300])
        return False
    log_event(event="ORDER", symbol=symbol, side="sell", qty=qty, price=price, order_id=str(order.id),
              order_status=crypto.text(order.status), equity=equity, details=tagged(sleeve, reason))
    state["positions"].pop(symbol, None)
    if sleeve:
        state["closed_pl"][sleeve] = round(state["closed_pl"].get(sleeve, 0.0) + (price - entry) * qty, 2)
    save_state(state)
    return True


def buy(trading, market, symbol, qty, price, sleeve):
    """Stocks use market orders. Crypto uses limit orders, to pay the lower maker fee."""
    if market == "crypto":
        request = LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
                                    limit_price=round(price, 9), client_order_id=make_order_id(sleeve, "buy"))
    else:
        request = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                                     client_order_id=make_order_id(sleeve, "buy"))
    return trading.submit_order(order_data=request)


def cancel_stale_buys(trading, state, open_buys, positions, today, dry_run):
    """A crypto limit buy still unfilled the next day is dropped. Its signal gets checked afresh."""
    stale = [symbol for symbol, order in open_buys.items() if str(order.created_at)[:10] < today]
    for symbol in stale:
        print(f"  {symbol}: unfilled limit buy from an earlier day" + (" (dry run, kept)" if dry_run else ", cancelled"))
        if dry_run:
            continue
        trading.cancel_order_by_id(open_buys[symbol].id)
        if symbol not in positions:
            state["positions"].pop(symbol, None)
    return set(stale)


def already_vetoed(todays_log, symbol):
    return config.AI_VETO_MODE == "enforce" and any(
        row["event"] == "AI_DECISION" and row["symbol"] == symbol and row["ai_decision"] == ai_analyst.VETO
        for row in todays_log)


def record_filled_stops(trading, state, equity, log_event):
    """A stop that filled at Alpaca never passed through this bot, so write it into the books here."""
    request = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500,
                               after=datetime.now(timezone.utc) - timedelta(days=5))
    seen = state.get("stop_fills", [])
    for order in trading.get_orders(filter=request):
        order_id = str(order.id)
        parts = str(order.client_order_id or "").split("-")
        if len(parts) < 4 or parts[0] != "tach" or parts[2] != "stop" or crypto.text(order.status) != "filled":
            continue
        if order_id in seen:
            continue
        seen.append(order_id)
        sleeve = "" if parts[1] == "legacy" else parts[1]
        qty, fill = float(order.filled_qty), float(order.filled_avg_price)
        entry = float(order.stop_price) / (1 - config.STOP_LOSS_PCT)
        log_event(event="ORDER", symbol=order.symbol, side="sell", qty=qty, price=fill, order_id=order_id,
                  order_status="filled", equity=equity,
                  details=tagged(sleeve, f"stop-loss: filled at Alpaca, {fill / entry - 1:+.1%} from buy price {entry:,.2f}"))
        if sleeve:
            state["closed_pl"][sleeve] = round(state["closed_pl"].get(sleeve, 0.0) + (fill - entry) * qty, 2)
        if sleeve in ("momentum_stocks", "rotation"):
            state.setdefault(sleeve.split("_")[0], {"month": "", "targets": [], "stopped": []})["stopped"].append(order.symbol)
    if seen:
        state["stop_fills"] = seen[-200:]


# ---------------------------------------------------------------------------
# One market's run
# ---------------------------------------------------------------------------
def run(market, trading, equity, cash, killed, dry_run, today, todays_log, log_event, download, latest_price,
        save_plan, pre_open=False):
    """Stops on every run, then signals and orders on the first run of the day. pre_open means the
    market opens shortly: orders queue to fill at the open, and prices are too stale to judge stops."""
    print(f"\n=== Sleeves · {market} · on: {', '.join(sleeves_for(market)) or 'none'} ===")
    state = load_state()
    crypto_side = market == "crypto"
    name = crypto.normalize if crypto_side else str
    positions = {name(p.symbol): p for p in trading.get_all_positions() if crypto.is_crypto(p) == crypto_side}
    orders = [o for o in trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
              if crypto.is_crypto(o) == crypto_side]
    alpaca_stops = market == "stock" and config.STOP_EXECUTION == "alpaca"
    working = stops.by_symbol(orders)
    open_sells = {name(o.symbol) for o in orders if crypto.text(o.side) == "sell" and not stops.is_stop(o)}
    open_buys = tagged_buys(orders, name)
    if alpaca_stops and not dry_run:
        record_filled_stops(trading, state, equity, log_event)
    closed_buys = {}
    if set(positions) - set(state["positions"]):      # an owner may need rebuilding from the order ids
        request = GetOrdersRequest(status=QueryOrderStatus.CLOSED, side=OrderSide.BUY, limit=500)
        filled = [o for o in trading.get_orders(filter=request)
                  if crypto.is_crypto(o) == crypto_side and float(o.filled_qty or 0) > 0]
        closed_buys = tagged_buys(filled, name)
    reconcile(state, market, positions, open_buys, closed_buys)

    # --- Stops, on every run. They always go through. ---------------------------
    # With a stop waiting at Alpaca, this check is only the backup.
    sold = set()
    for symbol, position in positions.items():
        entry, price = float(position.avg_entry_price), float(position.current_price or 0)
        record = state["positions"].get(symbol)
        if pre_open or float(position.qty) <= 0 or price <= 0 or symbol in open_sells \
                or (alpaca_stops and symbol in working) or not stop_hit(record, entry, price):
            continue
        reason = f"stop-loss: {price / entry - 1:+.1%} from buy price {entry:,.2f}"
        print(f"  {symbol}: {reason}" + (" (dry run, not sold)" if dry_run else ""))
        sleeve = owner_of(state, symbol)
        if not dry_run and exit_position(trading, market, symbol, position, state, reason, equity, log_event):
            sold.add(symbol)
            if sleeve == "momentum_stocks":
                state["momentum"]["stopped"].append(symbol)
            elif sleeve == "rotation":
                state.setdefault("rotation", {"month": "", "targets": [], "stopped": []})["stopped"].append(symbol)

    first_run = state["days"].get(market) != today
    if alpaca_stops:
        fixes, _ = stops.sync(trading, positions, working, sold | open_sells, state["positions"], today, first_run, dry_run)
        working = stops.by_symbol(trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)))
        if fixes and not dry_run:
            log_event(event="STOP_FIXED", equity=equity, details="; ".join(fixes))

    if state["days"].get(market) == today and not dry_run:
        save_state(state)
        return state

    # --- First run of the day: signals from finished days ----------------------
    wanted_symbols = {row["symbol"] for row in universe.rows(market)} | set(positions)
    if not crypto_side:
        wanted_symbols.add(config.MOMENTUM_MARKET_SYMBOL)
        if is_enabled("rotation"):
            wanted_symbols.update(config.ROTATION_SYMBOLS)
        if is_enabled("turn_of_month"):
            wanted_symbols.add(config.TURN_OF_MONTH_SYMBOL)
    tables = download(sorted(wanted_symbols), datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS))
    latest = {symbol: strategies.indicators(table).iloc[-1] for symbol, table in tables.items()
              if len(table) > config.TREND_FILTER_SMA_DAYS}
    if not latest:
        print("  No prices came back, so there are no signals yet. The next run tries again.")
        save_state(state)
        return state

    if crypto_side and config.USE_ATR_STOPS:          # crypto stops trail upwards, once a day
        for symbol, record in state["positions"].items():
            if symbol in latest and symbol in positions and not pd.isna(latest[symbol]["atr"]):
                trailing = risk.atr_stop(float(latest[symbol]["close"]), float(latest[symbol]["atr"]))
                record["stop"] = round(max(record.get("stop") or 0, trailing), 9)

    month = max(row.name for row in latest.values()).strftime("%Y-%m")
    held_days = {s: days_held(tables[s], r.get("entry_date")) for s, r in state["positions"].items() if s in tables}
    calendar = trading_days(trading, today) if not crypto_side and (
        is_enabled("turn_of_month") or any(r["sleeve"] == "turn_of_month" for r in state["positions"].values())) else []
    exits, entries = daily_decisions(market, latest, state, month, held_days, tables, calendar, today)
    legacy = {s: tables[s] for s in positions if s not in state["positions"] and s in tables}
    for candidate in signals.scan(legacy, set(legacy), latest_signal):
        if candidate["action"] == SELL:
            exits[candidate["symbol"]] = candidate["reason"]
    print(f"  {len(latest)} checked · {len(exits)} to sell · {len(entries)} entries wanted")

    cancelled = cancel_stale_buys(trading, state, open_buys, positions, today, dry_run) if crypto_side else set()

    # --- Exits always go through ---------------------------------------------------
    for symbol, reason in exits.items():
        if symbol not in positions or symbol in sold or symbol in open_sells:
            continue
        print(f"  {symbol}: sell · {reason}")
        if not dry_run and exit_position(trading, market, symbol, positions[symbol], state, reason, equity, log_event,
                                         working.get(symbol, []) if alpaca_stops else ()):
            sold.add(symbol)

    # --- New entries, short-term sleeves first ---------------------------------------
    # Money from today's sells only counts tomorrow, so a buy can never lean on margin.
    placed_today = sum(row["event"] == "ORDER" and row.get("side") == "buy" and in_market(row["symbol"], market)
                       for row in todays_log)
    busy = (set(positions) | sold | {name(o.symbol) for o in orders} | set(state["positions"])
            | {row["symbol"] for row in todays_log if row["event"] == "ORDER"}) - cancelled
    open_count = len(set(positions) - sold)
    ai_left = config.MAX_AI_REVIEWS_PER_DAY[market]
    added, shortlist = {}, []

    def note(candidate, why):
        if why and len(shortlist) >= config.MAX_AI_REVIEWS_PER_DAY[market]:
            return
        candidate.update(note=why)
        shortlist.append(candidate)

    for sleeve, symbol in entries:
        if symbol in busy:
            continue
        candidate = {"rank": len(shortlist) + 1, "symbol": symbol, "sleeve": sleeve, "action": "BUY",
                     "reason": f"{config.SLEEVES[sleeve]['horizon'].lower()}-term entry signal", "trend": "",
                     "signal_date": today, "name": "", "status": "skipped", "ai_decision": "", "ai_reason": "",
                     "note": ""}
        if killed:
            note(candidate, "kill switch on")
            continue
        if placed_today >= config.MAX_TRADES_PER_DAY or ai_left <= 0:
            note(candidate, "daily entry limit reached")
            continue
        if open_count >= config.SLEEVE_MAX_OPEN_POSITIONS[market]:
            note(candidate, "position limit reached")
            continue
        row = latest[symbol]
        price = latest_price(symbol, float(row["close"]))
        stop = stop_for(price, float(row["atr"]))
        used = exposure(state, positions, sleeve) + added.get(sleeve, 0.0)
        scale = state["momentum"].get("scale", 1.0) if sleeve == "momentum_stocks" and config.VOL_SCALED_MOMENTUM else 1.0
        dollars = risk.risk_sized_dollars(equity, price, stop, entry_room(sleeve, equity, used, scale))
        if config.VOL_SIZING and not crypto_side:
            typical = float(pd.Series([r["vol"] for r in latest.values()]).median())
            dollars = risk.vol_sized_dollars(dollars, float(row["vol"]), typical, equity, equity * share_of(sleeve) - used)
        dollars = min(dollars, cash)
        qty = units(dollars / price, market, not crypto_side and fractionable(trading, symbol))
        if qty <= 0 or (qty * price < 1 and not crypto_side):
            note(candidate, "no room left in this sleeve, or not enough cash")
            continue
        busy.add(symbol)
        if dry_run:
            print(f"  {symbol}: would buy {qty:g} for {sleeve}, about ${qty * price:,.2f}")
            candidate["status"] = "dry run"
            note(candidate, "")
            placed_today += 1
            open_count += 1
            added[sleeve] = added.get(sleeve, 0.0) + qty * price
            cash -= qty * price
            continue
        if already_vetoed(todays_log, symbol):
            continue
        ai_left -= 1
        prices = tables[symbol] if config.USE_AI_ANALYST else None
        decision, ai_reason = ai_analyst.review(symbol, "BUY", candidate["reason"], prices, RULE_TEXT.get(sleeve))
        candidate.update(ai_decision=decision, ai_reason=ai_reason if decision != ai_analyst.OFF else "")
        if decision != ai_analyst.OFF:
            log_event(event="AI_DECISION", symbol=symbol, signal="BUY", signal_date=today, ai_decision=decision,
                      ai_reason=ai_reason, equity=equity, details=tagged(sleeve, candidate["reason"]))
        if decision == ai_analyst.VETO and config.AI_VETO_MODE == "enforce":
            candidate["status"] = "vetoed"
            note(candidate, "")
            continue
        try:
            order = buy(trading, market, symbol, qty, price, sleeve)
        except Exception as error:
            log_event(event="ERROR", symbol=symbol, equity=equity,
                      details=" ".join(notifier.redact(f"{symbol} buy failed: {error}").split())[:300])
            note(candidate, "order refused")
            continue
        log_event(event="ORDER", symbol=symbol, signal="BUY", signal_date=today, side="buy", qty=float(qty),
                  price=price, order_id=str(order.id), order_status=crypto.text(order.status), equity=equity,
                  ai_decision=decision, ai_reason=ai_reason, details=tagged(sleeve, candidate["reason"]))
        state["positions"][symbol] = {"sleeve": sleeve, "entry_date": today, "stop": round(stop, 9)}
        save_state(state)
        candidate["status"] = "ordered"
        note(candidate, "")
        print(f"  {symbol}: bought {qty:g} for {sleeve}")
        placed_today += 1
        open_count += 1
        added[sleeve] = added.get(sleeve, 0.0) + qty * price
        cash -= qty * price

    if not dry_run:
        state["days"][market] = today
        save_state(state)
        save_plan({"day": today, "scanned": len(latest), "found": len(entries), "candidates": shortlist})
    return state


def summary(position_rows):
    """Per-sleeve numbers for the dashboard."""
    closed = load_state()["closed_pl"]
    rows = []
    for name, sleeve in config.SLEEVES.items():
        mine = [row for row in position_rows if row.get("sleeve") == name]
        if sleeve["market"] == "crypto" and not config.CRYPTO_ENABLED and not mine:
            continue
        rows.append({"name": name, "horizon": sleeve["horizon"], "market": sleeve["market"],
                     "allocation": share_of(name), "enabled": is_enabled(name), "positions": len(mine),
                     "value": round(sum(row["market_value"] or 0 for row in mine), 2),
                     "open_pl": round(sum(row["unrealized_pl"] or 0 for row in mine), 2),
                     "closed_pl": closed.get(name, 0.0)})
    return {"mode": config.STRATEGY_MODE, "horizons": config.HORIZONS, "sleeves": rows}
