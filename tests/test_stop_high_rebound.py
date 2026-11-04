import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kabucom"))

from special_quote import SIGN_SPECIAL_QUOTE, PaperBroker, Phase  # noqa: E402
from stop_high_rebound import (  # noqa: E402
    StopHighConfig, StopHighReboundEngine, collect_stop_high, load_stop_high,
    previous_market_day, save_stop_high,
)
from tick_size import round_up_to_tick  # noqa: E402


def board(price, opening=1000, prev_close=1000, special=False):
    return {
        "CurrentPrice": price,
        "OpeningPrice": opening,
        "PreviousClose": prev_close,
        "BidPrice": price,   # 最良売気配（kabu API の命名）
        "AskSign": SIGN_SPECIAL_QUOTE if special else "0101",
        "BidSign": "0101",
    }


class FakeApi:
    def __init__(self):
        self.boards = {}
        self.symbols = {}
        self.ranking = []

    def get_board(self, symbol):
        return self.boards.get(symbol)

    def get_symbol(self, symbol):
        return self.symbols.get(symbol, {"PriceRangeGroup": "10000"})

    def get_ranking(self, exchange):
        return self.ranking


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class HelperTest(unittest.TestCase):
    def test_round_up_to_tick(self):
        self.assertEqual(round_up_to_tick(1020.4), 1021)
        self.assertEqual(round_up_to_tick(1021), 1021)
        self.assertEqual(round_up_to_tick(3001), 3005)
        self.assertEqual(round_up_to_tick(800.03, "10003"), 800.1)

    def test_previous_market_day(self):
        self.assertEqual(previous_market_day(date(2026, 11, 4)), date(2026, 11, 2))  # 11/3 文化の日
        self.assertEqual(previous_market_day(date(2026, 11, 9)), date(2026, 11, 6))  # 月曜 → 金曜
        self.assertEqual(previous_market_day(date(2027, 1, 4)), date(2026, 12, 30))  # 年末年始

    def test_collect_and_load(self):
        api = FakeApi()
        api.ranking = [
            {"Symbol": "1111", "SymbolName": "張付", "ChangePercentage": 30.0},
            {"Symbol": "2222", "SymbolName": "届かず", "ChangePercentage": 25.0},
            {"Symbol": "3333", "SymbolName": "対象外", "ChangePercentage": 5.0},
        ]
        api.symbols = {"1111": {"UpperLimit": 1300, "PriceRangeGroup": "10000"},
                       "2222": {"UpperLimit": 1300, "PriceRangeGroup": "10000"}}
        api.boards = {"1111": {"CurrentPrice": 1300}, "2222": {"CurrentPrice": 1250}}
        items = collect_stop_high(api, "T")
        self.assertEqual([i["Symbol"] for i in items], ["1111"])

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "list.json")
            save_stop_high(path, date(2026, 11, 2), items)
            self.assertEqual(len(load_stop_high(path, date(2026, 11, 4))), 1)
            self.assertEqual(load_stop_high(path, date(2026, 11, 5)), [])  # 古いリストは使わない


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.list_file = os.path.join(self.tmp.name, "list.json")
        save_stop_high(self.list_file, date(2026, 11, 2), [
            {"Symbol": "1111", "SymbolName": "テスト", "Close": 1000, "PriceRangeGroup": "10000"},
        ])
        self.api = FakeApi()
        self.broker = PaperBroker()
        self.clock = Clock(datetime(2026, 11, 4, 8, 59))  # 水曜日（前営業日 11/2）

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, **kwargs):
        defaults = dict(api_password="x", account_type=4, budget_per_trade=1_000_000,
                        list_file=self.list_file, min_dip_pct=3, rebound_ticks=3,
                        take_profit_pct=2, stop_loss_pct=2)
        defaults.update(kwargs)
        return StopHighReboundEngine(self.api, self.broker, StopHighConfig(**defaults), self.clock)

    def feed(self, e, *prices, opening=1000):
        for p in prices:
            self.api.boards["1111"] = board(p, opening=opening)
            e.tick()
            self.clock.advance(1)

    def test_buys_rebound_after_dip_and_takes_profit(self):
        e = self.engine()
        e.tick()
        self.assertIn("1111", e.watchers)
        self.clock.now = datetime(2026, 11, 4, 9, 0)

        # 寄り 1000 → 1010 → 970 まで下落（基準 1010 から 4%）
        self.feed(e, 1000, 1010, 990, 970, 972)
        self.assertEqual(e.active, {})        # まだ 2ティックしか戻していない
        self.feed(e, 973)                     # 安値 970 + 3ティック
        t = e.active["1111"]
        self.assertEqual(t.buy_price, 973)
        self.assertEqual(t.qty, 900)

        self.feed(e, 973)                     # 約定
        self.assertEqual(t.phase, Phase.HOLDING)
        self.assertEqual(t.tp_price, 993)     # 973 * 1.02 = 992.46 → 993
        self.feed(e, 993)
        self.assertEqual(t.phase, Phase.DONE)
        self.assertEqual(t.pnl, 20 * 900)
        self.assertIn("1111", e.traded_today)

        # 同日の再エントリーはしない
        self.feed(e, 960, 950, 990)
        self.assertEqual(e.active, {})

    def test_gap_down_open_counts_as_dip(self):
        e = self.engine()
        self.clock.now = datetime(2026, 11, 4, 9, 0)
        self.feed(e, 950, 948, opening=950)   # 前日終値 1000 から 5% 安く寄った
        self.assertEqual(e.active, {})
        self.feed(e, 951, opening=950)
        self.assertIn("1111", e.active)

    def test_stop_loss(self):
        e = self.engine()
        self.clock.now = datetime(2026, 11, 4, 9, 0)
        self.feed(e, 1000, 960, 963, 963)
        t = e.active["1111"]
        self.assertEqual(t.phase, Phase.HOLDING)
        self.feed(e, 943, 943, 943)           # 963 * 0.98 = 943.74 を下回る
        self.assertEqual(t.phase, Phase.DONE)
        self.assertIn("損切り", t.exit_reason)
        self.assertEqual(t.pnl, -20 * 900)

    def test_skips_when_still_special_quote_or_dip_too_deep(self):
        e = self.engine(max_dip_pct=10)
        self.clock.now = datetime(2026, 11, 4, 9, 0)
        self.api.boards["1111"] = board(1100, opening=None, special=True)  # 寄らず張り付き
        e.tick()
        self.assertIsNone(e.watchers["1111"].peak)
        self.feed(e, 1000, 880, 890)          # 12% 下落 → 見送り
        self.assertTrue(e.watchers["1111"].gave_up)
        self.assertEqual(e.active, {})

    def test_no_entry_after_entry_end(self):
        e = self.engine()
        self.clock.now = datetime(2026, 11, 4, 10, 0)
        self.feed(e, 1000, 960, 963)
        self.assertEqual(e.active, {})

    def test_unfilled_buy_is_cancelled_and_retried(self):
        e = self.engine(fill_wait_sec=3)
        self.clock.now = datetime(2026, 11, 4, 9, 0)
        self.feed(e, 1000, 960, 963)
        t = e.active["1111"]
        self.api.boards["1111"] = dict(board(965), CurrentPrice=965)
        for _ in range(4):
            self.api.boards["1111"]["CurrentPrice"] = 965  # 指値 963 より上 → 約定しない
            e.tick()
            self.clock.advance(1)
        self.assertEqual(t.phase, Phase.DONE)
        self.assertEqual(t.filled_qty, 0)
        self.assertEqual(e.watchers["1111"].attempts, 1)
        self.assertNotIn("1111", e.traded_today)
        e.tick()
        self.assertIn("1111", e.active)       # 条件を満たしていれば狙い直す

    def test_collects_after_close(self):
        e = self.engine()
        self.api.ranking = [{"Symbol": "9999", "SymbolName": "新", "ChangePercentage": 20.0}]
        self.api.symbols["9999"] = {"UpperLimit": 600, "PriceRangeGroup": "10000"}
        self.api.boards["9999"] = {"CurrentPrice": 600}
        self.clock.now = datetime(2026, 11, 4, 15, 35)
        e.tick()
        items = load_stop_high(self.list_file, date(2026, 11, 5))
        self.assertEqual([i["Symbol"] for i in items], ["9999"])
        self.assertFalse(e.collection_due(self.clock.now))


if __name__ == "__main__":
    unittest.main()
