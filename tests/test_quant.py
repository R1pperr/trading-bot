"""
The multi-horizon upgrade: new sleeves, the A/B rule, stops at Alpaca, fractional shares,
queued orders before the open, the paper-only guard, AI shadow mode, the journal and drift.

Alpaca, Gemini and Telegram are fakes here, so no test can reach a real service or place a
real order. Prices are small and hand-made, so each rule can be checked by eye.
"""

import contextlib
import io
import json
import os
import unittest
import isolate  # noqa: F401  (keeps tests away from the real control, feed and watchdog files)
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce

import ai_analyst
import bot
import config
import dashboard
import drift
import journal
import market_hours
import notifier
import quant_lab
import risk
import rt_backtest
import sleeve_backtest as backtest
import sleeves
import stops
import strategies
import universe
from test_sleeves import FakeTrading, SleeveCase, TODAY, fake_position, pullback, rising, shaped, stock_bars


def bars_with(closes, highs=None, lows=None, opens=None, end="2026-10-30"):
    """Daily bars with exact highs and lows, so IBS can be set by hand."""
    closes = np.asarray(closes, dtype=float)
    days = pd.bdate_range(end=end, periods=len(closes))
    return pd.DataFrame({"open": closes if opens is None else opens,
                         "high": closes * 1.01 if highs is None else highs,
                         "low": closes * 0.99 if lows is None else lows, "close": closes, "volume": 1000.0}, index=days)


def working_stop(symbol, qty=10.0, stop_price=93.0, order_id=None):
    return SimpleNamespace(id=order_id or f"stop-{symbol}", symbol=symbol, side="sell", qty=str(qty), stop_price=str(stop_price),
                           order_type="stop", client_order_id=f"tach-rsi2_stocks-stop-{symbol.lower()}",
                           created_at="2026-11-01T14:00:00Z", status="new", asset_class="us_equity", filled_at=None, filled_qty="0")


def filled(symbol, side, qty, price, when, client_id="", order_id=None, stop_price=None):
    return SimpleNamespace(id=order_id or f"{symbol}-{side}-{when}", symbol=symbol, side=side, status="filled", filled_qty=str(qty),
                           filled_avg_price=str(price), filled_at=when, client_order_id=client_id, stop_price=stop_price,
                           asset_class="us_equity", created_at=when, order_type="market")


class Broker(FakeTrading):
    """A fake broker that also knows assets, calendars and cancelling."""

    def __init__(self, positions=(), open_orders=(), closed_orders=(), refuse=(), fractional=(), days=()):
        super().__init__(positions, open_orders, closed_orders, refuse)
        self.fractional = set(fractional)
        self.days = list(days)
        self.calls = []

    def get_asset(self, symbol):
        return SimpleNamespace(fractionable=symbol in self.fractional)

    def get_calendar(self, filters=None):
        return [SimpleNamespace(date=day) for day in self.days]

    def submit_order(self, order_data):
        self.calls.append(("submit", order_data.symbol, getattr(order_data, "type", "")))
        return super().submit_order(order_data)

    def cancel_order_by_id(self, order_id):
        self.calls.append(("cancel", order_id, ""))
        super().cancel_order_by_id(order_id)
        self.open_orders = [o for o in self.open_orders if o.id != order_id]

    def stop_orders(self):
        return [o for o in self.submitted if o.type == OrderType.STOP]

    def market_sells(self):
        return [o for o in self.sells() if o.type == OrderType.MARKET]


# ---------------------------------------------------------------------------
# Rules, on hand-made numbers
# ---------------------------------------------------------------------------
class IbsRuleTests(unittest.TestCase):
    def test_ibs_is_where_the_close_sits_in_the_days_range(self):
        np.testing.assert_allclose(strategies.internal_bar_strength([110, 110, 110], [100, 100, 100], [100, 105, 110]), [0, 0.5, 1])

    def test_a_day_with_no_range_has_no_reading(self):
        self.assertTrue(np.isnan(strategies.internal_bar_strength([100], [100], [100])[0]))

    def test_entry_needs_ibs_under_a_fifth(self):
        self.assertTrue(strategies.ibs_entry(0.19))
        self.assertFalse(strategies.ibs_entry(0.2))

    def test_exit_on_ibs_over_four_fifths_or_after_five_days(self):
        self.assertTrue(strategies.ibs_exit(0.81, 1))
        self.assertTrue(strategies.ibs_exit(0.5, 5))
        self.assertFalse(strategies.ibs_exit(0.8, 4))


class TurnOfMonthCalendarTests(unittest.TestCase):
    """Trading days, not calendar days: February 2026 opens on a Monday after a weekend."""

    CALENDAR = [day.date().isoformat() for day in pd.bdate_range("2026-01-02", "2026-02-13")]

    def order(self, day):
        return strategies.turn_of_month_order(self.CALENDAR, self.CALENDAR.index(day))

    def test_buys_on_the_last_trading_day_not_the_last_calendar_day(self):
        self.assertEqual(self.order("2026-01-30"), "buy")        # Friday. The 31st is a Saturday
        self.assertEqual(self.order("2026-01-29"), "")

    def test_sells_on_the_fourth_trading_day_of_the_new_month(self):
        self.assertEqual(self.order("2026-02-05"), "sell")       # Feb 2, 3, 4, then 5
        self.assertEqual(self.order("2026-02-04"), "")

    def test_a_holiday_moves_the_days(self):
        calendar = [d for d in self.CALENDAR if d != "2026-02-03"]
        self.assertEqual(strategies.turn_of_month_order(calendar, calendar.index("2026-02-05")), "")
        self.assertEqual(strategies.turn_of_month_order(calendar, calendar.index("2026-02-06")), "sell")

    def test_a_month_end_on_a_holiday_moves_the_buy_earlier(self):
        calendar = [d for d in self.CALENDAR if d != "2026-01-30"]
        self.assertEqual(strategies.turn_of_month_order(calendar, calendar.index("2026-01-29")), "buy")

    def test_the_last_known_day_is_not_called_the_last_of_its_month(self):
        self.assertFalse(strategies.is_last_trading_day(self.CALENDAR, len(self.CALENDAR) - 1))

    def test_first_day_of_a_month_counts_as_one(self):
        self.assertEqual(strategies.trading_day_of_month(self.CALENDAR, self.CALENDAR.index("2026-02-02")), 1)


class RotationRuleTests(unittest.TestCase):
    def test_score_averages_the_four_lookbacks(self):
        close = np.arange(1.0, 301.0)
        score = strategies.rotation_score(close)[-1]
        expected = np.mean([300 / 279 - 1, 300 / 237 - 1, 300 / 174 - 1, 300 / 48 - 1])
        self.assertAlmostEqual(score, expected)

    def test_top_three_by_score(self):
        picks = strategies.rotation_picks(["A", "B", "C", "D"], [0.4, 0.3, 0.2, 0.1], [2, 2, 2, 2], [1, 1, 1, 1])
        self.assertEqual(picks, ["A", "B", "C"])

    def test_a_top_symbol_below_its_average_leaves_its_slot_in_cash(self):
        picks = strategies.rotation_picks(["A", "B", "C", "D"], [0.4, 0.3, 0.2, 0.1], [2, 0.5, 2, 2], [1, 1, 1, 1])
        self.assertEqual(picks, ["A", "C"])        # D doesn't move up into B's slot

    def test_symbols_with_no_score_are_left_out(self):
        picks = strategies.rotation_picks(["A", "B"], [np.nan, 0.2], [2, 2], [1, 1])
        self.assertEqual(picks, ["B"])


class VolatilityRuleTests(unittest.TestCase):
    def wild(self, size):
        return [size if day % 2 else -size for day in range(config.MOMENTUM_VOL_DAYS)]

    def test_calm_returns_are_capped_at_one_so_there_is_no_leverage(self):
        self.assertEqual(strategies.vol_scale(self.wild(0.001)), 1.0)

    def test_wild_returns_scale_exposure_down_to_target_over_volatility(self):
        returns = self.wild(0.02)
        annual = pd.Series(returns).std() * 252 ** 0.5
        self.assertAlmostEqual(strategies.vol_scale(returns), 0.12 / annual)
        self.assertLess(strategies.vol_scale(returns), 1.0)

    def test_too_little_history_stays_at_one(self):
        self.assertEqual(strategies.vol_scale([0.05, -0.05, 0.05]), 1.0)

    def test_vol_sizing_gives_a_jumpier_stock_less(self):
        self.assertAlmostEqual(risk.vol_sized_dollars(2_000, 0.02, 0.01, 100_000, 50_000), 1_000)

    def test_vol_sizing_gives_a_calmer_stock_more(self):
        self.assertAlmostEqual(risk.vol_sized_dollars(2_000, 0.005, 0.01, 100_000, 50_000), 4_000)

    def test_vol_sizing_never_passes_the_5_percent_rule(self):
        self.assertAlmostEqual(risk.vol_sized_dollars(4_000, 0.001, 0.01, 100_000, 50_000), 5_000)

    def test_vol_sizing_stays_inside_the_room_left(self):
        self.assertAlmostEqual(risk.vol_sized_dollars(4_000, 0.001, 0.01, 100_000, 700), 700)

    def test_no_reading_keeps_the_usual_size(self):
        self.assertEqual(risk.vol_sized_dollars(2_000, float("nan"), 0.01, 100_000, 50_000), 2_000)


