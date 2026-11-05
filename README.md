# Tachyon

株価監視と自動売買を行う Python アプリケーション

> **⚠️ 注意**: このアプリケーションは実際の株式取引を実行します。本番環境での使用には十分な注意と検証が必要です。

> 毎日の起動手順は [運用手順書](docs/運用手順書.md) を参照してください。

## 概要

このアプリケーションは、kabu.com の API を利用して以下の機能を提供します：

1. 上方修正銘柄の自動監視

   - カスタマイズ可能な監視間隔（デフォルト 30 秒）で上方修正された銘柄を監視
   - 上方修正された銘柄を自動的に検出
   - 市場が閉まっている時間帯（夜間・休日）は自動監視・売買は実行されず、市場が開かれるまで待機します
   - 市場が開いている時間帯（9:00-11:30, 12:30-15:30）のみ自動監視・売買が実行されます
   - 上方修正が検出された場合、Slack に通知されます

2. 自動売買機能

   - 上方修正された銘柄に対して自動的に買い注文を実行
   - 利用可能な余力に基づいて購入可能な株数を自動計算（最小単位：100 株）
   - 成行注文による即時取引（成行は値幅上限で余力を拘束されるため、株数は値幅上限の値段で計算）
   - デフォルトはペーパートレード（`UR_DRY_RUN=true`）。発注せず、約定を模擬してログに出す。実発注は `UR_DRY_RUN=false`
   - 約定後に指値売りで利益確定（約定単価の +`UR_TAKE_PROFIT_PCT`%、既定 20%。ストップ高が上限）
   - 1日に買う銘柄数は `UR_MAX_BUYS_PER_DAY`（既定 1）まで。買った後も監視・通知は続く

3. リスク管理
   - 利用可能な余力の 90%までを使用する制限
   - エラーハンドリングとログ記録

