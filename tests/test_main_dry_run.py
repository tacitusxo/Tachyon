import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kabucom"))

import main  # noqa: E402

ENV = {"KABUCOM_API_PASSWORD": "x", "AVAILABLE_MARGIN": "1000000", "KABUCOM_ACCOUNT_TYPE": "4"}


class MainDryRunTest(unittest.TestCase):
    def run_main(self, dry_run_value):
        env = dict(ENV)
        if dry_run_value is not None:
            env["UR_DRY_RUN"] = dry_run_value
        scraper = mock.Mock()
        scraper.run_continuous_monitoring.return_value = ["1234"]
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(main, "load_environment_variables",
                                  return_value=("x", 1_000_000.0, 4, 1)), \
                mock.patch.object(main, "get_token", return_value="TOKEN"), \
                mock.patch.object(main, "NikkeiScraper", return_value=scraper), \
                mock.patch.object(main, "get_board_info", return_value={"CurrentPrice": 1000}), \
                mock.patch.object(main, "send_order", return_value=True) as send_order:
            if dry_run_value is None:
                os.environ.pop("UR_DRY_RUN", None)
            main.main()
        return send_order

    def test_default_is_dry_run(self):
        self.run_main(None).assert_not_called()

    def test_dry_run_true_does_not_order(self):
        self.run_main("true").assert_not_called()

    def test_dry_run_false_sends_order(self):
        self.run_main("false").assert_called_once_with("TOKEN", "1234", 4, 900, 1000)

    def test_monitoring_stopped_returns_none(self):
        scraper = mock.Mock()
        scraper.run_continuous_monitoring.return_value = None
        with mock.patch.object(main, "load_environment_variables", return_value=("x", 1e6, 4, 1)), \
                mock.patch.object(main, "get_token", return_value="TOKEN"), \
                mock.patch.object(main, "NikkeiScraper", return_value=scraper):
            main.main()  # 例外にならない


if __name__ == "__main__":
    unittest.main()