class HorizonSplitTests(unittest.TestCase):
    def test_each_horizon_is_split_equally_among_its_enabled_sleeves(self):
        shares = strategies.split_by_horizon(["rsi2_stocks", "ibs_etfs", "turn_of_month", "rotation", "momentum_stocks"])
        for name in ("rsi2_stocks", "ibs_etfs", "turn_of_month"):
            self.assertAlmostEqual(shares[name], 0.20)
        self.assertAlmostEqual(shares["rotation"], 0.20)
        self.assertAlmostEqual(shares["momentum_stocks"], 0.20)

    def test_one_sleeve_alone_gets_its_whole_horizon(self):
        self.assertAlmostEqual(strategies.split_by_horizon(["rsi2_stocks"])["rsi2_stocks"], 0.60)

    def test_unused_horizons_stay_in_cash(self):
        self.assertAlmostEqual(sum(strategies.split_by_horizon(["rsi2_stocks", "momentum_stocks"]).values()), 0.80)

    def test_the_mix_never_passes_the_whole_account(self):
        self.assertAlmostEqual(sum(strategies.split_by_horizon(config.SLEEVES).values()), 1.0)


class SettingsTests(unittest.TestCase):
    def source(self):
        with open(os.path.join(sleeves.BASE_DIR, "config.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_crypto_is_off_and_its_code_is_kept(self):
        self.assertRegex(self.source(), r"(?m)^CRYPTO_ENABLED = False\b")
        self.assertTrue(os.path.exists(os.path.join(sleeves.BASE_DIR, "crypto.py")))

    def test_the_five_chosen_sleeves_are_on_and_crypto_sleeves_off(self):
        for flag in ("ENABLE_RSI2_STOCKS", "ENABLE_IBS_ETFS", "ENABLE_TURN_OF_MONTH", "ENABLE_ROTATION",
                     "ENABLE_MOMENTUM_STOCKS"):
            self.assertRegex(self.source(), rf"(?m)^{flag} = True\b")
        for flag in ("ENABLE_CRYPTO_TREND", "ENABLE_CRYPTO_HOLD"):
            self.assertRegex(self.source(), rf"(?m)^{flag} = False\b")

    def test_every_sleeve_has_a_flag_and_a_horizon(self):
        self.assertEqual(set(sleeves.FLAGS), set(config.SLEEVES))
        self.assertTrue({s["horizon"] for s in config.SLEEVES.values()} <= set(config.HORIZONS))

    def test_ivv_is_the_only_symbol_kept_for_turn_of_the_month(self):
        self.assertEqual(config.ONLY_FOR_TURN_OF_MONTH, ["IVV"])

    def test_the_rotation_list_is_the_published_one(self):
        self.assertEqual(config.ROTATION_SYMBOLS, ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "DBC", "GLD", "IEF", "TLT"])


class CryptoOffTests(SleeveCase):
    def test_a_run_does_no_crypto_work(self):
        args = SimpleNamespace(test_order=None, dry_run=False)
        with mock.patch.object(config, "CRYPTO_ENABLED", False), mock.patch.object(sleeves, "run") as run:
            self.assertEqual(bot.run_crypto_side(args), 0)
        run.assert_not_called()

    def test_the_universe_refresh_skips_crypto(self):
        with mock.patch.object(config, "CRYPTO_ENABLED", False), mock.patch.object(universe, "build") as build, \
                mock.patch.object(universe, "save"), contextlib.redirect_stdout(io.StringIO()):
            build.return_value = {"symbols": []}
            universe.refresh()
        self.assertEqual([call.args[0] for call in build.call_args_list], ["stock"])

    def test_crypto_sleeves_leave_the_dashboard(self):
        with mock.patch.object(config, "CRYPTO_ENABLED", False):
            names = {row["name"] for row in sleeves.summary([])["sleeves"]}
        self.assertFalse(names & {"crypto_trend", "crypto_hold"})

    def test_nights_are_skipped(self):
        settings = 'CRYPTO_ENABLED = False\nSTRATEGY_MODE = "sleeves"\nENABLE_CRYPTO_TREND = True\n'
        self.assertFalse(market_hours.crypto_is_trading(settings))


# ---------------------------------------------------------------------------
# The sleeves in a live run
# ---------------------------------------------------------------------------
class LiveSleeveTests(SleeveCase):
    def ibs_table(self, last_ibs):
        closes = np.array(rising(300))
        low, high = closes[-1] * 0.99, closes[-1] * 1.01
        highs, lows = closes * 1.01, closes * 0.99
        highs[-1], lows[-1] = high, low
        table = bars_with(closes, highs, lows)
        table.iloc[-1, table.columns.get_loc("close")] = low + (high - low) * last_ibs
        return table

    def test_ibs_buys_a_fund_on_a_weak_close(self):
        self.enable("ENABLE_IBS_ETFS")
        self.go("stock", {"SPY": self.ibs_table(0.1)})
        self.assertEqual([o.symbol for o in self.trading.buys()], ["SPY"])

    def test_ibs_ignores_a_stock_that_isnt_on_its_list(self):
        self.enable("ENABLE_IBS_ETFS")
        self.go("stock", {"NVDA": self.ibs_table(0.1)})
        self.assertEqual(self.trading.buys(), [])

    def test_ibs_sells_on_a_strong_close(self):
        self.enable("ENABLE_IBS_ETFS")
        self.own(SPY="ibs_etfs")
        self.go("stock", {"SPY": self.ibs_table(0.9)}, FakeTrading([fake_position("SPY", entry=390, price=395)]))
        self.assertEqual([o.symbol for o in self.trading.sells()], ["SPY"])

    def test_ibs_sells_after_five_trading_days(self):
        self.enable("ENABLE_IBS_ETFS")
        state = sleeves.load_state()
        state["positions"]["SPY"] = {"sleeve": "ibs_etfs", "entry_date": "2026-10-26"}    # Mon to Fri is 5 bars
        sleeves.save_state(state)
        self.go("stock", {"SPY": self.ibs_table(0.5)}, FakeTrading([fake_position("SPY", entry=390, price=395)]))
        self.assertEqual([o.symbol for o in self.trading.sells()], ["SPY"])

    def calendar(self):
        return [d.date().isoformat() for d in pd.bdate_range("2026-10-01", "2026-11-30")]

    def test_turn_of_the_month_buys_ivv_on_the_last_trading_day(self):
        self.enable("ENABLE_TURN_OF_MONTH")
        tables = {"IVV": stock_bars(rising()), "SPY": stock_bars(rising())}
        self.go("stock", tables, Broker(days=self.calendar()), today="2026-10-30")
        self.assertEqual([o.symbol for o in self.trading.buys()], ["IVV"])

    def test_turn_of_the_month_sells_on_the_fourth_trading_day(self):
        self.enable("ENABLE_TURN_OF_MONTH")
        self.own(IVV="turn_of_month")
        tables = {"IVV": stock_bars(rising()), "SPY": stock_bars(rising())}
        self.go("stock", tables, Broker([fake_position("IVV", entry=390, price=395)], days=self.calendar()), today="2026-11-05")
        self.assertEqual([o.symbol for o in self.trading.sells()], ["IVV"])

    def test_turn_of_the_month_does_nothing_on_a_plain_day(self):
        self.enable("ENABLE_TURN_OF_MONTH")
        self.go("stock", {"IVV": stock_bars(rising()), "SPY": stock_bars(rising())}, Broker(days=self.calendar()), today="2026-10-20")
        self.assertEqual(self.trading.submitted, [])

    def test_no_other_sleeve_may_hold_ivv(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.go("stock", {"IVV": stock_bars(pullback()), "AAA": stock_bars(pullback())})
        self.assertEqual([o.symbol for o in self.trading.buys()], ["AAA"])

    def rotation_tables(self):
        tables = {s: stock_bars(rising(step=0.1 + n * 0.1)) for n, s in enumerate(config.ROTATION_SYMBOLS)}
        tables["TLT"] = stock_bars([500 - day for day in range(300)])          # falling, so it's never picked
        return tables

    def test_rotation_buys_the_top_three_in_equal_slots(self):
        self.enable("ENABLE_ROTATION")
        self.go("stock", self.rotation_tables())
        bought = {o.symbol for o in self.trading.buys()}
        self.assertEqual(len(bought), 3)
        self.assertNotIn("TLT", bought)
        for order in self.trading.buys():
            self.assertLessEqual(float(order.qty) * self.rotation_tables()[order.symbol]["close"].iloc[-1], 5_000 + 1e-6)

    def test_rotation_sells_what_leaves_the_top_at_the_new_month(self):
        self.enable("ENABLE_ROTATION")
        self.own(SPY="rotation")
        state = sleeves.load_state()
        state["rotation"] = {"month": "2026-09", "targets": ["SPY"], "stopped": []}
        sleeves.save_state(state)
        self.go("stock", self.rotation_tables(), FakeTrading([fake_position("SPY", entry=100, price=130)]))
        self.assertIn("SPY", [o.symbol for o in self.trading.sells()])

    def test_a_stopped_rotation_slot_stays_in_cash_until_the_next_month(self):
        self.enable("ENABLE_ROTATION")
        self.go("stock", self.rotation_tables())
        picked = sleeves.load_state()["rotation"]["targets"]
        state = sleeves.load_state()
        state["rotation"]["stopped"] = [picked[0]]
        state["positions"] = {}
        state["days"] = {}
        sleeves.save_state(state)
        self.go("stock", self.rotation_tables(), FakeTrading())
        self.assertNotIn(picked[0], [o.symbol for o in self.trading.buys()])

    def test_a_sleeve_that_is_off_makes_no_new_entries_but_still_sells(self):
        self.own(SPY="ibs_etfs")
        self.go("stock", {"SPY": self.ibs_table(0.9)}, FakeTrading([fake_position("SPY", entry=390, price=395)]))
        self.assertEqual(len(self.trading.sells()), 1)
        self.assertEqual(self.trading.buys(), [])

    def test_the_vol_scaled_sleeve_buys_a_smaller_slot(self):
        self.enable("ENABLE_MOMENTUM_STOCKS")
        with mock.patch.object(config, "VOL_SCALED_MOMENTUM", True):
            state = sleeves.load_state()
            state["momentum"] = {"month": "2026-10", "targets": ["AAA"], "stopped": [], "scale": 0.5}
            sleeves.save_state(state)
            self.go("stock", {"SPY": stock_bars(rising()), "AAA": stock_bars(rising())})
        spent = float(self.trading.buys()[0].qty) * rising()[-1]
        self.assertLessEqual(spent, 20_000 / 10 * 0.5)


class FractionalTests(SleeveCase):
    def test_a_fractionable_stock_buys_parts_of_a_share(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.go("stock", {"AAA": stock_bars(pullback())}, Broker(fractional=["AAA"]))
        qty = float(self.trading.buys()[0].qty)
        self.assertAlmostEqual(qty * pullback()[-1], 5_000, delta=0.01)
        self.assertFalse(qty.is_integer())

    def test_a_stock_alpaca_wont_split_buys_whole_shares(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.go("stock", {"AAA": stock_bars(pullback())}, Broker())
        self.assertTrue(float(self.trading.buys()[0].qty).is_integer())

    def test_a_failed_asset_lookup_falls_back_to_whole_shares(self):
        broker = mock.MagicMock()
        broker.get_asset.side_effect = RuntimeError("no")
        self.assertFalse(sleeves.fractionable(broker, "AAA"))

    def test_the_setting_turns_it_off(self):
        with mock.patch.object(config, "FRACTIONAL_SHARES", False):
            self.assertFalse(sleeves.fractionable(Broker(fractional=["AAA"]), "AAA"))

    def test_fractions_round_down(self):
        self.assertEqual(sleeves.units(1.23456789, "stock", True), 1.234567)


# ---------------------------------------------------------------------------
# Stops held at Alpaca
# ---------------------------------------------------------------------------
class AlpacaStopTests(SleeveCase):
    def setUp(self):
        super().setUp()
        config.STOP_EXECUTION = "alpaca"
        self.enable("ENABLE_RSI2_STOCKS")

    def flat(self, symbol="AAA"):
        return {symbol: stock_bars([100.0] * 300)}

    def done_today(self):
        state = sleeves.load_state()
        state["days"]["stock"] = TODAY
        sleeves.save_state(state)

    def test_a_stop_is_placed_right_after_an_entry_fills(self):
        self.own(AAA="rsi2_stocks")
        self.done_today()
        self.go("stock", self.flat(), Broker([fake_position("AAA", qty=10, entry=100, price=100)]))
        order = self.trading.stop_orders()[0]
        self.assertEqual((order.symbol, float(order.qty), float(order.stop_price)), ("AAA", 10.0, 93.0))
        self.assertEqual(order.side, OrderSide.SELL)

    def test_whole_shares_use_good_till_cancelled(self):
        self.assertEqual(stops.request_for("AAA", 10.0, 93.0, "rsi2_stocks").time_in_force, TimeInForce.GTC)

    def test_part_shares_use_a_day_stop(self):
        self.assertEqual(stops.request_for("AAA", 6.58, 93.0, "rsi2_stocks").time_in_force, TimeInForce.DAY)

    def test_the_stop_id_carries_the_sleeve(self):
        self.assertTrue(stops.request_for("AAA", 1.0, 93.0, "rotation").client_order_id.startswith("tach-rotation-stop-"))

    def test_an_expired_day_stop_is_placed_again_the_next_day(self):
        self.own(AAA="rsi2_stocks")
        self.go("stock", self.flat(), Broker([fake_position("AAA", qty=6.5, entry=100, price=100)]))
        order = self.trading.stop_orders()[0]
        self.assertEqual(order.time_in_force, TimeInForce.DAY)
        self.assertFalse(any(e.get("event") == "STOP_FIXED" for e in self.events))       # expected, so no alert

    def test_a_missing_good_till_cancelled_stop_is_fixed_and_reported(self):
        self.own(AAA="rsi2_stocks")
        self.done_today()
        self.go("stock", self.flat(), Broker([fake_position("AAA", qty=10, entry=100, price=100)]))
        reports = [e for e in self.events if e.get("event") == "STOP_FIXED"]
        self.assertEqual(len(reports), 1)
        self.assertIn("AAA", reports[0]["details"])

    def test_no_stop_while_the_buy_is_still_filling(self):
        # Alpaca refused XLK's stop on 2026-11-03 (code 40310000, possible wash trade) because its buy was still open.
        self.own(AAA="rsi2_stocks")
        self.done_today()
        still_open = SimpleNamespace(id="buy-AAA", symbol="AAA", side="buy", client_order_id="tach-rsi2_stocks-buy-aaa",
                                     created_at=f"{TODAY}T14:17:00Z", filled_at=None, filled_qty="4", status="partially_filled",
                                     asset_class="us_equity", order_type="market", qty="10")
        self.go("stock", self.flat(), Broker([fake_position("AAA", qty=4, entry=100, price=100)], [still_open]))
        self.assertEqual(self.trading.stop_orders(), [])
        self.assertFalse(any(e.get("event") == "STOP_FIXED" for e in self.events))

    def test_a_right_stop_is_left_alone(self):
        self.own(AAA="rsi2_stocks")
        self.done_today()
        self.go("stock", self.flat(), Broker([fake_position("AAA", qty=10, entry=100, price=100)], [working_stop("AAA")]))
        self.assertEqual(self.trading.submitted, [])
        self.assertEqual(self.trading.cancelled, [])

    def test_a_position_never_keeps_two_stops(self):
        self.own(AAA="rsi2_stocks")
        self.done_today()
        pair = [working_stop("AAA", order_id="one"), working_stop("AAA", order_id="two")]
        self.go("stock", self.flat(), Broker([fake_position("AAA", qty=10, entry=100, price=100)], pair))
        self.assertEqual(self.trading.cancelled, ["two"])
        self.assertEqual(self.trading.stop_orders(), [])
        self.assertEqual(len([e for e in self.events if e.get("event") == "STOP_FIXED"]), 1)

    def test_a_stop_for_the_wrong_size_is_replaced(self):
        self.own(AAA="rsi2_stocks")
        self.done_today()
        self.go("stock", self.flat(), Broker([fake_position("AAA", qty=10, entry=100, price=100)], [working_stop("AAA", qty=4)]))
        self.assertEqual(self.trading.cancelled, ["stop-AAA"])
        self.assertEqual(float(self.trading.stop_orders()[0].qty), 10.0)

    def test_a_position_already_under_its_stop_gets_the_backup_sale(self):
        self.own(AAA="rsi2_stocks")
        self.done_today()
        self.go("stock", self.flat(), Broker([fake_position("AAA", qty=10, entry=100, price=90)]))
        self.assertEqual(self.trading.stop_orders(), [])
        self.assertEqual(len(self.trading.market_sells()), 1)

    def test_the_backup_check_stays_quiet_while_a_stop_is_working(self):
        self.own(AAA="rsi2_stocks")
        self.done_today()
        self.go("stock", self.flat(), Broker([fake_position("AAA", qty=10, entry=100, price=90)], [working_stop("AAA")]))
        self.assertEqual(self.trading.market_sells(), [])

    def test_the_stop_is_cancelled_before_a_normal_exit(self):
        self.own(OLD="rsi2_stocks")
        broker = Broker([fake_position("OLD", qty=100, entry=300, price=301)], [working_stop("OLD", qty=100, stop_price=279)])
        self.go("stock", {"OLD": stock_bars(rising())}, broker)
        kinds = [call[0] for call in broker.calls]
        self.assertEqual(kinds, ["cancel", "submit"])
        self.assertEqual(self.trading.market_sells()[0].symbol, "OLD")

    def test_a_slow_cancel_gets_two_more_tries(self):
        self.own(OLD="rsi2_stocks")
        broker = Broker([fake_position("OLD", qty=100, entry=300, price=301)], [working_stop("OLD", qty=100, stop_price=279)])
        answers = [RuntimeError("insufficient qty"), RuntimeError("insufficient qty"), SimpleNamespace(id="x", status="accepted")]
        broker.submit_order = mock.Mock(side_effect=answers)
        with mock.patch.object(sleeves.time, "sleep"):
            self.go("stock", {"OLD": stock_bars(rising())}, broker)
        self.assertEqual(broker.submit_order.call_count, 3)

    def test_a_refused_stop_is_reported_and_the_run_carries_on(self):
        self.own(AAA="rsi2_stocks")
        self.done_today()
        self.go("stock", self.flat(), Broker([fake_position("AAA", qty=10, entry=100, price=100)], refuse=["AAA"]))
        self.assertIn("refused", next(e for e in self.events if e.get("event") == "STOP_FIXED")["details"])

    def test_a_dry_run_changes_no_stops(self):
        self.own(AAA="rsi2_stocks")
        self.go("stock", self.flat(), Broker([fake_position("AAA", qty=10, entry=100, price=100)],
                                             [working_stop("AAA", order_id="a"), working_stop("AAA", order_id="b")]), dry_run=True)
        self.assertEqual(self.trading.submitted, [])
        self.assertEqual(self.trading.cancelled, [])

    def test_a_stop_that_filled_at_alpaca_is_written_into_the_books_once(self):
        self.own(AAA="rsi2_stocks")
        self.done_today()
        fill = filled("AAA", "sell", 10, 92.5, "2026-11-02T15:00:00Z", "tach-rsi2_stocks-stop-abc", "fill-1", "93.0")
        self.go("stock", self.flat(), Broker(closed_orders=[fill]))
        self.go("stock", self.flat(), Broker(closed_orders=[fill]))
        rows = [e for e in self.events if e.get("event") == "ORDER"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["details"].startswith("sleeve=rsi2_stocks; stop-loss"))
        self.assertAlmostEqual(sleeves.load_state()["closed_pl"]["rsi2_stocks"], -75.0)

    def test_a_rotation_slot_stopped_at_alpaca_stays_empty(self):
        fill = filled("GLD", "sell", 1, 90, "2026-11-02T15:00:00Z", "tach-rotation-stop-abc", "fill-2", "93.0")
        self.done_today()
        self.go("stock", self.flat(), Broker(closed_orders=[fill]))
        self.assertEqual(sleeves.load_state()["rotation"]["stopped"], ["GLD"])

    def test_the_15_minute_mode_places_no_stop_orders(self):
        config.STOP_EXECUTION = "check"
        self.own(AAA="rsi2_stocks")
        self.done_today()
        self.go("stock", self.flat(), Broker([fake_position("AAA", qty=10, entry=100, price=100)]))
        self.assertEqual(self.trading.submitted, [])


# ---------------------------------------------------------------------------
# Orders queued before the open
# ---------------------------------------------------------------------------
class PreOpenTests(SleeveCase):
    def clock(self, minutes_to_open, is_open=False):
        now = datetime(2026, 11, 3, 14, 0, tzinfo=timezone.utc)
        return SimpleNamespace(is_open=is_open, timestamp=now, next_open=now + timedelta(minutes=minutes_to_open))

    def test_the_half_hour_before_the_open_counts(self):
        self.assertTrue(bot.is_pre_open(self.clock(25)))
        self.assertTrue(bot.is_pre_open(self.clock(30)))

    def test_earlier_than_that_does_not(self):
        self.assertFalse(bot.is_pre_open(self.clock(31)))

    def test_a_holiday_has_no_open_nearby(self):
        self.assertFalse(bot.is_pre_open(self.clock(60 * 24)))

    def test_an_open_market_is_not_pre_open(self):
        self.assertFalse(bot.is_pre_open(self.clock(25, is_open=True)))

    def test_an_old_style_clock_is_not_pre_open(self):
        self.assertFalse(bot.is_pre_open(SimpleNamespace(is_open=False)))

    def queued(self, tables, trading):
        self.trading = trading
        rows = [{"symbol": s, "name": ""} for s in tables]
        with mock.patch.object(universe, "rows", return_value=rows), contextlib.redirect_stdout(io.StringIO()):
            sleeves.run("stock", trading, 100_000.0, 100_000.0, False, False, TODAY, [], self.log_event,
                        lambda symbols, start: {s: t for s, t in tables.items() if s in symbols},
                        lambda symbol, fallback: fallback, self.plans.append, pre_open=True)

    def test_todays_orders_queue_before_the_open(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.queued({"AAA": stock_bars(pullback())}, Broker())
        order = self.trading.buys()[0]
        self.assertEqual((order.time_in_force, order.type), (TimeInForce.DAY, OrderType.MARKET))
        self.assertEqual(sleeves.load_state()["days"]["stock"], TODAY)

    def test_stale_prices_cant_trigger_the_backup_stop(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.own(AAA="rsi2_stocks")
        state = sleeves.load_state()
        state["days"]["stock"] = TODAY
        sleeves.save_state(state)
        self.queued({"AAA": stock_bars([100.0] * 300)}, Broker([fake_position("AAA", entry=100, price=80)]))
        self.assertEqual(self.trading.market_sells(), [])

    def test_the_first_tick_lands_before_the_open_all_year(self):
        for day in ("2026-01-05T14:06:00", "2026-07-06T13:06:00"):
            when = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
            self.assertTrue(market_hours.in_market_hours(when), day)


# ---------------------------------------------------------------------------
# The paper-only guard
# ---------------------------------------------------------------------------
class PaperGuardTests(unittest.TestCase):
    PAPER = SimpleNamespace(_base_url="https://paper-api.alpaca.markets")
    LIVE = SimpleNamespace(_base_url="https://api.alpaca.markets")

    def refuses(self, action):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            action()

    def test_a_paper_client_and_account_pass(self):
        config.require_paper_account(self.PAPER, SimpleNamespace(account_number="PA3ABCDEF"))

    def test_a_live_server_is_refused(self):
        self.refuses(lambda: config.require_paper_account(self.LIVE, SimpleNamespace(account_number="PA3ABCDEF")))

    def test_a_live_account_number_is_refused(self):
        self.refuses(lambda: config.require_paper_account(self.PAPER, SimpleNamespace(account_number="123456")))

    def test_a_missing_account_number_is_refused(self):
        self.refuses(lambda: config.require_paper_account(self.PAPER, SimpleNamespace()))

    def test_a_live_key_is_refused_before_any_call(self):
        self.refuses(lambda: config.require_paper_key("AK123"))

    def test_a_paper_key_passes(self):
        config.require_paper_key("PK123")

    def test_the_real_paper_client_passes_the_url_check(self):
        from alpaca.trading.client import TradingClient
        client = TradingClient("PKFAKE", "fake", paper=True)
        config.require_paper_account(client, SimpleNamespace(account_number="PA1"))

    def test_every_trading_client_in_the_code_is_paper(self):
        for name in sorted(os.listdir(sleeves.BASE_DIR)):
            if not name.endswith(".py") or name == "test_connection.py":
                continue
            with open(os.path.join(sleeves.BASE_DIR, name), encoding="utf-8") as handle:
                for line in handle:
                    if "TradingClient(" in line and "class " not in line:
                        self.assertIn("paper=True", line, name)


# ---------------------------------------------------------------------------
# AI shadow mode
# ---------------------------------------------------------------------------
class AiShadowTests(SleeveCase):
    def tables(self):
        return {"AAA": stock_bars(pullback())}

    def setUp(self):
        super().setUp()
        self.enable("ENABLE_RSI2_STOCKS")
        config.USE_AI_ANALYST = True
        self.start(mock.patch.object(ai_analyst, "load_dotenv"))
        self.start(mock.patch.dict(os.environ, {"GEMINI_API_KEY": "made-up-for-tests"}))

    def ask(self, decision, reason):
        return self.start(mock.patch.object(ai_analyst, "_ask_gemini", return_value=(decision, reason)))

    def test_enforce_is_the_default(self):
        # The brief of 2026-11-04: the AI analyst on, in enforce mode. The daily review may still switch it to shadow.
        with open(os.path.join(sleeves.BASE_DIR, "config.py"), encoding="utf-8") as handle:
            self.assertRegex(handle.read(), r'(?m)^AI_VETO_MODE = "enforce"')

    def test_in_shadow_a_veto_is_logged_but_the_trade_goes_ahead(self):
        config.AI_VETO_MODE = "shadow"
        self.ask(ai_analyst.VETO, "Looks shaky")
        self.go("stock", self.tables())
        self.assertEqual(len(self.trading.buys()), 1)
        order = next(e for e in self.events if e.get("event") == "ORDER")
        self.assertEqual((order["ai_decision"], order["ai_reason"]), (ai_analyst.VETO, "Looks shaky"))
        self.assertTrue(any(e.get("event") == "AI_DECISION" for e in self.events))

    def test_in_enforce_a_veto_blocks_the_trade(self):
        config.AI_VETO_MODE = "enforce"
        self.ask(ai_analyst.VETO, "Looks shaky")
        self.go("stock", self.tables())
        self.assertEqual(self.trading.buys(), [])

    def test_an_ai_error_cant_block_a_shadow_trade(self):
        config.AI_VETO_MODE = "shadow"
        self.start(mock.patch.object(ai_analyst, "_ask_gemini", side_effect=RuntimeError("quota")))
        self.go("stock", self.tables())
        self.assertEqual(len(self.trading.buys()), 1)

    def test_an_ai_error_still_blocks_in_enforce(self):
        config.AI_VETO_MODE = "enforce"
        self.start(mock.patch.object(ai_analyst, "_ask_gemini", side_effect=RuntimeError("quota")))
        self.go("stock", self.tables())
        self.assertEqual(self.trading.buys(), [])

    def test_the_ai_limit_counts_the_whole_day_not_one_run(self):
        # Before the fix every 15-minute run started again at 5 reviews, so vetoes could add up to ~130 calls a day.
        config.AI_VETO_MODE = "enforce"
        asked = self.ask(ai_analyst.APPROVE, "Fine")
        earlier = [{"event": "AI_DECISION", "symbol": f"X{i}", "side": "", "ai_decision": "VETO"}
                   for i in range(config.MAX_AI_REVIEWS_PER_DAY["stock"])]
        self.go("stock", self.tables(), log=earlier)
        asked.assert_not_called()
        self.assertEqual(self.trading.buys(), [])

    def test_failed_ai_calls_dont_use_up_the_day(self):
        # 4 Nov 2026: five 503 "high demand" errors used up the day's 5 reviews, so no buy could happen all day.
        config.AI_VETO_MODE = "enforce"
        asked = self.ask(ai_analyst.APPROVE, "Fine")
        failed = [{"event": "AI_DECISION", "symbol": f"X{i}", "side": "", "ai_decision": "VETO",
                   "ai_reason": "AI error, so no trade: 503 UNAVAILABLE. This model is currently experiencing high demand."}
                  for i in range(config.MAX_AI_REVIEWS_PER_DAY["stock"])]
        self.go("stock", self.tables(), log=failed)
        asked.assert_called_once()
        self.assertEqual(len(self.trading.buys()), 1)

    def test_one_run_still_asks_at_most_five_times(self):
        config.AI_VETO_MODE = "enforce"
        calls = self.start(mock.patch.object(ai_analyst, "_ask_gemini", side_effect=RuntimeError("503 UNAVAILABLE")))
        tables = {f"A{i}": stock_bars(pullback()) for i in range(config.MAX_AI_REVIEWS_PER_RUN["stock"] + 3)}
        self.go("stock", tables)
        self.assertEqual(calls.call_count, config.MAX_AI_REVIEWS_PER_RUN["stock"])
        self.assertEqual(self.trading.buys(), [])

    def test_five_vetoes_dont_end_the_day(self):
        # Asked for on 4 Nov 2026: if the AI says no to the first five, keep asking about the next signals.
        config.AI_VETO_MODE = "enforce"
        asked = self.ask(ai_analyst.APPROVE, "Fine")
        vetoed = [{"event": "AI_DECISION", "symbol": f"X{i}", "side": "", "ai_decision": "VETO", "ai_reason": "Too choppy"}
                  for i in range(5)]
        self.go("stock", self.tables(), log=vetoed)
        asked.assert_called_once()
        self.assertEqual(len(self.trading.buys()), 1)

    def test_the_trade_cap_still_holds_after_vetoes(self):
        config.AI_VETO_MODE = "enforce"
        asked = self.ask(ai_analyst.APPROVE, "Fine")
        earlier = [{"event": "ORDER", "symbol": f"B{i}", "side": "buy"} for i in range(config.MAX_TRADES_PER_DAY)]
        earlier += [{"event": "AI_DECISION", "symbol": f"X{i}", "side": "", "ai_decision": "VETO", "ai_reason": "No"}
                    for i in range(5)]
        self.go("stock", self.tables(), log=earlier)
        asked.assert_not_called()
        self.assertEqual(self.trading.buys(), [])

    def test_a_symbol_the_ai_said_no_to_is_not_asked_again_today(self):
        config.AI_VETO_MODE = "enforce"
        asked = self.ask(ai_analyst.APPROVE, "Fine")
        vetoed = [{"event": "AI_DECISION", "symbol": "AAA", "side": "", "ai_decision": "VETO", "ai_reason": "Too choppy"}]
        self.go("stock", self.tables(), log=vetoed)
        asked.assert_not_called()
        self.assertEqual(self.trading.buys(), [])

    def test_a_symbol_whose_ai_call_failed_is_asked_again(self):
        config.AI_VETO_MODE = "enforce"
        asked = self.ask(ai_analyst.APPROVE, "Fine")
        failed = [{"event": "AI_DECISION", "symbol": "AAA", "side": "", "ai_decision": "VETO",
                   "ai_reason": "AI error, so no trade: 503 UNAVAILABLE"}]
        self.go("stock", self.tables(), log=failed)
        asked.assert_called_once()
        self.assertEqual(len(self.trading.buys()), 1)

    def test_a_busy_gemini_is_asked_again_with_the_backup_model(self):
        class Busy(Exception):
            code = 503
        answers = [Busy("503 UNAVAILABLE high demand"), "answer"]
        asked, waits = [], []

        def send(model):
            asked.append(model)
            item = answers.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        self.assertEqual(ai_analyst.with_retries(send, sleep=waits.append), ("answer", config.GEMINI_BACKUP_MODELS[0]))
        self.assertEqual(asked, [config.GEMINI_MODEL, config.GEMINI_BACKUP_MODELS[0]])
        self.assertEqual(waits, [ai_analyst.RETRY_WAITS[0]])

    def test_the_backups_are_other_models(self):
        self.assertTrue(config.GEMINI_BACKUP_MODELS)
        self.assertNotIn(config.GEMINI_MODEL, config.GEMINI_BACKUP_MODELS)
        self.assertEqual(ai_analyst.models_to_try(), [config.GEMINI_MODEL, *config.GEMINI_BACKUP_MODELS][:3])

    def test_three_tries_even_with_one_backup_or_none(self):
        with mock.patch.object(config, "GEMINI_BACKUP_MODELS", ("b",)):
            self.assertEqual(ai_analyst.models_to_try(), [config.GEMINI_MODEL, "b", "b"])
        with mock.patch.object(config, "GEMINI_BACKUP_MODELS", ()):
            self.assertEqual(ai_analyst.models_to_try(), [config.GEMINI_MODEL] * 3)

    def test_a_gemini_that_stays_busy_gives_up_after_three_tries(self):
        asked, waits = [], []

        def send(model):
            asked.append(model)
            raise RuntimeError("503 UNAVAILABLE")

        with self.assertRaises(RuntimeError):
            ai_analyst.with_retries(send, sleep=waits.append)
        self.assertEqual(asked, ai_analyst.models_to_try())
        self.assertEqual(waits, list(ai_analyst.RETRY_WAITS))

    def test_a_bad_key_is_not_retried(self):
        class BadKey(Exception):
            code = 400
        asked, waits = [], []

        def send(model):
            asked.append(model)
            raise BadKey("API key not valid")

        with self.assertRaises(BadKey):
            ai_analyst.with_retries(send, sleep=waits.append)
        self.assertEqual((asked, waits), ([config.GEMINI_MODEL], []))

    def test_the_client_stays_open_for_the_whole_call(self):
        # 4 Nov 2026: the review built the client inline, and it was closed before its request went out.
        from google import genai
        live = {"open": True}

        class Models:
            def generate_content(self, model, contents, config):
                if not live["open"]:
                    raise RuntimeError("Cannot send a request, as the client has been closed.")
                return SimpleNamespace(text="fine")

        class Client:
            def __init__(self, api_key):
                self.models = Models()

            def __del__(self):
                live["open"] = False

        self.start(mock.patch.object(genai, "Client", Client))
        response, model = ai_analyst.generate("hello")
        self.assertEqual((response.text, model), ("fine", config.GEMINI_MODEL))
        live["open"] = True
        import review
        self.assertEqual(review.explain({"fallback": "numbers only", "data": {}}), "fine")

    def test_a_shadow_error_never_says_the_trade_was_stopped(self):
        config.AI_VETO_MODE = "shadow"
        self.start(mock.patch.object(ai_analyst, "_ask_gemini", side_effect=RuntimeError("quota")))
        decision, reason = ai_analyst.review("AAA", "BUY", "dip", stock_bars(pullback()))
        self.assertEqual(decision, ai_analyst.VETO)
        self.assertIn("trade not blocked", reason)
        self.assertNotIn("no trade", reason)
        self.assertFalse(ai_analyst.is_real_veto(decision, reason))

    def test_without_a_key_shadow_mode_doesnt_ask_and_the_trade_goes_ahead(self):
        config.AI_VETO_MODE = "shadow"
        os.environ.pop("GEMINI_API_KEY")
        asked = self.ask(ai_analyst.APPROVE, "Fine")
        self.go("stock", self.tables())
        self.assertEqual(len(self.trading.buys()), 1)
        asked.assert_not_called()
        order = next(e for e in self.events if e.get("event") == "ORDER")
        self.assertEqual(order["ai_decision"], ai_analyst.OFF)
        self.assertFalse(any(e.get("event") == "AI_DECISION" for e in self.events))

    def test_without_a_key_enforce_mode_still_blocks(self):
        config.AI_VETO_MODE = "enforce"
        os.environ.pop("GEMINI_API_KEY")
        self.go("stock", self.tables())
        self.assertEqual(self.trading.buys(), [])

    def test_the_trades_page_only_shows_vetoes_that_blocked_a_trade(self):
        veto = {"event": "AI_DECISION", "symbol": "AAA", "time_utc": "2026-11-03 14:31:00", "ai_decision": "VETO",
                "ai_reason": "Shaky", "details": "sleeve=rsi2_stocks; dip"}
        order = {"event": "ORDER", "side": "buy", "symbol": "AAA", "time_utc": "2026-11-03 14:31:01",
                 "ai_decision": "VETO", "ai_reason": "Shaky", "details": "sleeve=rsi2_stocks; dip"}
        config.AI_VETO_MODE = "shadow"
        self.assertEqual([row["event"] for row in dashboard.trade_rows([veto, order])], ["ORDER"])
        self.assertEqual(dashboard.trade_rows([veto]), [])
        config.AI_VETO_MODE = "enforce"
        self.assertEqual([row["event"] for row in dashboard.trade_rows([veto])], ["AI_DECISION"])
        self.assertEqual([row["event"] for row in dashboard.trade_rows([veto, order])], ["ORDER"])

    def test_only_a_real_veto_counts_as_one(self):
        self.assertTrue(ai_analyst.is_real_veto(ai_analyst.VETO, "Looks shaky"))
        self.assertFalse(ai_analyst.is_real_veto(ai_analyst.VETO, "AI error, so no trade: quota"))
        self.assertFalse(ai_analyst.is_real_veto(ai_analyst.VETO, "AI gave an unclear answer ('MAYBE'), so no trade"))
        self.assertFalse(ai_analyst.is_real_veto(ai_analyst.APPROVE, "Fine"))

    def test_the_prompt_names_the_real_strategy(self):
        table = pd.DataFrame({"close": [100.0, 101.0]}, index=pd.bdate_range("2026-10-29", periods=2))
        prompt = ai_analyst._build_prompt("AAA", "BUY", "dip", table, sleeves.RULE_TEXT["rsi2_stocks"])
        self.assertIn("RSI", prompt)
        self.assertNotIn("crossover", prompt)


# ---------------------------------------------------------------------------
# The journal, the scoreboard and drift
# ---------------------------------------------------------------------------
class JournalTests(unittest.TestCase):
    def test_a_buy_and_a_sell_make_one_finished_trade(self):
        book = journal.update(None, [filled("AAA", "buy", 10, 100, "2026-11-03T14:31:00Z", "tach-rsi2_stocks-buy-a", "b"),
                                     filled("AAA", "sell", 10, 110, "2026-11-05T14:31:00Z", "tach-rsi2_stocks-sell-a", "s")])
        trade = book["trades"][0]
        self.assertEqual((trade["symbol"], trade["sleeve"], trade["days"], trade["pnl"]), ("AAA", "rsi2_stocks", 2, 100.0))
        self.assertAlmostEqual(trade["ret"], 0.10)
        self.assertEqual(book["lots"], {})

    def test_two_fills_average_the_entry(self):
        book = journal.update(None, [filled("AAA", "buy", 1, 100, "2026-11-03T14:31:00Z", "", "b1"),
                                     filled("AAA", "buy", 1, 110, "2026-11-03T14:32:00Z", "", "b2"),
                                     filled("AAA", "sell", 2, 120, "2026-11-04T14:31:00Z", "", "s")])
        self.assertEqual(book["trades"][0]["entry"], 105.0)

    def test_an_order_is_only_counted_once(self):
        orders = [filled("AAA", "buy", 1, 100, "2026-11-03T14:31:00Z", "", "b"),
                  filled("AAA", "sell", 1, 120, "2026-11-04T14:31:00Z", "", "s")]
        self.assertEqual(len(journal.update(journal.update(None, orders), orders)["trades"]), 1)

    def test_a_stop_that_filled_at_alpaca_is_marked(self):
        book = journal.update(None, [filled("AAA", "buy", 1, 100, "2026-11-03T14:31:00Z", "tach-rsi2_stocks-buy-a", "b"),
                                     filled("AAA", "sell", 1, 93, "2026-11-04T14:31:00Z", "tach-rsi2_stocks-stop-a", "s")])
        self.assertTrue(book["trades"][0]["stop"])

    def test_a_sell_with_no_known_buy_is_skipped(self):
        self.assertEqual(journal.update(None, [filled("AAA", "sell", 1, 93, "2026-11-04T14:31:00Z", "", "s")])["trades"], [])

    def test_orders_from_before_the_journal_began_are_ignored(self):
        self.assertEqual(journal.update(None, [filled("AAA", "buy", 1, 93, "2026-10-01T14:31:00Z", "", "b")])["lots"], {})

    def test_the_scoreboard_tracks_what_the_ai_would_have_vetoed(self):
        trades = [{"entry_order": "a", "pnl": -50.0, "ret": -0.05}, {"entry_order": "b", "pnl": 80.0, "ret": 0.08}]
        log = [{"event": "ORDER", "side": "buy", "order_id": "a", "ai_decision": "VETO", "ai_reason": "Shaky"},
               {"event": "ORDER", "side": "buy", "order_id": "b", "ai_decision": "APPROVE", "ai_reason": "Fine"}]
        board = journal.scoreboard(trades, log)
        self.assertEqual((board["would_veto"]["trades"], board["would_veto"]["pnl"]), (1, -50.0))
        self.assertEqual((board["approved"]["trades"], board["approved"]["win_rate"]), (1, 1.0))

    def test_ai_errors_dont_count_against_it(self):
        trades = [{"entry_order": "a", "pnl": -50.0, "ret": -0.05}]
        log = [{"event": "ORDER", "side": "buy", "order_id": "a", "ai_decision": "VETO", "ai_reason": "AI error, so no trade: x"},
               {"event": "AI_DECISION", "ai_decision": "VETO", "ai_reason": "AI error, so no trade: x"}]
        board = journal.scoreboard(trades, log)
        self.assertEqual((board["would_veto"]["trades"], board["ai_errors"]), (0, 1))


class DriftTests(unittest.TestCase):
    LIMITS = {"expectancy_low": 0.0, "expectancy_high": 0.02, "win_low": 0.4, "win_high": 0.8, "trades": 100}

    def trades(self, count, ret):
        return [{"sleeve": "rsi2_stocks", "ret": ret, "pnl": ret * 1000} for _ in range(count)]

    def test_under_twenty_trades_it_only_waits(self):
        self.assertEqual(drift.compare(self.trades(19, -0.5), {"rsi2_stocks": self.LIMITS})[0]["status"], "waiting")

    def test_inside_the_range_is_fine(self):
        trades = self.trades(12, 0.02) + self.trades(8, -0.01)
        self.assertEqual(drift.compare(trades, {"rsi2_stocks": self.LIMITS})[0]["status"], "ok")

    def test_outside_the_range_is_flagged(self):
        row = drift.compare(self.trades(20, -0.01), {"rsi2_stocks": self.LIMITS})[0]
        self.assertEqual(row["status"], "outside")
        self.assertEqual(len(row["problems"]), 2)

    def test_it_warns_once_a_day_and_changes_nothing_else(self):
        rows = drift.compare(self.trades(20, -0.01), {"rsi2_stocks": self.LIMITS})
        with mock.patch.object(notifier, "send", return_value=True) as send:
            first = drift.warn(rows, {}, TODAY)
            second = drift.warn(rows, first, TODAY)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(second, {"rsi2_stocks": TODAY})

    def test_a_sleeve_without_a_tested_range_only_waits(self):
        self.assertEqual(drift.compare(self.trades(30, 0.1), {"rsi2_stocks": None})[0]["status"], "waiting")


# ---------------------------------------------------------------------------
# The A/B rule
# ---------------------------------------------------------------------------
class AbRuleTests(unittest.TestCase):
    def side(self, sharpe, drawdown):
        return {"sharpe": sharpe, "max_drawdown": drawdown}

    def test_a_clear_win_replaces_the_default(self):
        self.assertEqual(strategies.ab_winner(self.side(1.0, -0.15), self.side(1.2, -0.15)), "challenger")

    def test_an_edge_that_is_too_small_keeps_the_default(self):
        self.assertEqual(strategies.ab_winner(self.side(1.0, -0.15), self.side(1.09, -0.10)), "default")

    def test_exactly_ten_hundredths_is_enough(self):
        self.assertEqual(strategies.ab_winner(self.side(1.0, -0.15), self.side(1.1, -0.15)), "challenger")

    def test_a_much_worse_drawdown_keeps_the_default(self):
        self.assertEqual(strategies.ab_winner(self.side(1.0, -0.15), self.side(2.0, -0.18)), "default")

    def test_exactly_two_points_worse_is_allowed(self):
        self.assertEqual(strategies.ab_winner(self.side(1.0, -0.15), self.side(1.2, -0.17)), "challenger")

    def test_missing_numbers_keep_the_default(self):
        self.assertEqual(strategies.ab_winner(self.side(1.0, -0.15), self.side(float("nan"), -0.1)), "default")

    def test_a_tie_keeps_the_default(self):
        self.assertEqual(strategies.ab_winner(self.side(1.0, -0.15), self.side(1.0, -0.15)), "default")


# ---------------------------------------------------------------------------
# The backtest
# ---------------------------------------------------------------------------
def month_tables(days=600):
    index = pd.bdate_range("2023-01-02", periods=days)
    rng = np.random.default_rng(7)
    tables = {}
    for symbol in sorted(set(config.ROTATION_SYMBOLS + config.IBS_SYMBOLS + ["IVV", "AAA", "BBB"])):
        closes = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.012, days)))
        opens = closes * (1 + rng.normal(0, 0.004, days))
        tables[symbol] = pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) * (1 + abs(rng.normal(0, 0.006, days))),
                                       "low": np.minimum(opens, closes) * (1 - abs(rng.normal(0, 0.006, days))),
                                       "close": closes, "volume": 1000.0}, index=index)
    return tables


class BacktestSleeveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = month_tables()
        cls.panel = backtest.build_panel(cls.tables, not_scanned=("DBC",))

    def run_sleeves(self, *sleeves_on, **changes):
        return backtest.simulate(self.panel, backtest.Options(tuple(sleeves_on), atr_stops=False, **changes))

    def test_changing_the_future_cannot_change_the_past_for_the_new_sleeves(self):
        cut = len(self.panel.dates) * 3 // 4
        cut_day = self.panel.dates[cut]
        changed = {}
        for symbol, table in self.tables.items():
            copy = table.copy()
            copy.loc[copy.index > cut_day, ["open", "high", "low", "close"]] *= 3.7
            changed[symbol] = copy
        other = backtest.build_panel(changed, not_scanned=("DBC",))
        for options in (backtest.Options(("ibs_etfs",), atr_stops=False), backtest.Options(("turn_of_month",), atr_stops=False),
                        backtest.Options(("rotation",), atr_stops=False), backtest.Options(quant_lab.MIX, atr_stops=False),
                        backtest.Options(("momentum_stocks",), atr_stops=False, vol_scaled=True),
                        backtest.Options(quant_lab.MIX, atr_stops=False, vol_sizing=True)):
            with self.subTest(sleeves=options.sleeves):
                first, second = backtest.simulate(self.panel, options), backtest.simulate(other, options)
                self.assertGreater(len(first["trades"]), 0)
                np.testing.assert_array_equal(first["equity"].to_numpy()[:cut + 1], second["equity"].to_numpy()[:cut + 1])

    def test_turn_of_the_month_holds_from_the_last_day_to_the_fourth(self):
        dates = self.panel.dates
        for _, trade in self.run_sleeves("turn_of_month")["trades"].iterrows():
            if trade["reason"] == "stop":
                continue
            entry, leaving = int(trade["entry"]), int(trade["exit"])
            self.assertNotEqual(dates[entry].month, dates[entry + 1].month)
            self.assertEqual(strategies.trading_day_of_month(dates, leaving), 4)
            self.assertEqual(trade["symbol"], "IVV")

    def test_ibs_only_trades_its_list_and_holds_five_days_at_most(self):
        trades = self.run_sleeves("ibs_etfs")["trades"]
        self.assertGreater(len(trades), 10)
        self.assertTrue(set(trades["symbol"]) <= set(config.IBS_SYMBOLS))
        self.assertLessEqual(trades["days"].max(), 5)

    def test_rotation_holds_at_most_three_and_only_from_its_list(self):
        result = self.run_sleeves("rotation")
        self.assertTrue(set(result["trades"]["symbol"]) <= set(config.ROTATION_SYMBOLS))
        self.assertLessEqual((result["exposure"]["rotation"] / result["equity"]).max(), 3 * config.MAX_POSITION_PCT + 0.05)

    def test_a_symbol_only_one_sleeve_follows_stays_out_of_the_scans(self):
        trades = self.run_sleeves("rsi2_stocks", "momentum_stocks")["trades"]
        self.assertNotIn("DBC", set(trades["symbol"]))

    def test_ivv_stays_out_of_every_other_sleeve(self):
        trades = self.run_sleeves(*quant_lab.MIX)["trades"]
        self.assertEqual(set(trades[trades["symbol"] == "IVV"]["sleeve"]), {"turn_of_month"})

    def test_each_sleeve_stays_inside_its_share(self):
        result = self.run_sleeves(*quant_lab.MIX)
        shares = strategies.split_by_horizon(quant_lab.MIX)
        for sleeve, share in shares.items():
            self.assertLessEqual((result["exposure"][sleeve] / result["equity"]).max(), share + 0.03, sleeve)

    def test_no_symbol_is_held_twice_in_the_mix(self):
        trades = self.run_sleeves(*quant_lab.MIX)["trades"]
        for symbol, rows in trades.sort_values("entry").groupby("symbol"):
            self.assertTrue((rows["entry"].to_numpy()[1:] >= rows["exit"].to_numpy()[:-1]).all(), symbol)

    def test_ownership_skips_are_counted_per_sleeve(self):
        self.assertEqual(set(self.run_sleeves(*quant_lab.MIX)["skips"]), set(quant_lab.MIX))

    def test_vol_scaling_only_ever_shrinks_momentum(self):
        plain, scaled = self.run_sleeves("momentum_stocks"), self.run_sleeves("momentum_stocks", vol_scaled=True)
        self.assertLessEqual(scaled["exposure"]["momentum_stocks"].max(), plain["exposure"]["momentum_stocks"].max() * 1.05)

    def test_vol_sizing_changes_the_portfolio(self):
        plain, sized = self.run_sleeves(*quant_lab.MIX), self.run_sleeves(*quant_lab.MIX, vol_sizing=True)
        self.assertNotEqual(plain["equity"].iloc[-1], sized["equity"].iloc[-1])

    def test_the_close_to_close_reference_trades_on_the_signal_day(self):
        at_close = self.run_sleeves("ibs_etfs", ibs_at_close=True)["trades"]
        next_open = self.run_sleeves("ibs_etfs")["trades"]
        self.assertGreater(len(at_close), 0)
        self.assertNotEqual(at_close["entry"].iloc[0], next_open["entry"].iloc[0])

    def test_nearby_settings_change_the_trades(self):
        standard = self.run_sleeves("ibs_etfs")["trades"]
        nearby = self.run_sleeves("ibs_etfs", params={"ibs_entry": 0.25})["trades"]
        self.assertGreater(len(nearby), len(standard))


