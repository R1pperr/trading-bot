"""
grading.py  -  grades each finished trade in plain English, and tags the conditions at entry.

For every closed trade it writes what went right and wrong: the entry and exit against the plan,
why it closed, slippage and costs against what the backtest expects, whether the AI's call was
right, and any rule that was broken. It also works out what a vetoed or skipped trade would have
done ("would have"), using the strategy's own exit rule.

Each trade is tagged with the conditions when it was bought:
  trend   SPY above or below its 200-day average (as of the day before)
  vol     the symbol's 20-day volatility: high (top 20% of its year), low (bottom 20%) or normal
  time    time of day of the entry
  gap     the overnight gap on the entry day: big (over 2%) or normal
  weekday, symbol
  spread  wide or normal (recorded at the order)
  volume  thin or normal (main bot: under $20M a day. Engine: the signal bar under half its average)

The tags are labels, never prices, because the review pages are public. The text only quotes our
own fill prices. Nothing here talks to Alpaca or places anything: the review hands it the prices.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import config
import fixes
import strategies

NEW_YORK = ZoneInfo("America/New_York")
COST_PER_SIDE = 0.0005          # what the backtest assumes, so live costs can be compared with it
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
RULE_NAMES = {"rsi2_stocks": "RSI(2) dip", "ibs_etfs": "IBS dip", "turn_of_month": "turn of the month",
              "rotation": "rotation pick", "momentum_stocks": "momentum pick", "realtime": "EMA(9/21) cross"}


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------
def ny_time(stamp):
    """An Alpaca time string (UTC) as New York time, or None."""
    if not stamp:
        return None
    value = pd.Timestamp(stamp)
    value = value.tz_localize("UTC") if value.tzinfo is None else value
    return value.tz_convert(NEW_YORK)


def time_bucket(when, realtime=False):
    if when is None:
        return "unknown"
    minute = when.hour * 60 + when.minute
    if minute < 9 * 60 + 30:
        return "before the open"
    if realtime and minute < 9 * 60 + 45:
        return "first 15 minutes"
    if minute < 10 * 60:
        return "first 30 minutes" if not realtime else "15 to 30 minutes in"
    if minute >= 15 * 60:
        return "last hour"
    return "middle of the day"


def bucket_vol(rank):
    if rank is None:
        return "unknown"
    return "high" if rank >= 1 - fixes.VOL_EXTREME else "low" if rank < fixes.VOL_EXTREME else "normal"


def daily_tags(symbol_table, spy_table, entry_day):
    """Trend, volatility, gap and volume from daily bars, as known before the entry day's open."""
    day = pd.Timestamp(entry_day)
    tags = {"trend": "unknown", "vol": "unknown", "gap": "unknown", "volume": "unknown"}
    if spy_table is not None and len(spy_table):
        before = spy_table[spy_table.index < day]["close"]
        if len(before) >= fixes.TREND_DAYS:
            tags["trend"] = ("SPY above its 200-day average" if before.iloc[-1] > before.tail(fixes.TREND_DAYS).mean()
                             else "SPY below its 200-day average")
    if symbol_table is not None and len(symbol_table):
        before = symbol_table[symbol_table.index < day]
        tags["vol"] = bucket_vol(fixes.vol_rank(before["close"].to_numpy()))
        on_day = symbol_table[symbol_table.index.normalize() == day.normalize()]
        if len(before) and len(on_day):
            gap = float(on_day["open"].iloc[0]) / float(before["close"].iloc[-1]) - 1
            tags["gap"] = "big (over 2%)" if abs(gap) > fixes.GAP_LIMIT else "normal"
        volume = fixes.dollar_volume(before)
        if volume is not None:
            tags["volume"] = "thin" if volume < fixes.MIN_DOLLAR_VOLUME else "normal"
    return tags


def excursion(table, entry_day, exit_day, entry_price):
    """The best and worst the trade got to, as returns, from daily highs and lows while it was held."""
    if table is None or not len(table) or not entry_price:
        return None, None
    held = table[(table.index.normalize() >= pd.Timestamp(entry_day).normalize())
                 & (table.index.normalize() <= pd.Timestamp(exit_day).normalize())]
    if not len(held):
        return None, None
    return float(held["high"].max() / entry_price - 1), float(held["low"].min() / entry_price - 1)


