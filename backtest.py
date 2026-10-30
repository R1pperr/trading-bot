"""
backtest.py  -  replays the strategy over past prices and compares it with
simply buying and holding.

Run with the settings from config.py:
    python backtest.py
Or choose your own period / symbols:
    python backtest.py --start 2023-01-01
    python backtest.py --start 2018-01-01 --end 2021-12-31 --symbols SPY AAPL

What is simulated, for each symbol separately, starting with BACKTEST_CASH:
  * A signal from a day's close is traded at the NEXT day's open.
  * Every buy is filled SLIPPAGE_PCT above the market price and every sell
    SLIPPAGE_PCT below it, and FEE_PCT is charged on every trade.
  * Stop-loss: if the day's low touches STOP_LOSS_PCT below the buy price, the
    position is sold at the stop price (or at the open, if the price had
    already gapped below the stop overnight).
  * When the strategy buys, it uses all of that symbol's test money.
    Buy-and-hold buys on the first day and never sells.

Results are printed and also saved in the "results" folder:
    backtest_summary.csv  backtest_trades.csv  backtest_equity.csv  backtest_chart.png
"""

import argparse
import os
import sys
from datetime import datetime, timedelta

import pandas as pd

import config
from data import get_daily_bars, last_complete_day_end
from strategy import BUY, HOLD, SELL, generate_signals

RESULTS_DIR = "results"
ALPACA_HISTORY_START = datetime(2016, 1, 1)


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
def simulate(symbol, prices, start, cash, fee_pct, slippage_pct, stop_pct):
    """Run the strategy and buy-and-hold over one symbol's prices."""
    table = generate_signals(prices)
    first = int(table.index.searchsorted(pd.Timestamp(start)))
    if first >= len(table):
        return None

    cash_left = float(cash)
    shares = 0.0
    entry = None
    costs = 0.0
    trades = []
    equity = []
    in_market = []

    def sell(day, market_price, reason):
        nonlocal cash_left, shares, entry, costs
        fill = market_price * (1 - slippage_pct)
        value = shares * fill
        fee = value * fee_pct
        costs += fee + shares * market_price * slippage_pct
        proceeds = value - fee
        pnl = proceeds - entry["cost"]
        trades.append({
            "symbol": symbol,
            "entry_date": entry["date"].date().isoformat(),
            "entry_price": round(float(entry["price"]), 4),
            "exit_date": day.date().isoformat(),
            "exit_price": round(float(fill), 4),
            "exit_reason": reason,
            "days_held": (day - entry["date"]).days,
            "pnl_dollars": round(float(pnl), 2),
            "pnl_pct": round(float(pnl / entry["cost"] * 100), 2),
        })
        cash_left += proceeds
        shares = 0.0
        entry = None

    for i in range(first, len(table)):
        day = table.index[i]
        bar = table.iloc[i]
        yesterdays_signal = table["signal"].iloc[i - 1] if i > 0 else HOLD

        # 1) Act on yesterday's signal at today's open.
        if yesterdays_signal == BUY and shares == 0:
            fill = bar["open"] * (1 + slippage_pct)
            budget = cash_left
            shares = budget / (fill * (1 + fee_pct))
            fee = budget - shares * fill
            costs += fee + shares * bar["open"] * slippage_pct
            cash_left = 0.0
            entry = {"date": day, "price": fill, "cost": budget}
        elif yesterdays_signal == SELL and shares > 0:
            sell(day, bar["open"], "sell signal")

        # 2) Stop-loss check during the day.
        if shares > 0:
            stop_price = entry["price"] * (1 - stop_pct)
            if bar["low"] <= stop_price:
                sell(day, min(bar["open"], stop_price), "stop-loss")

        equity.append(cash_left + shares * bar["close"])
        in_market.append(shares > 0)

    test = table.iloc[first:]
    strategy_equity = pd.Series(equity, index=test.index)

    # Buy-and-hold: buy at the first day's open, never sell.
    first_open = test["open"].iloc[0]
    hold_fill = first_open * (1 + slippage_pct)
    hold_shares = cash / (hold_fill * (1 + fee_pct))
    hold_costs = (cash - hold_shares * hold_fill) + hold_shares * first_open * slippage_pct
    hold_equity = hold_shares * test["close"]

    open_position = None
    if shares > 0:
        open_position = {
            "since": entry["date"].date().isoformat(),
            "buy_price": entry["price"],
            "unrealized_pct": (shares * test["close"].iloc[-1] / entry["cost"] - 1) * 100,
        }

    return {
        "symbol": symbol,
        "first_day": test.index[0],
        "last_day": test.index[-1],
        "days": len(test),
        "strategy_equity": strategy_equity,
        "hold_equity": hold_equity,
        "trades": trades,
        "costs": costs,
        "hold_costs": hold_costs,
        "time_in_market": sum(in_market) / len(in_market),
        "open_position": open_position,
        "cash": cash,
    }


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------
def total_return(equity, cash):
    return equity.iloc[-1] / cash - 1


