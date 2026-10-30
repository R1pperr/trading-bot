"""
data.py  -  downloads daily price bars from Alpaca's free market data.

A "daily bar" is one row per trading day: open, high, low, close, volume.

Free-plan rules this file respects:
  * Full-market ("SIP") history is free, except the most recent 15 minutes.
    We only ask for days that have completely finished, so that never matters.
  * If Alpaca still refuses SIP data, we fall back to the IEX feed, which is
    always free but only includes trades made on the IEX exchange.
Prices are adjusted for stock splits and dividends, so a 4-for-1 split does
not look like a 75% crash.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

import config

NEW_YORK = ZoneInfo("America/New_York")


def last_complete_day_end():
    """
    Midnight in New York today (US market time), so we only receive trading
    days that have fully finished, and never anything from the last 20 minutes.
    """
    now = datetime.now(timezone.utc)
    midnight_new_york = datetime.now(NEW_YORK).replace(hour=0, minute=0, second=0, microsecond=0)
    return min(midnight_new_york.astimezone(timezone.utc), now - timedelta(minutes=20))


def _to_utc(when):
    """Accept '2020-01-01', a date or a datetime; return a UTC datetime."""
    if isinstance(when, str):
        when = datetime.fromisoformat(when)
    if not isinstance(when, datetime):  # a plain date
        when = datetime(when.year, when.month, when.day)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _download(client, symbols, start, end, feed):
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        adjustment=Adjustment.ALL,
        feed=feed,
    )
    return client.get_stock_bars(request).df


def get_daily_bars(symbols, start, end=None):
    """
    Download daily bars for one or more symbols.

    Returns a dictionary: {"SPY": table, "AAPL": table, ...}
    Each table has one row per trading day (dates as the index) and the
    columns open, high, low, close, volume. Symbols with no data are left out.
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    start = _to_utc(start)
    end = min(_to_utc(end), last_complete_day_end()) if end else last_complete_day_end()

    api_key, secret_key = config.get_alpaca_keys()
    client = StockHistoricalDataClient(api_key, secret_key)

    try:
        raw = _download(client, symbols, start, end, DataFeed.SIP)
    except Exception as error:
        if "subscription" not in str(error).lower():
            raise
        print("⚠️  Alpaca refused full-market (SIP) data on your plan; using the free IEX feed.")
        raw = _download(client, symbols, start, end, DataFeed.IEX)

    tables = {}
    if raw is None or raw.empty:
        return tables

    for symbol in symbols:
        if "symbol" in raw.index.names:
            if symbol not in raw.index.get_level_values("symbol"):
                continue
            table = raw.xs(symbol, level="symbol").copy()
        else:
            table = raw.copy()

        # Alpaca stamps each daily bar at midnight New York time. Keep just the date.
        stamps = pd.DatetimeIndex(table.index)
        if stamps.tz is not None:
            stamps = stamps.tz_convert(NEW_YORK).tz_localize(None)
        table.index = stamps.normalize()
        table.index.name = "date"

        table = table[["open", "high", "low", "close", "volume"]].astype(float)
        tables[symbol] = table.sort_index()

    return tables