# ---------------------------------------------------------------------------
# Grades
# ---------------------------------------------------------------------------
def money(value):
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def exit_reason(trade, sleeve):
    if trade.get("stop"):
        return "the stop-loss"
    return {"rsi2_stocks": "the RSI(2) exit rule", "ibs_etfs": "the IBS exit rule",
            "turn_of_month": "the 4th-trading-day sale", "rotation": "the monthly rotation",
            "momentum_stocks": "the monthly momentum list", "realtime": "the EMA cross back down"}.get(sleeve, "its exit rule")


def ai_verdict(decision, won):
    if decision == "APPROVE":
        return "AI approved: right call" if won else "AI approved: wrong call, the trade lost"
    if decision == "VETO":
        return "AI vetoed (shadow mode, so it traded anyway): right call" if not won else \
            "AI vetoed (shadow mode, so it traded anyway): wrong call, the trade won"
    return "AI not asked"


def grade_main(trade, entry=None, planned_price=None, symbol_table=None, spy_table=None):
    """One finished main-bot trade, graded. entry is the record the bot kept when it bought."""
    entry = entry or {}
    sleeve = trade.get("sleeve") or entry.get("sleeve") or ""
    won = trade["pnl"] > 0
    when = ny_time(trade.get("entry_time"))
    tags = daily_tags(symbol_table, spy_table, trade["entry_day"])
    tags.update(time="before the open (filled at the open)" if entry.get("pre_open") else time_bucket(when),
                weekday=WEEKDAYS[date.fromisoformat(trade["entry_day"]).weekday()], symbol=trade["symbol"],
                spread=entry.get("spread") or "unknown")
    best, worst = excursion(symbol_table, trade["entry_day"], trade["exit_day"], trade["entry"])
    cost = trade["qty"] * (trade["entry"] + trade["exit"]) * COST_PER_SIDE
    lines = [f"Bought {trade['symbol']} ({RULE_NAMES.get(sleeve, sleeve or 'no sleeve')}) on {trade['entry_day']} "
             f"at {money(trade['entry'])}"]
    slippage = None
    if planned_price:
        slippage = trade["entry"] / planned_price - 1
        lines[0] += f", against {money(planned_price)} when the order went in (slippage {slippage:+.2%})"
    lines.append(f"Sold on {trade['exit_day']} at {money(trade['exit'])} by {exit_reason(trade, sleeve)} after "
                 f"{trade['days']} day{'s' if trade['days'] != 1 else ''}: {trade['ret']:+.2%} ({money(trade['pnl'])})")
    lines.append(f"Costs: Alpaca charged nothing on paper; the backtest allows {money(cost)} for this trade")
    if best is not None and best > 0.03 and not won:
        lines.append(f"It was up {best:+.1%} at its best and still closed at a loss")
    lines.append(ai_verdict(entry.get("ai_decision", ""), won))
    bugs = []
    if entry.get("equity") and trade["entry"] * trade["qty"] > entry["equity"] * (config.MAX_POSITION_PCT + 0.001):
        bugs.append(f"position was {trade['entry'] * trade['qty'] / entry['equity']:.1%} of the account, over the 5% cap")
    if trade.get("stop") and trade["ret"] < -(config.STOP_LOSS_PCT + 0.02):
        bugs.append(f"the stop sold at {trade['ret']:+.1%}, well past the 7% level (late stop or a gap)")
    if slippage is not None and abs(slippage) > 0.03:
        bugs.append(f"filled {slippage:+.1%} away from the price when ordered (late order or bad data)")
    if trade["entry"] <= 0 or trade["exit"] <= 0:
        bugs.append("a price of zero or less (bad data)")
    lines.append("Rules: " + ("; ".join(bugs) if bugs else "none broken"))
    return {"strategy": sleeve or "legacy", "symbol": trade["symbol"], "entry_day": trade["entry_day"],
            "exit_day": trade["exit_day"], "ret": trade["ret"], "pnl": trade["pnl"], "win": won,
            "days": trade["days"], "stop": bool(trade.get("stop")), "ai": entry.get("ai_decision", ""),
            "best": best, "worst": worst, "tags": tags, "text": ". ".join(lines) + ".", "bugs": bugs,
            "entry_order": trade.get("entry_order", ""), "kind": "live"}


