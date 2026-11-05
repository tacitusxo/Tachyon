"""
① 上方修正監視・自動買い

適時開示（日経の開示一覧）を監視し、取引時間中に上方修正などの好材料が出た銘柄を成行で買う。
約定したら「約定単価 × (1 + UR_TAKE_PROFIT_PCT%)」（ストップ高が上限）で利確の指値売りを出す。

  - 監視は Ctrl+C で止めるまで続く。1日に買う銘柄数は UR_MAX_BUYS_PER_DAY（既定 1）まで
  - 成行の買いは、証券会社が当日の値幅上限（ストップ高）の値段で買付余力を拘束するため、
    株数は値幅上限の値段で計算する（現在値で計算すると余力不足で発注が拒否される）
  - ペーパートレード（UR_DRY_RUN=true、既定）では発注せず、板情報から約定を模擬してログに出す
"""
import logging
import os
from datetime import date, datetime
from typing import Callable, Dict, Optional, Set

from config import get_bool_env, load_environment_variables
from kabu_token import get_token
from price_limit import upper_limit as calc_upper_limit
from scraper import NikkeiScraper
from special_quote import KabuApi, KabuBroker, PaperBroker
from tick_size import round_down_to_tick, round_up_to_tick
from utils import calculate_purchase_quantity

# ロギングの設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class UpwardRevisionTrader:
    """検出した銘柄を成行で買い、約定後に利確の指値売りを出す"""

    def __init__(self, api, broker, budget: float, take_profit_pct: float = 20.0,
                 max_buys_per_day: int = 1, clock: Callable[[], datetime] = datetime.now):
        self.api = api
        self.broker = broker
        self.budget = budget
        self.take_profit_pct = take_profit_pct
        self.max_buys_per_day = max_buys_per_day
        self.clock = clock
        self._day: Optional[date] = None
        self.bought_today: Set[str] = set()
        self.pending: Dict[str, dict] = {}   # 買い注文の約定待ち

    def _roll_day(self):
        today = self.clock().date()
        if self._day != today:
            self._day, self.bought_today = today, set()

    def on_revision(self, revision: dict) -> bool:
        """上方修正を検出したときに呼ぶ。発注したら True"""
        self._roll_day()
        symbol = revision.get("symbol")
        if not symbol:
            return False
        if symbol in self.bought_today or symbol in self.pending:
            logger.info(f"[{symbol}] 本日は発注済みのためスキップします")
            return False
        if len(self.bought_today) >= self.max_buys_per_day:
            logger.info(f"[{symbol}] 1日の買い上限（{self.max_buys_per_day}銘柄）に達しているためスキップします")
            return False

        board = self.api.get_board(symbol) or {}
        info = self.api.get_symbol(symbol) or {}
        current = board.get("CurrentPrice")
        prev_close = board.get("PreviousClose")
        upper = info.get("UpperLimit") or (calc_upper_limit(prev_close) if prev_close else None)
        group = info.get("PriceRangeGroup")
        # 成行の買付余力は値幅上限で拘束される。上限が分からなければ現在値で計算する
        sizing_price = upper or current
        if not sizing_price:
            logger.error(f"[{symbol}] 価格情報を取得できないため発注をスキップします")
            return False
        qty = calculate_purchase_quantity(sizing_price, self.budget)
        if qty <= 0:
            logger.error(f"[{symbol}] 購入可能な株数が0株のため、発注をスキップします（値幅上限 {sizing_price}円）")
            return False

        logger.info(f"[{symbol}] 成行で {qty}株 買います（現在値 {current}円 / 値幅上限 {upper}円 で株数を計算）")
        order_id = self.broker.buy_market(symbol, qty)
        if not order_id:
            logger.error(f"[{symbol}] 発注に失敗しました")
            return False
        self.bought_today.add(symbol)
        self.pending[symbol] = {"order_id": order_id, "upper": upper, "group": group}
        return True

    def poll(self):
        """約定待ちの買い注文を確認し、約定したら利確の指値売りを出す"""
        for symbol, p in list(self.pending.items()):
            board = self.api.get_board(symbol)
            if board:
                self.broker.observe(symbol, board)
            status = self.broker.get_order(p["order_id"])
            if status is None or not status.finished:
                continue
            del self.pending[symbol]
            if status.cum_qty <= 0:
                logger.warning(f"[{symbol}] 買い注文が約定せずに終了しました")
                continue
            logger.info(f"[{symbol}] 約定 {status.cum_qty}株 @ {status.avg_price}")
            self._place_take_profit(symbol, status.cum_qty, status.avg_price, p["upper"], p["group"])

    def _place_take_profit(self, symbol: str, qty: int, avg_price: float,
                           upper: Optional[float], group: Optional[str]):
        if self.take_profit_pct <= 0:
            return
        tp = round_up_to_tick(avg_price * (1 + self.take_profit_pct / 100), group)
        if upper:
            tp = min(tp, round_down_to_tick(upper, group))
        if tp <= avg_price:
            logger.info(f"[{symbol}] 値幅上限で約定したため利確注文は出しません。手動で決済してください")
            return
        if self.broker.sell_limit(symbol, qty, tp):
            logger.info(f"[{symbol}] 利確の指値売りを出しました: {qty}株 @ {tp}（当日限り）")
        else:
            logger.error(f"[{symbol}] 利確注文の発注に失敗しました。手動で決済してください（{qty}株）")


def main():
    # 環境変数の読み込み
    api_password, available_margin, account_type, interval_seconds = load_environment_variables()
    # ペーパートレード（UR_DRY_RUN=true、デフォルト）では発注せず、約定を模擬してログに出すだけ
    dry_run = get_bool_env("UR_DRY_RUN", True)
    take_profit_pct = float(os.getenv("UR_TAKE_PROFIT_PCT", "20"))
    max_buys = int(os.getenv("UR_MAX_BUYS_PER_DAY", "1"))
    if dry_run:
        logger.info("ペーパートレードモードです（発注しません）。実発注するには UR_DRY_RUN=false を設定してください")
    else:
        logger.warning("実発注モードです。上方修正を検出すると成行で買い注文を出します")

    # トークンの取得
    api = KabuApi(get_token(api_password))
    broker = PaperBroker() if dry_run else KabuBroker(api, account_type)
    trader = UpwardRevisionTrader(api, broker, available_margin, take_profit_pct, max_buys)

    # トークン発効後にscraperを実行
    logger.info(f"上方修正監視を開始します（利確 +{take_profit_pct}% / 1日 {max_buys}銘柄まで）")
    scraper = NikkeiScraper()
    scraper.run_continuous_monitoring(
        interval_seconds=interval_seconds, on_detect=trader.on_revision, on_poll=trader.poll,
    )
    if trader.pending:
        logger.warning(f"約定待ちの注文があります: {list(trader.pending)}。手動で確認してください")
    logger.info("監視を停止しました")


if __name__ == "__main__":
    main()
