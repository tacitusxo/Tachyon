import os
import sys
import tempfile
import unittest
from datetime import date, datetime, time as dt_time, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kabucom"))

from disclosure_score import DisclosureScoreConfig, DisclosureScoreStrategy, aggregate  # noqa: E402
from disclosures import (  # noqa: E402
    CsvDisclosureSource, Disclosure, DisclosureStore, normalize_code, rule_score,
)
from futures_close_momentum import (  # noqa: E402
    FuturesMomentumConfig, FuturesMomentumEngine, PaperFuturesBroker, parse_exposures,
    rebalance_amount,
)
from no_news_reversal import NoNewsReversalConfig, NoNewsReversalStrategy  # noqa: E402
from orb import ORBConfig, ORBEngine  # noqa: E402
from special_quote import PaperBroker, Phase  # noqa: E402
from swing import (  # noqa: E402
    CLOSED, EXIT_PENDING, HOLDING, PaperSwingBroker, PriceHistory, SwingBook, SwingEngine,
    add_market_days, last_market_day_of_month,
)
from topix_rebalance import TopixRebalanceConfig, TopixRebalanceStrategy, default_event_dates  # noqa: E402


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def at(self, d, hh, mm=0):
        self.now = datetime.combine(d, dt_time(hh, mm))
        return self.now


class FakeApi:
    def __init__(self):
        self.boards = {}
        self.symbols = {}
        self.ranking = []
        self.requests = {}

    def get_board(self, symbol):
        return self.boards.get(str(symbol))

    def get_symbol(self, symbol):
        return self.symbols.get(symbol, {"PriceRangeGroup": "10000"})

    def get_ranking(self, exchange, ranking_type=1):
        return self.ranking

    def request(self, method, path, params=None, body=None):
        return self.requests.get(path)


def board(price, opening=None, prev=None, special=False, day=None, volume=0, value=0):
    b = {"CurrentPrice": price, "OpeningPrice": opening, "PreviousClose": prev, "BidPrice": price,
         "AskSign": "0102" if special else "0101", "BidSign": "0101",
         "TradingVolume": volume, "TradingValue": value}
    if day:
        b["CurrentPriceTime"] = f"{day.isoformat()}T15:30:00+09:00"
    return b


def swing_common(tmp, **kw):
    base = dict(api_password="x", account_type=4, budget_per_trade=1_000_000, state_dir=tmp)
    base.update(kw)
    return base


# ---------------------------------------------------------------------------

class DateHelperTest(unittest.TestCase):
    def test_market_days(self):
        self.assertEqual(add_market_days(date(2026, 10, 30), 3), date(2026, 11, 5))  # 11/3 文化の日
        self.assertEqual(last_market_day_of_month(2027, 1), date(2027, 1, 29))
        self.assertEqual(last_market_day_of_month(2026, 12), date(2026, 12, 30))  # 12/31 休場

    def test_default_event_dates(self):
        dates = default_event_dates()
        self.assertEqual(len(dates), 8)
        self.assertEqual(dates[0], date(2026, 10, 30))
        self.assertEqual(dates[1], date(2027, 1, 29))
        self.assertEqual(dates[-1], date(2028, 7, 31))


class PaperSwingBrokerTest(unittest.TestCase):
    def test_fills_and_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "orders.json")
            b = PaperSwingBroker(path)
            day = date(2027, 1, 29)
            o_close = b.order("buy", "1111", 100, "close", datetime.combine(day, dt_time(15, 10)))
            b.observe("1111", board(990), datetime.combine(day, dt_time(15, 20)))
            self.assertFalse(b.get_order(o_close).finished)
            # 再起動しても注文が残り、翌営業日に前日終値で約定する
            b2 = PaperSwingBroker(path)
            b2.observe("1111", board(1010, opening=1005, prev=980), datetime(2027, 2, 1, 9, 1))
            s = b2.get_order(o_close)
            self.assertTrue(s.finished)
            self.assertEqual((s.cum_qty, s.avg_price), (100, 980))
            o_open = b2.order("sell", "1111", 100, "open", datetime(2027, 2, 2, 8, 50))
            b2.observe("1111", board(1000, opening=None, special=True), datetime(2027, 2, 2, 9, 0))
            self.assertFalse(b2.get_order(o_open).finished)
            b2.observe("1111", board(1003, opening=1001), datetime(2027, 2, 2, 9, 5))
            self.assertEqual(b2.get_order(o_open).avg_price, 1001)
            self.assertNotEqual(o_open, o_close)


