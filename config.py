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
# file, because it never closes. False stops all crypto activity, and the code stays.
CRYPTO_ENABLED = False
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
MAX_TRADES_PER_DAY = 5      # at most 5 orders per day
KILL_SWITCH_PCT = 0.03      # stop trading for the day if equity falls 3%
# At 5% each, 18 positions is 90% of the account. The other 10% stays as cash,
# because crypto can't be bought on margin.
MAX_OPEN_POSITIONS = {"stock": 12, "crypto": 6}


# ---------------------------------------------------------------------------
# Strategy sleeves (see sleeves.py). Nothing here trades until you approve it.
# ---------------------------------------------------------------------------
STRATEGY_MODE = "sleeves"          # "sleeves", or "crossover" for the old strategy
ENABLE_RSI2_STOCKS = True          # short-term: RSI(2) pullback on stocks
ENABLE_IBS_ETFS = True             # short-term: IBS mean reversion on index and sector funds
ENABLE_TURN_OF_MONTH = True        # short-term: hold IVV over the turn of the month
ENABLE_CRYPTO_TREND = False        # short-term: trend following on crypto
ENABLE_ROTATION = True             # medium-term: asset-class rotation
ENABLE_MOMENTUM_STOCKS = True      # long-term: 12-1 momentum on stocks
ENABLE_CRYPTO_HOLD = False         # long-term: BTC and ETH above their 200-day average

# Each horizon is split equally among its enabled sleeves. Unused money stays in cash.
SHORT_ALLOCATION = 0.60            # holds of 1 to 5 trading days
MEDIUM_ALLOCATION = 0.20           # weeks to months
LONG_ALLOCATION = 0.20             # months
HORIZONS = {"SHORT": SHORT_ALLOCATION, "MEDIUM": MEDIUM_ALLOCATION, "LONG": LONG_ALLOCATION}
SLEEVES = {                        # listed in priority order for the daily entry cap
    "turn_of_month": {"horizon": "SHORT", "market": "stock"},      # one trade a month, so it goes first
    "ibs_etfs": {"horizon": "SHORT", "market": "stock"},
    "rsi2_stocks": {"horizon": "SHORT", "market": "stock"},
    "crypto_trend": {"horizon": "SHORT", "market": "crypto"},
    "rotation": {"horizon": "MEDIUM", "market": "stock"},
    "momentum_stocks": {"horizon": "LONG", "market": "stock"},
    "crypto_hold": {"horizon": "LONG", "market": "crypto"},
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

# IBS mean reversion (Pagonidis 2013): where the day's close sits in the day's range.
IBS_SYMBOLS = UNIVERSE_ALWAYS_INCLUDE
IBS_ENTRY_BELOW = 0.2
IBS_EXIT_ABOVE = 0.8
IBS_MAX_HOLD_DAYS = 5

# Turn of the month: buy at the open of the month's last trading day, sell at the open of
# the new month's 4th. IVV trades only here, so no other sleeve may hold it.
TURN_OF_MONTH_SYMBOL = "IVV"
TURN_OF_MONTH_SELL_DAY = 4
ONLY_FOR_TURN_OF_MONTH = [TURN_OF_MONTH_SYMBOL]

# Asset-class rotation (Faber 2010). Ranked on the first trading day of each month.
ROTATION_SYMBOLS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "DBC", "GLD", "IEF", "TLT"]
ROTATION_TOP_N = 3
ROTATION_LOOKBACK_DAYS = [21, 63, 126, 252]     # 1, 3, 6 and 12 months

# Volatility-scaled momentum (Barroso & Santa-Clara 2015). Never above 1.0, so no leverage.
MOMENTUM_VOL_TARGET = 0.12
MOMENTUM_VOL_DAYS = 126

# Volatility-based position size: the inverse of each symbol's 20-day volatility.
VOL_SIZING_DAYS = 20

# The three choices the A/B test decides. quant_lab.py reports the winners, and they are
# written here by hand. The challenger needs a clear win, see strategies.ab_winner.
STOP_EXECUTION = "alpaca"          # "alpaca": a stop order waits at Alpaca. "check": the 15-minute check
VOL_SCALED_MOMENTUM = False        # False: plain 12-1 momentum. True: volatility-scaled
VOL_SIZING = False                 # False: the current sizing. True: inverse-volatility sizing
AB_MIN_SHARPE_GAIN = 0.10          # the challenger's out-of-sample Sharpe must beat this much
AB_MAX_DRAWDOWN_LOSS = 0.02        # and its max drawdown may be at most this much worse

FRACTIONAL_SHARES = True           # buy parts of a share where Alpaca allows it
PRE_OPEN_MINUTES = 30              # the day's orders queue this close to the open, to fill at it


# ---------------------------------------------------------------------------
# Optional AI analyst. Set USE_AI_ANALYST to False to stop asking it.
# ---------------------------------------------------------------------------
USE_AI_ANALYST = True
# "shadow": Gemini logs its decision and reason but can't block a trade.
# "enforce": a veto blocks the trade.
AI_VETO_MODE = "shadow"
GEMINI_MODEL = "gemini-3.8-flash"
# Only the top candidates are looked at each day, sells first and then the most
# traded buys. That caps Gemini at 10 calls a day. Google doesn't publish the free
# limit: check aistudio.google.com/rate-limit before raising these.
MAX_AI_REVIEWS_PER_DAY = {"stock": 5, "crypto": 5}


# ---------------------------------------------------------------------------
# Phone alerts (see notifier.py)
# ---------------------------------------------------------------------------
DAILY_SUMMARY_NY = "16:05"    # New York time, after the 16:00 close all year. The first weekday run from then sends it


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
    require_paper_key(api_key)
    return api_key, secret_key


def require_paper_key(api_key):
    """Alpaca's live keys start with AK and its paper keys with PK. A live key stops the bot here."""
    if api_key.startswith("AK"):
        print("❌ This is a LIVE Alpaca key. This bot is paper only, so it will not run.")
        sys.exit(1)


def require_paper_account(trading, account):
    """Stop unless the client talks to Alpaca's paper server and the account is a paper one."""
    url = str(getattr(getattr(trading, "_base_url", None), "value", getattr(trading, "_base_url", "")))
    number = str(getattr(account, "account_number", ""))
    if "paper-api" not in url or not number.startswith("PA"):
        print("❌ This isn't an Alpaca paper account. This bot is paper only, so it will not run.")
        sys.exit(1)
