import urllib.request
import json
import pprint
import logging

# ロギングの設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

def send_order(token: str, symbol: str, account_type: int, quantity: int = 100, price: float = 0):
    """
    指定された銘柄に対して発注を行う

    Args:
        token (str): APIトークン
        symbol (str): 証券コード
        account_type (int): 口座種別
        quantity (int, optional): 注文数量. Defaults to 100.
        price (float, optional): トリガー価格の基準値. Defaults to 0.

    Returns:
        bool: 発注が成功したかどうか
    """
    order_obj = {
        'Symbol': symbol,
        'Exchange': 1,
        'SecurityType': 1,
        'Side': '2',  # 買い注文
        'CashMargin': 1,
        'DelivType': 2,
        'FundType': 'AA',
        'AccountType': account_type,
        'Qty': quantity,
        'FrontOrderType': 10,  # 成行注文
        'ExpireDay': 0,
        'ReverseLimitOrder': {
            'TriggerSec': 1,
            'TriggerPrice': price * 1.2,  # 価格の1.2倍をトリガー価格として設定
            'UnderOver': 2,
            'AfterHitOrderType': 1,
            'AfterHitPrice': 0
        }
    }
    order_json_data = json.dumps(order_obj).encode('utf-8')

    order_url = 'http://localhost:18080/kabusapi/sendorder'
    order_req = urllib.request.Request(order_url, order_json_data, method='POST')
    order_req.add_header('Content-Type', 'application/json')
    order_req.add_header('X-API-KEY', token)

    try:
        with urllib.request.urlopen(order_req) as res:
            logger.info("\n発注結果:")
            logger.info(f"{res.status} {res.reason}")
            content = json.loads(res.read())
            logger.info(pprint.pformat(content))
            return True
    except urllib.error.HTTPError as e:
        logger.error(e)
        content = json.loads(e.read())
        logger.error(pprint.pformat(content))
        return False
    except Exception as e:
        logger.error(e)
        return False

if __name__ == "__main__":
    # テスト用のコード
    token = "your_token_here"  # 実際のトークンを設定してください
    symbol = "9433"  # NTTドコモ
    account_type = 2  # デフォルト値
    send_order(token, symbol, account_type)