class PriceHistoryTest(unittest.TestCase):
    def test_history(self):
        with tempfile.TemporaryDirectory() as d:
            h = PriceHistory(os.path.join(d, "h.json"))
            day = date(2027, 1, 18)
            for i in range(5):
                h.record("1111", day + timedelta(days=i), 100 + i, 1000, 100_000)
            self.assertFalse(h.record_board("1111", date(2027, 1, 25), board(50, day=date(2027, 1, 22))))
            h.save()
            h2 = PriceHistory(os.path.join(d, "h.json"))
            self.assertEqual(h2.close_days_ago("1111", 1, date(2027, 1, 23)), 104)
            self.assertEqual(h2.close_days_ago("1111", 5, date(2027, 1, 23)), 100)
            self.assertIsNone(h2.close_days_ago("1111", 6, date(2027, 1, 23)))
            self.assertEqual(h2.avg_volume("1111", 20, date(2027, 1, 23)), 1000)


# ---------------------------------------------------------------------------

def run_until(engine, clock, end, step_min=1):
    while clock.now < end:
        engine.tick()
        clock.now += timedelta(minutes=step_min)


class TopixRebalanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.event = date(2027, 1, 29)
        self.config = TopixRebalanceConfig(**swing_common(self.tmp.name), event_dates=[self.event],
                                           min_avg_value=0, hold_days=2)
        self.api = FakeApi()
        self.history = PriceHistory(os.path.join(self.tmp.name, "h.json"))
        d = date(2027, 1, 13)
        for i in range(12):  # 実施日までの終値: 1000 から横ばい
            self.history.record("1111", d + timedelta(days=i), 1000, 10_000, 10_000_000)
            self.history.record("2222", d + timedelta(days=i), 1000, 10_000, 10_000_000)
        universe = [{"Code": "1111", "Name": "下落", "SellShares": "50000"},
                    {"Code": "2222", "Name": "横ばい"}]
        self.strategy = TopixRebalanceStrategy(self.api, self.config, universe, self.history)

    def tearDown(self):
        self.tmp.cleanup()

    def test_select_only_on_event_day(self):
        self.api.boards = {"1111": board(900), "2222": board(995)}
        cands = self.strategy.select(datetime(2027, 1, 29, 15, 10), self.config.entry_at)
        self.assertEqual([c.symbol for c in cands], ["1111"])
        self.assertEqual(cands[0].kind, "close")
        self.assertIn("5.0日分", cands[0].note)
        self.assertEqual(self.strategy.select(datetime(2027, 1, 28, 15, 10), self.config.entry_at), [])

    def test_full_cycle(self):
        clock = Clock(datetime(2027, 1, 29, 15, 0))
        broker = PaperSwingBroker(os.path.join(self.tmp.name, "orders.json"))
        book = SwingBook(os.path.join(self.tmp.name, "pos.json"))
        engine = SwingEngine(self.strategy, self.api, broker, book, self.config, clock)
        self.api.boards = {"1111": board(900), "2222": board(1000)}
        run_until(engine, clock, datetime(2027, 1, 29, 15, 25))
        self.assertEqual(len(book.open_positions()), 1)
        self.api.boards["1111"] = board(880, day=self.event)   # 終値 880
        run_until(engine, clock, datetime(2027, 1, 29, 15, 40))
        pos = book.open_positions()[0]
        self.assertEqual((pos.status, pos.entry_price), (HOLDING, 880))
        self.assertEqual(pos.exit_date, "2027-02-02")
        # 決済日の朝に寄成、寄り付きで約定
        clock.at(date(2027, 2, 2), 8, 50)
        self.api.boards["1111"] = board(950, opening=None, prev=930)
        engine.tick()
        self.assertEqual(book.open_positions()[0].status, EXIT_PENDING)
        clock.at(date(2027, 2, 2), 9, 1)
        self.api.boards["1111"] = board(952, opening=950, prev=930)
        engine.tick()
        self.assertEqual(book.positions[0].status, CLOSED)
        self.assertEqual(book.positions[0].pnl, (950 - 880) * book.positions[0].qty)
        # ポジションはファイルから復元できる
        self.assertEqual(SwingBook(os.path.join(self.tmp.name, "pos.json")).positions[0].status, CLOSED)

    def test_stop_loss(self):
        self.config.stop_loss_pct = 5
        clock = Clock(datetime(2027, 1, 29, 15, 10))
        broker = PaperSwingBroker(os.path.join(self.tmp.name, "orders.json"))
        book = SwingBook(os.path.join(self.tmp.name, "pos.json"))
        engine = SwingEngine(self.strategy, self.api, broker, book, self.config, clock)
        self.api.boards = {"1111": board(900, day=self.event), "2222": board(1000)}
        engine.tick()
        clock.at(self.event, 15, 31)
        engine.tick()
        clock.at(date(2027, 2, 1), 10, 0)
        self.api.boards["1111"] = board(850, opening=870, prev=900)
        engine.tick()   # 損切り判定 → 成行
        engine.tick()   # 約定
        self.assertEqual(book.positions[0].status, CLOSED)
        self.assertIn("損切り", book.positions[0].exit_reason)


