"""
（予備）注目銘柄のオープニングレンジブレイク（ORB）

なぜ: 材料が出て売買が急増した銘柄（Stocks in Play）は、寄り後に方向が決まると
     その方向へ動き続けやすい（米国の研究で、対象をこうした銘柄に絞ると成績が改善したと報告）。

流れ:
  1. 9:00 + ORB_RANGE_MINUTES 分（既定 9:05）に、売買代金急増ランキング（ORB_RANKING_TYPE=7）の
     上位 ORB_RANKING_TOP_N 銘柄の板を見て、寄ってからの高値・安値（＝レンジ）を記録する
     - ORB_REQUIRE_BULL=true なら、レンジが陽線（現在値 > 始値）の銘柄だけ
  2. ORB_ENTRY_END（既定 10:30）までに、現在値がレンジの高値を上抜けたら最良売気配で指値買い
     - 損切りはレンジの安値の1ティック下。損切りまでの幅が ORB_MAX_RISK_PCT% を超える銘柄は見送る
  3. 利確は「約定単価 + 損切り幅 × ORB_TAKE_PROFIT_R」の指値（ストップ高を上限）。
     届かなければ ORB_FORCE_EXIT（既定 15:20）に成行で売る
  保有中・決済の処理は特別買い気配スキャルピング（special_quote.py）と共通。

安全のため既定はペーパートレード。実発注は ORB_DRY_RUN=false。

起動:
  cd kabucom
  python orb.py
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as dt_time
from typing import Callable, Dict, Optional, Set

from dotenv import load_dotenv

from special_quote import KabuApi, KabuBroker, PaperBroker, Phase, _env_bool, _env_time, in_session, is_market_day
from stop_high_rebound import ReboundTrade
from swing import is_special
from tick_size import add_ticks, round_up_to_tick
from utils import calculate_purchase_quantity

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)


@dataclass
class ORBConfig:
    api_password: str
    account_type: int
    budget_per_trade: float
    dry_run: bool = True
    ranking_exchange: str = "T"
    ranking_type: int = 7              # 7=売買代金急増 / 6=売買高急増
    ranking_top_n: int = 20
    range_minutes: int = 5
    require_bull: bool = True
    max_risk_pct: float = 4.0          # 損切りまでの幅（%）の上限
    take_profit_r: float = 5.0         # 利確 = 損切り幅の何倍か
    min_price: float = 0
    max_price: float = 0
    poll_interval_sec: float = 1.0
    fill_wait_sec: float = 5.0         # 買い指値が約定しなければ何秒で取消すか
    max_entry_attempts: int = 2
    max_positions: int = 2
    entry_end: dt_time = dt_time(10, 30)
    force_exit: dt_time = dt_time(15, 20)

    @classmethod
    def from_env(cls) -> "ORBConfig":
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            load_dotenv(dotenv_path=env_path)
        api_password = os.getenv("KABUCOM_API_PASSWORD")
        if not api_password:
            raise SystemExit("Error: KABUCOM_API_PASSWORD environment variable is not set")
        account_type = os.getenv("KABUCOM_ACCOUNT_TYPE")
        if not account_type:
            raise SystemExit("Error: KABUCOM_ACCOUNT_TYPE environment variable is not set")
        budget = os.getenv("ORB_BUDGET_PER_TRADE") or os.getenv("AVAILABLE_MARGIN")
        if not budget or float(budget) <= 0:
            raise SystemExit("Error: ORB_BUDGET_PER_TRADE (or AVAILABLE_MARGIN) is not set or invalid")
        g = os.getenv
        return cls(
            api_password=api_password, account_type=int(account_type), budget_per_trade=float(budget),
            dry_run=_env_bool("ORB_DRY_RUN", True),
            ranking_exchange=g("ORB_RANKING_EXCHANGE", "T"),
            ranking_type=int(g("ORB_RANKING_TYPE", "7")),
            ranking_top_n=int(g("ORB_RANKING_TOP_N", "20")),
            range_minutes=int(g("ORB_RANGE_MINUTES", "5")),
            require_bull=_env_bool("ORB_REQUIRE_BULL", True),
            max_risk_pct=float(g("ORB_MAX_RISK_PCT", "4")),
            take_profit_r=float(g("ORB_TAKE_PROFIT_R", "5")),
            min_price=float(g("ORB_MIN_PRICE", "0")),
            max_price=float(g("ORB_MAX_PRICE", "0")),
            poll_interval_sec=float(g("ORB_POLL_INTERVAL_SEC", "1")),
            fill_wait_sec=float(g("ORB_FILL_WAIT_SEC", "5")),
            max_entry_attempts=int(g("ORB_MAX_ENTRY_ATTEMPTS", "2")),
            max_positions=int(g("ORB_MAX_POSITIONS", "2")),
            entry_end=_env_time("ORB_ENTRY_END", "10:30"),
            force_exit=_env_time("ORB_FORCE_EXIT", "15:20"),
        )


@dataclass
class RangeWatch:
    symbol: str
    name: str
    high: float
    low: float
    group: Optional[str]
    upper_limit: Optional[float]
    attempts: int = 0
    gave_up: bool = False


@dataclass
class ORBTrade(ReboundTrade):
    """押し目買い（ReboundTrade）と同じ買い方・決済の流れで、利確・損切りだけレンジ基準にする"""
    stop_price: float = 0.0
    take_profit_r: float = 5.0

    def start(self, price: float, now: datetime) -> bool:  # type: ignore[override]
        self.initial_price, self.ordered_at = price, now
        order_id = self.broker.buy_limit(self.symbol, self.qty, price)
        if not order_id:
            self.phase = Phase.DONE
            return False
        self.buy_order_id, self.buy_price = order_id, price
        self._info(f"レンジ上抜けで {price} に指値買い {self.qty}株（損切り {self.stop_price}）")
        return True

    def _exit_targets(self):
        sl = self.stop_price
        risk = max(self.avg_price - sl, 0)
        tp = round_up_to_tick(self.avg_price + risk * self.take_profit_r, self.price_range_group)
        # ストップ高（upper_limit）での上限は SpecialQuoteTrade._on_bought で共通に当てる
        return max(tp, add_ticks(self.avg_price, 1, self.price_range_group)), sl


class ORBEngine:
    def __init__(self, api: KabuApi, broker, config: ORBConfig,
                 clock: Callable[[], datetime] = datetime.now):
        self.api = api
        self.broker = broker
        self.config = config
        self.clock = clock
        self.ranges: Dict[str, RangeWatch] = {}
        self.active: Dict[str, ORBTrade] = {}
        self.traded_today: Set[str] = set()
        self._day: Optional[date] = None
        self._scanned = False

    def range_end(self, now: datetime) -> datetime:
        return datetime.combine(now.date(), dt_time(9, 0)) + timedelta(minutes=self.config.range_minutes)

    def tick(self):
        now = self.clock()
        if self._day != now.date():
            self._day, self._scanned, self.ranges, self.traded_today = now.date(), False, {}, set()

        for symbol, trade in list(self.active.items()):
            board = self.api.get_board(symbol)
            if board:
                trade.step(now, board)
            if trade.phase == Phase.DONE:
                del self.active[symbol]
                if trade.filled_qty > 0:
                    self.traded_today.add(symbol)
                elif symbol in self.ranges:
                    self.ranges[symbol].attempts += 1

        if not (is_market_day(now) and in_session(now) and now.time() < self.config.entry_end):
            return
        if not self._scanned and now >= self.range_end(now):
            self._scanned = True
            self.scan()
        if self.ranges:
            self._watch(now)

    def scan(self):
        c = self.config
        for item in self.api.get_ranking(c.ranking_exchange, c.ranking_type)[: c.ranking_top_n]:
            symbol = str(item.get("Symbol"))
            board = self.api.get_board(symbol) or {}
            opening, current = board.get("OpeningPrice"), board.get("CurrentPrice")
            high, low = board.get("HighPrice"), board.get("LowPrice")
            if not (opening and current and high and low) or is_special(board) or high <= low:
                continue
            if c.require_bull and current <= opening:
                continue
            if current < c.min_price or (c.max_price and current > c.max_price):
                continue
            info = self.api.get_symbol(symbol) or {}
            self.ranges[symbol] = RangeWatch(symbol, item.get("SymbolName", ""), high, low,
                                             info.get("PriceRangeGroup"), info.get("UpperLimit"))
        names = ", ".join(f"{r.symbol}({r.low}-{r.high})" for r in self.ranges.values()) or "なし"
        logger.info(f"オープニングレンジを記録しました: {names}")

    def _watch(self, now: datetime):
        c = self.config
        for symbol, r in self.ranges.items():
            if (r.gave_up or symbol in self.active or symbol in self.traded_today
                    or r.attempts >= c.max_entry_attempts or len(self.active) >= c.max_positions):
                continue
            board = self.api.get_board(symbol) or {}
            current = board.get("CurrentPrice")
            if not current or is_special(board) or current <= r.high:
                continue
            # kabu API は名称が逆: BidPrice が最良売気配
            price = board.get("BidPrice") or current
            stop = add_ticks(r.low, -1, r.group)
            risk_pct = (price - stop) / price * 100
            if risk_pct > c.max_risk_pct:
                logger.info(f"[{symbol} {r.name}] 損切りまで {risk_pct:.1f}% と広すぎるため見送ります")
                r.gave_up = True
                continue
            qty = calculate_purchase_quantity(price, c.budget_per_trade)
            if qty <= 0:
                r.gave_up = True
                continue
            trade = ORBTrade(symbol=symbol, name=r.name, price_range_group=r.group, qty=qty,
                             broker=self.broker, config=c, stop_price=stop,
                             take_profit_r=c.take_profit_r, upper_limit=r.upper_limit)
            logger.info(f"[{symbol} {r.name}] レンジ高値 {r.high} を上抜け（現在値 {current}）")
            if trade.start(price, now):
                self.active[symbol] = trade
            else:
                r.attempts += 1

    def run_forever(self):
        mode = "ペーパートレード（発注しません）" if self.config.dry_run else "実発注"
        logger.info(f"オープニングレンジブレイクを開始します（{mode}）")
        while True:
            try:
                now = self.clock()
                active_window = is_market_day(now) and in_session(now) and now.time() < self.config.entry_end
                if not self.active and not active_window:
                    time.sleep(30)
                    continue
                self.tick()
                time.sleep(self.config.poll_interval_sec)
            except KeyboardInterrupt:
                if self.active:
                    logger.warning(f"未決済/注文中の銘柄があります: {list(self.active)}。手動で確認してください")
                logger.info("停止しました")
                return
            except Exception as e:
                logger.exception(f"エラーが発生しました: {e}")
                time.sleep(5)


def main():
    from kabu_token import get_token

    config = ORBConfig.from_env()
    api = KabuApi(get_token(config.api_password))
    broker = PaperBroker() if config.dry_run else KabuBroker(api, config.account_type)
    ORBEngine(api, broker, config).run_forever()


if __name__ == "__main__":
    main()
