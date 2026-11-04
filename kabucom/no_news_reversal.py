"""
C. 材料なしで下がった銘柄の短期の戻りを取る戦略

なぜ: 日本株は「直近で下がった銘柄が戻る（短期リバーサル）」効果が比較的強く、
     「上がった銘柄が上がり続ける（モメンタム）」効果は弱いと報告されている。
     同じ下落でも、開示（材料）が無い下落は需給の一時的な歪みなので戻りやすく、
     悪材料のある下落はさらに下がりやすい。そこで適時開示が無い下落銘柄だけを買う。

流れ:
  1. 毎日引け後、対象銘柄（CR_UNIVERSE_FILE）の終値・出来高・売買代金を記録する
  2. 買う曜日（CR_WEEKDAYS、既定 金曜）の CR_ENTRY_AT（既定 15:10）に、
       - 直近 CR_LOOKBACK_DAYS 営業日の騰落率が -CR_MIN_DROP_PCT% 以下（-CR_MAX_DROP_PCT% より下は除外）
       - 20日平均売買代金が CR_MIN_AVG_VALUE 円以上
       - その期間に、その銘柄の適時開示が無い（CR_EXCLUDE_ANY_DISCLOSURE=false なら悪い開示だけ除外）
     の銘柄を、下落率の大きい順に引成で買う
  3. CR_HOLD_DAYS 営業日後の寄りで売る（損切り・利確%も指定可）

開示データは B（disclosure_score.py）と同じ state/disclosures/ を使う。
CR_REQUIRE_NEWS_DATA=true（既定）では、期間内の開示データが揃っていない日は買わない
（悪材料の下落を誤って買わないため）。データは disclosure_score.py を動かすか、
このスクリプトの fetch サブコマンドで取得する。

安全のため既定はペーパートレード。実発注は CR_DRY_RUN=false。

起動:
  cd kabucom
  python no_news_reversal.py            # 監視・売買（引け後の終値記録・開示取得も自動）
  python no_news_reversal.py collect    # 終値の記録だけを今すぐ行う
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta
from typing import List, Optional

from disclosures import DEFAULT_STORE_DIR, DisclosureSource, DisclosureStore, source_from_env
from special_quote import _env_bool, _env_time, is_market_day
from swing import (
    Candidate, PriceHistory, SwingConfig, SwingStrategy, build_engine, collect_closes, is_special,
    iter_boards, load_universe, previous_market_day, state_path,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

NAME = "no_news_reversal"


@dataclass
class NoNewsReversalConfig(SwingConfig):
    universe_file: str = ""
    store_dir: str = DEFAULT_STORE_DIR
    weekdays: List[int] = field(default_factory=lambda: [4])   # 0=月 … 4=金
    entry_at: dt_time = dt_time(15, 10)
    lookback_days: int = 5
    min_drop_pct: float = 8.0
    max_drop_pct: float = 30.0
    min_avg_value: float = 100_000_000
    exclude_any_disclosure: bool = True
    block_score: float = -1.0
    require_news_data: bool = True

    @classmethod
    def from_env(cls) -> "NoNewsReversalConfig":
        common = SwingConfig.common_from_env("CR", {"hold_days": 5, "max_positions": 5,
                                                    "max_new_per_day": 5, "stop_loss_pct": 10.0})
        weekdays = [int(x) for x in os.getenv("CR_WEEKDAYS", "4").split(",") if x.strip()]
        return cls(
            **common,
            universe_file=os.getenv("CR_UNIVERSE_FILE",
                                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "reversal_universe.csv")),
            store_dir=os.getenv("DS_STORE_DIR", DEFAULT_STORE_DIR),
            weekdays=weekdays,
            entry_at=_env_time("CR_ENTRY_AT", "15:10"),
            lookback_days=int(os.getenv("CR_LOOKBACK_DAYS", "5")),
            min_drop_pct=float(os.getenv("CR_MIN_DROP_PCT", "8")),
            max_drop_pct=float(os.getenv("CR_MAX_DROP_PCT", "30")),
            min_avg_value=float(os.getenv("CR_MIN_AVG_VALUE", "100000000")),
            exclude_any_disclosure=_env_bool("CR_EXCLUDE_ANY_DISCLOSURE", True),
            block_score=float(os.getenv("CR_BLOCK_SCORE", "-1")),
            require_news_data=_env_bool("CR_REQUIRE_NEWS_DATA", True),
        )


class NoNewsReversalStrategy(SwingStrategy):
    name = NAME

    def __init__(self, api, config: NoNewsReversalConfig, universe: Optional[List[dict]] = None,
                 history: Optional[PriceHistory] = None, store: Optional[DisclosureStore] = None,
                 source: Optional[DisclosureSource] = None):
        self.api = api
        self.config = config
        self.universe = universe if universe is not None else load_universe(config.universe_file)
        self.history = history or PriceHistory(state_path(config, NAME, "price_history.json"))
        self.store = store or DisclosureStore(config.store_dir)
        self.source = source

    def entry_slots(self):
        return [self.config.entry_at]

    def _window_start(self, now: datetime) -> datetime:
        """騰落率を測る期間の始まり（lookback_days 営業日前の引け後）"""
        d = now.date()
        for _ in range(self.config.lookback_days):
            d = previous_market_day(d)
        return datetime.combine(d, dt_time(15, 30))

    def news_data_ready(self, now: datetime) -> bool:
        d = self._window_start(now).date()
        while d <= now.date():
            if is_market_day(d) and not self.store.has_day(d):
                return False
            d += timedelta(days=1)
        return True

    def select(self, now: datetime, slot) -> List[Candidate]:
        c = self.config
        today = now.date()
        if today.weekday() not in c.weekdays:
            return []
        if self.source:
            # 引け後〜夜に出た開示も拾うため、期間内の日を全部取り直す
            d = self._window_start(now).date()
            while d <= today:
                self.store.update(d, self.source)   # 休日の開示も拾う
                d += timedelta(days=1)
        if c.require_news_data and not self.news_data_ready(now):
            logger.warning(f"[{NAME}] 期間内の開示データが揃っていないため、今日は買いません"
                           f"（{self.store.folder} を確認してください）")
            return []
        news = self.store.between(self._window_start(now), now)
        if c.exclude_any_disclosure:
            blocked = {d.code for d in news}
        else:
            blocked = {d.code for d in news if d.score <= c.block_score}

        names, refs = {}, {}
        for item in self.universe:
            code = item["Code"]
            if code in blocked:
                continue
            ref = self.history.close_days_ago(code, c.lookback_days, today)
            avg_value = self.history.avg_value(code, 20, today) or 0
            if not ref or (c.min_avg_value and avg_value < c.min_avg_value):
                continue
            names[code], refs[code] = item.get("Name", ""), ref
        result = []
        for code, board in iter_boards(self.api, list(refs)):
            ref = refs[code]
            current = board.get("CurrentPrice")
            if not current or is_special(board):
                continue
            ret = (current / ref - 1) * 100
            if ret > -c.min_drop_pct or ret < -c.max_drop_pct:
                continue
            result.append(Candidate(code, names[code], "close", current,
                                    note=f"{c.lookback_days}日で {ret:+.1f}%（開示なし）", score=-ret))
        result.sort(key=lambda x: x.score, reverse=True)
        return result

    def collect(self, now: datetime):
        collect_closes(self.api, self.history, [i["Code"] for i in self.universe], now.date())
        if self.source:
            self.store.update(now.date(), self.source)


def main():
    from kabu_token import get_token
    from special_quote import KabuApi

    config = NoNewsReversalConfig.from_env()
    api = KabuApi(get_token(config.api_password))
    strategy = NoNewsReversalStrategy(api, config, source=source_from_env())
    if len(sys.argv) > 1 and sys.argv[1] == "collect":
        strategy.collect(datetime.now())
        return
    build_engine(strategy, api, config).run_forever()


if __name__ == "__main__":
    main()
