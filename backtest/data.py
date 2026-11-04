"""
バックテスト用データの読み書き

データフォルダの構成（本番データもダミーデータも同じ形式）:

  symbols.csv                    Code,Name,PriceRangeGroup
  daily/YYYY-MM-DD.csv           Code,Open,High,Low,Close,Volume   （その日の全銘柄の日足）
  minute/YYYY-MM-DD/CODE.csv     Time,Open,High,Low,Close,Volume   （Time は HH:MM。1分足）
  revisions.csv                  Code,DisclosedDate,DisclosedTime,Upward,Title
                                 （業績予想修正などの開示。Upward=1 が上方修正）

分足が無い銘柄・日は「データなし」として扱う。
J-Quants から取得したデータもこの形式に変換して置けば、そのままバックテストできる。
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from typing import Dict, List, Optional


@dataclass(frozen=True)
class DailyBar:
    code: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class MinuteBar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Revision:
    code: str
    disclosed_at: datetime
    upward: bool
    title: str


def _num(value: str) -> Optional[float]:
    return float(value) if value not in ("", None) else None


class CsvDataSource:
    def __init__(self, root: str):
        self.root = root
        self._daily_cache: Dict[date, Dict[str, DailyBar]] = {}
        self._symbols: Optional[Dict[str, dict]] = None

    # --- 銘柄 ---------------------------------------------------------------

    def symbols(self) -> Dict[str, dict]:
        if self._symbols is None:
            path = os.path.join(self.root, "symbols.csv")
            self._symbols = {}
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        self._symbols[row["Code"]] = row
        return self._symbols

    def name(self, code: str) -> str:
        return self.symbols().get(code, {}).get("Name", "")

    def price_range_group(self, code: str) -> Optional[str]:
        return self.symbols().get(code, {}).get("PriceRangeGroup") or None

    # --- 日足 ---------------------------------------------------------------

    def trading_days(self, start: Optional[date] = None, end: Optional[date] = None) -> List[date]:
        folder = os.path.join(self.root, "daily")
        days = sorted(date.fromisoformat(n[:-4]) for n in os.listdir(folder) if n.endswith(".csv"))
        return [d for d in days if (start is None or d >= start) and (end is None or d <= end)]

    def daily(self, day: date) -> Dict[str, DailyBar]:
        if day not in self._daily_cache:
            bars = {}
            path = os.path.join(self.root, "daily", f"{day.isoformat()}.csv")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        if _num(row["Open"]) is None:
                            continue  # 売買なし
                        bars[row["Code"]] = DailyBar(
                            row["Code"], float(row["Open"]), float(row["High"]),
                            float(row["Low"]), float(row["Close"]), _num(row.get("Volume")) or 0,
                        )
            if len(self._daily_cache) > 5:
                self._daily_cache.pop(next(iter(self._daily_cache)))
            self._daily_cache[day] = bars
        return self._daily_cache[day]

    # --- 分足 ---------------------------------------------------------------

    def minute(self, day: date, code: str) -> List[MinuteBar]:
        path = os.path.join(self.root, "minute", day.isoformat(), f"{code}.csv")
        if not os.path.exists(path):
            return []
        bars = []
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                hh, mm = row["Time"].split(":")
                bars.append(MinuteBar(
                    datetime.combine(day, dt_time(int(hh), int(mm))),
                    float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"]),
                    _num(row.get("Volume")) or 0,
                ))
        bars.sort(key=lambda b: b.time)
        return bars

    # --- 開示 ---------------------------------------------------------------

    def revisions(self) -> List[Revision]:
        path = os.path.join(self.root, "revisions.csv")
        if not os.path.exists(path):
            return []
        result = []
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                hh, mm = row["DisclosedTime"].split(":")[:2]
                result.append(Revision(
                    row["Code"],
                    datetime.combine(date.fromisoformat(row["DisclosedDate"]), dt_time(int(hh), int(mm))),
                    row["Upward"] in ("1", "true", "True"),
                    row.get("Title", ""),
                ))
        result.sort(key=lambda r: r.disclosed_at)
        return result


# --- 書き込み（ダミーデータ生成・データ変換用） -----------------------------

def write_symbols(root: str, rows: List[dict]):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "symbols.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, ["Code", "Name", "PriceRangeGroup"])
        w.writeheader()
        w.writerows(rows)


def write_daily(root: str, day: date, bars: List[DailyBar]):
    folder = os.path.join(root, "daily")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, f"{day.isoformat()}.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Code", "Open", "High", "Low", "Close", "Volume"])
        for b in bars:
            w.writerow([b.code, b.open, b.high, b.low, b.close, b.volume])


def write_minute(root: str, day: date, code: str, bars: List[MinuteBar]):
    folder = os.path.join(root, "minute", day.isoformat())
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, f"{code}.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Time", "Open", "High", "Low", "Close", "Volume"])
        for b in bars:
            w.writerow([b.time.strftime("%H:%M"), b.open, b.high, b.low, b.close, b.volume])


def write_revisions(root: str, revisions: List[Revision]):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "revisions.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Code", "DisclosedDate", "DisclosedTime", "Upward", "Title"])
        for r in revisions:
            w.writerow([r.code, r.disclosed_at.date().isoformat(), r.disclosed_at.strftime("%H:%M"),
                        1 if r.upward else 0, r.title])
