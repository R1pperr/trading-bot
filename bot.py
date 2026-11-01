"""
bot.py  -  one run of the paper-trading bot. PAPER ONLY: paper=True, no live mode.

What one run does, in order:
  1. Logs in to your Alpaca PAPER account.
  2. Kill switch: if equity is down KILL_SWITCH_PCT (3%) or more since the
     previous trading day's close, no new trades for the rest of the day.
  3. Stop-loss: checks EVERY open position and sells any that is STOP_LOSS_PCT
     (7%) or more below its buy price.
  4. Downloads daily prices and works out each symbol's signal (strategy.py).
  5. For each BUY or SELL: trade limit -> size cap (5% of equity) ->
     optional AI approve/veto -> market order.
  6. Writes every signal, AI decision, order and block to logs/trade_log.csv.

Orders are only placed while the US market is open; at other times a run just
says so and stops. Run it as often as you like (e.g. every hour): it never
trades the same symbol twice in one day, so extra runs just re-check the
stop-losses.

    python bot.py               normal run
    python bot.py --dry-run     show what it would do, place nothing
    python bot.py --test-order  buy 1 share of SPY to check orders reach your dashboard
"""

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

import ai_analyst
import config
import dashboard
import notifier
import risk
from data import NEW_YORK, get_daily_bars
from strategy import BUY, SELL, latest_signal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "logs", "trade_log.csv")
LOG_COLUMNS = ["time_utc", "event", "symbol", "signal", "signal_date", "details",
               "ai_decision", "ai_reason", "side", "qty", "price", "order_id",
               "order_status", "equity"]
NOT_A_TRADE = {"canceled", "rejected", "expired"}
RUN_EVENTS = []   # rows logged during this run, for the phone alerts at the end


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def text(value):
    """Alpaca returns some values as enums; turn them into plain text."""
    return str(getattr(value, "value", value) or "")


def money(x):
    return f"${x:,.2f}"


def new_york_now():
    return datetime.now(NEW_YORK)


def start_of_today_utc():
    midnight = new_york_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# The CSV log
