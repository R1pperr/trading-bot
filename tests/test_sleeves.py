"""
The four sleeves: their rules on small hand-made prices, the live runner, and the backtest.

Alpaca is a fake here, so no test can reach a real service or place a real order.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd
from alpaca.trading.enums import OrderSide, OrderType, QueryOrderStatus, TimeInForce

import ai_analyst
import bot
import config
import crypto
import dashboard
import market_hours
import notifier
import risk
import sleeve_backtest as backtest
import sleeves
import strategies
import universe

TODAY = "2026-11-02"
ALL_FLAGS = list(sleeves.FLAGS.values())


# ---------------------------------------------------------------------------
# Hand-made prices
# ---------------------------------------------------------------------------
def stock_bars(closes):
    """Daily stock bars ending on Friday 30 October 2026."""
    closes = np.asarray(closes, dtype=float)
    days = pd.bdate_range(end="2026-10-30", periods=len(closes))
    return pd.DataFrame({"open": closes, "high": closes * 1.002, "low": closes * 0.998, "close": closes,
                         "volume": 1000.0}, index=days)


def crypto_bars(closes):
    closes = np.asarray(closes, dtype=float)
    days = pd.date_range(end="2026-11-01", periods=len(closes))
    return pd.DataFrame({"open": closes, "high": closes * 1.002, "low": closes * 0.998, "close": closes,
                         "volume": 1000.0}, index=days)


def rising(days=300, first=100.0, step=1.0):
    return [first + step * day for day in range(days)]


def falling(days=300, first=400.0, step=1.0):
    return [first - step * day for day in range(days)]


def pullback(days=300):
    """A long rise, then three sharp down days: still above the 200-day average, RSI near zero."""
    prices = rising(days - 3)
    for _ in range(3):
        prices.append(prices[-1] * 0.96)
    return prices


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
def fake_position(symbol, qty=10.0, entry=100.0, price=100.0):
    pair = "/" in symbol or symbol.endswith("USD")
    return SimpleNamespace(symbol=symbol, qty=str(qty), avg_entry_price=str(entry), current_price=str(price),
                           market_value=str(qty * price), asset_class="crypto" if pair else "us_equity")


def fake_order(symbol, side="buy", client_id="", created="2026-11-01T00:07:00Z", filled_qty="0"):
    return SimpleNamespace(id=f"id-{symbol}", symbol=symbol, side=side, client_order_id=client_id,
                           created_at=created, filled_at=None, filled_qty=filled_qty, status="new",
                           asset_class="crypto" if "/" in symbol else "us_equity")


class FakeTrading:
    """Remembers orders instead of sending them."""

    def __init__(self, positions=(), open_orders=(), closed_orders=(), refuse=()):
        self.positions = list(positions)
        self.open_orders = list(open_orders)
        self.closed_orders = list(closed_orders)
        self.refuse = set(refuse)
        self.submitted = []
        self.cancelled = []

    def get_all_positions(self):
        return self.positions

    def get_orders(self, filter=None):
        return self.closed_orders if filter.status == QueryOrderStatus.CLOSED else self.open_orders

    def submit_order(self, order_data):
        if order_data.symbol in self.refuse:
            raise RuntimeError("refused by the fake broker")
        self.submitted.append(order_data)
        return SimpleNamespace(id=f"order-{len(self.submitted)}", status="accepted")

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)

    def buys(self):
        return [o for o in self.submitted if o.side == OrderSide.BUY]

    def sells(self):
        return [o for o in self.submitted if o.side == OrderSide.SELL]


class SleeveCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)
        self.start(mock.patch.object(sleeves, "STATE_FILE", os.path.join(self.folder, "state", "sleeves.json")))
        self.start(mock.patch.object(config, "USE_AI_ANALYST", False))
        self.start(mock.patch.object(config, "USE_ATR_STOPS", False))
        for flag in ALL_FLAGS:
            self.start(mock.patch.object(config, flag, False))
        self.events, self.plans = [], []

    def start(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def enable(self, *flags):
        for flag in flags:
            setattr(config, flag, True)

    def own(self, **records):
        """Save a state file: symbol=sleeve, with keyword names using __ for / in pairs."""
        state = sleeves.load_state()
        for symbol, sleeve in records.items():
            state["positions"][symbol.replace("__", "/")] = {"sleeve": sleeve, "entry_date": "2026-10-26"}
        sleeves.save_state(state)

    def log_event(self, **fields):
        self.events.append(fields)

    def go(self, market, tables, trading=None, equity=100_000.0, cash=100_000.0, killed=False, dry_run=False,
           today=TODAY, log=None, listed=None):
        self.trading = trading or FakeTrading()
        rows = [{"symbol": s, "name": ""} for s in (listed if listed is not None else tables)]
        with mock.patch.object(universe, "rows", return_value=rows), contextlib.redirect_stdout(io.StringIO()):
            return sleeves.run(market, self.trading, equity, cash, killed, dry_run, today, log or [], self.log_event,
                               lambda symbols, start: {s: t for s, t in tables.items() if s in symbols},
                               lambda symbol, fallback: fallback, self.plans.append)


# ---------------------------------------------------------------------------
# Indicators, worked out by hand
# ---------------------------------------------------------------------------
class IndicatorTests(unittest.TestCase):
    def test_wilder_rsi_matches_a_worked_example(self):
        # Changes +1, -0.5, -0.5. Gains average 0.5 then 0.25; losses 0.25 then 0.375.
        values = strategies.rsi([10.0, 11.0, 10.5, 10.0], period=2)
        self.assertTrue(np.isnan(values[:2]).all())
        self.assertAlmostEqual(values[2], 100 - 100 / (1 + 0.5 / 0.25))
        self.assertAlmostEqual(values[3], 100 - 100 / (1 + 0.25 / 0.375))

    def test_rsi_is_100_when_nothing_fell_and_0_when_nothing_rose(self):
        self.assertEqual(strategies.rsi(rising(10))[-1], 100.0)
        self.assertEqual(strategies.rsi(falling(10))[-1], 0.0)

    def test_atr_of_a_steady_two_dollar_range_is_two(self):
        high, low, close = [12.0] * 30, [10.0] * 30, [11.0] * 30
        self.assertAlmostEqual(strategies.atr(high, low, close)[-1], 2.0)

    def test_atr_counts_an_overnight_gap(self):
        high = [11.0, 21.0, 21.0]
        low = [9.0, 19.0, 19.0]
        close = [10.0, 20.0, 20.0]
        self.assertEqual(strategies.atr(high, low, close, period=2)[1], (2 + 11) / 2)

    def test_momentum_skips_the_most_recent_month(self):
        closes = [100.0] * 300
        closes[-22] = 150.0
        closes[-1] = 500.0            # last month's jump must not count
        self.assertAlmostEqual(strategies.momentum(closes)[-1], 0.5)

    def test_trend_score_is_up_on_a_rise_down_on_a_fall_and_blank_while_warming_up(self):
        wobble = [0.5 if day % 2 else -0.5 for day in range(300)]
        up = strategies.trend_score(np.add(rising(), wobble))
        down = strategies.trend_score(np.add(falling(), wobble))
        self.assertGreater(up[-1], 0)
        self.assertLess(down[-1], 0)
        self.assertTrue(np.isnan(up[:90]).all())

    def test_trend_score_is_measured_in_volatility_so_price_level_does_not_matter(self):
        wobble = [0.5 if day % 2 else -0.5 for day in range(300)]
        prices = np.add(rising(), wobble)
        self.assertAlmostEqual(strategies.trend_score(prices)[-1], strategies.trend_score(prices * 10)[-1])

    def test_a_coin_that_never_moves_has_no_trend(self):
        score = strategies.trend_score([100.0] * 300)
        self.assertFalse(strategies.trend_long(score[-1]))


# ---------------------------------------------------------------------------
# Each sleeve's entry and exit rules
# ---------------------------------------------------------------------------
class Rsi2RuleTests(unittest.TestCase):
    def latest(self, closes):
        return strategies.indicators(stock_bars(closes)).iloc[-1]

    def test_enters_on_a_pullback_above_the_200_day_average(self):
        row = self.latest(pullback())
        self.assertTrue(strategies.rsi2_entry(row["close"], row["sma_trend"], row["rsi"]))

    def test_does_not_enter_below_the_200_day_average(self):
        row = self.latest(falling())
        self.assertLess(row["rsi"], config.RSI_ENTRY_BELOW)
        self.assertFalse(strategies.rsi2_entry(row["close"], row["sma_trend"], row["rsi"]))

    def test_does_not_enter_while_rsi_is_high(self):
        row = self.latest(rising())
        self.assertFalse(strategies.rsi2_entry(row["close"], row["sma_trend"], row["rsi"]))

    def test_rsi_of_exactly_10_is_not_below_10(self):
        self.assertFalse(strategies.rsi2_entry(110.0, 100.0, 10.0))
        self.assertTrue(strategies.rsi2_entry(110.0, 100.0, 9.99))

    def test_exits(self):
        self.assertTrue(strategies.rsi2_exit(106, 105, 90, 1))       # above the 5-day average
        self.assertTrue(strategies.rsi2_exit(89, 95, 90, 1))         # below the 200-day average
        self.assertTrue(strategies.rsi2_exit(100, 105, 90, 10))      # held 10 trading days
        self.assertFalse(strategies.rsi2_exit(100, 105, 90, 9))      # none of the three


class RuleRunTests(SleeveCase):
    def test_rsi_sleeve_buys_the_pullback(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.go("stock", {"AAA": stock_bars(pullback()), "BBB": stock_bars(rising())})
        self.assertEqual([o.symbol for o in self.trading.buys()], ["AAA"])

    def test_rsi_sleeve_sells_after_ten_trading_days(self):
        self.enable("ENABLE_RSI2_STOCKS")
        tail = rising(280) + [379.0 - 0.1 * day for day in range(20)]     # drifting down: no other exit fires
        table = stock_bars(tail)
        for entry_date, sold in ((table.index[-10], True), (table.index[-9], False)):
            state = sleeves.load_state()
            state["days"] = {}
            state["positions"] = {"AAA": {"sleeve": "rsi2_stocks", "entry_date": entry_date.date().isoformat()}}
            sleeves.save_state(state)
            self.go("stock", {"AAA": table}, FakeTrading([fake_position("AAA", price=370, entry=371)]))
            self.assertEqual(len(self.trading.sells()), 1 if sold else 0, entry_date)

    def test_trend_sleeve_buys_only_the_most_liquid_coins(self):
        self.enable("ENABLE_CRYPTO_TREND")
        table = crypto_bars(np.add(rising(), [0.5 if d % 2 else -0.5 for d in range(300)]))
        tables = {f"C{n}/USD": table for n in range(10)}
        with mock.patch.object(config, "CRYPTO_TREND_TOP_N", 2):
            self.go("crypto", tables)
        self.assertEqual([o.symbol for o in self.trading.buys()], ["C0/USD", "C1/USD"])

    def test_trend_sleeve_sells_when_the_signal_is_no_longer_positive(self):
        self.own(BTC__USD="crypto_trend")
        table = crypto_bars(np.add(falling(), [0.5 if d % 2 else -0.5 for d in range(300)]))
        self.go("crypto", {"BTC/USD": table}, FakeTrading([fake_position("BTCUSD")]))
        self.assertEqual([o.symbol for o in self.trading.sells()], ["BTC/USD"])

    def test_momentum_sleeve_holds_the_top_names_ranked_by_12_1_momentum(self):
        self.enable("ENABLE_MOMENTUM_STOCKS")
        tables = {f"S{n:02}": stock_bars(rising(first=1000.0 - 60 * n, step=1 + n)) for n in range(14)}
        tables["SPY"] = stock_bars(rising())
        with mock.patch.object(config, "MOMENTUM_TOP_N", 3):
            state = self.go("stock", tables)
        self.assertEqual(state["momentum"]["targets"], ["S13", "S12", "S11"])
        self.assertEqual([o.symbol for o in self.trading.buys()], ["S13", "S12", "S11"])

    def test_momentum_goes_to_cash_when_spy_closes_below_its_200_day_average(self):
        self.enable("ENABLE_MOMENTUM_STOCKS")
        self.own(AAA="momentum_stocks")
        state = sleeves.load_state()
        state["momentum"] = {"month": "2026-10", "targets": ["AAA"], "stopped": []}
        sleeves.save_state(state)
        self.go("stock", {"AAA": stock_bars(rising()), "SPY": stock_bars(falling())},
                FakeTrading([fake_position("AAA")]))
        self.assertEqual([o.symbol for o in self.trading.sells()], ["AAA"])
        self.assertEqual(self.trading.buys(), [])

    def test_momentum_sells_what_drops_out_of_the_list_at_the_monthly_rebalance(self):
        self.enable("ENABLE_MOMENTUM_STOCKS")
        self.own(OLD="momentum_stocks")
        state = sleeves.load_state()
        state["momentum"] = {"month": "2026-09", "targets": ["OLD"], "stopped": []}
        sleeves.save_state(state)
        tables = {"NEW": stock_bars(rising(step=3)), "OLD": stock_bars(rising(first=300, step=0.1)),
                  "SPY": stock_bars(rising(step=1))}
        with mock.patch.object(config, "MOMENTUM_TOP_N", 2):
            state = self.go("stock", tables, FakeTrading([fake_position("OLD")]))
        self.assertEqual(state["momentum"]["targets"], ["NEW", "SPY"])
        self.assertEqual([o.symbol for o in self.trading.sells()], ["OLD"])

    def test_momentum_ranks_only_once_a_month(self):
        self.enable("ENABLE_MOMENTUM_STOCKS")
        state = sleeves.load_state()
        state["momentum"] = {"month": "2026-10", "targets": ["KEPT"], "stopped": []}
        sleeves.save_state(state)
        state = self.go("stock", {"KEPT": stock_bars(rising()), "SPY": stock_bars(rising(step=3))})
        self.assertEqual(state["momentum"]["targets"], ["KEPT"])

    def test_a_stopped_out_momentum_stock_waits_for_the_next_rebalance(self):
        self.enable("ENABLE_MOMENTUM_STOCKS")
        self.own(AAA="momentum_stocks")
        state = sleeves.load_state()
        state["momentum"] = {"month": "2026-10", "targets": ["AAA"], "stopped": []}
        sleeves.save_state(state)
        tables = {"AAA": stock_bars(rising()), "SPY": stock_bars(rising())}
        state = self.go("stock", tables, FakeTrading([fake_position("AAA", entry=100, price=90)]), listed=["AAA"])
        self.assertEqual(state["momentum"]["stopped"], ["AAA"])
        self.assertEqual(self.trading.buys(), [])
        self.go("stock", tables, FakeTrading(), today="2026-11-03", listed=["AAA"])
        self.assertEqual(self.trading.buys(), [])

    def test_a_failed_spy_download_cannot_sell_the_momentum_sleeve(self):
        self.own(AAA="momentum_stocks")
        self.go("stock", {"AAA": stock_bars(rising())}, FakeTrading([fake_position("AAA")]))
        self.assertEqual(self.trading.sells(), [])

    def test_crypto_hold_buys_above_the_200_day_average_and_sells_below_it(self):
        self.enable("ENABLE_CRYPTO_HOLD")
        tables = {"BTC/USD": crypto_bars(rising()), "ETH/USD": crypto_bars(falling()),
                  "SOL/USD": crypto_bars(rising())}
        self.own(ETH__USD="crypto_hold")
        self.go("crypto", tables, FakeTrading([fake_position("ETHUSD")]))
        self.assertEqual([o.symbol for o in self.trading.buys()], ["BTC/USD"])
        self.assertEqual([o.symbol for o in self.trading.sells()], ["ETH/USD"])


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------
class SizingTests(SleeveCase):
    def test_stop_is_three_atr_below_the_entry(self):
        self.assertEqual(risk.atr_stop(100, 2), 94)

    def test_a_wide_stop_buys_less_so_hitting_it_loses_half_a_percent(self):
        dollars = risk.risk_sized_dollars(100_000, 100, 80, room=1_000_000)
        self.assertAlmostEqual(dollars, 2_500)
        self.assertAlmostEqual(dollars * 0.20, 100_000 * config.RISK_PER_TRADE)

    def test_a_tight_stop_is_still_held_to_the_5_percent_limit(self):
        self.assertAlmostEqual(risk.risk_sized_dollars(100_000, 100, 99, room=1_000_000), 5_000)

    def test_room_left_in_the_sleeve_limits_the_size(self):
        self.assertAlmostEqual(risk.risk_sized_dollars(100_000, 100, 90, room=700), 700)

    def test_a_stop_at_or_above_the_price_buys_nothing(self):
        self.assertEqual(risk.risk_sized_dollars(100_000, 100, 100, room=1_000), 0)

    def test_sleeves_split_their_share_into_slots(self):
        self.assertAlmostEqual(sleeves.entry_room("momentum_stocks", 100_000, 0), 1_500)
        self.assertAlmostEqual(sleeves.entry_room("crypto_trend", 100_000, 0), 3_750)
        self.assertAlmostEqual(sleeves.entry_room("crypto_hold", 100_000, 0), 2_500)
        self.assertAlmostEqual(sleeves.entry_room("rsi2_stocks", 100_000, 49_000), 1_000)

    def test_stocks_buy_whole_shares_and_crypto_buys_fractions(self):
        self.assertEqual(sleeves.units(9.99, "stock"), 9)
        self.assertEqual(sleeves.units(0.123456789, "crypto"), 0.12345678)


# ---------------------------------------------------------------------------
# Sharing the account
# ---------------------------------------------------------------------------
class OwnershipTests(SleeveCase):
    def test_a_stock_two_sleeves_want_goes_to_the_short_term_sleeve(self):
        self.enable("ENABLE_RSI2_STOCKS", "ENABLE_MOMENTUM_STOCKS")
        state = self.go("stock", {"AAA": stock_bars(pullback()), "SPY": stock_bars(rising())})
        self.assertEqual(state["positions"]["AAA"]["sleeve"], "rsi2_stocks")
        self.assertEqual(state["positions"]["SPY"]["sleeve"], "momentum_stocks")
        self.assertEqual(sorted(o.symbol for o in self.trading.buys()), ["AAA", "SPY"])

    def test_btc_wanted_by_both_crypto_sleeves_gets_one_owner(self):
        self.enable("ENABLE_CRYPTO_TREND", "ENABLE_CRYPTO_HOLD")
        table = crypto_bars(np.add(rising(), [0.5 if d % 2 else -0.5 for d in range(300)]))
        state = self.go("crypto", {"BTC/USD": table})
        self.assertEqual(state["positions"]["BTC/USD"]["sleeve"], "crypto_trend")
        self.assertEqual(len(self.trading.buys()), 1)

    def test_a_held_symbol_is_never_bought_by_another_sleeve(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.own(AAA="momentum_stocks")
        self.go("stock", {"AAA": stock_bars(pullback())}, FakeTrading([fake_position("AAA", entry=300, price=300)]))
        self.assertEqual(self.trading.buys(), [])

    def test_a_position_with_no_owner_is_never_added_to(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.go("stock", {"AAA": stock_bars(pullback())}, FakeTrading([fake_position("AAA", entry=300, price=300)]))
        self.assertEqual(self.trading.buys(), [])

    def test_an_open_order_blocks_a_second_buy(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.go("stock", {"AAA": stock_bars(pullback())}, FakeTrading(open_orders=[fake_order("AAA")]))
        self.assertEqual(self.trading.buys(), [])

    def test_a_stock_run_keeps_the_crypto_owners_and_a_crypto_run_keeps_the_stock_owners(self):
        self.own(AAA="rsi2_stocks", BTC__USD="crypto_trend")
        everything = FakeTrading([fake_position("AAA", price=101), fake_position("BTCUSD", price=101)])
        self.go("stock", {}, everything, listed=[])
        self.assertIn("BTC/USD", sleeves.load_state()["positions"])
        self.go("crypto", {}, everything, listed=[])
        self.assertIn("AAA", sleeves.load_state()["positions"])

    def test_sold_symbols_lose_their_owner(self):
        self.own(AAA="rsi2_stocks")
        state = self.go("stock", {}, FakeTrading(), listed=[])
        self.assertEqual(state["positions"], {})

    def test_a_lost_state_file_is_rebuilt_from_the_order_ids(self):
        order = fake_order("AAA", client_id=sleeves.make_order_id("rsi2_stocks", "buy"), filled_qty="10")
        order.filled_at = "2026-10-20T14:00:00Z"
        state = self.go("stock", {}, FakeTrading([fake_position("AAA", price=99)], closed_orders=[order]), listed=[])
        self.assertEqual(state["positions"]["AAA"], {"sleeve": "rsi2_stocks", "entry_date": "2026-10-20"})

    def test_order_ids_carry_the_sleeve(self):
        for name in config.SLEEVES:
            self.assertEqual(sleeves.sleeve_from_order_id(sleeves.make_order_id(name, "buy")), name)
        for other in ("", None, "abc", "tach-legacy-sell-123", "tach-unknown-buy-1"):
            self.assertEqual(sleeves.sleeve_from_order_id(other), "")

    def test_order_ids_fit_alpacas_limit(self):
        self.assertLessEqual(max(len(sleeves.make_order_id(n, "sell")) for n in config.SLEEVES), 128)

    def test_the_state_file_holds_no_price_history(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.go("stock", {"AAA": stock_bars(pullback())})
        with open(sleeves.STATE_FILE, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(set(saved), {"positions", "days", "momentum", "closed_pl"})
        self.assertEqual(set(saved["positions"]["AAA"]), {"sleeve", "entry_date", "stop"})


class AllocationTests(SleeveCase):
    def tables(self):
        return {name: stock_bars(pullback()) for name in ("AAA", "BBB", "CCC")}

    def spent(self):
        return sum(float(o.qty) * pullback()[-1] for o in self.trading.buys())

    def test_each_buy_stays_within_5_percent_of_equity(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.go("stock", self.tables())
        for order in self.trading.buys():
            self.assertLessEqual(float(order.qty) * pullback()[-1], 5_000)

    def test_buys_in_the_same_run_count_against_the_sleeve_share(self):
        self.enable("ENABLE_RSI2_STOCKS")
        with mock.patch.dict(config.SLEEVES["rsi2_stocks"], {"allocation": 0.06}):
            self.go("stock", self.tables())
        self.assertLessEqual(self.spent(), 6_000)
        self.assertEqual(len(self.trading.buys()), 2)

    def test_what_the_sleeve_already_holds_counts_too(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.own(HELD="rsi2_stocks")
        held = fake_position("HELD", qty=460, entry=100, price=100)        # $46,000 of the $50,000 share
        tables = {**self.tables(), "HELD": stock_bars([100.0] * 300)}
        self.go("stock", tables, FakeTrading([held]))
        self.assertLessEqual(self.spent(), 4_000)
        self.assertGreater(self.spent(), 0)

    def test_buys_never_spend_more_cash_than_the_account_has(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.go("stock", self.tables(), cash=6_000)
        self.assertLessEqual(self.spent(), 6_000)
        self.assertGreater(self.spent(), 0)

    def test_no_cash_means_no_buy(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.go("stock", self.tables(), cash=0)
        self.assertEqual(self.trading.buys(), [])

    def test_money_from_todays_sells_is_not_spent_today(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.own(OLD="rsi2_stocks")
        tables = {"OLD": stock_bars(rising()), "AAA": stock_bars(pullback())}
        self.go("stock", tables, FakeTrading([fake_position("OLD", qty=100, entry=300, price=301)]), cash=0)
        self.assertEqual(len(self.trading.sells()), 1)
        self.assertEqual(self.trading.buys(), [])

    def test_position_limit_applies(self):
        self.enable("ENABLE_RSI2_STOCKS")
        with mock.patch.dict(config.SLEEVE_MAX_OPEN_POSITIONS, {"stock": 1}):
            self.go("stock", self.tables())
        self.assertEqual(len(self.trading.buys()), 1)


class EnableFlagTests(SleeveCase):
    def tables(self):
        return {"AAA": stock_bars(pullback()), "SPY": stock_bars(rising())}

    def test_only_the_approved_sleeves_are_on_and_the_fixed_stop_stays(self):
        with open(os.path.join(sleeves.BASE_DIR, "config.py"), encoding="utf-8") as handle:
            source = handle.read()
        for flag in ("ENABLE_RSI2_STOCKS", "ENABLE_MOMENTUM_STOCKS"):
            self.assertRegex(source, rf"(?m)^{flag} = True\b")
        for flag in ("ENABLE_CRYPTO_TREND", "ENABLE_CRYPTO_HOLD", "USE_ATR_STOPS"):
            self.assertRegex(source, rf"(?m)^{flag} = False\b")
        self.assertRegex(source, r'(?m)^STRATEGY_MODE = "sleeves"')

    def test_with_every_flag_off_nothing_is_bought(self):
        self.go("stock", self.tables())
        self.assertEqual(self.trading.submitted, [])

    def test_each_flag_turns_on_only_its_own_sleeve(self):
        self.enable("ENABLE_MOMENTUM_STOCKS")
        state = self.go("stock", self.tables())
        self.assertEqual({r["sleeve"] for r in state["positions"].values()}, {"momentum_stocks"})

    def test_a_switched_off_sleeve_still_sells_what_it_holds(self):
        self.own(AAA="rsi2_stocks")
        self.go("stock", {"AAA": stock_bars(rising())}, FakeTrading([fake_position("AAA", entry=390, price=395)]))
        self.assertEqual([o.symbol for o in self.trading.sells()], ["AAA"])


class DailyCapTests(SleeveCase):
    def test_the_daily_limit_fills_short_term_sleeves_first(self):
        self.enable("ENABLE_RSI2_STOCKS", "ENABLE_MOMENTUM_STOCKS")
        tables = {"SPY": stock_bars(rising()), "MOM": stock_bars(rising(step=4))}
        names = [f"RSI{number}" for number in range(config.MAX_TRADES_PER_DAY + 1)]
        tables.update({name: stock_bars(pullback()) for name in names})
        state = self.go("stock", tables)
        self.assertEqual(len(self.trading.buys()), config.MAX_TRADES_PER_DAY)
        self.assertEqual({r["sleeve"] for r in state["positions"].values()}, {"rsi2_stocks"})

    def test_momentum_builds_up_on_later_days(self):
        self.enable("ENABLE_MOMENTUM_STOCKS")
        tables = {f"S{n}": stock_bars(rising(step=0.5 + n / 5)) for n in range(config.MAX_TRADES_PER_DAY)}
        tables["SPY"] = stock_bars(rising())
        self.go("stock", tables)
        self.assertEqual(len(self.trading.buys()), config.MAX_TRADES_PER_DAY)
        held = [fake_position(o.symbol, entry=100, price=100) for o in self.trading.buys()]
        self.go("stock", tables, FakeTrading(held), today="2026-11-03")
        self.assertEqual(len(self.trading.buys()), 1)
        self.assertFalse({o.symbol for o in self.trading.buys()} & {p.symbol for p in held})

    def test_exits_are_not_held_back_by_the_cap(self):
        self.enable("ENABLE_RSI2_STOCKS")
        names = ["A1", "A2", "A3", "A4", "A5"]
        self.own(**{name: "rsi2_stocks" for name in names})
        tables = {name: stock_bars(rising()) for name in names}
        held = [fake_position(name, entry=390, price=395) for name in names]
        full_log = [{"event": "ORDER", "side": "buy", "symbol": f"X{n}", "ai_decision": ""}
                    for n in range(config.MAX_TRADES_PER_DAY)]
        self.go("stock", tables, FakeTrading(held), log=full_log)
        self.assertEqual(len(self.trading.sells()), 5)

    def test_todays_earlier_entries_use_up_the_cap(self):
        self.enable("ENABLE_RSI2_STOCKS")
        log = [{"event": "ORDER", "side": "buy", "symbol": f"X{n}", "ai_decision": ""}
               for n in range(config.MAX_TRADES_PER_DAY - 1)]
        self.go("stock", {name: stock_bars(pullback()) for name in ("AAA", "BBB", "CCC")}, log=log)
        self.assertEqual(len(self.trading.buys()), 1)

    def test_crypto_buys_do_not_use_up_the_stock_cap(self):
        self.enable("ENABLE_RSI2_STOCKS")
        log = [{"event": "ORDER", "side": "buy", "symbol": f"C{n}/USD", "ai_decision": ""}
               for n in range(config.MAX_TRADES_PER_DAY)]
        self.go("stock", {"AAA": stock_bars(pullback())}, log=log)
        self.assertEqual(len(self.trading.buys()), 1)

    def test_a_symbol_is_not_traded_twice_in_one_day(self):
        self.enable("ENABLE_RSI2_STOCKS")
        log = [{"event": "ORDER", "side": "sell", "symbol": "AAA", "ai_decision": ""}]
        self.go("stock", {"AAA": stock_bars(pullback())}, log=log)
        self.assertEqual(self.trading.buys(), [])

    def test_a_symbol_sold_this_run_is_not_bought_back(self):
        self.enable("ENABLE_RSI2_STOCKS")
        table = stock_bars(pullback())
        self.go("stock", {"AAA": table}, FakeTrading([fake_position("AAA", entry=400, price=300)]))
        self.assertEqual(len(self.trading.sells()), 1)
        self.assertEqual(self.trading.buys(), [])


class RunRhythmTests(SleeveCase):
    def test_kill_switch_stops_entries_but_not_exits(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.own(OLD="rsi2_stocks")
        tables = {"OLD": stock_bars(rising()), "AAA": stock_bars(pullback())}
        self.go("stock", tables, FakeTrading([fake_position("OLD", entry=390, price=395)]), killed=True)
        self.assertEqual(self.trading.buys(), [])
        self.assertEqual(len(self.trading.sells()), 1)

    def test_signals_run_once_a_day_and_stops_on_every_run(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.go("stock", {"AAA": stock_bars(pullback())})
        self.assertEqual(len(self.trading.buys()), 1)
        downloads = mock.Mock(side_effect=AssertionError("no download on a later run"))
        trading = FakeTrading([fake_position("AAA", entry=100, price=90)])
        with mock.patch.object(universe, "rows", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            sleeves.run("stock", trading, 100_000.0, 100_000.0, False, False, TODAY, [], self.log_event,
                        downloads, lambda s, f: f, self.plans.append)
        self.assertEqual(trading.buys(), [])
        self.assertEqual([o.symbol for o in trading.sells()], ["AAA"])
        downloads.assert_not_called()

    def test_a_dry_run_places_nothing_and_saves_nothing(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.own(OLD="rsi2_stocks")
        before = sleeves.load_state()
        tables = {"OLD": stock_bars(rising()), "AAA": stock_bars(pullback())}
        self.go("stock", tables, FakeTrading([fake_position("OLD", entry=500, price=390)]), dry_run=True)
        self.assertEqual(self.trading.submitted, [])
        self.assertEqual(self.events, [])
        self.assertEqual(self.plans, [])
        self.assertEqual(sleeves.load_state(), before)

    def test_no_prices_means_the_next_run_tries_again(self):
        self.enable("ENABLE_RSI2_STOCKS")
        state = self.go("stock", {}, listed=[])
        self.assertNotIn("stock", state["days"])

    def test_the_ai_veto_stops_an_entry(self):
        self.enable("ENABLE_RSI2_STOCKS")
        with mock.patch.object(ai_analyst, "review", return_value=(ai_analyst.VETO, "looks weak")) as review:
            self.go("stock", {"AAA": stock_bars(pullback())})
        review.assert_called_once()
        self.assertEqual(self.trading.buys(), [])
        self.assertEqual(self.plans[0]["candidates"][0]["status"], "vetoed")

    def test_ai_calls_stay_within_the_daily_cap(self):
        self.enable("ENABLE_RSI2_STOCKS")
        tables = {f"S{n}": stock_bars(pullback()) for n in range(12)}
        with mock.patch.object(ai_analyst, "review", return_value=(ai_analyst.VETO, "no")) as review:
            self.go("stock", tables)
        self.assertEqual(review.call_count, config.MAX_AI_REVIEWS_PER_DAY["stock"])

    def test_exits_never_ask_the_ai(self):
        self.own(OLD="rsi2_stocks")
        with mock.patch.object(ai_analyst, "review") as review:
            self.go("stock", {"OLD": stock_bars(rising())}, FakeTrading([fake_position("OLD", entry=390, price=395)]))
        review.assert_not_called()

    def test_a_refused_sell_does_not_block_the_other_stops(self):
        self.own(BAD="rsi2_stocks", OK="rsi2_stocks")
        held = [fake_position("BAD", entry=100, price=80), fake_position("OK", entry=100, price=80)]
        state = self.go("stock", {}, FakeTrading(held, refuse={"BAD"}), listed=[])
        self.assertEqual([o.symbol for o in self.trading.sells()], ["OK"])
        self.assertIn("BAD", state["positions"])
        self.assertEqual([e["event"] for e in self.events], ["ERROR", "ORDER"])

    def test_a_pending_sell_is_not_sent_again(self):
        self.own(AAA="rsi2_stocks")
        trading = FakeTrading([fake_position("AAA", entry=100, price=80)], [fake_order("AAA", side="sell")])
        self.go("stock", {}, trading, listed=[])
        self.assertEqual(self.trading.sells(), [])

    def test_a_blank_price_is_not_read_as_a_crash(self):
        self.own(AAA="rsi2_stocks")
        position = fake_position("AAA")
        position.current_price = None
        self.go("stock", {}, FakeTrading([position]), listed=[])
        self.assertEqual(self.trading.sells(), [])

    def test_closed_profit_is_kept_per_sleeve(self):
        self.own(AAA="rsi2_stocks")
        state = self.go("stock", {"AAA": stock_bars(rising())},
                        FakeTrading([fake_position("AAA", qty=10, entry=390, price=395)]))
        self.assertEqual(state["closed_pl"], {"rsi2_stocks": 50.0})

    def test_candidates_for_the_public_page_hold_no_prices(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.go("stock", {"AAA": stock_bars(pullback())})
        self.assertNotIn(str(round(pullback()[-1], 2)), json.dumps(self.plans[0]))
        self.assertEqual(self.plans[0]["candidates"][0]["sleeve"], "rsi2_stocks")


class StopRuleTests(SleeveCase):
    def saved_stop(self, symbol, stop):
        state = sleeves.load_state()
        state["positions"][symbol]["stop"] = stop
        sleeves.save_state(state)

    def test_fixed_stop_stays_in_charge_until_atr_stops_are_approved(self):
        self.own(AAA="rsi2_stocks")
        self.saved_stop("AAA", 99.0)
        self.go("stock", {}, FakeTrading([fake_position("AAA", entry=100, price=98)]), listed=[])
        self.assertEqual(self.trading.sells(), [])              # above the 7% stop
        self.go("stock", {}, FakeTrading([fake_position("AAA", entry=100, price=92)]), listed=[])
        self.assertEqual(len(self.trading.sells()), 1)

    def test_approved_atr_stop_is_used_from_the_state_file(self):
        config.USE_ATR_STOPS = True
        self.own(AAA="rsi2_stocks")
        self.saved_stop("AAA", 99.0)
        self.go("stock", {}, FakeTrading([fake_position("AAA", entry=100, price=98)]), listed=[])
        self.assertEqual(len(self.trading.sells()), 1)

    def test_atr_stop_is_saved_at_entry_once_approved(self):
        config.USE_ATR_STOPS = True
        self.enable("ENABLE_RSI2_STOCKS")
        state = self.go("stock", {"AAA": stock_bars(pullback())})
        row = strategies.indicators(stock_bars(pullback())).iloc[-1]
        self.assertAlmostEqual(state["positions"]["AAA"]["stop"], row["close"] - 3 * row["atr"], places=6)

    def test_crypto_stops_trail_up_and_never_down(self):
        config.USE_ATR_STOPS = True
        self.own(BTC__USD="crypto_hold")
        self.saved_stop("BTC/USD", 1.0)
        table = crypto_bars(rising())
        position = fake_position("BTCUSD", entry=300, price=399)
        state = self.go("crypto", {"BTC/USD": table}, FakeTrading([position]))
        row = strategies.indicators(table).iloc[-1]
        trailed = state["positions"]["BTC/USD"]["stop"]
        self.assertAlmostEqual(trailed, row["close"] - 3 * row["atr"], places=6)
        state = self.go("crypto", {"BTC/USD": crypto_bars(rising(first=10.0))}, FakeTrading([position]),
                        today="2026-11-03")
        self.assertEqual(state["positions"]["BTC/USD"]["stop"], trailed)

    def test_stock_stops_do_not_trail(self):
        config.USE_ATR_STOPS = True
        self.own(AAA="rsi2_stocks")
        self.saved_stop("AAA", 1.0)
        drifting = rising(297) + [395.0, 394.5, 394.0]
        state = self.go("stock", {"AAA": stock_bars(drifting)},
                        FakeTrading([fake_position("AAA", entry=390, price=394)]))
        self.assertEqual(state["positions"]["AAA"]["stop"], 1.0)


class LegacyPositionTests(SleeveCase):
    def test_an_unowned_position_keeps_the_fixed_stop(self):
        self.go("stock", {}, FakeTrading([fake_position("OLD", entry=100, price=92)]), listed=[])
        self.assertEqual([o.symbol for o in self.trading.sells()], ["OLD"])

    def test_an_unowned_position_keeps_the_crossover_sell(self):
        self.go("stock", {"OLD": stock_bars(falling())}, FakeTrading([fake_position("OLD", entry=100, price=100)]),
                listed=[])
        self.assertEqual([o.symbol for o in self.trading.sells()], ["OLD"])

    def test_an_unowned_position_in_an_uptrend_is_left_alone(self):
        self.go("stock", {"OLD": stock_bars(rising())}, FakeTrading([fake_position("OLD", entry=100, price=100)]),
                listed=[])
        self.assertEqual(self.trading.sells(), [])

    def test_its_sell_is_logged_without_a_sleeve(self):
        self.go("stock", {}, FakeTrading([fake_position("OLD", entry=100, price=92)]), listed=[])
        self.assertTrue(self.events[0]["details"].startswith("stop-loss"))


class CryptoOrderTests(SleeveCase):
    def setUp(self):
        super().setUp()
        self.table = crypto_bars(np.add(rising(), [0.5 if d % 2 else -0.5 for d in range(300)]))

    def test_crypto_buys_are_limit_orders_that_stay_open(self):
        self.enable("ENABLE_CRYPTO_TREND")
        self.go("crypto", {"BTC/USD": self.table})
        order = self.trading.buys()[0]
        self.assertEqual(order.type, OrderType.LIMIT)
        self.assertEqual(order.time_in_force, TimeInForce.GTC)
        self.assertEqual(sleeves.sleeve_from_order_id(order.client_order_id), "crypto_trend")

    def test_crypto_sells_are_market_orders(self):
        self.own(BTC__USD="crypto_trend")
        self.go("crypto", {}, FakeTrading([fake_position("BTCUSD", entry=100, price=80)]), listed=[])
        self.assertEqual(self.trading.sells()[0].type, OrderType.MARKET)

    def test_stock_buys_are_market_orders(self):
        self.enable("ENABLE_RSI2_STOCKS")
        self.go("stock", {"AAA": stock_bars(pullback())})
        self.assertEqual(self.trading.buys()[0].type, OrderType.MARKET)
        self.assertEqual(self.trading.buys()[0].time_in_force, TimeInForce.DAY)

    def test_a_limit_buy_unfilled_from_yesterday_is_cancelled_and_its_owner_forgotten(self):
        stale = fake_order("BTC/USD", client_id=sleeves.make_order_id("crypto_trend", "buy"),
                           created="2026-11-01T00:07:00Z")
        self.own(BTC__USD="crypto_trend")
        state = self.go("crypto", {"ETH/USD": self.table}, FakeTrading(open_orders=[stale]))
        self.assertEqual(self.trading.cancelled, ["id-BTC/USD"])
        self.assertNotIn("BTC/USD", state["positions"])

    def test_todays_limit_buy_is_left_working_on_a_later_run(self):
        waiting = fake_order("BTC/USD", client_id=sleeves.make_order_id("crypto_trend", "buy"), created=TODAY)
        state = sleeves.load_state()
        state["days"]["crypto"] = TODAY
        state["positions"]["BTC/USD"] = {"sleeve": "crypto_trend", "entry_date": TODAY}
        sleeves.save_state(state)
        state = self.go("crypto", {}, FakeTrading(open_orders=[waiting]), listed=[])
        self.assertEqual(self.trading.cancelled, [])
        self.assertIn("BTC/USD", state["positions"])

    def test_someone_elses_open_order_is_never_cancelled(self):
        self.go("crypto", {"ETH/USD": self.table}, FakeTrading(open_orders=[fake_order("BTC/USD")]))
        self.assertEqual(self.trading.cancelled, [])


class DaysHeldTests(unittest.TestCase):
    def test_counts_finished_bars_from_the_day_of_the_buy(self):
        table = stock_bars(rising(5))
        self.assertEqual(sleeves.days_held(table, table.index[-1].date().isoformat()), 1)
        self.assertEqual(sleeves.days_held(table, table.index[-4].date().isoformat()), 4)
        self.assertEqual(sleeves.days_held(table, TODAY), 0)
        self.assertEqual(sleeves.days_held(table, ""), 0)


# ---------------------------------------------------------------------------
# The live messages and dashboard
# ---------------------------------------------------------------------------
class MessageTests(unittest.TestCase):
    def order_row(self, symbol, details, side="buy"):
        return {"event": "ORDER", "symbol": symbol, "side": side, "qty": "1", "price": "100",
                "order_status": "accepted", "details": details, "ai_decision": "OFF", "ai_reason": ""}

    def test_messages_carry_both_tags(self):
        for symbol, sleeve, tag in (("AAPL", "rsi2_stocks", "[SHORT] [STOCK]"),
                                    ("BTC/USD", "crypto_trend", "[SHORT] [CRYPTO]"),
                                    ("MSFT", "momentum_stocks", "[LONG] [STOCK]"),
                                    ("ETH/USD", "crypto_hold", "[LONG] [CRYPTO]")):
            message = notifier.message_for(self.order_row(symbol, f"sleeve={sleeve}; entry signal"))
            self.assertTrue(message.startswith(tag), message)

    def test_old_strategy_messages_keep_their_old_label(self):
        message = notifier.message_for(self.order_row("AAPL", "strategy BUY: 1 share"))
        self.assertTrue(message.startswith("[STOCK] "))

    def test_a_sleeve_stop_still_reads_as_a_stop(self):
        message = notifier.message_for(self.order_row("AAPL", "sleeve=rsi2_stocks; stop-loss: -7.0%", side="sell"))
        self.assertIn("STOP-LOSS", message)
        self.assertNotIn("sleeve=", message)


class DashboardTests(SleeveCase):
    def position(self, symbol, asset_class="us_equity"):
        return SimpleNamespace(symbol=symbol, asset_class=asset_class, qty="1", avg_entry_price="100",
                               current_price="100", market_value="100", unrealized_pl="0", unrealized_plpc="0")

    def test_trades_carry_their_sleeve_and_a_clean_reason(self):
        row = {"event": "ORDER", "symbol": "AAPL", "details": "sleeve=momentum_stocks; entry signal"}
        trade = dashboard.trade_rows([row])[0]
        self.assertEqual(trade["sleeve"], "momentum_stocks")
        self.assertEqual(trade["reason"], "entry signal")

    def test_positions_carry_their_sleeve(self):
        self.own(AAA="rsi2_stocks", BTC__USD="crypto_trend")
        rows = dashboard.position_rows([self.position("AAA"), self.position("BTCUSD", "crypto"), self.position("OLD")])
        self.assertEqual({r["symbol"]: r["sleeve"] for r in rows},
                         {"AAA": "rsi2_stocks", "BTC/USD": "crypto_trend", "OLD": ""})

    def test_the_shown_stop_is_the_fixed_one_until_atr_is_approved(self):
        state = sleeves.load_state()
        state["positions"]["AAA"] = {"sleeve": "rsi2_stocks", "entry_date": TODAY, "stop": 99.0}
        sleeves.save_state(state)
        self.assertEqual(dashboard.position_rows([self.position("AAA")])[0]["stop_price"], 93.0)
        config.USE_ATR_STOPS = True
        self.assertEqual(dashboard.position_rows([self.position("AAA")])[0]["stop_price"], 99.0)

    def test_the_daily_count_shows_new_entries_in_sleeves_mode_and_every_order_otherwise(self):
        rows = [{"event": "ORDER", "side": "buy", "symbol": "AAA"}, {"event": "ORDER", "side": "sell", "symbol": "BBB"}]
        def count():
            return dashboard.build_status("now", 0, "", False, True, 100.0, 100.0, rows)["trades_today"]
        with mock.patch.object(config, "STRATEGY_MODE", "crossover"):
            self.assertEqual(count(), 2)
        with mock.patch.object(config, "STRATEGY_MODE", "sleeves"):
            self.assertEqual(count(), 1)

    def test_the_page_knows_when_crypto_is_really_trading(self):
        with mock.patch.object(config, "CRYPTO_ENABLED", True), mock.patch.object(config, "STRATEGY_MODE", "sleeves"):
            self.assertFalse(dashboard.crypto_trading())
            self.enable("ENABLE_CRYPTO_HOLD")
            self.assertTrue(dashboard.crypto_trading())
        with mock.patch.object(config, "CRYPTO_ENABLED", True), mock.patch.object(config, "STRATEGY_MODE", "crossover"):
            self.assertTrue(dashboard.crypto_trading())
        with mock.patch.object(config, "CRYPTO_ENABLED", False):
            self.assertFalse(dashboard.crypto_trading())

    def test_summary_shows_each_sleeve_on_or_off_with_its_share_and_profit(self):
        self.enable("ENABLE_MOMENTUM_STOCKS")
        state = sleeves.load_state()
        state["closed_pl"]["rsi2_stocks"] = 12.5
        sleeves.save_state(state)
        rows = [{"sleeve": "rsi2_stocks", "market_value": 1000.0, "unrealized_pl": 40.0}]
        summary = {row["name"]: row for row in sleeves.summary(rows)["sleeves"]}
        self.assertAlmostEqual(sum(row["allocation"] for row in summary.values()), 1.0)
        self.assertEqual((summary["rsi2_stocks"]["open_pl"], summary["rsi2_stocks"]["closed_pl"]), (40.0, 12.5))
        self.assertFalse(summary["rsi2_stocks"]["enabled"])
        self.assertTrue(summary["momentum_stocks"]["enabled"])


class SettingsTests(unittest.TestCase):
    def test_shares_add_up_and_match_the_short_and_long_split(self):
        shares = config.SLEEVES
        self.assertAlmostEqual(sum(s["allocation"] for s in shares.values()), 1.0)
        self.assertAlmostEqual(sum(s["allocation"] for s in shares.values() if s["horizon"] == "SHORT"),
                               config.SHORT_TERM_ALLOCATION)
        self.assertAlmostEqual(sum(s["allocation"] for s in shares.values() if s["horizon"] == "LONG"),
                               config.LONG_TERM_ALLOCATION)

    def test_short_term_sleeves_come_first(self):
        horizons = [s["horizon"] for s in config.SLEEVES.values()]
        self.assertEqual(horizons, sorted(horizons, reverse=True))

    def test_the_liquid_funds_are_kept_in_the_universe(self):
        self.assertTrue({"SPY", "QQQ", "IWM", "XLF", "XLK"} <= set(config.UNIVERSE_ALWAYS_INCLUDE))

    def test_a_fund_that_ranks_below_the_cut_stays_in_the_universe(self):
        bars = {"BIG": stock_bars(rising(first=500.0)), "MID": stock_bars(rising(first=300.0)),
                "XLU": stock_bars(rising(first=100.0))}
        with mock.patch.object(config, "STOCK_UNIVERSE_SIZE", 1), \
                mock.patch.object(config, "UNIVERSE_ALWAYS_INCLUDE", ("XLU",)):
            ranked = universe.rank_stocks([], bars)
        self.assertEqual([row["symbol"] for row in ranked], ["BIG", "XLU"])

    def test_the_workflow_saves_the_state_file(self):
        path = os.path.join(sleeves.BASE_DIR, ".github", "workflows", "trading-bot.yml")
        with open(path, encoding="utf-8") as handle:
            self.assertIn("git add state", handle.read())


class MarketHoursTests(unittest.TestCase):
    """Most ticks skip the bot, but never when anything could still need a stop-loss check."""

    SLEEVES_ONLY = 'STRATEGY_MODE = "sleeves"\nENABLE_CRYPTO_TREND = False\nENABLE_CRYPTO_HOLD = False\n'
    NOTHING_HELD = '{"positions": {}}'

    def at(self, text):
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)

    def decide(self, when, by_hand=False, settings=None, state=None):
        return market_hours.should_run(self.at(when), by_hand, settings or self.SLEEVES_ONLY, state or self.NOTHING_HELD)

    def test_winter_hours(self):       # New York is UTC-5 on Monday 2 November 2026
        self.assertFalse(self.decide("2026-11-02T14:19:00"))
        self.assertTrue(self.decide("2026-11-02T14:20:00"))
        self.assertTrue(self.decide("2026-11-02T21:59:00"))
        self.assertFalse(self.decide("2026-11-02T22:00:00"))

    def test_summer_hours(self):       # New York is UTC-4 on Monday 15 June 2026
        self.assertFalse(self.decide("2026-06-15T13:19:00"))
        self.assertTrue(self.decide("2026-06-15T13:20:00"))
        self.assertTrue(self.decide("2026-06-15T20:59:00"))
        self.assertFalse(self.decide("2026-06-15T21:00:00"))

    def test_the_market_open_and_the_daily_summary_are_inside_the_window_all_year(self):
        summary = config.DAILY_SUMMARY_UTC
        for day in ("2026-01-05", "2026-07-06"):
            self.assertTrue(self.decide(f"{day}T{summary}:00"), day)
        self.assertTrue(self.decide("2026-01-05T14:30:00"))     # 09:30 in New York, winter
        self.assertTrue(self.decide("2026-07-06T13:30:00"))     # 09:30 in New York, summer

    def test_weekends_are_skipped(self):
        self.assertFalse(self.decide("2026-11-07T16:00:00"))    # Saturday
        self.assertFalse(self.decide("2026-11-08T16:00:00"))    # Sunday

    def test_a_run_started_by_hand_always_runs(self):
        self.assertTrue(self.decide("2026-11-08T03:00:00", by_hand=True))

    def test_any_crypto_sleeve_keeps_it_running_all_night(self):
        for flag in ("ENABLE_CRYPTO_TREND", "ENABLE_CRYPTO_HOLD"):
            settings = self.SLEEVES_ONLY.replace(f"{flag} = False", f"{flag} = True")
            self.assertTrue(self.decide("2026-11-08T03:00:00", settings=settings), flag)

    def test_the_old_strategy_keeps_it_running_all_night(self):
        self.assertTrue(self.decide("2026-11-08T03:00:00", settings='STRATEGY_MODE = "crossover"\n'))

    def test_settings_that_cant_be_read_keep_it_running(self):
        self.assertTrue(self.decide("2026-11-08T03:00:00", settings="nothing useful"))

    def test_crypto_still_held_keeps_it_running_after_its_sleeve_is_switched_off(self):
        state = '{"positions": {"BTC/USD": {"sleeve": "crypto_trend"}}}'
        self.assertTrue(self.decide("2026-11-08T03:00:00", state=state))

    def test_stocks_held_overnight_can_wait_for_the_open(self):
        state = '{"positions": {"AAPL": {"sleeve": "rsi2_stocks"}}}'
        self.assertFalse(self.decide("2026-11-08T03:00:00", state=state))

    def test_a_damaged_state_file_keeps_it_running(self):
        self.assertTrue(self.decide("2026-11-08T03:00:00", state="not json"))

    def test_todays_real_settings_skip_the_night(self):
        with open(os.path.join(sleeves.BASE_DIR, "config.py"), encoding="utf-8") as handle:
            self.assertFalse(market_hours.crypto_is_trading(handle.read()))

    def run_main(self, event, actor):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        output = os.path.join(folder, "output")
        settings = mock.patch.object(market_hours, "read", side_effect=[self.SLEEVES_ONLY, self.NOTHING_HELD])
        clock = mock.patch.object(market_hours, "datetime", wraps=market_hours.datetime)
        with settings, clock as fake_clock, contextlib.redirect_stdout(io.StringIO()), mock.patch.dict(
                os.environ, {"GITHUB_EVENT_NAME": event, "GITHUB_TRIGGERING_ACTOR": actor, "GITHUB_OUTPUT": output}):
            fake_clock.now.return_value = self.at("2026-11-08T03:00:00")
            market_hours.main()
        with open(output, encoding="utf-8") as handle:
            return handle.read()

    def test_a_tick_from_the_chain_is_not_a_run_by_hand(self):
        self.assertEqual(self.run_main("workflow_dispatch", "github-actions[bot]"), "run=false\n")

    def test_the_run_button_counts_as_by_hand(self):
        self.assertEqual(self.run_main("workflow_dispatch", "R1pperr"), "run=true\n")

    def test_a_scheduled_tick_at_night_skips(self):
        self.assertEqual(self.run_main("schedule", "R1pperr"), "run=false\n")


class PacemakerTests(unittest.TestCase):
    """Each run starts the next one, after a wait that only the pacemaker environment provides."""

    def setUp(self):
        path = os.path.join(sleeves.BASE_DIR, ".github", "workflows", "trading-bot.yml")
        with open(path, encoding="utf-8") as handle:
            self.workflow = handle.read()
        self.job = self.workflow.split("  next-run:\n")[1]

    def test_the_next_run_waits_for_the_timer(self):
        self.assertIn("    environment: pacemaker\n", self.job)

    def test_a_failed_run_keeps_the_chain_going_and_a_cancelled_run_stops_it(self):
        self.assertIn("    needs: run-bot\n", self.job)
        self.assertIn("    if: ${{ !cancelled() }}\n", self.job)

    def test_only_this_job_may_start_runs(self):
        self.assertIn("    permissions:\n      actions: write\n", self.job)
        self.assertEqual(self.workflow.count("actions: write"), 1)

    def test_a_skipped_bot_still_starts_the_next_tick(self):
        self.assertNotIn("decide.outputs", self.job)

    def test_only_a_clear_no_can_skip_the_bot(self):
        self.assertIn("needs.decide.outputs.run != 'false'", self.workflow)
        self.assertIn('python3 market_hours.py || echo "run=true"', self.workflow)

    def test_it_restarts_this_workflow_on_main(self):
        self.assertIn("gh workflow run trading-bot.yml", self.job)
        self.assertIn("--ref main", self.job)

    def test_runs_never_overlap(self):
        self.assertIn("  group: trading-bot", self.workflow)
        self.assertIn("  cancel-in-progress: false", self.workflow)

    def test_the_schedule_stays_as_a_backup(self):
        self.assertIn('- cron: "7,22,37,52 * * * *"', self.workflow)


class BotWiringTests(unittest.TestCase):
    """bot.py picks the strategy from config.STRATEGY_MODE."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)
        self.trading = mock.MagicMock()
        self.trading.get_account.return_value = SimpleNamespace(
            trading_blocked=False, account_blocked=False, equity="100000", last_equity="100000", cash="9000",
            buying_power="200000", non_marginable_buying_power="8000")
        self.trading.get_clock.return_value = SimpleNamespace(is_open=True)
        self.trading.get_orders.return_value = []
        self.trading.get_all_positions.return_value = []
        for patcher in (mock.patch.object(bot, "TradingClient", return_value=self.trading),
                        mock.patch.object(dashboard, "TradingClient", return_value=self.trading),
                        mock.patch.object(bot, "StockHistoricalDataClient"),
                        mock.patch.object(bot, "LOG_FILE", os.path.join(self.folder, "log.csv")),
                        mock.patch.object(dashboard, "DATA_DIR", os.path.join(self.folder, "data")),
                        mock.patch.object(config, "get_alpaca_keys", return_value=("fake", "fake")),
                        mock.patch.object(config, "CRYPTO_ENABLED", True),
                        mock.patch.object(bot, "summary_due", return_value=False),
                        mock.patch.object(bot, "get_daily_bars", return_value={}),
                        mock.patch.object(universe, "rows", return_value=[]),
                        mock.patch("urllib.request.urlopen")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_bot(self, mode):
        bot.RUN_EVENTS.clear()
        with mock.patch.object(config, "STRATEGY_MODE", mode), \
                mock.patch.object(sleeves, "run") as sleeve_run, mock.patch.object(crypto, "run") as crypto_run, \
                mock.patch.object(sys, "argv", ["bot.py"]), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                bot.main()
        return sleeve_run, crypto_run

    def test_crossover_mode_never_touches_the_sleeves(self):
        sleeve_run, crypto_run = self.run_bot("crossover")
        sleeve_run.assert_not_called()
        crypto_run.assert_called_once()

    def test_sleeves_mode_replaces_the_old_stock_and_crypto_runs(self):
        sleeve_run, crypto_run = self.run_bot("sleeves")
        crypto_run.assert_not_called()
        self.assertEqual([call.args[0] for call in sleeve_run.call_args_list], ["stock", "crypto"])

    def test_stock_buys_are_limited_to_real_cash(self):
        sleeve_run, _ = self.run_bot("sleeves")
        self.assertEqual(sleeve_run.call_args_list[0].args[3], 8000.0)


# ---------------------------------------------------------------------------
# The backtest
# ---------------------------------------------------------------------------
def walk(rng, days, index):
    closes = 100 * np.exp(np.cumsum(rng.normal(0.0006, 0.02, days)))
    opens = closes * (1 + rng.normal(0, 0.005, days))
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) * (1 + abs(rng.normal(0, 0.008, days))),
                         "low": np.minimum(opens, closes) * (1 - abs(rng.normal(0, 0.008, days))),
                         "close": closes, "volume": 1000.0}, index=index)