# ---------------------------------------------------------------------------
# "Would have": a vetoed or skipped main-bot trade, played out with the sleeve's own exit rule
# ---------------------------------------------------------------------------
def would_have_main(sleeve, symbol, signal_day, price, table, calendar=None, max_days=60):
    """Buy at the given price on signal_day, then sell at the next open after the exit rule fires,
    or at the 7% stop. Returns (exit_day, return after costs) or None while it's still open."""
    if table is None or not price:
        return None
    data = strategies.indicators(table)
    after = data[data.index.normalize() > pd.Timestamp(signal_day).normalize()]
    opens = table["open"].reindex(after.index)
    lows = table["low"].reindex(after.index)
    stop = price * (1 - config.STOP_LOSS_PCT)
    for held, (day, row) in enumerate(after.iterrows(), start=1):
        if lows.loc[day] <= stop:
            exit_price = min(float(opens.loc[day]), stop)
            return day.date().isoformat(), exit_price / price - 1 - 2 * COST_PER_SIDE
        fire = False
        if sleeve == "rsi2_stocks":
            fire = bool(strategies.rsi2_exit(row["close"], row["sma_exit"], row["sma_trend"], held))
        elif sleeve == "ibs_etfs":
            fire = bool(strategies.ibs_exit(row["ibs"], held))
        elif sleeve == "turn_of_month":
            fire = held >= config.TURN_OF_MONTH_SELL_DAY
        else:                                   # rotation and momentum re-rank monthly: hold to the month's end
            fire = held >= 21
        if fire or held >= max_days:
            later = after.index[after.index > day]
            if not len(later):
                return None                     # the exit would fill tomorrow
            return later[0].date().isoformat(), float(opens.loc[later[0]]) / price - 1 - 2 * COST_PER_SIDE
    return None


# ---------------------------------------------------------------------------
# The real-time engine
# ---------------------------------------------------------------------------
def pair_engine_fills(orders):
    """The engine's filled orders, oldest first, as finished trades: one buy, then the sell that closed it."""
    trades, open_buy = [], None
    for order in sorted(orders, key=lambda o: str(o.get("filled_at", ""))):
        if order.get("status") != "filled" or not order.get("filled_at"):
            continue
        if order["side"] == "buy":
            open_buy = order
        elif open_buy is not None:
            entry, exit_ = float(open_buy["filled_avg_price"]), float(order["filled_avg_price"])
            qty = float(order["filled_qty"])
            kind = str(order.get("client_order_id", "")).rsplit("-", 1)[-1]
            trades.append({"symbol": order.get("symbol", "SPY"), "entry_time": open_buy["filled_at"],
                           "exit_time": order["filled_at"], "entry": entry, "exit": exit_, "qty": qty,
                           "pnl": round((exit_ - entry) * qty, 2), "ret": round(exit_ / entry - 1, 6),
                           "exit_kind": {"stop": "the 2% stop", "close": "the 15:55 close-out"}.get(kind, "the EMA cross back down"),
                           "stop": kind == "stop", "entry_order": str(open_buy.get("id", "")),
                           "client_id": str(open_buy.get("client_order_id", ""))})
            open_buy = None
    return trades


