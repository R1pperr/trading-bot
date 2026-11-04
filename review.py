"""
review.py  -  the daily review, after the US close. Runs in GitHub Actions (review.yml).

    python review.py              the full review
    python review.py --no-send    the same, without Telegram or GitHub issues (for a local look)

For each strategy (the five sleeves and the real-time engine) it:
  1. grades every finished trade in plain English and tags its conditions (grading.py), and
     works out what vetoed and skipped trades would have done (shadow trades);
  2. shows where it wins and loses, split by those conditions, for today and the last 20 and
     60 trading days, and lists its top 3 repeated problems, explained by Gemini;
  3. runs the fix pipeline (below), moves money between a horizon's sleeves, and lets the AI
     analyst learn from its record;
  4. writes the Review and Fixes pages (docs/data/review.json, fixes.json) and sends a short
     Telegram summary. Bugs go to GitHub issues, with the evidence.

The fix pipeline. A fix goes live only if ALL of these pass:
  1. the problem shows in at least 10 live or shadow trades;
  2. the fix helps on older data (from before live trading began): a walk-forward test, year by
     year, with costs and next-open fills. Higher Sharpe, max drawdown no worse, and better in
     at least 60% of the years;
  3. it runs in shadow next to the live version for at least 20 trading days and does better
     after costs.
Then it switches on, you get a Telegram message, the old version keeps running in shadow, and
it rolls back by itself if over the next 20 trading days it does worse than the old version or
hits its drawdown limit. One fix under test per strategy. A rejected fix waits 60 trading days.

It may change only data files (control.json and the state files): never code, and never a
locked safety rule. It reads Alpaca prices and both paper accounts, and never places an order.
"""

import argparse
import csv
import json
import math
import os
import pickle
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

import ai_analyst
import config
import fixes
import grading
import notifier

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "docs", "data")
REVIEW_FILE = os.path.join(DATA_DIR, "review.json")
FIXES_LOG = os.path.join(DATA_DIR, "fixes.json")
GRADES_FILE = os.path.join(DATA_DIR, "grades.json")
PIPE_FILE = os.path.join(BASE_DIR, "state", "fixes_state.json")
CACHE_DIR = os.path.join(BASE_DIR, ".cache", "review")
LOG_FILE = os.path.join(BASE_DIR, "logs", "trade_log.csv")

LIVE_START = {"main": "2026-11-03", "realtime": "2026-11-04"}     # the first live day of each bot
MIN_SHOW = 5                  # a group needs this many trades to be listed
MIN_PROBLEM = 10              # and this many before a fix may be tried
SHADOW_DAYS = 20
PROBATION_DAYS = 20
COOLDOWN_DAYS = 60
MAX_SHIFT = 0.05              # money moves at most 5 percentage points per strategy per day
HELP_SHARE = 0.60
AI_VETOES_NEEDED = 20
AI_RECORD_SIZE = 50
STRATEGY_NAMES = {"turn_of_month": "Turn of the month", "ibs_etfs": "IBS funds", "rsi2_stocks": "RSI(2) stocks",
                  "rotation": "Asset rotation", "momentum_stocks": "12-1 momentum", "realtime": "Real-time engine"}
