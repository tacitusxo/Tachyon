"""
バックテストの実行

  python -m backtest --data data/dummy
  python -m backtest --data data/dummy --strategy 3 --set SHR_MIN_DIP_PCT=5 --set SHR_TAKE_PROFIT_PCT=3

パラメータはライブと同じ kabucom/.env の値（SQ_* / SHR_* / AVAILABLE_MARGIN）を使い、
--set で上書きできる。バックテスト専用のパラメータは README を参照。
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
from datetime import date

from . import data as _  # noqa: F401  (sys.path 設定)
from .data import CsvDataSource
from .sim import apply_position_limit, summarize
from .strategies import (
    backtest_special_quote, backtest_stop_high, backtest_upward_revision, load_params,
)

STRATEGIES = {
    "1": "①上方修正",
    "2": "②特別買い気配",
    "3": "③ストップ高押し目",
}


def _fmt_yen(v: float) -> str:
    return f"{v:+,.0f}円"


def print_summary(s, max_positions: int):
    pf = f"{s.profit_factor:.2f}" if s.profit_factor is not None else "-"
    reasons = "、".join(f"{k} {v}" for k, v in sorted(s.reasons.items(), key=lambda x: -x[1])) or "-"
    print(f"\n■ {s.strategy}（同時保有 {max_positions} 銘柄まで）")
    print(f"  取引回数     {s.trades} 回（{s.days} 営業日）")
    print(f"  勝率         {s.win_rate:.1f}%")
    print(f"  損益合計     {_fmt_yen(s.total_pnl)}")
    print(f"  平均損益率   {s.avg_return_pct:+.2f}% / 回")
    print(f"  PF           {pf}（総利益 ÷ 総損失）")
    print(f"  最大DD       {s.max_drawdown:,.0f}円（損益累計の山からの最大下落）")
    print(f"  決済理由     {reasons}")


def write_trades(path: str, trades):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ロジック", "銘柄", "名称", "買い時刻", "買値", "株数", "売り時刻", "売値",
                    "決済理由", "損益", "損益率%", "メモ"])
        for t in sorted(trades, key=lambda x: x.entry_time):
            w.writerow([t.strategy, t.code, t.name, t.entry_time.strftime("%Y-%m-%d %H:%M"),
                        t.entry_price, t.qty, t.exit_time.strftime("%Y-%m-%d %H:%M"), t.exit_price,
                        t.exit_reason, round(t.pnl), round(t.return_pct, 2), t.note])


def run(data_dir: str, strategies: str = "123", start=None, end=None, overrides=None,
        out: str = "backtest_result"):
    logging.getLogger().setLevel(logging.WARNING)  # ライブ部品の INFO ログを抑える
    logging.getLogger("utils").setLevel(logging.ERROR)  # 予算で買えない銘柄の警告（見送りとして扱う）
    params, sq, shr = load_params(overrides)
    ds = CsvDataSource(data_dir)
    days = ds.trading_days(start, end)
    if not days:
        raise SystemExit(f"データがありません: {data_dir}")
    print(f"期間: {days[0]} 〜 {days[-1]}（{len(days)} 営業日） データ: {data_dir}")
    print(f"共通: スリッページ {params.slippage_ticks} ティック / 手数料率 {params.fee_rate}")

    runs = []
    if "1" in strategies:
        print(f"① 利確 +{params.ur_take_profit_pct}% / 損切り -{params.ur_stop_loss_pct}% / "
              f"検出遅れ {params.ur_detect_delay_min}分 / 予算 {params.ur_budget:,.0f}円")
        runs.append(("1", backtest_upward_revision(ds, days, params), 1))
    if "2" in strategies:
        print(f"② 利確 +{sq.take_profit_ticks}ティック / 損切り -{sq.stop_loss_ticks}ティック / "
              f"上位 {sq.ranking_top_n} 銘柄 / 予算 {sq.budget_per_trade:,.0f}円")
        runs.append(("2", backtest_special_quote(ds, days, params, sq), sq.max_positions))
    if "3" in strategies:
        print(f"③ 押し目 {shr.min_dip_pct}〜{shr.max_dip_pct}% / 反発 {shr.rebound_ticks}ティック / "
              f"利確 +{shr.take_profit_pct}% / 損切り -{shr.stop_loss_pct}% / 予算 {shr.budget_per_trade:,.0f}円")
        runs.append(("3", backtest_stop_high(ds, days, params, shr), shr.max_positions))

    all_trades = []
    for key, trades, max_positions in runs:
        trades = apply_position_limit(trades, max_positions)
        all_trades += trades
        print_summary(summarize(STRATEGIES[key], trades, days), max_positions)

    path = os.path.join(out, "trades.csv")
    write_trades(path, all_trades)
    print(f"\n取引明細: {path}")
    return all_trades


def main():
    parser = argparse.ArgumentParser(description="3ロジックのバックテスト")
    parser.add_argument("--data", default="data/dummy", help="データフォルダ")
    parser.add_argument("--strategy", default="123", help="実行するロジック（例: 13）")
    parser.add_argument("--start", help="開始日 YYYY-MM-DD")
    parser.add_argument("--end", help="終了日 YYYY-MM-DD")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                        help="パラメータの上書き（例: SHR_MIN_DIP_PCT=5）。複数指定可")
    parser.add_argument("--out", default="backtest_result", help="結果の出力先フォルダ")
    a = parser.parse_args()
    overrides = dict(kv.split("=", 1) for kv in a.set)
    run(a.data, a.strategy,
        date.fromisoformat(a.start) if a.start else None,
        date.fromisoformat(a.end) if a.end else None,
        overrides, a.out)


if __name__ == "__main__":
    main()
