"""
3ロジックのバックテスト用シミュレーター

パラメータはライブと同じ設定クラス（SpecialQuoteConfig / StopHighConfig）を
kabucom/.env と環境変数から読むので、ライブで使う値でそのまま検証できる。
ライブに無い、バックテスト専用のパラメータは BT_* / UR_BT_* / SQ_BT_*。

再現の前提（ライブとの違い）は各関数の docstring と docs/バックテスト.md を参照。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from typing import List, Optional

from dotenv import load_dotenv

from price_limit import upper_limit
from special_quote import SpecialQuoteConfig, SpecialQuoteTrade, _env_time, in_session
from stop_high_rebound import DipWatcher, ReboundTrade, StopHighConfig
from tick_size import add_ticks, round_up_to_tick

from .data import CsvDataSource
from .sim import Exit, Trade, make_trade, price_path, simulate_exit

NO_FORCE_EXIT = dt_time(23, 59)
_KABUCOM_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kabucom")


@dataclass
class BacktestParams:
    """全ロジック共通＋①（上方修正）用のバックテスト専用パラメータ"""
    slippage_ticks: int = 1          # 成行の約定が何ティック不利になるか
    fee_rate: float = 0.0            # 売買代金に対する手数料率（片道）。0.001 = 0.1%
    ur_budget: float = 1_000_000     # ① の1トレード予算（ライブと同じ AVAILABLE_MARGIN）
    ur_take_profit_pct: float = 2.0  # ① の利確%（ライブには売りが無いのでバックテスト専用）
    ur_stop_loss_pct: float = 2.0    # ① の損切り%
    ur_detect_delay_min: int = 1     # ① 開示から検出・発注までの遅れ（分）
    ur_force_exit: dt_time = NO_FORCE_EXIT  # ① の強制決済時刻（既定: なし＝引けで売る）
    sq_min_open_delay_min: int = 1   # ② 9:00 から何分以上寄りが遅れたら特別気配だったとみなすか

    @classmethod
    def from_env(cls) -> "BacktestParams":
        g = os.getenv
        return cls(
            slippage_ticks=int(g("BT_SLIPPAGE_TICKS", "1")),
            fee_rate=float(g("BT_FEE_RATE", "0")),
            ur_budget=float(g("AVAILABLE_MARGIN", "1000000")),
            ur_take_profit_pct=float(g("UR_BT_TAKE_PROFIT_PCT", "2")),
            ur_stop_loss_pct=float(g("UR_BT_STOP_LOSS_PCT", "2")),
            ur_detect_delay_min=int(g("UR_BT_DETECT_DELAY_MIN", "1")),
            ur_force_exit=_env_time("UR_BT_FORCE_EXIT", "23:59"),
            sq_min_open_delay_min=int(g("SQ_BT_MIN_OPEN_DELAY_MIN", "1")),
        )


def load_params(overrides: Optional[dict] = None):
    """ライブ用の設定クラスを .env / 環境変数から読み、バックテスト用に返す"""
    os.environ.update(overrides or {})
    # 先に .env を読む（--set の値・既存の環境変数が優先。ダミー値で .env の値を潰さないように）
    env_path = os.path.join(_KABUCOM_DIR, ".env")
    if os.path.exists(env_path):
        load_dotenv(dotenv_path=env_path)
    # ライブの from_env は API パスワード等を必須にしているので、未設定ならダミーを入れる
    os.environ.setdefault("KABUCOM_API_PASSWORD", "backtest")
    os.environ.setdefault("KABUCOM_ACCOUNT_TYPE", "4")
    os.environ.setdefault("AVAILABLE_MARGIN", "1000000")
    sq = SpecialQuoteConfig.from_env()
    shr = StopHighConfig.from_env()
    return BacktestParams.from_env(), sq, shr


def _in_scraper_session(t: datetime) -> bool:
    """main.py（scraper.py）が開示を監視している時間: 9:00-11:30 / 12:30-15:30"""
    return in_session(t)


# ---------------------------------------------------------------------------
# ① 上方修正
# ---------------------------------------------------------------------------

def backtest_upward_revision(ds: CsvDataSource, days: List[date], p: BacktestParams) -> List[Trade]:
    """
    - 対象: 取引時間中（9:00-11:30 / 12:30-15:30）に出た上方修正の開示
      （ライブは取引時間外の開示を「処理済み」として読み飛ばすため、時間外の開示は対象外）
    - 買い: 開示から ur_detect_delay_min 分後の最初の分足の始値に、成行のスリッページを足した価格
    - 株数: ライブと同じく値幅上限（前日終値から計算）の値段で計算する（成行の余力拘束）
    - 売り: % 指定の利確（指値）・損切り（成行）。どちらも無ければ引けで売る
      （ライブは UR_TAKE_PROFIT_PCT の利確指値だけで、損切りはしない）
    """
    day_set = set(days)
    prev_day = dict(zip(days[1:], days))
    trades = []
    for rev in ds.revisions():
        if not rev.upward or rev.disclosed_at.date() not in day_set or not _in_scraper_session(rev.disclosed_at):
            continue
        day = rev.disclosed_at.date()
        bars = ds.minute(day, rev.code)
        start = rev.disclosed_at + timedelta(minutes=p.ur_detect_delay_min)
        bars = [b for b in bars if b.time >= start]
        if not bars:
            continue
        group = ds.price_range_group(rev.code)
        entry_price = add_ticks(bars[0].open, p.slippage_ticks, group)
        tp = round_up_to_tick(entry_price * (1 + p.ur_take_profit_pct / 100), group)
        sl = entry_price * (1 - p.ur_stop_loss_pct / 100)
        path = price_path(bars)
        next(path)  # 始値で買ったので、その直後から決済を判定
        exit_ = simulate_exit(path, tp, sl, p.ur_force_exit, p.slippage_ticks, group)
        if exit_ is None:  # 買った分足が引けの1本だけだった
            exit_ = Exit(bars[-1].time, bars[-1].close, "引け")
        prev_bar = ds.daily(prev_day[day]).get(rev.code) if day in prev_day else None
        sizing = upper_limit(prev_bar.close) if prev_bar else None
        trade = make_trade("①上方修正", rev.code, ds.name(rev.code), (bars[0].time, entry_price),
                           p.ur_budget, exit_, p.fee_rate, note=rev.title, sizing_price=sizing)
        if trade:
            trades.append(trade)
    return trades


# ---------------------------------------------------------------------------
# ② 特別買い気配スキャルピング（近似）
# ---------------------------------------------------------------------------

def backtest_special_quote(ds: CsvDataSource, days: List[date], p: BacktestParams,
                           sq: SpecialQuoteConfig) -> List[Trade]:
    """
    過去の気配データが無いため、次のように近似する（結果は楽観的な上限値）:
    - 「前日終値より高く、かつ 9:00 から sq_min_open_delay_min 分以上遅れて寄った銘柄」を
      寄り前に特別買い気配だった銘柄とみなす
    - ライブはランキング上位 SQ_RANKING_TOP_N 銘柄だけを見るので、前日比の上昇率で上位 N 銘柄に絞る
    - 寄り付き（始値）で全量約定したと仮定する（ライブの上値指値モード SQ_ENTRY_MODE=cap に近い。
      追随モード chase では時間優先で約定しないことが多いので、さらに楽観的になる）
    - 伸びる余地の判定（SQ_MIN_ROOM_PCT / SQ_MAX_GAP_PCT）は、検知時の気配の代わりに始値で行う。
      始値は検知時の気配以上なので、ライブより厳しめ（見送りが多め）になる
    - 上値指値の上限（SQ_MAX_CHASE_PCT）・追随回数（SQ_MAX_REORDERS）は気配データが無いため再現しない
    - 決済はライブと同じ: 約定単価 + 利確ティックの指値、- 損切りティックで成行、強制決済
    """
    trades = []
    for prev_day, day in zip(days, days[1:]):
        prev, today = ds.daily(prev_day), ds.daily(day)
        gaps = []
        for code, bar in today.items():
            base = prev.get(code)
            if base and bar.open > base.close:
                if bar.open < sq.min_price or (sq.max_price and bar.open > sq.max_price):
                    continue
                if sq.max_gap_pct > 0 and (bar.open / base.close - 1) * 100 > sq.max_gap_pct:
                    continue
                if sq.min_room_pct > 0 and \
                        (upper_limit(base.close) / bar.open - 1) * 100 < sq.min_room_pct:
                    continue
                gaps.append((bar.open / base.close - 1, code))
        gaps.sort(reverse=True)

        candidates = []
        for gap, code in gaps:
            bars = ds.minute(day, code)
            if not bars:
                continue
            delay = (bars[0].time - datetime.combine(day, dt_time(9, 0))).total_seconds() / 60
            if delay >= p.sq_min_open_delay_min:
                candidates.append((code, bars, gap))
            if len(candidates) >= sq.ranking_top_n:
                break

        for code, bars, gap in candidates:
            group = ds.price_range_group(code)
            t = SpecialQuoteTrade(code, ds.name(code), group, 0, None, sq)
            t.avg_price = bars[0].open
            tp, sl = t._exit_targets()
            path = price_path(bars)
            next(path)
            exit_ = simulate_exit(path, tp, sl, sq.force_exit, p.slippage_ticks, group)
            if exit_ is None:
                continue
            trade = make_trade("②特別買い気配", code, ds.name(code), (bars[0].time, bars[0].open),
                               sq.budget_per_trade, exit_, p.fee_rate, note=f"寄り {gap * 100:+.1f}%")
            if trade:
                trades.append(trade)
    return trades


# ---------------------------------------------------------------------------
# ③ 前日ストップ高の押し目買い
# ---------------------------------------------------------------------------

def find_stop_high(ds: CsvDataSource, base_day: date, day: date) -> List[str]:
    """day に終値がストップ高（基準値段 = base_day の終値）だった銘柄"""
    base, today = ds.daily(base_day), ds.daily(day)
    return [code for code, bar in today.items()
            if code in base and bar.close >= upper_limit(base[code].close)]


def backtest_stop_high(ds: CsvDataSource, days: List[date], p: BacktestParams,
                       shr: StopHighConfig) -> List[Trade]:
    """
    - 前日の終値がストップ高だった銘柄を対象にする（ライブの引け後収集と同じ判定。
      ただしライブはランキング上位50銘柄までしか見ない）
    - 寄り後の値動きをライブと同じ DipWatcher に1点ずつ流し、買いシグナルの価格に
      スリッページを足した価格で約定したとみなす（ライブは最良売気配で指値）
    - 決済はライブと同じ ReboundTrade の利確・損切り価格と強制決済時刻
    """
    trades = []
    for i in range(2, len(days)):
        prevprev, prev, day = days[i - 2], days[i - 1], days[i]
        prev_bars = ds.daily(prev)
        for code in find_stop_high(ds, prevprev, prev):
            bars = ds.minute(day, code)
            if not bars:
                continue
            group = ds.price_range_group(code)
            watcher = DipWatcher(code, ds.name(code), prev_bars[code].close, group, shr)
            path = price_path(bars)
            entry = None
            for t, price in path:
                if t.time() < shr.entry_start:
                    continue
                if t.time() >= shr.entry_end:
                    break
                board = {"CurrentPrice": price, "OpeningPrice": bars[0].open,
                         "PreviousClose": prev_bars[code].close, "AskSign": "0101", "BidSign": "0101"}
                if watcher.update(board):
                    entry = (t, add_ticks(price, p.slippage_ticks, group))
                    break
            if entry is None:
                continue
            if entry[1] < shr.min_price or (shr.max_price and entry[1] > shr.max_price):
                continue
            rt = ReboundTrade(code, ds.name(code), group, 0, None, shr)
            rt.avg_price = entry[1]
            tp, sl = rt._exit_targets()
            exit_ = simulate_exit(path, tp, sl, shr.force_exit, p.slippage_ticks, group)
            if exit_ is None:
                continue
            note = f"基準 {watcher.peak} → 安値 {watcher.trough}"
            trade = make_trade("③ストップ高押し目", code, ds.name(code), entry, shr.budget_per_trade,
                               exit_, p.fee_rate, note=note)
            if trade:
                trades.append(trade)
    return trades