TAG_TITLES = {"trend": "Market trend", "vol": "Volatility", "time": "Time of day", "gap": "Overnight gap",
              "weekday": "Day of week", "symbol": "Symbol", "spread": "Spread", "volume": "Volume"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def read_json(path, missing):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return missing


def write_json(path, value, pretty=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=1 if pretty else None, separators=None if pretty else (",", ":"), default=str)
        handle.write("\n")
    os.replace(path + ".tmp", path)


def read_log():
    try:
        with open(LOG_FILE, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


def add_trading_days(day, count):
    return str(np.busday_offset(np.datetime64(day), count, roll="forward"))


def trading_days_since(day, today):
    return int(np.busday_count(np.datetime64(day), np.datetime64(today)))


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return None if not math.isfinite(value) else round(float(value), 5)
    if isinstance(value, np.integer):
        return int(value)
    return value


# ---------------------------------------------------------------------------
# Numbers for a group of trades
# ---------------------------------------------------------------------------
def stats(trades):
    """Results after costs. The Sharpe here is per trade (average over spread), a rough guide."""
    if not trades:
        return {"trades": 0, "win_rate": None, "avg_ret": None, "total_pnl": 0.0, "sharpe": None}
    rets = np.array([t["ret"] for t in trades], dtype=float) - 2 * grading.COST_PER_SIDE * np.array(
        [t.get("kind") == "live" for t in trades], dtype=float)
    spread = rets.std(ddof=1) if len(rets) > 1 else 0.0
    return {"trades": len(trades), "win_rate": float(np.mean([t["win"] for t in trades])),
            "avg_ret": float(rets.mean()), "total_pnl": float(sum(t.get("pnl", 0.0) for t in trades)),
            "sharpe": float(rets.mean() / spread * math.sqrt(len(rets))) if spread > 0 else None}


def splits(trades):
    """Where it wins and where it loses, by each condition."""
    result = {}
    for tag in TAG_TITLES:
        groups = {}
        for trade in trades:
            groups.setdefault(trade["tags"].get(tag, "unknown"), []).append(trade)
        rows = [{"bucket": bucket, **stats(rows)} for bucket, rows in groups.items()]
        result[tag] = sorted(rows, key=lambda row: row["total_pnl"])
    return result


def window(trades, days, today):
    first = add_trading_days(today, -days) if days else today
    return [t for t in trades if t["exit_day"] > first or (days == 0 and t["exit_day"] == today)]


# ---------------------------------------------------------------------------
# Problems: groups that lose, matched to the fix that answers them
# ---------------------------------------------------------------------------
def losing_group(trades, test, label, problem_id, others_must_win=True):
    inside = [t for t in trades if test(t)]
    outside = [t for t in trades if not test(t)]
    if len(inside) < MIN_SHOW:
        return None
    mine, rest = stats(inside), stats(outside)
    if mine["avg_ret"] is None or mine["avg_ret"] >= 0:
        return None
    if others_must_win and rest["trades"] >= 3 and rest["avg_ret"] is not None and rest["avg_ret"] <= mine["avg_ret"]:
        return None                       # it loses everywhere, so this condition isn't the cause
    return {"id": problem_id, "label": label, "trades": mine["trades"], "avg_ret": mine["avg_ret"],
            "total_pnl": mine["total_pnl"], "win_rate": mine["win_rate"], "others_avg_ret": rest["avg_ret"]}


def find_problems(strategy, trades):
    realtime = strategy == "realtime"
    found = []
    checks = [
        (lambda t: t["tags"].get("trend") == "SPY below its 200-day average", "Loses when SPY is below its 200-day average", "trend_down"),
        (lambda t: t["tags"].get("trend") == "SPY above its 200-day average", "Loses when SPY is above its 200-day average", "trend_up"),
        (lambda t: t["tags"].get("vol") in ("high", "low"), "Loses when volatility is unusually high or low", "vol_extreme"),
        (lambda t: t["tags"].get("gap") == "big (over 2%)", "Loses after overnight gaps over 2%", "gaps"),
        (lambda t: t["tags"].get("volume") == "thin" or t["tags"].get("spread") == "wide", "Loses on thin volume or wide spreads", "thin"),
        (lambda t: (t.get("best") or 0) > (0.005 if realtime else 0.03) and not t["win"], "Winners turn into losers (gives gains back)", "giveback"),
        (lambda t: t.get("stop"), "Losing trades run all the way to the stop-loss", "full_stops"),
        (lambda t: t["tags"].get("vol") == "high", "Losses bunch up in the most volatile names", "volatile_names"),
    ]
    if realtime:
        checks += [
            (lambda t: t["tags"].get("time") == "first 15 minutes", "Loses on entries in the first 15 minutes", "early_entries_15"),
            (lambda t: t["tags"].get("time") in ("first 15 minutes", "15 to 30 minutes in"), "Loses on entries in the first 30 minutes", "early_entries_30"),
            (lambda t: t.get("minutes") is not None and t["minutes"] <= 15, "Whipsaws: crossovers reverse within 3 bars", "whipsaw"),
            (lambda t: t.get("minutes") is not None and t["minutes"] >= 120, "Long holds into the afternoon lose", "long_holds"),
        ]
    else:
        normal = {"rsi2_stocks": config.RSI_MAX_HOLD_DAYS, "ibs_etfs": config.IBS_MAX_HOLD_DAYS}.get(strategy)
        if normal:
            checks.append((lambda t: t.get("days", 0) > math.ceil(normal / 2), "Trades held past half the time limit lose", "long_holds"))
    for test, label, problem_id in checks:
        group = losing_group(trades, test, label, problem_id, others_must_win=problem_id not in ("full_stops",))
        if group:
            found.append(group)
    overall = stats(trades)
    for tag, off_id in (("weekday", "weekday"), ("symbol", "symbol")):
        buckets = {}
        for trade in trades:
            buckets.setdefault(trade["tags"].get(tag, "unknown"), []).append(trade)
        for bucket, rows in buckets.items():
            numbers = stats(rows)
            if numbers["trades"] >= MIN_PROBLEM and numbers["avg_ret"] < 0 <= (overall["avg_ret"] or 0):
                found.append({"id": off_id, "label": f"{fixes.OFF_MENU[off_id]}: {bucket}", "trades": numbers["trades"],
                              "avg_ret": numbers["avg_ret"], "total_pnl": numbers["total_pnl"],
                              "win_rate": numbers["win_rate"], "others_avg_ret": overall["avg_ret"]})
    for problem in found:
        problem["fix"] = next((fix_id for fix_id, item in fixes.MENU.items()
                               if item["problem"] == problem["id"] and fixes.applies(fix_id, strategy)), None)
    return sorted(found, key=lambda p: p["total_pnl"])


# ---------------------------------------------------------------------------
# Gemini writes the plain-English explanation (free tier, one call a day)
# ---------------------------------------------------------------------------
def explain(facts):
    fallback = facts["fallback"]
    try:
        from google.genai import types
        if not os.getenv("GEMINI_API_KEY", "").strip():
            return fallback
        prompt = ("You review a student's paper-trading bots. In plain English for a beginner, at most 6 short sentences, "
                  "explain where each strategy is winning and losing and what its top problems mean. Don't invent numbers; "
                  "use only these facts. Say clearly when there are too few trades to tell.\n\n" + json.dumps(facts["data"]))
        response, _ = ai_analyst.generate(prompt, types.GenerateContentConfig(temperature=0))
        text = " ".join((response.text or "").split())
        return text[:1500] or fallback
    except Exception as error:
        print("Gemini explanation not available:", notifier.redact(error))
        return fallback


# ---------------------------------------------------------------------------
# Tests for a fix: older data (walk-forward) and the shadow window
# ---------------------------------------------------------------------------
def yearly_sharpe(equity):
    result = {}
    for year, part in equity.groupby(equity.index.year):
        returns = part.pct_change().dropna()
        if len(returns) > 20 and returns.std() > 0:
            result[int(year)] = float(returns.mean() / returns.std() * math.sqrt(252))
    return result


def curve_numbers(equity):
    returns = equity.pct_change().dropna()
    sharpe = float(returns.mean() / returns.std() * math.sqrt(252)) if len(returns) > 1 and returns.std() > 0 else float("nan")
    drawdown = float((equity / equity.cummax() - 1).min()) if len(equity) else float("nan")
    return sharpe, drawdown, float(equity.iloc[-1] / equity.iloc[0] - 1) if len(equity) else float("nan")


def judge(base_equity, fixed_equity):
    """The rule for older data: higher Sharpe, drawdown no worse, better in at least 60% of years."""
    base_sharpe, base_dd, _ = curve_numbers(base_equity)
    fixed_sharpe, fixed_dd, _ = curve_numbers(fixed_equity)
    base_years, fixed_years = yearly_sharpe(base_equity), yearly_sharpe(fixed_equity)
    years = sorted(set(base_years) & set(fixed_years))
    helped = sum(fixed_years[y] > base_years[y] for y in years)
    share = helped / len(years) if years else 0.0
    passed = (math.isfinite(fixed_sharpe) and math.isfinite(base_sharpe) and fixed_sharpe > base_sharpe
              and fixed_dd >= base_dd and share >= HELP_SHARE)
    return {"passed": bool(passed), "sharpe_base": base_sharpe, "sharpe_fixed": fixed_sharpe, "dd_base": base_dd,
            "dd_fixed": fixed_dd, "years": len(years), "years_helped": helped, "share_helped": share}


def main_bot_history():
    """The saved older prices (before live trading), the same copy the backtest report used."""
    import quant_lab
    import sleeve_backtest as backtest
    tables, funds, extras = quant_lab.load_prices(False)
    return backtest.build_panel(tables, funds, extras)


def walk_forward_main(strategy, fixset_new, fixset_old, panel=None):
    import sleeve_backtest as backtest
    import strategies
    panel = panel if panel is not None else main_bot_history()
    cut = pd.Timestamp(LIVE_START["main"])
    share = strategies.split_by_horizon(fixes.BOT1_STRATEGIES)[strategy]

    def run(fixset):
        options = backtest.Options((strategy,), atr_stops=False, allocations={strategy: share},
                                   fixes={strategy: fixset}, stop_bars=None)
        equity = backtest.simulate(panel, options)["equity"]
        return equity[equity.index < cut]
    return judge(run(fixset_old), run(fixset_new))


def engine_history(until):
    """SPY 5-minute bars on the free IEX feed, from July 2020 (the start of Alpaca's IEX history)."""
    import rt_backtest
    from alpaca.data.enums import DataFeed
    from data import get_intraday_bars
    path = os.path.join(CACHE_DIR, "spy_iex_5min.pkl")
    if os.path.exists(path):
        with open(path, "rb") as handle:
            table = pickle.load(handle)
    else:
        table = get_intraday_bars("SPY", datetime(2020, 7, 1), datetime.fromisoformat(until), 5, DataFeed.IEX)
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "wb") as handle:
            pickle.dump(table, handle)
    return rt_backtest.days_from(table[table.index < pd.Timestamp(until)])


def engine_daily(since="2019-06-01"):
    """What the engine's fixes need from daily bars (trend, volatility rank, yesterday's close), per day."""
    import rt_backtest
    from data import get_daily_bars
    closes = get_daily_bars(["SPY"], datetime.fromisoformat(since))["SPY"]["close"]
    closes.index = pd.DatetimeIndex(closes.index).tz_localize(None).normalize()
    return rt_backtest.daily_contexts(closes)


def walk_forward_engine(fixset_new, fixset_old, days=None, daily=None):
    import rt_backtest
    days = days if days is not None else engine_history(LIVE_START["realtime"])
    daily = daily if daily is not None else engine_daily()
    base = rt_backtest.simulate(days, fixes=fixset_old, daily=daily)["equity"]
    fixed = rt_backtest.simulate(days, fixes=fixset_new, daily=daily)["equity"]
    return judge(base, fixed)


def compare_recent(strategy, fixset_new, fixset_old, since, panel=None, days=None):
    """The shadow (or probation) window: both versions played on the same recent prices, after costs."""
    if strategy == "realtime":
        import rt_backtest
        days = days if days is not None else recent_engine_days(since)
        daily = engine_daily(str((pd.Timestamp(since) - pd.Timedelta(days=420)).date()))
        new = rt_backtest.simulate(days, fixes=fixset_new, daily=daily)["equity"]
        old = rt_backtest.simulate(days, fixes=fixset_old, daily=daily)["equity"]
    else:
        import sleeve_backtest as backtest
        import strategies
        panel = panel if panel is not None else recent_panel(strategy)
        share = strategies.split_by_horizon(fixes.BOT1_STRATEGIES)[strategy]
        result = {}
        for name, fixset in (("new", fixset_new), ("old", fixset_old)):
            options = backtest.Options((strategy,), atr_stops=False, allocations={strategy: share},
                                       fixes={strategy: fixset}, stop_bars=None, start=since)
            equity = backtest.simulate(panel, options)["equity"]
            result[name] = equity[equity.index >= pd.Timestamp(since)]
        new, old = result["new"], result["old"]
    new_sharpe, new_dd, new_ret = curve_numbers(new)
    old_sharpe, old_dd, old_ret = curve_numbers(old)
    return {"return_new": new_ret, "return_old": old_ret, "dd_new": new_dd, "dd_old": old_dd,
            "better": bool(math.isfinite(new_ret) and math.isfinite(old_ret) and new_ret > old_ret)}


def recent_panel(strategy):
    import sleeve_backtest as backtest
    import universe
    from data import get_daily_bars
    symbols = {config.MOMENTUM_MARKET_SYMBOL}
    if strategy in ("rsi2_stocks", "momentum_stocks"):
        symbols |= {row["symbol"] for row in universe.rows("stock")}
    symbols |= set({"ibs_etfs": config.IBS_SYMBOLS, "rotation": config.ROTATION_SYMBOLS,
                    "turn_of_month": [config.TURN_OF_MONTH_SYMBOL]}.get(strategy, []))
    tables = get_daily_bars(sorted(symbols), datetime.now(timezone.utc) - timedelta(days=500))
    return backtest.build_panel(tables)


def recent_engine_days(since):
    import rt_backtest
    from alpaca.data.enums import DataFeed
    from data import get_intraday_bars
    start = datetime.fromisoformat(since) - timedelta(days=5)
    table = get_intraday_bars("SPY", start, datetime.now(timezone.utc).replace(tzinfo=None), 5, DataFeed.IEX)
    days = rt_backtest.days_from(table)
    keep = [i for i, (day, _) in enumerate(days) if day >= pd.Timestamp(since)]
    return days[max(0, keep[0] - 1):] if keep else []


def drawdown_limit(strategy):
    """The strategy's worst out-of-sample drawdown in the backtest report: a fix that does worse rolls back."""
    report = read_json(os.path.join(DATA_DIR, "backtest.json"), {})
    if strategy == "realtime":
        value = (report.get("realtime", {}).get("default", {}).get("out_of_sample", {}) or {}).get("max_drawdown")
    else:
        row = next((r for r in report.get("rows", []) if r.get("key") == strategy), {})
        value = (row.get("out_of_sample") or {}).get("max_drawdown")
    return float(value) if isinstance(value, (int, float)) else -0.10


# ---------------------------------------------------------------------------
# The fix pipeline, one strategy at a time
# ---------------------------------------------------------------------------
def step_pipeline(strategy, problems, control, pipe, today, log, say, tests):
    """Move this strategy's fix one step. tests = {"walk": fn(strategy, new, old), "recent": fn(strategy, new, old, since)}."""
    entry = pipe.setdefault(strategy, {"stage": "idle"})
    cooldown = pipe.setdefault("cooldown", {}).setdefault(strategy, {})
    live = control["fixes"][strategy]
    name = STRATEGY_NAMES[strategy]

    def record(fix_id, stage, result, note):
        log.append(clean({"day": today, "strategy": strategy, "fix": fix_id, "label": fixes.MENU[fix_id]["label"],
                          "stage": stage, "note": note, "result": result}))

    if entry["stage"] == "idle":
        for problem in problems:
            fix_id = problem.get("fix")
            if not fix_id or problem["trades"] < MIN_PROBLEM:
                continue
            if fixes.with_fix(fix_id, live) == live:
                continue                                   # already on
            if cooldown.get(fix_id, "") > today:
                continue                                   # rejected lately: waits 60 trading days
            new = fixes.with_fix(fix_id, live)
            result = tests["walk"](strategy, new, live)
            if result["passed"]:
                pipe[strategy] = {"stage": "shadow", "fix": fix_id, "since": today, "problem": problem["label"]}
                control["shadow"][strategy] = new
                record(fix_id, "shadow", result, f"Passed the older-data test, now running in shadow for {SHADOW_DAYS} trading days")
            else:
                cooldown[fix_id] = add_trading_days(today, COOLDOWN_DAYS)
                record(fix_id, "rejected", result, "Didn't help on older data, so it isn't used. It can be retried in 60 trading days")
            return                                         # one fix under test per strategy at a time
        return

    fix_id = entry["fix"]
    if entry["stage"] == "shadow":
        if trading_days_since(entry["since"], today) < SHADOW_DAYS:
            return
        new = control["shadow"][strategy] or fixes.with_fix(fix_id, live)
        result = tests["recent"](strategy, new, live, entry["since"])
        if result["better"]:
            control["shadow"][strategy] = live              # the old version keeps running in shadow
            control["fixes"][strategy] = new
            pipe[strategy] = {"stage": "probation", "fix": fix_id, "since": today, "old": live, "problem": entry.get("problem", "")}
            record(fix_id, "live", result, f"Did better in shadow after costs, so it's switched on. It rolls back if it does worse over {PROBATION_DAYS} trading days")
            say(f"🔧 {name}: what was wrong: {entry.get('problem', 'a repeated loss')}. What changed: {fixes.MENU[fix_id]['label']} is now on. "
                f"It did better in shadow ({result['return_new']:+.2%} against {result['return_old']:+.2%}). The old version keeps running in shadow.")
        else:
            control["shadow"][strategy] = None
            cooldown[fix_id] = add_trading_days(today, COOLDOWN_DAYS)
            pipe[strategy] = {"stage": "idle"}
            record(fix_id, "rejected", result, "Didn't do better in shadow, so it isn't used. It can be retried in 60 trading days")
        return

    if entry["stage"] == "probation":
        old = entry.get("old") or dict(fixes.OFF)
        result = tests["recent"](strategy, live, old, entry["since"])
        limit = drawdown_limit(strategy)
        hit_limit = result["dd_new"] is not None and math.isfinite(result["dd_new"]) and result["dd_new"] < limit
        done = trading_days_since(entry["since"], today) >= PROBATION_DAYS
        if hit_limit or (done and not result["better"]):
            control["fixes"][strategy] = old
            control["shadow"][strategy] = None
            cooldown[fix_id] = add_trading_days(today, COOLDOWN_DAYS)
            pipe[strategy] = {"stage": "idle"}
            why = "it hit its drawdown limit" if hit_limit else "it did worse than the old version"
            record(fix_id, "rolled back", result, f"Rolled back because {why}")
            say(f"↩️ {name}: {fixes.MENU[fix_id]['label']} was switched off again because {why}. The old version is back.")
        elif done:
            control["shadow"][strategy] = None
            pipe[strategy] = {"stage": "idle"}
            record(fix_id, "kept", result, f"Kept: it did at least as well as the old version for {PROBATION_DAYS} trading days")


# ---------------------------------------------------------------------------
# Money follows what works
# ---------------------------------------------------------------------------
def rebalance(control, live_by_strategy, shadow_by_strategy, ranges, today):
    """Within each horizon, nudge money toward the sleeves doing best over the last 60 trading days,
    at most 5 points a day each. A sleeve below its backtest's worst range drops to 0% and runs in shadow."""
    import sleeves
    import strategies
    notes = []
    enabled = [s for s in fixes.BOT1_STRATEGIES if sleeves.is_enabled(s)]
    base = strategies.split_by_horizon(enabled)
    paused = set(control["paused"])
    for sleeve in enabled:
        recent = window(live_by_strategy.get(sleeve, []), 60, today)[-20:]
        floor = (ranges.get(sleeve) or {}).get("expectancy_low")
        if floor is None or len(recent) < 20:
            continue
        average = float(np.mean([t["ret"] for t in recent]))
        if sleeve not in paused and average < floor:
            paused.add(sleeve)
            notes.append(f"{STRATEGY_NAMES[sleeve]} paused: its last 20 trades averaged {average:+.2%}, below its backtest's worst range ({floor:+.2%}). It runs in shadow until it recovers.")
    for sleeve in list(paused):
        shadow = window(shadow_by_strategy.get(sleeve, []), 60, today)[-20:]
        floor = (ranges.get(sleeve) or {}).get("expectancy_low")
        if floor is not None and len(shadow) >= 20 and float(np.mean([t["ret"] for t in shadow])) >= floor:
            paused.discard(sleeve)
            notes.append(f"{STRATEGY_NAMES[sleeve]} back on: its shadow trades recovered.")
    shares = dict(control["allocations"])
    was_paused = set(control["paused"])
    for horizon in config.HORIZONS:
        members = [s for s in enabled if config.SLEEVES[s]["horizon"] == horizon]
        total = config.HORIZONS[horizon]
        if len(members) < 2:
            for sleeve in members:
                shares.pop(sleeve, None)
            continue
        free = [s for s in members if s not in paused]
        current = {s: shares.get(s, base[s]) for s in members}
        if any(s in was_paused and s not in paused for s in members):
            current = {s: base[s] for s in members}            # a sleeve came back: start again from equal shares
        # 1. A paused sleeve drops to 0% straight away. Its money goes to the others, equally.
        moved = {s: 0.0 if s in paused else current[s] for s in members}
        freed = sum(current[s] for s in members if s in paused)
        for sleeve in free:
            moved[sleeve] += freed / len(free)
        # 2. Nudge toward the best Sharpe over 60 trading days: each move at most 5 points, and the
        #    moves cancel out, so the horizon keeps its share. Too few trades to judge: no move.
        scores = {}
        for sleeve in free:
            numbers = stats(window(live_by_strategy.get(sleeve, []), 60, today))
            scores[sleeve] = None if numbers["trades"] < MIN_PROBLEM else max(numbers["sharpe"] or 0.0, 0.0)
        if len(free) >= 2 and all(score is not None for score in scores.values()):
            weight = sum(scores.values())
            target = {s: total * scores[s] / weight if weight > 0 else total / len(free) for s in free}
            delta = {s: max(-MAX_SHIFT, min(MAX_SHIFT, target[s] - moved[s])) for s in free}
            up = sum(d for d in delta.values() if d > 0)
            down = -sum(d for d in delta.values() if d < 0)
            if up > down > 0 or (up > 0 and down == 0):
                delta = {s: d * (down / up) if d > 0 else d for s, d in delta.items()}
            elif down > up:
                delta = {s: d * (up / down) if d < 0 else d for s, d in delta.items()}
            for sleeve in free:
                moved[sleeve] += delta[sleeve]
        for sleeve in members:
            if abs(moved[sleeve] - current[sleeve]) > 1e-9:
                notes.append(f"{STRATEGY_NAMES[sleeve]}: {current[sleeve]:.0%} → {moved[sleeve]:.0%} of the account")
            shares[sleeve] = round(max(0.0, moved[sleeve]), 4)
        if free:                                    # rounding mustn't lose or add money: the horizon keeps its share
            shares[free[0]] = round(shares[free[0]] + total - sum(shares[s] for s in members), 6)
    control["allocations"] = shares
    control["paused"] = sorted(paused)
    return notes


# ---------------------------------------------------------------------------
# The AI analyst learns from its record
# ---------------------------------------------------------------------------
def ai_record(calls):
    """calls: newest last, each {"decision", "ret" or None}. A short summary for the AI's prompt."""
    recent = calls[-AI_RECORD_SIZE:]
    known = [c for c in recent if c.get("ret") is not None]
    if not known:
        return ""
    approvals = [c["ret"] for c in known if c["decision"] == "APPROVE"]
    vetoes = [c["ret"] for c in known if c["decision"] == "VETO"]
    parts = [f"of your last {len(recent)} calls, {len(known)} have results"]
    if approvals:
        parts.append(f"{len(approvals)} approvals averaged {np.mean(approvals):+.2%} a trade "
                     f"({np.mean([r > 0 for r in approvals]):.0%} won)")
    if vetoes:
        parts.append(f"the {len(vetoes)} trades you vetoed would have averaged {np.mean(vetoes):+.2%} after costs "
                     f"({'so your vetoes cost money' if np.mean(vetoes) > 0 else 'so your vetoes helped'})")
    return "; ".join(parts) + "."


def ai_learning(control, bot, calls, pipe, say):
    """After 20 vetoes: if the blocked trades would have made money on average, switch the AI to
    shadow. Switch it back on when, in shadow, its vetoes add value again."""
    control["ai_record"][bot] = ai_record(calls)
    vetoes = [c["ret"] for c in calls if c["decision"] == "VETO" and c.get("ret") is not None][-AI_VETOES_NEEDED:]
    state = pipe.setdefault("ai", {}).setdefault(bot, {"auto_shadow": False})
    label = "main bot" if bot == "main" else "real-time engine"
    if len(vetoes) < AI_VETOES_NEEDED:
        return
    average = float(np.mean(vetoes))
    if control["ai_mode"][bot] == "enforce" and average > 0:
        control["ai_mode"][bot] = "shadow"
        state["auto_shadow"] = True
        say(f"🤖 AI analyst ({label}) switched to shadow: the last {len(vetoes)} trades it blocked would have made "
            f"{average:+.2%} on average after costs. It keeps giving its opinion but can't block trades.")
    elif control["ai_mode"][bot] == "shadow" and state.get("auto_shadow") and average < 0:
        control["ai_mode"][bot] = "enforce"
        state["auto_shadow"] = False
        say(f"🤖 AI analyst ({label}) switched back on: in shadow, the trades it would have blocked averaged "
            f"{average:+.2%}, so its vetoes help again.")


# ---------------------------------------------------------------------------
# Bugs go to GitHub issues, with the evidence
# ---------------------------------------------------------------------------
def open_issue(title, body, send=True):
    """Opens an issue unless an open one already has this title. Needs GITHUB_TOKEN in Actions."""
    token, repo = os.getenv("GITHUB_TOKEN", ""), os.getenv("GITHUB_REPOSITORY", "")
    if not send or not token or not repo:
        print(f"(issue not opened here) {title}")
        return False
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "User-Agent": "tach-trading-bot-review"}
    try:
        request = urllib.request.Request(f"https://api.github.com/repos/{repo}/issues?state=open&per_page=100", headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            if any(issue.get("title") == title for issue in json.load(response)):
                return False
        data = json.dumps({"title": title, "body": notifier.redact(body)[:60000]}).encode()
        request = urllib.request.Request(f"https://api.github.com/repos/{repo}/issues", data=data, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=20):
            return True
    except Exception as error:
        print("GitHub issue not opened:", notifier.redact(error))
        return False


# ---------------------------------------------------------------------------
# Gathering the trades
# ---------------------------------------------------------------------------
def main_bot_trades(log_rows, download):
    """Graded live trades and shadow ("would have") trades of the main bot."""
    journal = read_json(os.path.join(DATA_DIR, "journal.json"), {}).get("trades", [])
    entries = read_json(os.path.join(BASE_DIR, "state", "sleeves.json"), {}).get("entries", {})
    planned = {row.get("order_id"): float(row["price"]) for row in log_rows
               if row.get("event") == "ORDER" and row.get("side") == "buy" and row.get("price")}
    wanted = {t["symbol"] for t in journal} | {r["symbol"] for r in log_rows
                                               if r.get("event") in ("AI_DECISION", "SIGNAL_SKIPPED") and r.get("symbol")}
    tables = {}
    if wanted:                                     # nothing to grade yet means nothing to download
        try:
            tables = download(sorted(wanted | {"SPY"}))
        except Exception as error:
            print("Prices for grading not downloaded, so the tags stay unknown:", notifier.redact(error))
    spy = tables.get("SPY")
    live = [grading.grade_main(t, entries.get(t.get("entry_order", "")), planned.get(t.get("entry_order")),
                               tables.get(t["symbol"]), spy) for t in journal]
    ordered = {(r["symbol"], r.get("signal_date")) for r in log_rows if r.get("event") == "ORDER" and r.get("side") == "buy"}
    shadow, calls = [], []
    for row in log_rows:
        event = row.get("event")
        sleeve = (row.get("details") or "").split(";")[0].replace("sleeve=", "") if (row.get("details") or "").startswith("sleeve=") else ""
        if event not in ("AI_DECISION", "SIGNAL_SKIPPED") or not row.get("price") or not sleeve:
            continue
        blocked = event == "SIGNAL_SKIPPED" or (
            ai_analyst.is_real_veto(row.get("ai_decision"), row.get("ai_reason", ""))
            and (row["symbol"], row.get("signal_date")) not in ordered)
        if event == "AI_DECISION" and row.get("ai_decision") in ("APPROVE", "VETO"):
            outcome = next((t for t in live if t["symbol"] == row["symbol"] and t["entry_day"] >= (row.get("signal_date") or "")), None)
            if row["ai_decision"] == "APPROVE":
                calls.append({"decision": "APPROVE", "ret": outcome["ret"] if outcome else None})
        if not blocked:
            continue
        result = grading.would_have_main(sleeve, row["symbol"], row.get("signal_date") or row["time_utc"][:10],
                                         float(row["price"]), tables.get(row["symbol"]))
        if event == "AI_DECISION" and ai_analyst.is_real_veto(row.get("ai_decision"), row.get("ai_reason", "")):
            calls.append({"decision": "VETO", "ret": None if result is None else result[1]})
        if result is None:
            continue
        exit_day, ret = result
        tags = grading.daily_tags(tables.get(row["symbol"]), spy, row.get("signal_date") or row["time_utc"][:10])
        tags.update(time="unknown", weekday=grading.WEEKDAYS[date.fromisoformat(row["time_utc"][:10]).weekday()],
                    symbol=row["symbol"], spread="unknown")
        why = "AI veto" if event == "AI_DECISION" else (row.get("details") or "").split("; ", 1)[-1]
        shadow.append({"strategy": sleeve, "symbol": row["symbol"], "entry_day": row["time_utc"][:10], "exit_day": exit_day,
                       "ret": ret, "pnl": 0.0, "win": ret > 0, "days": 0, "stop": False, "ai": row.get("ai_decision", ""),
                       "best": None, "tags": tags, "kind": "shadow", "bugs": [],
                       "text": f"Shadow trade ({why}): {row['symbol']} would have made {ret:+.2%} after costs by {exit_day}."})
    return live, shadow, calls


def engine_trades(fetch_orders, fetch_status, download_bars, download_daily):
    """Graded trades of the real-time engine, and its vetoed buys played out."""
    try:
        orders = fetch_orders()
    except Exception as error:
        print("Real-time account not read:", notifier.redact(error))
        orders = []
    try:
        status = fetch_status() or {}
    except Exception as error:
        print("Engine status not read:", notifier.redact(error))
        status = {}
    decisions = (status.get("ai") or {}).get("decisions", []) or []
    by_client = {d.get("client_id"): d for d in decisions if d.get("client_id")}
    raw = grading.pair_engine_fills(orders)
    days = sorted({grading.ny_time(t["entry_time"]).date().isoformat() for t in raw}
                  | {grading.ny_time(datetime.fromtimestamp(d["t"] / 1000, timezone.utc).isoformat()).date().isoformat()
                     for d in decisions if d.get("t")})
    bars, spy_daily = {}, None
    try:
        bars = download_bars(days) if days else {}
        spy_daily = download_daily() if (raw or decisions) else None
    except Exception as error:
        print("Engine prices not downloaded, so the tags stay unknown:", notifier.redact(error))
    live = [grading.grade_engine(t, bars.get(grading.ny_time(t["entry_time"]).date().isoformat()), spy_daily,
                                 by_client.get(t["client_id"])) for t in raw]
    graded_by_client = {t["client_id"]: graded for t, graded in zip(raw, live)}
    shadow, calls = [], []
    for decision in decisions:
        if decision.get("decision") not in ("APPROVE", "VETO") or not decision.get("bar_t"):
            continue
        when = grading.ny_time(datetime.fromtimestamp(decision["bar_t"] / 1000, timezone.utc).isoformat())
        day = when.date().isoformat()
        if decision["decision"] == "APPROVE":
            done = graded_by_client.get(decision.get("client_id"))
            calls.append({"decision": "APPROVE", "ret": done["ret"] if done else None})
            continue
        if str(decision.get("reason", "")).startswith(("AI error", "AI gave an unclear answer")):
            continue
        table = bars.get(day)
        earlier = [key for key in bars if key < day]
        previous = bars[max(earlier)] if earlier else None       # yesterday warms the averages up, as in the engine
        ret = grading.would_have_engine(table, previous, when.tz_localize(None)) if table is not None else None
        calls.append({"decision": "VETO", "ret": ret})
        if ret is not None:
            tags = grading.daily_tags(spy_daily, spy_daily, day)
            tags.update(time=grading.time_bucket(when, realtime=True), weekday=grading.WEEKDAYS[when.weekday()],
                        symbol="SPY", spread="unknown", volume="unknown")
            shadow.append({"strategy": "realtime", "symbol": "SPY", "entry_day": day, "exit_day": day, "ret": ret,
                           "pnl": 0.0, "win": ret > 0, "minutes": None, "stop": False, "ai": "VETO", "best": None,
                           "tags": tags, "kind": "shadow", "bugs": [],
                           "text": f"Shadow trade (AI veto at {when:%H:%M}): would have made {ret:+.2%} after costs."})
    return live, shadow, calls, status


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def review(today, log_rows, main_data, engine_data, tests, send_message, open_issues=True):
    """Everything except the downloads, so it can be tested with hand-made trades."""
    control = fixes.load_control()
    pipe = read_json(PIPE_FILE, {})
    fix_log = read_json(FIXES_LOG, {}).get("attempts", [])
    messages = []
    main_live, main_shadow, main_calls = main_data
    engine_live, engine_shadow, engine_calls = engine_data
    by_strategy = {name: [] for name in fixes.STRATEGIES}
    shadow_by = {name: [] for name in fixes.STRATEGIES}
    for trade in main_live + engine_live:
        by_strategy.setdefault(trade["strategy"], []).append(trade)
    for trade in main_shadow + engine_shadow:
        shadow_by.setdefault(trade["strategy"], []).append(trade)

    report = {"day": today, "made_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "strategies": {},
              "notes": []}
    facts = {}
    for name in fixes.STRATEGIES:
        live, shadow = by_strategy.get(name, []), shadow_by.get(name, [])
        everything = live + shadow
        recent = window(everything, 60, today)
        problems = find_problems(name, recent)
        report["strategies"][name] = {
            "name": STRATEGY_NAMES[name], "fixes_on": [k for k, v in control["fixes"][name].items() if v != fixes.OFF[k]],
            "windows": {"today": stats(window(live, 0, today)), "d20": stats(window(live, 20, today)),
                        "d60": stats(window(live, 60, today)), "shadow_d60": stats(window(shadow, 60, today))},
            "splits": splits(recent), "problems": problems[:3],
            "pipeline": pipe.get(name, {"stage": "idle"}),
        }
        facts[name] = {"live_60d": report["strategies"][name]["windows"]["d60"],
                       "top_problems": [{k: p[k] for k in ("label", "trades", "avg_ret")} for p in problems[:3]]}
        try:
            step_pipeline(name, problems, control, pipe, today, fix_log, messages.append, tests)
        except Exception as error:
            report["notes"].append(f"{STRATEGY_NAMES[name]}: the fix test couldn't run ({notifier.redact(error)[:120]})")
        report["strategies"][name]["pipeline"] = pipe.get(name, {"stage": "idle"})
        for problem in problems:
            if problem["id"] in fixes.OFF_MENU and problem["trades"] >= MIN_PROBLEM and open_issues:
                open_issue(f"[review] {STRATEGY_NAMES[name]}: {problem['label']}",
                           f"The daily review found a repeated problem with no fix on the menu.\n\n"
                           f"- Strategy: {name}\n- Trades: {problem['trades']}\n- Average after costs: {problem['avg_ret']:+.2%}\n"
                           f"- Everything else averaged: {problem['others_avg_ret']}\n\nA person should look at whether "
                           f"a new standard fix belongs on the menu. The bot won't change code by itself.")

    ranges = read_json(os.path.join(DATA_DIR, "drift.json"), {}).get("ranges") or \
        read_json(os.path.join(DATA_DIR, "backtest.json"), {}).get("drift_ranges", {})
    report["allocations"] = {"notes": rebalance(control, by_strategy, shadow_by, ranges or {}, today)}
    report["allocations"]["shares"] = control["allocations"]
    report["allocations"]["paused"] = control["paused"]
    for note in report["allocations"]["notes"]:
        if "paused" in note or "back on" in note:
            messages.append(f"💰 {note}")
    ai_learning(control, "main", main_calls, pipe, messages.append)
    ai_learning(control, "realtime", engine_calls, pipe, messages.append)
    report["ai"] = {bot: {"mode": control["ai_mode"][bot], "record": control["ai_record"][bot],
                          "calls": len(calls), "vetoes": sum(c["decision"] == "VETO" for c in calls)}
                    for bot, calls in (("main", main_calls), ("realtime", engine_calls))}

    bugs = [t for t in main_live + engine_live if t["bugs"]]
    for trade in bugs:
        if open_issues and trade["exit_day"] >= add_trading_days(today, -2):
            open_issue(f"[bug] {STRATEGY_NAMES.get(trade['strategy'], trade['strategy'])} trade {trade['entry_order'][:12]}: "
                       f"{trade['bugs'][0][:80]}",
                       f"The daily review found a rule problem (a bug, not a strategy problem).\n\n- Trade: {trade['text']}\n"
                       f"- Order id: {trade['entry_order']}\n- Problems: {'; '.join(trade['bugs'])}\n")
    report["bugs"] = [{"strategy": t["strategy"], "text": t["text"], "bugs": t["bugs"]} for t in bugs[-20:]]

    total_live = sum(len(v) for v in by_strategy.values())
    fallback = ("No trades have closed yet, so there's nothing to judge. The review will start finding patterns once "
                "each strategy has closed some trades." if total_live == 0 else
                " ".join(f"{STRATEGY_NAMES[n]}: {r['windows']['d60']['trades']} trades in 60 days"
                         + (f", top problem: {r['problems'][0]['label']}" if r["problems"] else ", no repeated problem")
                         + "." for n, r in report["strategies"].items()))
    report["explanation"] = explain({"fallback": fallback, "data": facts}) if total_live else fallback
    control["updated_utc"] = report["made_utc"]

    fixes.save_control(clean(control))
    write_json(PIPE_FILE, clean(pipe), pretty=True)
    write_json(FIXES_LOG, clean({"updated_utc": report["made_utc"], "attempts": fix_log[-200:],
                                 "switches": control["fixes"], "shadow": control["shadow"],
                                 "stages": {n: pipe.get(n, {"stage": "idle"}) for n in fixes.STRATEGIES},
                                 "cooldown": pipe.get("cooldown", {}),
                                 "menu": {k: v["label"] for k, v in fixes.MENU.items()}}))
    write_json(GRADES_FILE, clean(sorted(main_live + engine_live + main_shadow + engine_shadow,
                                         key=lambda t: t["exit_day"], reverse=True)[:400]))
    write_json(REVIEW_FILE, clean(report))

    lines = [f"📋 Daily review, {today}"]
    for name, part in report["strategies"].items():
        d60 = part["windows"]["d60"]
        if d60["trades"] or part["windows"]["today"]["trades"]:
            line = (f"{part['name']}: {part['windows']['today']['trades']} closed today, {d60['trades']} in 60 days"
                    + (f", avg {d60['avg_ret']:+.2%}" if d60["avg_ret"] is not None else ""))
            if part["problems"]:
                line += f". Top problem: {part['problems'][0]['label']}"
            lines.append(line)
    if len(lines) == 1:
        lines.append("No trades have closed yet, so there's nothing to judge.")
    lines.append(f"AI: main bot {control['ai_mode']['main']}, engine {control['ai_mode']['realtime']}.")
    testing = [f"{STRATEGY_NAMES[n]} ({pipe[n]['stage']}: {fixes.MENU[pipe[n]['fix']]['label']})"
               for n in fixes.STRATEGIES if pipe.get(n, {}).get("stage", "idle") != "idle"]
    lines.append("Fixes under test: " + (", ".join(testing) if testing else "none") + ".")
    send_message("\n".join(lines + messages))
    return report


def download_daily(symbols):
    from data import get_daily_bars
    return get_daily_bars(sorted(symbols), datetime.now(timezone.utc) - timedelta(days=500))


def engine_orders():
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    key, secret = os.getenv("RT_ALPACA_API_KEY", "").strip(), os.getenv("RT_ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        return []
    config.require_paper_key(key)
    trading = TradingClient(key, secret, paper=True)         # PAPER ONLY, reads only
    config.require_paper_account(trading, trading.get_account())
    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500,
                                                 after=datetime.fromisoformat(LIVE_START["realtime"]).replace(tzinfo=timezone.utc)))
    return [{"id": str(o.id), "symbol": o.symbol, "side": str(o.side).split(".")[-1].lower(),
             "status": str(o.status).split(".")[-1].lower(), "filled_at": str(o.filled_at or ""),
             "filled_avg_price": float(o.filled_avg_price or 0), "filled_qty": float(o.filled_qty or 0),
             "client_order_id": o.client_order_id} for o in orders]


