"""
The real-time engine's fix menu in the Python backtest (rt_backtest.py), on small hand-made days.
Every fix must match realtime/src/engine.ts, never loosen a locked rule, and with all fixes off the
engine must give exactly what it gave before. No network, no orders.
"""

import itertools
import math
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import rt_backtest as rt  # noqa: E402

YESTERDAY, TODAY = "2026-11-02", "2026-11-03"


def session(date, closes, volumes=None, opens=None, lows=None):
    """A full day of 5-minute bars from 09:30. Missing bars repeat the last close."""
    closes = list(closes) + [closes[-1]] * (78 - len(closes))
    opens = list(opens) + closes[len(opens):] if opens is not None else closes
    lows = list(lows) + [c - 0.1 for c in closes[len(lows):]] if lows is not None else [c - 0.1 for c in closes]
    volumes = list(volumes) + [1000.0] * (78 - len(volumes)) if volumes is not None else [1000.0] * 78
    stamps = pd.date_range(f"{date} 09:30", periods=78, freq="5min")
    table = pd.DataFrame({"open": opens, "high": [c + 0.1 for c in closes], "low": lows, "close": closes,
                          "volume": volumes}, index=stamps, dtype=float)
    table["minute"] = table.index.hour * 60 + table.index.minute
    return pd.Timestamp(date), table


def dip_then_rise():
    """Yesterday flat. Today down for 12 bars, then up for 14: one buy crossover, then flat to the close."""
    return [100 - i * 0.2 for i in range(12)] + [98 + i * 0.5 for i in range(14)]


def two_days(today=None, **changes):
    return [session(YESTERDAY, [100.0] * 78), session(TODAY, today or dip_then_rise(), **changes)]


CONTEXT = {"prev_close": 100.0, "above_sma200": True, "vol_rank": 0.5, "vol_scale": 1.0}


def run(fixes=None, days=None, context=CONTEXT):
    daily = {pd.Timestamp(TODAY): context} if context is not None else {}
    return rt.simulate(days or two_days(), fixes={**rt.NO_FIXES, **(fixes or {})}, daily=daily)


def signal_bar(days):
    """Where today's buy crossover is, and the minute its entry would happen (the next bar's open)."""
    signals = rt.day_signals(days[0][1]["close"].to_numpy(), days[1][1]["close"].to_numpy())
    index = int(np.flatnonzero(signals == 1)[0])
    return index, int(days[1][1]["minute"].iloc[index + 1])


class DefaultsTests(unittest.TestCase):
    def test_all_fixes_off_gives_exactly_the_plain_engine(self):
        generator = np.random.default_rng(7)
        days = []
        for k, date in enumerate(pd.bdate_range("2026-03-02", periods=12)):
            walk = 100 + np.cumsum(generator.normal(0, 0.3, 78))
            days.append(session(date.date().isoformat(), walk.tolist(), volumes=generator.uniform(500, 1500, 78).tolist()))
        plain, fixed = rt.simulate(days), rt.simulate(days, fixes=rt.NO_FIXES, daily={})
        self.assertTrue(plain["equity"].equals(fixed["equity"]))
        self.assertTrue(plain["trades"].equals(fixed["trades"]))
        self.assertGreater(len(plain["trades"]), 0)

    def test_junk_in_the_control_file_counts_as_off(self):
        junk = {"trend_filter": "sideways", "vol_filter": "yes", "time_filter": 5, "gap_filter": 1, "confirmation": None,
                "tight_stop": "true", "vol_sizing": [True]}
        self.assertEqual(rt.clean_fixes(junk), rt.NO_FIXES)
        self.assertEqual(rt.clean_fixes(None), rt.NO_FIXES)
        self.assertEqual(rt.clean_fixes({"trend_filter": "above", "time_filter": 30})["time_filter"], 30)

    def test_no_fix_can_loosen_a_locked_rule(self):
        for combo in itertools.product(("off", "above", "below"), (0, 15, 30), *[(False, True)] * 8):
            fixes = dict(zip(["trend_filter", "time_filter", "vol_filter", "gap_filter", "liquidity_filter", "confirmation",
                              "time_exit", "trailing_stop", "tight_stop", "vol_sizing"], combo))
            for scale in (None, 0.3, 1.0, 5.0):
                limits = rt.limits_for(fixes, {"vol_scale": scale})
                self.assertLessEqual(limits["position_pct"], rt.POSITION_PCT)
                self.assertLessEqual(limits["stop_pct"], rt.STOP_PCT)
                self.assertLessEqual(limits["max_entries"], rt.MAX_ENTRIES)
                self.assertLessEqual(limits["daily_loss_pct"], rt.DAILY_LOSS_PCT)
                self.assertLessEqual(limits["no_entries_from"], rt.NO_ENTRIES_FROM)
                self.assertLessEqual(limits["flat_from"], rt.FLAT_FROM)