# ---------------------------------------------------------------------------
def log_event(**fields):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    is_new = not os.path.exists(LOG_FILE)
    row = {column: "" for column in LOG_COLUMNS}
    row["time_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for key, value in fields.items():
        if key in row and value is not None:
            row[key] = round(value, 4) if isinstance(value, float) else value
    RUN_EVENTS.append(row)
    with open(LOG_FILE, "a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def read_todays_log():
    """Rows written today (New York date). Used so repeated runs don't repeat themselves."""
    if not os.path.exists(LOG_FILE):
        return []
    start = start_of_today_utc().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, newline="") as handle:
        return [row for row in csv.DictReader(handle) if row.get("time_utc", "") >= start]


def already_logged(todays_log, event, symbol, signal_date, decision=None):
    for row in todays_log:
        if row["event"] == event and row["symbol"] == symbol and row["signal_date"] == signal_date:
            if decision is None or row["ai_decision"] == decision:
                return True
    return False


# ---------------------------------------------------------------------------
# Talking to Alpaca
# ---------------------------------------------------------------------------
def orders_placed_today(trading):
    request = GetOrdersRequest(status=QueryOrderStatus.ALL, after=start_of_today_utc(), limit=500)
    return [o for o in trading.get_orders(filter=request) if text(o.status) not in NOT_A_TRADE]


def latest_price(data_client, symbol, fallback):
    """Most recent trade price from the free IEX feed (falls back to the last close)."""
    try:
        request = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
        return float(data_client.get_stock_latest_trade(request)[symbol].price)
    except Exception:
        return fallback


def place_order(trading, symbol, side, qty, wait_for_fill):
    """Submit a PAPER market order. Returns (order_id, status, fill_price or None)."""
    request = MarketOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY)
    order = trading.submit_order(order_data=request)
    status = text(order.status)
    if wait_for_fill:
        for _ in range(5):  # give the paper exchange up to ~5 seconds to fill it
            if status == "filled":
                break
            time.sleep(1)
            try:
                order = trading.get_order_by_id(order.id)
                status = text(order.status)
            except Exception:
                break
    fill = float(order.filled_avg_price) if order.filled_avg_price else None
    return str(order.id), status, fill


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def run(args):
    api_key, secret_key = config.get_alpaca_keys()
    trading = TradingClient(api_key, secret_key, paper=True)   # PAPER ONLY
    data_client = StockHistoricalDataClient(api_key, secret_key)

    account = trading.get_account()
    if account.trading_blocked or account.account_blocked:
        print("❌ Alpaca says this account is blocked from trading. Check your dashboard.")
        log_event(event="ERROR", details="account is blocked from trading")
        return 1

    equity = float(account.equity)
    start_equity = float(account.last_equity)
    change = risk.daily_change(equity, start_equity)
    market_open = trading.get_clock().is_open
    dry_run = args.dry_run

    print(f"=== Trading bot · Alpaca PAPER · {new_york_now():%a %d %b %Y, %H:%M} New York time ===")
    print(f"Equity {money(equity)} (yesterday's close {money(start_equity)}, {change:+.2%})"
          f" · Market {'OPEN' if market_open else 'CLOSED'}")
    if not market_open and not dry_run and not args.test_order:
        print("The US market is closed, so there is nothing to do.")
        print("To see what the bot would do right now:  python bot.py --dry-run")
        return 0
    if dry_run:
        print("DRY RUN: showing what the bot would do. No orders placed, nothing logged.")

    todays_log = read_todays_log()
    todays_orders = orders_placed_today(trading)
    trades_today = len(todays_orders)
    traded_today = {o.symbol for o in todays_orders}
    positions = {p.symbol: p for p in trading.get_all_positions()}

    # --- Rule 4: kill switch -------------------------------------------------
    killed_earlier = any(row["event"] == "KILL_SWITCH" for row in todays_log)
    killed = killed_earlier or risk.kill_switch_triggered(equity, start_equity)
    if killed and not killed_earlier and not dry_run:
        log_event(event="KILL_SWITCH", equity=equity,
                  details=f"equity {change:+.2%} since yesterday's close (limit "
                          f"-{config.KILL_SWITCH_PCT:.0%}); no new trades today")
    print(f"Trades today: {trades_today} of {config.MAX_TRADES_PER_DAY}"
          f" · Kill switch: {'ON, no new trades today' if killed else 'off'}")

    if args.test_order:
        return run_test_order(trading, data_client, args.test_order.upper(), equity,
                              positions, trades_today, killed, market_open)

    # --- Rule 2: stop-loss on every position ----------------------------------
    print("\nStop-loss check:")
    if not positions:
        print("  No open positions.")
    sold_now = set()
    for symbol, position in positions.items():
        qty = float(position.qty)
        entry = float(position.avg_entry_price)
        price = float(position.current_price or 0)
        if qty <= 0:
            continue
        line = f"  {symbol}: {qty:g} shares, bought {money(entry)}, now {money(price)} ({price / entry - 1:+.1%})"
        if not risk.stop_loss_hit(entry, price):
            print(f"{line} · stop at {money(risk.stop_price(entry))} · OK")
            continue
        details = (f"stop-loss: {price / entry - 1:+.1%} from buy price {money(entry)} "
                   f"(limit -{config.STOP_LOSS_PCT:.0%})")
        if dry_run:
            print(f"{line} · STOP-LOSS HIT, would sell {qty:g}")
            continue
        order_id, status, fill = place_order(trading, symbol, OrderSide.SELL, qty, market_open)
        log_event(event="ORDER", symbol=symbol, side="sell", qty=qty, price=fill or price,
                  order_id=order_id, order_status=status, details=details, equity=equity)
        print(f"{line} · STOP-LOSS HIT → SOLD {qty:g} ({status})")
        trades_today += 1
        traded_today.add(symbol)
        sold_now.add(symbol)

    # --- Signals --------------------------------------------------------------
    history_start = datetime.now(timezone.utc) - timedelta(days=int(config.SLOW_SMA_DAYS * 1.6) + 60)
    bars = get_daily_bars(config.SYMBOLS, history_start)
    candidates = []
    print("\nSignals:")
    for symbol in config.SYMBOLS:
        prices = bars.get(symbol)
        if prices is None or len(prices) < config.SLOW_SMA_DAYS + 1:
            print(f"  {symbol}: not enough price history, skipped")
            continue
        signal = latest_signal(prices)
        holding = symbol in positions and symbol not in sold_now
        action, note = None, ""
        if signal["signal"] == BUY:
            action, note = (None, "already holding") if holding else (BUY, "")
        elif signal["signal"] == SELL:
            action, note = (SELL, "") if holding else (None, "no position, nothing to sell")
        elif holding and signal["trend"] == "DOWN":
            action, note = SELL, "holding while the trend is down (sell signal was missed)"

        print(f"  {symbol:5} {signal['signal']:4} {signal['reason']}"
              f" [close of {signal['date']}]" + (f" · {note}" if note else ""))

        logged_signal = signal["signal"] if signal["signal"] != "HOLD" else (SELL if action else None)
        if logged_signal and not dry_run and not already_logged(todays_log, "SIGNAL", symbol, signal["date"]):
            log_event(event="SIGNAL", symbol=symbol, signal=logged_signal, signal_date=signal["date"],
                      details=f"{signal['reason']}" + (f"; {note}" if note else ""), equity=equity)
        if action:
            candidates.append((symbol, action, signal, prices))

    # Sells first (they free up cash and reduce risk), then buys.
    candidates.sort(key=lambda c: 0 if c[1] == SELL else 1)
    if candidates:
        print("\nOrders:")
    for symbol, action, signal, prices in candidates:
        placed = handle_candidate(trading, data_client, symbol, action, signal, prices, positions,
                                  equity, todays_log, dry_run, killed, market_open,
                                  trades_today, traded_today)
        if placed:
            trades_today += 1

    print("\nDone." + ("" if dry_run else f" Log: {os.path.relpath(LOG_FILE, BASE_DIR)}"))
    return 0


