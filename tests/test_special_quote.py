import os
import sys
import unittest
from datetime import datetime, time as dt_time, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kabucom"))

from special_quote import (  # noqa: E402
    SIGN_SPECIAL_QUOTE, OrderStatus, PaperBroker, Phase, SpecialQuoteConfig,
    SpecialQuoteEngine, SpecialQuoteTrade,
)
from tick_size import add_ticks, round_down_to_tick, tick_size  # noqa: E402


class TickSizeTest(unittest.TestCase):
    def test_standard_table(self):
        self.assertEqual(tick_size(2999), 1)
        self.assertEqual(tick_size(3000), 1)
        self.assertEqual(tick_size(3005), 5)
        self.assertEqual(add_ticks(1000, 2), 1002)
        self.assertEqual(add_ticks(1000, -3), 997)

    def test_crossing_band(self):
        self.assertEqual(add_ticks(2999, 2), 3005)   # 2999 -> 3000 -> 3005
        self.assertEqual(add_ticks(3005, -2), 2999)  # 3005 -> 3000 -> 2999

    def test_topix500_table(self):
        self.assertEqual(tick_size(800, "10003"), 0.1)
        self.assertEqual(add_ticks(1500, 2, "10003"), 1501)
        self.assertEqual(add_ticks(999.9, 2, "10003"), 1000.5)

    def test_round_down(self):
        self.assertEqual(round_down_to_tick(1050.7), 1050)
        self.assertEqual(round_down_to_tick(3009.9), 3005)
        self.assertEqual(round_down_to_tick(999.95, "10003"), 999.9)


def board(price, ask, special=True, prev_close=None):
    b = {
        "CurrentPrice": price,
        "AskPrice": ask,
        "AskSign": SIGN_SPECIAL_QUOTE if special else "0101",
    }
    if prev_close is not None:
        b["PreviousClose"] = prev_close
    return b


class FakeApi:
    def __init__(self):
        self.boards = {}
        self.ranking = []
        self.upper_limit = 1500

    def get_board(self, symbol):
        return self.boards.get(symbol)

    def get_symbol(self, symbol):
        return {"PriceRangeGroup": "10000", "UpperLimit": self.upper_limit}

    def get_ranking(self, exchange):
        return self.ranking


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def make_config(**kwargs):
    defaults = dict(api_password="x", account_type=4, budget_per_trade=1_000_000,
                    scan_interval_sec=0, take_profit_ticks=2, stop_loss_ticks=3,
                    entry_mode="chase")
    defaults.update(kwargs)
    return SpecialQuoteConfig(**defaults)


class EngineScenarioTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.broker = PaperBroker()
        # 2026-11-04 は水曜日（平日）
        self.clock = Clock(datetime(2026, 11, 4, 9, 5))
        self.api.ranking = [{"Symbol": "1234", "SymbolName": "テスト"},
                            {"Symbol": "5678", "SymbolName": "通常"}]
        self.api.boards["1234"] = board(900, 1000)
        self.api.boards["5678"] = board(500, 500, special=False)

    def engine(self, **kwargs):
        return SpecialQuoteEngine(self.api, self.broker, make_config(**kwargs), self.clock)

    def trade(self, engine):
        return engine.active.get("1234")

    def test_enters_only_special_quote_and_chases_raise(self):
        e = self.engine()
        e.tick()
        t = self.trade(e)
        self.assertIsNotNone(t)
        self.assertNotIn("5678", e.active)
        self.assertEqual(t.buy_price, 1000)
        self.assertEqual(t.qty, 900)  # 90万円 / 1000円 を100株単位で切り捨て

        # 気配切り上げ → 取消 → 次ステップで再発注
        self.api.boards["1234"] = board(900, 1020)
        e.tick()
        self.assertEqual(t.pending_cancel, "reorder")
        e.tick()
        self.assertEqual(t.buy_price, 1020)
        self.assertEqual(t.reorder_count, 1)

        # 寄り付いて約定 → 利確注文
        self.api.boards["1234"] = board(1020, 1021, special=False)
        e.tick()
        self.assertEqual(t.phase, Phase.HOLDING)
        self.assertEqual(t.avg_price, 1020)
        self.assertEqual(t.tp_price, 1022)
        self.assertEqual(t.sl_price, 1017)

        # 利確
        self.api.boards["1234"] = board(1022, 1023, special=False)
        e.tick()
        self.assertEqual(t.phase, Phase.DONE)
        self.assertEqual(t.pnl, 2 * 900)
        self.assertNotIn("1234", e.active)

        # 同日同銘柄は再エントリーしない
        self.api.boards["1234"] = board(1022, 1100)
        e.tick()
        self.assertNotIn("1234", e.active)

    def test_stop_loss(self):
        e = self.engine()
        e.tick()
        t = self.trade(e)
        self.api.boards["1234"] = board(1000, 1001, special=False)
        e.tick()
        self.assertEqual(t.phase, Phase.HOLDING)

        self.api.boards["1234"] = board(997, 998, special=False)
        e.tick()  # 損切り判定 → 利確注文取消
        e.tick()  # 成行売り発注
        e.tick()  # 約定
        self.assertEqual(t.phase, Phase.DONE)
        self.assertIn("損切り", t.exit_reason)
        self.assertEqual(t.pnl, -3 * 900)

    def test_force_exit(self):
        e = self.engine()
        e.tick()
        t = self.trade(e)
        self.api.boards["1234"] = board(1000, 1001, special=False)
        e.tick()
        self.clock.now = datetime(2026, 11, 4, 15, 20)
        for _ in range(3):
            e.tick()
        self.assertEqual(t.phase, Phase.DONE)
        self.assertEqual(t.exit_reason, "強制決済時刻")

    def test_gives_up_when_chase_limit_exceeded(self):
        e = self.engine(max_chase_pct=3)
        e.tick()
        t = self.trade(e)
        self.api.boards["1234"] = board(900, 1040)  # +4%
        e.tick()
        self.assertEqual(t.pending_cancel, "abort")
        e.tick()
        self.assertEqual(t.phase, Phase.DONE)
        self.assertEqual(t.filled_qty, 0)

    def test_cancels_if_unfilled_after_open(self):
        e = self.engine(fill_wait_after_open_sec=10)
        e.tick()
        t = self.trade(e)
        self.api.boards["1234"] = board(1050, 1051, special=False)  # 指値より上で寄った
        e.tick()
        self.clock.advance(11)
        e.tick()
        e.tick()
        self.assertEqual(t.phase, Phase.DONE)
        self.assertEqual(t.filled_qty, 0)

    def test_no_entry_outside_session(self):
        self.clock.now = datetime(2026, 11, 4, 11, 45)
        e = self.engine()
        e.tick()
        self.assertEqual(e.active, {})
        self.clock.now = datetime(2026, 11, 7, 10, 0)  # 土曜日
        e.tick()
        self.assertEqual(e.active, {})


