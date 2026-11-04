"""
バックテストの動作確認用ダミーデータを作る（乱数なので成績に意味はない）

  python -m backtest.dummy --out data/dummy --start 2025-11-01 --end 2026-10-31

日足は全銘柄・全営業日分を作り、分足は各ロジックが使う日だけ作る:
  - ストップ高の翌日（③）
  - 寄り付きが遅れた・寄らなかったギャップアップの日（②）
  - 上方修正などの開示があった日（①）
"""
from __future__ import annotations

import argparse
import random
from datetime import date, datetime, time as dt_time, timedelta
from typing import List, Optional

from . import data as d  # noqa: F401  (sys.path 設定のため先に読み込む)
from .data import DailyBar, MinuteBar, Revision
from price_limit import lower_limit, upper_limit
from special_quote import is_market_day
from tick_size import tick_size


def session_minutes(day: date) -> List[datetime]:
    """1分足の時刻: 9:00-11:29, 12:30-15:24, 15:30（引けの板寄せ）"""
    times = []
    for start, end in ((dt_time(9, 0), dt_time(11, 30)), (dt_time(12, 30), dt_time(15, 25))):
        t = datetime.combine(day, start)
        while t.time() < end:
            times.append(t)
            t += timedelta(minutes=1)
    times.append(datetime.combine(day, dt_time(15, 30)))
    return times


def round_tick(price: float, group: Optional[str]) -> float:
    tick = tick_size(max(price, 1), group)
    value = round(round(price / tick) * tick, 1)
    return value if value > 0 else tick


class DummyGenerator:
    def __init__(self, seed: int = 1, n_symbols: int = 200):
        self.rng = random.Random(seed)
        self.symbols = []
        codes = self.rng.sample(range(1300, 9999), n_symbols)
        for code in sorted(codes):
            group = "10003" if self.rng.random() < 0.1 else "10000"
            self.symbols.append({"Code": str(code), "Name": f"ダミー{code}", "PriceRangeGroup": group})
        self.price = {s["Code"]: round_tick(self.rng.lognormvariate(7.0, 0.8), s["PriceRangeGroup"])
                      for s in self.symbols}

    # --- 1日分 ------------------------------------------------------------------

    def _clip(self, price, prev_close, group):
        return round_tick(min(max(price, lower_limit(prev_close)), upper_limit(prev_close)), group)

    def _normal_day(self, prev_close, group, sigma=0.02):
        r = self.rng
        o = self._clip(prev_close * (1 + r.gauss(0, sigma / 2)), prev_close, group)
        c = self._clip(o * (1 + r.gauss(0, sigma)), prev_close, group)
        h = self._clip(max(o, c) * (1 + abs(r.gauss(0, sigma / 2))), prev_close, group)
        l = self._clip(min(o, c) * (1 - abs(r.gauss(0, sigma / 2))), prev_close, group)
        return o, h, l, c

    def generate_day(self, day: date, after_stop_high: set):
        """1日分の日足と、分足を作るべき (code, 種類, OHLC, 寄りの遅れ分) を返す"""
        r = self.rng
        daily, minute_jobs, revisions, stop_high = [], [], [], set()
        for s in self.symbols:
            code, group = s["Code"], s["PriceRangeGroup"]
            prev = self.price[code]
            up = upper_limit(prev)
            kind, delay = "normal", 0

            if code in after_stop_high:
                kind = "after_stop_high"
                if r.random() < 0.15:  # 連続ストップ高（寄らずに引けで比例配分）
                    o = h = l = c = round_tick(up, group)
                    delay = None
                    stop_high.add(code)
                else:
                    o = self._clip(prev * (1 + r.gauss(0.02, 0.05)), prev, group)
                    c = self._clip(o * (1 + r.gauss(-0.01, 0.05)), prev, group)
                    h = self._clip(max(o, c) * (1 + abs(r.gauss(0, 0.03))), prev, group)
                    l = self._clip(min(o, c) * (1 - r.uniform(0, 0.10)), prev, group)
            elif r.random() < 0.004:  # ストップ高で引ける日
                kind = "stop_high"
                o = self._clip(prev * (1 + r.uniform(0.0, 0.15)), prev, group)
                h = c = round_tick(up, group)
                l = self._clip(min(o, prev) * (1 - r.uniform(0, 0.03)), prev, group)
                stop_high.add(code)
            elif r.random() < 0.006:  # 寄り前に買いが殺到してギャップアップ
                kind = "gap_up"
                o = self._clip(prev * (1 + r.uniform(0.03, 0.15)), prev, group)
                c = self._clip(o * (1 + r.gauss(0, 0.04)), prev, group)
                h = self._clip(max(o, c) * (1 + abs(r.gauss(0, 0.02))), prev, group)
                l = self._clip(min(o, c) * (1 - abs(r.gauss(0, 0.03))), prev, group)
                delay = r.choice([0, 0, r.randint(1, 45)])  # 3回に1回は寄りが遅れない
            else:
                o, h, l, c = self._normal_day(prev, group)
                if r.random() < 0.0015:  # 開示あり
                    kind = "revision"
                    t = datetime.combine(day, dt_time(8, 0)) + timedelta(minutes=r.randint(0, 9 * 60))
                    revisions.append(Revision(code, t, r.random() < 0.7, "業績予想の修正に関するお知らせ"))

            daily.append(DailyBar(code, o, h, l, c, r.randint(10_000, 2_000_000)))
            if kind != "normal":
                minute_jobs.append((code, group, kind, (o, h, l, c), delay))
            self.price[code] = c
        return daily, minute_jobs, revisions, stop_high

    # --- 分足 -------------------------------------------------------------------

    def minute_bars(self, day: date, group, kind, ohlc, delay) -> List[MinuteBar]:
        o, h, l, c = ohlc
        times = session_minutes(day)
        if delay is None:  # 寄らなかった
            return [MinuteBar(times[-1], c, c, c, c, self.rng.randint(1000, 50000))]
        times = times[delay:]
        n = len(times)
        r = self.rng
        if n < 4:
            return [MinuteBar(times[-1], o, h, l, c, 1000)]

        # 高値・安値をつける時刻（ストップ高の翌日は寄り後早めに安値をつけやすくする）
        if kind == "after_stop_high" and r.random() < 0.6:
            i_low = r.randint(1, min(40, n - 3))
            i_high = r.randint(i_low + 1, n - 2)
        else:
            i_low, i_high = r.sample(range(1, n - 1), 2)
        anchors = sorted([(0, o), (i_low, l), (i_high, h), (n - 1, c)])

        path = []
        noise = (h - l) * 0.08
        for (i0, p0), (i1, p1) in zip(anchors, anchors[1:]):
            for i in range(i0, i1):
                base = p0 + (p1 - p0) * (i - i0) / (i1 - i0)
                p = base if i == i0 else base + r.gauss(0, noise)
                path.append(round_tick(min(max(p, l), h), group))
        path.append(c)

        bars, prev = [], o
        for t, p in zip(times, path):
            bars.append(MinuteBar(t, prev, max(prev, p), min(prev, p), p, r.randint(100, 20000)))
            prev = p
        return bars


