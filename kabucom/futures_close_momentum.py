"""
D. 引け前のモメンタム（日経225 マイクロ / ミニ先物）

なぜ: レバレッジ ETF（日経レバなど）は、指数が上がった日は引けにかけて買い増し、
     下がった日は売る必要がある（値段を気にしない機械的な売買）。オプションを売っている業者の
     ヘッジも同じ方向になりやすい。そのため、その日の値動きの方向が引けにかけて続きやすい。

流れ:
  1. FM_SIGNAL_AT（既定 15:00）に、日経平均（FM_INDEX_SYMBOL=101）の前日比 r を見る
     - |r| が FM_MIN_MOVE_PCT% 以上
     - FM_LETF_EXPOSURES（レバレッジ ETF の倍率と純資産額）を設定していれば、
       想定リバランス額 = Σ 純資産額 × L × (L − 1) × r が FM_MIN_REBALANCE_OKU 億円以上
     のとき、r の方向に先物を成行で新規建てする（FM_LONG_ONLY=true なら買いだけ）
  2. 約定したらすぐに、日中立会の引け（クロージング・オークション）で返済する引成注文を出す
  3. 現在値が建値から FM_STOP_LOSS_PCT% 逆行したら、引成を取消して成行で返済する

注意:
  - 先物口座が必要。証拠金を超える損失が出ることがある
  - 先物の注文の執行条件（FrontOrderType / TimeInForce）は kabu ステーション API の仕様で確認すること
    （既定: 成行=120・FAK、引成=18・FAK。FM_TIF_MARKET / FM_TIF_CLOSE で変更できる）
  - 1日1回まで。メジャー SQ 日などの特殊な日は FM_SKIP_DATES で除外できる

安全のため既定はペーパートレード。実発注は FM_DRY_RUN=false。

起動:
  cd kabucom
  python futures_close_momentum.py
"""
from __future__ import annotations

import itertools
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from typing import Callable, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from special_quote import KabuApi, OrderStatus, _env_bool, _env_time, is_market_day

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

MULTIPLIER = {"NK225micro": 10, "NK225mini": 100, "NK225": 1000, "TOPIXmini": 1000, "TOPIX": 10000}
DAY_SESSION_CLOSE = dt_time(15, 45)   # 日中立会の引け（クロージング・オークション終了）


def parse_exposures(text: str) -> List[Tuple[float, float]]:
    """'2:1500000000000,-1:200000000000' → [(2.0, 1.5e12), (-1.0, 2e11)]"""
    result = []
    for part in (text or "").split(","):
        if ":" in part:
            lev, aum = part.split(":", 1)
            result.append((float(lev), float(aum)))
    return result


def rebalance_amount(exposures: List[Tuple[float, float]], r: float) -> float:
    """レバレッジ ETF が引けにかけて売買する想定額（円）。正なら買い、負なら売り"""
    return sum(aum * lev * (lev - 1) * r for lev, aum in exposures)