# ---------------------------------------------------------------------------

class DisclosureTest(unittest.TestCase):
    def test_rule_score(self):
        self.assertEqual(rule_score("業績予想の上方修正に関するお知らせ")[:2], ("上方修正", 2))
        self.assertEqual(rule_score("継続企業の前提に関する注記")[1], -3)
        self.assertEqual(rule_score("株主優待制度の新設に関するお知らせ")[:2], ("優待新設", 2))
        self.assertEqual(rule_score("株主優待制度の廃止に関するお知らせ")[1], -2)
        cat, score, _ = rule_score("自己株式取得に係る事項の決定に関するお知らせ",
                                   "取得し得る株式の総数 100万株（発行済株式総数（自己株式を除く）に対する割合 6.2％）")
        self.assertEqual((cat, score), ("自己株式取得", 3))
        cat, score, _ = rule_score("業績予想の修正に関するお知らせ", "経常利益 前回予想比 増減率 35.0%")
        self.assertEqual(score, 2)
        self.assertEqual(normalize_code("72030"), "7203")

    def test_store_update_and_between(self):
        with tempfile.TemporaryDirectory() as d:
            csv_dir = os.path.join(d, "csv")
            os.makedirs(csv_dir)
            with open(os.path.join(csv_dir, "2027-01-28.csv"), "w", encoding="utf-8") as f:
                f.write("Code,Name,Time,Title\n11110,A社,15:40,業績予想の上方修正\n2222,B社,14:00,下方修正\n")
            store = DisclosureStore(os.path.join(d, "store"))
            items = store.update(date(2027, 1, 28), CsvDisclosureSource(csv_dir))
            self.assertEqual(len(items), 2)
            again = store.update(date(2027, 1, 28), CsvDisclosureSource(csv_dir))
            self.assertEqual(len(again), 2)
            after_close = store.between(datetime(2027, 1, 28, 15, 30), datetime(2027, 1, 29, 9, 0))
            self.assertEqual([x.code for x in after_close], ["1111"])

    def test_aggregate_blocks_bad_news(self):
        items = [Disclosure("1111", "A", "2027-01-28T15:40:00", "上方修正", score=2),
                 Disclosure("1111", "A", "2027-01-28T15:41:00", "公募", score=-2)]
        agg = aggregate(items, -1)
        self.assertTrue(agg["1111"]["blocked"])


class ClaudeScorerTest(unittest.TestCase):
    def test_score_with_fake_client(self):
        try:
            import anthropic
        except ImportError:
            self.skipTest("anthropic が入っていない")
        from types import SimpleNamespace
        from disclosures import ClaudeScorer, score_disclosure
        calls = []

        class FakeMessages:
            def create(self, **kw):
                calls.append(kw)
                return SimpleNamespace(stop_reason="end_turn", content=[
                    SimpleNamespace(type="text", text='{"category":"業績修正","score":2.5,"reason":"大幅増額"}')])

        scorer = ClaudeScorer.__new__(ClaudeScorer)
        scorer.anthropic, scorer.model, scorer.effort, scorer.max_body_chars = anthropic, "claude-opus-5-5", "low", 100
        scorer.client = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()))
        d = score_disclosure(Disclosure("1111", "A", "2027-01-28T15:40:00", "業績予想の修正に関するお知らせ",
                                        body="x" * 500), scorer)
        self.assertEqual((d.score, d.scored_by), (2.5, "claude"))
        self.assertEqual(calls[0]["output_config"]["format"]["type"], "json_schema")
        self.assertIn("先頭のみ", calls[0]["messages"][0]["content"])
        self.assertEqual(len(calls), 1)


