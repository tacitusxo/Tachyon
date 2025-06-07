import urllib.request
import json
import pprint
from dotenv import load_dotenv
import os
import logging
from scraper import NikkeiScraper

# ロギングの設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 環境変数の読み込み
env_path = os.path.join(os.path.dirname(__file__), ".env")
logger.info(f"Loading .env from: {env_path}")
load_dotenv(dotenv_path=env_path)

api_password = os.getenv("APIPASSWORD")
if api_password is None:
    logger.error("Error: APIPASSWORD environment variable is not set")
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
        logger.info("トークンの発行に成功しました")
        logger.info(token)
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
except KeyboardInterrupt:
    logger.info("\n監視を停止しました")

# 発注
# order_obj = {
#     'Symbol': '9433',
#     'Exchange': 1,
#     'SecurityType': 1,
#     'Side': '2',
#     'CashMargin': 1,
#     'DelivType': 2,
#     'FundType': 'AA',
#     'AccountType': 2,
#     'Qty': 100,
#     'FrontOrderType': 30,
#     'Price': 2762.5,
#     'ExpireDay': 0,
#     'ReverseLimitOrder': {
#         'TriggerSec': 3,
#         'TriggerPrice': 1600,
#         'UnderOver': 2,
#         'AfterHitOrderType': 1,
#         'AfterHitPrice': 0
#     }
# }
# order_json_data = json.dumps(order_obj).encode('utf-8')

# order_url = 'http://localhost:18080/kabusapi/sendorder'
# order_req = urllib.request.Request(order_url, order_json_data, method='POST')
# order_req.add_header('Content-Type', 'application/json')
# order_req.add_header('X-API-KEY', token)

# try:
#     with urllib.request.urlopen(order_req) as res:
#         print("\n発注結果:")
#         print(res.status, res.reason)
#         for header in res.getheaders():
#             print(header)
#         print()
#         content = json.loads(res.read())
#         pprint.pprint(content)
# except urllib.error.HTTPError as e:
#     print(e)
#     content = json.loads(e.read())
#     pprint.pprint(content)
# except Exception as e:
#     print(e)