@dataclass
class FuturesMomentumConfig:
    api_password: str
    dry_run: bool = True
    future_code: str = "NK225micro"
    deriv_month: int = 0               # 0 = 直近限月
    qty: int = 1
    index_symbol: str = "101"          # 日経平均
    signal_at: dt_time = dt_time(15, 0)
    entry_deadline: dt_time = dt_time(15, 30)
    min_move_pct: float = 0.5
    min_rebalance_oku: float = 0.0     # 億円（0 = 判定しない）
    letf_exposures: List[Tuple[float, float]] = field(default_factory=list)
    long_only: bool = False
    stop_loss_pct: float = 0.7
    poll_interval_sec: float = 2.0
    exchange: int = 23                 # 23 = 日中
    tif_market: int = 2                # 2 = FAK
    tif_close: int = 2
    skip_dates: List[date] = field(default_factory=list)

    @property
    def multiplier(self) -> int:
        return MULTIPLIER.get(self.future_code, 1)

    @classmethod
    def from_env(cls) -> "FuturesMomentumConfig":
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            load_dotenv(dotenv_path=env_path)
        api_password = os.getenv("KABUCOM_API_PASSWORD")
        if not api_password:
            raise SystemExit("Error: KABUCOM_API_PASSWORD environment variable is not set")
        g = os.getenv
        skip = [date.fromisoformat(d.strip()) for d in g("FM_SKIP_DATES", "").split(",") if d.strip()]
        return cls(
            api_password=api_password,
            dry_run=_env_bool("FM_DRY_RUN", True),
            future_code=g("FM_FUTURE_CODE", "NK225micro"),
            deriv_month=int(g("FM_DERIV_MONTH", "0")),
            qty=int(g("FM_QTY", "1")),
            index_symbol=g("FM_INDEX_SYMBOL", "101"),
            signal_at=_env_time("FM_SIGNAL_AT", "15:00"),
            entry_deadline=_env_time("FM_ENTRY_DEADLINE", "15:30"),
            min_move_pct=float(g("FM_MIN_MOVE_PCT", "0.5")),
            min_rebalance_oku=float(g("FM_MIN_REBALANCE_OKU", "0")),
            letf_exposures=parse_exposures(g("FM_LETF_EXPOSURES", "")),
            long_only=_env_bool("FM_LONG_ONLY", False),
            stop_loss_pct=float(g("FM_STOP_LOSS_PCT", "0.7")),
            poll_interval_sec=float(g("FM_POLL_INTERVAL_SEC", "2")),
            exchange=int(g("FM_EXCHANGE", "23")),
            tif_market=int(g("FM_TIF_MARKET", "2")),
            tif_close=int(g("FM_TIF_CLOSE", "2")),
            skip_dates=skip,
        )


# ---------------------------------------------------------------------------
# ブローカー
# ---------------------------------------------------------------------------

class KabuFuturesBroker:
    def __init__(self, api: KabuApi, config: FuturesMomentumConfig):
        self.api = api
        self.config = config

    def order(self, symbol: str, side: str, qty: int, kind: str, close: bool) -> Optional[str]:
        """side: buy/sell, kind: market（成行）/ close（引成）, close: True なら返済"""
        c = self.config
        body = {
            "Symbol": symbol, "Exchange": c.exchange, "TradeType": 2 if close else 1,
            "TimeInForce": c.tif_close if kind == "close" else c.tif_market,
            "Side": "2" if side == "buy" else "1", "Qty": qty,
            "FrontOrderType": 18 if kind == "close" else 120, "Price": 0, "ExpireDay": 0,
        }
        if close:
            body["ClosePositionOrder"] = 0   # 日付の古い順に返済
        content = self.api.request("POST", "/sendorder/future", body=body)
        if not content or content.get("Result") != 0:
            logger.error(f"先物の発注失敗: {body} -> {content}")
            return None
        logger.info(f"先物の発注成功: {body} OrderId={content.get('OrderId')}")
        return content.get("OrderId")

    def cancel(self, order_id: str) -> bool:
        content = self.api.request("PUT", "/cancelorder", body={"OrderId": order_id})
        return bool(content) and content.get("Result") == 0

    def get_order(self, order_id: str) -> Optional[OrderStatus]:
        content = self.api.request("GET", "/orders", {"product": 3, "id": order_id})
        if not content:
            return None
        o = content[0] if isinstance(content, list) else content
        fills = [d for d in (o.get("Details") or []) if d.get("RecType") == 8]
        qty = sum(d.get("Qty") or 0 for d in fills)
        avg = sum((d.get("Price") or 0) * (d.get("Qty") or 0) for d in fills) / qty if qty else 0.0
        return OrderStatus(order_id, o.get("State") == 5, int(o.get("CumQty") or qty), avg)

    def observe(self, board: dict, now: datetime):
        """実ブローカーでは不要"""


