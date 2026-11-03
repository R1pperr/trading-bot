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
     optional AI review -> market order.
  6. Writes every signal, AI decision, order and block to logs/trade_log.csv.

Orders are only placed while the US market is open, or in the half hour before
it, when they queue to fill at the open. At other times a run just says so and
stops. Run it as often as you like (e.g. every hour): it never trades the same
symbol twice in one day, so extra runs just re-check the stop-losses.

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
import crypto
import dashboard
import notifier
import risk
import signals
import sleeves
import universe
from data import NEW_YORK, get_crypto_daily_bars, get_crypto_latest_price, get_daily_bars
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


def read_crypto_days_log():
    """Rows written since midnight UTC, because crypto's day runs on UTC."""
    if not os.path.exists(LOG_FILE):
        return []
    start = crypto.utc_midnight().strftime("%Y-%m-%d %H:%M:%S")
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
    """Stock orders only: crypto keeps its own count, so it can't use up the stocks' trades."""
    request = GetOrdersRequest(status=QueryOrderStatus.ALL, after=start_of_today_utc(), limit=500)
    return [o for o in trading.get_orders(filter=request)
            if text(o.status) not in NOT_A_TRADE and not crypto.is_crypto(o)]


def latest_price(data_client, symbol, fallback):
    """Most recent trade price from the free IEX feed (falls back to the last close)."""
    try:
        request = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
        return float(data_client.get_stock_latest_trade(request)[symbol].price)
    except Exception:
        return fallback


def is_pre_open(clock):
    """True in the half hour before a trading day's open. Alpaca's clock knows the holidays."""
    opens, now = getattr(clock, "next_open", None), getattr(clock, "timestamp", None)
    if getattr(clock, "is_open", False) or opens is None or now is None:
        return False
    return timedelta(0) < opens - now <= timedelta(minutes=config.PRE_OPEN_MINUTES)


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
    config.require_paper_account(trading, account)
    if account.trading_blocked or account.account_blocked:
        print("❌ Alpaca says this account is blocked from trading. Check your dashboard.")
        log_event(event="ERROR", details="account is blocked from trading")
        return 1

    equity = float(account.equity)
    start_equity = float(account.last_equity)
    change = risk.daily_change(equity, start_equity)
    clock = trading.get_clock()
    market_open = clock.is_open
    pre_open = config.STRATEGY_MODE == "sleeves" and is_pre_open(clock)
    dry_run = args.dry_run

    print(f"=== Trading bot · Alpaca PAPER · {new_york_now():%a %d %b %Y, %H:%M} New York time ===")
    print(f"Equity {money(equity)} (yesterday's close {money(start_equity)}, {change:+.2%})"
          f" · Market {'OPEN' if market_open else 'CLOSED'}")
    if not market_open and not pre_open and not dry_run and not args.test_order:
        print("The US market is closed, so there is nothing to do.")
        print("To see what the bot would do right now:  python bot.py --dry-run")
        return 0
    if dry_run:
        print("DRY RUN: showing what the bot would do. No orders placed, nothing logged.")

    todays_log = read_todays_log()
    todays_orders = orders_placed_today(trading)
    trades_today = len(todays_orders)
    traded_today = {o.symbol for o in todays_orders}
    positions = {p.symbol: p for p in trading.get_all_positions() if not crypto.is_crypto(p)}

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

    if config.STRATEGY_MODE == "sleeves":
        spendable = min(float(account.cash), float(account.non_marginable_buying_power))
        sleeves.run("stock", trading, equity, spendable, killed, dry_run,
                    new_york_now().date().isoformat(), todays_log, log_event, get_daily_bars,
                    lambda symbol, fallback: latest_price(data_client, symbol, fallback),
                    lambda plan: dashboard.save_candidates("stock", plan), pre_open=pre_open)
        print("\nDone.")
        return 0

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

    # --- Signals: worked out once a day, then read from the saved list ---------
    today = new_york_now().date().isoformat()
    history_start = datetime.now(timezone.utc) - timedelta(days=int(config.SLOW_SMA_DAYS * 1.6) + 60)
    plan = dashboard.load_candidates().get("stock", {})
    bars = {}
    if dry_run or plan.get("day") != today:
        rows = universe.rows("stock")
        bars = get_daily_bars(sorted({row["symbol"] for row in rows} | set(positions)), history_start)
        found = signals.scan(bars, set(positions) - sold_now, latest_signal)
        plan = signals.start_day("stock", today, found, rows, len(bars))
    signals.describe(plan, "Signals")

    def prices_for(symbol):
        """Closing prices for the AI. A later run downloads them for just this symbol."""
        if symbol not in bars:
            bars.update(get_daily_bars([symbol], history_start))
        return bars.get(symbol)

    waiting = signals.waiting(plan)
    if waiting:
        print("\nOrders:")
    open_count = len(positions) - len(sold_now)
    try:
        for candidate in waiting:
            if not dry_run and not already_logged(todays_log, "SIGNAL", candidate["symbol"], candidate["signal_date"]):
                log_event(event="SIGNAL", symbol=candidate["symbol"], signal=candidate["action"],
                          signal_date=candidate["signal_date"], details=candidate["reason"], equity=equity)
            placed = handle_candidate(trading, data_client, candidate, prices_for, positions, open_count,
                                      equity, todays_log, dry_run, killed, market_open,
                                      trades_today, traded_today)
            if placed:
                trades_today += 1
                open_count += 1 if candidate["action"] == BUY else -1
    finally:
        if not dry_run:
            dashboard.save_candidates("stock", plan)

    print("\nDone." + ("" if dry_run else f" Log: {os.path.relpath(LOG_FILE, BASE_DIR)}"))
    return 0


