"""
fixes.py  -  the fix menu, and the switches that turn each fix on or off per strategy.

Each strategy can have a few standard fixes switched on. They all ship OFF. The daily review
(review.py) may switch one on after it passes every test, by writing docs/data/control.json.
Nothing here edits code, and no fix can loosen a locked safety rule:

  * the 5% cap per position, the 7% stop-loss (2% for the real-time engine), the daily trade cap,
    the kill switch and the position limit stay exactly as they are or get tighter.

Every fix uses one standard setting, written below. They are never fine-tuned.

The live bot (sleeves.py, stops.py), the backtest (sleeve_backtest.py) and the real-time engine
(realtime/src) all follow these same rules, so what was tested is what trades.
"""

import json
import math
import os

import numpy as np

import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONTROL_FILE = os.path.join(BASE_DIR, "docs", "data", "control.json")

STRATEGIES = ("turn_of_month", "ibs_etfs", "rsi2_stocks", "rotation", "momentum_stocks", "realtime")
BOT1_STRATEGIES = STRATEGIES[:-1]

# Every switch, OFF.
OFF = {"trend_filter": "off", "vol_filter": False, "time_filter": 0, "gap_filter": False, "liquidity_filter": False,
       "confirmation": False, "time_exit": False, "trailing_stop": False, "tight_stop": False, "vol_sizing": False}

# The standard settings. Never fine-tuned.
TREND_DAYS = 200                    # SPY above or below its 200-day average
VOL_DAYS = 20                       # 20-day volatility of daily closes
VOL_LOOKBACK = 252                  # compared with the last year
VOL_EXTREME = 0.20                  # top or bottom 20% of that year
GAP_LIMIT = 0.02                    # an overnight gap bigger than 2%
MIN_DOLLAR_VOLUME = 20_000_000      # main bot: skip stocks trading under $20M a day (20-day average)
STOP = {"main": config.STOP_LOSS_PCT, "realtime": 0.02}          # the locked stops
TIGHT_STOP = {"main": 0.05, "realtime": 0.01}                    # the tighter stop
TIME_EXIT_SHARE = 0.5               # the earlier time exit: half the normal holding limit

# The menu: each item is one change, the problem it answers, and where it applies.
MENU = {
    "trend_above": {"set": {"trend_filter": "above"}, "label": "Trend filter: only when SPY is above its 200-day average",
                    "problem": "trend_down"},
    "trend_below": {"set": {"trend_filter": "below"}, "label": "Trend filter: only when SPY is below its 200-day average",
                    "problem": "trend_up"},
    "vol_filter": {"set": {"vol_filter": True}, "label": "Volatility filter: skip the top and bottom 20% of the year",
                   "problem": "vol_extreme"},
    "time_15": {"set": {"time_filter": 15}, "label": "Time-of-day filter: no entries in the first 15 minutes",
                "problem": "early_entries_15", "only": ("realtime",)},
    "time_30": {"set": {"time_filter": 30}, "label": "Time-of-day filter: no entries in the first 30 minutes",
                "problem": "early_entries_30", "only": ("realtime",)},
    "gap_filter": {"set": {"gap_filter": True}, "label": "Gap filter: skip entries after an overnight gap over 2%",
                   "problem": "gaps"},
    "liquidity_filter": {"set": {"liquidity_filter": True}, "label": "Liquidity filter: skip thin volume or wide spreads",
                         "problem": "thin"},
    "confirmation": {"set": {"confirmation": True}, "label": "Confirmation: wait one more bar before acting",
                     "problem": "whipsaw", "only": ("realtime",)},
    "time_exit": {"set": {"time_exit": True}, "label": "Earlier time exit", "problem": "long_holds",
                  "only": ("ibs_etfs", "rsi2_stocks", "realtime")},
    "trailing_stop": {"set": {"trailing_stop": True}, "label": "Trailing stop", "problem": "giveback"},
    "tight_stop": {"set": {"tight_stop": True}, "label": "Tighter stop", "problem": "full_stops"},
    "vol_sizing": {"set": {"vol_sizing": True}, "label": "Volatility-based sizing, inside the size cap",
                   "problem": "volatile_names"},
}

# Problems that don't fit a menu fix. The review opens a GitHub issue for these instead.
OFF_MENU = {"weekday": "Losses bunch up on one day of the week",
            "symbol": "Losses bunch up in one symbol"}


def applies(fix_id, strategy):
    only = MENU[fix_id].get("only")
    return only is None or strategy in only


# ---------------------------------------------------------------------------
# control.json: the live switches. Written only by the daily review.
# ---------------------------------------------------------------------------
def default_control():
    """With no control file yet, the main bot's AI follows config.AI_VETO_MODE."""
    return {"updated_utc": "", "ai_mode": {"main": config.AI_VETO_MODE, "realtime": "enforce"},
            "ai_record": {"main": "", "realtime": ""},
            "fixes": {name: dict(OFF) for name in STRATEGIES},
            "shadow": {name: None for name in STRATEGIES},
            "allocations": {}, "paused": []}