def random_tables(seed=11, days=620):
    rng = np.random.default_rng(seed)
    business = pd.bdate_range("2023-01-02", periods=days)
    tables = {name: walk(rng, days, business) for name in ("SPY", "AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG")}
    calendar = pd.date_range("2023-01-02", periods=int(days * 1.4))
    for name in ("BTC/USD", "ETH/USD", "SOL/USD"):
        tables[name] = walk(rng, len(calendar), calendar)
    return tables


def shaped(closes, index, opens=None, spread=0.002):
    closes = np.asarray(closes, dtype=float)
    opens = closes if opens is None else np.asarray(opens, dtype=float)
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) * (1 + spread),
                         "low": np.minimum(opens, closes) * (1 - spread), "close": closes, "volume": 1000.0},
                        index=index)


class NoLookAheadTests(unittest.TestCase):
    OPTIONS = (backtest.Options(tuple(config.SLEEVES)),
               backtest.Options(tuple(config.SLEEVES), atr_stops=False),
               backtest.Options(("rsi2_stocks",)),
               backtest.Options(("momentum_stocks",)),
               backtest.Options(("crypto_trend",)),
               backtest.Options(("crypto_hold",)),
               backtest.Options(("crossover_stock", "crossover_crypto")))

    def test_changing_the_future_cannot_change_the_past(self):
        tables = random_tables()
        panel = backtest.build_panel(tables)
        cut = len(panel.dates) * 3 // 4
        cut_day = panel.dates[cut]
        changed = {}
        for symbol, table in tables.items():
            copy = table.copy()
            copy.loc[copy.index > cut_day, ["open", "high", "low", "close"]] *= 3.7
            changed[symbol] = copy
        other = backtest.build_panel(changed)
        for options in self.OPTIONS:
            with self.subTest(sleeves=options.sleeves, atr=options.atr_stops):
                first, second = backtest.simulate(panel, options), backtest.simulate(other, options)
                self.assertGreater(len(first["trades"]), 0)
                np.testing.assert_array_equal(first["equity"].to_numpy()[:cut + 1], second["equity"].to_numpy()[:cut + 1])
                pd.testing.assert_frame_equal(first["trades"][first["trades"]["exit"] <= cut],
                                              second["trades"][second["trades"]["exit"] <= cut])

    def test_a_signal_on_todays_close_fills_at_tomorrows_open(self):
        days = pd.bdate_range("2024-01-01", periods=300)
        closes = rising(280)
        signal_day = len(closes)                  # one sharp fall is enough for RSI(2)
        for _ in range(3):
            closes.append(closes[-1] * 0.96)
        closes += [closes[-1] * 1.02 ** n for n in range(1, 18)]
        opens = np.array(closes) * 1.01
        panel = backtest.build_panel({"AAA": shaped(closes, days, opens), "SPY": shaped(rising(), days)})
        result = backtest.simulate(panel, backtest.Options(("rsi2_stocks",), atr_stops=False))
        trade = result["trades"].iloc[0]
        self.assertEqual(trade["entry"], signal_day + 1)

        # $5,000 goes in at the next open and comes out at the open after the exit signal.
        fee = backtest.STOCK_FEE
        buy_price, sell_price = opens[trade["entry"]], opens[trade["exit"]]
        expected = 5_000 / buy_price * sell_price * (1 - fee) - 5_000 * (1 + fee)
        self.assertAlmostEqual(trade["pnl"], expected, places=6)
        self.assertGreater(trade["exit"], trade["entry"])

    def test_a_stop_fills_at_the_open_if_the_price_gapped_past_it(self):
        days = pd.bdate_range("2024-01-01", periods=300)
        closes = rising(280)
        for _ in range(3):
            closes.append(closes[-1] * 0.96)
        closes += [closes[-1]] * 17
        opens = np.array(closes)
        opens[284] = closes[283] * 0.80          # gaps straight through a 7% stop
        panel = backtest.build_panel({"AAA": shaped(closes, days, opens), "SPY": shaped(rising(), days)})
        trade = backtest.simulate(panel, backtest.Options(("rsi2_stocks",), atr_stops=False))["trades"].iloc[0]
        self.assertEqual(trade["reason"], "stop")
        rate = backtest.STOCK_FEE
        self.assertEqual((trade["entry"], trade["exit"]), (281, 284))
        expected = 5_000 / opens[281] * opens[284] * (1 - rate) - 5_000 * (1 + rate)
        self.assertAlmostEqual(trade["pnl"], expected, places=6)


