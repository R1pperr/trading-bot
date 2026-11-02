"""
sleeve_backtest.py  -  an honest whole-portfolio backtest of the four sleeves.

    python sleeve_backtest.py

Rules of the test:
  * Signals use a day's close. Orders fill at the next bar's open, so the test
    can't see the future. Stops fill at the stop, or at the open if it gapped past.
  * Costs: crypto 0.25% a side, stocks 0.05%. The limit-order version charges 0.15%
    on crypto buys only, because the live bot's sells are market orders.
  * Everything shares one account, with the same caps as the live bot: sleeve
    allocations, position limits, 3 new entries a day per market (short-term
    sleeves first), exits always allowed, one owner per symbol, and the kill switch.
  * The oldest 70% of the history is in-sample, the newest 30% is out-of-sample.
  * The AI veto can't be tested honestly, so it is left out.

The lists are today's lists, so companies that shrank or vanished are missing.
That flatters every stock result here, and momentum most of all.
"""

import argparse
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import config
import risk
import strategies
import universe
from data import get_crypto_daily_bars, get_daily_bars, last_complete_utc_day_end

RESULTS_DIR = "results"
STOCK_FEE = 0.0005
START_CASH = 100_000.0
STOCK_HISTORY_START = "2016-01-01"
CRYPTO_HISTORY_START = "2021-01-01"
OLD_LIMITS = dict(config.MAX_OPEN_POSITIONS)
FUND_WORDS = (" ETF", "ISHARES", "SPDR", "INVESCO QQQ", " FUND")     # how the names mark a fund
PRIORITY_SHORT = {name for name, sleeve in config.SLEEVES.items() if sleeve["horizon"] == "SHORT"}


@dataclass
class Panel:
    """Every price and indicator as a table of days by symbols."""
    dates: pd.DatetimeIndex
    symbols: list
    is_crypto: np.ndarray
    fields: dict
    etf: set = field(default_factory=set)

    def __getitem__(self, name):
        return self.fields[name]


@dataclass
class Options:
    sleeves: tuple
    atr_stops: bool = True
    crypto_fee: float = config.CRYPTO_FEE_PCT
    daily_cap: int = config.MAX_TRADES_PER_DAY
    one_owner: bool = True
    only_symbols: set = None
    start: str = None


def build_panel(tables, etf=()):
    """Line up every symbol's prices on one calendar. Days a symbol didn't trade stay empty."""
    dates = pd.DatetimeIndex(sorted(set().union(*[set(t.index) for t in tables.values()])))
    symbols = list(tables)
    names = ("open", "high", "low", "close", "rsi", "sma_exit", "sma_trend", "atr", "trend",
             "momentum", "cross")
    fields = {name: np.full((len(dates), len(symbols)), np.nan) for name in names}
    for column, symbol in enumerate(symbols):
        table = tables[symbol]
        rows = dates.get_indexer(table.index)
        for name in ("open", "high", "low", "close"):
            fields[name][rows, column] = table[name].to_numpy()
        values = strategies.indicators(table)
        for name in ("rsi", "sma_exit", "sma_trend", "atr", "trend", "momentum"):
            fields[name][rows, column] = values[name].to_numpy()
        fast = table["close"].rolling(config.FAST_SMA_DAYS).mean().to_numpy()
        slow = table["close"].rolling(config.SLOW_SMA_DAYS).mean().to_numpy()
        above = fast > slow
        was_above = np.roll(above, 1)
        ready = ~np.isnan(slow) & ~np.isnan(np.roll(slow, 1))
        ready[0] = False
        signal = np.zeros(len(table))
        signal[ready & above & ~was_above] = 1       # crossed up
        signal[ready & ~above & was_above] = -1      # crossed down
        signal[ready & ~above & (signal == 0)] = -0.5   # already below: the "missed sell" rule
        fields["cross"][rows, column] = signal
    is_crypto = np.array(["/" in symbol for symbol in symbols])
    return Panel(dates, symbols, is_crypto, fields, set(etf))