def grade_engine(trade, bars=None, spy_daily=None, decision=None):
    """One finished engine trade. bars: that day's 5-minute bars (New York times), for the tags."""
    decision = decision or {}
    entry_at, exit_at = ny_time(trade["entry_time"]), ny_time(trade["exit_time"])
    minutes = (exit_at - entry_at).total_seconds() / 60 if entry_at and exit_at else None
    won = trade["pnl"] > 0
    day = entry_at.date().isoformat() if entry_at else ""
    tags = daily_tags(spy_daily, spy_daily, day) if day else {}
    tags.pop("volume", None)
    tags.update(time=time_bucket(entry_at, realtime=True), weekday=WEEKDAYS[entry_at.weekday()] if entry_at else "unknown",
                symbol=trade["symbol"], volume="unknown", spread="unknown")
    spread = decision.get("spread_pct")
    if spread is not None:
        tags["spread"] = "wide" if spread > 0.0005 else "normal"
    best = None
    if bars is not None and len(bars) and entry_at is not None:
        held = bars[(bars.index >= entry_at.tz_localize(None)) & (bars.index <= exit_at.tz_localize(None))]
        if len(held):
            best = float(held["high"].max() / trade["entry"] - 1)
        before = bars[bars.index < entry_at.tz_localize(None)]
        if "volume" in bars.columns and len(before) > 21:
            signal_volume = float(before["volume"].iloc[-1])
            average = float(before["volume"].iloc[-21:-1].mean())
            tags["volume"] = "thin" if average > 0 and signal_volume < average / 2 else "normal"
    cost = trade["qty"] * (trade["entry"] + trade["exit"]) * COST_PER_SIDE
    lines = [f"Bought {trade['qty']:g} {trade['symbol']} at {entry_at:%H:%M} at {money(trade['entry'])}" if entry_at else
             f"Bought {trade['qty']:g} {trade['symbol']} at {money(trade['entry'])}",
             f"Sold at {exit_at:%H:%M} by {trade['exit_kind']} at {money(trade['exit'])}"
             + (f" after {minutes:.0f} minutes" if minutes is not None else "")
             + f": {trade['ret']:+.2%} ({money(trade['pnl'])})",
             f"Costs: none charged on paper; the backtest allows {money(cost)}"]
    if best is not None and best > 0.005 and not won:
        lines.append(f"It was up {best:+.2%} at its best and still lost")
    lines.append(ai_verdict(decision.get("decision", ""), won))
    bugs = []
    if exit_at is not None and exit_at.hour * 60 + exit_at.minute > 15 * 60 + 58:
        bugs.append("still open after 15:58, past the 15:55 close-out")
    if trade.get("stop") and trade["ret"] < -0.03:
        bugs.append(f"the stop sold at {trade['ret']:+.1%}, well past the 2% level")
    lines.append("Rules: " + ("; ".join(bugs) if bugs else "none broken"))
    return {"strategy": "realtime", "symbol": trade["symbol"], "entry_day": day,
            "exit_day": exit_at.date().isoformat() if exit_at else day, "ret": trade["ret"], "pnl": trade["pnl"],
            "win": won, "minutes": minutes, "stop": trade.get("stop", False), "ai": decision.get("decision", ""),
            "best": best, "worst": None, "tags": tags, "text": ". ".join(lines) + ".", "bugs": bugs,
            "entry_order": trade["entry_order"], "kind": "live"}


def would_have_engine(day_bars, previous_bars, signal_bar_time, qty=1):
    """A vetoed engine buy, played out: in at the next bar's open, out at the next cross down (next
    bar's open), the 2% stop, or 15:55. Returns the return after costs, or None if not knowable."""
    import rt_backtest
    if day_bars is None or not len(day_bars):
        return None
    times = list(day_bars.index)
    later = [i for i, t in enumerate(times) if t > signal_bar_time]
    if not later:
        return None
    start = later[0]
    closes = day_bars["close"].to_numpy()
    signals = rt_backtest.day_signals(previous_bars["close"].to_numpy() if previous_bars is not None else np.array([]), closes)
    entry = float(day_bars["open"].iloc[start])
    stop = entry * (1 - fixes.STOP["realtime"])
    for i in range(start, len(times)):
        minute = times[i].hour * 60 + times[i].minute
        if minute >= rt_backtest.FLAT_FROM:
            return float(day_bars["open"].iloc[i]) / entry - 1 - 2 * COST_PER_SIDE
        if float(day_bars["low"].iloc[i]) <= stop:
            return min(float(day_bars["open"].iloc[i]), stop) / entry - 1 - 2 * COST_PER_SIDE
        if signals[i] < 0 and i + 1 < len(times):
            return float(day_bars["open"].iloc[i + 1]) / entry - 1 - 2 * COST_PER_SIDE
    return float(closes[-1]) / entry - 1 - 2 * COST_PER_SIDE
