"""
B. 適時開示（TDnet）を採点し、好材料の銘柄を翌日（昼休みの開示は後場）に買う戦略

なぜ: 好材料でも株価がすぐには織り込みきらず、数日かけて上がり続ける傾向がある（決算後ドリフト）。
     発表の瞬間に速さで競わず、開示の「中身」を採点して翌日に入る。

流れ:
  1. 開示を取得して採点し、state/disclosures/ に保存する（disclosures.py）
       - 取得元: J-Quants の TDnet アドオン（DS_SOURCE=jquants）か CSV（DS_SOURCE=csv）
       - 採点: ルール（タイトル・本文）＋ 任意で Claude（DS_LLM_ENABLED=true）
  2. 前場（DS_MORNING_AT、既定 9:05）:
       前営業日の DS_AFTER_CLOSE_FROM（既定 15:30）〜当日 9:00 の開示のうち、点数が DS_MIN_SCORE 以上の銘柄
       - 寄り付いている（特別気配でない）
       - 寄りの窓（前日終値比）が DS_MIN_GAP_PCT 〜 DS_MAX_GAP_PCT の範囲（窓を空けすぎた銘柄は織り込み済みとして外す）
       - DS_REQUIRE_ABOVE_OPEN=true なら、現在値が始値以上（寄り天になっていない）
     を満たすものを点数の高い順に成行で買う
  3. 後場（DS_AFTERNOON_AT、既定 12:35）:
       11:30〜12:30 の開示について、12:25 時点の価格（前場の終値）と比べて同じ判定をする
  4. DS_HOLD_DAYS 営業日後の寄りで売る（損切り・利確%も指定可）

同じ銘柄に悪い開示（DS_BLOCK_SCORE 以下）が同じ期間に出ていれば買わない。
安全のため既定はペーパートレード。実発注は DS_DRY_RUN=false。

起動:
  cd kabucom
  python disclosure_score.py               # 監視・売買
  python disclosure_score.py fetch [日付]   # 開示の取得・採点だけ（既定は今日）。結果を表示する
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from typing import Dict, List, Optional

from disclosures import (
    DEFAULT_STORE_DIR, ClaudeScorer, Disclosure, DisclosureSource, DisclosureStore,
    scorer_from_env, source_from_env,
)
from special_quote import _env_bool, _env_time
from swing import Candidate, SwingConfig, SwingStrategy, build_engine, is_special, previous_market_day

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

NAME = "disclosure_score"
LUNCH_START = dt_time(11, 30)
LUNCH_END = dt_time(12, 30)


@dataclass
class DisclosureScoreConfig(SwingConfig):
    store_dir: str = DEFAULT_STORE_DIR
    morning_at: dt_time = dt_time(9, 5)
    snapshot_at: dt_time = dt_time(12, 25)
    afternoon_at: dt_time = dt_time(12, 35)
    afternoon_enabled: bool = True
    after_close_from: dt_time = dt_time(15, 30)
    min_score: float = 2.0
    block_score: float = -1.0
    min_gap_pct: float = 0.0
    max_gap_pct: float = 10.0
    require_above_open: bool = True

    @classmethod
    def from_env(cls) -> "DisclosureScoreConfig":
        common = SwingConfig.common_from_env("DS", {"hold_days": 3, "stop_loss_pct": 5.0,
                                                    "collect_at": "17:30", "poll_interval_sec": 20})
        return cls(
            **common,
            store_dir=os.getenv("DS_STORE_DIR", DEFAULT_STORE_DIR),
            morning_at=_env_time("DS_MORNING_AT", "9:05"),
            snapshot_at=_env_time("DS_SNAPSHOT_AT", "12:25"),
            afternoon_at=_env_time("DS_AFTERNOON_AT", "12:35"),
            afternoon_enabled=_env_bool("DS_AFTERNOON_ENABLED", True),
            after_close_from=_env_time("DS_AFTER_CLOSE_FROM", "15:30"),
            min_score=float(os.getenv("DS_MIN_SCORE", "2")),
            block_score=float(os.getenv("DS_BLOCK_SCORE", "-1")),
            min_gap_pct=float(os.getenv("DS_MIN_GAP_PCT", "0")),
            max_gap_pct=float(os.getenv("DS_MAX_GAP_PCT", "10")),
            require_above_open=_env_bool("DS_REQUIRE_ABOVE_OPEN", True),
        )


def aggregate(items: List[Disclosure], block_score: float) -> Dict[str, dict]:
    """銘柄ごとに点数を合計する。悪い開示が1つでもあれば blocked"""
    result: Dict[str, dict] = {}
    for d in items:
        if not d.code:
            continue
        r = result.setdefault(d.code, {"name": d.name, "score": 0.0, "blocked": False, "titles": []})
        r["score"] = max(-3.0, min(3.0, r["score"] + d.score))
        r["titles"].append(f"{d.title}（{d.score:+g}）")
        if d.score <= block_score:
            r["blocked"] = True
    return result


class DisclosureScoreStrategy(SwingStrategy):
    name = NAME

    def __init__(self, api, config: DisclosureScoreConfig, source: Optional[DisclosureSource],
                 store: Optional[DisclosureStore] = None, llm: Optional[ClaudeScorer] = None):
        self.api = api
        self.config = config
        self.source = source
        self.store = store or DisclosureStore(config.store_dir)
        self.llm = llm
        self._snapshot: Dict[str, float] = {}

    def entry_slots(self):
        slots = [self.config.morning_at]
        if self.config.afternoon_enabled:
            slots += [self.config.snapshot_at, self.config.afternoon_at]
        return slots

    def refresh(self, day: date):
        if self.source:
            self.store.update(day, self.source, self.llm)

    def select(self, now: datetime, slot) -> List[Candidate]:
        c = self.config
        today = now.date()
        if slot == c.morning_at:
            prev = previous_market_day(today)
            self.refresh(prev)
            self.refresh(today)
            window = self.store.between(datetime.combine(prev, c.after_close_from),
                                        datetime.combine(today, dt_time(9, 0)))
            return self._candidates(window, reference="open")
        if slot == c.snapshot_at:
            self.refresh(today)
            window = self.store.between(datetime.combine(today, LUNCH_START), now)
            self._snapshot = {}
            for code, info in aggregate(window, c.block_score).items():
                if info["score"] >= c.min_score and not info["blocked"]:
                    board = self.api.get_board(code) or {}
                    if board.get("CurrentPrice"):
                        self._snapshot[code] = board["CurrentPrice"]
            return []
        if slot == c.afternoon_at:
            self.refresh(today)
            window = self.store.between(datetime.combine(today, LUNCH_START),
                                        datetime.combine(today, LUNCH_END))
            return self._candidates(window, reference="snapshot")
        return []

    def _candidates(self, window: List[Disclosure], reference: str) -> List[Candidate]:
        c = self.config
        result = []
        for code, info in aggregate(window, c.block_score).items():
            if info["blocked"] or info["score"] < c.min_score:
                continue
            board = self.api.get_board(code) or {}
            current, opening = board.get("CurrentPrice"), board.get("OpeningPrice")
            if not current or not opening or is_special(board):
                logger.info(f"[{code} {info['name']}] 寄り付いていない（特別気配など）ため見送ります")
                continue
            base = board.get("PreviousClose") if reference == "open" else self._snapshot.get(code)
            if not base:
                continue
            start = opening if reference == "open" else current
            gap = (start / base - 1) * 100
            if gap < c.min_gap_pct or gap > c.max_gap_pct:
                logger.info(f"[{code} {info['name']}] 窓 {gap:+.1f}% が範囲外のため見送ります")
                continue
            if reference == "open" and c.require_above_open and current < opening:
                logger.info(f"[{code} {info['name']}] 現在値が始値を下回っているため見送ります")
                continue
            note = f"点数 {info['score']:+g} / 窓 {gap:+.1f}% / " + " / ".join(info["titles"])[:120]
            result.append(Candidate(code, info["name"], "market", current, note=note, score=info["score"]))
        result.sort(key=lambda x: x.score, reverse=True)
        return result

    def collect(self, now: datetime):
        self.refresh(now.date())


def main():
    from kabu_token import get_token
    from special_quote import KabuApi

    config = DisclosureScoreConfig.from_env()
    source = source_from_env()
    llm = scorer_from_env()
    if len(sys.argv) > 1 and sys.argv[1] == "fetch":
        day = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today()
        store = DisclosureStore(config.store_dir)
        items = store.update(day, source, llm) if source else store.load_day(day)
        for d in sorted(items, key=lambda x: -x.score):
            print(f"{d.score:+.1f} {d.code} {d.name} {d.disclosed_at[11:16]} [{d.category}] {d.title}")
        return
    if source is None:
        logger.warning("開示の取得元がありません（DS_SOURCE=none）。保存済みの開示だけで判定します")
    api = KabuApi(get_token(config.api_password))
    strategy = DisclosureScoreStrategy(api, config, source, llm=llm)
    build_engine(strategy, api, config).run_forever()


if __name__ == "__main__":
    main()