class DailyContextTests(unittest.TestCase):
    def test_same_numbers_as_the_worker(self):
        # The same series is in realtime/test/engine.test.ts, with the same expected numbers.
        dates = pd.bdate_range("2025-01-01", periods=301)
        closes = pd.Series([100 + 10 * math.sin(k / 9) + 0.05 * k + 3 * math.sin(k / 2.3) for k in range(301)], index=dates)
        context = rt.daily_contexts(closes)[dates[-1]]
        self.assertAlmostEqual(context["prev_close"], 121.88366162664722, places=9)
        self.assertTrue(context["above_sma200"])
        self.assertAlmostEqual(context["vol_rank"], 0.4444444444444444, places=9)
        self.assertAlmostEqual(context["vol_scale"], 1.0127883333957304, places=9)

    def test_a_short_history_is_unknown_not_guessed(self):
        dates = pd.bdate_range("2025-01-01", periods=150)
        context = rt.daily_contexts(pd.Series(np.linspace(100, 120, 150), index=dates))[dates[-1]]
        self.assertIsNone(context["above_sma200"])
        self.assertIsNone(context["vol_rank"])

    def test_only_yesterday_and_before_are_used(self):
        dates = pd.bdate_range("2025-01-01", periods=301)
        closes = pd.Series(np.linspace(100, 130, 301), index=dates)
        spiked = closes.copy()
        spiked.iloc[-1] = 1.0
        self.assertEqual(rt.daily_contexts(closes)[dates[-1]], rt.daily_contexts(spiked)[dates[-1]])


class FilterTests(unittest.TestCase):
    def test_the_made_up_day_trades_once_without_fixes(self):
        self.assertEqual(len(run()["trades"]), 1)

    def test_time_filter_blocks_entries_in_the_first_minutes(self):
        early = two_days(today=[99.8, 99.6] + [99.6 + i * 0.7 for i in range(1, 12)])
        _, entry_minute = signal_bar(early)
        self.assertEqual(entry_minute, rt.SESSION_OPEN + 20)            # the buy would go in at 09:50
        self.assertEqual(len(run(days=early)["trades"]), 1)
        self.assertEqual(len(run({"time_filter": 15}, days=early)["trades"]), 1)
        self.assertEqual(len(run({"time_filter": 30}, days=early)["trades"]), 0)
        self.assertEqual(rt.entry_block({**rt.NO_FIXES, "time_filter": 15}, CONTEXT, rt.SESSION_OPEN + 14, 100, None, 0), "time")
        self.assertIsNone(rt.entry_block({**rt.NO_FIXES, "time_filter": 15}, CONTEXT, rt.SESSION_OPEN + 15, 100, None, 0))

    def test_trend_filter(self):
        self.assertEqual(len(run({"trend_filter": "above"}, context={**CONTEXT, "above_sma200": False})["trades"]), 0)
        self.assertEqual(len(run({"trend_filter": "above"})["trades"]), 1)
        self.assertEqual(len(run({"trend_filter": "below"})["trades"]), 0)
        self.assertEqual(len(run({"trend_filter": "above"}, context={**CONTEXT, "above_sma200": None})["trades"]), 0)

    def test_volatility_filter_skips_the_top_and_bottom_fifth(self):
        for rank, trades in ((0.85, 0), (0.8, 0), (0.1, 0), (0.2, 1), (0.5, 1), (0.79, 1)):
            self.assertEqual(len(run({"vol_filter": True}, context={**CONTEXT, "vol_rank": rank})["trades"]), trades, rank)

    def test_gap_filter(self):
        self.assertEqual(len(run({"gap_filter": True}, context={**CONTEXT, "prev_close": 97.0})["trades"]), 0)   # 3% gap
        self.assertEqual(len(run({"gap_filter": True}, context={**CONTEXT, "prev_close": 99.0})["trades"]), 1)   # 1% gap

    def test_missing_daily_data_blocks_rather_than_guesses(self):
        self.assertEqual(len(run({"gap_filter": True}, context=None)["trades"]), 0)
        self.assertEqual(len(run({"vol_sizing": True}, context={**CONTEXT, "vol_scale": None})["trades"]), 0)

    def test_liquidity_filter_skips_a_thin_signal_bar(self):
        index, _ = signal_bar(two_days())
        thin = [1000.0] * 78
        thin[index] = 400.0
        self.assertEqual(len(run({"liquidity_filter": True}, days=two_days(volumes=thin))["trades"]), 0)
        self.assertEqual(len(run({"liquidity_filter": True})["trades"]), 1)


