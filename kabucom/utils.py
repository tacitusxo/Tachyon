import math
import logging

# ロギングの設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

def calculate_purchase_quantity(price: float, available_margin: float) -> int:
    """
    現在の余力から購入可能な株数を計算する
    
    Args:
        price (float): 購入予定の株価
        available_margin (float): 利用可能な余力
        
    Returns:
        int: 購入可能な株数（100株単位で切り捨て）
    """
    # 余裕を持たせるため、余力の90%までを使用
    usable_margin = available_margin * 0.9
    
    # 購入可能な株数を計算（100株単位で切り捨て）
    quantity = math.floor((usable_margin / price) / 100) * 100
    
    if quantity < 100:
        logger.warning(f"購入可能な株数が100株未満です: {quantity}株")
        return 0
        
    return quantity 

if __name__ == "__main__":
   quantity = calculate_purchase_quantity(100, 1000000)
   logger.info(f"購入可能な株数: {quantity}株")