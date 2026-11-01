"""
The JSON files behind the dashboard, written into a temporary folder.
Alpaca is replaced by a fake, so these tests never touch the network.
"""

import contextlib
import csv
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pandas as pd

import config
import dashboard

ACCOUNT = SimpleNamespace(equity="100500.5", last_equity="100000", cash="95530.5",
                          buying_power="196000")
POSITION = SimpleNamespace(symbol="SPY", qty="7", avg_entry_price="700", current_price="710",
                           market_value="4970", unrealized_pl="70", unrealized_plpc="0.0142857",
                           change_today="0.0123")
DAYS = pd.bdate_range(end="2026-10-30", periods=config.SLOW_SMA_DAYS + 5)
BARS = {"SPY": pd.DataFrame({"close": [123.45] * len(DAYS)}, index=DAYS)}
LOG_COLUMNS = ["time_utc", "event", "symbol", "signal", "signal_date", "details", "ai_decision",
               "ai_reason", "side", "qty", "price", "order_id", "order_status", "equity"]
DAY_START = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)
        self.log_file = os.path.join(self.folder, "trade_log.csv")
        for patcher in (mock.patch.object(dashboard, "DATA_DIR", os.path.join(self.folder, "data")),
                        mock.patch.object(dashboard, "fetch_account",
                                          return_value=(ACCOUNT, [POSITION], True)),
                        mock.patch.object(config, "SYMBOLS", ["SPY", "NEW"]),
                        mock.patch.object(config, "CRYPTO_ENABLED", False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        bars_patcher = mock.patch.object(dashboard, "get_daily_bars", return_value=BARS)
        self.get_bars = bars_patcher.start()
        self.addCleanup(bars_patcher.stop)

    def write_log(self, *rows):
        with open(self.log_file, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=LOG_COLUMNS, restval="")
            writer.writeheader()
            writer.writerows(rows)

    def load(self, name):
        with open(os.path.join(dashboard.DATA_DIR, name), encoding="utf-8") as handle:
            return json.load(handle)

    def update(self, **options):
        with contextlib.redirect_stdout(io.StringIO()):
            dashboard.update(self.log_file, DAY_START, **options)

    def test_every_file_has_the_expected_fields(self):
        self.write_log({"time_utc": "2026-11-02 15:00:00", "event": "ORDER", "symbol": "SPY",
                        "side": "buy", "qty": "7.0", "price": "700.0", "order_status": "filled",
                        "details": "strategy BUY: 7 shares", "ai_decision": "APPROVE",
                        "ai_reason": "Looks steady", "order_id": "order-abc"})
        self.update()

        status = self.load("status.json")
        self.assertEqual(status["last_run"]["status"], "ok")
        self.assertRegex(status["last_run"]["time_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIs(status["market_open"], True)
        self.assertEqual(status["equity"], 100500.5)
        self.assertAlmostEqual(status["day_change_pct"], 0.005005)
        self.assertEqual(status["kill_switch"], {"on": False, "reason": ""})
        self.assertEqual(status["ai_analyst_on"], config.USE_AI_ANALYST)
        self.assertIs(status["paper"], True)

        history = self.load("equity_history.json")
        self.assertEqual(len(history), 1)
        self.assertEqual(set(history[0]), {"time_utc", "equity"})

        position = self.load("positions.json")[0]
        self.assertEqual(set(position), {"symbol", "asset_class", "qty", "avg_entry_price",
                                         "current_price", "market_value", "unrealized_pl",
                                         "unrealized_pl_pct", "change_today_pct", "stop_price"})
        self.assertEqual(position["asset_class"], "stock")
        self.assertEqual(position["unrealized_pl"], 70.0)
        self.assertEqual(position["stop_price"], 651.0)

        trade = self.load("trades.json")[0]
        self.assertEqual(set(trade), {"time_utc", "event", "symbol", "asset_class", "side", "qty",
                                      "price", "order_status", "reason", "ai_decision", "ai_reason"})
        self.assertEqual(trade["asset_class"], "stock")
        self.assertEqual((trade["side"], trade["reason"], trade["ai_decision"]),
                         ("buy", "strategy BUY: 7 shares", "APPROVE"))

    def test_status_has_the_account_figures_and_limits(self):
        self.write_log({"time_utc": "2026-11-02 15:00:00", "event": "ORDER", "symbol": "SPY"},
                       {"time_utc": "2026-11-01 15:00:00", "event": "ORDER", "symbol": "QQQ"})
        self.update()
        status = self.load("status.json")
        self.assertEqual((status["cash"], status["buying_power"]), (95530.5, 196000.0))
        self.assertEqual((status["invested"], status["open_pl"]), (4970.0, 70.0))
        self.assertEqual(status["trades_today"], 1)
        self.assertEqual(status["limits"], {
            "max_trades_per_day": config.MAX_TRADES_PER_DAY,
            "max_position_pct": config.MAX_POSITION_PCT,
            "stop_loss_pct": config.STOP_LOSS_PCT,
            "kill_switch_pct": config.KILL_SWITCH_PCT})

    def test_every_run_is_remembered_with_its_order_count(self):
        self.update(orders=2)
        self.update(exit_code=1, error_text="boom")
        runs = self.load("runs.json")
        self.assertEqual([(run["status"], run["orders"]) for run in runs], [("ok", 2), ("error", 0)])
        self.assertEqual(set(runs[0]), {"time_utc", "status", "orders"})

    def test_run_history_keeps_the_newest_runs(self):
        with mock.patch.object(dashboard, "MAX_RUNS", 2):
            for orders in (1, 2, 3):
                self.update(orders=orders)
        self.assertEqual([run["orders"] for run in self.load("runs.json")], [2, 3])

    def test_watchlist_has_views_in_words_and_never_any_prices(self):
        self.update()
        watchlist = self.load("watchlist.json")
        self.assertRegex(watchlist["updated_for"], r"^\d{4}-\d\d-\d\d$")
        spy, new = watchlist["stocks"]
        self.assertEqual(set(spy), {"symbol", "trend", "signal", "signal_date"})
        self.assertEqual((spy["symbol"], spy["signal"], spy["signal_date"]), ("SPY", "HOLD", "2026-10-30"))
        self.assertEqual((new["symbol"], new["trend"], new["signal"]), ("NEW", "", ""))
        with open(os.path.join(dashboard.DATA_DIR, "watchlist.json"), encoding="utf-8") as handle:
            self.assertNotIn("123.45", handle.read())

    def test_no_public_file_holds_price_history(self):
        self.update()
        for name in os.listdir(dashboard.DATA_DIR):
            with open(os.path.join(dashboard.DATA_DIR, name), encoding="utf-8") as handle:
                self.assertNotIn("123.45", handle.read(), name)

    def test_watchlist_downloads_prices_once_a_day(self):
        for _ in range(4):
            self.update()
        self.get_bars.assert_called_once()

    def test_failed_price_download_keeps_everything_else(self):
        self.update()
        path = os.path.join(dashboard.DATA_DIR, "watchlist.json")
        os.remove(path)
        for failure in (RuntimeError("no prices"), SystemExit(1)):
            self.get_bars.side_effect = failure
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                dashboard.update(self.log_file, DAY_START, orders=1)
            self.assertIn("Could not refresh the stock list", output.getvalue())
            self.assertIn("Dashboard data written", output.getvalue())
            self.assertEqual(self.load("status.json")["last_run"]["status"], "ok")
            self.assertFalse(os.path.exists(path))

    def test_keys_and_order_ids_never_reach_the_public_files(self):
        self.write_log({"time_utc": "2026-11-02 15:00:00", "event": "ORDER", "symbol": "SPY",
                        "side": "buy", "qty": "1", "price": "1", "order_id": "order-abc",
                        "details": "strange error with SECRET123 inside"})
        with mock.patch.dict(os.environ, {"ALPACA_SECRET_KEY": "SECRET123"}):
            self.update(exit_code=1, error_text="crash mentioning SECRET123")
        for name in os.listdir(dashboard.DATA_DIR):
            with open(os.path.join(dashboard.DATA_DIR, name), encoding="utf-8") as handle:
                text = handle.read()
            self.assertNotIn("SECRET123", text, name)
            self.assertNotIn("order-abc", text, name)

    def test_only_orders_and_vetoes_are_listed_newest_first(self):
        self.write_log(
            {"time_utc": "2026-11-02 15:00:00", "event": "SIGNAL", "symbol": "SPY"},
            {"time_utc": "2026-11-02 15:01:00", "event": "ORDER", "symbol": "SPY", "side": "buy",
             "qty": "1", "price": "1"},
            {"time_utc": "2026-11-02 15:02:00", "event": "AI_DECISION", "symbol": "JPM", "signal": "BUY",
             "ai_decision": "APPROVE"},
            {"time_utc": "2026-11-02 15:03:00", "event": "AI_DECISION", "symbol": "QQQ", "signal": "BUY",
             "ai_decision": "VETO", "ai_reason": "Choppy"})
        self.update()
        trades = self.load("trades.json")
        self.assertEqual([row["symbol"] for row in trades], ["QQQ", "SPY"])
        self.assertEqual(trades[0]["side"], "buy")

    def test_status_names(self):
        with mock.patch.object(dashboard, "fetch_account", return_value=(ACCOUNT, [], False)):
            self.update()
            self.assertEqual(self.load("status.json")["last_run"]["status"], "market_closed")
            self.update(dry_run=True)
            self.assertEqual(self.load("status.json")["last_run"]["status"], "dry_run")
            self.update(exit_code=1, error_text="boom")
            status = self.load("status.json")["last_run"]
            self.assertEqual((status["status"], status["message"]), ("error", "boom"))

    def test_kill_switch_stays_on_all_day_once_logged(self):
        self.write_log({"time_utc": "2026-11-02 15:00:00", "event": "KILL_SWITCH", "details": "equity -3.1%"})
        self.update()
        self.assertEqual(self.load("status.json")["kill_switch"], {"on": True, "reason": "equity -3.1%"})

    def test_kill_switch_from_yesterday_does_not_count(self):
        self.write_log({"time_utc": "2026-11-01 15:00:00", "event": "KILL_SWITCH", "details": "old"})
        self.update()
        self.assertIs(self.load("status.json")["kill_switch"]["on"], False)

    def test_alpaca_outage_keeps_last_good_numbers_and_shows_the_error(self):
        self.update()
        with mock.patch.object(dashboard, "fetch_account", side_effect=RuntimeError("Alpaca down")):
            self.update(exit_code=1, error_text="Alpaca down")
        status = self.load("status.json")
        self.assertEqual(status["last_run"]["status"], "error")
        self.assertEqual(status["equity"], 100500.5)
        self.assertEqual(status["cash"], 95530.5)
        self.assertEqual(len(self.load("positions.json")), 1)
        self.assertEqual(len(self.load("equity_history.json")), 1)

    def test_a_failed_write_never_raises(self):
        with mock.patch.object(dashboard, "_write_json", side_effect=OSError("disk full")):
            self.update()

    def test_missing_keys_exit_is_caught(self):
        with mock.patch.object(dashboard, "fetch_account", side_effect=SystemExit(1)):
            self.update(exit_code=1)
        self.assertEqual(self.load("status.json")["last_run"]["status"], "error")

    def test_summary_text_reads_the_files_just_written(self):
        self.update()
        text = dashboard.summary_text(2)
        for expected in ("Daily summary", "$100,500.50", "+0.50%", "Orders today: 2",
                         "Open positions: 1", "Kill switch: off", "Last run: ok"):
            self.assertIn(expected, text)

    def test_summary_text_copes_with_missing_files(self):
        text = dashboard.summary_text(0)
        self.assertIn("Account value: unknown", text)
        self.assertIn("Last run: unknown", text)

    def test_equity_history_rules(self):
        first = dashboard.add_equity_point([], "2026-11-02T15:00:00Z", 100.0)
        self.assertEqual(len(first), 1)
        same_day_same_value = dashboard.add_equity_point(first, "2026-11-02T15:15:00Z", 100.0)
        self.assertEqual(len(same_day_same_value), 1)
        new_value = dashboard.add_equity_point(first, "2026-11-02T15:15:00Z", 101.0)
        self.assertEqual(len(new_value), 2)
        new_day = dashboard.add_equity_point(first, "2026-11-03T15:00:00Z", 100.0)
        self.assertEqual(len(new_day), 2)
        with mock.patch.object(dashboard, "MAX_EQUITY_POINTS", 2):
            capped = dashboard.add_equity_point(new_value, "2026-11-02T15:30:00Z", 102.0)
        self.assertEqual([point["equity"] for point in capped], [101.0, 102.0])


if __name__ == "__main__":
    unittest.main()