class DisclosureScoreStrategyTest(unittest.TestCase):
    def test_morning_selection(self):
        with tempfile.TemporaryDirectory() as d:
            store = DisclosureStore(d)
            store.save_day(date(2027, 1, 28), [
                Disclosure("1111", "上方", "2027-01-28T15:40:00", "上方修正", score=2),
                Disclosure("2222", "窓大", "2027-01-28T16:00:00", "上方修正", score=2),
                Disclosure("3333", "場中", "2027-01-28T13:00:00", "上方修正", score=3),
                Disclosure("4444", "寄天", "2027-01-28T17:00:00", "優待新設", score=2),
            ])
            store.save_day(date(2027, 1, 29), [])
            config = DisclosureScoreConfig(**swing_common(d), store_dir=d)
            api = FakeApi()
            api.boards = {"1111": board(1030, opening=1020, prev=1000),
                          "2222": board(1250, opening=1250, prev=1000),
                          "3333": board(1100, opening=1100, prev=1000),
                          "4444": board(1010, opening=1040, prev=1000)}
            s = DisclosureScoreStrategy(api, config, source=None, store=store)
            cands = s.select(datetime(2027, 1, 29, 9, 5), config.morning_at)
            self.assertEqual([c.symbol for c in cands], ["1111"])
            self.assertEqual(cands[0].kind, "market")

    def test_lunch_selection(self):
        with tempfile.TemporaryDirectory() as d:
            store = DisclosureStore(d)
            store.save_day(date(2027, 1, 29), [
                Disclosure("1111", "昼", "2027-01-29T12:00:00", "上方修正", score=2)])
            config = DisclosureScoreConfig(**swing_common(d), store_dir=d)
            api = FakeApi()
            api.boards = {"1111": board(1000, opening=990, prev=980)}
            s = DisclosureScoreStrategy(api, config, source=None, store=store)
            self.assertEqual(s.select(datetime(2027, 1, 29, 12, 25), config.snapshot_at), [])
            api.boards["1111"] = board(1050, opening=990, prev=980)
            cands = s.select(datetime(2027, 1, 29, 12, 35), config.afternoon_at)
            self.assertEqual([c.symbol for c in cands], ["1111"])
            self.assertIn("+5.0%", cands[0].note)


class NoNewsReversalTest(unittest.TestCase):
    def test_select(self):
        with tempfile.TemporaryDirectory() as d:
            store = DisclosureStore(os.path.join(d, "store"))
            friday = date(2027, 1, 29)
            for i in range(10):
                store.save_day(friday - timedelta(days=i), [])
            store.save_day(date(2027, 1, 27), [Disclosure("2222", "悪材料", "2027-01-27T15:00:00", "下方修正", score=-2)])
            history = PriceHistory(os.path.join(d, "h.json"))
            for i in range(20):
                day = friday - timedelta(days=20 - i)
                for code in ("1111", "2222", "3333"):
                    history.record(code, day, 1000, 100_000, 200_000_000)
            config = NoNewsReversalConfig(**swing_common(d), store_dir=store.folder)
            api = FakeApi()
            api.boards = {"1111": board(880), "2222": board(850), "3333": board(990)}
            universe = [{"Code": c, "Name": c} for c in ("1111", "2222", "3333")]
            s = NoNewsReversalStrategy(api, config, universe, history, store)
            cands = s.select(datetime(2027, 1, 29, 15, 10), config.entry_at)
            self.assertEqual([c.symbol for c in cands], ["1111"])
            self.assertEqual(s.select(datetime(2027, 1, 28, 15, 10), config.entry_at), [])  # 木曜

    def test_requires_news_data(self):
        with tempfile.TemporaryDirectory() as d:
            config = NoNewsReversalConfig(**swing_common(d), store_dir=os.path.join(d, "empty"))
            s = NoNewsReversalStrategy(FakeApi(), config, [{"Code": "1111"}],
                                       PriceHistory(os.path.join(d, "h.json")))
            self.assertEqual(s.select(datetime(2027, 1, 29, 15, 10), config.entry_at), [])


# ---------------------------------------------------------------------------

class ORBTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.api.ranking = [{"Symbol": "1111", "SymbolName": "注目"}, {"Symbol": "2222", "SymbolName": "陰線"}]
        self.api.symbols = {"1111": {"PriceRangeGroup": "10000", "UpperLimit": 1300}}
        self.config = ORBConfig(api_password="x", account_type=4, budget_per_trade=1_000_000)
        self.clock = Clock(datetime(2027, 1, 29, 9, 5))
        self.engine = ORBEngine(self.api, PaperBroker(), self.config, self.clock)

    def b(self, price, high, low, opening=1000):
        x = board(price, opening=opening, prev=950)
        x.update(HighPrice=high, LowPrice=low)
        return x

    def test_breakout_and_stop(self):
        self.api.boards = {"1111": self.b(1010, 1020, 995), "2222": self.b(990, 1005, 985)}
        self.engine.tick()
        self.assertEqual(list(self.engine.ranges), ["1111"])
        self.api.boards["1111"] = self.b(1022, 1022, 995)
        self.clock.now += timedelta(seconds=30)
        self.engine.tick()
        trade = self.engine.active["1111"]
        self.assertEqual(trade.stop_price, 994)
        self.engine.tick()   # 約定 → 利確注文
        self.assertEqual(trade.phase, Phase.HOLDING)
        self.assertEqual(trade.tp_price, 1022 + (1022 - 994) * 5)
        self.api.boards["1111"] = self.b(990, 1030, 990)
        for _ in range(3):
            self.engine.tick()
        self.assertNotIn("1111", self.engine.active)
        self.assertIn("1111", self.engine.traded_today)
        self.assertIn("損切り", trade.exit_reason)

    def test_skip_wide_risk(self):
        self.api.boards = {"1111": self.b(1010, 1020, 900), "2222": self.b(990, 1005, 985)}
        self.engine.tick()
        self.api.boards["1111"] = self.b(1025, 1025, 900)
        self.engine.tick()
        self.assertEqual(self.engine.active, {})
        self.assertTrue(self.engine.ranges["1111"].gave_up)


# ---------------------------------------------------------------------------

class FuturesMomentumTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.api.requests = {"/symbolname/future": {"Symbol": "169120019"}}
        self.config = FuturesMomentumConfig(api_password="x", min_move_pct=0.5, stop_loss_pct=0.7)
        self.clock = Clock(datetime(2027, 1, 29, 14, 59))
        self.broker = PaperFuturesBroker()
        self.engine = FuturesMomentumEngine(self.api, self.broker, self.config, self.clock)

    def set_prices(self, index_now, future_now):
        self.api.boards["101"] = {"CurrentPrice": index_now, "PreviousClose": 40000}
        self.api.requests["/board/169120019@2"] = {"CurrentPrice": future_now}

    def test_rebalance_amount(self):
        exposures = parse_exposures("2:1000000000000,-1:100000000000,-2:100000000000")
        # 2倍: 2*1*r, -1倍: (-1)(-2)=2, -2倍: (-2)(-3)=6 → いずれも同じ方向
        self.assertAlmostEqual(rebalance_amount(exposures, 0.01), (2e12 + 2e11 + 6e11) * 0.01)

    def test_long_to_close(self):
        self.set_prices(40400, 40410)   # +1.0%
        self.engine.tick()
        self.assertEqual(self.engine.phase, "idle")
        self.clock.at(date(2027, 1, 29), 15, 0)
        self.engine.tick()
        self.assertEqual((self.engine.phase, self.engine.side), ("entering", "buy"))
        self.engine.tick()
        self.assertEqual((self.engine.phase, self.engine.entry_price), ("holding", 40410))
        self.set_prices(40500, 40520)
        self.clock.at(date(2027, 1, 29), 15, 45)
        self.engine.tick()
        self.assertEqual(self.engine.phase, "done")
        self.assertEqual(self.engine.pnl, (40520 - 40410) * 10)

    def test_short_stop_loss(self):
        self.set_prices(39600, 39600)   # -1.0%
        self.clock.at(date(2027, 1, 29), 15, 0)
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.engine.side, "sell")
        self.set_prices(39900, 39900)   # 建値から +0.76% 逆行
        self.engine.tick()
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.engine.phase, "done")
        self.assertIn("損切り", self.engine.exit_reason)
        self.assertEqual(self.engine.pnl, -(39900 - 39600) * 10)

    def test_small_move_skipped(self):
        self.set_prices(40100, 40100)   # +0.25%
        self.clock.at(date(2027, 1, 29), 15, 0)
        self.engine.tick()
        self.assertEqual(self.engine.phase, "done")
        self.assertIsNone(self.engine.side)


if __name__ == "__main__":
    unittest.main()
