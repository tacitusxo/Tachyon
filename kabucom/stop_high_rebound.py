"""
前日ストップ高銘柄の寄り後押し目買い戦略

前日にストップ高で引けた銘柄は、翌日の寄り付き後にいったん売られて下がりやすい。
下がりきって反発し始めたところで買い、数%の値幅を取って売る。

流れ:
  1. 【前日 引け後】値上がり率ランキングの銘柄について、現在値（＝終値）が
     銘柄情報の値幅上限（UpperLimit）に達しているものを「ストップ高銘柄」として
     ファイル（stop_high_list.json）に保存する
  2. 【当日 寄り後】保存した銘柄の板を監視し、次の条件で指値買いする
       - 基準値（前日終値と寄り後の高値の大きい方）から SHR_MIN_DIP_PCT% 以上下落
       - その安値から SHR_REBOUND_TICKS ティック以上反発
     （下落が SHR_MAX_DIP_PCT% を超えた銘柄は見送る）
     寄らずに特別気配のままの銘柄（ストップ高張り付きなど）は対象外
  3. 約定したら「約定単価 × (1 + 利確%)」で指値売りを出す
  4. 現在値が「約定単価 × (1 - 損切り%)」以下になったら、利確注文を取消して成行で売る
  5. 強制決済時刻になったら保有分を成行で売る

安全のため、デフォルトはペーパートレード（実際には発注しない）。
実発注するには環境変数 SHR_DRY_RUN=false を設定する。

起動:
  cd kabucom
  python stop_high_rebound.py           # 監視・売買（引け後の銘柄収集も自動で行う）
  python stop_high_rebound.py collect   # ストップ高銘柄の収集だけを今すぐ行う
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as dt_time
from typing import Callable, Dict, List, Optional, Set

from dotenv import load_dotenv

from special_quote import (
    SIGN_SPECIAL_QUOTE, KabuApi, KabuBroker, PaperBroker, Phase,
    SpecialQuoteTrade, _env_bool, _env_time, in_session, is_market_day,
)
from tick_size import add_ticks, round_up_to_tick
from utils import calculate_purchase_quantity

logger = logging.getLogger(__name__)

DEFAULT_LIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stop_high_list.json")

# 東証の値幅制限は最小でも 14% 程度。これ未満の上昇率の銘柄はストップ高ではありえない
_MIN_LIMIT_UP_PCT = 10.0


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

@dataclass
class StopHighConfig:
    api_password: str
    account_type: int
    budget_per_trade: float            # 1トレードあたりの予算（円）
    dry_run: bool = True               # True: ペーパートレード
    ranking_exchange: str = "T"        # 収集に使うランキングの市場（ALL/T/TP/TS/TG）
    collect_at: dt_time = dt_time(15, 35)   # 引け後にストップ高銘柄を収集する時刻
    list_file: str = DEFAULT_LIST_FILE
    poll_interval_sec: float = 1.0
    min_price: float = 0
    max_price: float = 0               # 0 = 上限なし
    min_dip_pct: float = 3.0           # 基準値からこれ以上下げたら押し目とみなす
    max_dip_pct: float = 15.0          # これ以上下げた銘柄は見送る
    rebound_ticks: int = 3             # 安値から何ティック戻したら買うか
    take_profit_pct: float = 2.0       # 利確（約定単価からの%）
    stop_loss_pct: float = 2.0         # 損切り（約定単価からの%）
    fill_wait_sec: float = 5.0         # 買い指値が約定しなければ何秒で取消すか
    max_entry_attempts: int = 3        # 未約定で取消した後、同じ銘柄を狙い直す回数の上限
    max_positions: int = 1             # 同時に保有する銘柄数
    entry_start: dt_time = dt_time(9, 0)
    entry_end: dt_time = dt_time(10, 0)     # 新規エントリーの締切
    force_exit: dt_time = dt_time(15, 20)   # 強制決済時刻

    @classmethod
    def from_env(cls) -> "StopHighConfig":
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            load_dotenv(dotenv_path=env_path)

        api_password = os.getenv("KABUCOM_API_PASSWORD")
        if not api_password:
            raise SystemExit("Error: KABUCOM_API_PASSWORD environment variable is not set")
        account_type = os.getenv("KABUCOM_ACCOUNT_TYPE")
        if not account_type:
            raise SystemExit("Error: KABUCOM_ACCOUNT_TYPE environment variable is not set")
        budget = os.getenv("SHR_BUDGET_PER_TRADE") or os.getenv("AVAILABLE_MARGIN")
        if not budget or float(budget) <= 0:
            raise SystemExit("Error: SHR_BUDGET_PER_TRADE (or AVAILABLE_MARGIN) is not set or invalid")

        return cls(
            api_password=api_password,
            account_type=int(account_type),
            budget_per_trade=float(budget),
            dry_run=_env_bool("SHR_DRY_RUN", True),
            ranking_exchange=os.getenv("SHR_RANKING_EXCHANGE", "T"),
            collect_at=_env_time("SHR_COLLECT_AT", "15:35"),
            list_file=os.getenv("SHR_LIST_FILE", DEFAULT_LIST_FILE),
            poll_interval_sec=float(os.getenv("SHR_POLL_INTERVAL_SEC", "1")),
            min_price=float(os.getenv("SHR_MIN_PRICE", "0")),
            max_price=float(os.getenv("SHR_MAX_PRICE", "0")),
            min_dip_pct=float(os.getenv("SHR_MIN_DIP_PCT", "3")),
            max_dip_pct=float(os.getenv("SHR_MAX_DIP_PCT", "15")),
            rebound_ticks=int(os.getenv("SHR_REBOUND_TICKS", "3")),
            take_profit_pct=float(os.getenv("SHR_TAKE_PROFIT_PCT", "2")),
            stop_loss_pct=float(os.getenv("SHR_STOP_LOSS_PCT", "2")),
            fill_wait_sec=float(os.getenv("SHR_FILL_WAIT_SEC", "5")),
            max_entry_attempts=int(os.getenv("SHR_MAX_ENTRY_ATTEMPTS", "3")),
            max_positions=int(os.getenv("SHR_MAX_POSITIONS", "1")),
            entry_start=_env_time("SHR_ENTRY_START", "9:00"),
            entry_end=_env_time("SHR_ENTRY_END", "10:00"),
            force_exit=_env_time("SHR_FORCE_EXIT", "15:20"),
        )


# ---------------------------------------------------------------------------
# ストップ高銘柄の収集・保存
# ---------------------------------------------------------------------------

def previous_market_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_market_day(d):
        d -= timedelta(days=1)
    return d


def collect_stop_high(api: KabuApi, exchange: str) -> List[dict]:
    """値上がり率ランキングから、現在値が値幅上限に達している銘柄を返す（引け後に呼ぶ想定）"""
    result = []
    for item in api.get_ranking(exchange):
        change = item.get("ChangePercentage")
        if change is not None and change < _MIN_LIMIT_UP_PCT:
            break  # ランキングは上昇率順なので、以降はストップ高になりえない
        symbol = str(item.get("Symbol"))
        info = api.get_symbol(symbol) or {}
        board = api.get_board(symbol) or {}
        upper, close = info.get("UpperLimit"), board.get("CurrentPrice")
        if upper and close and close >= upper:
            result.append({
                "Symbol": symbol,
                "SymbolName": item.get("SymbolName") or info.get("SymbolName", ""),
                "Close": close,
                "PriceRangeGroup": info.get("PriceRangeGroup"),
            })
    return result


def save_stop_high(path: str, trade_date: date, items: List[dict]):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": trade_date.isoformat(), "symbols": items}, f, ensure_ascii=False, indent=2)
    names = ", ".join(f"{i['Symbol']} {i['SymbolName']}" for i in items) or "なし"
    logger.info(f"{trade_date} のストップ高銘柄 {len(items)}件を保存しました: {names}")


def load_stop_high(path: str, today: date) -> List[dict]:
    """前営業日に収集したストップ高銘柄を読み込む（日付が合わなければ空）"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.error(f"ストップ高銘柄ファイルを読めません: {path}: {e}")
        return []
    expected = previous_market_day(today).isoformat()
    if data.get("date") != expected:
        logger.warning(f"ストップ高銘柄ファイルの日付 {data.get('date')} が前営業日 {expected} と一致しません")
        return []
    return data.get("symbols") or []


