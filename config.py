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
# The bot trades the long lists in universe/stocks.json and universe/crypto.json,
# which the weekly workflow rebuilds. These two short lists are only a backup, used
# if a list file is missing or damaged, and the small list for the old backtest.
SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "JPM"]

# Crypto pairs, written the way Alpaca orders them. See the crypto backtest with
# python backtest.py --crypto. Crypto needs the every-day schedule in the workflow
# file, because it never closes. Set this to False to stop all crypto activity.
CRYPTO_ENABLED = True
CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD"]


# ---------------------------------------------------------------------------
# Universe: how the weekly refresh builds the long lists (see universe.py)
# ---------------------------------------------------------------------------
STOCK_UNIVERSE_SIZE = 500          # keep this many stocks, the most traded first
UNIVERSE_EXCHANGES = ["NYSE", "NASDAQ", "ARCA", "NYSEARCA"]   # OTC is left out
UNIVERSE_MIN_PRICE = 5.0           # leave out anything under $5
UNIVERSE_LOOKBACK_DAYS = 20        # average dollar volume over this many trading days
UNIVERSE_MIN_DAYS = 15             # needs prices on at least this many of those days
UNIVERSE_MIN_STOCKS = 100          # a shorter list than this means the refresh went wrong
UNIVERSE_MIN_CRYPTO = 2            # the same check for crypto
# Always kept, because the RSI(2) evidence is strongest on index and sector funds.
UNIVERSE_ALWAYS_INCLUDE = ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV", "XLI",
                           "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
# Names matching any of these are left out. Leveraged and inverse funds move 2 or 3
# times as fast as the market, so a 7% stop-loss wouldn't mean what it means elsewhere.
UNIVERSE_EXCLUDED_NAME_PATTERNS = [
    r"\b[2-5]X\b", r"\bPROSHARES (ULTRA|SHORT)", r"\bDIREXION DAILY\b", r"\bDAILY TARGET\b",
    r"\bINVERSE\b", r"\bBULL\b", r"\bBEAR\b", r"\bLEVERAGED?\b", r"\bWARRANTS?\b", r"\bRIGHTS?\b",
]

# Crypto: every tradable USD pair except these coins and anything too thinly traded.
CRYPTO_STABLECOINS = ["USDT", "USDC", "USDG", "DAI", "PYUSD", "TUSD", "FDUSD", "USDP", "USD1", "GUSD"]
CRYPTO_PEGGED_COINS = ["PAXG", "XAUT"]    # these follow the price of gold
# Average dollars traded per day on Alpaca. Checked against Alpaca's crypto feed: BTC/USD
# trades about $228,000 a day there, and the thinnest pairs under $100, so a $5,000
# order in those wouldn't be realistic even though a paper fill would hide that.
CRYPTO_MIN_DAILY_DOLLAR_VOLUME = 1_000

# Alpaca allows 200 market data calls a minute. Stay well under it.
ALPACA_BATCH_SIZE = 500            # symbols per request, never one call per symbol
ALPACA_CALLS_PER_MINUTE = 100      # how fast batches may be sent
ALPACA_RETRIES = 4                 # tries per batch, waiting longer after each error


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
CRYPTO_LIMIT_FEE_PCT = 0.0015     # the lower fee when a limit order rests on the book


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
# At 5% each, 18 positions is 90% of the account. The other 10% stays as cash,
# because crypto can't be bought on margin.
MAX_OPEN_POSITIONS = {"stock": 12, "crypto": 6}


# ---------------------------------------------------------------------------
# Strategy sleeves (see sleeves.py). Nothing here trades until you approve it.
# ---------------------------------------------------------------------------
STRATEGY_MODE = "sleeves"          # "sleeves", or "crossover" for the old strategy
ENABLE_RSI2_STOCKS = True          # short-term: RSI(2) pullback on stocks
ENABLE_CRYPTO_TREND = False        # short-term: trend following on crypto
ENABLE_MOMENTUM_STOCKS = True      # long-term: 12-1 momentum on stocks
ENABLE_CRYPTO_HOLD = False         # long-term: BTC and ETH above their 200-day average

SHORT_TERM_ALLOCATION = 0.80       # share of equity for the two short-term sleeves
LONG_TERM_ALLOCATION = 0.20        # share of equity for the two long-term sleeves
SLEEVES = {                        # listed in priority order for the daily entry cap
    "rsi2_stocks": {"horizon": "SHORT", "market": "stock", "allocation": 0.50},
    "crypto_trend": {"horizon": "SHORT", "market": "crypto", "allocation": 0.30},
    "momentum_stocks": {"horizon": "LONG", "market": "stock", "allocation": 0.15},
    "crypto_hold": {"horizon": "LONG", "market": "crypto", "allocation": 0.05},
}
SLEEVE_MAX_OPEN_POSITIONS = {"stock": 20, "crypto": 10}   # used only in sleeves mode
RISK_PER_TRADE = 0.005             # hitting a stop loses at most 0.5% of equity
ATR_DAYS = 14
ATR_STOP_MULTIPLE = 3              # stops sit 3 x ATR from entry; crypto stops trail
USE_ATR_STOPS = False              # False keeps the fixed STOP_LOSS_PCT. Approve before changing
RSI_DAYS = 2
RSI_ENTRY_BELOW = 10
RSI_EXIT_SMA_DAYS = 5
RSI_MAX_HOLD_DAYS = 10
TREND_FILTER_SMA_DAYS = 200
CRYPTO_TREND_TOP_N = 8
CRYPTO_TREND_EMA_PAIRS = [(8, 24), (16, 48), (32, 96)]
CRYPTO_TREND_VOL_DAYS = 63
CRYPTO_HOLD_SYMBOLS = ["BTC/USD", "ETH/USD"]
MOMENTUM_TOP_N = 10
MOMENTUM_LOOKBACK_DAYS = 252       # 12 months
MOMENTUM_SKIP_DAYS = 21            # leave out the most recent month
MOMENTUM_MARKET_SYMBOL = "SPY"


# ---------------------------------------------------------------------------
# Optional AI analyst (Stage 3). OFF unless you change this to True.
# ---------------------------------------------------------------------------
USE_AI_ANALYST = False
GEMINI_MODEL = "gemini-3.8-flash"
# Only the top candidates are looked at each day, sells first and then the most
# traded buys. That caps Gemini at 10 calls a day. Google doesn't publish the free
# limit: check aistudio.google.com/rate-limit before raising these.
MAX_AI_REVIEWS_PER_DAY = {"stock": 5, "crypto": 5}


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