class BacktestRuleTests(unittest.TestCase):
    def test_no_symbol_is_held_by_two_sleeves_at_once(self):
        panel = backtest.build_panel(random_tables())
        trades = backtest.simulate(panel, backtest.Options(tuple(config.SLEEVES)))["trades"]
        self.assertGreater(trades["sleeve"].nunique(), 2)
        for symbol, rows in trades.sort_values("entry").groupby("symbol"):
            self.assertTrue((rows["entry"].to_numpy()[1:] >= rows["exit"].to_numpy()[:-1]).all(), symbol)

    def test_without_the_limit_two_sleeves_do_overlap(self):
        panel = backtest.build_panel(random_tables())
        trades = backtest.simulate(panel, backtest.Options(tuple(config.SLEEVES), one_owner=False))["trades"]
        overlaps = sum((rows["entry"].to_numpy()[1:] < rows["exit"].to_numpy()[:-1]).any()
                       for _, rows in trades.sort_values("entry").groupby("symbol"))
        self.assertGreater(overlaps, 0)

    def test_skipped_signals_are_counted(self):
        panel = backtest.build_panel(random_tables())
        skips = backtest.simulate(panel, backtest.Options(tuple(config.SLEEVES), daily_cap=1))["skips"]
        self.assertGreater(skips["crypto_hold"]["owned"], 0)
        self.assertGreater(sum(row["daily_cap"] for row in skips.values()), 0)

    def test_the_daily_cap_holds_in_each_market(self):
        panel = backtest.build_panel(random_tables())
        trades = backtest.simulate(panel, backtest.Options(tuple(config.SLEEVES), daily_cap=1))["trades"]
        trades = trades.assign(crypto=trades["symbol"].str.contains("/"))
        self.assertEqual(trades.groupby(["entry", "crypto"]).size().max(), 1)

    def test_short_term_sleeves_fill_first_under_the_cap(self):
        days = pd.bdate_range("2024-01-01", periods=330)
        first_month = next(t for t in range(253, 330) if days[t].month != days[t - 1].month)
        tables = {"SPY": shaped(rising(330), days)}
        for n in range(8):
            tables[f"S{n}"] = shaped(rising(330, step=1 + n / 10), days)
        dip = rising(330)
        for t in range(first_month, first_month + 3):
            dip[t] = dip[t - 1] * 0.96
        scale = dip[first_month + 2] / dip[first_month + 3]
        dip[first_month + 3:] = [price * scale for price in dip[first_month + 3:]]
        tables["ZZZ"] = shaped(dip, days)

        options = backtest.Options(("momentum_stocks", "rsi2_stocks"), daily_cap=1, atr_stops=False)
        trades = backtest.simulate(backtest.build_panel(tables), options)["trades"]
        short = trades[trades["sleeve"] == "rsi2_stocks"]
        self.assertEqual(short.iloc[0]["symbol"], "ZZZ")
        self.assertEqual(short.iloc[0]["entry"], first_month + 1)     # the first day momentum could fill too

    def test_a_monday_entry_uses_fridays_atr(self):
        """Weekends are blank for a stock, which must not quietly swap in the fixed stop."""
        days = pd.bdate_range("2024-01-01", periods=300)
        friday = next(t for t in range(250, 290) if days[t].weekday() == 4)
        closes = [100 * 1.002 ** day for day in range(friday)]
        closes.append(closes[-1] * 0.96)                                      # the signal lands on the Friday
        closes += [closes[-1]] * (len(days) - len(closes))
        table = shaped(closes, days, spread=0.001)
        table.loc[table.index[friday + 1], "low"] = closes[friday] * 0.96     # past 3 x ATR, not past 7%
        calendar = pd.date_range("2024-01-01", days[-1])
        tables = {"AAA": table, "SPY": shaped(rising(300), days), "BTC/USD": shaped(rising(len(calendar)), calendar)}
        panel = backtest.build_panel(tables)
        with_atr = backtest.simulate(panel, backtest.Options(("rsi2_stocks",)))["trades"]
        fixed = backtest.simulate(panel, backtest.Options(("rsi2_stocks",), atr_stops=False))["trades"]
        self.assertEqual(with_atr.iloc[0]["reason"], "stop")
        self.assertNotEqual(fixed.iloc[0]["reason"], "stop")

    def test_a_coin_with_a_missing_day_is_not_sold_for_it(self):
        calendar = pd.date_range("2024-01-01", periods=300)
        table = shaped(np.add(rising(), [0.5 if d % 2 else -0.5 for d in range(300)]), calendar)
        gappy = table.drop(table.index[250])
        panel = backtest.build_panel({"BTC/USD": table, "GAP/USD": gappy})
        trades = backtest.simulate(panel, backtest.Options(("crypto_trend",)))["trades"]
        self.assertEqual(len(trades[trades["symbol"] == "GAP/USD"]), 0)

    def test_crypto_sells_pay_the_market_fee_even_in_the_limit_version(self):
        panel = backtest.build_panel({"BTC/USD": crypto_bars(rising())})
        book = backtest.Book()
        key = ("crypto_trend", 0)
        book.positions[key] = {"qty": 1.0, "cost": 100.0, "entry_fee": 0.15, "entry_day": 1}
        options = backtest.Options(("crypto_trend",), crypto_fee=config.CRYPTO_LIMIT_FEE_PCT)
        backtest.close_position(book, key, 100.0, 5, panel, options, "signal")
        self.assertAlmostEqual(book.fees, 0.25)

    def test_crypto_buys_pay_the_limit_fee_in_the_limit_version(self):
        panel = backtest.build_panel({"BTC/USD": crypto_bars(rising())})
        cheap = backtest.Options(("crypto_trend",), crypto_fee=config.CRYPTO_LIMIT_FEE_PCT)
        self.assertEqual(backtest.fee_for(panel, 0, cheap, True), 0.0015)
        self.assertEqual(backtest.fee_for(panel, 0, backtest.Options(("crypto_trend",)), True), 0.0025)

    def test_weekend_rows_do_not_dilute_a_stock_results_sharpe(self):
        trading_days = pd.bdate_range("2024-01-01", periods=300)
        rng = np.random.default_rng(3)
        equity = pd.Series(100_000 * np.exp(np.cumsum(rng.normal(0.001, 0.01, 300))), index=trading_days)
        every_day = equity.reindex(pd.date_range(trading_days[0], trading_days[-1])).ffill()
        empty = pd.DataFrame(columns=["sleeve", "symbol", "entry", "exit", "days", "pnl", "reason", "win", "fee"])
        first, last = trading_days[0], trading_days[-1]
        clean = backtest.report_row("x", {"equity": equity, "trades": empty, "dates": equity.index}, first, last, 252)
        padded = backtest.report_row("x", {"equity": every_day, "trades": empty, "dates": every_day.index},
                                     first, last, 252, trading_days)
        self.assertAlmostEqual(clean[1]["sharpe"], padded[1]["sharpe"])

    def test_the_oldest_70_percent_is_in_sample(self):
        first, cut, last = backtest.split_points(pd.Timestamp("2020-01-01"), pd.Timestamp("2030-01-01"))
        self.assertAlmostEqual((cut - first) / (last - first), 0.7)

    def test_a_fund_is_told_from_a_company_called_a_trust(self):
        def is_fund(name):
            return any(word in name.upper() for word in backtest.FUND_WORDS)
        self.assertTrue(is_fund("SPDR S&P 500 ETF Trust"))
        self.assertTrue(is_fund("United States Oil Fund, LP"))
        self.assertFalse(is_fund("Digital Realty Trust, Inc."))


if __name__ == "__main__":
    unittest.main()
