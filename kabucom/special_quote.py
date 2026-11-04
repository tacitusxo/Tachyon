"""
特別買い気配（特気）スキャルピング戦略

概要:
  1. 値上がり率ランキングの上位銘柄から「特別買い気配」（AskSign == "0102"）の銘柄を探す
  2. 気配が切り上がる前に、現在の特別気配値で指値買いを出す
  3. 特別気配のまま気配値が切り上がったら、買い注文を取消して新しい気配値で出し直す
     （kabu API には株式の訂正注文がないため「取消 → 再発注」で訂正する）
  4. 約定したら「約定単価 + 利確ティック」で指値売りを出す
  5. 現在値が「約定単価 - 損切りティック」以下になったら、利確注文を取消して成行で売る
     （kabu API は OCO 注文に対応していないため、損切りはプログラム側で監視する）
  6. 強制決済時刻になったら保有分を成行で売る

安全のため、デフォルトはペーパートレード（実際には発注しない）。
実発注するには環境変数 SQ_DRY_RUN=false を設定する。

起動:
  cd kabucom
  python special_quote.py
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from enum import Enum
from typing import Callable, Dict, List, Optional, Set

import jpholiday
from dotenv import load_dotenv

from tick_size import add_ticks
from utils import calculate_purchase_quantity

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_URL = "http://localhost:18080/kabusapi"

# kabu API の気配フラグ（AskSign = 最良買気配フラグ）
SIGN_SPECIAL_QUOTE = "0102"  # 特別気配


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_time(name: str, default: str) -> dt_time:
    hour, minute = os.getenv(name, default).split(":")
    return dt_time(int(hour), int(minute))


@dataclass
class SpecialQuoteConfig:
    api_password: str
    account_type: int
    budget_per_trade: float           # 1トレードあたりの予算（円）
    dry_run: bool = True              # True: ペーパートレード
    ranking_exchange: str = "T"       # ランキングの市場（ALL/T/TP/TS/TG）
    ranking_top_n: int = 10           # ランキング上位何銘柄の板を確認するか
    scan_interval_sec: float = 5.0    # ランキング確認間隔
    poll_interval_sec: float = 1.0    # 保有/注文中銘柄の監視間隔
    min_price: float = 0              # 対象とする気配値の下限
    max_price: float = 0              # 対象とする気配値の上限（0 = 上限なし）
    take_profit_ticks: int = 2        # 利確ティック数
    stop_loss_ticks: int = 3          # 損切りティック数
    max_reorders: int = 5             # 切り上げに追随して出し直す最大回数
    max_chase_pct: float = 5.0        # 初回発注価格から何%上まで追随するか
    fill_wait_after_open_sec: float = 10.0  # 特気解消後、未約定なら何秒で取消すか
    max_positions: int = 1            # 同時に扱う銘柄数
    entry_end: dt_time = dt_time(15, 0)     # 新規エントリーの締切
    force_exit: dt_time = dt_time(15, 20)   # 強制決済時刻

    @classmethod
    def from_env(cls) -> "SpecialQuoteConfig":
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            load_dotenv(dotenv_path=env_path)

        api_password = os.getenv("KABUCOM_API_PASSWORD")
        if not api_password:
            raise SystemExit("Error: KABUCOM_API_PASSWORD environment variable is not set")
        account_type = os.getenv("KABUCOM_ACCOUNT_TYPE")
        if not account_type:
            raise SystemExit("Error: KABUCOM_ACCOUNT_TYPE environment variable is not set")
        budget = os.getenv("SQ_BUDGET_PER_TRADE") or os.getenv("AVAILABLE_MARGIN")
        if not budget or float(budget) <= 0:
            raise SystemExit("Error: SQ_BUDGET_PER_TRADE (or AVAILABLE_MARGIN) is not set or invalid")

        return cls(
            api_password=api_password,
            account_type=int(account_type),
            budget_per_trade=float(budget),
            dry_run=_env_bool("SQ_DRY_RUN", True),
            ranking_exchange=os.getenv("SQ_RANKING_EXCHANGE", "T"),
            ranking_top_n=int(os.getenv("SQ_RANKING_TOP_N", "10")),
            scan_interval_sec=float(os.getenv("SQ_SCAN_INTERVAL_SEC", "5")),
            poll_interval_sec=float(os.getenv("SQ_POLL_INTERVAL_SEC", "1")),
            min_price=float(os.getenv("SQ_MIN_PRICE", "0")),
            max_price=float(os.getenv("SQ_MAX_PRICE", "0")),
            take_profit_ticks=int(os.getenv("SQ_TAKE_PROFIT_TICKS", "2")),
            stop_loss_ticks=int(os.getenv("SQ_STOP_LOSS_TICKS", "3")),
            max_reorders=int(os.getenv("SQ_MAX_REORDERS", "5")),
            max_chase_pct=float(os.getenv("SQ_MAX_CHASE_PCT", "5")),
            fill_wait_after_open_sec=float(os.getenv("SQ_FILL_WAIT_AFTER_OPEN_SEC", "10")),
            max_positions=int(os.getenv("SQ_MAX_POSITIONS", "1")),
            entry_end=_env_time("SQ_ENTRY_END", "15:00"),
            force_exit=_env_time("SQ_FORCE_EXIT", "15:20"),
        )


# ---------------------------------------------------------------------------
# kabu API クライアント（情報系）
# ---------------------------------------------------------------------------

class KabuApi:
    """kabu ステーション API の薄いラッパー。情報系APIの流量制限（10回/秒）に配慮して間隔を空ける"""

    MIN_INTERVAL_SEC = 0.12

    def __init__(self, token: str):
        self.token = token
        self._last_call = 0.0

    def _throttle(self):
        wait = self.MIN_INTERVAL_SEC - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def request(self, method: str, path: str, params: Optional[dict] = None,
                body: Optional[dict] = None):
        self._throttle()
        url = f"{BASE_URL}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-API-KEY", self.token)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return json.loads(res.read())
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read())
            except Exception:
                detail = None
            logger.error(f"API エラー {method} {path}: {e} {detail}")
            return None
        except Exception as e:
            logger.error(f"API 通信エラー {method} {path}: {e}")
            return None

    def get_board(self, symbol: str) -> Optional[dict]:
        return self.request("GET", f"/board/{symbol}@1")

    def get_symbol(self, symbol: str) -> Optional[dict]:
        return self.request("GET", f"/symbol/{symbol}@1")

    def get_ranking(self, exchange: str) -> List[dict]:
        content = self.request("GET", "/ranking", {"type": 1, "ExchangeDivision": exchange})
        if not content:
            return []
        return content.get("Ranking") or []


# ---------------------------------------------------------------------------
# 発注（ブローカー）
# ---------------------------------------------------------------------------

@dataclass
class OrderStatus:
    order_id: str
    finished: bool        # 全約定・取消・失効などで注文が終了しているか
    cum_qty: int          # 約定済数量
    avg_price: float      # 平均約定単価（約定なしは 0）


class KabuBroker:
    """kabu API で実際に発注するブローカー（現物）"""

    def __init__(self, api: KabuApi, account_type: int):
        self.api = api
        self.account_type = account_type

    def _send(self, obj: dict) -> Optional[str]:
        content = self.api.request("POST", "/sendorder", body=obj)
        if not content or content.get("Result") != 0:
            logger.error(f"発注失敗: {obj} -> {content}")
            return None
        logger.info(f"発注成功: {obj['Symbol']} Side={obj['Side']} Qty={obj['Qty']} "
                    f"Price={obj['Price']} OrderId={content.get('OrderId')}")
        return content.get("OrderId")

    def buy_limit(self, symbol: str, qty: int, price: float) -> Optional[str]:
        return self._send({
            "Symbol": symbol, "Exchange": 1, "SecurityType": 1,
            "Side": "2", "CashMargin": 1, "DelivType": 2, "FundType": "02",
            "AccountType": self.account_type, "Qty": qty,
            "FrontOrderType": 20, "Price": price, "ExpireDay": 0,
        })

    def sell_limit(self, symbol: str, qty: int, price: float) -> Optional[str]:
        return self._send({
            "Symbol": symbol, "Exchange": 1, "SecurityType": 1,
            "Side": "1", "CashMargin": 1, "DelivType": 0, "FundType": "  ",
            "AccountType": self.account_type, "Qty": qty,
            "FrontOrderType": 20, "Price": price, "ExpireDay": 0,
        })

    def sell_market(self, symbol: str, qty: int) -> Optional[str]:
        return self._send({
            "Symbol": symbol, "Exchange": 1, "SecurityType": 1,
            "Side": "1", "CashMargin": 1, "DelivType": 0, "FundType": "  ",
            "AccountType": self.account_type, "Qty": qty,
            "FrontOrderType": 10, "Price": 0, "ExpireDay": 0,
        })

    def cancel(self, order_id: str) -> bool:
        content = self.api.request("PUT", "/cancelorder", body={"OrderId": order_id})
        ok = bool(content) and content.get("Result") == 0
        if not ok:
            logger.warning(f"取消失敗（約定済の可能性あり）: {order_id} -> {content}")
        return ok

    def get_order(self, order_id: str) -> Optional[OrderStatus]:
        content = self.api.request("GET", "/orders", {"product": 1, "id": order_id})
        if not content:
            return None
        order = content[0] if isinstance(content, list) else content
        fills = [d for d in (order.get("Details") or []) if d.get("RecType") == 8]
        qty = sum(d.get("Qty") or 0 for d in fills)
        avg = sum((d.get("Price") or 0) * (d.get("Qty") or 0) for d in fills) / qty if qty else 0.0
        return OrderStatus(
            order_id=order_id,
            finished=order.get("State") == 5,
            cum_qty=int(order.get("CumQty") or qty),
            avg_price=avg,
        )

    def observe(self, symbol: str, board: dict):
        """実ブローカーでは不要（ペーパートレードとのインターフェース合わせ）"""


@dataclass
class _PaperOrder:
    symbol: str
    side: str          # "buy" / "sell"
    qty: int
    price: Optional[float]  # None = 成行
    cum_qty: int = 0
    avg_price: float = 0.0
    finished: bool = False


class PaperBroker:
    """
    発注せずに板情報から約定を模擬するブローカー。
      - 指値買い: 特別気配が解消し、現在値が指値以下になったら指値で約定
      - 指値売り: 現在値が指値以上になったら指値で約定
      - 成行売り: 現在値で即約定
    実際より約定しやすい（楽観的な）想定なので、成績はあくまで参考値。
    """

    def __init__(self):
        self.orders: Dict[str, _PaperOrder] = {}
        self._ids = itertools.count(1)

    def _new(self, order: _PaperOrder) -> str:
        order_id = f"PAPER-{next(self._ids)}"
        self.orders[order_id] = order
        price = "成行" if order.price is None else order.price
        logger.info(f"[DRY RUN] 発注: {order.symbol} {order.side} {order.qty}株 @ {price} ({order_id})")
        return order_id

    def buy_limit(self, symbol, qty, price):
        return self._new(_PaperOrder(symbol, "buy", qty, price))

    def sell_limit(self, symbol, qty, price):
        return self._new(_PaperOrder(symbol, "sell", qty, price))

    def sell_market(self, symbol, qty):
        return self._new(_PaperOrder(symbol, "sell", qty, None))

    def cancel(self, order_id):
        order = self.orders[order_id]
        if order.finished:
            return False
        order.finished = True
        logger.info(f"[DRY RUN] 取消: {order_id}")
        return True

    def get_order(self, order_id):
        o = self.orders[order_id]
        return OrderStatus(order_id, o.finished, o.cum_qty, o.avg_price)

    def observe(self, symbol: str, board: dict):
        current = board.get("CurrentPrice")
        special = board.get("AskSign") == SIGN_SPECIAL_QUOTE
        if current is None:
            return
        for order in self.orders.values():
            if order.finished or order.symbol != symbol:
                continue
            fill_price = None
            if order.side == "buy" and not special and current <= order.price:
                fill_price = order.price
            elif order.side == "sell" and order.price is None:
                fill_price = current
            elif order.side == "sell" and current >= order.price:
                fill_price = order.price
            if fill_price is not None:
                order.cum_qty, order.avg_price, order.finished = order.qty, fill_price, True


# ---------------------------------------------------------------------------
# トレード（1銘柄ごとの状態機械）
# ---------------------------------------------------------------------------

class Phase(Enum):
    BUYING = "買い注文中"
    HOLDING = "保有中（利確注文中）"
    EXITING = "決済中（成行）"
    DONE = "終了"


@dataclass
class SpecialQuoteTrade:
    symbol: str
    name: str
    price_range_group: Optional[str]
    qty: int
    broker: object
    config: SpecialQuoteConfig

    phase: Phase = Phase.BUYING
    initial_price: float = 0.0
    buy_price: float = 0.0
    buy_order_id: Optional[str] = None
    reorder_count: int = 0
    pending_cancel: Optional[str] = None   # "reorder" / "abort" / "exit"
    reorder_price: float = 0.0
    quote_ended_at: Optional[datetime] = None

    filled_qty: int = 0
    avg_price: float = 0.0
    tp_order_id: Optional[str] = None
    tp_price: float = 0.0
    sl_price: float = 0.0
    tp_filled_qty: int = 0
    tp_avg_price: float = 0.0
    exit_order_id: Optional[str] = None
    exit_reason: str = ""
    pnl: float = 0.0

    def _info(self, message: str):
        logger.info(f"[{self.symbol} {self.name}] {message}")

    # --- エントリー ---------------------------------------------------------

    def start(self, quote_price: float) -> bool:
        self.initial_price = quote_price
        return self._place_buy(quote_price)

    def _place_buy(self, price: float) -> bool:
        order_id = self.broker.buy_limit(self.symbol, self.qty, price)
        if not order_id:
            self.phase = Phase.DONE
            return False
        self.buy_order_id, self.buy_price = order_id, price
        self._info(f"特別買い気配 {price} で指値買い {self.qty}株")
        return True

    # --- 1ステップ ----------------------------------------------------------

    def step(self, now: datetime, board: dict):
        self.broker.observe(self.symbol, board)
        if self.phase == Phase.BUYING:
            self._step_buying(now, board)
        elif self.phase == Phase.HOLDING:
            self._step_holding(now, board)
        elif self.phase == Phase.EXITING:
            self._step_exiting()

    def _step_buying(self, now: datetime, board: dict):
        status = self.broker.get_order(self.buy_order_id)
        if status is None:
            return

        if status.finished:
            if status.cum_qty > 0:
                self._on_bought(status)
            elif self.pending_cancel == "reorder":
                self.pending_cancel = None
                self.reorder_count += 1
                self._info(f"気配切り上げに追随して再発注（{self.reorder_count}回目）")
                self._place_buy(self.reorder_price)
            else:
                self._info("買い注文が約定せずに終了しました")
                self.phase = Phase.DONE
            return

        if self.pending_cancel:
            return  # 取消の完了待ち

        if status.cum_qty > 0:
            # 一部約定: 追随はやめ、残りを取消して約定分だけで利確に移る
            self._info(f"一部約定（{status.cum_qty}株）。残りを取消します")
            self._cancel_buy("abort")
            return

        special = board.get("AskSign") == SIGN_SPECIAL_QUOTE
        quote = board.get("AskPrice")

        if not special:
            # 特別気配が解消（寄り付いた）。少し待っても約定しなければ取消
            if self.quote_ended_at is None:
                self.quote_ended_at = now
                self._info("特別気配が解消しました。約定を待ちます")
            elif (now - self.quote_ended_at).total_seconds() >= self.config.fill_wait_after_open_sec:
                self._info("特気解消後に約定しなかったため取消します")
                self._cancel_buy("abort")
            return
        self.quote_ended_at = None

        if quote and quote > self.buy_price:
            limit = self.initial_price * (1 + self.config.max_chase_pct / 100)
            if self.reorder_count >= self.config.max_reorders or quote > limit:
                self._info(f"気配 {quote} が追随上限を超えたため見送ります")
                self._cancel_buy("abort")
            else:
                self.reorder_price = quote
                self._cancel_buy("reorder")

        if now.time() >= self.config.entry_end and not self.pending_cancel:
            self._info("エントリー締切時刻のため取消します")
            self._cancel_buy("abort")

    def _cancel_buy(self, reason: str):
        self.pending_cancel = reason
        self.broker.cancel(self.buy_order_id)
        # 取消に失敗しても（約定済など）次ステップで注文状態を見て判断する

    def _on_bought(self, status: OrderStatus):
        self.filled_qty, self.avg_price = status.cum_qty, status.avg_price
        self.pending_cancel = None
        self.tp_price = add_ticks(self.avg_price, self.config.take_profit_ticks, self.price_range_group)
        self.sl_price = add_ticks(self.avg_price, -self.config.stop_loss_ticks, self.price_range_group)
        self._info(f"約定 {self.filled_qty}株 @ {self.avg_price}  利確 {self.tp_price} / 損切り {self.sl_price}")
        self.tp_order_id = self.broker.sell_limit(self.symbol, self.filled_qty, self.tp_price)
        self.phase = Phase.HOLDING
        if not self.tp_order_id:
            self._start_market_exit("利確注文の発注失敗")

    # --- 保有中 -------------------------------------------------------------

    def _step_holding(self, now: datetime, board: dict):
        status = self.broker.get_order(self.tp_order_id)
        if status is None:
            return
        self.tp_filled_qty, self.tp_avg_price = status.cum_qty, status.avg_price

        if status.finished:
            if status.cum_qty >= self.filled_qty:
                self._finish("利確")
            else:
                # 取消済（損切り/強制決済）または失効 → 残りを成行で売る
                self._start_market_exit(self.exit_reason or "利確注文が終了")
            return

        if self.pending_cancel == "exit":
            return

        current = board.get("CurrentPrice")
        if current is not None and current <= self.sl_price:
            self.exit_reason = f"損切り（現在値 {current} <= {self.sl_price}）"
        elif now.time() >= self.config.force_exit:
            self.exit_reason = "強制決済時刻"
        else:
            return
        self._info(f"{self.exit_reason}: 利確注文を取消して成行で売ります")
        self.pending_cancel = "exit"
        self.broker.cancel(self.tp_order_id)

    def _start_market_exit(self, reason: str):
        remaining = self.filled_qty - self.tp_filled_qty
        self.exit_reason = reason
        if remaining <= 0:
            self._finish("利確")
            return
        self.exit_order_id = self.broker.sell_market(self.symbol, remaining)
        self.phase = Phase.EXITING
        if not self.exit_order_id:
            logger.error(f"[{self.symbol}] 成行売りの発注に失敗しました。手動で決済してください（{remaining}株）")
            self.phase = Phase.DONE

    def _step_exiting(self):
        status = self.broker.get_order(self.exit_order_id)
        if status and status.finished:
            if status.cum_qty < self.filled_qty - self.tp_filled_qty:
                logger.error(f"[{self.symbol}] 成行売りが全量約定せずに終了しました。手動で確認してください")
            self._finish(self.exit_reason, status.cum_qty, status.avg_price)

    def _finish(self, reason: str, exit_qty: int = 0, exit_price: float = 0.0):
        proceeds = self.tp_filled_qty * self.tp_avg_price + exit_qty * exit_price
        self.pnl = proceeds - (self.tp_filled_qty + exit_qty) * self.avg_price
        self.exit_reason = reason
        self._info(f"{reason}で終了。概算損益 {self.pnl:+,.0f}円（手数料除く）")
        self.phase = Phase.DONE


# ---------------------------------------------------------------------------
# エンジン
# ---------------------------------------------------------------------------

def is_market_day(now: datetime) -> bool:
    return now.weekday() < 5 and not jpholiday.is_holiday(now.date())


def in_session(now: datetime) -> bool:
    """前場 9:00-11:30 / 後場 12:30-15:30"""
    t = now.time()
    return (dt_time(9, 0) <= t <= dt_time(11, 30)) or (dt_time(12, 30) <= t <= dt_time(15, 30))


class SpecialQuoteEngine:
    def __init__(self, api: KabuApi, broker, config: SpecialQuoteConfig,
                 clock: Callable[[], datetime] = datetime.now):
        self.api = api
        self.broker = broker
        self.config = config
        self.clock = clock
        self.active: Dict[str, SpecialQuoteTrade] = {}
        self.traded_today: Set[str] = set()   # 現物の差金決済を避けるため同日同銘柄は1回まで
        self._trade_date = None
        self._last_scan: Optional[datetime] = None

    def tick(self):
        now = self.clock()
        if self._trade_date != now.date():
            self._trade_date, self.traded_today = now.date(), set()

        for symbol, trade in list(self.active.items()):
            board = self.api.get_board(symbol)
            if board:
                trade.step(now, board)
            if trade.phase == Phase.DONE:
                del self.active[symbol]

        can_enter = (
            is_market_day(now) and in_session(now)
            and now.time() < self.config.entry_end
            and len(self.active) < self.config.max_positions
        )
        due = self._last_scan is None or \
            (now - self._last_scan).total_seconds() >= self.config.scan_interval_sec
        if can_enter and due:
            self._last_scan = now
            self.scan(now)

    def scan(self, now: datetime):
        for item in self.api.get_ranking(self.config.ranking_exchange)[: self.config.ranking_top_n]:
            if len(self.active) >= self.config.max_positions:
                return
            symbol = str(item.get("Symbol"))
            if symbol in self.traded_today or symbol in self.active:
                continue
            board = self.api.get_board(symbol)
            if not board or board.get("AskSign") != SIGN_SPECIAL_QUOTE:
                continue
            quote = board.get("AskPrice")
            if not quote:
                continue
            if quote < self.config.min_price or (self.config.max_price and quote > self.config.max_price):
                continue
            self._enter(symbol, item.get("SymbolName", ""), quote, now)

    def _enter(self, symbol: str, name: str, quote: float, now: datetime):
        info = self.api.get_symbol(symbol) or {}
        qty = calculate_purchase_quantity(quote, self.config.budget_per_trade)
        if qty <= 0:
            logger.info(f"[{symbol}] 予算内で購入できないためスキップ（気配 {quote}）")
            return
        trade = SpecialQuoteTrade(
            symbol=symbol, name=name, price_range_group=info.get("PriceRangeGroup"),
            qty=qty, broker=self.broker, config=self.config,
        )
        self.traded_today.add(symbol)
        if trade.start(quote):
            self.active[symbol] = trade

    def run_forever(self):
        mode = "ペーパートレード（発注しません）" if self.config.dry_run else "実発注"
        logger.info(f"特別買い気配スキャルピングを開始します（{mode}）")
        while True:
            try:
                now = self.clock()
                if not self.active and not (is_market_day(now) and in_session(now)):
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

    config = SpecialQuoteConfig.from_env()
    api = KabuApi(get_token(config.api_password))
    broker = PaperBroker() if config.dry_run else KabuBroker(api, config.account_type)
    SpecialQuoteEngine(api, broker, config).run_forever()


if __name__ == "__main__":
    main()
