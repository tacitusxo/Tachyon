import urllib.request
import json
import pprint
import sys
from config import load_environment_variables
from kabu_token import get_token
def get_board_info(symbol, token):
    """
    指定されたシンボルの価格情報を取得する
    
    Args:
        symbol (str): 取得したいシンボル（例: '5401@1'）
        token (str): Kabuステーションのトークン
    
    Returns:
        dict: 価格情報
    """
    url = f'http://localhost:18080/kabusapi/board/{symbol}'
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
    api_password, _, _ = load_environment_variables()
    
    # トークンを取得
    token = get_token(api_password)
    
    # ボード情報を取得
    result = get_board_info(8117, token)
    
    if result:
        pprint.pprint(result)
