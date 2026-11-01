"""
Telegram alerts, tested with the network call replaced by a fake.

Run all tests from the project folder:   python -m unittest discover -s tests
"""

import contextlib
import io
import os
import unittest
import urllib.error
from unittest import mock

import notifier

TOKEN = "123456:FAKE-TOKEN-FOR-TESTS"
TELEGRAM_ON = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}

ORDER = {"event": "ORDER", "symbol": "SPY", "side": "buy", "qty": 7.0, "price": 770.12,
         "order_status": "filled", "ai_decision": "APPROVE", "ai_reason": "Trend looks steady",
         "details": "strategy BUY: 7 shares · 20-day average crossed ABOVE the 50-day average"}


class SendTests(unittest.TestCase):
    def test_missing_settings_skip_silently(self):
        for missing in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            settings = {key: value for key, value in TELEGRAM_ON.items() if key != missing}
            output = io.StringIO()
            with mock.patch.dict(os.environ, settings, clear=True), \
                    mock.patch("urllib.request.urlopen") as urlopen, \
                    contextlib.redirect_stdout(output):
                self.assertFalse(notifier.send("hello"))
            urlopen.assert_not_called()
            self.assertEqual(output.getvalue(), "")

    def test_network_failure_is_reported_but_never_raised(self):
        failures = [urllib.error.URLError("no internet"), TimeoutError("too slow"), ValueError("odd")]
        for failure in failures:
            output = io.StringIO()
            with mock.patch.dict(os.environ, TELEGRAM_ON, clear=True), \
                    mock.patch("urllib.request.urlopen", side_effect=failure), \
                    contextlib.redirect_stdout(output):
                self.assertFalse(notifier.send("hello"))
            self.assertIn("Telegram message not sent", output.getvalue())

    def test_token_never_appears_in_a_printed_error(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, TELEGRAM_ON, clear=True), \
                mock.patch("urllib.request.urlopen", side_effect=RuntimeError(f"bad url .../bot{TOKEN}/send")), \
                contextlib.redirect_stdout(output):
            notifier.send("hello")
        self.assertNotIn(TOKEN, output.getvalue())

    def test_keys_are_blanked_and_long_messages_are_cut(self):
        with mock.patch.dict(os.environ, {**TELEGRAM_ON, "ALPACA_SECRET_KEY": "SECRET123"}, clear=True), \
                mock.patch("urllib.request.urlopen") as urlopen:
            self.assertTrue(notifier.send("key SECRET123 " + "x" * 9000))
        request = urlopen.call_args.args[0]
        self.assertNotIn(b"SECRET123", request.data)
        self.assertLess(len(request.data), 5000)


class MessageTests(unittest.TestCase):
    def test_order_message_has_every_detail(self):
        message = notifier.message_for(ORDER)
        for expected in ("BUY", "SPY", "7", "$770.12", "filled", "crossed ABOVE",
                         "APPROVE", "Trend looks steady"):
            self.assertIn(expected, message)

    def test_unfilled_order_says_placed(self):
        message = notifier.message_for({**ORDER, "order_status": "accepted"})
        self.assertIn("placed (accepted)", message)

    def test_stop_loss_message(self):
        message = notifier.message_for({"event": "ORDER", "symbol": "AAPL", "side": "sell", "qty": 5.0,
                                        "price": 90.0, "order_status": "filled",
                                        "details": "stop-loss: -10.0% from buy price $100.00 (limit -7%)"})
        for expected in ("STOP-LOSS", "SELL", "AAPL", "$90.00", "-10.0%", "not asked"):
            self.assertIn(expected, message)

    def test_kill_switch_veto_and_crash_messages(self):
        self.assertIn("KILL SWITCH", notifier.message_for({"event": "KILL_SWITCH", "details": "equity -3.2%"}))
        veto = notifier.message_for({"event": "AI_DECISION", "ai_decision": "VETO", "signal": "BUY",
                                     "symbol": "JPM", "ai_reason": "Prices look choppy"})
        for expected in ("VETOED", "BUY", "JPM", "Prices look choppy"):
            self.assertIn(expected, veto)
        self.assertIn("boom", notifier.message_for({"event": "ERROR", "details": "boom"}))

    def test_quiet_events_send_nothing(self):
        quiet = [{"event": "SIGNAL"}, {"event": "BLOCKED"},
                 {"event": "AI_DECISION", "ai_decision": "APPROVE"}]
        for row in quiet:
            self.assertIsNone(notifier.message_for(row))

    def test_one_bad_row_does_not_stop_the_others(self):
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(notifier, "send") as send:
            notifier.notify([{"event": "ORDER"}, {"event": "ERROR", "details": "boom"}])
        send.assert_called_once()
        self.assertIn("boom", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
