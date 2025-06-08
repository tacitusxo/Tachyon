import os
import logging
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

def load_environment_variables():
    """環境変数を読み込む"""
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
    available_margin = float(os.getenv("AVAILABLE_MARGIN", "0"))
    account_type = int(os.getenv("KABUCOM_ACCOUNT_TYPE", "2"))

    if api_password is None:
        logger.error("Error: KABUCOM_API_PASSWORD environment variable is not set")
        exit(1)

    if available_margin <= 0:
        logger.error("Error: AVAILABLE_MARGIN environment variable is not set or invalid")
        exit(1)

    if account_type is None:
        logger.error("Error: KABUCOM_ACCOUNT_TYPE environment variable is not set")
        exit(1)

    logger.info(f"APIパスワード: {api_password}")
    logger.info(f"利用可能な余力: {available_margin:,.0f}円")
    logger.info(f"口座種別: {account_type}")

    return api_password, available_margin, account_type 