4. 特別買い気配スキャルピング（`special_quote.py`、上方修正監視とは独立して起動）
   - 値上がり率ランキングから特別買い気配の銘柄を検出し、ストップ高まで伸びる余地がある銘柄だけ上値指値で買う
   - 追随モード（気配の切り上げに合わせて取消→再発注）も選択可
   - 約定後はティック指定で利確（指値）・損切り（成行）、引け前に強制決済
   - デフォルトはペーパートレード（実発注しない）
   - 詳細は「[特別買い気配スキャルピング](#特別買い気配スキャルピングspecial_quotepy)」を参照

5. 前日ストップ高銘柄の押し目買い（`stop_high_rebound.py`、独立して起動）
   - 引け後にストップ高で引けた銘柄を自動収集し、翌日の寄り後に監視
   - 寄り後に一定%下落 → 安値から数ティック反発したところで指値買い
   - 約定後は % 指定で利確（指値）・損切り（成行）、強制決済時刻あり
   - デフォルトはペーパートレード（実発注しない）
   - 詳細は「[前日ストップ高銘柄の押し目買い](#前日ストップ高銘柄の押し目買いstop_high_reboundpy)」を参照

6. 新しい戦略 A〜D・予備（それぞれ独立して起動。既定はペーパートレード）
   - A: TOPIX 段階的ウエイト低減の売られすぎの戻り（`topix_rebalance.py`）
   - B: 適時開示の採点 → 翌日・後場に買う（`disclosure_score.py`、Claude による採点は任意）
   - C: 材料なしで下がった銘柄の短期の戻り（`no_news_reversal.py`）
   - D: 引け前のモメンタム、日経225 先物（`futures_close_momentum.py`）
   - 予備: 注目銘柄のオープニングレンジブレイク（`orb.py`）
   - 詳細は [新しい戦略](docs/新戦略.md) を参照

7. バックテスト（`backtest/`）
   - 3 ロジックを過去データ（日足・分足・開示）で検証。ライブと同じパラメータ・判定ロジックを使う
   - 詳細は「[バックテスト](#バックテストbacktest)」を参照

## 環境構築

1. Python 3.8 以上がインストールされていること
2. kabu.com の API サービス（localhost:18080）が起動していること

## セットアップ

1. 必要なパッケージをインストール

```bash
pip install -r requirements.txt
```

## 環境変数の設定

`.env`ファイルを作成し、以下の環境変数を設定してください：

```
KABUCOM_API_PASSWORD=your_api_password  # kabu.comのAPIパスワード
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/~~~~  # Slack通知用のWebhook URL
AVAILABLE_MARGIN=500000  # 利用可能な証拠金（円）
KABUCOM_ACCOUNT_TYPE=12  # kabu.comの口座種別（2:一般口座、4:特定口座、12:法人口座）
MONITORING_INTERVAL_SECONDS=1  # 監視間隔（秒、推奨値：1）
UR_DRY_RUN=true  # 上方修正監視（main.py）を true: ペーパートレード（発注しない）/ false: 実発注
```

特別買い気配スキャルピング用の設定（`SQ_*`）は「[特別買い気配スキャルピング](#特別買い気配スキャルピングspecial_quotepy)」、前日ストップ高銘柄の押し目買い用の設定（`SHR_*`）は「[前日ストップ高銘柄の押し目買い](#前日ストップ高銘柄の押し目買いstop_high_reboundpy)」を参照してください。

## 使用方法

1. kabu.com の API サービスを起動
2. アプリケーションを起動

```bash
cd kabucom
python main.py            # 上方修正監視・自動売買
python special_quote.py   # 特別買い気配スキャルピング
python stop_high_rebound.py   # 前日ストップ高銘柄の押し目買い
python topix_rebalance.py     # A. TOPIX 段階的ウエイト低減の戻り
python disclosure_score.py    # B. 適時開示の採点
python no_news_reversal.py    # C. 材料なしで下がった銘柄の戻り
python futures_close_momentum.py   # D. 引け前のモメンタム（先物）
python orb.py                 # 予備. オープニングレンジブレイク
```

Windows では `run.bat` / `run_special_quote.bat` / `run_stop_high_rebound.bat`、新しい戦略は `run_<ファイル名>.bat` でも起動できます。新しい戦略の設定は [新しい戦略](docs/新戦略.md) を参照してください。

## 特別買い気配スキャルピング（`special_quote.py`）

上方修正監視（`main.py`）とは独立して動く戦略です。

### ロジック

1. 値上がり率ランキング上位 `SQ_RANKING_TOP_N` 銘柄の板を確認し、**特別買い気配**（`AskSign == "0102"`）の銘柄を探す
2. **伸びる余地があるか**を判定し、無ければその日は見送る
   - ストップ高（銘柄情報の `UpperLimit`）までの余地 `(UpperLimit − 気配) / 気配` が `SQ_MIN_ROOM_PCT` % 未満なら見送り（出遅れ）。`UpperLimit` が取れない場合も見送り
   - 前日比 `(気配 / 前日終値 − 1)` が `SQ_MAX_GAP_PCT` % を超えたら見送り（0 = 判定しない）
3. 買い注文の出し方は `SQ_ENTRY_MODE` で選ぶ
   - **`cap`（既定）: 上値指値を 1 回だけ出す。** 指値 = `気配 × (1 + SQ_MAX_CHASE_PCT%)` を呼値に切り下げ（ストップ高が上限）。
     寄り付き（板寄せ）の約定値段は全員共通で、約定値段より高い買い指値は全量約定するため、寄り値が指値以下なら**寄り値で**優先的に約定する。取消→再発注をしないので時間優先も失わない。
     気配が指値を超えたら（寄り値も指値を超えるので）取消
   - `chase`: 現在の特別気配値で指値買いし、気配が切り上がったら**取消して新しい気配値で出し直す**（kabu API に株式の訂正注文はないため取消→再発注）。
     出し直しは `SQ_MAX_REORDERS` 回まで、初回価格から `SQ_MAX_CHASE_PCT` % 上までで、超えたら見送り。
     気配と同値の指値は寄り付きで後回しになりやすく、約定しにくい
   - どちらも、特気が解消（寄り付き）して `SQ_FILL_WAIT_AFTER_OPEN_SEC` 秒たっても約定しなければ取消。一部約定した場合は残りを取消し、約定分だけで次に進む
   - 株数は指値（最悪の約定価格）で計算する
4. 約定したら **約定単価 + `SQ_TAKE_PROFIT_TICKS` ティック** で指値売り（呼値は TOPIX500 / それ以外を自動判別）
5. 現在値が **約定単価 - `SQ_STOP_LOSS_TICKS` ティック** 以下になったら、利確注文を取消して成行売り（kabu API は OCO 非対応のため、プログラム側で監視）
6. `SQ_FORCE_EXIT` の時刻になったら、保有分を成行で売る
7. 現物の差金決済を避けるため、同じ銘柄は 1 日 1 回までしかエントリーしない

### 設定（`.env`）

```
SQ_DRY_RUN=true              # true: ペーパートレード（発注しない）。実発注は false
SQ_BUDGET_PER_TRADE=300000   # 1トレードの予算（未設定なら AVAILABLE_MARGIN）。90%まで使用
SQ_RANKING_EXCHANGE=T        # ALL/T/TP/TS/TG
SQ_RANKING_TOP_N=10
SQ_SCAN_INTERVAL_SEC=5
SQ_POLL_INTERVAL_SEC=1
SQ_MIN_PRICE=0
SQ_MAX_PRICE=0               # 0 = 上限なし
SQ_TAKE_PROFIT_TICKS=2
SQ_STOP_LOSS_TICKS=3
SQ_ENTRY_MODE=cap            # cap: 上値指値を1回 / chase: 気配に追随して出し直す
SQ_MAX_CHASE_PCT=5           # 検知した気配から何%上まで買うか（cap: 指値 / chase: 追随上限）
SQ_MIN_ROOM_PCT=5            # ストップ高までの余地がこれ未満なら見送り（0 = 判定しない）
SQ_MAX_GAP_PCT=0             # 前日比がこれを超えたら見送り（0 = 判定しない）
SQ_MAX_REORDERS=5            # chase のみ
SQ_FILL_WAIT_AFTER_OPEN_SEC=10
SQ_MAX_POSITIONS=1
SQ_ENTRY_END=15:00
SQ_FORCE_EXIT=15:20
```

### 起動

```bash
cd kabucom
python special_quote.py
```

デフォルトは **ペーパートレード** です。板情報から約定を模擬してログに損益を出すので、まずこれで挙動を確認してください（模擬は実際より約定しやすい楽観的な想定です）。

### 注意

- 特別気配中は売買が成立しないため、買いが約定するのは基本的に**寄り付いたとき**です。`chase` は気配と同値の指値なので後回しになり約定しないことが多く、`cap` は約定しやすい代わりに**寄り天（寄り値が高値）でも約定する**ので、損益は寄り後の値動き次第です。`SQ_MAX_CHASE_PCT` を大きくするほど高値掴みのリスクが上がります
- ペーパートレードの約定は、寄り値（現在値）が指値以下なら寄り値で約定とみなします
- 寄り付いた後に急落すると、損切りは成行のため滑る可能性があります。特別売り気配になると売りも成立しません
- テストは `python -m unittest discover tests` で実行できます

## 前日ストップ高銘柄の押し目買い（`stop_high_rebound.py`）

前日にストップ高で引けた銘柄は、翌日の寄り付き後にいったん売られやすい、という傾向を狙う戦略です。他の機能とは独立して動きます。

### ロジック

1. **前日の引け後（`SHR_COLLECT_AT`、デフォルト 15:35）**: 値上がり率ランキングの銘柄のうち、終値が銘柄情報の値幅上限（`UpperLimit`）に達している銘柄を `kabucom/stop_high_list.json` に保存する
2. **当日の寄り後（`SHR_ENTRY_START`〜`SHR_ENTRY_END`）**: 保存した銘柄の板を監視する
   - 基準値 = 前日終値と寄り後の高値の大きい方
   - 基準値から **`SHR_MIN_DIP_PCT` % 以上下落**し、その安値から **`SHR_REBOUND_TICKS` ティック反発**したら、最良売気配で指値買い
   - 下落が `SHR_MAX_DIP_PCT` % を超えた銘柄は見送る
   - 寄らずに特別気配のままの銘柄（ストップ高張り付きなど）は対象外
   - `SHR_FILL_WAIT_SEC` 秒で約定しなければ取消し、条件を満たせば再度狙う（`SHR_MAX_ENTRY_ATTEMPTS` 回まで）
3. 約定したら **約定単価 × (1 + `SHR_TAKE_PROFIT_PCT` %)** を呼値に切り上げた価格で指値売り
4. 現在値が **約定単価 × (1 − `SHR_STOP_LOSS_PCT` %)** 以下になったら、利確注文を取消して成行売り
5. `SHR_FORCE_EXIT` の時刻になったら、保有分を成行で売る
6. 同じ銘柄は 1 日 1 回まで（現物の差金決済対策）

起動したままにしておけば、引け後の収集と翌朝の売買を毎日自動で繰り返します。前日の引け後に起動していなかった場合は、翌朝の寄り前に `python stop_high_rebound.py collect` を実行すれば前営業日分を収集できます（取引時間中は実行できません）。

### 設定（`.env`）

```
SHR_DRY_RUN=true             # true: ペーパートレード（発注しない）。実発注は false
SHR_BUDGET_PER_TRADE=300000  # 1トレードの予算（未設定なら AVAILABLE_MARGIN）。90%まで使用
SHR_RANKING_EXCHANGE=T       # 収集に使うランキングの市場 ALL/T/TP/TS/TG
SHR_COLLECT_AT=15:35         # 引け後の収集時刻
SHR_LIST_FILE=               # 収集結果の保存先（未設定なら kabucom/stop_high_list.json）
SHR_POLL_INTERVAL_SEC=1
SHR_MIN_PRICE=0
SHR_MAX_PRICE=0              # 0 = 上限なし
SHR_MIN_DIP_PCT=3            # 基準値から何%下げたら押し目とみなすか
SHR_MAX_DIP_PCT=15           # これ以上下げた銘柄は見送り
SHR_REBOUND_TICKS=3          # 安値から何ティック戻したら買うか
SHR_TAKE_PROFIT_PCT=2
SHR_STOP_LOSS_PCT=2
SHR_FILL_WAIT_SEC=5
SHR_MAX_ENTRY_ATTEMPTS=3
SHR_MAX_POSITIONS=1
SHR_ENTRY_START=9:00
SHR_ENTRY_END=10:00
SHR_FORCE_EXIT=15:20
```

### 起動

```bash
cd kabucom
python stop_high_rebound.py           # 監視・売買（引け後の収集も自動）
python stop_high_rebound.py collect   # ストップ高銘柄の収集だけを今すぐ実行
```

### 注意

- ランキング API は上位 50 銘柄までしか返さないため、ストップ高銘柄がそれより多い日は取りこぼします
- 収集は「終値 = 値幅上限」で判定するため、ザラ場でストップ高をつけて引けで崩れた銘柄は含みません。値幅制限が拡大された銘柄は銘柄情報の上限値で判定します
- 寄り後の値動きは 1 秒ごとの現在値で追っているため、その間の瞬間的な高値・安値は拾えません
- 損切りは成行のため、急落時は滑る可能性があります。ペーパートレードの約定は楽観的な想定です

## バックテスト（`backtest/`）

過去データで 3 ロジックを検証します。現在は**ダミーデータでの動作確認**まで対応しています（実データは J-Quants などから取得して、下記の形式で置けばそのまま使えます）。

### 使い方

```bash
pip install -r requirements.txt
python -m backtest.dummy --out data/dummy                   # ダミーデータ作成（約1年・200銘柄、乱数なので成績に意味はない）
python -m backtest --data data/dummy                        # 3ロジックを実行
python -m backtest --data data/dummy --strategy 3 --set SHR_MIN_DIP_PCT=5 --set SHR_TAKE_PROFIT_PCT=3
```

- `--strategy`: 実行するロジック（`1` 上方修正 / `2` 特別買い気配 / `3` ストップ高押し目、`13` のように複数可）
- `--start` / `--end`: 期間（YYYY-MM-DD）
- `--set NAME=VALUE`: パラメータの上書き（複数可）
- 結果は画面に「取引回数・勝率・損益合計・平均損益率・PF・最大ドローダウン・決済理由」を表示し、取引明細を `backtest_result/trades.csv` に出力

パラメータは**ライブと同じ `kabucom/.env` の値**（`SQ_*` / `SHR_*` / `AVAILABLE_MARGIN`）を使います。バックテスト専用のパラメータ:

```
BT_SLIPPAGE_TICKS=1          # 成行の約定が何ティック不利になるか（全ロジック）
BT_FEE_RATE=0                # 手数料率（片道、売買代金に対して。0.001 = 0.1%）
UR_BT_TAKE_PROFIT_PCT=2      # ① 利確%（ライブの UR_TAKE_PROFIT_PCT とは別に指定）
UR_BT_STOP_LOSS_PCT=2        # ① 損切り%
UR_BT_DETECT_DELAY_MIN=1     # ① 開示から発注までの遅れ（分）
UR_BT_FORCE_EXIT=23:59       # ① 強制決済時刻（既定はなし＝引けで売る）
SQ_BT_MIN_OPEN_DELAY_MIN=1   # ② 9:00 から何分以上寄りが遅れたら特別気配だったとみなすか
```

### データ形式

```
<データフォルダ>/
  symbols.csv                  Code,Name,PriceRangeGroup（10003 = TOPIX500、それ以外は空か 10000）
  daily/YYYY-MM-DD.csv         Code,Open,High,Low,Close,Volume（その日の全銘柄の日足）
  minute/YYYY-MM-DD/CODE.csv   Time,Open,High,Low,Close,Volume（Time は HH:MM の1分足）
  revisions.csv                Code,DisclosedDate,DisclosedTime,Upward,Title（Upward=1 が上方修正）
```

### 再現の前提（ライブとの違い）

- **共通**: 分足の中の値動きは「陽線は 始→安→高→終、陰線は 始→高→安→終」とみなす（損切りが先に当たる側の仮定）。利確の指値は価格が届けば全量約定とする。同時保有数の上限（`SQ_MAX_POSITIONS` / `SHR_MAX_POSITIONS`、①は 1）と同日同銘柄 1 回までを適用
- **① 上方修正**: 取引時間中（9:00-11:30 / 12:30-15:30）に出た上方修正だけを対象に、開示の `UR_BT_DETECT_DELAY_MIN` 分後の始値＋スリッページで買う（株数はライブと同じく値幅上限で計算）。% 指定の利確・損切り、届かなければ引けで売る
- **② 特別買い気配（近似。結果は楽観的な上限値）**: 過去の気配データが無いため、「前日終値より高く、9:00 より遅れて寄った銘柄」を特別気配だったとみなし、上昇率の上位 `SQ_RANKING_TOP_N` 銘柄を**始値で全量約定**したと仮定する。実際は時間優先で約定しないことが多い。気配の切り上げへの追随上限は再現しない
- **③ ストップ高押し目**: 前日終値が制限値幅の上限だった銘柄を対象に、ライブと同じ `DipWatcher`（押し目・反発の判定）に分足の価格を流し、シグナルの価格＋スリッページで約定したとみなす。ライブの「ランキング上位50銘柄まで」の制限と、未約定時の取消・再挑戦は再現しない

## セキュリティに関する注意

- **環境変数の管理**: `.env` ファイルには API パスワードなどの機密情報が含まれます。絶対にリポジトリに含めないでください（.gitignore に登録済み）
- **API パスワード**: kabu.com の API パスワードは外部に漏らさないように注意してください
- **ログ出力**: このアプリケーションのログには取引情報が含まれるため、適切に管理してください

## 注意事項

- **本番環境での使用**: 実際の株式取引を行うため、設定には十分注意してください。テスト環境で十分に検証してからの使用を強く推奨します
- **リスク管理**: このアプリケーションは自動売買を行うため、予期しない取引が発生する可能性があります
- **市場時間**: 日本の株式市場の営業時間（9:00-11:30, 12:30-15:30）のみ自動売買が実行されます
- **サーバー依存**: kabu.com の API サーバーが正常に動作していることを確認してください

## ライセンス

このプロジェクトのライセンスについては、LICENSE ファイルを参照してください

## 貢献

このプロジェクトへのフィードバックや改善提案は、Issue や Pull Request でお願いします
