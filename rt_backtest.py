"""
rt_backtest.py  -  the A/B test for the real-time engine: 5-minute bars against 1-minute bars.

    python rt_backtest.py

It follows the rules in realtime/src/engine.ts: EMA(9) and EMA(21) on completed SPY bars,
warmed up with the previous session. A signal on a bar's close fills at the next bar's open.
Long only, 4x equity on margin in whole shares (within 95% of Alpaca's 4x day-trade buying
power), a stop 2% below the entry, at most 20 entries a day,
no new entries after 15:45 or once the account is down 2% on the day, and flat by 15:55.

It uses the same free feed the engine sees live (IEX, one exchange). The oldest 70% is
in-sample, the newest 30% is out-of-sample, and the winner comes from strategies.ab_winner.
Reads price history only. It never places an order.
"""

import json
import os
import pickle
from datetime import datetime

import numpy as np
import pandas as pd
from alpaca.data.enums import DataFeed

import quant_lab
import sleeve_backtest as backtest
import strategies
from data import get_intraday_bars

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, ".cache", "quant_lab")
OUTPUT = os.path.join(BASE_DIR, "docs", "data", "backtest.json")
FIRST_DAY, LAST_DAY = "2024-11-01", "2026-10-31"     # the last 2 years
START_CASH = 100_000.0
COST_PER_SIDE = backtest.STOCK_FEE
EMA_FAST, EMA_SLOW = 9, 21
POSITION_PCT = 4.0            # 4x equity per trade on margin (the user chose 4x leverage on 5 Nov 2026)
DAY_TRADE_MARGIN = 4          # Alpaca's buying power for day trades: 4x equity
BUYING_POWER_USE = 0.95       # the engine uses at most 95% of that buying power
STOP_PCT = 0.02
MAX_ENTRIES = 20
DAILY_LOSS_PCT = 0.02
NO_ENTRIES_FROM = 15 * 60 + 45
FLAT_FROM = 15 * 60 + 55
WHIPSAW_BARS = 3
SESSION_OPEN = 9 * 60 + 30

# The fix menu, with the same standard settings as realtime/src/engine.ts. They are never fine-tuned,
# and each one can only make a locked rule tighter.
TIGHT_STOP_PCT = 0.01
TIME_EXIT_NO_ENTRIES_FROM = 15 * 60 + 20
TIME_EXIT_FLAT_FROM = 15 * 60 + 30
GAP_LIMIT = 0.02
VOL_BAND = 0.2
VOL_DAYS = 20
VOL_YEAR = 252
TREND_DAYS = 200
LIQUIDITY_BARS = 20
LIQUIDITY_SHARE = 0.5
NO_FIXES = {"trend_filter": "off", "vol_filter": False, "time_filter": 0, "gap_filter": False, "liquidity_filter": False,
            "confirmation": False, "time_exit": False, "trailing_stop": False, "tight_stop": False, "vol_sizing": False}


def clean_fixes(raw):
    """Only the menu's own values get through. Anything else counts as off, like cleanFixes() in the Worker."""
    given = raw if isinstance(raw, dict) else {}
    flag = lambda key: given.get(key) is True
    return {"trend_filter": given.get("trend_filter") if given.get("trend_filter") in ("above", "below") else "off",
            "vol_filter": flag("vol_filter"),
            "time_filter": given.get("time_filter") if given.get("time_filter") in (15, 30) and given.get("time_filter") is not True else 0,
            "gap_filter": flag("gap_filter"), "liquidity_filter": flag("liquidity_filter"), "confirmation": flag("confirmation"),
            "time_exit": flag("time_exit"), "trailing_stop": flag("trailing_stop"), "tight_stop": flag("tight_stop"),
            "vol_sizing": flag("vol_sizing")}


def needs_daily(fixes):
    return fixes["trend_filter"] != "off" or fixes["vol_filter"] or fixes["gap_filter"] or fixes["vol_sizing"]


