"""
universe.py  -  the long lists of stocks and crypto pairs the bot may trade.

The weekly workflow runs this file. It reads Alpaca's list of assets, filters it
with the settings in config.py, ranks what is left by average daily dollar volume,
and saves universe/stocks.json and universe/crypto.json.

    Stocks: active and tradable on NYSE, NASDAQ or NYSE Arca, $5 or more, no
            leveraged or inverse funds. Keeps the top STOCK_UNIVERSE_SIZE.
    Crypto: every tradable USD pair, minus stablecoins, gold-pegged coins and
            pairs too thinly traded for a realistic order.

If a refresh fails, the old file stays as it is and a Telegram alert goes out.
If a file is ever missing, the bot falls back to the short lists in config.py.

These files are public, so the liquidity figure is a rounded average. No price
history goes in them, because Alpaca doesn't allow republishing its data.

    python universe.py      rebuild both lists (reads data only, places nothing)
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

import config
import data
import notifier

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOLDER = os.path.join(BASE_DIR, "universe")
FILES = {"stock": "stocks.json", "crypto": "crypto.json"}
LABELS = {"stock": "[STOCK]", "crypto": "[CRYPTO]"}


# ---------------------------------------------------------------------------
# Reading the saved lists (used by the bot on every run)
# ---------------------------------------------------------------------------
def load(market):
    """The saved file, or None if it is missing or damaged."""
    try:
        with open(os.path.join(FOLDER, FILES[market]), encoding="utf-8") as handle:
            saved = json.load(handle)
        return saved if saved.get("symbols") else None
    except (OSError, ValueError, AttributeError):
        return None


def rows(market):
    """Everything the bot may trade, the most traded first."""
    saved = load(market)
    if saved:
        return saved["symbols"]
    backup = config.SYMBOLS if market == "stock" else config.CRYPTO_SYMBOLS
    return [{"symbol": symbol, "name": ""} for symbol in backup]


def info(market):
    """The size and date for the dashboard."""
    saved = load(market)
    if not saved:
        return {"size": len(rows(market)), "refreshed": "", "feed": ""}
    return {"size": len(saved["symbols"]), "refreshed": saved.get("refreshed", ""),
            "feed": saved.get("feed", "")}


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
def fetch_assets(asset_class):
    """
    Alpaca's asset list, as plain rows. The library's normal version crashes on
    exchanges it doesn't know yet, such as TXSE, so this asks for the raw rows.
    """
    api_key, secret_key = config.get_alpaca_keys()
    trading = TradingClient(api_key, secret_key, paper=True, raw_data=True)   # PAPER ONLY, reads only
    return trading.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=asset_class))


def excluded_name(name):
    name = (name or "").upper()
    return any(re.search(pattern, name) for pattern in config.UNIVERSE_EXCLUDED_NAME_PATTERNS)


def usable_stocks(assets):
    """Active, tradable and on an allowed exchange. OTC is never on that list."""
    return [asset for asset in assets
            if asset.get("tradable") and asset.get("status") == "active"
            and asset.get("exchange") in config.UNIVERSE_EXCHANGES
            and not excluded_name(asset.get("name"))]


def usable_crypto(assets):
    """Tradable USD pairs, without stablecoins or gold-pegged coins."""
    left_out = set(config.CRYPTO_STABLECOINS) | set(config.CRYPTO_PEGGED_COINS)
    return [asset for asset in assets
            if asset.get("tradable") and asset.get("status") == "active"
            and asset.get("symbol", "").endswith("/USD")
            and asset["symbol"].split("/")[0] not in left_out]


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def dollar_volume(table):
    """Average dollars traded per day over the last UNIVERSE_LOOKBACK_DAYS days."""
    recent = table.tail(config.UNIVERSE_LOOKBACK_DAYS)
    return float((recent["close"] * recent["volume"]).mean())


def rounded(value):
    """Two significant figures: enough to rank by eye, without publishing exact data."""
    return float(f"{value:.2g}")


def to_rows(ranked, names):
    return [{"symbol": symbol, "name": names.get(symbol, ""), "avg_dollar_volume": rounded(value)}
            for value, symbol in ranked]


def rank_stocks(assets, bars):
    """
    The most traded stocks first. IEX is only a small slice of the market, so the
    ranking compares symbols with each other and uses no fixed volume threshold.
    """
    ranked = []
    for symbol, table in bars.items():
        recent = table.tail(config.UNIVERSE_LOOKBACK_DAYS)
        if len(recent) < config.UNIVERSE_MIN_DAYS:
            continue
        if float(recent["close"].iloc[-1]) < config.UNIVERSE_MIN_PRICE:
            continue
        ranked.append((dollar_volume(table), symbol))
    ranked.sort(reverse=True)
    names = {asset["symbol"]: asset.get("name", "") for asset in assets}
    return to_rows(ranked[:config.STOCK_UNIVERSE_SIZE], names)


def rank_crypto(assets, bars):
    """The most traded pairs first, leaving out pairs under the volume floor."""
    ranked = []
    for symbol, table in bars.items():
        if len(table) < config.UNIVERSE_MIN_DAYS:
            continue
        value = dollar_volume(table)
        if value >= config.CRYPTO_MIN_DAILY_DOLLAR_VOLUME:
            ranked.append((value, symbol))
    ranked.sort(reverse=True)
    names = {asset["symbol"]: asset.get("name", "") for asset in assets}
    return to_rows(ranked, names)


# ---------------------------------------------------------------------------
# The weekly refresh
# ---------------------------------------------------------------------------
def build(market):
    """Make a fresh list. Raises an error rather than return a list that looks wrong."""
    start = datetime.now(timezone.utc) - timedelta(days=config.UNIVERSE_LOOKBACK_DAYS * 2)
    if market == "stock":
        assets = usable_stocks(fetch_assets(AssetClass.US_EQUITY))
        bars = data.get_daily_bars([asset["symbol"] for asset in assets], start)
        symbols = rank_stocks(assets, bars)
        smallest, feed = config.UNIVERSE_MIN_STOCKS, data.LAST_STOCK_FEED
    else:
        assets = usable_crypto(fetch_assets(AssetClass.CRYPTO))
        bars = data.get_crypto_daily_bars([asset["symbol"] for asset in assets], start)
        symbols = rank_crypto(assets, bars)
        smallest, feed = config.UNIVERSE_MIN_CRYPTO, "crypto"
    if len(symbols) < smallest:
        raise RuntimeError(f"only {len(symbols)} symbols passed, expected at least {smallest}")
    return {"refreshed": datetime.now(timezone.utc).date().isoformat(), "feed": feed,
            "ranked_by": f"average daily dollar volume, last {config.UNIVERSE_LOOKBACK_DAYS} days",
            "symbols": symbols}


def save(market, payload):
    """Write the new list in one step, so a crash can't leave half a file."""
    os.makedirs(FOLDER, exist_ok=True)
    path = os.path.join(FOLDER, FILES[market])
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    os.replace(path + ".tmp", path)


def refresh():
    """Rebuild both lists. A list that fails keeps its last good version."""
    exit_code = 0
    for market in FILES:
        try:
            payload = build(market)
            save(market, payload)
            print(f"✅ {market}: {len(payload['symbols'])} symbols saved")
        except (Exception, SystemExit) as error:
            exit_code = 1
            problem = notifier.redact(" ".join(str(error).split()))[:200]
            print(f"❌ {market}: refresh failed, keeping the last good list: {problem}")
            notifier.send(f"{LABELS[market]} ❌ Universe refresh failed. The bot keeps using the "
                          f"last good list.\n{problem}")
    return exit_code


if __name__ == "__main__":
    sys.exit(refresh())
