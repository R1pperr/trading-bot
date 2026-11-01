"""
risk.py  -  the four risk rules, as small functions the bot must pass.

  1. Position size : never more than MAX_POSITION_PCT of equity in one position.
  2. Stop-loss     : sell any position that has fallen STOP_LOSS_PCT below its
                     buy price. Checked on every run, for every position.
  3. Trade limit   : at most MAX_TRADES_PER_DAY orders per day.
  4. Kill switch   : if equity has fallen KILL_SWITCH_PCT since the end of the
                     previous trading day, stop trading for the rest of the day.

Two deliberate exceptions keep you safer, not riskier:
  * Stop-loss sells always go through, even after the trade limit or the
    kill switch has stopped everything else. (They still count as trades.)
  * The AI analyst is never asked about stop-loss sells and never sees or
    sets the position size.

None of these functions talks to Alpaca; they just do the maths, so you can
read and test them on their own.
"""

import math

import config


def max_shares_to_buy(equity, price, already_held_value=0.0, max_pct=None):
    """
    Whole number of shares we may buy so the position stays within the cap.
    Example: equity $100,000, cap 5% -> $5,000; at $680 a share -> 7 shares.
    """
    max_pct = config.MAX_POSITION_PCT if max_pct is None else max_pct
    if price <= 0 or equity <= 0:
        return 0
    room = equity * max_pct - already_held_value
    return max(0, math.floor(room / price))


def max_crypto_qty(equity, price, max_pct=None):
    """
    The same position cap as max_shares_to_buy, for crypto, where parts of a coin
    can be bought. Rounded down to 8 decimal places, so it stays within the cap.
    Example: equity $100,000, cap 5% -> $5,000; at $78,000 a coin -> 0.06410256.
    """
    max_pct = config.MAX_POSITION_PCT if max_pct is None else max_pct
    if price <= 0 or equity <= 0:
        return 0.0
    return math.floor(equity * max_pct / price * 100_000_000) / 100_000_000


def stop_price(entry_price, stop_pct=None):
    stop_pct = config.STOP_LOSS_PCT if stop_pct is None else stop_pct
    return entry_price * (1 - stop_pct)


def stop_loss_hit(entry_price, current_price, stop_pct=None):
    """True if the price has fallen to or below the stop level."""
    if entry_price <= 0 or current_price <= 0:
        return False
    return current_price <= stop_price(entry_price, stop_pct)


def daily_change(equity, start_of_day_equity):
    """Equity change since the previous trading day's close, e.g. -0.031 = -3.1%."""
    if start_of_day_equity <= 0:
        return 0.0
    return equity / start_of_day_equity - 1


def kill_switch_triggered(equity, start_of_day_equity, limit_pct=None):
    """True if equity has fallen by the limit (3%) or more today."""
    limit_pct = config.KILL_SWITCH_PCT if limit_pct is None else limit_pct
    return daily_change(equity, start_of_day_equity) <= -limit_pct


def trades_left(trades_today, max_trades=None):
    max_trades = config.MAX_TRADES_PER_DAY if max_trades is None else max_trades
    return max(0, max_trades - trades_today)
