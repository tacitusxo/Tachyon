import urllib.request
import json
import pprint
import logging
from config import load_environment_variables

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

def get_token(api_password: str) -> str:
    """
    kabu.comのAPIトークンを取得する
    
    Args:
        api_password (str): APIパスワード
        
    Returns:
        str: 取得したトークン
        
    Raises:
        SystemExit: トークンの取得に失敗した場合
    """
    obj = {"APIPassword": api_password}
    json_data = json.dumps(obj).encode("utf8")
    url = "http://localhost:18080/kabusapi/token"
    
    req = urllib.request.Request(url, json_data, method="POST")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req) as res:
            content = json.loads(res.read())
            token = content.get("Token")
            
            if not token:
                logger.error("トークンの取得に失敗しました")
                exit(1)
                
            logger.info("トークンの発行に成功しました")
            logger.info(token)
            return token
            
    except urllib.error.HTTPError as e:
        logger.error(e)
        content = json.loads(e.read())
        logger.error(pprint.pformat(content))
        exit(1)
    except Exception as e:
        logger.error(e)
        exit(1)

def main():
    """
    メイン関数
    """
    # 環境変数の読み込み
    api_password, _, _, _ = load_environment_variables()
    
    # トークンの取得
    token = get_token(api_password)
    return token

if __name__ == "__main__":
    main() 