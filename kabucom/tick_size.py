"""
東証の呼値（ティックサイズ）計算

kabu API の銘柄情報（/symbol）の PriceRangeGroup で呼値テーブルが決まる。
  - "10000": 一般銘柄（TOPIX500 以外）
  - "10003": TOPIX500 構成銘柄
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from typing import List, Optional, Tuple

# (価格の上限（以下）, 呼値) のリスト。最後の要素は上限なし。
_STANDARD: List[Tuple[Optional[Decimal], Decimal]] = [
    (Decimal("3000"), Decimal("1")),
    (Decimal("5000"), Decimal("5")),
    (Decimal("30000"), Decimal("10")),
    (Decimal("50000"), Decimal("50")),
    (Decimal("300000"), Decimal("100")),
    (Decimal("500000"), Decimal("500")),
    (Decimal("3000000"), Decimal("1000")),
    (Decimal("5000000"), Decimal("5000")),
    (Decimal("30000000"), Decimal("10000")),
    (Decimal("50000000"), Decimal("50000")),
    (None, Decimal("100000")),
]

_TOPIX500: List[Tuple[Optional[Decimal], Decimal]] = [
    (Decimal("1000"), Decimal("0.1")),
    (Decimal("3000"), Decimal("0.5")),
    (Decimal("10000"), Decimal("1")),
    (Decimal("30000"), Decimal("5")),
    (Decimal("100000"), Decimal("10")),
    (Decimal("300000"), Decimal("50")),
    (Decimal("1000000"), Decimal("100")),
    (Decimal("3000000"), Decimal("500")),
    (Decimal("10000000"), Decimal("1000")),
    (Decimal("30000000"), Decimal("5000")),
    (None, Decimal("10000")),
]

TOPIX500_GROUP = "10003"


def _table(price_range_group: Optional[str]):
    return _TOPIX500 if str(price_range_group) == TOPIX500_GROUP else _STANDARD


def _tick_down(price: Decimal, table) -> Decimal:
    """price から1ティック下げるときの呼値（price が属する帯 = 「以下」の帯）"""
    for upper, tick in table:
        if upper is None or price <= upper:
            return tick
    raise AssertionError("unreachable")


def _tick_up(price: Decimal, table) -> Decimal:
    """price から1ティック上げるときの呼値（price より上の帯）"""
    for upper, tick in table:
        if upper is None or price < upper:
            return tick
    raise AssertionError("unreachable")


def tick_size(price: float, price_range_group: Optional[str] = None) -> float:
    """price における呼値を返す"""
    return float(_tick_down(Decimal(str(price)), _table(price_range_group)))


def add_ticks(price: float, ticks: int, price_range_group: Optional[str] = None) -> float:
    """
    price から ticks ティック分移動した価格を返す（負数なら下方向）。
    呼値の帯をまたぐ場合も帯ごとの呼値で1ティックずつ移動する。
    """
    table = _table(price_range_group)
    p = Decimal(str(price))
    for _ in range(abs(ticks)):
        if ticks > 0:
            p += _tick_up(p, table)
        else:
            p -= _tick_down(p, table)
    return float(p)


def round_up_to_tick(price: float, price_range_group: Optional[str] = None) -> float:
    """price 以上で最も近い呼値の価格を返す"""
    p = Decimal(str(price))
    tick = _tick_down(p, _table(price_range_group))
    return float((p / tick).to_integral_value(rounding=ROUND_CEILING) * tick)
