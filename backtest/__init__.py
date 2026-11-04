"""
3ロジックのバックテスト

  python -m backtest.dummy --out data/dummy          # ダミーデータを作る
  python -m backtest --data data/dummy               # バックテストを実行
"""
import os
import sys

# ライブ用のモジュール（kabucom/*.py）をそのまま再利用する
_KABUCOM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kabucom")
if _KABUCOM not in sys.path:
    sys.path.insert(0, _KABUCOM)
