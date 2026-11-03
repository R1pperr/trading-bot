"""
rt_backtest.py  -  the A/B test for the real-time engine: 5-minute bars against 1-minute bars.

    python rt_backtest.py

It follows the rules in realtime/src/engine.ts: EMA(9) and EMA(21) on completed SPY bars,
warmed up with the previous session. A signal on a bar's close fills at the next bar's open.
Long only, 25% of equity in whole shares, a stop 2% below the entry, at most 20 entries a day,
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
POSITION_PCT = 0.25
STOP_PCT = 0.02
MAX_ENTRIES = 20
DAILY_LOSS_PCT = 0.02
NO_ENTRIES_FROM = 15 * 60 + 45
FLAT_FROM = 15 * 60 + 55
WHIPSAW_BARS = 3


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


def simulate(days):
    """days: a list of (date, table) with open, high, low, close and a minute column, oldest first."""
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
                qty = int(cash * POSITION_PCT // opens[i])
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