class CapModeTest(unittest.TestCase):
    """上値指値（cap）モードと、伸びる余地による見送り"""

    def setUp(self):
        self.api = FakeApi()
        self.broker = PaperBroker()
        self.clock = Clock(datetime(2026, 11, 4, 9, 5))
        self.api.ranking = [{"Symbol": "1234", "SymbolName": "テスト"}]
        self.api.boards["1234"] = board(900, 1000, prev_close=900)

    def engine(self, **kwargs):
        kwargs.setdefault("entry_mode", "cap")
        return SpecialQuoteEngine(self.api, self.broker, make_config(**kwargs), self.clock)

    def test_places_cap_limit_and_fills_at_open_price(self):
        e = self.engine(max_chase_pct=5)
        e.tick()
        t = e.active["1234"]
        self.assertEqual(t.initial_price, 1000)
        self.assertEqual(t.buy_price, 1050)       # 1000 × 1.05
        self.assertEqual(t.qty, 800)              # 90万円 / 指値 1050円 を100株単位で切り捨て

        # 気配が切り上がっても指値以下なら出し直さない
        self.api.boards["1234"] = board(900, 1030)
        e.tick()
        self.assertIsNone(t.pending_cancel)
        self.assertEqual(t.reorder_count, 0)

        # 1030 で寄り付き → 指値ではなく寄り値で約定
        self.api.boards["1234"] = board(1030, 1031, special=False)
        e.tick()
        self.assertEqual(t.phase, Phase.HOLDING)
        self.assertEqual(t.avg_price, 1030)
        self.assertEqual(t.tp_price, 1032)

    def test_cap_is_limited_to_upper_limit(self):
        self.api.upper_limit = 1100
        e = self.engine(max_chase_pct=20, min_room_pct=5)
        e.tick()
        self.assertEqual(e.active["1234"].buy_price, 1100)

    def test_aborts_when_quote_exceeds_cap(self):
        e = self.engine(max_chase_pct=5)
        e.tick()
        t = e.active["1234"]
        self.api.boards["1234"] = board(900, 1060)
        e.tick()
        self.assertEqual(t.pending_cancel, "abort")
        e.tick()
        self.assertEqual(t.phase, Phase.DONE)
        self.assertEqual(t.filled_qty, 0)

    def test_skips_when_little_room_to_limit_up(self):
        self.api.upper_limit = 1040     # 余地 4%
        e = self.engine(min_room_pct=5)
        e.tick()
        self.assertEqual(e.active, {})
        self.assertIn("1234", e.skipped_today)

        # 同日は再判定しない（get_symbol を呼ばない）
        self.api.upper_limit = 1500
        e.tick()
        self.assertEqual(e.active, {})

        # 翌営業日は再判定する
        self.clock.now = datetime(2026, 11, 5, 9, 5)
        e.tick()
        self.assertIn("1234", e.active)

    def test_skips_when_upper_limit_unknown(self):
        self.api.upper_limit = None
        e = self.engine(min_room_pct=5)
        e.tick()
        self.assertEqual(e.active, {})

    def test_room_check_can_be_disabled(self):
        self.api.upper_limit = None
        e = self.engine(min_room_pct=0)
        e.tick()
        self.assertEqual(e.active["1234"].buy_price, 1050)

    def test_skips_large_gap(self):
        e = self.engine(max_gap_pct=10)   # 900 → 1000 は +11.1%
        e.tick()
        self.assertEqual(e.active, {})
        e2 = SpecialQuoteEngine(self.api, PaperBroker(),
                                make_config(entry_mode="cap", max_gap_pct=12), self.clock)
        e2.tick()
        self.assertIn("1234", e2.active)


class PartialFillTest(unittest.TestCase):
    """一部約定時は残りを取消し、約定分だけで利確に移る"""

    class Broker:
        def __init__(self):
            self.statuses = {}
            self.cancelled = []
            self.sells = []

        def observe(self, symbol, board):
            pass

        def buy_limit(self, symbol, qty, price):
            self.statuses["B"] = OrderStatus("B", False, 0, 0)
            return "B"

        def sell_limit(self, symbol, qty, price):
            self.sells.append((qty, price))
            self.statuses["S"] = OrderStatus("S", False, 0, 0)
            return "S"

        def sell_market(self, symbol, qty):
            return "M"

        def cancel(self, order_id):
            self.cancelled.append(order_id)
            return True

        def get_order(self, order_id):
            return self.statuses[order_id]

    def test_partial_fill(self):
        broker = self.Broker()
        t = SpecialQuoteTrade("1234", "x", None, 500, broker, make_config())
        t.start(1000)
        now = datetime(2026, 11, 4, 9, 5)
        broker.statuses["B"] = OrderStatus("B", False, 200, 1000)
        t.step(now, board(1000, 1000, special=False))
        self.assertEqual(broker.cancelled, ["B"])
        broker.statuses["B"] = OrderStatus("B", True, 200, 1000)
        t.step(now, board(1000, 1000, special=False))
        self.assertEqual(t.phase, Phase.HOLDING)
        self.assertEqual(broker.sells, [(200, 1002)])


if __name__ == "__main__":
    unittest.main()