def generate(out: str, start: date, end: date, seed: int = 1, n_symbols: int = 200):
    gen = DummyGenerator(seed, n_symbols)
    d.write_symbols(out, gen.symbols)
    all_revisions, after_stop_high = [], set()
    day, n_days, n_minute = start, 0, 0
    while day <= end:
        if is_market_day(day):
            daily, jobs, revisions, stop_high = gen.generate_day(day, after_stop_high)
            d.write_daily(out, day, daily)
            for code, group, kind, ohlc, delay in jobs:
                if kind == "stop_high":
                    continue  # 収集は日足で判定するので分足は不要
                d.write_minute(out, day, code, gen.minute_bars(day, group, kind, ohlc, delay))
                n_minute += 1
            all_revisions += revisions
            after_stop_high = stop_high
            n_days += 1
        day += timedelta(days=1)
    d.write_revisions(out, all_revisions)
    print(f"ダミーデータを作成しました: {out}（{n_days}営業日・{n_symbols}銘柄・分足 {n_minute}件・開示 {len(all_revisions)}件）")


def main():
    parser = argparse.ArgumentParser(description="バックテスト用ダミーデータの作成")
    parser.add_argument("--out", default="data/dummy")
    parser.add_argument("--start", default="2025-11-01")
    parser.add_argument("--end", default="2026-10-31")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--symbols", type=int, default=200)
    a = parser.parse_args()
    generate(a.out, date.fromisoformat(a.start), date.fromisoformat(a.end), a.seed, a.symbols)


if __name__ == "__main__":
    main()
