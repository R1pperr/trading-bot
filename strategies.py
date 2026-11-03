"""
strategies.py  -  the rules of the sleeves, as small functions.

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


def internal_bar_strength(high, low, close):
    """IBS: 0 means the close was the day's low, 1 means it was the day's high.
    A day with no range has no reading."""
    high, low, close = (pd.Series(x, dtype=float).to_numpy() for x in (high, low, close))
    span = high - low
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(span > 0, (close - low) / span, np.nan)


def rotation_score(close, lookbacks=None):
    """The average of the 1, 3, 6 and 12-month returns. Dividends are in the prices already."""
    close = pd.Series(close, dtype=float)
    returns = [close / close.shift(days) - 1 for days in lookbacks or config.ROTATION_LOOKBACK_DAYS]
    return pd.concat(returns, axis=1).mean(axis=1, skipna=False).to_numpy()


def volatility(close, days):
    """How much the daily return swings, over the last so many days. Not annualized."""
    return pd.Series(close, dtype=float).pct_change().rolling(days).std().to_numpy()


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
        "ibs": internal_bar_strength(table["high"], table["low"], close),
        "rotation": rotation_score(close),
        "vol": volatility(close, config.VOL_SIZING_DAYS),
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


def ibs_entry(ibs_value, below=None):
    """Buy a weak close: the day ended in the bottom fifth of its range."""
    return ibs_value < (config.IBS_ENTRY_BELOW if below is None else below)


def ibs_exit(ibs_value, days_held, above=None, max_days=None):
    """Sell on a strong close, or after 5 trading days, whichever comes first."""
    return (ibs_value > (config.IBS_EXIT_ABOVE if above is None else above)) | (
        days_held >= (config.IBS_MAX_HOLD_DAYS if max_days is None else max_days))


def trading_day_of_month(calendar, index):
    """1 for the month's first trading day. calendar is the exchange's days in order, not calendar days."""
    day = pd.Timestamp(calendar[index])
    count = 1
    while index - count >= 0 and pd.Timestamp(calendar[index - count]).month == day.month:
        count += 1
    return count


def is_last_trading_day(calendar, index):
    """A day with no known day after it can't be called the last one."""
    return index + 1 < len(calendar) and pd.Timestamp(calendar[index + 1]).month != pd.Timestamp(calendar[index]).month


def turn_of_month_order(calendar, index):
    """What the turn-of-the-month sleeve does at this day's open: "buy", "sell" or nothing."""
    if is_last_trading_day(calendar, index):
        return "buy"
    return "sell" if trading_day_of_month(calendar, index) == config.TURN_OF_MONTH_SELL_DAY else ""


def rotation_picks(symbols, scores, closes, trend_averages, count=None):
    """The top symbols by score, keeping only those above their 200-day average.
    A top symbol below its average leaves its slot empty, and the next in line doesn't take it."""
    ranked = sorted(((score, i) for i, score in enumerate(scores) if np.isfinite(score)),
                    key=lambda pair: (-pair[0], symbols[pair[1]]))
    top = [i for _, i in ranked[:count or config.ROTATION_TOP_N]]
    return [symbols[i] for i in top if closes[i] > trend_averages[i]]


def vol_scale(daily_returns, target=None):
    """Barroso & Santa-Clara: target volatility over annualized volatility, capped at 1.0, so no leverage.
    With too little history there is no reading, so exposure stays at 1.0."""
    returns = pd.Series(daily_returns, dtype=float).dropna().tail(config.MOMENTUM_VOL_DAYS)
    if len(returns) < config.MOMENTUM_VOL_DAYS:
        return 1.0
    annual = float(returns.std()) * 252 ** 0.5
    if not annual > 0:
        return 1.0
    return min(1.0, (config.MOMENTUM_VOL_TARGET if target is None else target) / annual)


def split_by_horizon(enabled):
    """Each horizon's money, split equally among its enabled sleeves."""
    shares = {}
    for horizon, money in config.HORIZONS.items():
        mates = [name for name in enabled if config.SLEEVES.get(name, {}).get("horizon") == horizon]
        shares.update({name: money / len(mates) for name in mates})
    return shares


def intraday_stop_fill(bars, stop, mode):
    """Where a stop fills during one day, from that day's 15-minute bars in time order.
    bars: rows of (open, high, low, close). Returns the fill price, or None if it didn't fill.
    "alpaca": a stop order fills at the stop when a bar trades through it, or at the bar's open
    if the price gapped below it. "check": the first 15-minute bar closing below the stop."""
    for bar_open, _, bar_low, bar_close in bars:
        if mode == "alpaca" and bar_low <= stop:
            return min(bar_open, stop)
        if mode == "check" and bar_close < stop:
            return bar_close
    return None


def ab_winner(default, challenger):
    """The fixed A/B rule, on out-of-sample results only. Each side needs "sharpe" and "max_drawdown".
    The challenger replaces the default only if its Sharpe is at least 0.10 higher AND its max
    drawdown is no more than 2 percentage points worse. Otherwise the default stays."""
    sharpe_gain = challenger["sharpe"] - default["sharpe"]
    drawdown_loss = default["max_drawdown"] - challenger["max_drawdown"]     # drawdowns are negative numbers
    tiny = 1e-12                                                             # exact ties in decimal count as met
    if not (np.isfinite(sharpe_gain) and np.isfinite(drawdown_loss)):
        return "default"
    if sharpe_gain >= config.AB_MIN_SHARPE_GAIN - tiny and drawdown_loss <= config.AB_MAX_DRAWDOWN_LOSS + tiny:
        return "challenger"
    return "default"