def handle_candidate(trading, data_client, symbol, action, signal, prices, positions,
                     equity, todays_log, dry_run, killed, market_open, trades_today, traded_today):
    """Run one BUY/SELL through the risk rules and the AI. Returns True if an order was placed."""
    date = signal["date"]
    side_word = "buy" if action == BUY else "sell"

    def block(reason):
        print(f"  {symbol}: {side_word} BLOCKED · {reason}")
        if not dry_run and not already_logged(todays_log, "BLOCKED", symbol, date):
            log_event(event="BLOCKED", symbol=symbol, signal=action, signal_date=date,
                      side=side_word, details=reason, equity=equity)

    if symbol in traded_today:
        print(f"  {symbol}: already traded today, skipping")
        return

    # --- Rule 1: size (and what we would sell) ---------------------------------
    if action == BUY:
        price = latest_price(data_client, symbol, fallback=signal["close"])
        qty = risk.max_shares_to_buy(equity, price)
        if qty < 1:
            return block(f"{config.MAX_POSITION_PCT:.0%} of equity ({money(equity * config.MAX_POSITION_PCT)})"
                         f" is less than one share ({money(price)})")
        size_note = f"{qty} shares ≈ {money(qty * price)} ({qty * price / equity:.1%} of equity)"
    else:
        position = positions[symbol]
        qty = float(position.qty)
        price = float(position.current_price or signal["close"])
        size_note = f"all {qty:g} shares ≈ {money(qty * price)}"

    if dry_run:
        print(f"  {symbol}: would {side_word} {size_note}" +
              (" (but the kill switch is ON)" if killed else ""))
        return
    # --- Rule 4 and Rule 3 -------------------------------------------------------
    if killed:
        return block(f"kill switch is on (equity fell {config.KILL_SWITCH_PCT:.0%}+ today)")
    if risk.trades_left(trades_today) == 0:
        return block(f"daily trade limit reached ({config.MAX_TRADES_PER_DAY})")

    # --- Optional AI analyst: approve or veto only -----------------------------
    if already_logged(todays_log, "AI_DECISION", symbol, date, decision=ai_analyst.VETO):
        print(f"  {symbol}: AI already vetoed this signal today, skipping")
        return
    decision, ai_reason = ai_analyst.review(symbol, action, signal["reason"], prices)
    if decision != ai_analyst.OFF:
        log_event(event="AI_DECISION", symbol=symbol, signal=action, signal_date=date,
                  ai_decision=decision, ai_reason=ai_reason, equity=equity)
        print(f"  {symbol}: AI analyst says {decision} · {ai_reason}")
    if decision == ai_analyst.VETO:
        return

    # --- Place the PAPER order ------------------------------------------------
    side = OrderSide.BUY if action == BUY else OrderSide.SELL
    order_id, status, fill = place_order(trading, symbol, side, qty, market_open)
    log_event(event="ORDER", symbol=symbol, signal=action, signal_date=date, side=side_word,
              qty=float(qty), price=fill or price, order_id=order_id, order_status=status,
              ai_decision=decision, ai_reason=ai_reason,
              details=f"strategy {action}: {size_note} · {signal['reason']}", equity=equity)
    fill_text = f" at {money(fill)}" if fill else ""
    print(f"  {symbol}: {side_word.upper()} {size_note} → order {status}{fill_text}")
    traded_today.add(symbol)
    return True


