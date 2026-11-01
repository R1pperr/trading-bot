"""
config.py  -  every setting for the backtest and the bot, in one place.

Change numbers HERE, not inside the other files. Percentages are written as
decimals: 0.05 means 5%.
"""

import os
import sys

from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# What to trade
# ---------------------------------------------------------------------------
# US stock and ETF tickers. SPY and QQQ are funds that follow the S&P 500 and
# Nasdaq-100 indexes; the rest are single large companies. Edit freely.
SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "JPM"]

# Crypto pairs, written the way Alpaca orders them. See the crypto backtest with
# python backtest.py --crypto. Crypto needs the every-day schedule in the workflow
# file, because it never closes. Set this to False to stop all crypto activity.
CRYPTO_ENABLED = True
CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD"]


# ---------------------------------------------------------------------------
# Strategy: moving-average crossover (see strategy.py)
# ---------------------------------------------------------------------------
FAST_SMA_DAYS = 20   # short average: follows recent prices closely
SLOW_SMA_DAYS = 50   # long average: shows the bigger trend


# ---------------------------------------------------------------------------
# Trading costs used by the backtest
# ---------------------------------------------------------------------------
# Alpaca advertises commission-free US stock trading, but sells pay small
# regulatory fees. 0.05% per trade is deliberately pessimistic so the test
# does not flatter the strategy.
FEE_PCT = 0.0005
# Slippage: you rarely get exactly the price you saw. Every simulated buy is
# filled 0.05% higher and every sell 0.05% lower than the market price.
SLIPPAGE_PCT = 0.0005
# Crypto backtest only. Alpaca's crypto fees start at 0.25% for orders that fill
# straight away, which is much more than stocks, so using FEE_PCT would flatter it.
CRYPTO_FEE_PCT = 0.0025


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------
BACKTEST_START = "2020-01-01"   # Alpaca's free history starts in 2016
BACKTEST_CASH = 10_000          # pretend dollars per symbol tested


# ---------------------------------------------------------------------------
# Risk rules
# The bot enforces all four (Stage 3). The backtest also uses the stop-loss.
# ---------------------------------------------------------------------------
MAX_POSITION_PCT = 0.05     # never put more than 5% of equity in one position
STOP_LOSS_PCT = 0.07        # sell if a position falls 7% below the buy price
MAX_TRADES_PER_DAY = 3      # at most 3 orders per day
KILL_SWITCH_PCT = 0.03      # stop trading for the day if equity falls 3%


# ---------------------------------------------------------------------------
# Optional AI analyst (Stage 3). OFF unless you change this to True.
# ---------------------------------------------------------------------------
USE_AI_ANALYST = False
GEMINI_MODEL = "gemini-3.8-flash"


# ---------------------------------------------------------------------------
# Phone alerts (see notifier.py)
# ---------------------------------------------------------------------------
DAILY_SUMMARY_UTC = "20:45"   # the first weekday run from this time on sends the day's summary


# ---------------------------------------------------------------------------
# Keys (read from the .env file, never written in code)
# ---------------------------------------------------------------------------
def get_alpaca_keys():
    """Return (api_key, secret_key) from .env, or stop with a clear message."""
    load_dotenv()
    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key or "paste" in api_key or "paste" in secret_key:
        print("❌ Could not find your Alpaca keys in the .env file.")
        print("   Run this from inside your trading-bot folder, and check that .env contains")
        print("   ALPACA_API_KEY=... and ALPACA_SECRET_KEY=... (no spaces, no quotes).")
        sys.exit(1)
    return api_key, secret_key
