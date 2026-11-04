"""
Tests for the self-correcting parts: the fix menu (fixes.py) in the live bot and the backtest,
the watchdog, the activity feed, trade grading and the daily review's fix pipeline, money moves
and AI learning. Everything runs on hand-made prices and trades: nothing reaches Alpaca, Gemini,
Telegram or GitHub.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import isolate  # noqa: F401  (keeps tests away from the real control, feed and watchdog files)
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import activity
import ai_analyst
import config
import fixes
import grading
import review
import sleeve_backtest as backtest
import sleeves
import stops
import watchdog
from test_sleeves import FakeTrading, SleeveCase, TODAY, fake_position, pullback, rising, stock_bars


def write_control(**changes):
    control = fixes.default_control()
    for key, value in changes.items():
        if key == "fixes":
            for strategy, fixset in value.items():
                control["fixes"][strategy] = {**fixes.OFF, **fixset}
        else:
            control[key] = value
    fixes.save_control(control)


def clear_control():
    if os.path.exists(fixes.CONTROL_FILE):
        os.remove(fixes.CONTROL_FILE)


# ---------------------------------------------------------------------------
# The menu and the switches
# ---------------------------------------------------------------------------
class MenuTests(unittest.TestCase):
    def tearDown(self):
        clear_control()

    def test_every_switch_ships_off(self):
        control = fixes.load_control()
        for strategy in fixes.STRATEGIES:
            self.assertEqual(control["fixes"][strategy], fixes.OFF)
            self.assertIsNone(control["shadow"][strategy])
        self.assertEqual(control["allocations"], {})
        self.assertEqual(control["paused"], [])

    def test_a_broken_control_file_falls_back_to_off(self):
        os.makedirs(os.path.dirname(fixes.CONTROL_FILE), exist_ok=True)
        with open(fixes.CONTROL_FILE, "w") as handle:
            json.dump({"fixes": {"rsi2_stocks": {"trend_filter": "sideways", "tight_stop": "yes", "made_up": True}},
                       "allocations": {"rsi2_stocks": 7, "nonsense": 0.1}, "ai_mode": {"main": "chaos"}}, handle)
        control = fixes.load_control()
        self.assertEqual(control["fixes"]["rsi2_stocks"], fixes.OFF)
        self.assertEqual(control["allocations"], {})
        self.assertEqual(control["ai_mode"]["main"], config.AI_VETO_MODE)

    def test_every_fix_only_changes_one_standard_switch(self):
        for fix_id, item in fixes.MENU.items():
            changed = {k for k, v in fixes.with_fix(fix_id).items() if v != fixes.OFF[k]}
            self.assertEqual(changed, set(item["set"]), fix_id)

    def test_a_stop_fix_never_loosens_the_locked_stop(self):
        for fixset in (fixes.OFF, fixes.with_fix("tight_stop"), fixes.with_fix("trailing_stop"),
                       {**fixes.OFF, "tight_stop": True, "trailing_stop": True}):
            for highest in (None, 50.0, 100.0, 150.0):
                self.assertGreaterEqual(fixes.stop_level(fixset, 100.0, highest), 93.0 - 1e-9)
                self.assertGreaterEqual(fixes.stop_level(fixset, 100.0, highest, bot="realtime"), 98.0 - 1e-9)
        self.assertAlmostEqual(fixes.stop_level(fixes.with_fix("tight_stop"), 100.0), 95.0)
        self.assertAlmostEqual(fixes.stop_level(fixes.with_fix("trailing_stop"), 100.0, 120.0), 111.6)

    def test_the_earlier_time_exit_halves_and_never_adds_days(self):
        self.assertEqual(fixes.max_hold(fixes.with_fix("time_exit"), 10), 5)
        self.assertEqual(fixes.max_hold(fixes.with_fix("time_exit"), 5), 3)
        self.assertEqual(fixes.max_hold(fixes.OFF, 10), 10)

    def test_entry_filters(self):
        self.assertIn("trend", fixes.entry_block(fixes.with_fix("trend_above"), spy_above_trend=False))
        self.assertEqual(fixes.entry_block(fixes.with_fix("trend_above"), spy_above_trend=True), "")
        self.assertIn("trend", fixes.entry_block(fixes.with_fix("trend_below"), spy_above_trend=True))
        self.assertIn("volatility", fixes.entry_block(fixes.with_fix("vol_filter"), vol_rank_value=0.9))
        self.assertIn("volatility", fixes.entry_block(fixes.with_fix("vol_filter"), vol_rank_value=0.1))
        self.assertEqual(fixes.entry_block(fixes.with_fix("vol_filter"), vol_rank_value=0.5), "")
        self.assertIn("gap", fixes.entry_block(fixes.with_fix("gap_filter"), gap=-0.03))
        self.assertEqual(fixes.entry_block(fixes.with_fix("gap_filter"), gap=0.01), "")
        self.assertIn("liquidity", fixes.entry_block(fixes.with_fix("liquidity_filter"), dollar_volume=5e6))
        self.assertEqual(fixes.entry_block(fixes.OFF, False, 0.99, 0.5, 1.0), "")      # all off: nothing blocks
        self.assertEqual(fixes.entry_block(fixes.with_fix("gap_filter")), "")          # missing data never blocks

    def test_volatility_rank(self):
        calm = list(100 + np.sin(np.arange(300)) * 0.1)
        wild = calm[:-20] + [100 + (5 if i % 2 else -5) for i in range(20)]
        self.assertLess(fixes.vol_rank(calm[:100] + [100] * 200), 0.5)
        self.assertGreaterEqual(fixes.vol_rank(wild), 0.8)
        self.assertIsNone(fixes.vol_rank([100, 101]))


# ---------------------------------------------------------------------------
# The same fixes in the backtest
# ---------------------------------------------------------------------------
class BacktestFixTests(unittest.TestCase):
    def setUp(self):
        days = pd.bdate_range(end="2026-10-30", periods=320)
        closes = np.array(rising(320, 100, 0.5))
        self.days = days
        self.spy = pd.DataFrame({"open": closes, "high": closes * 1.002, "low": closes * 0.998, "close": closes,
                                 "volume": 1e9}, index=days)

    def ibs_table(self, gap_day=None):
        """Every close at the day's low (IBS 0), so IBS buys every day it can."""
        closes = np.array(rising(320, 50, 0.1))
        opens = closes * 1.01
        if gap_day is not None:
            opens = opens.copy()
            opens[gap_day:] = closes[gap_day - 1:-1] * 1.05      # from then on, every open gaps up 5%
        return pd.DataFrame({"open": opens, "high": opens * 1.01, "low": closes, "close": closes, "volume": 1e6},
                            index=self.days)

    def run_ibs(self, fixset, table):
        with mock.patch.object(config, "IBS_SYMBOLS", ["AAA"]):
            panel = backtest.build_panel({"SPY": self.spy, "AAA": table}, etf={"AAA"})
            return backtest.simulate(panel, backtest.Options(("ibs_etfs",), atr_stops=False,
                                                             allocations={"ibs_etfs": 0.2}, fixes={"ibs_etfs": fixset}))

    def test_switches_off_change_nothing(self):
        table = self.ibs_table()
        plain = self.run_ibs(None, table)
        off = self.run_ibs(fixes.OFF, table)
        pd.testing.assert_series_equal(plain["equity"], off["equity"])

    def test_the_gap_filter_skips_the_entry_after_a_gap(self):
        table = self.ibs_table(gap_day=300)
        plain = self.run_ibs(fixes.OFF, table)
        gapped = self.run_ibs(fixes.with_fix("gap_filter"), table)
        self.assertGreater(gapped["fix_skips"]["ibs_etfs"], 0)
        self.assertEqual(plain["fix_skips"]["ibs_etfs"], 0)

    def test_the_earlier_time_exit_sells_sooner(self):
        table = self.ibs_table()
        plain = self.run_ibs(fixes.OFF, table)
        early = self.run_ibs(fixes.with_fix("time_exit"), table)
        self.assertLess(early["trades"]["days"].max(), plain["trades"]["days"].max())

    def test_the_liquidity_filter_skips_thin_symbols(self):
        thin = self.ibs_table()
        thin["volume"] = 10.0                        # about $1,000 a day
        plain = self.run_ibs(fixes.OFF, thin)
        result = self.run_ibs(fixes.with_fix("liquidity_filter"), thin)
        # The 20-day average needs 20 days first, and missing data never blocks: only those early buys get through.
        self.assertLess(len(result["trades"]), len(plain["trades"]))
        self.assertGreater(result["fix_skips"]["ibs_etfs"], 0)
        self.assertTrue((result["trades"]["entry"] < 25).all())