def handle_candidate(trading, data_client, candidate, prices_for, positions, open_count,
                     equity, todays_log, dry_run, killed, market_open, trades_today, traded_today):
    """Run one BUY/SELL through the risk rules and the AI. Returns True if an order was placed."""
    symbol, action, date = candidate["symbol"], candidate["action"], candidate["signal_date"]
    reason = candidate["reason"]
    side_word = "buy" if action == BUY else "sell"

    def block(why):
        print(f"  {symbol}: {side_word} BLOCKED · {why}")
        candidate["note"] = why
        if not dry_run and not already_logged(todays_log, "BLOCKED", symbol, date):
            log_event(event="BLOCKED", symbol=symbol, signal=action, signal_date=date,
                      side=side_word, details=why, equity=equity)

    if symbol in traded_today:
        print(f"  {symbol}: already traded today, skipping")
        candidate["status"] = "already traded"
        return
    if action == SELL and symbol not in positions:
        candidate["status"] = "no longer held"
        return

    # --- Rule 4, Rule 3 and the position limit, before any more calls are made ---
    if not dry_run:
        if killed:
            return block(f"kill switch is on (equity fell {config.KILL_SWITCH_PCT:.0%}+ today)")
        if risk.trades_left(trades_today) == 0:
            return block(f"daily trade limit reached ({config.MAX_TRADES_PER_DAY})")
        if action == BUY and open_count >= config.MAX_OPEN_POSITIONS["stock"]:
            return block(f"position limit reached ({config.MAX_OPEN_POSITIONS['stock']} stocks)")

    # --- Rule 1: size (and what we would sell) ---------------------------------
    if action == BUY:
        price = latest_price(data_client, symbol, fallback=0.0)
        if price <= 0:
            return block("could not get a price")
        qty = risk.max_shares_to_buy(equity, price)
        if qty < 1:
            return block(f"{config.MAX_POSITION_PCT:.0%} of equity ({money(equity * config.MAX_POSITION_PCT)})"
                         f" is less than one share ({money(price)})")
        size_note = f"{qty} shares ≈ {money(qty * price)} ({qty * price / equity:.1%} of equity)"
    else:
        position = positions[symbol]
        qty = float(position.qty)
        price = float(position.current_price or 0)
        size_note = f"all {qty:g} shares ≈ {money(qty * price)}"

    if dry_run:
        print(f"  {symbol}: would {side_word} {size_note}" +
              (" (but the kill switch is ON)" if killed else ""))
        return

    # --- Optional AI analyst: approve or veto only -----------------------------
    if config.AI_VETO_MODE == "enforce" and already_logged(todays_log, "AI_DECISION", symbol, date, decision=ai_analyst.VETO):
        print(f"  {symbol}: AI already vetoed this signal today, skipping")
        candidate.update(status="vetoed", ai_decision=ai_analyst.VETO)
        return
    prices = prices_for(symbol) if config.USE_AI_ANALYST else None
    decision, ai_reason = ai_analyst.review(symbol, action, reason, prices)
    candidate.update(ai_decision=decision, ai_reason=ai_reason if decision != ai_analyst.OFF else "")
    if decision != ai_analyst.OFF:
        log_event(event="AI_DECISION", symbol=symbol, signal=action, signal_date=date,
                  ai_decision=decision, ai_reason=ai_reason, equity=equity)
        print(f"  {symbol}: AI analyst says {decision} · {ai_reason}")
    if decision == ai_analyst.VETO and config.AI_VETO_MODE == "enforce":
        candidate["status"] = "vetoed"
        return

    # --- Place the PAPER order ------------------------------------------------
    side = OrderSide.BUY if action == BUY else OrderSide.SELL
    order_id, status, fill = place_order(trading, symbol, side, qty, market_open)
    candidate["status"] = "ordered"
    log_event(event="ORDER", symbol=symbol, signal=action, signal_date=date, side=side_word,
              qty=float(qty), price=fill or price, order_id=order_id, order_status=status,
              ai_decision=decision, ai_reason=ai_reason,
              details=f"strategy {action}: {size_note} · {reason}", equity=equity)
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


