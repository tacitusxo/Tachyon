import requests
import re
import time
import logging
import os
from datetime import datetime
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
from unittest.mock import patch, MagicMock
import argparse
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# 環境変数の読み込み
try:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        logger.info(f"Loading .env from: {env_path}")
        load_dotenv(dotenv_path=env_path)
    else:
        logger.info("No .env file found, using system environment variables")
except Exception as e:
    logger.warning(f"Failed to load .env file: {e}")

class NikkeiScraper:
    def __init__(self, test_url: Optional[str] = None):
        self.base_url = test_url or (
            "https://r.nikkei.com/markets/release/disclosure?volume=80&hasImage=false"
        )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
        )
        self.processed_announcements = set()
        self.is_initial_load = True
        self.slack_webhook_url = os.getenv("SLACK_WEBHOOK_URL")

    def fetch_page(self) -> Optional[str]:
        """ページを取得する"""
        try:
            response = self.session.get(self.base_url, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            logger.error(f"ページ取得エラー: {e}")
            return None

    def parse_announcements(self, html_content: str) -> List[Dict]:
        """HTMLから適時開示情報を解析する"""
        soup = BeautifulSoup(html_content, "html.parser")
        announcements = []

        # market-table__row-wrapperクラスを持つli要素を探す
        disclosure_items = soup.find_all("li", class_="market-table__row-wrapper")

        for item in disclosure_items:
            announcement = self.extract_announcement_info(item)
            if announcement:
                announcements.append(announcement)

        return announcements

    def extract_announcement_info(self, item) -> Optional[Dict]:
        """個別の適時開示情報を抽出する"""
        try:

            # market-table__col-company-name--boldクラスから銘柄コードと会社名を抽出
            company_element = item.find(class_="market-table__col-company-name--bold")
            symbol = None
            company_name = None
            if company_element:
                company_text = company_element.get_text(strip=True)
                # 括弧内の銘柄コード（数字とアルファベットの組み合わせ）を抽出
                symbol_match = re.search(r"[\(（]([0-9A-Za-z]+)[\)）]", company_text)
                symbol = symbol_match.group(1) if symbol_match else None
                # 会社名を抽出（括弧部分を除去）
                company_name = re.sub(r"[\(（][0-9A-Za-z]+[\)）]", "", company_text).strip()

            # 時刻を抽出
            time_element = item.find(class_="market-table__col-timestamp")
            announcement_time = time_element.get_text(strip=True) if time_element else None

            # 特定のクラスを持つaタグからタイトルを取得
            a_tag = item.find('a', class_="market-table__col-item market-table__col-desc-link")
            title = a_tag.get_text(strip=True) if a_tag else None

            if symbol or company_name:
                return {
                    "symbol": symbol,
                    "company_name": company_name,
                    "title": title,
                    "time": announcement_time,
                    "timestamp": datetime.now().isoformat(),
                }
        except Exception as e:
            logger.warning(f"発表情報の抽出エラー: {e}")

        return None

    def is_upward_revision(self, text: str) -> bool:
        """上方修正かどうかを判定する"""
        upward_keywords = [
            "上方修正",
        ]

        return any(keyword in text for keyword in upward_keywords)

    def detect_upward_revisions(self, url: Optional[str] = None) -> List[Dict]:
        """上方修正のニュースを検出する"""
        try:
            # URLが指定された場合は一時的にbase_urlを変更
            original_url = self.base_url
            if url:
                self.base_url = url

            html_content = self.fetch_page()
            if not html_content:
                return []

            announcements = self.parse_announcements(html_content)

            upward_revisions = []

            for announcement in announcements:
                # 新しい発表かチェック
                announcement_id = (
                    f"{announcement.get('symbol', '')}-{announcement.get('title', '')[:50]}"
                )

                # 初回読み込み時は既存のニュースをすべて処理済みとして記録するが、通知はしない
                if self.is_initial_load:
                    self.processed_announcements.add(announcement_id)
                    continue

                if announcement_id in self.processed_announcements:
                    if announcement_id == f"{announcements[0].get('symbol', '')}-{announcements[0].get('title', '')[:50]}":
                        logger.info(
                            f"既に処理済みの発表: {announcement['company_name']} ({announcement['symbol']}) - {announcement['title'][:50]}..."
                        )
                    continue

                # 上方修正かチェック
                if self.is_upward_revision(announcement["title"]):
                    upward_revisions.append(announcement)
                    logger.info(
                        f"上方修正を検出: {announcement['company_name']} ({announcement['symbol']}) - {announcement['title'][:50]}..."
                    )
                else:
                    logger.info(
                        f"上方修正ではない: {announcement['company_name']} ({announcement['symbol']}) - {announcement['title'][:50]}..."
                    )
                self.processed_announcements.add(announcement_id)

            # URLを元に戻す
            if url:
                self.base_url = original_url

            return upward_revisions
        except Exception as e:
            logger.error(f"上方修正の検出中にエラーが発生: {e}")
            return []

    def run_continuous_monitoring(self, interval_seconds: int = 30) -> List[str]:
        """継続的な監視を実行する

        Args:
            interval_seconds (int): 監視間隔（秒）

        Returns:
            List[str]: 上方修正された企業のSymbolのリスト
        """
        logger.info(f"上方修正監視を開始します（{interval_seconds}秒間隔）")

        # 上方修正された企業のSymbolを保持するリスト
        upward_revision_symbols = []

        # 初回読み込みで既存のニュースを記録
        logger.info("初回読み込み中...")
        self.detect_upward_revisions()
        self.is_initial_load = False
        logger.info("初回読み込み完了。新しい上方修正のニュースを監視開始します。")

        while True:
            try:
                upward_revisions = self.detect_upward_revisions()

                if upward_revisions:
                    for revision in upward_revisions:
                        self.handle_detected_revision(revision)
                        # Symbolをリストに追加
                        if revision.get("symbol"):
                            upward_revision_symbols.append(revision["symbol"])
                            return upward_revision_symbols

                time.sleep(interval_seconds)

            except KeyboardInterrupt:
                logger.info("監視を停止します")
                break
            except Exception as e:
                logger.error(f"監視中にエラーが発生: {e}")
                time.sleep(10)  # エラー時は10秒待機

    def send_slack_notification(self, revision: Dict):
        """Slackに通知を送信する"""
        if not self.slack_webhook_url:
            logger.warning("SLACK_WEBHOOK_URLが設定されていません")
            return

        message = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "🔔 上方修正の検出",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*銘柄コード:*\n{revision.get('symbol', 'N/A')}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*会社名:*\n{revision.get('company_name', 'N/A')}"
                        }
                    ]
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*タイトル:*\n{revision.get('title', 'N/A')}"
                    }
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"*時刻:* {revision.get('time', 'N/A')}"
                        }
                    ]
                }
            ]
        }

        try:
            response = requests.post(
                self.slack_webhook_url,
                json=message,
                timeout=10
            )
            response.raise_for_status()
            logger.info("Slack通知を送信しました")
        except Exception as e:
            logger.error(f"Slack通知の送信に失敗: {e}")

    def handle_detected_revision(self, revision: Dict):
        """検出された上方修正を処理する"""
        symbol = revision.get("symbol")
        company_name = revision.get("company_name")

        logger.info(f"=== 上方修正検出 ===")
        logger.info(f"銘柄コード: {symbol}")
        logger.info(f"会社名: {company_name}")
        logger.info(f"タイトル: {revision.get('title')}")
        logger.info(f"時刻: {revision.get('time')}")

        # Slackに通知を送信
        self.send_slack_notification(revision)

