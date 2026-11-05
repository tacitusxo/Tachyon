"""
バックテストの共通部品: 分足からの価格経路、決済シミュレーション、同時保有制限、成績集計
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from typing import Iterable, Iterator, List, Optional, Tuple

from tick_size import add_ticks
from utils import calculate_purchase_quantity

from .data import MinuteBar

PricePoint = Tuple[datetime, float]


def price_path(bars: Iterable[MinuteBar]) -> Iterator[PricePoint]:
    """
    分足を価格の並びに展開する。1本の中の値動きは
      陽線: 始値 → 安値 → 高値 → 終値
      陰線: 始値 → 高値 → 安値 → 終値
    とみなす（買いポジションにとって不利な順＝損切りが先に当たる側に寄せた慣習的な仮定）。
    """
    for b in bars:
        if b.close >= b.open:
            points = (b.open, b.low, b.high, b.close)
        else:
            points = (b.open, b.high, b.low, b.close)
        last = None
        for p in points:
            if p != last:
                yield b.time, p
                last = p


@dataclass
class Exit:
    time: datetime
    price: float
    reason: str


def simulate_exit(path: Iterable[PricePoint], tp_price: float, sl_price: float,
                  force_exit: dt_time, slippage_ticks: int, group: Optional[str]) -> Optional[Exit]:
    """
    エントリー後の価格経路に対して、ライブと同じ決済ルールを当てる。
      - 利確: 指値。価格が利確価格以上になったら利確価格で約定
      - 損切り: 価格が損切り価格以下になったら成行（その価格から slippage_ticks 不利に約定）
      - 強制決済: 指定時刻以降の最初の価格で成行
    経路が尽きたら最後の価格で決済（引け）。
    """
    last = None
    for t, p in path:
        last = (t, p)
        if t.time() >= force_exit:
            return Exit(t, add_ticks(p, -slippage_ticks, group), "強制決済")
        if p <= sl_price:
            return Exit(t, add_ticks(p, -slippage_ticks, group), "損切り")
        if p >= tp_price:
            return Exit(t, tp_price, "利確")
    if last is None:
        return None
    return Exit(last[0], last[1], "引け")


@dataclass
class Trade:
    strategy: str
    code: str
    name: str
    entry_time: datetime
    entry_price: float
    qty: int
    exit_time: datetime
    exit_price: float
    exit_reason: str
    note: str = ""
    fee: float = 0.0

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.qty - self.fee

    @property
    def return_pct(self) -> float:
        return (self.exit_price / self.entry_price - 1) * 100


def make_trade(strategy: str, code: str, name: str, entry: PricePoint, budget: float,
               exit_: Exit, fee_rate: float, note: str = "",
               sizing_price: Optional[float] = None) -> Optional[Trade]:
    """sizing_price: 株数の計算に使う価格（成行買いは値幅上限で余力を拘束されるため。省略時は約定価格）"""
    qty = calculate_purchase_quantity(sizing_price or entry[1], budget)
    if qty <= 0:
        return None
    fee = (entry[1] + exit_.price) * qty * fee_rate
    return Trade(strategy, code, name, entry[0], entry[1], qty, exit_.time, exit_.price,
                 exit_.reason, note, fee)


def apply_position_limit(trades: List[Trade], max_positions: int) -> List[Trade]:
    """
    同時保有数の上限を当てる。エントリー時刻の早い順に採用し、その時点で
    保有中の数が上限に達していれば見送る。同日同銘柄は1回まで（現物の差金決済対策）。
    """
    accepted: List[Trade] = []
    seen = set()
    for t in sorted(trades, key=lambda x: x.entry_time):
        key = (t.entry_time.date(), t.code)
        if key in seen:
            continue
        holding = [a for a in accepted if a.entry_time <= t.entry_time < a.exit_time]
        if len(holding) >= max_positions:
            continue
        accepted.append(t)
        seen.add(key)
    return accepted


@dataclass
class Summary:
    strategy: str
    trades: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    max_drawdown: float = 0.0
    avg_return_pct: float = 0.0
    days: int = 0
    reasons: dict = field(default_factory=dict)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades else 0.0

    @property
    def profit_factor(self) -> Optional[float]:
        return self.gross_profit / -self.gross_loss if self.gross_loss else None


def summarize(strategy: str, trades: List[Trade], days: List[date]) -> Summary:
    s = Summary(strategy, days=len(days))
    equity = peak = 0.0
    for t in sorted(trades, key=lambda x: x.exit_time):
        s.trades += 1
        s.total_pnl += t.pnl
        if t.pnl > 0:
            s.wins += 1
            s.gross_profit += t.pnl
        else:
            s.gross_loss += t.pnl
        s.reasons[t.exit_reason] = s.reasons.get(t.exit_reason, 0) + 1
        equity += t.pnl
        peak = max(peak, equity)
        s.max_drawdown = max(s.max_drawdown, peak - equity)
    if trades:
        s.avg_return_pct = sum(t.return_pct for t in trades) / len(trades)
    return s