class StopFillTests(unittest.TestCase):
    BARS = [(100, 101, 99, 100), (100, 100, 92, 94), (94, 95, 91, 92), (92, 93, 90, 91)]

    def test_alpaca_fills_at_the_stop_when_a_bar_trades_through(self):
        self.assertEqual(strategies.intraday_stop_fill(self.BARS, 93, "alpaca"), 93)

    def test_alpaca_fills_at_the_open_if_the_price_gapped_below(self):
        self.assertEqual(strategies.intraday_stop_fill([(90, 91, 89, 90)], 93, "alpaca"), 90)

    def test_the_check_waits_for_a_bar_to_close_below(self):
        self.assertEqual(strategies.intraday_stop_fill(self.BARS, 93, "check"), 92)

    def test_a_dip_that_recovers_only_fills_the_alpaca_stop(self):
        recovered = [(100, 100, 92, 96), (96, 99, 95, 98)]
        self.assertEqual(strategies.intraday_stop_fill(recovered, 93, "alpaca"), 93)
        self.assertIsNone(strategies.intraday_stop_fill(recovered, 93, "check"))

    def test_the_two_versions_differ_in_a_backtest(self):
        days = pd.bdate_range("2024-01-01", periods=300)
        closes = rising(280)
        for _ in range(3):
            closes.append(closes[-1] * 0.96)
        closes += [closes[-1]] * 17
        table = shaped(closes, days)
        table.iloc[284, table.columns.get_loc("low")] = closes[283] * 0.90        # a dip past 7% that recovers
        panel = backtest.build_panel({"AAA": table, "SPY": shaped(rising(), days)})
        dip = np.array([(closes[283], closes[283], closes[283] * 0.9, closes[283])])

        def bars(symbol, day):
            return dip if day == days[284] else np.empty((0, 4))

        def reason(mode):
            options = backtest.Options(("rsi2_stocks",), atr_stops=False, stop_mode=mode, stop_bars=bars)
            return backtest.simulate(panel, options)["trades"].iloc[0]["reason"]
        self.assertEqual(reason("alpaca"), "stop")
        self.assertEqual(reason("check"), "signal")