class Book:
    """One account: cash, positions and everything that happened."""

    def __init__(self):
        self.cash = START_CASH
        self.positions = {}        # (sleeve, column) -> position
        self.trades = []
        self.fees = 0.0
        self.owned = {}            # column -> sleeve

    def sleeve_value(self, sleeve, prices):
        return sum(p["qty"] * prices[key[1]] for key, p in self.positions.items() if key[0] == sleeve)

    def count(self, is_crypto, crypto):
        return sum(is_crypto[key[1]] == crypto for key in self.positions)


def fee_for(panel, column, options, by_limit):
    if not panel.is_crypto[column]:
        return STOCK_FEE
    return min(options.crypto_fee, config.CRYPTO_FEE_PCT) if by_limit else config.CRYPTO_FEE_PCT


def close_position(book, key, price, t, panel, options, reason):
    position = book.positions.pop(key)
    rate = fee_for(panel, key[1], options, False)
    value = position["qty"] * price
    fee = value * rate
    book.cash += value - fee
    book.fees += fee
    book.owned.pop(key[1], None)
    pnl = value - fee - position["cost"]
    book.trades.append({"sleeve": key[0], "symbol": panel.symbols[key[1]], "entry": position["entry_day"],
                        "exit": t, "days": t - position["entry_day"], "pnl": pnl, "reason": reason,
                        "win": pnl > 0, "fee": fee + position["entry_fee"]})


def stop_for(price, atr_value, options, market_crypto):
    if options.atr_stops and np.isfinite(atr_value) and atr_value > 0:
        return risk.atr_stop(price, atr_value)
    return risk.stop_price(price)


