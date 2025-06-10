import urllib.request
import json
import pprint
import logging
from config import load_environment_variables
from kabu_token import get_token
from boardGet import get_board_info
from utils import calculate_purchase_quantity

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
        'Price': 0,
        'ExpireDay': 0,
        'ReverseLimitOrder': {
            'TriggerSec': 1,
            'TriggerPrice': float(price * 1.2),  # 価格の1.2倍をトリガー価格として設定（double型）
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
    # 環境変数を読み込む
    api_password, available_margin, account_type = load_environment_variables()
    
    # トークンを取得
    token = get_token(api_password)
    
    # テスト用の銘柄
    symbol = "9433"  # KDDI
    
    # ボード情報を取得
    board_info = get_board_info(symbol, token)
    if board_info and 'CurrentPrice' in board_info:
        price = board_info['CurrentPrice']
        logger.info(f"現在の価格: {price}")
        
        # 購入可能な株数を計算
        quantity = calculate_purchase_quantity(price, available_margin)
        if quantity > 0:
            logger.info(f"{symbol}の購入可能な株数: {quantity}株")
            if send_order(token, symbol, account_type, quantity, price):
                logger.info(f"{symbol}の発注が成功しました（{quantity}株）")
            else:
                logger.error(f"{symbol}の発注に失敗しました")
        else:
            logger.error(f"{symbol}の購入可能な株数が0株のため、発注をスキップします")
    else:
        logger.error(f"{symbol}の価格情報の取得に失敗しました")
