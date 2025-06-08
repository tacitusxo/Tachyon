from kabucom.scraper import NikkeiScraper


test_revision = {
    "symbol": "1234",
    "company_name": "テスト株式会社",
    "title": "業績予想の上方修正に関するお知らせ",
    "time": "15:00"
}
scraper = NikkeiScraper()
# テスト用のWebhook URLをここにセットしてください（本番用は絶対に使わないでください）
scraper.slack_webhook_url = "https://hooks.slack.com/services/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"
scraper.send_slack_notification(test_revision)
