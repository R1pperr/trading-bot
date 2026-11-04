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
import isolate  # noqa: F401  (keeps tests away from the real control, feed and watchdog files)
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pandas as pd

import ai_analyst
import bot
import config
import dashboard
import universe

TELEGRAM_ON = {"TELEGRAM_BOT_TOKEN": "123456:FAKE-TOKEN-FOR-TESTS", "TELEGRAM_CHAT_ID": "42"}
REAL_SUMMARY_DUE = bot.summary_due


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
        self.summary_due = self.start(mock.patch.object(bot, "summary_due", return_value=False))
        self.start(mock.patch.object(bot, "TradingClient", return_value=self.trading))
        self.start(mock.patch.object(dashboard, "TradingClient", return_value=self.trading))
        self.start(mock.patch.object(bot, "StockHistoricalDataClient"))
        self.start(mock.patch.object(bot, "get_daily_bars", return_value={"SPY": self.prices}))
        self.start(mock.patch.object(universe, "rows", return_value=[{"symbol": "SPY", "name": "SPDR"}]))
        self.start(mock.patch.object(bot, "latest_price", return_value=100.0))
        self.start(mock.patch.object(bot, "LOG_FILE", self.log_file))
        self.start(mock.patch.object(dashboard, "DATA_DIR", os.path.join(self.folder, "data")))
        self.start(mock.patch.object(config, "get_alpaca_keys", return_value=("fake", "fake")))
        self.start(mock.patch.object(config, "SYMBOLS", ["SPY"]))
        self.start(mock.patch.object(dashboard, "fetch_orders", return_value=([], {})))
        self.start(mock.patch.object(config, "require_paper_account"))
        self.start(mock.patch.object(config, "AI_VETO_MODE", "enforce"))
        self.start(mock.patch.object(config, "USE_AI_ANALYST", False))
        self.start(mock.patch.object(config, "CRYPTO_ENABLED", False))   # these tests are about stocks
        self.start(mock.patch.object(config, "STRATEGY_MODE", "crossover"))
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
            shutil.rmtree(dashboard.DATA_DIR, ignore_errors=True)   # a fresh day
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

    def test_ai_veto_is_asked_once_and_sends_no_instant_message(self):
        config.USE_AI_ANALYST = True
        self.signal.return_value = fake_signal("BUY")
        self.gemini.return_value = (ai_analyst.VETO, "Prices look choppy")
        for _ in range(30):
            self.assertEqual(self.run_bot(), 0)
        self.gemini.assert_called_once()
        self.place_order.assert_not_called()
        self.assertEqual(self.messages(), [])

    # --- Repeated crashes ---------------------------------------------------------
    def test_the_same_crash_alerts_once_a_day(self):
        self.trading.get_orders.side_effect = RuntimeError("Alpaca fell over")
        for _ in range(5):
            self.assertEqual(self.run_bot(), 1)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.status()["last_run"]["status"], "error")

    def test_numbers_inside_an_error_do_not_make_it_a_new_problem(self):
        for request_id in (123, 456, 789):
            self.trading.get_orders.side_effect = RuntimeError(f"request {request_id} failed")
            self.run_bot()
        self.assertEqual(len(self.messages()), 1)

    def test_a_different_crash_still_alerts(self):
        for problem in ("Alpaca fell over", "Prices did not load"):
            self.trading.get_orders.side_effect = RuntimeError(problem)
            self.run_bot()
        self.assertEqual(len(self.messages()), 2)

    def test_repeat_crash_filter_cannot_hide_other_alerts(self):
        self.signal.return_value = fake_signal("BUY")
        self.trading.get_orders.side_effect = RuntimeError("Alpaca fell over")
        self.run_bot()
        self.run_bot()
        self.trading.get_orders.side_effect = None
        self.run_bot()
        self.assertEqual(len(self.messages()), 2)
        self.assertIn("BUY", self.messages()[1])

    # --- Daily summary ---------------------------------------------------------
    def test_summary_is_sent_once_a_day(self):
        self.summary_due.return_value = True
        self.set_account(market_open=False, equity="100500")
        for _ in range(3):
            self.assertEqual(self.run_bot(), 0)
        self.assertEqual(len(self.messages()), 1)
        for expected in ("Daily summary", "$100,500.00", "+0.50%", "Orders today: 0",
                         "Open positions: 0", "Kill switch: off", "market closed"):
            self.assertIn(expected, self.messages()[0])

    def test_summary_counts_todays_orders(self):
        self.signal.return_value = fake_signal("BUY")
        self.run_bot()
        self.summary_due.return_value = True
        self.trading.get_orders.return_value = [filled_order("SPY")]
        self.run_bot()
        self.assertIn("Orders today: 1", self.messages()[-1])

    def test_failed_summary_tries_again_on_the_next_run(self):
        self.summary_due.return_value = True
        self.urlopen.side_effect = TimeoutError("too slow")
        self.assertEqual(self.run_bot(), 0)
        self.urlopen.side_effect = None
        self.urlopen.reset_mock()
        self.assertEqual(self.run_bot(), 0)
        self.assertEqual(len(self.messages()), 1)

    def test_summary_without_telegram_settings_changes_nothing(self):
        self.summary_due.return_value = True
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            self.assertEqual(self.run_bot(), 0)
        self.urlopen.assert_not_called()

    def test_summary_problem_cannot_change_a_runs_result(self):
        self.summary_due.return_value = True
        with mock.patch.object(dashboard, "summary_text", side_effect=RuntimeError("bad file")):
            self.assertEqual(self.run_bot(), 0)
        self.assertIn("Daily summary not sent", self.output.getvalue())

    def test_summary_is_due_only_on_weekdays_from_the_set_time(self):
        self.start(mock.patch.object(bot, "summary_due", wraps=REAL_SUMMARY_DUE))
        at = lambda *when: datetime(*when, tzinfo=timezone.utc)
        self.assertFalse(bot.summary_due(at(2026, 11, 2, 20, 52)))   # Monday 15:52 in New York, market still open
        self.assertTrue(bot.summary_due(at(2026, 11, 2, 21, 6)))     # Monday 16:06 in New York, after the close
        self.assertTrue(bot.summary_due(at(2026, 11, 2, 21, 40)))    # Monday, last run was late
        self.assertFalse(bot.summary_due(at(2026, 11, 7, 21, 6)))    # Saturday

    def test_the_summary_comes_after_the_close_in_summer_and_winter(self):
        self.start(mock.patch.object(bot, "summary_due", wraps=REAL_SUMMARY_DUE))
        at = lambda *when: datetime(*when, tzinfo=timezone.utc)
        self.assertFalse(bot.summary_due(at(2026, 7, 6, 19, 55)))    # 15:55 in New York, summer
        self.assertTrue(bot.summary_due(at(2026, 7, 6, 20, 6)))      # 16:06 in New York, summer
        self.assertFalse(bot.summary_due(at(2026, 1, 5, 20, 45)))    # 15:45 in New York, winter: the old bug
        self.assertTrue(bot.summary_due(at(2026, 1, 5, 21, 6)))      # 16:06 in New York, winter

    # --- Dry run -----------------------------------------------------------------
    def test_dry_run_sends_no_summary(self):
        self.summary_due.return_value = True
        self.assertEqual(self.run_bot("--dry-run"), 0)
        self.urlopen.assert_not_called()

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
