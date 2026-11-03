"""
quant_lab.py  -  the whole-portfolio tests for the sleeves.

    python quant_lab.py              run everything and write docs/data/backtest.json
    python quant_lab.py --refresh    download the prices again first

What it does, all on one saved copy of the prices so every comparison sees the same data:
  * Runs every sleeve, every horizon, the 60/20/20 mix, the old live setup and SPY.
  * Runs the three A/B tests and picks each winner with the fixed rule in
    strategies.ab_winner, on out-of-sample numbers only. It reports the winners, and
    they are copied into config.py by hand. Nothing here changes the bot.
  * Robustness: reruns the new sleeves with nearby settings. This is a report only.
    The live bot never moves to the best-looking one.
  * Monte Carlo: shuffles each sleeve's trades 1,000 times to show how bad the worst
    drop could have been with the same trades in a different order.
  * Correlations between sleeves, and the ranges the live drift monitor compares to.

Rules: signals use a day's close, orders fill at the next open, and costs are the same
as the last backtest. The AI can't be tested honestly, so it's left out.

The stock list is today's list, so companies that shrank or vanished are missing. That
flatters every stock result here, and momentum most of all.

This file reads price history only. It never places an order.
"""

import argparse
import json
import os
import pickle
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import config
import sleeve_backtest as backtest
import strategies
import universe
from data import get_intraday_bars

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, ".cache", "quant_lab")
OUTPUT = os.path.join(BASE_DIR, "docs", "data", "backtest.json")
LAST_DAY = "2026-10-30"             # the end of the last backtest, so the split stays the same
TRADING_DAYS = 252
MONTE_CARLO_RUNS = 1_000
MONTE_CARLO_SEED = 20261103
DRIFT_WINDOW = 20                   # the drift monitor compares the latest 20 live trades
CURVE_POINTS = 160                  # enough for a chart, small enough for the website

MIX = ("turn_of_month", "ibs_etfs", "rsi2_stocks", "rotation", "momentum_stocks")
HORIZON_SLEEVES = {"SHORT": ("turn_of_month", "ibs_etfs", "rsi2_stocks"), "MEDIUM": ("rotation",),
                   "LONG": ("momentum_stocks",)}
LIVE_SLEEVES = ("rsi2_stocks", "momentum_stocks")
LIVE_SHARES = {"rsi2_stocks": 0.50, "momentum_stocks": 0.15}      # the setup that ran before this upgrade


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------
def load_prices(refresh):
    """Every comparison uses one saved copy, because Alpaca's adjusted history can change between runs."""
    path = os.path.join(CACHE_DIR, "daily.pkl")
    if os.path.exists(path) and not refresh:
        with open(path, "rb") as handle:
            return pickle.load(handle)
    from data import get_daily_bars
    listed = [row["symbol"] for row in universe.rows("stock")]
    extras = [s for s in config.ROTATION_SYMBOLS + [config.TURN_OF_MONTH_SYMBOL] if s not in listed]
    print(f"Downloading {len(listed) + len(extras)} symbols ...", flush=True)
    end = datetime.fromisoformat(LAST_DAY) + timedelta(days=1)
    tables = get_daily_bars(listed + extras, backtest.STOCK_HISTORY_START, end)
    names = {row["symbol"]: row.get("name", "") for row in universe.rows("stock")}
    funds = {s for s, n in names.items() if any(w in (n or "").upper() for w in backtest.FUND_WORDS)}
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "wb") as handle:
        pickle.dump((tables, funds, extras), handle)
    return tables, funds, extras


