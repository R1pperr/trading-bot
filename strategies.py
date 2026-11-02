"""
strategies.py  -  the rules of the four sleeves, as small functions.

Both the live bot (sleeves.py) and the backtest (sleeve_backtest.py) use this
file, so what is tested is what would trade. The numbers are the published
standard ones in config.py. They were not tuned to make the backtest look good.

Nothing here talks to Alpaca or a file. The rule functions work on one number or
on whole columns of numbers, so they can be checked with small hand-made prices.
"""

import numpy as np
import pandas as pd

import config


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def wilder_average(values, period):
    """Wilder's smoothing: starts as a plain average, then each day adds 1/period of the new value."""
    values = np.asarray(values, dtype=float)
    result = np.full(len(values), np.nan)
    start = 0
    while start < len(values) and np.isnan(values[start]):
        start += 1
    first = start + period
    if first > len(values):
        return result
    average = float(np.mean(values[start:first]))
    result[first - 1] = average
    for i in range(first, len(values)):
        if not np.isnan(values[i]):
            average = (average * (period - 1) + values[i]) / period
        result[i] = average
    return result


def rsi(close, period=None):
    """Wilder's RSI: 0 means every recent day fell, 100 means every recent day rose."""
    period = period or config.RSI_DAYS
    change = pd.Series(close, dtype=float).diff().to_numpy()
    gains = np.where(np.isnan(change), np.nan, np.clip(change, 0, None))
    losses = np.where(np.isnan(change), np.nan, np.clip(-change, 0, None))
    average_gain = wilder_average(gains, period)
    average_loss = wilder_average(losses, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        strength = average_gain / average_loss
        result = 100 - 100 / (1 + strength)
    result = np.where(average_loss == 0, 100.0, result)
    return np.where(np.isnan(average_gain), np.nan, result)


def atr(high, low, close, period=None):
    """Average True Range: how far the price typically moves in a day."""
    period = period or config.ATR_DAYS
    high, low, close = (pd.Series(x, dtype=float) for x in (high, low, close))
    previous = close.shift(1)
    true_range = pd.concat([high - low, (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1)
    true_range = true_range.where(high.notna())
    return wilder_average(true_range.to_numpy(), period)


def trend_score(close):
    """
    The crypto trend signal: the average of three EMA crossovers, each divided by
    recent volatility so that wild and calm coins are measured the same way.
    Above zero means the trend is up.
    """
    close = pd.Series(close, dtype=float)
    daily_move = close.diff().rolling(config.CRYPTO_TREND_VOL_DAYS).std().replace(0, np.nan)
    scores = [(close.ewm(span=fast, adjust=False, min_periods=slow).mean()
               - close.ewm(span=slow, adjust=False, min_periods=slow).mean()) / daily_move
              for fast, slow in config.CRYPTO_TREND_EMA_PAIRS]
    return pd.concat(scores, axis=1).mean(axis=1, skipna=False).to_numpy()


def momentum(close):
    """12-1 momentum: the return over the past 12 months, leaving out the most recent month."""
    close = pd.Series(close, dtype=float)
    return (close.shift(config.MOMENTUM_SKIP_DAYS) / close.shift(config.MOMENTUM_LOOKBACK_DAYS) - 1).to_numpy()


def indicators(table):
    """Every number the sleeves need, one row per day, from a table of daily prices."""
    close = table["close"]
    return pd.DataFrame({
        "close": close,
        "rsi": rsi(close),
        "sma_exit": close.rolling(config.RSI_EXIT_SMA_DAYS).mean(),
        "sma_trend": close.rolling(config.TREND_FILTER_SMA_DAYS).mean(),
        "atr": atr(table["high"], table["low"], close),
        "trend": trend_score(close),
        "momentum": momentum(close),
    }, index=table.index)


# ---------------------------------------------------------------------------
# Rules. Each works on single numbers or whole columns.
# ---------------------------------------------------------------------------
def above_trend(close, sma_trend):
    return close > sma_trend


def rsi2_entry(close, sma_trend, rsi_value):
    """Buy a pullback: above the 200-day average, with a 2-day RSI under 10."""
    return (close > sma_trend) & (rsi_value < config.RSI_ENTRY_BELOW)


def rsi2_exit(close, sma_exit, sma_trend, days_held):
    """Sell on a close above the 5-day average, below the 200-day average, or after 10 days."""
    return (close > sma_exit) | (close < sma_trend) | (days_held >= config.RSI_MAX_HOLD_DAYS)


def trend_long(score):
    return score > 0


def top_momentum(symbols, scores, count=None):
    """The best symbols by 12-1 momentum, best first. Symbols without a score are left out."""
    ranked = sorted(((score, symbol) for symbol, score in zip(symbols, scores) if np.isfinite(score)),
                    key=lambda pair: (-pair[0], pair[1]))
    return [symbol for _, symbol in ranked[:count or config.MOMENTUM_TOP_N]]