class PaperFuturesBroker:
    """成行は現在値、引成は 15:45 以降に見た現在値（＝日中の清算前の終値）で約定したとみなす"""

    def __init__(self):
        self.orders: Dict[str, dict] = {}
        self._ids = itertools.count(1)

    def order(self, symbol, side, qty, kind, close):
        order_id = f"PAPER-F{next(self._ids)}"
        self.orders[order_id] = {"side": side, "qty": qty, "kind": kind, "cum": 0, "avg": 0.0, "done": False}
        logger.info(f"[DRY RUN] 先物 {'返済' if close else '新規'} {side} {qty}枚 {kind} ({order_id})")
        return order_id

    def cancel(self, order_id):
        o = self.orders[order_id]
        if o["done"]:
            return False
        o["done"] = True
        return True

    def get_order(self, order_id):
        o = self.orders[order_id]
        return OrderStatus(order_id, o["done"], o["cum"], o["avg"])

    def observe(self, board: dict, now: datetime):
        current = board.get("CurrentPrice")
        if not current:
            return
        for o in self.orders.values():
            if o["done"]:
                continue
            if o["kind"] == "market" or now.time() >= DAY_SESSION_CLOSE:
                o.update(cum=o["qty"], avg=current, done=True)


# ---------------------------------------------------------------------------
# エンジン
# ---------------------------------------------------------------------------

