import os
import sys
import tempfile
import unittest
from datetime import date, datetime, time as dt_time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest import data as d  # noqa: E402
from backtest.data import CsvDataSource, DailyBar, MinuteBar, Revision  # noqa: E402
from backtest.dummy import generate  # noqa: E402
from backtest.sim import (  # noqa: E402
    Trade, apply_position_limit, price_path, simulate_exit, summarize,
)
from backtest.strategies import (  # noqa: E402
    BacktestParams, backtest_special_quote, backtest_stop_high, backtest_upward_revision,
    find_stop_high, load_params,
)
from price_limit import upper_limit  # noqa: E402

D1, D2, D3 = date(2026, 11, 2), date(2026, 11, 4), date(2026, 11, 5)


def bar(day, hhmm, o, h, l, c):
    hh, mm = map(int, hhmm.split(":"))
    return MinuteBar(datetime.combine(day, dt_time(hh, mm)), o, h, l, c, 100)


def line(day, start, prices):
    """終値の並びから分足を作る（始値 = 前の終値）"""
    hh, mm = map(int, start.split(":"))
    bars, prev = [], prices[0]
    for i, p in enumerate(prices):
        t = datetime.combine(day, dt_time(hh + (mm + i) // 60, (mm + i) % 60))
        bars.append(MinuteBar(t, prev, max(prev, p), min(prev, p), p, 100))
        prev = p
    return bars


class SimTest(unittest.TestCase):
    def test_price_path_order(self):
        up = bar(D2, "09:00", 100, 110, 95, 105)
        down = bar(D2, "09:01", 105, 108, 90, 92)
        # 陽線は 始→安→高→終、陰線は 始→高→安→終
        self.assertEqual([p for _, p in price_path([up, down])], [100, 95, 110, 105, 105, 108, 90, 92])

    def test_simulate_exit(self):
        path = [(datetime(2026, 11, 4, 9, 1), 1001), (datetime(2026, 11, 4, 9, 2), 1010)]
        self.assertEqual(simulate_exit(iter(path), 1010, 990, dt_time(15, 20), 1, None).reason, "利確")
        path = [(datetime(2026, 11, 4, 9, 1), 989)]
        e = simulate_exit(iter(path), 1010, 990, dt_time(15, 20), 1, None)
        self.assertEqual((e.reason, e.price), ("損切り", 988))
        path = [(datetime(2026, 11, 4, 15, 20), 1000)]
        self.assertEqual(simulate_exit(iter(path), 1010, 990, dt_time(15, 20), 0, None).reason, "強制決済")
        path = [(datetime(2026, 11, 4, 15, 30), 1003)]
        e = simulate_exit(iter(path), 1010, 990, dt_time(23, 59), 1, None)
        self.assertEqual((e.reason, e.price), ("引け", 1003))

    def test_position_limit_and_summary(self):
        def t(code, h1, h2, pnl_per_share):
            return Trade("x", code, "", datetime(2026, 11, 4, *h1), 1000, 100,
                         datetime(2026, 11, 4, *h2), 1000 + pnl_per_share, "")
        trades = [t("A", (9, 0), (9, 30), 10), t("B", (9, 10), (9, 20), 5),
                  t("C", (9, 30), (9, 40), -20), t("A", (10, 0), (10, 5), 1)]
        kept = apply_position_limit(trades, 1)
        self.assertEqual([x.code for x in kept], ["A", "C"])  # B は保有中、2回目の A は同日同銘柄
        s = summarize("x", kept, [D2])
        self.assertEqual((s.trades, s.wins, s.total_pnl, s.max_drawdown), (2, 1, -1000, 2000))


class StrategyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.params, self.sq, self.shr = load_params({"SHR_BUDGET_PER_TRADE": "1000000",
                                                     "SQ_BUDGET_PER_TRADE": "1000000"})
        d.write_symbols(self.root, [{"Code": "1111", "Name": "A", "PriceRangeGroup": "10000"},
                                    {"Code": "2222", "Name": "B", "PriceRangeGroup": "10000"}])

    def tearDown(self):
        self.tmp.cleanup()

    def ds(self):
        return CsvDataSource(self.root)

    def test_stop_high(self):
        # D1 終値 990 → D2 ストップ高 1140 → D3 押し目から反発して利確
        d.write_daily(self.root, D1, [DailyBar("1111", 990, 990, 990, 990, 1)])
        d.write_daily(self.root, D2, [DailyBar("1111", 1000, 1140, 990, 1140, 1)])
        d.write_daily(self.root, D3, [DailyBar("1111", 1140, 1150, 1100, 1140, 1)])
        self.assertEqual(upper_limit(990), 1140)
        self.assertEqual(find_stop_high(self.ds(), D1, D2), ["1111"])
        d.write_minute(self.root, D3, "1111",
                       line(D3, "09:00", [1140, 1120, 1100, 1102, 1103, 1110, 1130, 1150]))
        trades = backtest_stop_high(self.ds(), [D1, D2, D3], self.params, self.shr)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t.entry_price, 1104)       # 安値 1100 + 3ティックの 1103 で検出、+1ティック
        self.assertEqual(t.exit_reason, "利確")
        self.assertEqual(t.exit_price, 1127)        # 1104 * 1.02 = 1126.08 → 1127
        self.assertEqual(t.qty, 800)

    def test_upward_revision_only_in_session(self):
        d.write_daily(self.root, D2, [DailyBar("1111", 1000, 1100, 990, 1050, 1)])
        d.write_minute(self.root, D2, "1111", line(D2, "10:00", [1000, 1001, 1005, 1030]))
        d.write_revisions(self.root, [
            Revision("1111", datetime(2026, 11, 4, 10, 0), True, "上方修正"),
            Revision("1111", datetime(2026, 11, 4, 15, 30), True, "引け後"),
            Revision("2222", datetime(2026, 11, 4, 10, 0), False, "下方修正"),
        ])
        trades = backtest_upward_revision(self.ds(), [D2], self.params)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].entry_price, 1001)  # 10:01 の始値 1000 + 1ティック
        self.assertEqual(trades[0].exit_reason, "利確")  # 1001 * 1.02 = 1021.02 → 1022
        self.assertEqual(trades[0].exit_price, 1022)

    def test_special_quote_needs_delayed_open(self):
        d.write_daily(self.root, D1, [DailyBar("1111", 1000, 1000, 1000, 1000, 1),
                                      DailyBar("2222", 1000, 1000, 1000, 1000, 1)])
        d.write_daily(self.root, D2, [DailyBar("1111", 1080, 1090, 1070, 1085, 1),
                                      DailyBar("2222", 1100, 1110, 1090, 1100, 1)])
        d.write_minute(self.root, D2, "1111", line(D2, "09:15", [1080, 1081, 1082]))  # 寄り遅れ
        d.write_minute(self.root, D2, "2222", line(D2, "09:00", [1100, 1101, 1102]))  # 9:00 に寄った
        trades = backtest_special_quote(self.ds(), [D1, D2], self.params, self.sq)
        self.assertEqual([t.code for t in trades], ["1111"])
        self.assertEqual((trades[0].entry_price, trades[0].exit_price), (1080, 1082))


class DummyTest(unittest.TestCase):
    def test_generate_and_run(self):
        from backtest.__main__ import run
        with tempfile.TemporaryDirectory() as tmp:
            generate(os.path.join(tmp, "data"), date(2026, 1, 5), date(2026, 3, 31), seed=3, n_symbols=80)
            ds = CsvDataSource(os.path.join(tmp, "data"))
            for day in ds.trading_days()[:5]:
                for b in ds.daily(day).values():
                    self.assertTrue(b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high)
            trades = run(os.path.join(tmp, "data"), out=os.path.join(tmp, "out"))
            self.assertTrue(os.path.exists(os.path.join(tmp, "out", "trades.csv")))
            self.assertTrue(trades)


if __name__ == "__main__":
    unittest.main()