if __name__ == "__main__":
    upward_url = "https://r.nikkei.com/markets/release/disclosure?hasImage=false&keyword=%E3%81%8A%E7%9F%A5%E3%82%89%E3%81%9B&volume=10"
    scraper_upward = NikkeiScraper(test_url=upward_url)
    
    # 初回読み込み（is_initial_load = True）
    logger.info("初回読み込みを実行します...")
    upward_revisions = scraper_upward.detect_upward_revisions()
    if upward_revisions:
        logger.info(f"初回読み込みで上方修正を{len(upward_revisions)}件検出しました：")
        for revision in upward_revisions:
            scraper_upward.handle_detected_revision(revision)
    else:
        logger.info("初回読み込みでは上方修正は検出されませんでした")

    # is_initial_loadをFalseに設定
    scraper_upward.is_initial_load = False
    
    # 2回目の読み込み（is_initial_load = False）
    logger.info("\n2回目の読み込みを実行します...")
    # 2回目の読み込み用のURLを設定
    second_url = "https://r.nikkei.com/markets/release/disclosure?hasImage=false&keyword=%E4%B8%8A%E6%96%B9%E4%BF%AE%E6%AD%A3&volume=10"
    upward_revisions = scraper_upward.detect_upward_revisions(url=second_url)
    if upward_revisions:
        logger.info(f"2回目の読み込みで上方修正を{len(upward_revisions)}件検出しました：")
        for revision in upward_revisions:
            scraper_upward.handle_detected_revision(revision)
    else:
        logger.info("2回目の読み込みでは上方修正は検出されませんでした")
