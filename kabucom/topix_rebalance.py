"""
A. TOPIX 移行措置銘柄の「段階的ウエイト低減」に伴う売られすぎの戻りを取る戦略

背景:
  TOPIX の見直し（第二段階）で、継続基準を満たさない「移行措置銘柄」は
  2026年10月から2028年7月まで、四半期ごと（1・4・7・10月の最終営業日）に8回に分けて
  元のウエイトを 12.5% ずつ下げられる。TOPIX 連動ファンドは実施日の引けで
  値段を気にせずに売るため、小型株では一時的に下がりすぎることがある。

流れ:
  1. 毎日引け後、対象銘柄の終値・出来高・売買代金を記録する（kabu API は過去の日足を返さないため）
  2. 実施日の TR_ENTRY_AT（既定 15:10）に、対象銘柄の板を確認し、次の条件の銘柄を引成で買う
       - 実施日までの TR_LOOKBACK_DAYS 営業日で TR_MIN_DROP_PCT% 以上下がっている
       - 平均売買代金が TR_MIN_AVG_VALUE 円以上（流動性が無さすぎる銘柄は避ける）
       - 銘柄リストに SellShares（想定売り株数）があれば、平均出来高の何日分か（インパクト日数）が
         TR_MIN_IMPACT_DAYS 以上
     インパクト日数が大きい順 → 下落率が大きい順に、上限まで買う
  3. TR_HOLD_DAYS 営業日後の寄りで売る（利確・損切り%も任意で指定可）

銘柄リスト（TR_UNIVERSE_FILE）:
  JPX が公表している「移行措置銘柄」の一覧を CSV にしたもの。列は Code,Name（必須は Code）。
  任意で SellShares（その回に TOPIX 連動ファンドが売る想定の株数）を入れると、売り圧力の大きさで選べる。
  例: Code,Name,SellShares
      1234,サンプル,150000

安全のため既定はペーパートレード。実発注は TR_DRY_RUN=false。

起動:
  cd kabucom
  python topix_rebalance.py            # 監視・売買（引け後の終値記録も自動）
  python topix_rebalance.py collect    # 終値の記録だけを今すぐ行う
  python topix_rebalance.py dates      # 実施日の一覧を表示
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from typing import List, Optional

from special_quote import _env_time
from swing import (
    Candidate, PriceHistory, SwingConfig, SwingStrategy, build_engine, collect_closes, is_special,
    iter_boards, last_market_day_of_month, load_universe, state_path,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

NAME = "topix_rebalance"


def default_event_dates(start: str = "2026-10", end: str = "2028-07") -> List[date]:
    """1・4・7・10月の最終営業日（start〜end の年月を含む）"""
    sy, sm = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    result = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        if m in (1, 4, 7, 10):
            result.append(last_market_day_of_month(y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return result


@dataclass
class TopixRebalanceConfig(SwingConfig):
    universe_file: str = ""
    event_dates: List[date] = field(default_factory=default_event_dates)
    entry_at: dt_time = dt_time(15, 10)
    lookback_days: int = 10          # 何営業日前の終値と比べて下落率を測るか
    min_drop_pct: float = 5.0        # これ以上下がった銘柄だけ買う
    max_drop_pct: float = 40.0       # 下がりすぎ（別の悪材料の可能性）は見送る
    min_avg_value: float = 20_000_000   # 20日平均売買代金（円）の下限
    min_impact_days: float = 0.0     # 想定売り株数 / 20日平均出来高 の下限（0 = 判定しない）

    @classmethod
    def from_env(cls) -> "TopixRebalanceConfig":
        common = SwingConfig.common_from_env("TR", {"hold_days": 3, "max_positions": 5,
                                                    "max_new_per_day": 5})
        dates_env = os.getenv("TR_EVENT_DATES", "").strip()
        dates = [date.fromisoformat(d.strip()) for d in dates_env.split(",") if d.strip()] \
            if dates_env else default_event_dates()
        return cls(
            **common,
            universe_file=os.getenv("TR_UNIVERSE_FILE",
                                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "topix_transition_list.csv")),
            event_dates=dates,
            entry_at=_env_time("TR_ENTRY_AT", "15:10"),
            lookback_days=int(os.getenv("TR_LOOKBACK_DAYS", "10")),
            min_drop_pct=float(os.getenv("TR_MIN_DROP_PCT", "5")),
            max_drop_pct=float(os.getenv("TR_MAX_DROP_PCT", "40")),
            min_avg_value=float(os.getenv("TR_MIN_AVG_VALUE", "20000000")),
            min_impact_days=float(os.getenv("TR_MIN_IMPACT_DAYS", "0")),
        )


class TopixRebalanceStrategy(SwingStrategy):
    name = NAME

    def __init__(self, api, config: TopixRebalanceConfig, universe: Optional[List[dict]] = None,
                 history: Optional[PriceHistory] = None):
        self.api = api
        self.config = config
        self.universe = universe if universe is not None else load_universe(config.universe_file)
        self.history = history or PriceHistory(state_path(config, NAME, "price_history.json"))

    def entry_slots(self):
        return [self.config.entry_at]

    def is_event_day(self, d: date) -> bool:
        return d in self.config.event_dates

    def select(self, now: datetime, slot) -> List[Candidate]:
        today = now.date()
        if not self.is_event_day(today):
            return []
        c = self.config
        logger.info(f"[{NAME}] 本日はウエイト低減の実施日です。{len(self.universe)} 銘柄を確認します")
        scored = []
        items = {i["Code"]: i for i in self.universe}
        for code, board in iter_boards(self.api, list(items)):
            item = items[code]
            current = board.get("CurrentPrice")
            if not current or is_special(board):
                continue
            ref = self.history.close_days_ago(code, c.lookback_days, today)
            if not ref:
                continue
            drop = (current / ref - 1) * 100
            if drop > -c.min_drop_pct or drop < -c.max_drop_pct:
                continue
            avg_value = self.history.avg_value(code, 20, today) or 0
            if c.min_avg_value and avg_value < c.min_avg_value:
                continue
            impact = None
            sell_shares = float(item.get("SellShares") or 0)
            avg_volume = self.history.avg_volume(code, 20, today)
            if sell_shares and avg_volume:
                impact = sell_shares / avg_volume
            if c.min_impact_days and (impact is None or impact < c.min_impact_days):
                continue
            note = f"{c.lookback_days}日で {drop:+.1f}%" + (f" / 売り {impact:.1f}日分" if impact else "")
            scored.append(((impact or 0, -drop), Candidate(code, item.get("Name", ""), "close", current,
                                                           note=note, score=impact or -drop)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [cand for _, cand in scored]

    def collect(self, now: datetime):
        collect_closes(self.api, self.history, [i["Code"] for i in self.universe], now.date())


def main():
    from kabu_token import get_token
    from special_quote import KabuApi

    config = TopixRebalanceConfig.from_env()
    if len(sys.argv) > 1 and sys.argv[1] == "dates":
        for d in config.event_dates:
            print(d.isoformat())
        return
    api = KabuApi(get_token(config.api_password))
    strategy = TopixRebalanceStrategy(api, config)
    if len(sys.argv) > 1 and sys.argv[1] == "collect":
        strategy.collect(datetime.now())
        return
    upcoming = [d for d in config.event_dates if d >= date.today()]
    logger.info(f"次の実施日: {upcoming[0] if upcoming else 'なし'} / 対象 {len(strategy.universe)} 銘柄")
    build_engine(strategy, api, config).run_forever()


if __name__ == "__main__":
    main()
