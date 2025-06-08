# Tachyon

株価監視と自動売買を行う Python アプリケーション

## 環境構築

1. Python 3.8 以上をインストール
2. 必要なパッケージをインストール

```bash
pip install -r requirements.txt
```

## 環境変数の設定

`.env`ファイルを作成し、以下の環境変数を設定してください：

```
API_PASSWORD=your_api_password
AVAILABLE_MARGIN=1000000
ACCOUNT_TYPE=特定口座
```

## 実行形式ファイルの作成

### 前提条件

- Python 3.8 以上がインストールされていること
- pip が利用可能であること

### 手順

1. PyInstaller をインストール

```bash
pip install pyinstaller
```

2. 実行形式ファイルを作成

```bash
pyinstaller --onefile --noconsole --name kabucom kabucom/main.py
```

3. 生成されたファイル

- 実行形式ファイル: `dist/kabucom.exe`

### 実行形式ファイルの使用方法

1. `dist`フォルダ内の`kabucom.exe`を任意の場所にコピー
2. `.env`ファイルを同じディレクトリに配置
3. `kabucom.exe`をダブルクリックで実行

### 注意事項

- 初回実行時は少し時間がかかる場合があります
- セキュリティソフトによって警告が出る場合があります（PyInstaller で作成された exe ファイルの一般的な動作です）
- 環境変数（`.env`ファイル）は実行時に必要なので、必ず exe ファイルと同じディレクトリに配置してください

### 追加オプション

必要に応じて、以下のオプションを使用できます：

- `--icon=path/to/icon.ico`: カスタムアイコンを設定
- `--add-data "path/to/file;destination"`: 追加のファイルを含める
- `--hidden-import=module_name`: 自動検出されないモジュールを明示的に含める