def simulate(panel, options):
    """Run the whole history day by day. Returns the daily equity and everything that happened."""
    T, N = panel["close"].shape
    is_crypto = panel.is_crypto
    last_price = pd.DataFrame(panel["close"]).ffill().to_numpy()
    last_atr = pd.DataFrame(panel["atr"]).ffill().to_numpy()    # stocks have no row on weekends
    book = Book()
    equity_curve = np.full(T, START_CASH)
    pending_exits, pending_entries = {}, []
    last_trade = np.full(N, -1)
    skips = {sleeve: {"owned": 0, "daily_cap": 0} for sleeve in options.sleeves}
    targets, stopped, month = set(), set(), None
    held_days = {}
    killed = False
    spy = panel.symbols.index(config.MOMENTUM_MARKET_SYMBOL) if config.MOMENTUM_MARKET_SYMBOL in panel.symbols else None
    start = 1 if options.start is None else max(1, int(panel.dates.searchsorted(pd.Timestamp(options.start))))
    equity_curve[:start] = START_CASH
    old_mode = any(sleeve.startswith("crossover") for sleeve in options.sleeves)
    limits = OLD_LIMITS if old_mode else config.SLEEVE_MAX_OPEN_POSITIONS

    for t in range(start, T):
        opens, lows, closes = panel["open"][t], panel["low"][t], panel["close"][t]

        # 1. Yesterday's decisions fill at today's open. Exits first: they are never capped.
        for key in list(pending_exits):
            if key in book.positions and np.isfinite(opens[key[1]]):
                close_position(book, key, opens[key[1]], t, panel, options, pending_exits[key])
                last_trade[key[1]] = t
                del pending_exits[key]
            elif key not in book.positions:
                del pending_exits[key]

        entries_today = {True: 0, False: 0}
        waiting = []
        for sleeve, column in pending_entries:
            crypto = bool(is_crypto[column])
            if not np.isfinite(opens[column]):
                waiting.append((sleeve, column))     # a stock signal waits for the next open
                continue
            if (sleeve, column) in book.positions or killed:
                continue
            if options.one_owner and column in book.owned:
                skips[sleeve]["owned"] += 1
                continue
            if last_trade[column] == t:
                continue
            if options.daily_cap and entries_today[crypto] >= options.daily_cap:
                skips[sleeve]["daily_cap"] += 1
                continue
            if book.count(is_crypto, crypto) >= limits["crypto" if crypto else "stock"]:
                continue
            equity_now = book.cash + sum(p["qty"] * last_price[t - 1][k[1]] for k, p in book.positions.items())
            allocation = 1.0 if old_mode else config.SLEEVES[sleeve]["allocation"]
            room = equity_now * allocation - book.sleeve_value(sleeve, last_price[t - 1])
            if sleeve == "momentum_stocks":
                room = min(room, equity_now * allocation / config.MOMENTUM_TOP_N)
            elif sleeve == "crypto_trend":
                room = min(room, equity_now * allocation / config.CRYPTO_TREND_TOP_N)
            elif sleeve == "crypto_hold":
                room = min(room, equity_now * allocation / len(config.CRYPTO_HOLD_SYMBOLS))
            price = opens[column]
            stop = stop_for(price, last_atr[t - 1][column], options, crypto) if not old_mode else risk.stop_price(price)
            dollars = risk.risk_sized_dollars(equity_now, price, stop, min(room, book.cash))
            rate = fee_for(panel, column, options, sleeve in ("crypto_trend", "crypto_hold"))
            dollars = min(dollars, book.cash / (1 + rate))
            if old_mode:
                dollars = min(equity_now * config.MAX_POSITION_PCT, book.cash / (1 + rate))
            if dollars < 1:
                continue
            fee = dollars * rate
            book.cash -= dollars + fee
            book.fees += fee
            book.positions[(sleeve, column)] = {"qty": dollars / price, "cost": dollars + fee, "stop": stop,
                                                "entry_fee": fee, "entry_day": t, "high": price, "bars": 0}
            book.owned[column] = sleeve
            last_trade[column] = t
            entries_today[crypto] += 1
        pending_entries = waiting

        # 2. Stops during the day. Stop-loss sells always go through.
        for key in list(book.positions):
            column = key[1]
            if key in pending_exits or not np.isfinite(lows[column]):
                continue
            position = book.positions[key]
            if lows[column] <= position["stop"]:
                close_position(book, key, min(opens[column], position["stop"]), t, panel, options, "stop")
                last_trade[column] = t
                if key[0] == "momentum_stocks":
                    stopped.add(column)

        # 3. The close: value the account, then decide tomorrow's orders.
        equity = book.cash + sum(p["qty"] * last_price[t][k[1]] for k, p in book.positions.items())
        equity_curve[t] = equity
        killed = equity <= equity_curve[t - 1] * (1 - config.KILL_SWITCH_PCT)

        for key, position in book.positions.items():
            column = key[1]
            if np.isfinite(closes[column]):
                position["bars"] += 1
                if is_crypto[column] and options.atr_stops and not old_mode and np.isfinite(panel["atr"][t][column]):
                    position["stop"] = max(position["stop"], closes[column] - config.ATR_STOP_MULTIPLE * panel["atr"][t][column])

        market_day = spy is not None and np.isfinite(closes[spy])
        order = []
        new_month = market_day and panel.dates[t].month != month
        if new_month:
            month = panel.dates[t].month

        for sleeve in options.sleeves:
            if sleeve == "rsi2_stocks" and market_day:
                for key, position in book.positions.items():
                    column = key[1]
                    if key[0] == sleeve and np.isfinite(closes[column]) and strategies.rsi2_exit(
                            closes[column], panel["sma_exit"][t][column], panel["sma_trend"][t][column], position["bars"]):
                        pending_exits[key] = "signal"
                wanted = strategies.rsi2_entry(closes, panel["sma_trend"][t], panel["rsi"][t]) & ~is_crypto
                if options.only_symbols is not None:
                    wanted &= np.array([s in options.only_symbols for s in panel.symbols])
                order += [(sleeve, column) for column in np.flatnonzero(wanted)]
            elif sleeve == "crypto_trend":
                top = np.flatnonzero(is_crypto)[:config.CRYPTO_TREND_TOP_N]
                for key in book.positions:
                    score = panel["trend"][t][key[1]]
                    if key[0] == sleeve and np.isfinite(score) and not strategies.trend_long(score):
                        pending_exits[key] = "signal"
                order += [(sleeve, column) for column in top if strategies.trend_long(panel["trend"][t][column])]
            elif sleeve == "momentum_stocks" and market_day:
                market_up = strategies.above_trend(closes[spy], panel["sma_trend"][t][spy])
                if new_month:
                    stocks = np.flatnonzero(~is_crypto)
                    names = strategies.top_momentum(stocks, panel["momentum"][t][stocks])
                    targets, stopped = set(names), set()
                for key in book.positions:
                    if key[0] == sleeve and (not market_up or key[1] not in targets):
                        pending_exits[key] = "signal"
                if market_up:
                    order += [(sleeve, column) for column in sorted(targets) if column not in stopped]
            elif sleeve == "crypto_hold":
                for symbol in config.CRYPTO_HOLD_SYMBOLS:
                    if symbol not in panel.symbols:
                        continue
                    column = panel.symbols.index(symbol)
                    if not np.isfinite(closes[column]):
                        continue                         # no bar today, so nothing new to act on
                    up = strategies.above_trend(closes[column], panel["sma_trend"][t][column])
                    if (sleeve, column) in book.positions and not up:
                        pending_exits[(sleeve, column)] = "signal"
                    elif up:
                        order.append((sleeve, column))
            elif sleeve.startswith("crossover"):
                crypto = sleeve == "crossover_crypto"
                if crypto or market_day:
                    for key in book.positions:
                        if key[0] == sleeve and panel["cross"][t][key[1]] < 0:
                            pending_exits[key] = "signal"
                    wanted = (panel["cross"][t] == 1) & (is_crypto == crypto)
                    order += [(sleeve, column) for column in np.flatnonzero(wanted)]

        already = set(pending_entries)
        pending_entries += [item for item in order if item not in already]
        pending_entries.sort(key=lambda item: 0 if item[0] in PRIORITY_SHORT else 1)

    trades = pd.DataFrame(book.trades, columns=["sleeve", "symbol", "entry", "exit", "days", "pnl", "reason", "win", "fee"])
    return {"equity": pd.Series(equity_curve, index=panel.dates), "trades": trades, "fees": book.fees,
            "skips": skips, "dates": panel.dates}