def per_year(total, first_day, last_day):
    years = (last_day - first_day).days / 365.25
    return (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else float("nan")


def max_drawdown(equity):
    """Largest fall from a previous high, e.g. -0.25 means it fell 25% at worst."""
    return (equity / equity.cummax() - 1).min()


def pct(x):
    return "  n/a" if pd.isna(x) else f"{x * 100:+.1f}%"


def measure(result):
    cash = result["cash"]
    closed = result["trades"]
    wins = [t for t in closed if t["pnl_dollars"] > 0]
    s_total = total_return(result["strategy_equity"], cash)
    h_total = total_return(result["hold_equity"], cash)
    return {
        "symbol": result["symbol"],
        "from": result["first_day"].date().isoformat(),
        "to": result["last_day"].date().isoformat(),
        "strategy_return": s_total,
        "hold_return": h_total,
        "strategy_per_year": per_year(s_total, result["first_day"], result["last_day"]),
        "hold_per_year": per_year(h_total, result["first_day"], result["last_day"]),
        "strategy_max_drawdown": max_drawdown(result["strategy_equity"]),
        "hold_max_drawdown": max_drawdown(result["hold_equity"]),
        "closed_trades": len(closed),
        "win_rate": len(wins) / len(closed) if closed else float("nan"),
        "stop_loss_exits": sum(1 for t in closed if t["exit_reason"] == "stop-loss"),
        "time_in_market": result["time_in_market"],
        "strategy_costs": result["costs"],
        "hold_costs": result["hold_costs"],
        "beat_buy_and_hold": s_total > h_total,
    }


def print_report(result, m):
    cash = result["cash"]
    print("\n" + "=" * 62)
    print(f"{m['symbol']}   {m['from']} → {m['to']}   ({result['days']} trading days)")
    print("=" * 62)
    print(f"{'':28}{'Strategy':>16}{'Buy & hold':>16}")
    print(f"{'Start money':28}{'$' + format(cash, ',.0f'):>16}{'$' + format(cash, ',.0f'):>16}")
    s_end, h_end = result["strategy_equity"].iloc[-1], result["hold_equity"].iloc[-1]
    print(f"{'End money':28}{'$' + format(s_end, ',.0f'):>16}{'$' + format(h_end, ',.0f'):>16}")
    print(f"{'Total return':28}{pct(m['strategy_return']):>16}{pct(m['hold_return']):>16}")
    print(f"{'Return per year':28}{pct(m['strategy_per_year']):>16}{pct(m['hold_per_year']):>16}")
    print(f"{'Max drawdown (worst fall)':28}{pct(m['strategy_max_drawdown']):>16}{pct(m['hold_max_drawdown']):>16}")
    print(f"{'Time in the market':28}{m['time_in_market'] * 100:>15.0f}%{'100%':>16}")
    print(f"{'Fees + slippage paid':28}{'$' + format(m['strategy_costs'], ',.0f'):>16}{'$' + format(m['hold_costs'], ',.0f'):>16}")
    print(f"{'Closed trades':28}{m['closed_trades']:>16}{'—':>16}")
    win_rate = "n/a" if pd.isna(m["win_rate"]) else f"{m['win_rate'] * 100:.0f}%"
    print(f"{'Win rate':28}{win_rate:>16}{'—':>16}")
    print(f"{'Exits by stop-loss':28}{m['stop_loss_exits']:>16}{'—':>16}")
    if result["open_position"]:
        p = result["open_position"]
        print(f"Still holding at the end: bought {p['since']} at ${p['buy_price']:,.2f}, "
              f"{p['unrealized_pct']:+.1f}% so far (counted in End money, not in win rate).")


def print_summary(measurements):
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"{'Symbol':8}{'Strategy':>10}{'Buy&hold':>10}{'Strat. max DD':>15}{'B&H max DD':>12}"
          f"{'Trades':>8}{'Win rate':>10}{'Beat B&H?':>11}")
    for m in measurements:
        win_rate = "n/a" if pd.isna(m["win_rate"]) else f"{m['win_rate'] * 100:.0f}%"
        print(f"{m['symbol']:8}{pct(m['strategy_return']):>10}{pct(m['hold_return']):>10}"
              f"{pct(m['strategy_max_drawdown']):>15}{pct(m['hold_max_drawdown']):>12}"
              f"{m['closed_trades']:>8}{win_rate:>10}{'yes' if m['beat_buy_and_hold'] else 'no':>11}")
    print("(max DD = max drawdown: the worst fall from a previous high)")
    beaten = sum(m["beat_buy_and_hold"] for m in measurements)
    print(f"\nThe strategy beat buy-and-hold on {beaten} of {len(measurements)} symbols over this period.")
    print(f"Reminder: the bot puts at most {config.MAX_POSITION_PCT:.0%} of your account into each position,")
    print(f"so a +20% result above would move your whole account by only about "
          f"{0.20 * config.MAX_POSITION_PCT:+.0%}.")
    print("Past results do not predict future results.")


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------
def save_files(results, measurements):
    os.makedirs(RESULTS_DIR, exist_ok=True)

    pd.DataFrame(measurements).to_csv(os.path.join(RESULTS_DIR, "backtest_summary.csv"), index=False)

    all_trades = [t for r in results for t in r["trades"]]
    columns = ["symbol", "entry_date", "entry_price", "exit_date", "exit_price",
               "exit_reason", "days_held", "pnl_dollars", "pnl_pct"]
    pd.DataFrame(all_trades, columns=columns).to_csv(
        os.path.join(RESULTS_DIR, "backtest_trades.csv"), index=False)

    curves = []
    for r in results:
        curves.append(pd.DataFrame({
            "date": r["strategy_equity"].index.date,
            "symbol": r["symbol"],
            "strategy_equity": r["strategy_equity"].round(2).values,
            "buy_and_hold_equity": r["hold_equity"].round(2).values,
        }))
    pd.concat(curves).to_csv(os.path.join(RESULTS_DIR, "backtest_equity.csv"), index=False)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker
    except ImportError:
        print("\n(Chart skipped: run  python -m pip install matplotlib  to get it.)")
        return

    fig, axes = plt.subplots(len(results), 1, figsize=(10, 2.8 * len(results)), squeeze=False)
    for ax, r in zip(axes[:, 0], results):
        ax.plot(r["hold_equity"].index, r["hold_equity"].values, label="Buy & hold", color="#9aa0a6")
        ax.plot(r["strategy_equity"].index, r["strategy_equity"].values, label="Strategy", color="#1a73e8")
        ax.set_title(f"{r['symbol']}: value of ${r['cash']:,.0f}", loc="left", fontsize=11)
        ax.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))
        ax.grid(alpha=0.3)
        ax.legend(loc="best", fontsize=9)
    fig.tight_layout()
    fig.savefig(os.path.join(RESULTS_DIR, "backtest_chart.png"), dpi=110)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Backtest the moving-average strategy.")
    parser.add_argument("--start", default=config.BACKTEST_START, help="first test day, e.g. 2020-01-01")
    parser.add_argument("--end", default=None, help="last test day (default: last finished trading day)")
    parser.add_argument("--symbols", nargs="+", default=config.SYMBOLS, help="e.g. --symbols SPY AAPL")
    args = parser.parse_args()

    symbols = [s.upper() for s in args.symbols]
    start = datetime.fromisoformat(args.start)
    latest_end = last_complete_day_end().replace(tzinfo=None)
    end = min(datetime.fromisoformat(args.end) + timedelta(days=1), latest_end) if args.end else latest_end

    if (end - start).days < 365:
        print("❌ The test period must be at least 1 year. Choose an earlier --start.")
        sys.exit(1)
    if start < ALPACA_HISTORY_START + timedelta(days=120):
        print("⚠️  Alpaca's free history starts in 2016, so the first months may be missing.")

    # Download extra days before the start so the averages are ready on day one.
    warm_up = timedelta(days=int(config.SLOW_SMA_DAYS * 1.6) + 30)
    print(f"Downloading daily prices for {', '.join(symbols)} ...")
    try:
        prices = get_daily_bars(symbols, start - warm_up, end)
    except Exception as error:
        print("❌ Could not download prices from Alpaca:", error)
        sys.exit(1)

    print(f"Strategy: {config.FAST_SMA_DAYS}/{config.SLOW_SMA_DAYS}-day moving-average crossover, "
          f"{config.STOP_LOSS_PCT:.0%} stop-loss")
    print(f"Costs: {config.FEE_PCT:.2%} fee + {config.SLIPPAGE_PCT:.2%} slippage on every trade")

    results, measurements = [], []
    for symbol in symbols:
        if symbol not in prices:
            print(f"\n⚠️  {symbol}: no price data found (check the ticker). Skipped.")
            continue
        result = simulate(symbol, prices[symbol], start, config.BACKTEST_CASH,
                          config.FEE_PCT, config.SLIPPAGE_PCT, config.STOP_LOSS_PCT)
        if result is None or (result["last_day"] - result["first_day"]).days < 365:
            print(f"\n⚠️  {symbol}: less than 1 year of prices in this period. Skipped.")
            continue
        m = measure(result)
        print_report(result, m)
        results.append(result)
        measurements.append(m)

    if not results:
        print("\n❌ Nothing to report.")
        sys.exit(1)

    print_summary(measurements)
    save_files(results, measurements)
    print(f"\nSaved: {RESULTS_DIR}/backtest_summary.csv, backtest_trades.csv, backtest_equity.csv, backtest_chart.png")


if __name__ == "__main__":
    main()