# ---------------------------------------------------------------------------
# 押し目の検出
# ---------------------------------------------------------------------------

def _is_special(board: dict) -> bool:
    return SIGN_SPECIAL_QUOTE in (board.get("AskSign"), board.get("BidSign"))


@dataclass
class DipWatcher:
    """1銘柄の寄り後の値動きを追い、「下落 → 反発」を検出する"""
    symbol: str
    name: str
    prev_close: float
    price_range_group: Optional[str]
    config: StopHighConfig

    peak: Optional[float] = None     # 基準値（前日終値・寄り後高値の大きい方）
    trough: Optional[float] = None   # 基準値をつけた後の安値
    attempts: int = 0
    gave_up: bool = False

    def update(self, board: dict) -> bool:
        """板情報を1つ取り込み、買いシグナルなら True"""
        if self.gave_up:
            return False
        current = board.get("CurrentPrice")
        if current is None or board.get("OpeningPrice") is None or _is_special(board):
            return False  # 寄っていない / 特別気配中

        if self.peak is None:
            prev_close = board.get("PreviousClose") or self.prev_close
            self.peak = max(prev_close or 0, board.get("OpeningPrice") or 0, current)
            self.trough = current
        if current > self.peak:
            self.peak = self.trough = current
            return False
        self.trough = min(self.trough, current)

        dip_pct = (self.peak - self.trough) / self.peak * 100
        if dip_pct > self.config.max_dip_pct:
            logger.info(f"[{self.symbol} {self.name}] 下落 {dip_pct:.1f}% が上限を超えたため見送ります")
            self.gave_up = True
            return False
        if dip_pct < self.config.min_dip_pct:
            return False
        return current >= add_ticks(self.trough, self.config.rebound_ticks, self.price_range_group)