class ExitAndSizeTests(unittest.TestCase):
    def test_confirmation_waits_one_bar_and_needs_the_same_side(self):
        signals = np.array([0, 1, 0, 0])
        self.assertEqual(rt._confirmed(signals, np.array([-1.0, 1.0, 1.0, 1.0]), 0).tolist(), [0, 0, 1, 0])
        self.assertEqual(rt._confirmed(signals, np.array([-1.0, 1.0, -1.0, -1.0]), 0).tolist(), [0, 0, 0, 0])

    def test_confirmation_enters_one_bar_later(self):
        plain = run()["trades"].iloc[0]
        late = run({"confirmation": True})["trades"].iloc[0]
        self.assertLess(late["ret"], plain["ret"])           # the rise went on, so a later entry costs more

    def test_earlier_time_exit_closes_by_15_30(self):
        plain, early = run()["trades"].iloc[0], run({"time_exit": True})["trades"].iloc[0]
        self.assertAlmostEqual(early["days"] - plain["days"], -25 / 390)

    def test_tighter_stop_sells_on_a_one_and_a_half_percent_drop(self):
        prices = dip_then_rise()[:19] + [101.0] * 20       # the entry fills at 101, then the price goes flat
        lows = [c - 0.1 for c in prices]
        lows[25] = 101.0 * 0.985                            # one bar dips 1.5%: under a 1% stop, not a 2% one
        days = two_days(today=prices, lows=lows)
        self.assertEqual(run(days=days)["trades"]["why"].tolist(), ["close"])
        self.assertEqual(run({"tight_stop": True}, days=days)["trades"]["why"].tolist(), ["stop"])

    def test_trailing_stop_keeps_more_of_a_rise(self):
        prices = dip_then_rise()[:19] + [101.0 + k for k in range(1, 10)] + [110.0, 110.0] + [110.0 - k for k in range(1, 11)]
        plain = run(days=two_days(today=prices))["trades"]
        trailing = run({"trailing_stop": True}, days=two_days(today=prices))["trades"]
        self.assertEqual((plain["why"].iloc[0], trailing["why"].iloc[0]), ("signal", "stop"))
        self.assertGreater(trailing["pnl"].iloc[0], plain["pnl"].iloc[0])

    def test_volatility_sizing_shrinks_but_never_grows_the_position(self):
        normal = run({"vol_sizing": True})["trades"].iloc[0]
        half = run({"vol_sizing": True}, context={**CONTEXT, "vol_scale": 0.5})["trades"].iloc[0]
        double = run({"vol_sizing": True}, context={**CONTEXT, "vol_scale": 2.0})["trades"].iloc[0]
        # Half of 4x is 2x, while a normal trade is held to 95% of the 4x buying power (3.8x).
        self.assertAlmostEqual(half["fee"] / normal["fee"], 2.0 / 3.8, delta=0.02)
        self.assertAlmostEqual(double["fee"], normal["fee"])


if __name__ == "__main__":
    unittest.main()