def limits_for(fixes, context=None):
    """The locked rules with any fixes applied. Each is clamped, so a fix can only tighten it."""
    scale = 1.0
    if fixes["vol_sizing"] and context and context.get("vol_scale"):
        scale = min(1.0, max(0.0, context["vol_scale"]))
    return {"position_pct": min(POSITION_PCT, POSITION_PCT * scale),
            "stop_pct": min(STOP_PCT, TIGHT_STOP_PCT if fixes["tight_stop"] else STOP_PCT),
            "max_entries": MAX_ENTRIES, "daily_loss_pct": DAILY_LOSS_PCT,
            "no_entries_from": min(NO_ENTRIES_FROM, TIME_EXIT_NO_ENTRIES_FROM if fixes["time_exit"] else NO_ENTRIES_FROM),
            "flat_from": min(FLAT_FROM, TIME_EXIT_FLAT_FROM if fixes["time_exit"] else FLAT_FROM)}


def shares_for(cash, price, pct=POSITION_PCT):
    """Like the engine's sharesFor: the smaller of pct x equity and 95% of Alpaca's 4x buying power."""
    budget = min(cash * min(POSITION_PCT, pct), cash * DAY_TRADE_MARGIN * BUYING_POWER_USE)
    return int(budget // price) if price > 0 and budget > 0 else 0


def daily_contexts(closes):
    """For each day: what the daily bars that ended the day before say. Same sums as dailyContext() in the Worker:
    SPY above its 200-day average, where today's 20-day volatility ranks among the last 252, the median of
    those over today's (for sizing), and yesterday's close (for the gap)."""
    closes = pd.Series(closes, dtype=float).sort_index()
    values = closes.to_numpy()
    sma = closes.rolling(TREND_DAYS).mean().to_numpy()
    vol = closes.pct_change().rolling(VOL_DAYS).std().to_numpy()
    result = {}
    for k in range(1, len(values)):
        j = k - 1
        context = {"prev_close": float(values[j]), "above_sma200": None, "vol_rank": None, "vol_scale": None}
        if j + 1 >= TREND_DAYS and np.isfinite(sma[j]):
            context["above_sma200"] = bool(values[j] > sma[j])
        window = vol[j - VOL_YEAR + 1: j + 1] if j + 1 >= VOL_YEAR else np.array([])
        if len(window) == VOL_YEAR and np.all(np.isfinite(window)) and window[-1] > 0:
            current = window[-1]
            context["vol_rank"] = float((window < current).mean())
            context["vol_scale"] = float(np.median(window) / current)
        result[pd.Timestamp(closes.index[k]).normalize()] = context
    return result


def entry_block(fixes, context, minute, today_open, volumes, signal_j):
    """Why a fix blocks this entry, or None. volumes are yesterday's and today's bars joined, signal_j indexes them.
    The live liquidity rule also checks the bid-ask spread. There are no old quotes, so only its volume half is tested here."""
    if fixes["time_filter"] and minute < SESSION_OPEN + fixes["time_filter"]:
        return "time"
    if needs_daily(fixes) and not context:
        return "no daily data"
    if fixes["trend_filter"] != "off":
        above = context.get("above_sma200")
        if above is None or (fixes["trend_filter"] == "above") != above:
            return "trend"
    if fixes["vol_filter"]:
        rank = context.get("vol_rank")
        if rank is None or rank >= 1 - VOL_BAND or rank < VOL_BAND:
            return "volatility"
    if fixes["gap_filter"]:
        previous = context.get("prev_close")
        if not previous or abs(today_open / previous - 1) > GAP_LIMIT:
            return "gap"
    if fixes["vol_sizing"] and not context.get("vol_scale"):
        return "no daily data"
    if fixes["liquidity_filter"]:
        if volumes is None or signal_j < LIQUIDITY_BARS:
            return "liquidity"
        before = volumes[signal_j - LIQUIDITY_BARS: signal_j]
        if not np.all(np.isfinite(before)) or volumes[signal_j] < LIQUIDITY_SHARE * before.mean():
            return "liquidity"
    return None


def ema(values, period):
    """The same sum as the Worker's ema(), so the two can't drift apart."""
    weight = 2 / (period + 1)
    result = np.empty(len(values))
    result[0] = values[0]
    for i in range(1, len(values)):
        result[i] = values[i] * weight + result[i - 1] * (1 - weight)
    return result


def day_signals(previous_closes, closes):
    """+1 for a cross up, -1 for a cross down, one number per bar of today. Yesterday only warms up."""
    joined = np.concatenate((previous_closes, closes))
    gap = ema(joined, EMA_FAST) - ema(joined, EMA_SLOW)
    signals = np.zeros(len(closes), dtype=int)
    for i in range(len(closes)):
        j = len(previous_closes) + i
        if j == 0:
            continue
        if gap[j - 1] <= 0 < gap[j]:
            signals[i] = 1
        elif gap[j - 1] >= 0 > gap[j]:
            signals[i] = -1
    return signals


def whipsaws(signals):
    """Signals reversed within 3 bars."""
    places = np.flatnonzero(signals)
    return int(sum(places[i + 1] - places[i] <= WHIPSAW_BARS for i in range(len(places) - 1)))


def simulate(days, fixes=None, daily=None):
    """days: a list of (date, table) with open, high, low, close and a minute column, oldest first.
    fixes: a fix-menu dict (see NO_FIXES), or None for the plain engine. daily: daily_contexts() output."""
    if fixes is None:
        return _simulate_plain(days)
    return _simulate_fixed(days, clean_fixes(fixes), daily or {})


def _confirmed(signals, gap, offset):
    """With the confirmation fix, a crossover counts one bar later, and only if the averages stay on the same side."""
    result = np.zeros(len(signals), dtype=int)
    for i in range(1, len(signals)):
        side = signals[i - 1]
        if side and np.sign(gap[offset + i]) == side:
            result[i] = side
    return result


def _simulate_fixed(days, fixes, daily):
    """The engine with fixes on. With every fix off it gives exactly what _simulate_plain gives (tested)."""
    cash, position = START_CASH, None
    trades, curve = [], []
    signal_count = whipsaw_count = 0

    def sell(price, stamp, why):
        nonlocal cash, position
        fee = position["qty"] * price * COST_PER_SIDE
        cash += position["qty"] * price - fee
        pnl = position["qty"] * price - fee - position["cost"]
        trades.append({"pnl": pnl, "ret": pnl / position["cost"], "win": pnl > 0, "fee": fee + position["fee"],
                       "days": (stamp - position["since"]) / 390, "why": why})
        position = None

    for index in range(1, len(days)):
        day, bars = days[index]
        previous_table = days[index - 1][1]
        previous = previous_table["close"].to_numpy()
        opens, lows, closes, minutes = (bars[name].to_numpy() for name in ("open", "low", "close", "minute"))
        has_volume = "volume" in bars and "volume" in previous_table
        volumes = np.concatenate((previous_table["volume"].to_numpy(dtype=float), bars["volume"].to_numpy(dtype=float))) if has_volume else None
        joined = np.concatenate((previous, closes))
        gap = ema(joined, EMA_FAST) - ema(joined, EMA_SLOW)
        signals = day_signals(previous, closes)
        signal_count += int(np.count_nonzero(signals))
        whipsaw_count += whipsaws(signals)
        acted = _confirmed(signals, gap, len(previous)) if fixes["confirmation"] else signals
        # Where each acted-on signal's crossover bar sits in the joined arrays, for the liquidity check.
        origin = (np.arange(len(bars)) - 1 if fixes["confirmation"] else np.arange(len(bars))) + len(previous)
        context = daily.get(pd.Timestamp(day).normalize())
        limits = limits_for(fixes, context)
        start_equity = cash
        entries, pending, pending_from = 0, 0, -1
        for i in range(len(bars)):
            minute = int(minutes[i])
            if position and minute >= limits["flat_from"]:
                sell(opens[i], minute, "close")
            elif position and pending < 0:
                sell(opens[i], minute, "signal")
            elif not position and pending > 0 and minute < limits["no_entries_from"] and entries < limits["max_entries"] \
                    and cash / start_equity - 1 > -limits["daily_loss_pct"]:
                if entry_block(fixes, context, minute, opens[0], volumes, pending_from) is None:
                    qty = shares_for(cash, opens[i], limits["position_pct"])
                    if qty >= 1:
                        fee = qty * opens[i] * COST_PER_SIDE
                        cash -= qty * opens[i] + fee
                        position = {"qty": qty, "entry": opens[i], "stop": round(opens[i] * (1 - limits["stop_pct"]), 2),
                                    "cost": qty * opens[i] + fee, "fee": fee, "since": minute}
                        entries += 1
            pending = 0
            if position and lows[i] <= position["stop"]:
                sell(min(opens[i], position["stop"]), minute, "stop")
            if position and fixes["trailing_stop"]:
                # The live engine moves the stop after a bar finishes, so the new level counts from the next bar.
                position["stop"] = max(position["stop"], round(closes[i] * (1 - limits["stop_pct"]), 2))
            if acted[i]:
                pending, pending_from = int(acted[i]), int(origin[i])
        if position:
            sell(closes[-1], int(minutes[-1]), "close")
        curve.append((day, cash))
    equity = pd.Series([value for _, value in curve], index=pd.DatetimeIndex([day for day, _ in curve]), dtype=float)
    table = pd.DataFrame(trades, columns=["pnl", "ret", "win", "fee", "days", "why"])
    return {"equity": equity, "trades": table, "signals": signal_count, "whipsaws": whipsaw_count}


def _simulate_plain(days):
    """The engine as it was A/B tested, with no fixes. Kept unchanged so old numbers stay the same."""
    cash, position = START_CASH, None
    trades, curve = [], []
    signal_count = whipsaw_count = 0

    def sell(price, stamp, why):
        nonlocal cash, position
        fee = position["qty"] * price * COST_PER_SIDE
        cash += position["qty"] * price - fee
        pnl = position["qty"] * price - fee - position["cost"]
        trades.append({"pnl": pnl, "ret": pnl / position["cost"], "win": pnl > 0, "fee": fee + position["fee"],
                       "days": (stamp - position["since"]) / 390, "why": why})
        position = None

    for index in range(1, len(days)):
        day, bars = days[index]
        previous = days[index - 1][1]["close"].to_numpy()
        opens, lows, closes, minutes = (bars[name].to_numpy() for name in ("open", "low", "close", "minute"))
        signals = day_signals(previous, closes)
        signal_count += int(np.count_nonzero(signals))
        whipsaw_count += whipsaws(signals)
        start_equity = cash
        entries, pending = 0, 0
        for i in range(len(bars)):
            minute = int(minutes[i])
            if position and minute >= FLAT_FROM:
                sell(opens[i], minute, "close")
            elif position and pending < 0:
                sell(opens[i], minute, "signal")
            elif not position and pending > 0 and minute < NO_ENTRIES_FROM and entries < MAX_ENTRIES \
                    and cash / start_equity - 1 > -DAILY_LOSS_PCT:
                qty = shares_for(cash, opens[i])
                if qty >= 1:
                    fee = qty * opens[i] * COST_PER_SIDE
                    cash -= qty * opens[i] + fee
                    position = {"qty": qty, "entry": opens[i], "stop": round(opens[i] * (1 - STOP_PCT), 2),
                                "cost": qty * opens[i] + fee, "fee": fee, "since": minute}
                    entries += 1
            pending = 0
            if position and lows[i] <= position["stop"]:
                sell(min(opens[i], position["stop"]), minute, "stop")
            if signals[i]:
                pending = int(signals[i])
        if position:
            sell(closes[-1], int(minutes[-1]), "close")
        curve.append((day, cash))
    equity = pd.Series([value for _, value in curve], index=pd.DatetimeIndex([day for day, _ in curve]), dtype=float)
    table = pd.DataFrame(trades, columns=["pnl", "ret", "win", "fee", "days", "why"])
    return {"equity": equity, "trades": table, "signals": signal_count, "whipsaws": whipsaw_count}


def days_from(table):
    """Group bars into sessions."""
    table = table.copy()
    table["minute"] = table.index.hour * 60 + table.index.minute
    return [(pd.Timestamp(day), group) for day, group in table.groupby(table.index.normalize())]


def load(minutes):
    path = os.path.join(CACHE_DIR, f"spy_iex_{minutes}min.pkl")
    if os.path.exists(path):
        with open(path, "rb") as handle:
            return pickle.load(handle)
    print(f"Downloading {minutes}-minute SPY bars ...", flush=True)
    table = get_intraday_bars("SPY", datetime.fromisoformat(FIRST_DAY), datetime.fromisoformat(LAST_DAY), minutes, DataFeed.IEX)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "wb") as handle:
        pickle.dump(table, handle)
    return table