def open_crypto_session(args):
    """Log in for the crypto side and work out the shared kill switch."""
    api_key, secret_key = config.get_alpaca_keys()
    trading = TradingClient(api_key, secret_key, paper=True)   # PAPER ONLY
    account = trading.get_account()
    config.require_paper_account(trading, account)
    equity = float(account.equity)
    start_equity = float(account.last_equity)
    killed_earlier = any(row["event"] == "KILL_SWITCH" for row in read_todays_log())
    killed = killed_earlier or risk.kill_switch_triggered(equity, start_equity)
    if killed and not killed_earlier and not args.dry_run:
        change = risk.daily_change(equity, start_equity)
        log_event(event="KILL_SWITCH", equity=equity,
                  details=f"equity {change:+.2%} since yesterday's close (limit "
                          f"-{config.KILL_SWITCH_PCT:.0%}); no new trades today")
    return trading, account, equity, killed


def run_crypto_side(args):
    """
    The crypto run. It comes after the stocks with its own error handling, so a
    crypto problem can't touch the stock run. Crypto never closes, so it doesn't
    check the market clock.
    """
    if not config.CRYPTO_ENABLED or args.test_order:
        return 0
    try:
        trading, account, equity, killed = open_crypto_session(args)
        if account.trading_blocked or account.account_blocked:
            print("❌ Crypto skipped: Alpaca says this account is blocked from trading.")
            return 1
        if config.STRATEGY_MODE == "sleeves":
            sleeves.run("crypto", trading, equity, float(account.non_marginable_buying_power), killed,
                        args.dry_run, crypto.utc_midnight().date().isoformat(), read_crypto_days_log(),
                        log_event, get_crypto_daily_bars, get_crypto_latest_price,
                        lambda plan: dashboard.save_candidates("crypto", plan))
            return 0
        crypto.run(trading, equity, float(account.non_marginable_buying_power), killed,
                   args.dry_run, read_crypto_days_log(), log_event,
                   dashboard.load_candidates().get("crypto", {}),
                   lambda plan: dashboard.save_candidates("crypto", plan))
        return 0
    except (Exception, SystemExit) as error:
        print("❌ The crypto run stopped because of an error:", error)
        record_crash("crypto: " + " ".join(str(error).split())[:290])
        return 1


def run_crypto_test_order(args):
    trading, _, equity, killed = open_crypto_session(args)
    return crypto.run_test_order(trading, args.test_order, equity, killed, log_event)


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
    """
    True from config.DAILY_SUMMARY_UTC onwards. With crypto on that's every day,
    because AI vetoes are only reported here and crypto never stops.
    """
    trading_day = config.CRYPTO_ENABLED or now.weekday() < 5
    return trading_day and now.astimezone(timezone.utc).strftime("%H:%M") >= config.DAILY_SUMMARY_UTC


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
    orders = sum(row["event"] == "ORDER" for row in RUN_EVENTS)
    dashboard.update(LOG_FILE, start_of_today_utc(), exit_code,
                     errors[-1] if errors else "", args.dry_run, orders)
    send_daily_summary(args)


def main():
    parser = argparse.ArgumentParser(description="Run the paper-trading bot once.")
    parser.add_argument("--dry-run", action="store_true", help="show decisions without placing orders")
    parser.add_argument("--test-order", nargs="?", const="SPY", default=None, metavar="SYMBOL",
                        help="buy 1 share (default SPY), or about $10 of a crypto pair such as "
                             "BTC/USD, to check orders reach your dashboard")
    args = parser.parse_args()
    os.chdir(BASE_DIR)
    exit_code = 1
    try:
        if args.test_order and "/" in args.test_order:
            exit_code = run_crypto_test_order(args)
        else:
            exit_code = run(args)
    except SystemExit:   # config.py stops the run this way when the keys are missing
        record_crash("Alpaca keys are missing or unreadable")
    except Exception as error:
        print("❌ The run stopped because of an error:", error)
        record_crash(" ".join(str(error).split())[:300])
    exit_code = max(exit_code, run_crypto_side(args))
    finish_run(args, exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
