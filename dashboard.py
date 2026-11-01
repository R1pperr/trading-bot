"""
dashboard.py  -  writes the JSON files the dashboard website reads.

bot.py calls update() at the end of every run. The files land in docs/data/ and
the workflow commits them, so GitHub Pages can show them.

The repository is public, so everything written here is public. Order ids are
left out and any key text is blanked before a file is written. The website never
talks to Alpaca or Gemini; it only reads these files.

Nothing here can stop the bot: update() catches every error and prints a note.
"""

import csv
import json
import os
from datetime import datetime, timedelta, timezone

from alpaca.trading.client import TradingClient

import ai_analyst
import config
import notifier
import risk
from data import NEW_YORK, get_daily_bars
from strategy import latest_signal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "docs", "data")
MAX_EQUITY_POINTS = 5000    # about 8 months of 15-minute runs
MAX_TRADES = 100            # newest rows kept in trades.json
MAX_RUNS = 120              # newest runs kept in runs.json, about 4 trading days


# ---------------------------------------------------------------------------
# Reading and writing files
# ---------------------------------------------------------------------------
def _clean(value):
    """Blank out key text in every string, however deeply nested."""
    if isinstance(value, str):
        return notifier.redact(value)
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    return value


def _read_json(name, default):
    try:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _write_json(name, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, name), "w", encoding="utf-8") as handle:
        json.dump(_clean(data), handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def read_log(log_file):
    if not os.path.exists(log_file):
        return []
    with open(log_file, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# ---------------------------------------------------------------------------
# Building each file's contents
# ---------------------------------------------------------------------------
def _number(value):
    """Turn a log or Alpaca value into a float, or None if there isn't one."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def trade_rows(log_rows):
    """Orders and AI vetoes, newest first."""
    wanted = [row for row in log_rows if row.get("event") == "ORDER"
              or (row.get("event") == "AI_DECISION" and row.get("ai_decision") == ai_analyst.VETO)]
    return [{
        "time_utc": row.get("time_utc", ""),
        "event": row.get("event", ""),
        "symbol": row.get("symbol", ""),
        "side": row.get("side", "") or row.get("signal", "").lower(),
        "qty": _number(row.get("qty")),
        "price": _number(row.get("price")),
        "order_status": row.get("order_status", ""),
        "reason": row.get("details", ""),
        "ai_decision": row.get("ai_decision", ""),
        "ai_reason": row.get("ai_reason", ""),
    } for row in reversed(wanted[-MAX_TRADES:])]


def position_rows(positions):
    """Open positions with unrealized profit or loss."""
    rows = []
    for position in positions:
        entry = float(position.avg_entry_price)
        rows.append({
            "symbol": position.symbol,
            "qty": float(position.qty),
            "avg_entry_price": entry,
            "current_price": _number(position.current_price),
            "market_value": _number(position.market_value),
            "unrealized_pl": _number(position.unrealized_pl),
            "unrealized_pl_pct": _number(position.unrealized_plpc),
            "change_today_pct": _number(getattr(position, "change_today", None)),
            "stop_price": round(risk.stop_price(entry), 2),
        })
    return sorted(rows, key=lambda row: row["symbol"])


def add_equity_point(history, now, equity):
    """Add a point when equity changed or a new day began, keeping the newest points."""
    if history:
        last = history[-1]
        if last["equity"] == equity and last["time_utc"][:10] == now[:10]:
            return history
    return (history + [{"time_utc": now, "equity": equity}])[-MAX_EQUITY_POINTS:]


def add_run(history, now, state, orders):
    """Remember this run, so the dashboard can show that the bot keeps running."""
    return (history + [{"time_utc": now, "status": state, "orders": orders}])[-MAX_RUNS:]


def account_figures(account, positions):
    """Extra numbers from your own account. Empty if Alpaca couldn't be reached."""
    if account is None:
        return {}
    return {
        "cash": _number(getattr(account, "cash", None)),
        "buying_power": _number(getattr(account, "buying_power", None)),
        "invested": round(sum(_number(p.market_value) or 0.0 for p in positions), 2),
        "open_pl": round(sum(_number(p.unrealized_pl) or 0.0 for p in positions), 2),
    }


def refresh_watchlist(today):
    """
    Once a New York day: the bot's current view of each stock, in words only.
    Alpaca doesn't allow republishing its price data, so no prices or averages
    go into this public file. The website shows price charts from TradingView.
    """
    if _read_json("watchlist.json", {}).get("updated_for") == today:
        return
    start = datetime.now(timezone.utc) - timedelta(days=int(config.SLOW_SMA_DAYS * 1.6) + 60)
    bars = get_daily_bars(config.SYMBOLS, start)
    stocks = []
    for symbol in config.SYMBOLS:
        prices = bars.get(symbol)
        view = {"trend": "", "signal": "", "date": ""}
        if prices is not None and len(prices) >= config.SLOW_SMA_DAYS + 1:
            view = latest_signal(prices)
        stocks.append({"symbol": symbol, "trend": view["trend"], "signal": view["signal"],
                       "signal_date": view["date"]})
    _write_json("watchlist.json", {"updated_for": today, "stocks": stocks})


def build_status(now, exit_code, error_text, dry_run, market_open, equity, start_equity,
                 todays_rows):
    if exit_code or error_text:
        state, message = "error", error_text or "The run stopped with an error."
    elif dry_run:
        state, message = "dry_run", "Dry run: no orders placed."
    elif market_open is False:
        state, message = "market_closed", "The US market is closed, so there was nothing to do."
    else:
        state, message = "ok", "The run finished normally."

    has_equity = equity is not None and start_equity is not None
    kill_row = next((row for row in todays_rows if row.get("event") == "KILL_SWITCH"), None)
    kill_on = kill_row is not None or (has_equity and risk.kill_switch_triggered(equity, start_equity))
    return {
        "last_run": {"time_utc": now, "status": state, "message": message},
        "market_open": market_open,
        "equity": equity,
        "day_change_pct": risk.daily_change(equity, start_equity) if has_equity else None,
        "kill_switch": {
            "on": kill_on,
            "reason": kill_row.get("details", "") if kill_row else "",
        },
        "ai_analyst_on": config.USE_AI_ANALYST,
        "paper": True,
        "trades_today": sum(row.get("event") == "ORDER" for row in todays_rows),
        "limits": {
            "max_trades_per_day": config.MAX_TRADES_PER_DAY,
            "max_position_pct": config.MAX_POSITION_PCT,
            "stop_loss_pct": config.STOP_LOSS_PCT,
            "kill_switch_pct": config.KILL_SWITCH_PCT,
        },
    }


def summary_text(orders_today):
    """The day's one-message summary, built from the files just written."""
    status = _read_json("status.json", {})
    equity = status.get("equity")
    change = status.get("day_change_pct")
    value = "unknown" if equity is None else f"${equity:,.2f}"
    if change is not None:
        value += f" ({change:+.2%} today)"
    run_status = status.get("last_run", {}).get("status", "unknown")
    kill_on = status.get("kill_switch", {}).get("on")
    return (f"📊 Daily summary\nAccount value: {value}\nOrders today: {orders_today}\n"
            f"Open positions: {len(_read_json('positions.json', []))}\n"
            f"Kill switch: {'ON' if kill_on else 'off'}\n"
            f"Last run: {run_status.replace('_', ' ')}")


# ---------------------------------------------------------------------------
# Called by bot.py at the end of every run
# ---------------------------------------------------------------------------
def fetch_account():
    """Fresh account numbers from Alpaca's PAPER server. Reads only, places nothing."""
    api_key, secret_key = config.get_alpaca_keys()
    trading = TradingClient(api_key, secret_key, paper=True)   # PAPER ONLY
    return trading.get_account(), trading.get_all_positions(), trading.get_clock().is_open


def _update(log_file, day_start, exit_code, error_text, dry_run, orders):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_rows = read_log(log_file)
    day_start_text = day_start.strftime("%Y-%m-%d %H:%M:%S")
    todays_rows = [row for row in log_rows if row.get("time_utc", "") >= day_start_text]
    previous = _read_json("status.json", {})

    try:
        account, positions, market_open = fetch_account()
        equity, start_equity = float(account.equity), float(account.last_equity)
    except (Exception, SystemExit) as error:
        # Alpaca can't be reached: keep the last good numbers and still show this run's status.
        print("⚠️  Could not refresh account data for the dashboard:", notifier.redact(error))
        account, positions, market_open, start_equity = None, None, None, None
        equity = previous.get("equity")

    status = build_status(now, exit_code, error_text, dry_run, market_open,
                          equity, start_equity, todays_rows)
    figures = account_figures(account, positions)
    for name in ("cash", "buying_power", "invested", "open_pl"):
        status[name] = figures.get(name, previous.get(name))
    _write_json("status.json", status)
    _write_json("trades.json", trade_rows(log_rows))
    _write_json("runs.json", add_run(_read_json("runs.json", []), now,
                                     status["last_run"]["status"], orders))
    if positions is not None:
        _write_json("positions.json", position_rows(positions))
        _write_json("equity_history.json",
                    add_equity_point(_read_json("equity_history.json", []), now, equity))

    try:
        refresh_watchlist(datetime.now(NEW_YORK).date().isoformat())
    except (Exception, SystemExit) as error:
        print("⚠️  Could not refresh the stock list for the dashboard:", notifier.redact(error))
    print("Dashboard data written to docs/data/")


def update(log_file, day_start, exit_code=0, error_text="", dry_run=False, orders=0):
    """Refresh every file in docs/data/. Never raises an error."""
    try:
        _update(log_file, day_start, exit_code, error_text, dry_run, orders)
    except (Exception, SystemExit) as error:
        print("⚠️  Dashboard data not updated:", notifier.redact(error))