# ---------------------------------------------------------------------------
# トレード
# ---------------------------------------------------------------------------

@dataclass
class ReboundTrade(SpecialQuoteTrade):
    """
    押し目買いの1トレード。保有中・決済の処理は SpecialQuoteTrade と共通で、
    買い注文の扱い（一定時間で取消）と利確・損切り価格（%指定）だけが異なる。
    """
    ordered_at: Optional[datetime] = None

    def start(self, price: float, now: datetime) -> bool:  # type: ignore[override]
        self.initial_price = price
        self.ordered_at = now
        order_id = self.broker.buy_limit(self.symbol, self.qty, price)
        if not order_id:
            self.phase = Phase.DONE
            return False
        self.buy_order_id, self.buy_price = order_id, price
        self._info(f"押し目からの反発を検出。{price} で指値買い {self.qty}株")
        return True

    def _step_buying(self, now: datetime, board: dict):
        status = self.broker.get_order(self.buy_order_id)
        if status is None:
            return
        if status.finished:
            if status.cum_qty > 0:
                self._on_bought(status)
            else:
                self._info("買い注文が約定せずに終了しました")
                self.phase = Phase.DONE
            return
        if self.pending_cancel:
            return  # 取消の完了待ち
        if status.cum_qty > 0:
            self._info(f"一部約定（{status.cum_qty}株）。残りを取消します")
            self._cancel_buy("abort")
        elif (now - self.ordered_at).total_seconds() >= self.config.fill_wait_sec:
            self._info("買い注文が約定しないため取消します")
            self._cancel_buy("abort")

    def _exit_targets(self):
        tp = round_up_to_tick(self.avg_price * (1 + self.config.take_profit_pct / 100),
                              self.price_range_group)
        tp = max(tp, add_ticks(self.avg_price, 1, self.price_range_group))
        sl = round(self.avg_price * (1 - self.config.stop_loss_pct / 100), 2)
        return tp, sl


# ---------------------------------------------------------------------------
# エンジン
# ---------------------------------------------------------------------------