class FuturesMomentumEngine:
    def __init__(self, api: KabuApi, broker, config: FuturesMomentumConfig,
                 clock: Callable[[], datetime] = datetime.now):
        self.api = api
        self.broker = broker
        self.config = config
        self.clock = clock
        self._day: Optional[date] = None
        self.symbol: Optional[str] = None
        self.reset()

    def reset(self):
        self.phase = "idle"          # idle → entering → holding →（stopping → exiting）→ done
        self.side: Optional[str] = None
        self.entry_order = self.close_order = self.exit_order = None
        self.entry_price = 0.0
        self.qty = 0
        self.exit_reason = ""
        self.pnl = 0.0

    def resolve_symbol(self) -> Optional[str]:
        c = self.config
        content = self.api.request("GET", "/symbolname/future",
                                   {"FutureCode": c.future_code, "DerivMonth": c.deriv_month})
        return (content or {}).get("Symbol")

    def future_board(self) -> dict:
        return self.api.request("GET", f"/board/{self.symbol}@2") or {}

    def signal(self) -> Optional[str]:
        """'buy' / 'sell' / None"""
        c = self.config
        idx = self.api.get_board(c.index_symbol) or {}
        current, prev = idx.get("CurrentPrice"), idx.get("PreviousClose")
        if not current or not prev:
            logger.warning("指数の価格が取れないため見送ります")
            return None
        r = current / prev - 1
        amount = rebalance_amount(c.letf_exposures, r)
        msg = f"日経平均 前日比 {r * 100:+.2f}%"
        if c.letf_exposures:
            msg += f" / 想定リバランス {amount / 1e8:+,.0f}億円"
        if abs(r) * 100 < c.min_move_pct:
            logger.info(f"{msg}: 値動きが小さいため見送ります")
            return None
        if c.min_rebalance_oku and abs(amount) / 1e8 < c.min_rebalance_oku:
            logger.info(f"{msg}: 想定リバランス額が小さいため見送ります")
            return None
        side = "buy" if r > 0 else "sell"
        if side == "sell" and c.long_only:
            logger.info(f"{msg}: 下げの日ですが買いのみの設定のため見送ります")
            return None
        logger.info(f"{msg}: {'買い' if side == 'buy' else '売り'}で入ります")
        return side

    def tick(self):
        now = self.clock()
        c = self.config
        if self._day != now.date():
            self._day = now.date()
            self.reset()
            if is_market_day(now):
                self.symbol = self.resolve_symbol()
                logger.info(f"対象の先物: {c.future_code} {self.symbol}")
        if not is_market_day(now) or not self.symbol:
            return

        if self.phase == "idle":
            if c.signal_at <= now.time() < c.entry_deadline:
                self.phase = "done"   # 判定は1日1回
                if now.date() in c.skip_dates:
                    logger.info("除外日のため見送ります")
                    return
                side = self.signal()
                if side:
                    self.side = side
                    self.entry_order = self.broker.order(self.symbol, side, c.qty, "market", close=False)
                    self.phase = "entering" if self.entry_order else "done"
            return

        if self.phase == "done":
            return
        board = self.future_board()
        self.broker.observe(board, now)
        exit_side = "sell" if self.side == "buy" else "buy"

        if self.phase == "entering":
            status = self.broker.get_order(self.entry_order)
            if status and status.finished:
                if status.cum_qty <= 0:
                    logger.info("新規注文が約定しませんでした")
                    self.phase = "done"
                    return
                self.qty, self.entry_price = status.cum_qty, status.avg_price
                logger.info(f"建玉 {self.side} {self.qty}枚 @ {self.entry_price}。引成で返済注文を出します")
                self.close_order = self.broker.order(self.symbol, exit_side, self.qty, "close", close=True)
                self.phase = "holding"
                if not self.close_order:
                    self._exit_market(exit_side, "引成の発注失敗")
            return

        if self.phase == "holding":
            status = self.broker.get_order(self.close_order)
            if status and status.finished:
                if status.cum_qty >= self.qty:
                    self._finish(status.avg_price, "引け")
                elif self.exit_reason:
                    self._exit_market(exit_side, self.exit_reason, self.qty - status.cum_qty)
                else:
                    logger.error("引成の返済注文が約定せずに終了しました。手動で建玉を確認してください")
                    self.phase = "done"
                return
            current = board.get("CurrentPrice")
            if current and c.stop_loss_pct and not self.exit_reason:
                loss = (self.entry_price - current) if self.side == "buy" else (current - self.entry_price)
                if loss / self.entry_price * 100 >= c.stop_loss_pct:
                    self.exit_reason = f"損切り（現在値 {current}）"
                    logger.info(f"{self.exit_reason}: 引成を取消して成行で返済します")
                    self.broker.cancel(self.close_order)
            return

        if self.phase == "exiting":
            status = self.broker.get_order(self.exit_order)
            if status and status.finished:
                if status.cum_qty < self.qty:
                    logger.error("成行の返済が全量約定しませんでした。手動で建玉を確認してください")
                self._finish(status.avg_price, self.exit_reason)

    def _exit_market(self, side: str, reason: str, qty: Optional[int] = None):
        self.exit_reason = reason
        self.qty = qty or self.qty
        self.exit_order = self.broker.order(self.symbol, side, self.qty, "market", close=True)
        self.phase = "exiting"
        if not self.exit_order:
            logger.error(f"成行の返済に失敗しました。手動で返済してください（{self.qty}枚）")
            self.phase = "done"

    def _finish(self, price: float, reason: str):
        sign = 1 if self.side == "buy" else -1
        self.pnl = (price - self.entry_price) * sign * self.qty * self.config.multiplier
        logger.info(f"{reason}で返済 @ {price}。概算損益 {self.pnl:+,.0f}円（手数料除く）")
        self.phase = "done"

    def run_forever(self):
        mode = "ペーパートレード（発注しません）" if self.config.dry_run else "実発注"
        logger.info(f"引け前モメンタム（{self.config.future_code}）を開始します（{mode}）")
        while True:
            try:
                now = self.clock()
                busy = self.phase in ("entering", "holding", "exiting") or \
                    (self.phase == "idle" and self.config.signal_at <= now.time() < self.config.entry_deadline) \
                    or self._day != now.date()
                if not busy:
                    time.sleep(20)
                    continue
                self.tick()
                time.sleep(self.config.poll_interval_sec)
            except KeyboardInterrupt:
                if self.phase in ("entering", "holding", "exiting"):
                    logger.warning("建玉・注文が残っている可能性があります。手動で確認してください")
                logger.info("停止しました")
                return
            except Exception as e:
                logger.exception(f"エラーが発生しました: {e}")
                time.sleep(5)


def main():
    from kabu_token import get_token

    config = FuturesMomentumConfig.from_env()
    api = KabuApi(get_token(config.api_password))
    broker = PaperFuturesBroker() if config.dry_run else KabuFuturesBroker(api, config)
    FuturesMomentumEngine(api, broker, config).run_forever()


if __name__ == "__main__":
    main()