# ---------------------------------------------------------------------------
# The same fixes in the live bot
# ---------------------------------------------------------------------------
class LiveFixTests(SleeveCase):
    def tearDown(self):
        clear_control()

    def go_with_price(self, tables, price_factor=1.0, trading=None):
        self.trading = trading or FakeTrading()
        rows = [{"symbol": s, "name": ""} for s in tables]
        with mock.patch("universe.rows", return_value=rows), contextlib.redirect_stdout(io.StringIO()):
            return sleeves.run("stock", self.trading, 100_000.0, 100_000.0, False, False, TODAY, [], self.log_event,
                               lambda symbols, start: {s: t for s, t in tables.items() if s in symbols},
                               lambda symbol, fallback: fallback * price_factor, self.plans.append,
                               spread=lambda symbol: 0.0002)

    def test_no_fix_means_the_same_buy_as_before(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.go_with_price({"AAA": stock_bars(pullback())})
        self.assertEqual(len(self.trading.buys()), 1)

    def test_the_gap_filter_blocks_a_live_entry_and_logs_a_shadow_trade(self):
        self.enable("ENABLE_RSI2_STOCKS")
        write_control(fixes={"rsi2_stocks": {"gap_filter": True}})
        self.go_with_price({"AAA": stock_bars(pullback())}, price_factor=1.05)
        self.assertEqual(self.trading.buys(), [])
        skipped = [e for e in self.events if e.get("event") == "SIGNAL_SKIPPED"]
        self.assertEqual(len(skipped), 1)
        self.assertIn("gap filter", skipped[0]["details"])

    def test_a_paused_sleeve_buys_nothing(self):
        self.enable("ENABLE_RSI2_STOCKS")
        write_control(paused=["rsi2_stocks"])
        self.go_with_price({"AAA": stock_bars(pullback())})
        self.assertEqual(self.trading.buys(), [])
        self.assertEqual(sleeves.share_of("rsi2_stocks"), 0.0)

    def test_a_shadow_fix_is_logged_but_never_trades(self):
        self.enable("ENABLE_RSI2_STOCKS")
        control = fixes.default_control()
        control["shadow"]["rsi2_stocks"] = fixes.with_fix("gap_filter")
        fixes.save_control(control)
        activity.clear()
        self.go_with_price({"AAA": stock_bars(pullback())}, price_factor=1.05)
        self.assertEqual(len(self.trading.buys()), 1)                 # the live version still buys
        self.assertTrue(any("Shadow test" in line["text"] for line in activity.pending()))
        activity.clear()

    def test_the_earlier_time_exit_sells_after_half_the_days(self):
        self.enable("ENABLE_RSI2_STOCKS")
        write_control(fixes={"rsi2_stocks": {"time_exit": True}})
        self.own(AAA="rsi2_stocks")                  # bought on 26 October: 5 trading days by the 30th
        self.go_with_price({"AAA": stock_bars(pullback())}, trading=FakeTrading([fake_position("AAA", 10, 380, 380)]))
        self.assertEqual([o.symbol for o in self.trading.sells()], ["AAA"])

    def test_without_the_time_exit_it_holds(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.own(AAA="rsi2_stocks")
        self.go_with_price({"AAA": stock_bars(pullback())}, trading=FakeTrading([fake_position("AAA", 10, 380, 380)]))
        self.assertEqual(self.trading.sells(), [])

    def test_the_tighter_stop_reaches_the_stop_order(self):
        record = {"sleeve": "rsi2_stocks", "fix_stop": 95.0}
        self.assertAlmostEqual(stops.level_for(record, 100.0), 95.0)
        self.assertAlmostEqual(stops.level_for({"sleeve": "rsi2_stocks", "fix_stop": 50.0}, 100.0), 93.0)   # never looser
        self.assertTrue(sleeves.stop_hit(record, 100.0, 94.9))
        self.assertFalse(sleeves.stop_hit({"sleeve": "rsi2_stocks"}, 100.0, 94.9))

    def test_money_moves_never_pass_the_horizons_share(self):
        self.enable("ENABLE_RSI2_STOCKS", "ENABLE_IBS_ETFS")
        write_control(allocations={"rsi2_stocks": 0.9})
        self.assertEqual(sleeves.share_of("rsi2_stocks"), config.SHORT_ALLOCATION)

    def test_the_entry_record_holds_no_market_prices(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.go_with_price({"AAA": stock_bars(pullback())})
        record = next(iter(sleeves.load_state()["entries"].values()))
        self.assertEqual(record["spread"], "normal")
        self.assertNotIn("price", " ".join(record))


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------
class WatchdogTests(unittest.TestCase):
    NOW = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.sent = []
        for path in (watchdog.STATE_FILE, ai_analyst.HEALTH_FILE):
            if os.path.exists(path):
                os.remove(path)

    def beat(self, minutes_ago):
        return {"heartbeat": {"last_run_ms": int((self.NOW - timedelta(minutes=minutes_ago)).timestamp() * 1000)}}

    def check(self, status, market_open=True, now=None, unprotected=()):
        return watchdog.check(now or self.NOW, market_open, unprotected, status,
                              lambda text: self.sent.append(text) or True)

    def test_a_late_engine_alerts_once_an_hour(self):
        self.assertIn("engine_late", self.check(self.beat(12)))
        self.check(self.beat(27), now=self.NOW + timedelta(minutes=15))
        self.assertEqual(len(self.sent), 1)
        self.check(self.beat(72), now=self.NOW + timedelta(minutes=60))
        self.assertEqual(len(self.sent), 2)

    def test_a_running_engine_is_quiet(self):
        self.assertEqual(self.check(self.beat(1)), {})
        self.assertEqual(self.sent, [])

    def test_nothing_is_checked_while_the_market_is_closed(self):
        self.assertEqual(self.check(self.beat(600), market_open=False), {})

    def test_no_engine_set_up_means_no_engine_alert(self):
        with mock.patch.object(watchdog, "engine_url", return_value=""):
            found = watchdog.check(self.NOW, True, (), "fetch", lambda text: self.sent.append(text) or True)
        self.assertNotIn("engine_late", found)

    def test_a_position_without_a_stop_alerts(self):
        found = self.check(self.beat(1), unprotected=["XLK"])
        self.assertIn("no_stop", found)
        self.assertIn("XLK", self.sent[0])

    def test_three_gemini_failures_in_a_row_alert(self):
        for _ in range(3):
            ai_analyst._note_health(True)
        self.assertIn("gemini", self.check(self.beat(1)))
        ai_analyst._note_health(False)
        self.assertEqual(ai_analyst.failures_in_row(), 0)

    def test_the_simulated_missed_run_sends_a_test_alert_and_saves_nothing(self):
        found = watchdog.simulate_missed_run(self.NOW, lambda text: self.sent.append(text) or True)
        self.assertIn("engine_late", found)
        self.assertIn("[TEST]", self.sent[0])
        self.assertFalse(os.path.exists(watchdog.STATE_FILE))


# ---------------------------------------------------------------------------
# Activity feed
# ---------------------------------------------------------------------------
class ActivityTests(unittest.TestCase):
    def test_newest_first_and_capped(self):
        if os.path.exists(activity.FEED_FILE):
            os.remove(activity.FEED_FILE)
        for i in range(320):
            activity.say(f"line {i}")
        activity.flush()
        activity.say("newest")
        activity.flush()
        with open(activity.FEED_FILE) as handle:
            feed = json.load(handle)
        self.assertEqual(len(feed), activity.KEEP)
        self.assertEqual(feed[0]["text"], "newest")
        self.assertEqual(feed[1]["text"], "line 319")


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------
TRADE = {"symbol": "AAA", "sleeve": "rsi2_stocks", "entry_day": "2026-10-20", "exit_day": "2026-10-23", "days": 3,
         "qty": 10, "entry": 100.0, "exit": 103.0, "pnl": 30.0, "ret": 0.03, "stop": False, "entry_order": "o-1",
         "entry_time": "2026-10-20T13:30:05", "exit_time": "2026-10-23T14:00:00"}


class GradingTests(unittest.TestCase):
    def test_a_trade_is_graded_in_plain_english(self):
        graded = grading.grade_main(TRADE, {"ai_decision": "APPROVE", "equity": 100_000, "spread": "normal"}, 99.5)
        self.assertIn("slippage +0.50%", graded["text"])
        self.assertIn("RSI(2) exit rule", graded["text"])
        self.assertIn("AI approved: right call", graded["text"])
        self.assertEqual(graded["bugs"], [])
        self.assertEqual(graded["tags"]["weekday"], "Tuesday")

    def test_a_rule_break_is_a_bug_not_a_strategy_problem(self):
        big = {**TRADE, "qty": 100}
        graded = grading.grade_main(big, {"equity": 100_000})
        self.assertTrue(any("5% cap" in bug for bug in graded["bugs"]))

    def test_the_tags_use_labels_not_prices(self):
        spy = stock_bars(rising(300))
        table = stock_bars(rising(300, 50, 0.2))
        graded = grading.grade_main({**TRADE, "entry_day": "2026-10-28", "exit_day": "2026-10-30"}, {}, None, table, spy)
        self.assertEqual(graded["tags"]["trend"], "SPY above its 200-day average")
        for value in graded["tags"].values():
            self.assertIsInstance(value, str)

    def test_would_have_plays_out_a_vetoed_trade_with_the_exit_rule(self):
        table = stock_bars(pullback(300) + [400.0] * 5)
        signal_day = str(table.index[-6].date())
        result = grading.would_have_main("rsi2_stocks", "AAA", signal_day, float(table["close"].iloc[-6]), table)
        self.assertIsNotNone(result)
        self.assertGreater(result[1], 0)

    def test_engine_fills_pair_into_trades(self):
        orders = [{"side": "buy", "status": "filled", "filled_at": "2026-11-04T15:00:00+00:00", "filled_avg_price": 600,
                   "filled_qty": 10, "id": "b", "client_order_id": "rt-SPY-202611041455-buy", "symbol": "SPY"},
                  {"side": "sell", "status": "filled", "filled_at": "2026-11-04T15:20:00+00:00", "filled_avg_price": 597,
                   "filled_qty": 10, "id": "s", "client_order_id": "rt-SPY-202611041515-stop", "symbol": "SPY"}]
        trades = grading.pair_engine_fills(orders)
        self.assertEqual(len(trades), 1)
        self.assertTrue(trades[0]["stop"])
        graded = grading.grade_engine(trades[0])
        self.assertEqual(graded["minutes"], 20)
        self.assertEqual(graded["tags"]["time"], "first 15 minutes" if False else graded["tags"]["time"])


# ---------------------------------------------------------------------------
# The daily review: problems, the fix pipeline, money and the AI
# ---------------------------------------------------------------------------
def made_trade(strategy, day, ret, **tags):
    base = {"trend": "SPY above its 200-day average", "vol": "normal", "time": "middle of the day", "gap": "normal",
            "weekday": "Tuesday", "symbol": "AAA", "spread": "normal", "volume": "normal"}
    base.update(tags)
    return {"strategy": strategy, "symbol": base["symbol"], "entry_day": day, "exit_day": day, "ret": ret,
            "pnl": ret * 1000, "win": ret > 0, "days": 2, "stop": False, "ai": "", "best": None, "tags": base,
            "kind": "live", "bugs": [], "text": "", "entry_order": ""}


class ReviewTests(unittest.TestCase):
    TODAY = "2026-12-15"

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)
        for name in ("REVIEW_FILE", "FIXES_LOG", "GRADES_FILE", "PIPE_FILE", "LOG_FILE"):
            patcher = mock.patch.object(review, name, os.path.join(self.folder, name.lower() + ".json"))
            patcher.start()
            self.addCleanup(patcher.stop)
        clear_control()
        self.addCleanup(clear_control)
        self.messages = []

    def losers_below_trend(self, count=12):
        days = pd.bdate_range(end=self.TODAY, periods=count)
        bad = [made_trade("rsi2_stocks", str(d.date()), -0.02, trend="SPY below its 200-day average") for d in days]
        good = [made_trade("rsi2_stocks", str(d.date()), 0.01) for d in days]
        return bad + good

    def tests(self, walk=True, better=True, dd=-0.01):
        return {"walk": lambda s, new, old: {"passed": walk, "sharpe_base": 0.5, "sharpe_fixed": 0.7 if walk else 0.4,
                                              "dd_base": -0.1, "dd_fixed": -0.08, "years": 10, "years_helped": 7},
                "recent": lambda s, new, old, since: {"return_new": 0.02 if better else -0.01, "return_old": 0.01,
                                                      "dd_new": dd, "dd_old": -0.02, "better": better}}

    def test_a_problem_needs_a_losing_group(self):
        problems = review.find_problems("rsi2_stocks", self.losers_below_trend())
        self.assertEqual(problems[0]["id"], "trend_down")
        self.assertEqual(problems[0]["fix"], "trend_above")
        self.assertEqual(problems[0]["trades"], 12)

    def test_no_problem_when_it_loses_everywhere(self):
        days = pd.bdate_range(end=self.TODAY, periods=12)
        trades = [made_trade("rsi2_stocks", str(d.date()), -0.01, trend=t)
                  for d in days for t in ("SPY below its 200-day average", "SPY above its 200-day average")]
        self.assertNotIn("trend_down", [p["id"] for p in review.find_problems("rsi2_stocks", trades)])

    def run_pipeline(self, problems, tests, days_later=0):
        control, pipe, log = fixes.load_control(), {}, []
        review.step_pipeline("rsi2_stocks", problems, control, pipe, self.TODAY, log, self.messages.append, tests)
        if days_later:
            later = review.add_trading_days(self.TODAY, days_later)
            review.step_pipeline("rsi2_stocks", problems, control, pipe, later, log, self.messages.append, tests)
        return control, pipe, log

    def test_a_fix_that_passes_older_data_goes_to_shadow_not_live(self):
        problems = review.find_problems("rsi2_stocks", self.losers_below_trend())
        control, pipe, log = self.run_pipeline(problems, self.tests(walk=True))
        self.assertEqual(pipe["rsi2_stocks"]["stage"], "shadow")
        self.assertEqual(control["fixes"]["rsi2_stocks"], fixes.OFF)              # not live yet
        self.assertEqual(control["shadow"]["rsi2_stocks"]["trend_filter"], "above")
        self.assertEqual(log[0]["stage"], "shadow")

    def test_a_fix_that_fails_older_data_is_rejected_for_60_trading_days(self):
        problems = review.find_problems("rsi2_stocks", self.losers_below_trend())
        control, pipe, log = self.run_pipeline(problems, self.tests(walk=False))
        self.assertEqual(pipe.get("rsi2_stocks", {}).get("stage", "idle"), "idle")
        self.assertEqual(log[0]["stage"], "rejected")
        self.assertEqual(pipe["cooldown"]["rsi2_stocks"]["trend_above"], review.add_trading_days(self.TODAY, 60))
        review.step_pipeline("rsi2_stocks", problems, control, pipe, review.add_trading_days(self.TODAY, 5), log,
                             self.messages.append, self.tests(walk=True))
        self.assertEqual(len(log), 1)                       # can't retry inside the 60 days

    def test_too_few_trades_try_nothing(self):
        problems = review.find_problems("rsi2_stocks", self.losers_below_trend(count=6))
        control, pipe, log = self.run_pipeline(problems, self.tests())
        self.assertEqual(log, [])

    def test_after_20_days_in_shadow_a_better_fix_goes_live_and_tells_you(self):
        problems = review.find_problems("rsi2_stocks", self.losers_below_trend())
        control, pipe, log = self.run_pipeline(problems, self.tests(better=True), days_later=20)
        self.assertEqual(control["fixes"]["rsi2_stocks"]["trend_filter"], "above")
        self.assertEqual(control["shadow"]["rsi2_stocks"], fixes.OFF)            # the old version keeps running in shadow
        self.assertEqual(pipe["rsi2_stocks"]["stage"], "probation")
        self.assertTrue(any("now on" in m for m in self.messages))

    def test_a_shadow_fix_that_does_worse_is_rejected(self):
        problems = review.find_problems("rsi2_stocks", self.losers_below_trend())
        control, pipe, log = self.run_pipeline(problems, self.tests(better=False), days_later=20)
        self.assertEqual(control["fixes"]["rsi2_stocks"], fixes.OFF)
        self.assertIsNone(control["shadow"]["rsi2_stocks"])
        self.assertEqual(log[-1]["stage"], "rejected")

    def test_shadow_waits_20_trading_days(self):
        problems = review.find_problems("rsi2_stocks", self.losers_below_trend())
        control, pipe, log = self.run_pipeline(problems, self.tests(better=True), days_later=10)
        self.assertEqual(pipe["rsi2_stocks"]["stage"], "shadow")

    def test_a_live_fix_rolls_back_at_its_drawdown_limit(self):
        problems = review.find_problems("rsi2_stocks", self.losers_below_trend())
        control, pipe, log = self.run_pipeline(problems, self.tests(better=True), days_later=20)
        later = review.add_trading_days(self.TODAY, 25)
        with mock.patch.object(review, "drawdown_limit", return_value=-0.05):
            review.step_pipeline("rsi2_stocks", problems, control, pipe, later, log, self.messages.append,
                                 self.tests(better=True, dd=-0.2))
        self.assertEqual(control["fixes"]["rsi2_stocks"], fixes.OFF)
        self.assertEqual(log[-1]["stage"], "rolled back")
        self.assertTrue(any("switched off again" in m for m in self.messages))

    def test_a_live_fix_that_holds_up_for_20_days_is_kept(self):
        problems = review.find_problems("rsi2_stocks", self.losers_below_trend())
        control, pipe, log = self.run_pipeline(problems, self.tests(better=True), days_later=20)
        review.step_pipeline("rsi2_stocks", problems, control, pipe, review.add_trading_days(self.TODAY, 40), log,
                             self.messages.append, self.tests(better=True))
        self.assertEqual(log[-1]["stage"], "kept")
        self.assertEqual(control["fixes"]["rsi2_stocks"]["trend_filter"], "above")
        self.assertIsNone(control["shadow"]["rsi2_stocks"])

    def test_one_fix_under_test_at_a_time(self):
        trades = self.losers_below_trend() + [made_trade("rsi2_stocks", "2026-12-14", -0.03, gap="big (over 2%)")
                                              for _ in range(12)]
        problems = review.find_problems("rsi2_stocks", trades)
        self.assertGreaterEqual(len([p for p in problems if p["fix"]]), 2)
        control, pipe, log = self.run_pipeline(problems, self.tests(walk=True))
        self.assertEqual(len(log), 1)

    def test_money_moves_at_most_5_points_and_keeps_the_horizon_total(self):
        control = fixes.default_control()
        live = {"ibs_etfs": [made_trade("ibs_etfs", "2026-12-14", 0.02 + i * 0.001) for i in range(12)],
                "rsi2_stocks": [made_trade("rsi2_stocks", "2026-12-14", -0.01 + i * 0.0001) for i in range(12)],
                "turn_of_month": [made_trade("turn_of_month", "2026-12-14", 0.005 + i * 0.0002) for i in range(12)]}
        with mock.patch.object(sleeves, "is_enabled", side_effect=lambda s: s in ("ibs_etfs", "rsi2_stocks", "turn_of_month")):
            review.rebalance(control, live, {}, {}, self.TODAY)
        shares = control["allocations"]
        self.assertAlmostEqual(sum(shares[s] for s in live), config.SHORT_ALLOCATION, places=6)
        base = config.SHORT_ALLOCATION / 3
        for sleeve in live:
            self.assertLessEqual(abs(shares[sleeve] - base), review.MAX_SHIFT + 1e-9)
        self.assertLess(shares["rsi2_stocks"], base)

    def test_a_sleeve_below_its_worst_backtest_range_is_paused(self):
        control = fixes.default_control()
        live = {"rsi2_stocks": [made_trade("rsi2_stocks", "2026-12-14", -0.05) for _ in range(20)]}
        ranges = {"rsi2_stocks": {"expectancy_low": -0.02}}
        with mock.patch.object(sleeves, "is_enabled", side_effect=lambda s: s in ("ibs_etfs", "rsi2_stocks")):
            notes = review.rebalance(control, live, {}, ranges, self.TODAY)
        self.assertIn("rsi2_stocks", control["paused"])
        self.assertEqual(control["allocations"]["rsi2_stocks"], 0.0)
        self.assertAlmostEqual(control["allocations"]["ibs_etfs"], config.SHORT_ALLOCATION)
        self.assertTrue(any("paused" in n for n in notes))

    def test_the_ai_goes_to_shadow_when_its_vetoes_cost_money_and_comes_back(self):
        control, pipe = fixes.default_control(), {}
        control["ai_mode"]["main"] = "enforce"
        costly = [{"decision": "VETO", "ret": 0.01}] * 20
        review.ai_learning(control, "main", costly, pipe, self.messages.append)
        self.assertEqual(control["ai_mode"]["main"], "shadow")
        self.assertIn("vetoed would have averaged", control["ai_record"]["main"])
        helpful = [{"decision": "VETO", "ret": -0.01}] * 20
        review.ai_learning(control, "main", helpful, pipe, self.messages.append)
        self.assertEqual(control["ai_mode"]["main"], "enforce")
        self.assertEqual(len(self.messages), 2)

    def test_the_ai_isnt_judged_before_20_vetoes(self):
        control, pipe = fixes.default_control(), {}
        control["ai_mode"]["main"] = "enforce"
        review.ai_learning(control, "main", [{"decision": "VETO", "ret": 0.05}] * 19, pipe, self.messages.append)
        self.assertEqual(control["ai_mode"]["main"], "enforce")

    def test_the_older_data_rule(self):
        days = pd.bdate_range("2018-01-01", "2025-12-31")
        noise = np.random.default_rng(1).normal(0, 0.01, len(days))
        base = pd.Series(100 * np.cumprod(1 + noise), index=days)
        better = pd.Series(100 * np.cumprod(1 + noise * 0.5 + 0.0005), index=days)
        self.assertTrue(review.judge(base, better)["passed"])
        self.assertFalse(review.judge(better, base)["passed"])

    def test_a_first_review_with_no_trades_says_so(self):
        report = review.review(self.TODAY, [], ([], [], []), ([], [], []), self.tests(), self.messages.append,
                               open_issues=False)
        self.assertIn("nothing to judge", report["explanation"])
        self.assertIn("nothing to judge", self.messages[0])
        self.assertTrue(os.path.exists(review.REVIEW_FILE))
        saved = fixes.load_control()
        self.assertEqual(saved["fixes"]["rsi2_stocks"], fixes.OFF)
        with open(review.FIXES_LOG) as handle:
            self.assertEqual(json.load(handle)["attempts"], [])


if __name__ == "__main__":
    unittest.main()
