"""
The long lists, the once-a-day signals and the limits on them. Everything outside
this computer is a fake, so no test can reach a real service or place an order.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pandas as pd

import ai_analyst
import bot
import config
import crypto
import dashboard
import data
import signals
import universe


def stock_asset(symbol, exchange="NYSE", name=None, tradable=True, status="active"):
    return {"symbol": symbol, "exchange": exchange, "name": name or f"{symbol} Inc.",
            "tradable": tradable, "status": status}


def table(close=100.0, volume=1000.0, days=20):
    return pd.DataFrame({"close": [close] * days, "volume": [volume] * days})


def fake_signal(signal="HOLD", trend="UP"):
    return {"date": "2026-10-30", "close": 100.0, "trend": trend, "signal": signal,
            "reason": f"{signal} reason (20-day avg 101.00 vs 50-day avg 100.00)"}


class FilterTests(unittest.TestCase):
    def test_stock_filter_drops_otc_and_untradable(self):
        kept = universe.usable_stocks([
            stock_asset("GOOD"), stock_asset("NAS", "NASDAQ"), stock_asset("ARC", "ARCA"),
            stock_asset("PINK", "OTC"), stock_asset("TXS", "TXSE"), stock_asset("BATS", "BATS"),
            stock_asset("NOPE", tradable=False), stock_asset("GONE", status="inactive")])
        self.assertEqual([a["symbol"] for a in kept], ["GOOD", "NAS", "ARC"])

    def test_stock_filter_drops_leveraged_inverse_and_warrants(self):
        names = {"TQQQ": "ProShares UltraPro QQQ", "SOXL": "Direxion Daily Semiconductor Bull 3X Shares",
                 "SQQQ": "ProShares UltraPro Short QQQ", "RWM": "ProShares Short Russell2000",
                 "TSLL": "Direxion Daily TSLA Bull 2X Shares", "ABCW": "ABC Corp Warrants",
                 "SPY": "SPDR S&P 500 ETF Trust", "VCSH": "Vanguard Short-Term Corporate Bond ETF",
                 "CART": "Maplebear Inc. Common Stock", "UCTT": "Ultra Clean Holdings, Inc."}
        kept = universe.usable_stocks([stock_asset(s, name=n) for s, n in names.items()])
        self.assertEqual([a["symbol"] for a in kept], ["SPY", "VCSH", "CART", "UCTT"])

    def test_sub_five_dollar_stocks_are_dropped(self):
        ranked = universe.rank_stocks([stock_asset("CHEAP"), stock_asset("OK")],
                                      {"CHEAP": table(close=4.99, volume=9e9), "OK": table(close=5.0)})
        self.assertEqual([row["symbol"] for row in ranked], ["OK"])

    def test_stocks_with_too_few_days_are_dropped(self):
        ranked = universe.rank_stocks([], {"NEW": table(days=config.UNIVERSE_MIN_DAYS - 1), "OLD": table()})
        self.assertEqual([row["symbol"] for row in ranked], ["OLD"])

    def test_crypto_filter_drops_stablecoins_pegged_coins_and_other_quotes(self):
        rows = [{"symbol": s, "tradable": True, "status": "active"}
                for s in ("BTC/USD", "ETH/USD", "USDT/USD", "USDC/USD", "USDG/USD", "PAXG/USD",
                          "BTC/USDT", "ETH/BTC")]
        rows.append({"symbol": "OLD/USD", "tradable": False, "status": "active"})
        self.assertEqual([a["symbol"] for a in universe.usable_crypto(rows)], ["BTC/USD", "ETH/USD"])

    def test_thin_crypto_pairs_are_dropped(self):
        ranked = universe.rank_crypto([], {"BTC/USD": table(close=1000.0, volume=100.0),
                                           "THIN/USD": table(close=1.0, volume=1.0)})
        self.assertEqual([row["symbol"] for row in ranked], ["BTC/USD"])


class RankingTests(unittest.TestCase):
    def test_most_traded_first_and_size_is_respected(self):
        bars = {f"S{i}": table(volume=1000.0 + i) for i in range(10)}
        with mock.patch.object(config, "STOCK_UNIVERSE_SIZE", 3):
            ranked = universe.rank_stocks([], bars)
        self.assertEqual([row["symbol"] for row in ranked], ["S9", "S8", "S7"])

    def test_liquidity_figure_is_rounded_for_the_public_file(self):
        ranked = universe.rank_stocks([stock_asset("A", name="Alpha Inc.")], {"A": table(close=123.45, volume=98765.0)})
        self.assertEqual(ranked[0], {"symbol": "A", "name": "Alpha Inc.", "avg_dollar_volume": 12000000.0})

    def test_sells_come_first_then_the_most_traded_buys(self):
        rows = [{"symbol": s, "name": s} for s in ("AAA", "BBB", "CCC", "DDD")]
        found = [{"symbol": "DDD", "action": "BUY"}, {"symbol": "BBB", "action": "BUY"},
                 {"symbol": "CCC", "action": "SELL"}, {"symbol": "ZZZ", "action": "BUY"}]
        plan = signals.start_day("stock", "2026-11-02", found, rows, 4)
        self.assertEqual([c["symbol"] for c in plan["candidates"]], ["CCC", "BBB", "DDD", "ZZZ"])
        self.assertEqual(plan["found"], 4)

    def test_only_the_top_candidates_are_kept(self):
        found = [{"symbol": f"S{i}", "action": "BUY"} for i in range(40)]
        plan = signals.start_day("stock", "2026-11-02", found, [], 40)
        self.assertEqual(len(plan["candidates"]), config.MAX_AI_REVIEWS_PER_DAY["stock"])
        self.assertEqual(plan["found"], 40)

    def test_scan_uses_the_same_rules_and_publishes_no_price_figures(self):
        prices = {"NEW": table(days=60), "HELD": table(days=60), "OUT": table(days=60), "SHORT": table(days=10)}
        answers = {"NEW": fake_signal("BUY"), "HELD": fake_signal("HOLD", "DOWN"), "OUT": fake_signal("SELL")}
        order = iter([answers["HELD"], answers["NEW"], answers["OUT"]])
        found = signals.scan(prices, {"HELD"}, lambda p: next(order))
        self.assertEqual([(c["symbol"], c["action"]) for c in found], [("HELD", "SELL"), ("NEW", "BUY")])
        self.assertNotIn("101.00", json.dumps(found))

    def test_limits_keep_exposure_at_or_under_the_whole_account(self):
        total = sum(config.MAX_OPEN_POSITIONS.values())
        self.assertLessEqual(total * config.MAX_POSITION_PCT, 1.0)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)
        for patcher in (mock.patch.object(universe, "FOLDER", self.folder),
                        mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "1:FAKE", "TELEGRAM_CHAT_ID": "42"}),
                        mock.patch.object(config, "get_alpaca_keys", return_value=("fake", "fake"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        universe.save("stock", {"refreshed": "2026-10-25", "symbols": [{"symbol": "OLD", "name": "Old"}]})
        universe.save("crypto", {"refreshed": "2026-10-25", "symbols": [{"symbol": "OLD/USD", "name": "Old"}]})

    def refresh(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return universe.refresh()

    def test_failed_refresh_keeps_the_old_lists_and_sends_an_alert(self):
        with mock.patch.object(universe, "fetch_assets", side_effect=RuntimeError("Alpaca down")), \
                mock.patch("urllib.request.urlopen") as urlopen:
            self.assertEqual(self.refresh(), 1)
        self.assertEqual(universe.rows("stock"), [{"symbol": "OLD", "name": "Old"}])
        self.assertEqual(universe.rows("crypto")[0]["symbol"], "OLD/USD")
        self.assertEqual(urlopen.call_count, 2)
        self.assertIn(b"Universe+refresh+failed", urlopen.call_args_list[0].args[0].data)

    def test_a_suspiciously_short_list_is_refused(self):
        with mock.patch.object(universe, "fetch_assets", return_value=[stock_asset("A")]), \
                mock.patch.object(data, "get_daily_bars", return_value={"A": table()}), \
                mock.patch.object(data, "get_crypto_daily_bars", return_value={}), \
                mock.patch("urllib.request.urlopen"):
            self.assertEqual(self.refresh(), 1)
        self.assertEqual(universe.rows("stock")[0]["symbol"], "OLD")

    def test_good_refresh_saves_the_required_fields(self):
        assets = [stock_asset(f"S{i}") for i in range(config.UNIVERSE_MIN_STOCKS)]
        bars = {f"S{i}": table() for i in range(config.UNIVERSE_MIN_STOCKS)}
        with mock.patch.object(universe, "fetch_assets", return_value=assets), \
                mock.patch.object(data, "get_daily_bars", return_value=bars):
            payload = universe.build("stock")
        self.assertEqual(set(payload["symbols"][0]), {"symbol", "name", "avg_dollar_volume"})
        self.assertRegex(payload["refreshed"], r"^\d{4}-\d\d-\d\d$")

    def test_missing_files_fall_back_to_the_short_lists(self):
        os.remove(os.path.join(self.folder, "stocks.json"))
        with open(os.path.join(self.folder, "crypto.json"), "w") as handle:
            handle.write("not json")
        self.assertEqual([r["symbol"] for r in universe.rows("stock")], config.SYMBOLS)
        self.assertEqual([r["symbol"] for r in universe.rows("crypto")], config.CRYPTO_SYMBOLS)


class BatchTests(unittest.TestCase):
    def test_many_symbols_go_in_batches_not_one_call_each(self):
        symbols = [f"S{i}" for i in range(1201)]
        seen = []

        def download(batch):
            seen.append(len(batch))
            return {symbol: 1 for symbol in batch}

        with mock.patch("time.sleep"):
            tables = data.in_batches(symbols, download)
        self.assertEqual(seen, [500, 500, 201])
        self.assertEqual(len(tables), 1201)

    def test_batches_are_paced_well_under_the_rate_limit(self):
        with mock.patch("time.sleep") as sleep:
            data.in_batches(["A"] * 1500, lambda batch: {})
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.6, 0.6])
        self.assertLess(config.ALPACA_CALLS_PER_MINUTE, 200)

    def test_errors_back_off_and_try_again(self):
        attempts = iter([RuntimeError("429"), RuntimeError("429"), {"A": 1}])

        def download(batch):
            answer = next(attempts)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with mock.patch("time.sleep") as sleep:
            self.assertEqual(data.in_batches(["A"], download), {"A": 1})
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_it_gives_up_after_the_last_try(self):
        calls = []

        def download(batch):
            calls.append(1)
            raise RuntimeError("down")

        with mock.patch("time.sleep"), self.assertRaises(RuntimeError):
            data.in_batches(["A"], download)
        self.assertEqual(len(calls), config.ALPACA_RETRIES)

    def test_stock_requests_stop_before_todays_unfinished_bar(self):
        client = mock.MagicMock()
        client.get_stock_bars.return_value = SimpleNamespace(df=pd.DataFrame())
        with mock.patch.object(data, "StockHistoricalDataClient", return_value=client), \
                mock.patch.object(config, "get_alpaca_keys", return_value=("fake", "fake")):
            data.get_daily_bars(["SPY"], datetime(2026, 1, 1, tzinfo=timezone.utc))
        request = client.get_stock_bars.call_args.args[0]
        self.assertLess(request.end, data.last_complete_day_end().replace(tzinfo=None))


class CachedRunTests(unittest.TestCase):
    """Whole runs: signals once a day, then acting on the saved list."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)
        self.trading = mock.MagicMock()
        self.trading.get_account.return_value = SimpleNamespace(
            trading_blocked=False, account_blocked=False, equity="100000", last_equity="100000",
            cash="95000", buying_power="195000", non_marginable_buying_power="95000")
        self.trading.get_clock.return_value = SimpleNamespace(is_open=True)
        self.trading.get_orders.side_effect = self.orders_so_far
        self.trading.get_all_positions.return_value = []
        self.trading.submit_order.return_value = SimpleNamespace(id="c", status="filled", filled_avg_price="50")
        days = pd.bdate_range(end="2026-10-30", periods=config.SLOW_SMA_DAYS + 5)
        self.prices = pd.DataFrame({"close": [100.0] * len(days)}, index=days)
        self.symbols = [f"S{i}" for i in range(20)]
        self.pairs = [f"C{i}/USD" for i in range(10)]

        self.place_order = self.start(mock.patch.object(bot, "place_order", return_value=("id", "filled", 100.0)))
        self.gemini = self.start(mock.patch.object(ai_analyst, "_ask_gemini", return_value=(ai_analyst.APPROVE, "Fine")))
        self.stock_bars = self.start(mock.patch.object(bot, "get_daily_bars", side_effect=self.bars))
        self.crypto_bars = self.start(mock.patch.object(crypto, "get_crypto_daily_bars", side_effect=self.bars))
        self.start(mock.patch.object(bot, "latest_signal", return_value=fake_signal("BUY")))
        self.start(mock.patch.object(crypto, "latest_signal", return_value=fake_signal("BUY")))
        self.start(mock.patch.object(crypto, "get_crypto_latest_price", return_value=50.0))
        self.start(mock.patch.object(universe, "rows", side_effect=self.rows))
        self.start(mock.patch.object(bot, "latest_price", return_value=100.0))
        self.start(mock.patch.object(bot, "summary_due", return_value=False))
        self.start(mock.patch.object(bot, "TradingClient", return_value=self.trading))
        self.start(mock.patch.object(dashboard, "TradingClient", return_value=self.trading))
        self.start(mock.patch.object(bot, "StockHistoricalDataClient"))
        self.start(mock.patch.object(bot, "LOG_FILE", os.path.join(self.folder, "log.csv")))
        self.start(mock.patch.object(dashboard, "DATA_DIR", os.path.join(self.folder, "data")))
        self.start(mock.patch.object(config, "get_alpaca_keys", return_value=("fake", "fake")))
        self.start(mock.patch.object(config, "USE_AI_ANALYST", True))
        self.start(mock.patch.object(config, "CRYPTO_ENABLED", True))
        self.start(mock.patch.object(config, "STRATEGY_MODE", "crossover"))
        self.start(mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}))

    def start(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def rows(self, market):
        return [{"symbol": s, "name": s} for s in (self.symbols if market == "stock" else self.pairs)]

    def orders_so_far(self, filter):
        """Like the real Alpaca, the fake account remembers the orders placed today."""
        stocks = [SimpleNamespace(symbol=call.args[1], status="filled", asset_class="us_equity")
                  for call in self.place_order.call_args_list]
        coins = [SimpleNamespace(symbol=call.kwargs["order_data"].symbol, status="filled", asset_class="crypto")
                 for call in self.trading.submit_order.call_args_list]
        return stocks + coins

    def bars(self, symbols, start, end=None):
        return {symbol: self.prices for symbol in symbols}

    def run_bot(self, *flags):
        bot.RUN_EVENTS.clear()
        self.output = io.StringIO()
        with mock.patch.object(sys, "argv", ["bot.py", *flags]), contextlib.redirect_stdout(self.output):
            with self.assertRaises(SystemExit) as finished:
                bot.main()
        return finished.exception.code

    def plan(self, market):
        with open(os.path.join(dashboard.DATA_DIR, "candidates.json"), encoding="utf-8") as handle:
            return json.load(handle)[market]

    def test_prices_are_downloaded_once_a_day_not_every_run(self):
        for _ in range(10):
            self.assertEqual(self.run_bot(), 0)
        self.assertEqual(self.stock_bars.call_count, 1)
        self.assertEqual(self.crypto_bars.call_count, 1)

    def test_one_request_covers_the_whole_universe(self):
        self.run_bot()
        self.assertEqual(len(self.stock_bars.call_args.args[0]), 20)

    def test_gemini_cap_holds_however_many_candidates_and_runs(self):
        self.symbols = [f"S{i}" for i in range(500)]
        self.pairs = [f"C{i}/USD" for i in range(32)]
        with mock.patch.object(config, "MAX_TRADES_PER_DAY", 100):
            for _ in range(96):
                self.run_bot()
        self.assertLessEqual(self.gemini.call_count, sum(config.MAX_AI_REVIEWS_PER_DAY.values()))
        self.assertEqual(self.gemini.call_count, 10)

    def test_daily_trade_limit_still_holds(self):
        for _ in range(5):
            self.run_bot()
        self.assertEqual(self.place_order.call_count, config.MAX_TRADES_PER_DAY)
        self.assertEqual(self.trading.submit_order.call_count, config.MAX_TRADES_PER_DAY)

    def test_position_limits_are_respected(self):
        held = [SimpleNamespace(symbol=f"H{i}", asset_class="us_equity", qty="1", avg_entry_price="100",
                                current_price="101", market_value="101", unrealized_pl="1", unrealized_plpc="0.01")
                for i in range(config.MAX_OPEN_POSITIONS["stock"])]
        held += [SimpleNamespace(symbol=f"K{i}USD", asset_class="crypto", qty="1", avg_entry_price="100",
                                 current_price="101", market_value="101", unrealized_pl="1", unrealized_plpc="0.01")
                 for i in range(config.MAX_OPEN_POSITIONS["crypto"])]
        self.trading.get_all_positions.return_value = held
        self.assertEqual(self.run_bot(), 0)
        self.place_order.assert_not_called()
        self.trading.submit_order.assert_not_called()
        self.gemini.assert_not_called()
        self.assertIn("position limit reached (12 stocks)", self.output.getvalue())
        self.assertIn("position limit reached (6 crypto)", self.output.getvalue())

    def test_a_blocked_candidate_is_tried_again_on_later_runs(self):
        self.trading.get_account.return_value.equity = "96000"
        self.run_bot()
        self.place_order.assert_not_called()
        self.assertEqual(self.plan("stock")["candidates"][0]["status"], "waiting")

    def test_stop_loss_is_still_checked_on_every_run(self):
        self.run_bot()
        falling = SimpleNamespace(symbol="S1", asset_class="us_equity", qty="10", avg_entry_price="100",
                                  current_price="90", market_value="900", unrealized_pl="-100", unrealized_plpc="-0.1")
        self.trading.get_all_positions.return_value = [falling]
        before = self.place_order.call_count
        self.run_bot()
        self.assertEqual(self.place_order.call_count, before + 1)
        self.assertEqual(self.place_order.call_args.args[1], "S1")
        self.assertEqual(self.stock_bars.call_count, 1)

    def test_a_held_symbol_outside_the_universe_still_gets_signals(self):
        self.trading.get_all_positions.return_value = [
            SimpleNamespace(symbol="GONE", asset_class="us_equity", qty="1", avg_entry_price="100",
                            current_price="101", market_value="101", unrealized_pl="1", unrealized_plpc="0.01")]
        self.run_bot()
        self.assertIn("GONE", self.stock_bars.call_args.args[0])

    def test_candidates_file_holds_no_prices(self):
        self.run_bot()
        with open(os.path.join(dashboard.DATA_DIR, "candidates.json"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("101.00", text)
        self.assertNotIn('"close"', text)
        self.assertEqual(set(self.plan("crypto")["candidates"][0]) >= {"rank", "symbol", "action", "ai_decision", "status"}, True)

    def test_dry_run_saves_nothing_and_asks_no_ai(self):
        self.run_bot("--dry-run")
        self.gemini.assert_not_called()
        self.place_order.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(dashboard.DATA_DIR, "candidates.json")))

    def test_a_new_day_scans_again(self):
        self.run_bot()
        plan = self.plan("stock")
        plan["day"] = "2000-01-01"
        dashboard.save_candidates("stock", plan)
        self.run_bot()
        self.assertEqual(self.stock_bars.call_count, 2)
        self.assertEqual(self.crypto_bars.call_count, 1)

    def test_market_closed_means_no_stock_scan_but_crypto_still_scans(self):
        self.trading.get_clock.return_value = SimpleNamespace(is_open=False)
        self.run_bot()
        self.stock_bars.assert_not_called()
        self.assertEqual(self.crypto_bars.call_count, 1)


if __name__ == "__main__":
    unittest.main()
