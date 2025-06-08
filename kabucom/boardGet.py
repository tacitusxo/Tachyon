import urllib.request
import json
import logging
from config import load_environment_variables
from kabu_token import get_token
from utils import calculate_purchase_quantity

# ロギングの設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

def get_board_info(symbol, token):
    """
    指定されたシンボルの価格情報を取得する
    
    Args:
        symbol (str): 取得したいシンボル（例: '5401@1'）
        token (str): Kabuステーションのトークン
    
    Returns:
        dict: 価格情報
    """
    url = f'http://localhost:18080/kabusapi/board/{symbol}@1'
    req = urllib.request.Request(url, method='GET')
    req.add_header('Content-Type', 'application/json')
    req.add_header('X-API-KEY', token)

    try:
        with urllib.request.urlopen(req) as res:
            print(f'Status: {res.status} {res.reason}')
            content = json.loads(res.read())
            return content
    except urllib.error.HTTPError as e:
        print(f'Error: {e}')
        content = json.loads(e.read())
        return content
    except Exception as e:
        print(f'Error: {e}')
        return None

if __name__ == '__main__':
    # 環境変数を読み込む
    api_password, available_margin, _ = load_environment_variables()
    
    # トークンを取得
    token = get_token(api_password)
    
    # ボード情報を取得
    symbol = 8117  # 例として8117（中央精機）を使用
    result = get_board_info(symbol, token)
    
    if result and 'CurrentPrice' in result:
        price = result['CurrentPrice']
        logger.info(f"現在の価格: {price}")
        
        # 購入可能な株数を計算
        quantity = calculate_purchase_quantity(price, available_margin)
        if quantity > 0:
            logger.info(f"購入可能な株数: {quantity}株")
        else:
            logger.warning("購入可能な株数が0株です")
    else:
        logger.error("価格情報の取得に失敗しました")