class StopHighReboundEngine:
    def __init__(self, api: KabuApi, broker, config: StopHighConfig,
                 clock: Callable[[], datetime] = datetime.now):
        self.api = api
        self.broker = broker
        self.config = config
        self.clock = clock
        self.watchers: Dict[str, DipWatcher] = {}
        self.active: Dict[str, ReboundTrade] = {}
        self.traded_today: Set[str] = set()   # 現物の差金決済を避けるため同日同銘柄は1回まで
        self._trade_date: Optional[date] = None
        self._collected_date: Optional[date] = None

    def _on_new_day(self, today: date):
        self._trade_date = today
        self.traded_today = set()
        self.watchers = {}
        if not is_market_day(today):
            return
        for item in load_stop_high(self.config.list_file, today):
            symbol = str(item["Symbol"])
            self.watchers[symbol] = DipWatcher(
                symbol=symbol, name=item.get("SymbolName", ""),
                prev_close=item.get("Close") or 0,
                price_range_group=item.get("PriceRangeGroup"), config=self.config,
            )
        if self.watchers:
            logger.info(f"前日ストップ高銘柄 {len(self.watchers)}件を監視します: {list(self.watchers)}")
        else:
            logger.info("監視対象の前日ストップ高銘柄はありません")

    def in_entry_window(self, now: datetime) -> bool:
        return (is_market_day(now) and in_session(now)
                and self.config.entry_start <= now.time() < self.config.entry_end)

    def collection_due(self, now: datetime) -> bool:
        return (is_market_day(now) and now.time() >= self.config.collect_at
                and self._collected_date != now.date())

    def tick(self):
        now = self.clock()
        if self._trade_date != now.date():
            self._on_new_day(now.date())

        for symbol, trade in list(self.active.items()):
            board = self.api.get_board(symbol)
            if board:
                trade.step(now, board)
            if trade.phase == Phase.DONE:
                del self.active[symbol]
                if trade.filled_qty > 0:
                    self.traded_today.add(symbol)
                else:
                    self.watchers[symbol].attempts += 1

        if self.in_entry_window(now):
            self._watch(now)

        if self.collection_due(now):
            self.collect(now.date())

    def _watch(self, now: datetime):
        for symbol, watcher in self.watchers.items():
            if (symbol in self.active or symbol in self.traded_today or watcher.gave_up
                    or watcher.attempts >= self.config.max_entry_attempts):
                continue
            board = self.api.get_board(symbol)
            if not board or not watcher.update(board):
                continue
            if len(self.active) >= self.config.max_positions:
                continue
            self._enter(watcher, board, now)

    def _enter(self, watcher: DipWatcher, board: dict, now: datetime):
        # kabu API は名称が逆: BidPrice が最良売気配。すぐ約定させるため売気配で指値
        price = board.get("BidPrice") or board.get("CurrentPrice")
        if price < self.config.min_price or (self.config.max_price and price > self.config.max_price):
            watcher.gave_up = True
            return
        qty = calculate_purchase_quantity(price, self.config.budget_per_trade)
        if qty <= 0:
            logger.info(f"[{watcher.symbol}] 予算内で購入できないためスキップ（{price}円）")
            watcher.gave_up = True
            return
        trade = ReboundTrade(
            symbol=watcher.symbol, name=watcher.name, price_range_group=watcher.price_range_group,
            qty=qty, broker=self.broker, config=self.config,
        )
        logger.info(f"[{watcher.symbol} {watcher.name}] 基準値 {watcher.peak} → 安値 {watcher.trough} "
                    f"→ 現在値 {board.get('CurrentPrice')}")
        if trade.start(price, now):
            self.active[watcher.symbol] = trade
        else:
            watcher.attempts += 1

    def collect(self, trade_date: date):
        self._collected_date = trade_date
        items = collect_stop_high(self.api, self.config.ranking_exchange)
        save_stop_high(self.config.list_file, trade_date, items)

    def run_forever(self):
        mode = "ペーパートレード（発注しません）" if self.config.dry_run else "実発注"
        logger.info(f"前日ストップ高銘柄の押し目買いを開始します（{mode}）")
        while True:
            try:
                now = self.clock()
                if not (self.active or self.in_entry_window(now) or self.collection_due(now)):
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

    config = StopHighConfig.from_env()
    api = KabuApi(get_token(config.api_password))

    if len(sys.argv) > 1 and sys.argv[1] == "collect":
        now = datetime.now()
        if is_market_day(now) and dt_time(9, 0) <= now.time() < dt_time(15, 30):
            raise SystemExit("取引時間中は収集できません。引け後（15:30以降）か翌朝の寄り前に実行してください")
        # 寄り前に実行した場合、板・ランキングは前営業日の引け時点の内容
        trade_date = now.date() if is_market_day(now) and now.time() >= dt_time(15, 30) \
            else previous_market_day(now.date())
        save_stop_high(config.list_file, trade_date, collect_stop_high(api, config.ranking_exchange))
        return

    broker = PaperBroker() if config.dry_run else KabuBroker(api, config.account_type)
    StopHighReboundEngine(api, broker, config).run_forever()


if __name__ == "__main__":
    main()
