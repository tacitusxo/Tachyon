import requests
import re
import time
import logging
import os
from datetime import datetime
from bs4 import BeautifulSoup
from typing import List, Dict, Optional

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class NikkeiScraper:
    def __init__(self):
        self.base_url = (
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

        # 適時開示のリスト項目を探す
        disclosure_items = soup.find_all(
            ["div", "tr", "li"],
            class_=re.compile(r"(disclosure|release|announcement)", re.I),
        )

        for item in disclosure_items:
            announcement = self.extract_announcement_info(item)
            if announcement:
                announcements.append(announcement)

        return announcements

    def extract_announcement_info(self, item) -> Optional[Dict]:
        """個別の適時開示情報を抽出する"""
        try:
            # テキスト内容を取得
            text = item.get_text(strip=True)

            # 銘柄コードを抽出（4桁の数字パターン）
            symbol_match = re.search(r"\b(\d{4})\b", text)
            symbol = symbol_match.group(1) if symbol_match else None

            # 会社名を抽出
            company_name = self.extract_company_name(text)

            # 時刻を抽出
            time_match = re.search(r"(\d{1,2}:\d{2})", text)
            announcement_time = time_match.group(1) if time_match else None

            if symbol or company_name:
                return {
                    "symbol": symbol,
                    "company_name": company_name,
                    "title": text[:100],  # 最初の100文字
                    "full_text": text,
                    "time": announcement_time,
                    "timestamp": datetime.now().isoformat(),
                }
        except Exception as e:
            logger.warning(f"発表情報の抽出エラー: {e}")

        return None

    def extract_company_name(self, text: str) -> Optional[str]:
        """テキストから会社名を抽出する"""
        # 株式会社、（株）などのパターンを含む会社名を抽出
        patterns = [
            r"([株式会社|㈱]\s*[一-龯ァ-ヶー]+)",
            r"([一-龯ァ-ヶー]+\s*[株式会社|㈱])",
            r"([A-Z][a-zA-Z\s&]+(?:Corporation|Corp|Inc|Ltd|Co)\.?)",
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()

        return None

    def is_upward_revision(self, text: str) -> bool:
        """上方修正かどうかを判定する"""
        upward_keywords = [
            "上方修正",
            "業績予想の修正",
            "増益",
            "増収増益",
            "予想を上回",
            "上振れ",
            "業績好調",
            "予想上方",
            "上方",
            "増額修正",
            "アップサイド",
            "上昇修正",
            "好業績",
            "増配",
        ]

        text_lower = text.lower()
        return any(keyword in text for keyword in upward_keywords)

    def detect_upward_revisions(self) -> List[Dict]:
        """上方修正のニュースを検出する"""
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
                continue

            # 上方修正かチェック
            if self.is_upward_revision(announcement["full_text"]):
                upward_revisions.append(announcement)
                logger.info(
                    f"上方修正を検出: {announcement['company_name']} ({announcement['symbol']})"
                )

            self.processed_announcements.add(announcement_id)

        return upward_revisions

    def run_continuous_monitoring(self, interval_minutes: int = 5) -> List[str]:
        """継続的な監視を実行する

        Returns:
            List[str]: 上方修正された企業のSymbolのリスト
        """
        logger.info(f"上方修正監視を開始します（{interval_minutes}分間隔）")

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

                time.sleep(interval_minutes * 60)

            except KeyboardInterrupt:
                logger.info("監視を停止します")
                break
            except Exception as e:
                logger.error(f"監視中にエラーが発生: {e}")
                time.sleep(60)  # エラー時は1分待機

        return upward_revision_symbols

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

        # ここでkabuステーションAPIを使った処理を追加可能
        if symbol:
            logger.info(f"銘柄 {symbol} の監視を開始します")


if __name__ == "__main__":
    scraper = NikkeiScraper()

    # 継続監視を開始
    upward_revision_symbols = scraper.run_continuous_monitoring(interval_minutes=5)
    logger.info("上方修正された企業のSymbolのリスト:", upward_revision_symbols)