def run_test_order(trading, data_client, symbol, equity, positions, trades_today, killed, market_open):
    print(f"\nTest order: buy 1 share of {symbol} (not a strategy signal; the AI is not asked).")
    if killed:
        print("❌ Not placed: the kill switch is on today.")
        return 1
    if risk.trades_left(trades_today) == 0:
        print(f"❌ Not placed: today's trade limit ({config.MAX_TRADES_PER_DAY}) is already used.")
        return 1
    price = latest_price(data_client, symbol, fallback=0.0)
    if price <= 0:
        print(f"❌ Not placed: could not get a price for {symbol}. Check the ticker.")
        return 1
    held_value = float(positions[symbol].market_value) if symbol in positions else 0.0
    if risk.max_shares_to_buy(equity, price, held_value) < 1:
        print(f"❌ Not placed: one more share would take {symbol} above "
              f"{config.MAX_POSITION_PCT:.0%} of your equity.")
        return 1

    order_id, status, fill = place_order(trading, symbol, OrderSide.BUY, 1, market_open)
    log_event(event="ORDER", symbol=symbol, side="buy", qty=1.0, price=fill or price,
              order_id=order_id, order_status=status, equity=equity,
              details="manual test order (not a strategy signal)")
    print(f"✅ Paper order sent: buy 1 {symbol} · status: {status}" + (f" at {money(fill)}" if fill else ""))
    if not market_open:
        print("   The market is closed, so it will fill when the US market next opens.")
    print(f"   Order id: {order_id}")
    print("   Now look for it in your Alpaca dashboard (Paper account) under Orders.")
    return 0


def record_crash(details):
    try:
        log_event(event="ERROR", details=details)
    except Exception:
        pass


def crash_key(row):
    """The error text with numbers removed, so the same problem matches itself."""
    return re.sub(r"\d+", "#", row["details"])


def alerts_for(rows):
    """This run's rows, minus a crash alert you already got today for the same problem."""
    try:
        todays_crashes = [crash_key(row) for row in read_todays_log() if row["event"] == "ERROR"]
    except Exception:
        return rows
    # Today's log already includes this run's own crash, so a repeat shows up twice.
    return [row for row in rows
            if row["event"] != "ERROR" or todays_crashes.count(crash_key(row)) < 2]


def summary_due(now):
    """True on a weekday from config.DAILY_SUMMARY_UTC onwards, around the day's last run."""
    return now.weekday() < 5 and now.astimezone(timezone.utc).strftime("%H:%M") >= config.DAILY_SUMMARY_UTC


def send_daily_summary(args):
    """One message a day, so a quiet phone can't hide a bot that stopped running."""
    try:
        if args.dry_run or not summary_due(new_york_now()):
            return
        todays_log = read_todays_log()
        if any(row["event"] == "DAILY_SUMMARY" for row in todays_log):
            return
        orders_today = sum(row["event"] == "ORDER" for row in todays_log)
        if notifier.send(dashboard.summary_text(orders_today)):
            log_event(event="DAILY_SUMMARY")
    except Exception as error:
        print("⚠️  Daily summary not sent:", notifier.redact(error))


def finish_run(args, exit_code):
    """
    After a run: tell your phone what happened, then refresh the dashboard data.
    Every order has already gone through by now, and none of this can stop the bot.
    """
    if not args.dry_run:
        notifier.notify(alerts_for(RUN_EVENTS))
    errors = [row["details"] for row in RUN_EVENTS if row["event"] == "ERROR"]
    dashboard.update(LOG_FILE, start_of_today_utc(), exit_code,
                     errors[-1] if errors else "", args.dry_run)
    send_daily_summary(args)


def main():
    parser = argparse.ArgumentParser(description="Run the paper-trading bot once.")
    parser.add_argument("--dry-run", action="store_true", help="show decisions without placing orders")
    parser.add_argument("--test-order", nargs="?", const="SPY", default=None, metavar="SYMBOL",
                        help="buy 1 share (default SPY) to check orders reach your dashboard")
    args = parser.parse_args()
    os.chdir(BASE_DIR)
    exit_code = 1
    try:
        exit_code = run(args)
    except SystemExit:   # config.py stops the run this way when the keys are missing
        record_crash("Alpaca keys are missing or unreadable")
    except Exception as error:
        print("❌ The run stopped because of an error:", error)
        record_crash(" ".join(str(error).split())[:300])
    finish_run(args, exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
