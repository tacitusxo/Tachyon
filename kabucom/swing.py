"""
複数日保有（スイング）戦略の共通部品

  - 引成（引けで買う）・寄成（寄りで売る）・成行の注文（現物）
  - 保有ポジションをファイルに保存する（再起動しても続きから管理できる）
  - 終値の履歴をファイルに貯める（kabu API は過去の日足を返さないため、毎日引け後に板から記録する）
  - ペーパートレード用の約定模擬（注文もファイルに保存する）
  - 戦略を差し替えて動かすエンジン（SwingEngine）

各戦略（topix_rebalance.py / disclosure_score.py / no_news_reversal.py）は
SwingStrategy を実装し、SwingEngine に渡して動かす。
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta, time as dt_time
from typing import Callable, Dict, List, Optional, Set

from dotenv import load_dotenv

from special_quote import (
    SIGN_SPECIAL_QUOTE, KabuBroker, OrderStatus, _env_bool, _env_time, in_session, is_market_day,
)
from utils import calculate_purchase_quantity

logger = logging.getLogger(__name__)

KABUCOM_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.join(KABUCOM_DIR, "state")

CLOSE_TIME = dt_time(15, 30)          # 大引け（クロージング・オークション終了）
OPEN_TIME = dt_time(9, 0)
MORNING_END = dt_time(11, 30)
CLOSE_ORDER_DEADLINE = dt_time(15, 24)  # 引成を出す締切（余裕をみてクロージング・オークション前）


# ---------------------------------------------------------------------------
# 日付
# ---------------------------------------------------------------------------

def add_market_days(d: date, n: int) -> date:
    """d から n 営業日後（n=0 なら d）"""
    while n > 0:
        d += timedelta(days=1)
        if is_market_day(d):
            n -= 1
    return d


def previous_market_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_market_day(d):
        d -= timedelta(days=1)
    return d


def last_market_day_of_month(year: int, month: int) -> date:
    d = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    while not is_market_day(d):
        d -= timedelta(days=1)
    return d


def is_special(board: dict) -> bool:
    return SIGN_SPECIAL_QUOTE in (board.get("AskSign"), board.get("BidSign"))


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write_json(path: str, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_universe(path: str) -> List[dict]:
    """銘柄リスト CSV（Code 列必須、Name ほか任意）を読む"""
    import csv
    if not path or not os.path.exists(path):
        raise SystemExit(f"Error: 銘柄リストのファイルがありません: {path}")
    with open(path, encoding="utf-8-sig") as f:
        rows = [dict(r) for r in csv.DictReader(f)]
    for r in rows:
        r["Code"] = str(r.get("Code") or r.get("コード") or "").strip()
        r.setdefault("Name", r.get("銘柄名", ""))
    return [r for r in rows if r["Code"]]


# ---------------------------------------------------------------------------
# 終値の履歴
# ---------------------------------------------------------------------------

class PriceHistory:
    """{symbol: [[日付, 終値, 出来高, 売買代金], ...]} を JSON に保存する"""

    def __init__(self, path: str, keep: int = 80):
        self.path = path
        self.keep = keep
        self.data: Dict[str, list] = _read_json(path, {})

    def record(self, symbol: str, day: date, close: float, volume: float = 0, value: float = 0):
        rows = [r for r in self.data.get(symbol, []) if r[0] != day.isoformat()]
        rows.append([day.isoformat(), close, volume or 0, value or 0])
        rows.sort(key=lambda r: r[0])
        self.data[symbol] = rows[-self.keep:]

    def record_board(self, symbol: str, day: date, board: Optional[dict]) -> bool:
        """引け後の板から当日の終値を記録する。当日に約定が無い銘柄は記録しない"""
        if not board or board.get("CurrentPrice") is None:
            return False
        ts = board.get("CurrentPriceTime") or ""
        if ts and not ts.startswith(day.isoformat()):
            return False
        self.record(symbol, day, board["CurrentPrice"], board.get("TradingVolume") or 0,
                    board.get("TradingValue") or 0)
        return True

    def save(self):
        _write_json(self.path, self.data)

    def _before(self, symbol: str, day: date) -> list:
        return [r for r in self.data.get(symbol, []) if r[0] < day.isoformat()]

    def close_days_ago(self, symbol: str, n: int, day: date) -> Optional[float]:
        """day より前の、記録上 n 本前の終値（n=1 で前日終値）"""
        rows = self._before(symbol, day)
        return rows[-n][1] if len(rows) >= n else None

    def avg(self, symbol: str, n: int, day: date, column: int) -> Optional[float]:
        rows = self._before(symbol, day)[-n:]
        if not rows:
            return None
        return sum(r[column] for r in rows) / len(rows)

    def avg_volume(self, symbol: str, n: int, day: date) -> Optional[float]:
        return self.avg(symbol, n, day, 2)

    def avg_value(self, symbol: str, n: int, day: date) -> Optional[float]:
        return self.avg(symbol, n, day, 3)


def iter_boards(api, symbols: List[str]):
    """
    多数の銘柄の板を順に取る。kabu ステーションは登録銘柄数に上限（50）があり、
    板の取得で銘柄が登録されると上限に達することがあるため、KABU_UNREGISTER_EVERY 銘柄ごとに
    登録を全解除する（0 で無効。PUSH 配信を併用している場合は 0 にすること）
    """
    every = int(os.getenv("KABU_UNREGISTER_EVERY", "40"))
    for i, symbol in enumerate(symbols):
        if every and i and i % every == 0 and hasattr(api, "request"):
            api.request("PUT", "/unregister/all", body={})
        yield symbol, api.get_board(symbol) or {}


def collect_closes(api, history: PriceHistory, symbols: List[str], day: date) -> int:
    count = sum(1 for s, b in iter_boards(api, symbols) if history.record_board(s, day, b))
    history.save()
    logger.info(f"{day} の終値を {count}/{len(symbols)} 銘柄記録しました")
    return count


# ---------------------------------------------------------------------------
# ブローカー
# ---------------------------------------------------------------------------

# 現物株の執行条件（kabu API の FrontOrderType）
FRONT_ORDER_TYPE = {
    "market": 10,  # 成行
    "open": 13,    # 寄成（前場）
    "close": 16,   # 引成（後場）
}


class SwingKabuBroker(KabuBroker):
    """kabu API で実発注する（現物）。取消・注文照会は KabuBroker のものを使う"""

    def order(self, side: str, symbol: str, qty: int, kind: str, now: datetime) -> Optional[str]:
        body = {
            "Symbol": symbol, "Exchange": 1, "SecurityType": 1,
            "CashMargin": 1, "AccountType": self.account_type, "Qty": qty,
            "FrontOrderType": FRONT_ORDER_TYPE[kind], "Price": 0, "ExpireDay": 0,
        }
        if side == "buy":
            body.update({"Side": "2", "DelivType": 2, "FundType": "02"})
        else:
            body.update({"Side": "1", "DelivType": 0, "FundType": "  "})
        return self._send(body)

    def observe(self, symbol: str, board: dict, now: Optional[datetime] = None):
        """実ブローカーでは不要"""


class PaperSwingBroker:
    """
    発注せずに板情報から約定を模擬する。注文はファイルに保存するので、再起動しても続く。
      - 成行: 特別気配でなければ現在値で約定
      - 寄成: 発注日の寄り付き（OpeningPrice）で約定。前場のうちに寄らなければ失効
      - 引成: 発注日の 15:30 以降に見た現在値（＝終値）で約定。
              翌営業日以降に初めて見た場合は前日終値（PreviousClose）で約定
    """

    def __init__(self, path: str):
        self.path = path
        self.orders: Dict[str, dict] = _read_json(path, {})
        start = max((int(k.split("-")[-1]) for k in self.orders), default=0) + 1
        self._ids = itertools.count(start)

    def _save(self):
        _write_json(self.path, self.orders)

    def order(self, side, symbol, qty, kind, now) -> str:
        order_id = f"PAPER-{next(self._ids)}"
        self.orders[order_id] = {"symbol": symbol, "side": side, "qty": qty, "kind": kind,
                                 "day": now.date().isoformat(), "cum_qty": 0, "avg_price": 0.0,
                                 "finished": False}
        self._save()
        logger.info(f"[DRY RUN] 発注: {symbol} {side} {qty}株 {kind} ({order_id})")
        return order_id

    def cancel(self, order_id) -> bool:
        o = self.orders.get(order_id)
        if not o or o["finished"]:
            return False
        o["finished"] = True
        self._save()
        logger.info(f"[DRY RUN] 取消: {order_id}")
        return True

    def get_order(self, order_id) -> Optional[OrderStatus]:
        o = self.orders.get(order_id)
        if not o:
            return None
        return OrderStatus(order_id, o["finished"], o["cum_qty"], o["avg_price"])

    def observe(self, symbol: str, board: dict, now: datetime):
        changed = False
        for o in self.orders.values():
            if o["finished"] or o["symbol"] != symbol or not board:
                continue
            day = date.fromisoformat(o["day"])
            price = None
            expire = False
            if o["kind"] == "market":
                if not is_special(board) and board.get("CurrentPrice"):
                    price = board["CurrentPrice"]
            elif o["kind"] == "open":
                if now.date() == day and board.get("OpeningPrice"):
                    price = board["OpeningPrice"]
                elif now.date() > day or now.time() >= MORNING_END:
                    expire = True
            elif o["kind"] == "close":
                if now.date() == day and now.time() >= CLOSE_TIME and board.get("CurrentPrice"):
                    price = board["CurrentPrice"]
                elif now.date() > day:
                    if previous_market_day(now.date()) == day and board.get("PreviousClose"):
                        price = board["PreviousClose"]
                    else:
                        expire = True
            if price is not None:
                o.update(cum_qty=o["qty"], avg_price=price, finished=True)
                changed = True
            elif expire:
                o["finished"] = True
                changed = True
                logger.info(f"[DRY RUN] {symbol} の {o['kind']} 注文は約定せずに失効しました")
        if changed:
            self._save()


# ---------------------------------------------------------------------------
# ポジション
# ---------------------------------------------------------------------------

ENTRY_PENDING = "entry_pending"
HOLDING = "holding"
EXIT_PENDING = "exit_pending"
CLOSED = "closed"


@dataclass
class SwingPosition:
    symbol: str
    name: str
    qty: int
    entry_date: str
    exit_date: str                 # この日以降に決済する予定日
    status: str = ENTRY_PENDING
    entry_order_id: Optional[str] = None
    entry_price: float = 0.0
    exit_order_id: Optional[str] = None
    exit_kind: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    closed_date: str = ""
    pnl: float = 0.0
    note: str = ""


class SwingBook:
    """ポジションを JSON に保存する"""

    def __init__(self, path: str):
        self.path = path
        names = {f.name for f in fields(SwingPosition)}
        self.positions: List[SwingPosition] = [
            SwingPosition(**{k: v for k, v in p.items() if k in names}) for p in _read_json(path, [])
        ]

    def save(self):
        _write_json(self.path, [asdict(p) for p in self.positions])

    def open_positions(self) -> List[SwingPosition]:
        return [p for p in self.positions if p.status != CLOSED]

    def add(self, position: SwingPosition):
        self.positions.append(position)
        self.save()


# ---------------------------------------------------------------------------
# 設定・戦略・エンジン
# ---------------------------------------------------------------------------

@dataclass
class SwingConfig:
    api_password: str
    account_type: int
    budget_per_trade: float
    dry_run: bool = True
    state_dir: str = DEFAULT_STATE_DIR
    max_positions: int = 5             # 同時に保有する銘柄数
    max_new_per_day: int = 3           # 1日に新規で買う銘柄数
    hold_days: int = 3                 # 何営業日後に決済するか
    take_profit_pct: float = 0.0       # 利確%（0 = 使わない）。場中に現在値で判定して成行
    stop_loss_pct: float = 0.0         # 損切り%（0 = 使わない）
    exit_at: dt_time = dt_time(8, 50)  # 決済日のこの時刻に寄成で売る（場中に起動した場合は成行）
    collect_at: dt_time = dt_time(15, 35)  # 引け後の処理（終値の記録など）
    poll_interval_sec: float = 30.0
    min_price: float = 0
    max_price: float = 0               # 0 = 上限なし

    @staticmethod
    def common_from_env(prefix: str, defaults: Optional[dict] = None) -> dict:
        """共通の設定を環境変数 {prefix}_* から読む。defaults で戦略ごとの既定値を変えられる"""
        env_path = os.path.join(KABUCOM_DIR, ".env")
        if os.path.exists(env_path):
            load_dotenv(dotenv_path=env_path)
        d = {
            "max_positions": 5, "max_new_per_day": 3, "hold_days": 3,
            "take_profit_pct": 0.0, "stop_loss_pct": 0.0, "exit_at": "8:50",
            "collect_at": "15:35", "poll_interval_sec": 30.0,
        }
        d.update(defaults or {})
        g = lambda name, default: os.getenv(f"{prefix}_{name}", str(default))  # noqa: E731

        api_password = os.getenv("KABUCOM_API_PASSWORD")
        if not api_password:
            raise SystemExit("Error: KABUCOM_API_PASSWORD environment variable is not set")
        account_type = os.getenv("KABUCOM_ACCOUNT_TYPE")
        if not account_type:
            raise SystemExit("Error: KABUCOM_ACCOUNT_TYPE environment variable is not set")
        budget = os.getenv(f"{prefix}_BUDGET_PER_TRADE") or os.getenv("AVAILABLE_MARGIN")
        if not budget or float(budget) <= 0:
            raise SystemExit(f"Error: {prefix}_BUDGET_PER_TRADE (or AVAILABLE_MARGIN) is not set or invalid")
        return dict(
            api_password=api_password,
            account_type=int(account_type),
            budget_per_trade=float(budget),
            dry_run=_env_bool(f"{prefix}_DRY_RUN", True),
            state_dir=os.getenv(f"{prefix}_STATE_DIR", DEFAULT_STATE_DIR),
            max_positions=int(g("MAX_POSITIONS", d["max_positions"])),
            max_new_per_day=int(g("MAX_NEW_PER_DAY", d["max_new_per_day"])),
            hold_days=int(g("HOLD_DAYS", d["hold_days"])),
            take_profit_pct=float(g("TAKE_PROFIT_PCT", d["take_profit_pct"])),
            stop_loss_pct=float(g("STOP_LOSS_PCT", d["stop_loss_pct"])),
            exit_at=_env_time(f"{prefix}_EXIT_AT", d["exit_at"]),
            collect_at=_env_time(f"{prefix}_COLLECT_AT", d["collect_at"]),
            poll_interval_sec=float(g("POLL_INTERVAL_SEC", d["poll_interval_sec"])),
            min_price=float(g("MIN_PRICE", 0)),
            max_price=float(g("MAX_PRICE", 0)),
        )


@dataclass
class Candidate:
    symbol: str
    name: str
    kind: str             # "close"（引成） / "open"（寄成） / "market"（成行）
    ref_price: float      # 株数の計算に使う価格
    note: str = ""
    score: float = 0.0


class SwingStrategy:
    """戦略のインターフェース"""
    name = "swing"

    def entry_slots(self) -> List[dt_time]:
        """select() を呼ぶ時刻（1日1回ずつ）"""
        return []

    def select(self, now: datetime, slot: dt_time) -> List[Candidate]:
        """買う候補を優先度の高い順に返す"""
        return []

    def collect(self, now: datetime):
        """引け後の処理（終値の記録など）"""


class SwingEngine:
    # 各スロットはこの時間を過ぎたら当日は実行しない（再起動時に古いスロットを実行しないため）
    SLOT_GRACE = timedelta(minutes=20)

    def __init__(self, strategy: SwingStrategy, api, broker, book: SwingBook, config: SwingConfig,
                 clock: Callable[[], datetime] = datetime.now):
        self.strategy = strategy
        self.api = api
        self.broker = broker
        self.book = book
        self.config = config
        self.clock = clock
        self._day: Optional[date] = None
        self._done_slots: Set[dt_time] = set()
        self._collected = False
        self._last_watch: Optional[datetime] = None

    # --- ループ ---------------------------------------------------------------

    def tick(self):
        now = self.clock()
        if self._day != now.date():
            self._day, self._done_slots, self._collected = now.date(), set(), False
        self._update_orders(now)
        if not is_market_day(now):
            return
        self._exits(now)
        for slot in self.strategy.entry_slots():
            start = datetime.combine(now.date(), slot)
            if slot in self._done_slots or now < start:
                continue
            self._done_slots.add(slot)
            if now - start <= self.SLOT_GRACE:
                self._enter(now, slot)
            else:
                logger.info(f"[{self.strategy.name}] {slot.strftime('%H:%M')} の処理時刻を過ぎているため今日は見送ります")
        if not self._collected and now.time() >= self.config.collect_at:
            self._collected = True
            self.strategy.collect(now)

    def run_forever(self):
        mode = "ペーパートレード（発注しません）" if self.config.dry_run else "実発注"
        logger.info(f"[{self.strategy.name}] 開始します（{mode}）。保有 {len(self.book.open_positions())} 件")
        while True:
            try:
                self.tick()
                time.sleep(self.config.poll_interval_sec)
            except KeyboardInterrupt:
                logger.info("停止しました（保有ポジションはファイルに保存済みです。再起動すると続きから管理します）")
                return
            except Exception as e:
                logger.exception(f"エラーが発生しました: {e}")
                time.sleep(10)

    # --- 注文の状態 -----------------------------------------------------------

    def _update_orders(self, now: datetime):
        changed = False
        for p in self.book.open_positions():
            order_id = p.entry_order_id if p.status == ENTRY_PENDING else \
                p.exit_order_id if p.status == EXIT_PENDING else None
            if not order_id:
                continue
            self.broker.observe(p.symbol, self.api.get_board(p.symbol) or {}, now)
            status = self.broker.get_order(order_id)
            if status is None or not status.finished:
                continue
            changed = True
            if p.status == ENTRY_PENDING:
                if status.cum_qty > 0:
                    p.qty, p.entry_price, p.status = status.cum_qty, status.avg_price, HOLDING
                    self._info(p, f"買い約定 {p.qty}株 @ {p.entry_price}（{p.exit_date} に決済予定）")
                else:
                    p.status, p.exit_reason, p.closed_date = CLOSED, "買い未約定", now.date().isoformat()
                    self._info(p, "買い注文が約定せずに終了しました")
            else:
                if status.cum_qty >= p.qty:
                    p.exit_price, p.status = status.avg_price, CLOSED
                    p.closed_date = now.date().isoformat()
                    p.pnl = (p.exit_price - p.entry_price) * p.qty
                    self._info(p, f"{p.exit_reason}で決済 @ {p.exit_price}。概算損益 {p.pnl:+,.0f}円（手数料除く）")
                else:
                    if status.cum_qty > 0:
                        # 一部だけ約定した分は損益に足し、残りを保有として扱い直す
                        p.pnl += (status.avg_price - p.entry_price) * status.cum_qty
                        p.qty -= status.cum_qty
                    p.status, p.exit_order_id = HOLDING, None
                    logger.warning(f"[{p.symbol}] 売り注文が全量約定しませんでした（残り {p.qty}株）。再度決済します")
        if changed:
            self.book.save()

    # --- 決済 -----------------------------------------------------------------

    def _exits(self, now: datetime):
        today = now.date().isoformat()
        session = in_session(now)
        watch_due = self._last_watch is None or \
            (now - self._last_watch).total_seconds() >= self.config.poll_interval_sec
        for p in self.book.open_positions():
            if p.status != HOLDING or p.entry_date == today:
                continue
            reason = kind = None
            if today >= p.exit_date and now.time() >= self.config.exit_at:
                reason = "保有期間満了"
                kind = "open" if now.time() < OPEN_TIME else "market" if session else None
            elif session and watch_due and (self.config.take_profit_pct or self.config.stop_loss_pct):
                board = self.api.get_board(p.symbol) or {}
                current = board.get("CurrentPrice")
                if current and self.config.stop_loss_pct and \
                        current <= p.entry_price * (1 - self.config.stop_loss_pct / 100):
                    reason, kind = f"損切り（現在値 {current}）", "market"
                elif current and self.config.take_profit_pct and \
                        current >= p.entry_price * (1 + self.config.take_profit_pct / 100):
                    reason, kind = f"利確（現在値 {current}）", "market"
            if not kind:
                continue
            order_id = self.broker.order("sell", p.symbol, p.qty, kind, now)
            if order_id:
                p.status, p.exit_order_id, p.exit_kind, p.exit_reason = EXIT_PENDING, order_id, kind, reason
                self._info(p, f"{reason}: {'寄成' if kind == 'open' else '成行'}で売ります")
            else:
                logger.error(f"[{p.symbol}] 売り注文に失敗しました。次の確認時に再度出します")
            self.book.save()
        if session and watch_due:
            self._last_watch = now

    # --- 新規 -----------------------------------------------------------------

    def _enter(self, now: datetime, slot: dt_time):
        today = now.date().isoformat()
        open_positions = self.book.open_positions()
        # 当日に売買した銘柄・保有中の銘柄は買わない（現物の差金決済を避ける）
        blocked = {p.symbol for p in open_positions} | \
            {p.symbol for p in self.book.positions if p.closed_date == today or p.entry_date == today}
        entered_today = sum(1 for p in self.book.positions if p.entry_date == today)
        room = min(self.config.max_positions - len(open_positions),
                   self.config.max_new_per_day - entered_today)
        if room <= 0:
            logger.info(f"[{self.strategy.name}] 保有数・当日の新規数の上限に達しているため新規は見送ります")
            return
        candidates = self.strategy.select(now, slot)
        if not candidates:
            logger.info(f"[{self.strategy.name}] {slot.strftime('%H:%M')} 買い候補なし")
            return
        for c in candidates:
            if room <= 0:
                break
            if c.symbol in blocked:
                continue
            if c.ref_price < self.config.min_price or \
                    (self.config.max_price and c.ref_price > self.config.max_price):
                continue
            if c.kind == "close" and now.time() > CLOSE_ORDER_DEADLINE:
                logger.warning(f"[{c.symbol}] 引成の締切（{CLOSE_ORDER_DEADLINE}）を過ぎたため見送ります")
                continue
            qty = calculate_purchase_quantity(c.ref_price, self.config.budget_per_trade)
            if qty <= 0:
                logger.info(f"[{c.symbol}] 予算内で購入できないためスキップ（{c.ref_price}円）")
                continue
            order_id = self.broker.order("buy", c.symbol, qty, c.kind, now)
            if not order_id:
                continue
            exit_day = add_market_days(now.date(), max(self.config.hold_days, 1))
            self.book.add(SwingPosition(
                symbol=c.symbol, name=c.name, qty=qty, entry_date=today,
                exit_date=exit_day.isoformat(), entry_order_id=order_id, note=c.note,
            ))
            blocked.add(c.symbol)
            room -= 1
            kind = {"close": "引成", "open": "寄成", "market": "成行"}[c.kind]
            logger.info(f"[{c.symbol} {c.name}] {kind}で買い {qty}株（目安 {c.ref_price}円）{c.note}")

    def _info(self, p: SwingPosition, message: str):
        logger.info(f"[{p.symbol} {p.name}] {message}")


def build_engine(strategy: SwingStrategy, api, config: SwingConfig, clock=datetime.now) -> SwingEngine:
    """設定に応じてペーパー / 実発注のブローカーとポジションファイルを用意する"""
    folder = os.path.join(config.state_dir, strategy.name)
    suffix = "paper" if config.dry_run else "live"
    broker = PaperSwingBroker(os.path.join(folder, "paper_orders.json")) if config.dry_run \
        else SwingKabuBroker(api, config.account_type)
    book = SwingBook(os.path.join(folder, f"positions_{suffix}.json"))
    return SwingEngine(strategy, api, broker, book, config, clock)


def state_path(config: SwingConfig, strategy_name: str, filename: str) -> str:
    return os.path.join(config.state_dir, strategy_name, filename)


__all__ = [
    "Candidate", "PaperSwingBroker", "PriceHistory", "SwingBook", "SwingConfig", "SwingEngine",
    "SwingKabuBroker", "SwingPosition", "SwingStrategy", "add_market_days", "build_engine",
    "collect_closes", "is_special", "iter_boards", "last_market_day_of_month", "load_universe",
    "previous_market_day", "state_path",
]
