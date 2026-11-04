import urllib.request
import json
import pprint
import logging
from scraper import NikkeiScraper
from order import send_order
from boardGet import get_board_info
from kabu_token import get_token
from config import get_bool_env, load_environment_variables
from utils import calculate_purchase_quantity

# ロギングの設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

def main():
    # 環境変数の読み込み
    api_password, available_margin, account_type, interval_seconds = load_environment_variables()
    # ペーパートレード（UR_DRY_RUN=true、デフォルト）では発注せず、発注内容をログに出すだけ
    dry_run = get_bool_env("UR_DRY_RUN", True)
    if dry_run:
        logger.info("ペーパートレードモードです（発注しません）。実発注するには UR_DRY_RUN=false を設定してください")
    else:
        logger.warning("実発注モードです。上方修正を検出すると成行で買い注文を出します")
    
    # トークンの取得
    token = get_token(api_password)
    
    # トークン発効後にscraperを実行
    logger.info("上方修正監視を開始します...")
    scraper = NikkeiScraper()
    try:
        upward_revision_symbols = scraper.run_continuous_monitoring(
            interval_seconds=interval_seconds
        ) or []  # Ctrl+C で監視を止めた場合は None が返る
        logger.info("\n上方修正された企業のSymbolのリスト:")
        for symbol in upward_revision_symbols:
            logger.info(f"- {symbol}")
            # 上方修正された銘柄に対して発注を実行
            board_info = get_board_info(symbol, token)
            if board_info and 'CurrentPrice' in board_info:
                price = board_info['CurrentPrice']
                logger.info(f"現在の価格: {price}")
                
                # 購入可能な株数を計算
                quantity = calculate_purchase_quantity(price, available_margin)
                if quantity > 0 and dry_run:
                    logger.info(f"[DRY RUN] {symbol} を成行で {quantity}株 買う想定です"
                                f"（現在値 {price}円 × {quantity}株 ≒ {price * quantity:,.0f}円）")
                elif quantity > 0:
                    if send_order(token, symbol, account_type, quantity, price):
                        logger.info(f"{symbol}の発注が成功しました（{quantity}株）")
                    else:
                        logger.error(f"{symbol}の発注に失敗しました")
                else:
                    logger.error(f"{symbol}の購入可能な株数が0株のため、発注をスキップします")
            else:
                logger.error(f"{symbol}の価格情報の取得に失敗しました")
    except KeyboardInterrupt:
        logger.info("\n監視を停止しました")

if __name__ == "__main__":
    main()