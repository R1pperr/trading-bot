"""
The crypto side of the bot, with everything outside this computer replaced by fakes.

Alpaca, Gemini and Telegram are all fakes here. Orders only ever reach a fake
client, so no test can place a real order.
"""

import contextlib
import csv
import io
import itertools
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pandas as pd
from alpaca.trading.enums import OrderSide, TimeInForce

import ai_analyst
import backtest
import bot
import config
import crypto
import dashboard
import data
import notifier
import risk
import universe

TELEGRAM_ON = {"TELEGRAM_BOT_TOKEN": "123456:FAKE-TOKEN-FOR-TESTS", "TELEGRAM_CHAT_ID": "42"}
REAL_STOCK_PLACE_ORDER = bot.place_order


def fake_signal(signal="HOLD", trend="UP"):
    reason = "20-day average crossed ABOVE the 50-day average" if signal == "BUY" else "No crossover today"
    return {"date": "2026-10-31", "close": 78000.0, "sma_fast": 101.0, "sma_slow": 100.0,
            "trend": trend, "signal": signal, "reason": reason}


def crypto_order(symbol="BTC/USD", status="filled"):
    return SimpleNamespace(symbol=symbol, status=status, asset_class="crypto")


def stock_order(symbol="SPY"):
    return SimpleNamespace(symbol=symbol, status="filled", asset_class="us_equity")


def crypto_position(symbol="BTCUSD", qty="0.05", entry="78000", current="79000"):
    value = float(qty) * float(current)
    pl = value - float(qty) * float(entry)
    return SimpleNamespace(symbol=symbol, asset_class="crypto", qty=qty, avg_entry_price=entry,
                           current_price=current, market_value=str(value), unrealized_pl=str(pl),
                           unrealized_plpc=str(float(current) / float(entry) - 1), change_today="0.01")


def stock_position(symbol="SPY", current="710"):
    return SimpleNamespace(symbol=symbol, asset_class="us_equity", qty="7", avg_entry_price="700",
                           current_price=current, market_value="4970", unrealized_pl="70",
                           unrealized_plpc="0.0143", change_today="0.01")


class CryptoRunTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)
        self.log_file = os.path.join(self.folder, "trade_log.csv")

        self.trading = mock.MagicMock()
        self.trading.submit_order.side_effect = self.fake_submit
        self.set_account(market_open=False)
        self.trading.get_orders.return_value = []
        self.trading.get_all_positions.return_value = []
        days = pd.bdate_range(end="2026-10-30", periods=config.SLOW_SMA_DAYS + 5)
        self.prices = pd.DataFrame({"close": [100.0] * len(days)}, index=days)

        self.stock_order = self.start(mock.patch.object(bot, "place_order",
                                                        return_value=("stock-id", "filled", 100.0)))
        self.stock_signal = self.start(mock.patch.object(bot, "latest_signal", return_value=fake_signal()))
        self.crypto_signal = self.start(mock.patch.object(crypto, "latest_signal", return_value=fake_signal()))
        self.gemini = self.start(mock.patch.object(ai_analyst, "_ask_gemini",
                                                   return_value=(ai_analyst.APPROVE, "Looks steady")))
        self.urlopen = self.start(mock.patch("urllib.request.urlopen"))
        self.crypto_bars = self.start(mock.patch.object(crypto, "get_crypto_daily_bars", side_effect=self.bars))
        self.crypto_price = self.start(mock.patch.object(crypto, "get_crypto_latest_price", return_value=78000.0))
        self.start(mock.patch.object(universe, "rows", side_effect=self.universe_rows))
        self.start(mock.patch.object(bot, "summary_due", return_value=False))
        self.start(mock.patch.object(bot, "TradingClient", return_value=self.trading))
        self.start(mock.patch.object(dashboard, "TradingClient", return_value=self.trading))
        self.start(mock.patch.object(bot, "StockHistoricalDataClient"))
        self.start(mock.patch.object(bot, "get_daily_bars", return_value={"SPY": self.prices}))
        self.start(mock.patch.object(bot, "latest_price", return_value=100.0))
        self.start(mock.patch.object(bot, "LOG_FILE", self.log_file))
        self.start(mock.patch.object(dashboard, "DATA_DIR", os.path.join(self.folder, "data")))
        self.start(mock.patch.object(config, "get_alpaca_keys", return_value=("fake", "fake")))
        self.start(mock.patch.object(config, "SYMBOLS", ["SPY"]))
        self.start(mock.patch.object(config, "USE_AI_ANALYST", False))
        self.start(mock.patch.object(config, "CRYPTO_ENABLED", True))
        self.start(mock.patch.object(config, "STRATEGY_MODE", "crossover"))
        self.start(mock.patch.dict(os.environ, TELEGRAM_ON))

    def start(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def universe_rows(self, market):
        symbols = config.SYMBOLS if market == "stock" else config.CRYPTO_SYMBOLS
        return [{"symbol": symbol, "name": symbol} for symbol in symbols]

    def bars(self, symbols, start, end=None):
        return {symbol: self.prices for symbol in symbols}

    def fake_submit(self, order_data):
        return SimpleNamespace(id="crypto-id", status="filled", filled_avg_price="78000")

    def set_account(self, market_open=False, equity="100000"):
        self.trading.get_account.return_value = SimpleNamespace(
            trading_blocked=False, account_blocked=False, equity=equity, last_equity="100000",
            cash="95000", buying_power="195000", non_marginable_buying_power="95000")
        self.trading.get_clock.return_value = SimpleNamespace(is_open=market_open)

    def crypto_signals(self, *by_symbol):
        """One signal per pair in config.CRYPTO_SYMBOLS, repeated for every run."""
        signals = itertools.cycle(by_symbol)
        self.crypto_signal.side_effect = lambda prices: next(signals)

    def buy_signal_for_btc(self):
        self.crypto_signals(fake_signal("BUY"), fake_signal())

    def run_bot(self, *flags):
        bot.RUN_EVENTS.clear()
        self.output = io.StringIO()
        with mock.patch.object(sys, "argv", ["bot.py", *flags]), contextlib.redirect_stdout(self.output):
            with self.assertRaises(SystemExit) as finished:
                bot.main()
        return finished.exception.code

    def orders(self):
        return [call.kwargs["order_data"] for call in self.trading.submit_order.call_args_list]

    def messages(self):
        return [urllib.parse.parse_qs(call.args[0].data.decode())["text"][0]
                for call in self.urlopen.call_args_list]

    def load(self, name):
        with open(os.path.join(dashboard.DATA_DIR, name), encoding="utf-8") as handle:
            return json.load(handle)

    def log_rows(self):
        if not os.path.exists(self.log_file):
            return []
        with open(self.log_file, newline="") as handle:
            return list(csv.DictReader(handle))

    # --- Orders -------------------------------------------------------------------
    def test_crypto_orders_use_gtc_and_a_fractional_quantity(self):
        self.buy_signal_for_btc()
        self.assertEqual(self.run_bot(), 0)
        order = self.orders()[0]
        self.assertEqual(order.symbol, "BTC/USD")
        self.assertEqual(order.side, OrderSide.BUY)
        self.assertIn(order.time_in_force, (TimeInForce.GTC, TimeInForce.IOC))
        self.assertNotEqual(order.time_in_force, TimeInForce.DAY)
        self.assertNotEqual(order.qty, int(order.qty))
        self.assertAlmostEqual(order.qty, 0.06410256)

    def test_buy_stays_within_the_position_cap(self):
        self.buy_signal_for_btc()
        self.run_bot()
        self.assertLessEqual(self.orders()[0].qty * 78000.0, 100000 * config.MAX_POSITION_PCT)

    def test_sell_signal_sells_the_whole_fractional_position(self):
        self.trading.get_all_positions.return_value = [crypto_position(qty="0.0512345")]
        self.crypto_signals(fake_signal("SELL"), fake_signal())
        self.run_bot()
        order = self.orders()[0]
        self.assertEqual((order.symbol, order.side, order.qty), ("BTC/USD", OrderSide.SELL, 0.0512345))
        self.assertEqual(order.time_in_force, TimeInForce.GTC)

    def test_stock_orders_still_use_day(self):
        trading = mock.MagicMock()
        trading.submit_order.return_value = SimpleNamespace(id="1", status="filled", filled_avg_price="1")
        REAL_STOCK_PLACE_ORDER(trading, "SPY", OrderSide.BUY, 1, False)
        self.assertEqual(trading.submit_order.call_args.kwargs["order_data"].time_in_force, TimeInForce.DAY)

    def test_not_enough_cash_blocks_a_buy(self):
        self.trading.get_account.return_value.non_marginable_buying_power = "100"
        self.buy_signal_for_btc()
        self.assertEqual(self.run_bot(), 0)
        self.assertEqual(self.orders(), [])
        self.assertIn("BLOCKED", self.output.getvalue())

    # --- Symbols -------------------------------------------------------------------
    def test_symbols_are_normalized(self):
        for given, expected in (("BTCUSD", "BTC/USD"), ("BTC/USD", "BTC/USD"), ("ethusd", "ETH/USD"),
                                ("BTCUSDT", "BTC/USDT"), ("SOLUSDC", "SOL/USDC"), ("ETHBTC", "ETH/BTC"),
                                ("USD", "USD")):
            self.assertEqual(crypto.normalize(given), expected)

    def test_position_written_btcusd_counts_as_btc_usd(self):
        self.trading.get_all_positions.return_value = [crypto_position("BTCUSD")]
        self.buy_signal_for_btc()
        self.run_bot()
        self.assertEqual(self.orders(), [])

    def test_stop_loss_on_a_btcusd_position_sells_btc_usd(self):
        self.trading.get_all_positions.return_value = [crypto_position("BTCUSD", current="70000")]
        self.run_bot()
        order = self.orders()[0]
        self.assertEqual((order.symbol, order.side), ("BTC/USD", OrderSide.SELL))
        self.assertEqual(len(self.messages()), 1)
        self.assertIn("[CRYPTO]", self.messages()[0])
        self.assertIn("STOP-LOSS", self.messages()[0])

    # --- Market hours -------------------------------------------------------------
    def test_crypto_runs_while_the_market_is_closed_and_stocks_do_not(self):
        self.stock_signal.return_value = fake_signal("BUY")
        self.buy_signal_for_btc()
        self.set_account(market_open=False)
        self.assertEqual(self.run_bot(), 0)
        self.stock_order.assert_not_called()
        self.assertEqual(len(self.orders()), 1)
        self.assertIn("The US market is closed", self.output.getvalue())

    def test_both_trade_while_the_market_is_open(self):
        self.stock_signal.return_value = fake_signal("BUY")
        self.buy_signal_for_btc()
        self.set_account(market_open=True)
        self.assertEqual(self.run_bot(), 0)
        self.stock_order.assert_called_once()
        self.assertEqual(len(self.orders()), 1)

    # --- Kill switch ----------------------------------------------------------------
    def test_kill_switch_blocks_stocks_and_crypto(self):
        self.stock_signal.return_value = fake_signal("BUY")
        self.buy_signal_for_btc()
        self.set_account(market_open=True, equity="96000")
        self.assertEqual(self.run_bot(), 0)
        self.stock_order.assert_not_called()
        self.assertEqual(self.orders(), [])
        self.assertTrue(self.load("status.json")["kill_switch"]["on"])

    def test_kill_switch_message_is_sent_once_with_both_labels(self):
        self.set_account(market_open=True, equity="96000")
        for _ in range(3):
            self.run_bot()
        kills = [m for m in self.messages() if "KILL SWITCH" in m]
        self.assertEqual(len(kills), 1)
        self.assertIn("[STOCK] [CRYPTO]", kills[0])

    def test_crypto_side_trips_the_kill_switch_at_weekends_when_stocks_are_shut(self):
        self.buy_signal_for_btc()
        self.set_account(market_open=False, equity="96000")
        self.run_bot()
        self.assertEqual(self.orders(), [])
        self.assertEqual([row["event"] for row in self.log_rows()].count("KILL_SWITCH"), 1)
        self.set_account(market_open=False, equity="100000")
        self.run_bot()
        self.assertEqual(self.orders(), [])   # stays on for the rest of the day

    def test_stop_loss_sell_goes_through_with_the_kill_switch_on(self):
        self.set_account(market_open=False, equity="96000")
        self.trading.get_all_positions.return_value = [crypto_position("BTCUSD", current="70000")]
        self.run_bot()
        self.assertEqual(self.orders()[0].side, OrderSide.SELL)

    # --- Switched off ---------------------------------------------------------------
    def test_crypto_disabled_means_no_crypto_activity(self):
        config.CRYPTO_ENABLED = False
        self.buy_signal_for_btc()
        self.trading.get_all_positions.return_value = [crypto_position("BTCUSD", current="70000")]
        self.assertEqual(self.run_bot(), 0)
        self.assertEqual(self.orders(), [])
        self.crypto_bars.assert_not_called()
        self.crypto_price.assert_not_called()
        self.assertNotIn("Crypto", self.output.getvalue())
        self.assertEqual(self.messages(), [])

    # --- Keeping stocks and crypto apart ---------------------------------------------
    def test_crypto_orders_do_not_use_up_the_stocks_daily_limit(self):
        self.trading.get_orders.return_value = [crypto_order(f"C{n}/USD") for n in range(config.MAX_TRADES_PER_DAY)]
        self.stock_signal.return_value = fake_signal("BUY")
        self.set_account(market_open=True)
        self.run_bot()
        self.stock_order.assert_called_once()
        self.assertIn(f"Trades today: 0 of {config.MAX_TRADES_PER_DAY}", self.output.getvalue())

    def test_stock_orders_do_not_use_up_the_crypto_daily_limit(self):
        self.trading.get_orders.return_value = [stock_order(f"S{n}") for n in range(config.MAX_TRADES_PER_DAY)]
        self.buy_signal_for_btc()
        self.run_bot()
        self.assertEqual(len(self.orders()), 1)

    def test_crypto_has_its_own_daily_limit(self):
        self.trading.get_orders.return_value = [crypto_order(f"C{n}/USD") for n in range(config.MAX_TRADES_PER_DAY)]
        self.buy_signal_for_btc()
        self.run_bot()
        self.assertEqual(self.orders(), [])
        self.assertIn("daily crypto trade limit", self.output.getvalue())

    def test_stock_stop_loss_never_touches_crypto_positions(self):
        self.set_account(market_open=True)
        self.trading.get_all_positions.return_value = [crypto_position("BTCUSD", current="70000")]
        self.run_bot()
        self.stock_order.assert_not_called()

    def test_stock_position_is_left_to_the_stock_side(self):
        self.trading.get_all_positions.return_value = [stock_position()]
        self.run_bot()
        self.assertEqual(self.orders(), [])

    def test_a_crypto_crash_does_not_stop_the_stock_run(self):
        self.stock_signal.return_value = fake_signal("BUY")
        self.set_account(market_open=True)
        with mock.patch.object(crypto, "run", side_effect=RuntimeError("crypto feed down")):
            self.assertEqual(self.run_bot(), 1)
        self.stock_order.assert_called_once()
        crashes = [m for m in self.messages() if "crashed" in m]
        self.assertEqual(len(crashes), 1)
        self.assertIn("[CRYPTO]", crashes[0])
        self.assertIn("crypto feed down", crashes[0])
        self.assertEqual(self.load("status.json")["last_run"]["status"], "error")

    def test_a_stock_crash_does_not_stop_the_crypto_run(self):
        self.buy_signal_for_btc()
        with mock.patch.object(bot, "run", side_effect=RuntimeError("stock data down")):
            self.assertEqual(self.run_bot(), 1)
        self.assertEqual(len(self.orders()), 1)
        crashes = [m for m in self.messages() if "crashed" in m]
        self.assertIn("[STOCK]", crashes[0])

    # --- One trade per symbol per UTC day -------------------------------------------
    def test_one_trade_per_symbol_per_utc_day_across_many_runs(self):
        config.USE_AI_ANALYST = True
        self.buy_signal_for_btc()
        self.run_bot()
        self.assertEqual(len(self.orders()), 1)
        self.trading.get_orders.return_value = [crypto_order("BTC/USD")]
        for _ in range(95):
            self.assertEqual(self.run_bot(), 0)
        self.assertEqual(len(self.orders()), 1)
        self.gemini.assert_called_once()

    def test_the_day_counted_starts_at_utc_midnight(self):
        self.run_bot()
        request = self.trading.get_orders.call_args.kwargs["filter"]
        self.assertEqual(request.after, crypto.utc_midnight())

    def test_no_candidate_means_gemini_is_not_called(self):
        config.USE_AI_ANALYST = True
        for _ in range(10):
            self.run_bot()
        self.gemini.assert_not_called()

    def test_ai_veto_is_asked_once_and_sends_no_instant_message(self):
        config.USE_AI_ANALYST = True
        self.gemini.return_value = (ai_analyst.VETO, "Prices look choppy")
        self.buy_signal_for_btc()
        for _ in range(10):
            self.run_bot()
        self.gemini.assert_called_once()
        self.assertEqual(self.orders(), [])
        self.assertEqual(self.messages(), [])
        self.assertIn("VETO", json.dumps(self.load("candidates.json")))

    def test_veto_from_early_in_the_utc_day_still_counts(self):
        config.USE_AI_ANALYST = True
        moment = (crypto.utc_midnight() + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
        with open(self.log_file, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=bot.LOG_COLUMNS, restval="")
            writer.writeheader()
            writer.writerow({"time_utc": moment, "event": "AI_DECISION", "symbol": "BTC/USD",
                             "signal_date": "2026-10-31", "ai_decision": "VETO"})
        self.buy_signal_for_btc()
        self.run_bot()
        self.gemini.assert_not_called()
        self.assertEqual(self.orders(), [])

    # --- Dry run and test order -------------------------------------------------------
    def test_dry_run_orders_nothing_logs_nothing_sends_nothing(self):
        config.USE_AI_ANALYST = True
        self.buy_signal_for_btc()
        self.trading.get_all_positions.return_value = [crypto_position("BTCUSD", current="70000")]
        self.assertEqual(self.run_bot("--dry-run"), 0)
        self.trading.submit_order.assert_not_called()
        self.gemini.assert_not_called()
        self.urlopen.assert_not_called()
        self.assertFalse(os.path.exists(self.log_file))
        self.assertIn("would sell", self.output.getvalue())

    def test_test_order_is_about_ten_dollars_even_with_crypto_off(self):
        config.CRYPTO_ENABLED = False
        self.assertEqual(self.run_bot("--test-order", "BTC/USD"), 0)
        order = self.orders()[0]
        self.assertEqual((order.symbol, order.side, order.time_in_force), ("BTC/USD", OrderSide.BUY, TimeInForce.GTC))
        self.assertAlmostEqual(order.qty * 78000, crypto.TEST_ORDER_DOLLARS, places=2)
        self.stock_order.assert_not_called()
        self.assertIn("[CRYPTO]", self.messages()[0])

    def test_test_order_is_refused_with_the_kill_switch_on(self):
        self.set_account(equity="96000")
        self.assertEqual(self.run_bot("--test-order", "BTC/USD"), 1)
        self.assertEqual(self.orders(), [])

    def test_stock_test_order_still_goes_the_stock_way(self):
        self.set_account(market_open=False)
        self.assertEqual(self.run_bot("--test-order", "SPY"), 0)
        self.stock_order.assert_called_once()
        self.assertEqual(self.orders(), [])

    # --- Dashboard data -----------------------------------------------------------------
    def test_trades_and_positions_are_tagged_with_their_asset_class(self):
        self.buy_signal_for_btc()
        self.stock_signal.return_value = fake_signal("BUY")
        self.set_account(market_open=True)
        self.trading.get_all_positions.return_value = [crypto_position("ETHUSD"), stock_position("QQQ")]
        self.run_bot()
        positions = {row["symbol"]: row["asset_class"] for row in self.load("positions.json")}
        self.assertEqual(positions, {"ETH/USD": "crypto", "QQQ": "stock"})
        trades = {row["symbol"]: row["asset_class"] for row in self.load("trades.json")}
        self.assertEqual(trades, {"BTC/USD": "crypto", "SPY": "stock"})

    def test_status_has_the_breakdown(self):
        self.trading.get_all_positions.return_value = [crypto_position("BTCUSD", qty="0.1"), stock_position()]
        self.run_bot()
        status = self.load("status.json")
        self.assertEqual(status["stock_value"], 4970.0)
        self.assertEqual(status["crypto_value"], 7900.0)
        self.assertEqual(status["invested"], 12870.0)
        self.assertIs(status["crypto_enabled"], True)

    def test_daily_counts_are_kept_separately(self):
        self.buy_signal_for_btc()
        self.stock_signal.return_value = fake_signal("BUY")
        self.set_account(market_open=True)
        self.run_bot()
        status = self.load("status.json")
        self.assertEqual((status["trades_today"], status["crypto_trades_today"]), (1, 1))


class SmallPieceTests(unittest.TestCase):
    def test_crypto_size_is_fractional_and_rounded_down(self):
        self.assertEqual(risk.max_crypto_qty(100000, 78000), 0.06410256)
        self.assertLessEqual(risk.max_crypto_qty(100000, 78000) * 78000, 5000)
        self.assertEqual(risk.max_crypto_qty(100000, 0), 0.0)
        self.assertEqual(risk.max_crypto_qty(0, 78000), 0.0)

    def test_stock_size_is_still_whole_shares(self):
        self.assertEqual(risk.max_shares_to_buy(100000, 680), 7)

    def test_asset_check_uses_the_class_or_the_slash(self):
        self.assertTrue(crypto.is_crypto(SimpleNamespace(symbol="BTCUSD", asset_class="crypto")))
        self.assertTrue(crypto.is_crypto(SimpleNamespace(symbol="BTC/USD")))
        self.assertFalse(crypto.is_crypto(SimpleNamespace(symbol="AAPL", asset_class="us_equity")))
        self.assertFalse(crypto.is_crypto(SimpleNamespace(symbol="AAPL")))

    def test_message_labels(self):
        order = {"event": "ORDER", "side": "buy", "qty": 0.00001234, "price": 78000.0,
                 "order_status": "filled", "details": "strategy BUY"}
        self.assertTrue(notifier.message_for({**order, "symbol": "BTC/USD"}).startswith("[CRYPTO] "))
        self.assertTrue(notifier.message_for({**order, "symbol": "SPY", "qty": 7.0}).startswith("[STOCK] "))
        self.assertIn("0.00001234", notifier.message_for({**order, "symbol": "BTC/USD"}))
        self.assertTrue(notifier.message_for({"event": "ERROR", "details": "boom"}).startswith("[STOCK] "))
        self.assertTrue(notifier.message_for({"event": "ERROR", "details": "crypto: boom"}).startswith("[CRYPTO] "))
        self.assertTrue(notifier.message_for({"event": "KILL_SWITCH", "details": "x"}).startswith("[STOCK] [CRYPTO] "))

    def test_daily_summary_carries_both_labels(self):
        self.assertTrue(dashboard.summary_text(0).startswith("[STOCK] [CRYPTO] "))


class CryptoBarsTests(unittest.TestCase):
    def frame(self, symbols, stamps):
        index = pd.MultiIndex.from_product([symbols, pd.DatetimeIndex(stamps)],
                                           names=["symbol", "timestamp"])
        columns = {name: [float(i) for i in range(len(index))]
                   for name in ("open", "high", "low", "close", "volume")}
        return pd.DataFrame(columns, index=index)

    def test_todays_unfinished_bar_is_thrown_away(self):
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        stamps = [today - timedelta(days=2), today - timedelta(days=1), today]
        client = mock.MagicMock()
        client.get_crypto_bars.return_value = SimpleNamespace(df=self.frame(["BTC/USD"], stamps))
        with mock.patch.object(data, "CryptoHistoricalDataClient", return_value=client), \
                mock.patch.object(config, "get_alpaca_keys", return_value=("fake", "fake")):
            tables = data.get_crypto_daily_bars("BTC/USD", today - timedelta(days=5))
        table = tables["BTC/USD"]
        self.assertEqual(len(table), 2)
        self.assertEqual(table.index[-1].date(), (today - timedelta(days=1)).date())
        self.assertEqual(list(table.columns), ["open", "high", "low", "close", "volume"])
        self.assertIsNone(table.index.tz)

    def test_missing_pair_is_left_out(self):
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        client = mock.MagicMock()
        client.get_crypto_bars.return_value = SimpleNamespace(df=self.frame(["BTC/USD"], [today - timedelta(days=1)]))
        with mock.patch.object(data, "CryptoHistoricalDataClient", return_value=client), \
                mock.patch.object(config, "get_alpaca_keys", return_value=("fake", "fake")):
            tables = data.get_crypto_daily_bars(["BTC/USD", "NOPE/USD"], today - timedelta(days=5))
        self.assertEqual(list(tables), ["BTC/USD"])

    def test_backtest_charges_the_higher_crypto_fee(self):
        days = pd.date_range("2024-01-01", periods=120, freq="D")
        closes = [100.0] * 60 + [100.0 + i for i in range(60)]
        prices = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes,
                               "volume": 1.0}, index=days)
        cheap = backtest.simulate("BTC/USD", prices, "2024-01-01", 10000, config.FEE_PCT,
                                  config.SLIPPAGE_PCT, config.STOP_LOSS_PCT)
        dear = backtest.simulate("BTC/USD", prices, "2024-01-01", 10000, config.CRYPTO_FEE_PCT,
                                 config.SLIPPAGE_PCT, config.STOP_LOSS_PCT)
        self.assertGreater(dear["costs"], cheap["costs"])
        self.assertGreater(config.CRYPTO_FEE_PCT, config.FEE_PCT)


if __name__ == "__main__":
    unittest.main()
