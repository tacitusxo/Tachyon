import urllib.request
import json
import pprint
import sys
import os
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

def get_board_info(symbol):
    """
    指定されたシンボルの価格情報を取得する
    
    Args:
        symbol (str): 取得したいシンボル（例: '5401@1'）
    
    Returns:
        dict: 価格情報
    """
    url = f'http://localhost:18080/kabusapi/board/{symbol}'
    req = urllib.request.Request(url, method='GET')
    req.add_header('Content-Type', 'application/json')
    req.add_header('X-API-KEY', os.getenv('KABUCOM_API_KEY'))

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

def main():
    if len(sys.argv) != 2:
        print('Usage: python boardGet.py <symbol>')
        print('Example: python boardGet.py 5401@1')
        sys.exit(1)

    symbol = sys.argv[1]
    result = get_board_info(symbol)
    
    if result:
        pprint.pprint(result)

if __name__ == '__main__':
    main()