def clean_fixset(raw):
    """Unknown keys are dropped, and a bad value falls back to OFF, so a broken file can't do harm."""
    result = dict(OFF)
    for key, value in (raw or {}).items():
        if key not in OFF:
            continue
        if key == "trend_filter" and value in ("off", "above", "below"):
            result[key] = value
        elif key == "time_filter" and value in (0, 15, 30):
            result[key] = value
        elif key not in ("trend_filter", "time_filter") and isinstance(value, bool):
            result[key] = value
    return result


def load_control(path=None):
    control = default_control()
    try:
        with open(path or CONTROL_FILE, encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        return control
    for bot in ("main", "realtime"):
        if saved.get("ai_mode", {}).get(bot) in ("enforce", "shadow"):
            control["ai_mode"][bot] = saved["ai_mode"][bot]
        record = saved.get("ai_record", {}).get(bot)
        if isinstance(record, str):
            control["ai_record"][bot] = record[:2000]
    for name in STRATEGIES:
        control["fixes"][name] = clean_fixset(saved.get("fixes", {}).get(name))
        shadow = saved.get("shadow", {}).get(name)
        control["shadow"][name] = clean_fixset(shadow) if shadow else None
    shares = saved.get("allocations", {})
    control["allocations"] = {k: float(v) for k, v in shares.items()
                              if k in BOT1_STRATEGIES and isinstance(v, (int, float)) and 0 <= v <= 1}
    control["paused"] = [name for name in saved.get("paused", []) if name in BOT1_STRATEGIES]
    control["updated_utc"] = str(saved.get("updated_utc", ""))
    return control


def save_control(control, path=None):
    path = path or CONTROL_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(control, handle, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(path + ".tmp", path)


def active(strategy, control=None):
    return (control or load_control())["fixes"].get(strategy, dict(OFF))


def is_off(fixset):
    return clean_fixset(fixset) == OFF


def with_fix(fix_id, base=None):
    """The strategy's switches with one more fix on."""
    result = dict(base or OFF)
    result.update(MENU[fix_id]["set"])
    return result


# ---------------------------------------------------------------------------
# The rules themselves, as plain functions
# ---------------------------------------------------------------------------
def vol_rank(closes):
    """Where today's 20-day volatility sits in the last year: 0 = calmest day, 1 = wildest."""
    closes = np.asarray(closes, dtype=float)
    if len(closes) < VOL_DAYS + 2:
        return None
    returns = np.diff(closes) / closes[:-1]
    windows = np.lib.stride_tricks.sliding_window_view(returns, VOL_DAYS)
    vols = windows.std(axis=1, ddof=1)[-VOL_LOOKBACK:]
    vols = vols[np.isfinite(vols)]
    if len(vols) < 60:              # under three months of history says too little
        return None
    return float((vols < vols[-1]).mean())


def entry_block(fixset, spy_above_trend=None, vol_rank_value=None, gap=None, dollar_volume=None):
    """The reason a fix blocks this entry, or "" if none does. Missing data never blocks."""
    trend = fixset.get("trend_filter", "off")
    if trend == "above" and spy_above_trend is False:
        return "trend filter: SPY is below its 200-day average"
    if trend == "below" and spy_above_trend is True:
        return "trend filter: SPY is above its 200-day average"
    if fixset.get("vol_filter") and vol_rank_value is not None and (
            vol_rank_value >= 1 - VOL_EXTREME or vol_rank_value < VOL_EXTREME):
        return "volatility filter: volatility is in the top or bottom 20% of the year"
    if fixset.get("gap_filter") and gap is not None and abs(gap) > GAP_LIMIT:
        return f"gap filter: overnight gap over {GAP_LIMIT:.0%}"
    if fixset.get("liquidity_filter") and dollar_volume is not None and dollar_volume < MIN_DOLLAR_VOLUME:
        return "liquidity filter: under $20M traded a day"
    return ""


def stop_level(fixset, entry, highest=None, bot="main"):
    """Where the stop sits. A fix can only move it closer to the price, never further away."""
    locked = entry * (1 - STOP[bot])
    pct = TIGHT_STOP[bot] if fixset.get("tight_stop") else STOP[bot]
    level = entry * (1 - pct)
    if fixset.get("trailing_stop") and highest and highest > 0:
        level = max(level, highest * (1 - pct))
    return max(level, locked)


def max_hold(fixset, normal_days):
    """The earlier time exit halves the holding limit, rounded up, and never adds days."""
    if fixset.get("time_exit"):
        return max(1, math.ceil(normal_days * TIME_EXIT_SHARE))
    return normal_days


def dollar_volume(table, days=20):
    """Average dollars traded a day over the last 20 days, or None if the table has no volume."""
    if table is None or "volume" not in getattr(table, "columns", ()) or len(table) < days:
        return None
    tail = table.tail(days)
    return float((tail["close"] * tail["volume"]).mean())