def measure(days, cut):
    """Each half is simulated on its own days, so no trade spans the split."""
    early = [item for item in days if item[0] <= cut]
    late = [item for item in days if item[0] > cut]
    parts = []
    for part in (early, late):
        result = simulate(part)
        row = quant_lab.metrics(result["equity"], result["trades"])
        row.update(signals=result["signals"], whipsaws=result["whipsaws"], costs=float(result["trades"]["fee"].sum()))
        parts.append(row)
    return parts


def main():
    os.chdir(BASE_DIR)
    tables = {minutes: load(minutes) for minutes in (5, 1)}
    versions = {minutes: days_from(table) for minutes, table in tables.items()}
    all_days = [day for day, _ in versions[5]]
    cut = all_days[int(len(all_days) * 0.7) - 1]
    rows = {}
    for minutes, days in versions.items():
        rows[minutes] = measure(days, cut)
        print(f"{minutes}-minute: in {rows[minutes][0]['trades']} trades, out {rows[minutes][1]['trades']} trades", flush=True)

    closes = pd.Series({day: table["close"].iloc[-1] for day, table in versions[5]})
    early, late = closes[closes.index <= cut], closes[closes.index > cut]
    empty = pd.DataFrame(columns=["pnl", "ret", "win", "days", "fee"])
    spy = {"in_sample": quant_lab.metrics(early, empty), "out_of_sample": quant_lab.metrics(late, empty)}

    winner = strategies.ab_winner(rows[5][1], rows[1][1])
    best = 1 if winner == "challenger" else 5
    expectancy = rows[best][1]["expectancy"]
    failed = expectancy is None or expectancy < 0
    payload = quant_lab.clean({
        "from_day": all_days[0].date().isoformat(), "split_day": cut.date().isoformat(), "to_day": all_days[-1].date().isoformat(),
        "feed": "iex", "winner": winner, "timeframe": "1Min" if best == 1 else "5Min", "experimental": failed,
        "default": {"name": "5-minute bars", "in_sample": rows[5][0], "out_of_sample": rows[5][1]},
        "challenger": {"name": "1-minute bars", "in_sample": rows[1][0], "out_of_sample": rows[1][1]},
        "spy": spy,
    })
    saved = {}
    if os.path.exists(OUTPUT):
        with open(OUTPUT, encoding="utf-8") as handle:
            saved = json.load(handle)
    saved["realtime"] = payload
    with open(OUTPUT, "w", encoding="utf-8") as handle:
        json.dump(saved, handle, separators=(",", ":"))
        handle.write("\n")
    print(f"Winner: {payload['timeframe']}. Experimental: {failed}.")
    print(json.dumps({k: payload[k] for k in ("default", "challenger", "spy")}, indent=1)[:2500])


if __name__ == "__main__":
    main()