class FifteenMinuteBars:
    """Fetches a symbol's 15-minute bars one month at a time, only when a stop might have been hit."""

    def __init__(self):
        self.path = os.path.join(CACHE_DIR, "bars15.pkl")
        self.months = {}
        self.fetched = 0
        if os.path.exists(self.path):
            with open(self.path, "rb") as handle:
                self.months = pickle.load(handle)

    def __call__(self, symbol, day):
        key = (symbol, day.strftime("%Y-%m"))
        if key not in self.months:
            first = day.replace(day=1)
            last = first + pd.offsets.MonthBegin(1)
            time.sleep(60 / config.ALPACA_CALLS_PER_MINUTE)
            table = get_intraday_bars(symbol, first.to_pydatetime(), last.to_pydatetime(), 15)
            self.months[key] = {stamp: group[["open", "high", "low", "close"]].to_numpy()
                                for stamp, group in table.groupby(table.index.normalize())}
            self.fetched += 1
            if self.fetched % 50 == 0:
                print(f"  ... {self.fetched} symbol-months of 15-minute bars so far", flush=True)
                self.save()
        return self.months[key].get(day, np.empty((0, 4)))

    def save(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(self.path, "wb") as handle:
            pickle.dump(self.months, handle)


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------
def max_drawdown(curve):
    curve = np.asarray(curve, dtype=float)
    return float((curve / np.maximum.accumulate(curve) - 1).min()) if len(curve) else float("nan")


def metrics(equity, trades, invested=None):
    """The full list of numbers for one stretch of an equity curve."""
    returns = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    total = equity.iloc[-1] / equity.iloc[0] - 1
    deviation = returns.std()
    downside = float(np.sqrt((np.minimum(returns, 0) ** 2).mean())) if len(returns) else 0.0
    row = {"total_return": float(total),
           "cagr": float((1 + total) ** (1 / years) - 1) if years > 0 and total > -1 else float("nan"),
           "sharpe": float(returns.mean() / deviation * TRADING_DAYS ** 0.5) if deviation > 0 else float("nan"),
           "sortino": float(returns.mean() / downside * TRADING_DAYS ** 0.5) if downside > 0 else float("nan"),
           "max_drawdown": max_drawdown(equity), "trades": int(len(trades))}
    if len(trades):
        wins, losses = trades.loc[trades["pnl"] > 0, "pnl"], trades.loc[trades["pnl"] <= 0, "pnl"]
        row.update(expectancy=float(trades["pnl"].mean()), expectancy_pct=float(trades["ret"].mean()),
                   profit_factor=float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else float("nan"),
                   win_rate=float(trades["win"].mean()), avg_days=float(trades["days"].mean()),
                   avg_win=float(wins.mean()) if len(wins) else 0.0, avg_loss=float(losses.mean()) if len(losses) else 0.0,
                   fees=float(trades["fee"].sum()))
    else:
        row.update(expectancy=None, expectancy_pct=None, profit_factor=None, win_rate=None, avg_days=None,
                   avg_win=None, avg_loss=None, fees=0.0)
    row["time_invested"] = None if invested is None else float((invested > 1).mean())
    return row


def report(result, cut, market_days):
    """(in-sample, out-of-sample) numbers. A trade counts in the period it ended."""
    equity = result["equity"][result["equity"].index.isin(market_days)]
    trades = result["trades"].copy()
    trades["exit_date"] = result["dates"][trades["exit"].to_numpy(dtype=int)] if len(trades) else pd.to_datetime([])
    invested = result["exposure"].sum(axis=1).reindex(equity.index)
    parts = []
    for early in (True, False):
        pick = equity.index <= cut if early else equity.index >= cut
        closed = trades[trades["exit_date"] <= cut] if early else trades[trades["exit_date"] > cut]
        parts.append(metrics(equity[pick], closed, invested[pick]))
    return parts[0], parts[1]


def hold_report(table, cut):
    """Buy and hold, measured the same way."""
    prices = table["close"]
    empty = pd.DataFrame(columns=["pnl", "ret", "win", "days", "fee"])
    early, late = prices[prices.index <= cut], prices[prices.index >= cut]
    return (metrics(early, empty, pd.Series(1e9, index=early.index)), metrics(late, empty, pd.Series(1e9, index=late.index)))


def monte_carlo(impacts, runs=MONTE_CARLO_RUNS, seed=MONTE_CARLO_SEED):
    """The same trades in 1,000 random orders: the 5th, 50th and 95th percentile of the worst drop."""
    impacts = np.asarray(impacts, dtype=float)
    if len(impacts) < 2:
        return None
    generator = np.random.default_rng(seed)
    worst = np.empty(runs)
    for run in range(runs):
        path = np.cumprod(1 + generator.permutation(impacts))
        path = np.concatenate(([1.0], path))
        worst[run] = (path / np.maximum.accumulate(path) - 1).min()
    low, middle, high = np.percentile(worst, [5, 50, 95])
    return {"p5": float(low), "p50": float(middle), "p95": float(high), "trades": int(len(impacts))}


def drift_range(returns, window=DRIFT_WINDOW):
    """What a run of 20 trades looked like out-of-sample. Live numbers outside this draw a warning."""
    series = pd.Series(np.asarray(returns, dtype=float))
    if len(series) < window:
        return None
    means = series.rolling(window).mean().dropna()
    wins = (series > 0).astype(float).rolling(window).mean().dropna()
    return {"expectancy_low": float(means.quantile(0.05)), "expectancy_high": float(means.quantile(0.95)),
            "win_low": float(wins.quantile(0.05)), "win_high": float(wins.quantile(0.95)), "trades": int(len(series))}


def thin(series, points=CURVE_POINTS):
    """A curve small enough for the website, rebased to 100."""
    series = series.dropna()
    if not len(series):
        return []
    step = max(1, len(series) // points)
    kept = pd.concat([series.iloc[::step], series.iloc[[-1]]])
    kept = kept[~kept.index.duplicated()]
    return [[stamp.date().isoformat(), round(float(value / series.iloc[0] * 100), 2)] for stamp, value in kept.items()]


def clean(value):
    """Plain numbers only, and None instead of NaN, so the file is valid JSON."""
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    if isinstance(value, (float, np.floating)):
        return None if not np.isfinite(value) else round(float(value), 5)
    if isinstance(value, np.integer):
        return int(value)
    return value


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Run the portfolio tests. Reads prices, places nothing.")
    parser.add_argument("--refresh", action="store_true", help="download the prices again")
    args = parser.parse_args()
    os.chdir(BASE_DIR)

    tables, funds, extras = load_prices(args.refresh)
    panel = backtest.build_panel(tables, funds, extras)
    spy = tables["SPY"]
    first, last = min(t.index[0] for t in tables.values()), max(t.index[-1] for t in tables.values())
    _, cut, _ = backtest.split_points(first, last)
    market_days = spy.index
    bars = FifteenMinuteBars()
    print(f"{len(tables)} symbols, {first.date()} to {last.date()}, split at {cut.date()}", flush=True)

    def options(sleeves=MIX, **changes):
        settings = dict(atr_stops=False, stop_bars=bars, stop_mode="alpaca")
        settings.update(changes)
        return backtest.Options(tuple(sleeves), **settings)

    def run(label, **changes):
        print(f"  {label}", flush=True)
        return backtest.simulate(panel, options(**changes))

    # --- The three A/B tests, each against the same default portfolio --------
    print("A/B tests:", flush=True)
    base = run("default portfolio")
    base_out = report(base, cut, market_days)
    ab_tests = []
    pairs = (("stop_execution", "Stop-loss execution", "Stop order at Alpaca", "15-minute check", {"stop_mode": "check"}),
             ("momentum", "Long-term momentum", "12-1 momentum", "Volatility-scaled momentum", {"vol_scaled": True}),
             ("sizing", "Position sizing", "Current sizing", "VOL_SIZING", {"vol_sizing": True}))
    winners = {}
    for key, title, default_name, challenger_name, change in pairs:
        result = run(challenger_name, **change)
        numbers = report(result, cut, market_days)
        winner = strategies.ab_winner(base_out[1], numbers[1])
        winners[key] = winner
        ab_tests.append({"key": key, "title": title, "winner": winner,
                         "default": {"name": default_name, "in_sample": base_out[0], "out_of_sample": base_out[1]},
                         "challenger": {"name": challenger_name, "in_sample": numbers[0], "out_of_sample": numbers[1]}})
    bars.save()

    chosen = {}
    if winners["stop_execution"] == "challenger":
        chosen["stop_mode"] = "check"
    if winners["momentum"] == "challenger":
        chosen["vol_scaled"] = True
    if winners["sizing"] == "challenger":
        chosen["vol_sizing"] = True

    # --- Everything else, with the winners applied ---------------------------
    print("Sleeves, horizons and the mix:", flush=True)
    mix_shares = strategies.split_by_horizon(MIX)
    rows, solo = [], {}

    def add(key, name, group, result):
        early, late = report(result, cut, market_days)
        rows.append({"key": key, "name": name, "group": group, "in_sample": early, "out_of_sample": late})

    names = {"turn_of_month": "Turn of the month", "ibs_etfs": "IBS funds", "rsi2_stocks": "RSI(2) stocks",
             "rotation": "Asset rotation", "momentum_stocks": "12-1 momentum"}
    for sleeve in MIX:
        solo[sleeve] = run(sleeve, sleeves=(sleeve,), allocations={sleeve: mix_shares[sleeve]}, **chosen)
        add(sleeve, names[sleeve], "sleeve", solo[sleeve])
    momentum_other = {**chosen, "vol_scaled": not chosen.get("vol_scaled", False)}
    other_name = "12-1 momentum" if momentum_other["vol_scaled"] is False else "Volatility-scaled momentum"
    add("momentum_other", other_name, "sleeve",
        run(other_name, sleeves=("momentum_stocks",), allocations={"momentum_stocks": mix_shares["momentum_stocks"]},
            **momentum_other))
    for horizon, sleeves in HORIZON_SLEEVES.items():
        add(horizon.lower(), f"{horizon.title()} horizon", "horizon",
            run(horizon, sleeves=sleeves, allocations={s: mix_shares[s] for s in sleeves}, **chosen))
    mix = run("60/20/20 mix", **chosen)
    add("mix", "60/20/20 mix", "mix", mix)
    live = run("current live setup", sleeves=LIVE_SLEEVES, allocations=LIVE_SHARES, excluded=(), stop_mode="check")
    add("live", "Current live setup", "live", live)
    early, late = hold_report(spy, cut)
    rows.append({"key": "spy", "name": "SPY buy and hold", "group": "benchmark", "in_sample": early, "out_of_sample": late})
    bars.save()

    # --- References and checks -------------------------------------------------
    print("IBS reference and robustness:", flush=True)
    ibs_share = {"ibs_etfs": mix_shares["ibs_etfs"]}
    reference = run("IBS close to close", sleeves=("ibs_etfs",), allocations=ibs_share, ibs_at_close=True, **chosen)
    add("ibs_close", "IBS, close to close (reference only)", "reference", reference)

    grid = []

    def nearby(sleeve, setting, value, params):
        result = run(f"{sleeve} {setting}={value}", sleeves=(sleeve,), allocations={sleeve: mix_shares[sleeve]},
                     params=params, **chosen)
        numbers = report(result, cut, market_days)[1]
        grid.append({"sleeve": sleeve, "setting": setting, "value": value, "standard": params == {},
                     "cagr": numbers["cagr"], "sharpe": numbers["sharpe"], "max_drawdown": numbers["max_drawdown"],
                     "expectancy_pct": numbers["expectancy_pct"], "trades": numbers["trades"]})

    nearby("ibs_etfs", "standard", "0.2 / 0.8, 5 days", {})
    for value in (0.15, 0.25):
        nearby("ibs_etfs", "entry below", value, {"ibs_entry": value})
    for value in (0.75, 0.85):
        nearby("ibs_etfs", "exit above", value, {"ibs_exit": value})
    for value in (4, 6):
        nearby("ibs_etfs", "time stop days", value, {"ibs_days": value})
    nearby("rotation", "standard", "top 3", {})
    for value in (2, 4):
        nearby("rotation", "top", value, {"rotation_top": value})

    print("Monte Carlo, correlations and drift ranges:", flush=True)
    carlo, ranges, skipped = {}, {}, {}
    for sleeve in MIX:
        trades = solo[sleeve]["trades"]
        ended = solo[sleeve]["dates"][trades["exit"].to_numpy(dtype=int)] if len(trades) else pd.to_datetime([])
        recent = trades[ended > cut]
        carlo[sleeve] = monte_carlo(recent["impact"].to_numpy())
        ranges[sleeve] = drift_range(recent["ret"].to_numpy())
        skipped[sleeve] = {"owned": mix["skips"][sleeve]["owned"], "daily_cap": mix["skips"][sleeve]["daily_cap"],
                           "taken": int((mix["trades"]["sleeve"] == sleeve).sum())}

    daily = pd.DataFrame({sleeve: solo[sleeve]["equity"][solo[sleeve]["equity"].index.isin(market_days)].pct_change()
                          for sleeve in MIX})
    daily = daily[daily.index >= cut]
    correlation = daily.corr().round(2)

    keep = market_days[market_days >= cut]
    exposure = mix["exposure"].reindex(keep).div(mix["equity"].reindex(keep), axis=0)
    curves = {"mix": thin(mix["equity"].reindex(keep)), "live": thin(live["equity"].reindex(keep))}
    curves.update({sleeve: thin(solo[sleeve]["equity"].reindex(keep)) for sleeve in MIX})

    payload = clean({
        "simulated": True,
        "made_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "from_day": first.date().isoformat(), "split_day": cut.date().isoformat(), "to_day": last.date().isoformat(),
        "symbols": len(tables), "start_cash": backtest.START_CASH,
        "notes": ["Simulated history, not real trading.",
                  "Today's stock list is used for the whole history, which flatters the stock results.",
                  "The AI veto can't be tested honestly, so it is left out."],
        "names": names, "rows": rows, "ab_tests": ab_tests, "shares": mix_shares,
        "skipped": skipped, "robustness": grid, "monte_carlo": carlo, "drift_ranges": ranges,
        "correlation": {"names": list(correlation.columns), "matrix": correlation.to_numpy().tolist()},
        "curves": curves,
        "exposure": {"average": exposure.sum(axis=1).mean(), "by_sleeve": exposure.mean().to_dict()},
    })
    previous = {}
    if os.path.exists(OUTPUT):
        with open(OUTPUT, encoding="utf-8") as handle:
            previous = json.load(handle)
    if "realtime" in previous:
        payload["realtime"] = previous["realtime"]
    with open(OUTPUT, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
        handle.write("\n")

    print("\nA/B winners:")
    for test in ab_tests:
        side = test[test["winner"]]
        print(f"  {test['title']}: {side['name']}")
    print(f"\nWrote {os.path.relpath(OUTPUT, BASE_DIR)} ({os.path.getsize(OUTPUT) / 1024:.0f} KB)")
    print("WARNING: today's list is used for the whole history, so survivorship bias flatters the stock results.")


if __name__ == "__main__":
    main()
