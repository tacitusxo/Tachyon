import pytest
from unittest.mock import patch, MagicMock
from kabucom.scraper import NikkeiScraper

def test_detect_upward_revisions_with_upward_revision():
    # 上方修正を含むページのモックレスポンス
    mock_response = MagicMock()
    mock_response.text = """
    <div class="disclosure-list">
        <li class="market-table__row-wrapper">
            <div class="market-table__col-company-name--bold">6013 タクマ</div>
            <div class="market-table__col-timestamp">15:30</div>
            <a class="market-table__col-item market-table__col-desc-link">第14次中期経営計画における財務目標の上方修正に関するお知らせ</a>
        </li>
    </div>
    """
    
    with patch('requests.Session.get', return_value=mock_response):
        scraper = NikkeiScraper()
        scraper.is_initial_load = False
        result = scraper.detect_upward_revisions()
        assert len(result) > 0
        assert "上方修正" in result[0]["title"]

def test_detect_upward_revisions_without_upward_revision():
    # 上方修正を含まないページのモックレスポンス
    mock_response = MagicMock()
    mock_response.text = """
    <div class="disclosure-list">
        <li class="market-table__row-wrapper">
            <div class="market-table__col-company-name--bold">6013 タクマ</div>
            <div class="market-table__col-timestamp">15:30</div>
            <a class="market-table__col-item market-table__col-desc-link">ご案内</a>
        </li>
    </div>
    """
    
    with patch('requests.Session.get', return_value=mock_response):
        scraper = NikkeiScraper()
        scraper.is_initial_load = False
        result = scraper.detect_upward_revisions()
        assert len(result) == 0

def test_detect_upward_revisions_with_empty_page():
    # 空のページのモックレスポンス
    mock_response = MagicMock()
    mock_response.text = "<div class='disclosure-list'></div>"
    
    with patch('requests.Session.get', return_value=mock_response):
        scraper = NikkeiScraper()
        scraper.is_initial_load = False
        result = scraper.detect_upward_revisions()
        assert len(result) == 0

def test_detect_upward_revisions_with_network_error():
    # ネットワークエラーのシミュレーション
    with patch('requests.Session.get', side_effect=Exception("Network error")):
        scraper = NikkeiScraper()
        scraper.is_initial_load = False
        result = scraper.detect_upward_revisions()
        assert len(result) == 0

def run_all():
    print("test_detect_upward_revisions_with_upward_revision ...", end=" ")
    test_detect_upward_revisions_with_upward_revision()
    print("OK")

    print("test_detect_upward_revisions_without_upward_revision ...", end=" ")
    test_detect_upward_revisions_without_upward_revision()
    print("OK")

    print("test_detect_upward_revisions_with_empty_page ...", end=" ")
    test_detect_upward_revisions_with_empty_page()
    print("OK")

    print("test_detect_upward_revisions_with_network_error ...", end=" ")
    test_detect_upward_revisions_with_network_error()
    print("OK")

if __name__ == "__main__":
    run_all()
