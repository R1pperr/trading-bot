"""
strategy.py  -  the buy/sell rule: a moving-average crossover.

In plain English:
  * Every day, average the last 20 closing prices (the "fast" average) and the
    last 50 closing prices (the "slow" average).
  * BUY  on the day the fast average crosses ABOVE the slow one
         (recent prices have climbed above the longer trend).
  * SELL on the day the fast average crosses BELOW the slow one
         (recent prices have dropped below the longer trend).
  * Every other day: HOLD (do nothing).

Signals are calculated from a day's CLOSING price, so they are acted on at
the NEXT day's opening price. The backtest and the bot both use this file,
so what you test is exactly what you trade.
"""

import pandas as pd

import config

BUY = "BUY"
SELL = "SELL"
HOLD = "HOLD"


def generate_signals(prices, fast_days=None, slow_days=None):
    """
    Take a table of daily prices (needs a 'close' column) and return a copy
    with these extra columns:
      sma_fast, sma_slow : the two moving averages
      trend              : 'UP' if fast > slow, 'DOWN' if not, empty during warm-up
      signal             : 'BUY', 'SELL' or 'HOLD'
      reason             : a one-line explanation of the signal
    """
    fast_days = fast_days or config.FAST_SMA_DAYS
    slow_days = slow_days or config.SLOW_SMA_DAYS
    if fast_days >= slow_days:
        raise ValueError("FAST_SMA_DAYS must be smaller than SLOW_SMA_DAYS")

    table = prices.copy()
    table["sma_fast"] = table["close"].rolling(fast_days).mean()
    table["sma_slow"] = table["close"].rolling(slow_days).mean()

    ready = table["sma_slow"].notna()          # enough history for both averages
    above = table["sma_fast"] > table["sma_slow"]
    was_above = above.shift(1, fill_value=False)
    was_ready = ready.shift(1, fill_value=False)

    crossed_up = ready & was_ready & above & ~was_above
    crossed_down = ready & was_ready & ~above & was_above

    table["trend"] = ""
    table.loc[ready & above, "trend"] = "UP"
    table.loc[ready & ~above, "trend"] = "DOWN"

    table["signal"] = HOLD
    table.loc[crossed_up, "signal"] = BUY
    table.loc[crossed_down, "signal"] = SELL

    table["reason"] = [
        _explain(row.signal, row.trend, row.sma_fast, row.sma_slow, fast_days, slow_days)
        for row in table.itertuples()
    ]
    return table


def _explain(signal, trend, fast, slow, fast_days, slow_days):
    if pd.isna(slow):
        return f"Not enough history yet (needs {slow_days} trading days)"
    numbers = f"{fast_days}-day avg {fast:.2f} vs {slow_days}-day avg {slow:.2f}"
    if signal == BUY:
        return f"{fast_days}-day average crossed ABOVE the {slow_days}-day average ({numbers})"
    if signal == SELL:
        return f"{fast_days}-day average crossed BELOW the {slow_days}-day average ({numbers})"
    if trend == "UP":
        return f"No crossover today; trend is up ({numbers})"
    return f"No crossover today; trend is down ({numbers})"


def latest_signal(prices, fast_days=None, slow_days=None):
    """
    Signal for the most recent finished trading day (used by the bot).
    Returns a dictionary with date, close, sma_fast, sma_slow, trend, signal, reason.
    """
    table = generate_signals(prices, fast_days, slow_days)
    last = table.iloc[-1]
    return {
        "date": table.index[-1].date().isoformat(),
        "close": float(last["close"]),
        "sma_fast": float(last["sma_fast"]),
        "sma_slow": float(last["sma_slow"]),
        "trend": last["trend"],
        "signal": last["signal"],
        "reason": last["reason"],
    }