class LabTests(unittest.TestCase):
    def test_max_drawdown(self):
        self.assertAlmostEqual(quant_lab.max_drawdown([100, 120, 90, 130]), -0.25)

    def equity(self):
        days = pd.bdate_range("2024-01-01", periods=5)
        return pd.Series([100.0, 110.0, 99.0, 108.9, 119.79], index=days)

    def trades(self):
        return pd.DataFrame({"pnl": [30.0, -10.0, 20.0, -10.0], "ret": [0.03, -0.01, 0.02, -0.01], "win": [True, False, True, False],
                             "days": [2, 4, 2, 4], "fee": [1.0, 1.0, 1.0, 1.0]})

    def test_expectancy_is_the_average_result_after_costs(self):
        self.assertAlmostEqual(quant_lab.metrics(self.equity(), self.trades())["expectancy"], 7.5)

    def test_profit_factor_is_wins_over_losses(self):
        self.assertAlmostEqual(quant_lab.metrics(self.equity(), self.trades())["profit_factor"], 2.5)

    def test_win_rate_and_days(self):
        row = quant_lab.metrics(self.equity(), self.trades())
        self.assertEqual((row["win_rate"], row["avg_days"]), (0.5, 3.0))

    def test_time_invested_counts_days_with_money_in(self):
        row = quant_lab.metrics(self.equity(), self.trades(), pd.Series([0, 100.0, 100.0, 0, 0]))
        self.assertAlmostEqual(row["time_invested"], 0.4)

    def test_sortino_only_counts_falling_days(self):
        row = quant_lab.metrics(self.equity(), self.trades())
        self.assertGreater(row["sortino"], row["sharpe"])

    def test_no_trades_gives_empty_numbers_not_a_crash(self):
        empty = pd.DataFrame(columns=["pnl", "ret", "win", "days", "fee"])
        self.assertIsNone(quant_lab.metrics(self.equity(), empty)["expectancy"])

    def test_monte_carlo_keeps_the_order_of_its_percentiles_and_repeats(self):
        trades = np.array([0.02, -0.03, 0.01, -0.02, 0.04, -0.01] * 10)
        first = quant_lab.monte_carlo(trades, runs=200)
        self.assertLessEqual(first["p5"], first["p50"])
        self.assertLessEqual(first["p50"], first["p95"])
        self.assertEqual(first, quant_lab.monte_carlo(trades, runs=200))

    def test_monte_carlo_needs_at_least_two_trades(self):
        self.assertIsNone(quant_lab.monte_carlo([0.01]))

    def test_the_drift_range_needs_twenty_trades(self):
        self.assertIsNone(quant_lab.drift_range([0.01] * 19))

    def test_the_drift_range_brackets_the_trades(self):
        limits = quant_lab.drift_range([0.01, -0.01] * 30)
        self.assertLessEqual(limits["expectancy_low"], limits["expectancy_high"])
        self.assertAlmostEqual(limits["win_low"], 0.5)

    def test_a_curve_is_thinned_and_rebased_to_100(self):
        series = pd.Series(np.arange(1.0, 1001.0), index=pd.date_range("2020-01-01", periods=1000))
        curve = quant_lab.thin(series, 50)
        self.assertLess(len(curve), 60)
        self.assertEqual(curve[0][1], 100.0)
        self.assertEqual(curve[-1][1], 100_000.0)

    def test_nan_becomes_none_so_the_file_is_valid_json(self):
        self.assertEqual(json.dumps(quant_lab.clean({"a": float("nan"), "b": np.float64(1.5), "c": [np.int64(2)]})),
                         '{"a": null, "b": 1.5, "c": [2]}')


