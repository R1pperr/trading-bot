"""
Whole bot runs with everything outside this computer replaced by fakes.

The Alpaca clients, the function that places orders, Gemini and Telegram are all
fakes here, so no test can reach a real service or place a real order.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from types import SimpleNamespace
from unittest import mock

import pandas as pd

import ai_analyst
import bot
import config
import dashboard

TELEGRAM_ON = {"TELEGRAM_BOT_TOKEN": "123456:FAKE-TOKEN-FOR-TESTS", "TELEGRAM_CHAT_ID": "42"}


def fake_signal(signal="HOLD"):
    reason = "20-day average crossed ABOVE the 50-day average" if signal == "BUY" else "No crossover today"
    return {"date": "2026-10-30", "close": 100.0, "sma_fast": 101.0, "sma_slow": 100.0,
            "trend": "UP", "signal": signal, "reason": reason}


def filled_order(symbol):
    return SimpleNamespace(symbol=symbol, status="filled")


def falling_position():
    return SimpleNamespace(symbol="SPY", qty="10", avg_entry_price="100", current_price="90",
                           market_value="900", unrealized_pl="-100", unrealized_plpc="-0.1")


class BotRunTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)
        self.log_file = os.path.join(self.folder, "trade_log.csv")

        self.trading = mock.MagicMock()
        self.set_account(market_open=True)
        self.trading.get_orders.return_value = []
        self.trading.get_all_positions.return_value = []
        days = pd.bdate_range(end="2026-10-30", periods=config.SLOW_SMA_DAYS + 5)
        self.prices = pd.DataFrame({"close": [100.0] * len(days)}, index=days)

        self.place_order = self.start(mock.patch.object(bot, "place_order",
                                                        return_value=("fake-id", "filled", 100.0)))
        self.signal = self.start(mock.patch.object(bot, "latest_signal", return_value=fake_signal()))
        self.gemini = self.start(mock.patch.object(ai_analyst, "_ask_gemini",
                                                   return_value=(ai_analyst.APPROVE, "Looks steady")))
        self.urlopen = self.start(mock.patch("urllib.request.urlopen"))
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
        self.start(mock.patch.dict(os.environ, TELEGRAM_ON))

    def start(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def set_account(self, market_open=True, equity="100000"):
        self.trading.get_account.return_value = SimpleNamespace(
            trading_blocked=False, account_blocked=False, equity=equity, last_equity="100000")
        self.trading.get_clock.return_value = SimpleNamespace(is_open=market_open)

    def run_bot(self, *flags):
        """One full run. Returns the exit code the workflow would see."""
        bot.RUN_EVENTS.clear()
        self.output = io.StringIO()
        with mock.patch.object(sys, "argv", ["bot.py", *flags]), contextlib.redirect_stdout(self.output):
            with self.assertRaises(SystemExit) as finished:
                bot.main()
        return finished.exception.code

    def messages(self):
        sent = []
        for call in self.urlopen.call_args_list:
            fields = urllib.parse.parse_qs(call.args[0].data.decode())
            sent.append(fields["text"][0])
        return sent

    def status(self):
        with open(os.path.join(dashboard.DATA_DIR, "status.json"), encoding="utf-8") as handle:
            return json.load(handle)

    # --- Runs with no activity -------------------------------------------------
    def test_market_closed_run_sends_nothing_but_updates_the_dashboard(self):
        self.set_account(market_open=False)
        self.assertEqual(self.run_bot(), 0)
        self.assertEqual(self.messages(), [])
        self.assertEqual(self.status()["last_run"]["status"], "market_closed")

    def test_open_market_with_no_signal_sends_nothing(self):
        self.assertEqual(self.run_bot(), 0)
        self.assertEqual(self.messages(), [])
        self.place_order.assert_not_called()
        self.assertEqual(self.status()["last_run"]["status"], "ok")

    def test_no_candidate_means_gemini_is_not_called(self):
        config.USE_AI_ANALYST = True
        for _ in range(4):
            self.run_bot()
        self.gemini.assert_not_called()

    # --- A failed notification must not hurt the run ---------------------------
    def test_telegram_failure_does_not_stop_a_run_or_block_a_trade(self):
        self.signal.return_value = fake_signal("BUY")
        for failure in (urllib.error.URLError("no internet"), TimeoutError("too slow")):
            self.urlopen.side_effect = failure
            self.trading.get_orders.return_value = []
            self.place_order.reset_mock()
            self.assertEqual(self.run_bot(), 0)
            self.place_order.assert_called_once()
            self.assertIn("Telegram message not sent", self.output.getvalue())
            self.assertEqual(self.status()["last_run"]["status"], "ok")

    def test_missing_telegram_settings_change_nothing(self):
        self.signal.return_value = fake_signal("BUY")
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            self.assertEqual(self.run_bot(), 0)
        self.place_order.assert_called_once()
        self.urlopen.assert_not_called()

    # --- Things that should send a message --------------------------------------
    def test_order_sends_one_message_with_the_reason(self):
        self.signal.return_value = fake_signal("BUY")
        self.run_bot()
        self.assertEqual(len(self.messages()), 1)
        message = self.messages()[0]
        for expected in ("BUY", "SPY", "$100.00", "filled", "crossed ABOVE", "AI: OFF"):
            self.assertIn(expected, message)

    def test_stop_loss_sends_one_stop_loss_message(self):
        self.trading.get_all_positions.return_value = [falling_position()]
        self.run_bot()
        self.place_order.assert_called_once()
        self.assertEqual(len(self.messages()), 1)
        self.assertIn("STOP-LOSS", self.messages()[0])

    def test_kill_switch_sends_one_message_all_day(self):
        self.set_account(equity="96000")
        for _ in range(3):
            self.run_bot()
        self.assertEqual(len(self.messages()), 1)
        self.assertIn("KILL SWITCH", self.messages()[0])
        self.assertIs(self.status()["kill_switch"]["on"], True)

    def test_crash_sends_a_message_and_fails_the_run(self):
        self.trading.get_orders.side_effect = RuntimeError("Alpaca fell over")
        self.assertEqual(self.run_bot(), 1)
        self.assertEqual(len(self.messages()), 1)
        self.assertIn("Alpaca fell over", self.messages()[0])
        self.assertEqual(self.status()["last_run"]["status"], "error")

    def test_missing_keys_count_as_a_crash(self):
        config.get_alpaca_keys.side_effect = SystemExit(1)
        self.assertEqual(self.run_bot(), 1)
        self.assertEqual(len(self.messages()), 1)
        self.assertIn("keys", self.messages()[0])

    # --- Four times as many runs -------------------------------------------------
    def test_one_trade_per_symbol_per_day_holds_across_many_runs(self):
        config.USE_AI_ANALYST = True
        self.signal.return_value = fake_signal("BUY")
        self.run_bot()
        self.place_order.assert_called_once()
        self.trading.get_orders.return_value = [filled_order("SPY")]   # Alpaca now shows the order
        for _ in range(29):
            self.assertEqual(self.run_bot(), 0)
        self.place_order.assert_called_once()
        self.gemini.assert_called_once()
        self.assertEqual(len(self.messages()), 1)

    def test_ai_veto_is_asked_and_reported_once_a_day(self):
        config.USE_AI_ANALYST = True
        self.signal.return_value = fake_signal("BUY")
        self.gemini.return_value = (ai_analyst.VETO, "Prices look choppy")
        for _ in range(30):
            self.assertEqual(self.run_bot(), 0)
        self.gemini.assert_called_once()
        self.place_order.assert_not_called()
        self.assertEqual(len(self.messages()), 1)
        self.assertIn("VETOED", self.messages()[0])

    # --- Dry run -----------------------------------------------------------------
    def test_dry_run_orders_nothing_logs_nothing_sends_nothing(self):
        config.USE_AI_ANALYST = True
        self.signal.return_value = fake_signal("BUY")
        self.trading.get_all_positions.return_value = [falling_position()]
        self.assertEqual(self.run_bot("--dry-run"), 0)
        self.place_order.assert_not_called()
        self.gemini.assert_not_called()
        self.urlopen.assert_not_called()
        self.assertFalse(os.path.exists(self.log_file))
        self.assertEqual(self.status()["last_run"]["status"], "dry_run")


if __name__ == "__main__":
    unittest.main()
