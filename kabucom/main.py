import urllib.request
import json
import pprint
from dotenv import load_dotenv
import os
import logging
from scraper import NikkeiScraper
from order import send_order
from boardGet import get_board_info

# ロギングの設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 環境変数の読み込み
try:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        logger.info(f"Loading .env from: {env_path}")
        load_dotenv(dotenv_path=env_path)
    else:
        logger.info("No .env file found, using system environment variables")
except Exception as e:
    logger.warning(f"Failed to load .env file: {e}")

api_password = os.getenv("KABUCOM_API_PASSWORD")

if api_password is None:
    logger.error("Error: KABUCOM_API_PASSWORD environment variable is not set")
    exit(1)

obj = {"APIPassword": api_password}

json_data = json.dumps(obj).encode("utf8")

url = "http://localhost:18080/kabusapi/token"
req = urllib.request.Request(url, json_data, method="POST")
req.add_header("Content-Type", "application/json")

token = None
try:
    with urllib.request.urlopen(req) as res:
        content = json.loads(res.read())
        token = content.get("Token")
except urllib.error.HTTPError as e:
    logger.error(e)
    content = json.loads(e.read())
    logger.error(pprint.pformat(content))
    exit(1)
except Exception as e:
    logger.error(e)
    exit(1)

if not token:
    logger.error("トークンの取得に失敗しました")
    exit(1)
logger.info("トークンの発行に成功しました")
logger.info(token)

# トークン発効後にscraperを実行
logger.info("上方修正監視を開始します...")
scraper = NikkeiScraper()
try:
    upward_revision_symbols = scraper.run_continuous_monitoring(
        interval_minutes=5
    )
    logger.info("\n上方修正された企業のSymbolのリスト:")
    for symbol in upward_revision_symbols:
        logger.info(f"- {symbol}")
        # 上方修正された銘柄に対して発注を実行
        board_info = get_board_info(symbol)
        if board_info and 'CurrentPrice' in board_info:
            price = board_info['CurrentPrice']
            logger.info(f"現在の価格: {price}")
            if send_order(token, symbol, price):
                logger.info(f"{symbol}の発注が成功しました")
            else:
                logger.error(f"{symbol}の発注に失敗しました")
        else:
            logger.error(f"{symbol}の価格情報の取得に失敗しました")
except KeyboardInterrupt:
    logger.info("\n監視を停止しました")