class RealtimeBacktestTests(unittest.TestCase):
    """The engine's rules in the Python test, on small hand-made days of 5-minute bars."""

    def day(self, date, closes, lows=None, opens=None):
        closes = np.asarray(closes, dtype=float)
        opens = closes if opens is None else np.asarray(opens, dtype=float)
        lows = np.minimum(opens, closes) - 0.05 if lows is None else np.asarray(lows, dtype=float)
        index = pd.date_range(f"{date} 09:30", periods=len(closes), freq="5min")
        table = pd.DataFrame({"open": opens, "high": closes + 0.05, "low": lows, "close": closes}, index=index)
        table["minute"] = table.index.hour * 60 + table.index.minute
        return pd.Timestamp(date), table

    def flat(self):
        return self.day("2026-11-02", [100.0] * 78)

    def rise_and_stay(self):
        return [100.0] * 5 + [100.0 + i * 0.2 for i in range(1, 74)]

    def test_the_ema_matches_the_workers(self):
        np.testing.assert_allclose(rt_backtest.ema(np.array([1.0, 2.0, 3.0]), 3), [1, 1.5, 2.25])

    def test_a_cross_up_is_marked_on_its_own_bar(self):
        signals = rt_backtest.day_signals(np.full(78, 100.0), np.array(self.rise_and_stay()))
        self.assertEqual(int(np.flatnonzero(signals)[0]), 5)

    def test_the_entry_fills_at_the_next_bars_open_and_it_is_flat_by_1555(self):
        closes = self.rise_and_stay()
        opens = np.array(closes) + 0.01
        result = rt_backtest.simulate([self.flat(), self.day("2026-11-03", closes, opens=opens)])
        trade = result["trades"].iloc[0]
        self.assertEqual(trade["why"], "close")
        qty = int(100_000 * 0.25 // opens[6])
        expected = qty * (opens[77] - opens[6]) - qty * (opens[6] + opens[77]) * rt_backtest.COST_PER_SIDE    # bar 77 opens at 15:55
        self.assertAlmostEqual(trade["pnl"], expected, places=6)

    def test_the_stop_fills_two_percent_below_the_entry(self):
        closes = self.rise_and_stay()
        lows = np.array(closes) - 0.05
        lows[20] = closes[6] * 0.97
        result = rt_backtest.simulate([self.flat(), self.day("2026-11-03", closes, lows=lows)])
        self.assertEqual(result["trades"].iloc[0]["why"], "stop")
        self.assertLess(result["trades"].iloc[0]["ret"], -0.019)

    def test_after_a_stop_there_is_no_way_back_in_until_the_next_cross(self):
        closes = self.rise_and_stay()
        lows = np.array(closes) - 0.05
        lows[20] = closes[6] * 0.97
        self.assertEqual(len(rt_backtest.simulate([self.flat(), self.day("2026-11-03", closes, lows=lows)])["trades"]), 1)

    def test_no_new_entries_from_1545(self):
        closes = [100.0] * 75 + [100.2, 100.4, 100.6]          # the cross lands on the 15:45 bar
        result = rt_backtest.simulate([self.flat(), self.day("2026-11-03", closes)])
        self.assertEqual(len(result["trades"]), 0)

    def test_every_day_ends_flat(self):
        result = rt_backtest.simulate([self.flat(), self.day("2026-11-03", self.rise_and_stay()), self.day("2026-11-04", [120.0] * 78)])
        self.assertEqual(len(result["equity"]), 2)
        self.assertEqual(len(result["trades"]), 1)

    def test_a_signal_reversed_within_three_bars_is_a_whipsaw(self):
        self.assertEqual(rt_backtest.whipsaws(np.array([0, 1, 0, 0, -1, 0, 0, 0, 0, 1])), 1)


class PublicFileTests(unittest.TestCase):
    def test_history_rows_have_each_horizon_and_replace_todays_row(self):
        summary = {"sleeves": [{"name": "rsi2_stocks", "horizon": "SHORT", "value": 100.0, "open_pl": 5.0, "closed_pl": 2.0},
                               {"name": "momentum_stocks", "horizon": "LONG", "value": 50.0, "open_pl": -1.0, "closed_pl": 0.0}]}
        first = dashboard.add_history([], "2026-11-03", 1000.0, 850.0, summary)
        second = dashboard.add_history(first, "2026-11-03", 1010.0, 860.0, summary)
        self.assertEqual(len(second), 1)
        self.assertEqual((second[0]["equity"], second[0]["short"], second[0]["long"]), (1010.0, 100.0, 50.0))
        self.assertEqual(second[0]["sleeves"]["rsi2_stocks"], 7.0)

    def test_the_realtime_keys_are_blanked_like_any_other(self):
        with mock.patch.dict(os.environ, {"RT_ALPACA_SECRET_KEY": "s3cret-value"}):
            self.assertNotIn("s3cret-value", notifier.redact("oops s3cret-value"))

    def test_a_fixed_stop_sends_a_message(self):
        self.assertIn("Stop orders fixed", notifier.message_for({"event": "STOP_FIXED", "details": "AAA: placed", "symbol": ""}))


if __name__ == "__main__":
    unittest.main()