def engine_bars(days):
    from alpaca.data.enums import DataFeed
    from data import get_intraday_bars
    start = datetime.fromisoformat(min(days)) - timedelta(days=4)
    end = datetime.fromisoformat(max(days)) + timedelta(days=1)
    table = get_intraday_bars("SPY", start, end, 5, DataFeed.IEX)
    return {str(day.date()): group for day, group in table.groupby(table.index.normalize())}


def main():
    parser = argparse.ArgumentParser(description="The daily review. Reads prices and both paper accounts, places nothing.")
    parser.add_argument("--no-send", action="store_true", help="no Telegram message and no GitHub issues")
    args = parser.parse_args()
    os.chdir(BASE_DIR)
    from dotenv import load_dotenv
    load_dotenv()
    today = datetime.now(grading.NEW_YORK).date().isoformat()
    log_rows = read_log()
    import watchdog
    main_data = main_bot_trades(log_rows, download_daily)
    live, shadow, calls, _ = engine_trades(engine_orders, watchdog.fetch_engine_status, engine_bars,
                                           lambda: download_daily({"SPY"}).get("SPY"))
    panels = {}

    def walk(strategy, new, old):
        if strategy == "realtime":
            return walk_forward_engine(new, old)
        if "history" not in panels:
            panels["history"] = main_bot_history()
        return walk_forward_main(strategy, new, old, panels["history"])

    def recent(strategy, new, old, since):
        return compare_recent(strategy, new, old, since)

    send = (lambda text: print(text)) if args.no_send else notifier.send
    report = review(today, log_rows, main_data, (live, shadow, calls), {"walk": walk, "recent": recent}, send,
                    open_issues=not args.no_send)
    print(json.dumps({k: report[k] for k in ("day", "explanation")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