# ---------------------------------------------------------------------------
# Measuring results
# ---------------------------------------------------------------------------
def measure(equity, trades, first, last, days_per_year=365):
    """Numbers for one stretch of the equity curve. Trades count where they ended."""
    part = equity[(equity.index >= first) & (equity.index <= last)]
    years = (part.index[-1] - part.index[0]).days / 365.25
    total = part.iloc[-1] / part.iloc[0] - 1
    returns = part.pct_change().dropna()
    deviation = returns.std()
    closed = trades[(trades["exit_date"] >= first) & (trades["exit_date"] <= last)] if len(trades) else trades
    return {"total_return": total, "cagr": (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else np.nan,
            "max_drawdown": (part / part.cummax() - 1).min(),
            "sharpe": returns.mean() / deviation * days_per_year ** 0.5 if deviation > 0 else np.nan,
            "trades": len(closed), "win_rate": closed["win"].mean() if len(closed) else np.nan,
            "avg_days": closed["days"].mean() if len(closed) else np.nan,
            "fees": closed["fee"].sum() if len(closed) else 0.0}


def split_points(first, last):
    """The oldest 70% is in-sample, the newest 30% is out-of-sample."""
    return first, first + (last - first) * 0.7, last


def report_row(name, result, first, last, days_per_year=365, trading_days=None):
    """trading_days keeps stock-only results off the weekends, when only crypto has bars."""
    equity = result["equity"]
    if trading_days is not None:
        equity = equity[equity.index.isin(trading_days)]
    trades = result["trades"].copy()
    dates = result["dates"]
    if len(trades):
        trades["exit_date"] = dates[trades["exit"].to_numpy()]
    else:
        trades["exit_date"] = pd.to_datetime([])
    start, cut, end = split_points(first, last)
    rows = []
    for label, a, b in (("in-sample", start, cut), ("out-of-sample", cut, end)):
        row = measure(equity, trades, a, b, days_per_year=days_per_year)
        row.update(name=name, period=label, from_day=a.date().isoformat(), to_day=b.date().isoformat())
        rows.append(row)
    return rows


def hold_row(name, table, first, last, days_per_year):
    prices = table["close"]
    prices = prices[(prices.index >= first) & (prices.index <= last)]
    equity = prices / prices.iloc[0] * START_CASH
    empty = pd.DataFrame(columns=["win", "days", "exit_date"])
    rows = []
    start, cut, end = split_points(first, last)
    for label, a, b in (("in-sample", start, cut), ("out-of-sample", cut, end)):
        row = measure(equity, empty, a, b, days_per_year=days_per_year)
        row.update(name=name, period=label, from_day=a.date().isoformat(), to_day=b.date().isoformat())
        rows.append(row)
    return rows


def pct(value):
    return "n/a" if value is None or not np.isfinite(value) else f"{value * 100:+.1f}%"


def print_rows(rows):
    print(f"{'':28}{'Period':15}{'Return':>9}{'CAGR':>8}{'Worst':>8}{'Sharpe':>7}{'Trades':>7}{'Win':>6}{'Days':>6}{'Fees':>9}")
    for row in rows:
        sharpe = "n/a" if not np.isfinite(row["sharpe"]) else f"{row['sharpe']:.2f}"
        win = "n/a" if not np.isfinite(row["win_rate"]) else f"{row['win_rate'] * 100:.0f}%"
        days = "n/a" if not np.isfinite(row["avg_days"]) else f"{row['avg_days']:.0f}"
        print(f"{row['name']:28}{row['period']:15}{pct(row['total_return']):>9}{pct(row['cagr']):>8}"
              f"{pct(row['max_drawdown']):>8}{sharpe:>7}{row['trades']:>7}{win:>6}{days:>6}{row['fees']:>9,.0f}")


# ---------------------------------------------------------------------------
# The full report
# ---------------------------------------------------------------------------
def load_tables():
    stocks = [row["symbol"] for row in universe.rows("stock")]
    pairs = [row["symbol"] for row in universe.rows("crypto")]
    end = last_complete_utc_day_end()
    print(f"Downloading {len(stocks)} stocks and {len(pairs)} crypto pairs ...")
    stock_tables = get_daily_bars(stocks, STOCK_HISTORY_START, end)
    crypto_tables = get_crypto_daily_bars(pairs, CRYPTO_HISTORY_START, end)
    names = {row["symbol"]: row.get("name", "") for row in universe.rows("stock")}
    funds = {s for s, n in names.items() if any(w in (n or "").upper() for w in FUND_WORDS)}
    return stock_tables, crypto_tables, funds


def main():
    argparse.ArgumentParser(description="Backtest the four sleeves.").parse_args()
    stock_tables, crypto_tables, funds = load_tables()
    panel = build_panel({**stock_tables, **crypto_tables}, funds)
    stock_first, stock_last = min(t.index[0] for t in stock_tables.values()), max(t.index[-1] for t in stock_tables.values())
    crypto_first, crypto_last = min(t.index[0] for t in crypto_tables.values()), max(t.index[-1] for t in crypto_tables.values())
    sleeves = tuple(config.SLEEVES)
    stock_sleeves = ("rsi2_stocks", "momentum_stocks")
    crypto_sleeves = ("crypto_trend", "crypto_hold")
    stock_days = stock_tables["SPY"].index
    rows = []

    def stock_row(name, result):
        return report_row(name, result, stock_first, stock_last, 252, stock_days)

    def crypto_row(name, result):
        return report_row(name, result, crypto_first, crypto_last)

    def row_for(name, result):
        return stock_row(name, result) if name.split()[0] in stock_sleeves else crypto_row(name, result)

    print("\nEach sleeve alone, with its own share of $100,000 and the rest in cash:")
    alone = {}
    for sleeve in sleeves:
        alone[sleeve] = simulate(panel, Options((sleeve,)))
        rows += row_for(sleeve, alone[sleeve])

    etf_only = simulate(panel, Options(("rsi2_stocks",), only_symbols=funds))
    single = simulate(panel, Options(("rsi2_stocks",), only_symbols=set(stock_tables) - funds))
    rows += stock_row("rsi2 funds only", etf_only)
    rows += stock_row("rsi2 single stocks only", single)

    combined = simulate(panel, Options(sleeves, start=CRYPTO_HISTORY_START))
    rows += crypto_row("combined mix (2021+)", combined)

    old = simulate(panel, Options(("crossover_stock", "crossover_crypto"), start=CRYPTO_HISTORY_START))
    rows += crypto_row("old crossover (2021+)", old)
    old_stock = simulate(panel, Options(("crossover_stock",)))
    rows += stock_row("old crossover, stocks", old_stock)

    rows += hold_row("buy and hold SPY", stock_tables["SPY"], stock_first, stock_last, 252)
    rows += hold_row("buy and hold BTC", crypto_tables["BTC/USD"], crypto_first, crypto_last, 365)
    rows += hold_row("SPY, same period as mix", stock_tables["SPY"], crypto_first, crypto_last, 252)
    print_rows(rows)

    print("\nThe two stock sleeves together, on the stock history:")
    pair_rows = []
    for label, atr_stops in (("fixed 7%", False), ("3 x ATR", True)):
        pair_rows += stock_row(f"rsi2 + momentum, {label}", simulate(panel, Options(stock_sleeves, atr_stops=atr_stops)))
    print_rows(pair_rows)
    rows += pair_rows

    print("\nStops before and after, out-of-sample (fixed 7% against 3 x ATR):")
    stop_rows = []
    for sleeve in sleeves:
        fixed = row_for(f"{sleeve} fixed 7%", simulate(panel, Options((sleeve,), atr_stops=False)))[1]
        stop_rows += [fixed, {**[r for r in rows if r["name"] == sleeve and r["period"] == "out-of-sample"][0], "name": f"{sleeve} 3 x ATR"}]
    fixed_mix = crypto_row("combined fixed 7%", simulate(panel, Options(sleeves, atr_stops=False, start=CRYPTO_HISTORY_START)))[1]
    stop_rows += [fixed_mix, {**[r for r in rows if r["name"].startswith("combined") and r["period"] == "out-of-sample"][0], "name": "combined 3 x ATR"}]
    print_rows(stop_rows)

    print("\nCrypto fee check, out-of-sample (0.15% on buys against 0.25%; sells stay at 0.25%):")
    fee_rows = []
    for sleeve in crypto_sleeves:
        cheap = crypto_row(f"{sleeve} at 0.15%", simulate(panel, Options((sleeve,), crypto_fee=config.CRYPTO_LIMIT_FEE_PCT)))[1]
        fee_rows += [{**[r for r in rows if r["name"] == sleeve and r["period"] == "out-of-sample"][0], "name": f"{sleeve} at 0.25%"}, cheap]
    print_rows(fee_rows)

    print("\nSignals skipped in the combined mix:")
    print(f"{'':22}{'Another sleeve owned it':>26}{'Daily cap of 3':>16}")
    for sleeve in sleeves:
        print(f"{sleeve:22}{combined['skips'][sleeve]['owned']:>26}{combined['skips'][sleeve]['daily_cap']:>16}")

    print("\nThe combined mix without those two limits:")
    free = simulate(panel, Options(sleeves, daily_cap=0, one_owner=False, start=CRYPTO_HISTORY_START))
    print_rows([{**r, "name": "with both limits"} for r in crypto_row("x", combined)]
               + [{**r, "name": "without both limits"} for r in crypto_row("x", free)])

    print("\nEach sleeve alone on the mix's dates, so the parts can be added up and compared:")
    parts = []
    for sleeve in sleeves:
        parts += crypto_row(sleeve, simulate(panel, Options((sleeve,), start=CRYPTO_HISTORY_START)))
    print_rows(parts)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    pd.DataFrame(rows).to_csv(os.path.join(RESULTS_DIR, "sleeve_backtest_summary.csv"), index=False)
    print(f"\nSaved: {RESULTS_DIR}/sleeve_backtest_summary.csv")
    print("WARNING: today's lists are used for the whole history, so survivorship bias flatters the")
    print("stock results, momentum most of all. The AI veto isn't included. Past results do not predict the future.")


if __name__ == "__main__":
    main()
