import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kabucom"))

import main  # noqa: E402
from scraper import NikkeiScraper  # noqa: E402
from special_quote import (  # noqa: E402
    SIGN_SPECIAL_QUOTE, PaperBroker, SpecialQuoteConfig, SpecialQuoteTrade,
)
from utils import calculate_purchase_quantity  # noqa: E402

ENV = {"KABUCOM_API_PASSWORD": "x", "AVAILABLE_MARGIN": "1000000", "KABUCOM_ACCOUNT_TYPE": "4"}


class FakeApi:
    def __init__(self, price=1000, prev_close=1000, upper=1150, special=False):
        self.board = {"CurrentPrice": price, "PreviousClose": prev_close,
                      "AskSign": SIGN_SPECIAL_QUOTE if special else "0101"}
        self.upper = upper

    def get_board(self, symbol):
        return dict(self.board)

    def get_symbol(self, symbol):
        return {"PriceRangeGroup": "10000", "UpperLimit": self.upper} if self.upper else {}


class UpwardRevisionTraderTest(unittest.TestCase):
    def make(self, api=None, **kwargs):
        broker = PaperBroker()
        trader = main.UpwardRevisionTrader(api or FakeApi(), broker, 1_000_000, **kwargs)
        return trader, broker

    def test_quantity_is_sized_by_upper_limit(self):
        # 現在値 1000 で計算すると 900株だが、成行は値幅上限 1150 で余力を拘束されるので 700株
        trader, broker = self.make()
        self.assertTrue(trader.on_revision({"symbol": "1234"}))
        order = next(iter(broker.orders.values()))
        self.assertEqual((order.side, order.price, order.qty), ("buy", None, 700))

    def test_upper_limit_falls_back_to_price_limit_table(self):
        trader, broker = self.make(FakeApi(upper=None))
        trader.on_revision({"symbol": "1234"})
        self.assertEqual(next(iter(broker.orders.values())).qty, 600)  # 基準 1000 → 上限 1300

    def test_take_profit_placed_after_fill_and_capped_at_upper_limit(self):
        trader, broker = self.make(take_profit_pct=20)
        trader.on_revision({"symbol": "1234"})
        trader.poll()
        self.assertEqual(trader.pending, {})
        sells = [o for o in broker.orders.values() if o.side == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertEqual((sells[0].qty, sells[0].price), (700, 1150))  # 1200 はストップ高超えなので 1150

    def test_no_fill_while_special_quote(self):
        api = FakeApi(special=True)
        trader, broker = self.make(api)
        trader.on_revision({"symbol": "1234"})
        trader.poll()
        self.assertIn("1234", trader.pending)
        api.board["AskSign"] = "0101"
        trader.poll()
        self.assertEqual(trader.pending, {})

    def test_max_buys_per_day_and_no_duplicate(self):
        trader, _ = self.make(max_buys_per_day=1)
        self.assertTrue(trader.on_revision({"symbol": "1234"}))
        self.assertFalse(trader.on_revision({"symbol": "1234"}))
        self.assertFalse(trader.on_revision({"symbol": "5678"}))

    def test_take_profit_disabled(self):
        trader, broker = self.make(take_profit_pct=0)
        trader.on_revision({"symbol": "1234"})
        trader.poll()
        self.assertEqual([o.side for o in broker.orders.values()], ["buy"])


class MainWiringTest(unittest.TestCase):
    def run_main(self, dry_run_value):
        env = dict(ENV)
        if dry_run_value is not None:
            env["UR_DRY_RUN"] = dry_run_value
        scraper = mock.Mock()
        scraper.run_continuous_monitoring.return_value = None
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(main, "load_environment_variables",
                                  return_value=("x", 1_000_000.0, 4, 1)), \
                mock.patch.object(main, "get_token", return_value="TOKEN"), \
                mock.patch.object(main, "NikkeiScraper", return_value=scraper):
            if dry_run_value is None:
                os.environ.pop("UR_DRY_RUN", None)
            main.main()
        kwargs = scraper.run_continuous_monitoring.call_args.kwargs
        return kwargs["on_detect"].__self__.broker

    def test_default_is_dry_run(self):
        self.assertIsInstance(self.run_main(None), PaperBroker)

    def test_dry_run_true_uses_paper_broker(self):
        self.assertIsInstance(self.run_main("true"), PaperBroker)

    def test_dry_run_false_uses_real_broker(self):
        self.assertIsInstance(self.run_main("false"), main.KabuBroker)


class ScraperTest(unittest.TestCase):
    def setUp(self):
        self.s = NikkeiScraper()

    def test_afternoon_session_until_1530(self):
        # 2026-11-05（木）は営業日。東証の大引けは 15:30
        self.assertTrue(self.s.is_trading_time(datetime(2026, 11, 5, 15, 20)))
        self.assertFalse(self.s.is_trading_time(datetime(2026, 11, 5, 15, 31)))
        self.assertFalse(self.s.is_trading_time(datetime(2026, 11, 5, 12, 0)))

    def test_next_trading_time(self):
        self.assertEqual(self.s.get_next_trading_time(datetime(2026, 11, 5, 8, 0)),
                         datetime(2026, 11, 5, 9, 0))
        self.assertEqual(self.s.get_next_trading_time(datetime(2026, 11, 5, 12, 0)),
                         datetime(2026, 11, 5, 12, 30))
        # 金曜の引け後 → 月曜の寄り
        self.assertEqual(self.s.get_next_trading_time(datetime(2026, 11, 6, 16, 0)),
                         datetime(2026, 11, 9, 9, 0))
        # 大納会の後 → 1/2, 1/3 は休場なので 1/4（月）
        self.assertEqual(self.s.get_next_trading_time(datetime(2026, 12, 30, 16, 0)),
                         datetime(2027, 1, 4, 9, 0))

    def test_keywords(self):
        self.assertTrue(self.s.is_upward_revision("業績予想の上方修正に関するお知らせ")[0])
        self.assertFalse(self.s.is_upward_revision("上期業績の上方修正及び通期業績の下方修正")[0])
        self.assertFalse(self.s.is_upward_revision("（訂正）「業績予想の上方修正」の一部訂正")[0])
        self.assertFalse(self.s.is_upward_revision(None)[0])

    def test_missing_title_does_not_break_detection(self):
        self.s.fetch_page = lambda: "<html></html>"
        self.s.parse_announcements = lambda html: [
            {"symbol": "1234", "company_name": "A", "title": None},
            {"symbol": "5678", "company_name": "B", "title": "上方修正のお知らせ"},
        ]
        self.s.is_initial_load = False
        found = self.s.detect_upward_revisions()
        self.assertEqual([r["symbol"] for r in found], ["5678"])

    def test_continuous_monitoring_calls_back_for_each_detection(self):
        seen = []
        results = [[{"symbol": "1111"}, {"symbol": "2222"}], [{"symbol": "3333"}]]

        def detect():
            if results:
                return results.pop(0)
            raise KeyboardInterrupt

        self.s.is_trading_time = lambda now=None: True
        self.s.detect_upward_revisions = detect
        self.s.handle_detected_revision = lambda r: None
        with mock.patch("scraper.time.sleep"):
            ret = self.s.run_continuous_monitoring(0, on_detect=lambda r: seen.append(r["symbol"]))
        self.assertIsNone(ret)
        self.assertEqual(seen, ["1111", "2222", "3333"])


class TakeProfitCapTest(unittest.TestCase):
    def test_tp_capped_at_upper_limit(self):
        config = SpecialQuoteConfig(api_password="x", account_type=4, budget_per_trade=1e6,
                                    take_profit_ticks=5)
        broker = PaperBroker()
        trade = SpecialQuoteTrade("1234", "A", "10000", 100, broker, config, upper_limit=1148)
        trade.start(1145)
        trade.step(datetime(2026, 11, 5, 9, 1), {"CurrentPrice": 1145, "AskSign": "0101"})
        self.assertEqual(trade.tp_price, 1148)  # 1150 はストップ高超え


class QuantityTest(unittest.TestCase):
    def test_invalid_price(self):
        self.assertEqual(calculate_purchase_quantity(0, 1e6), 0)
        self.assertEqual(calculate_purchase_quantity(None, 1e6), 0)


if __name__ == "__main__":
    unittest.main()
