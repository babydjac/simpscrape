import tempfile
import unittest
from pathlib import Path
from unittest import mock

import drivers.playwright_driver as playwright_driver


class PlaywrightDriverTests(unittest.TestCase):
    def test_stops_playwright_when_chrome_session_init_fails(self) -> None:
        fake_runner = mock.Mock()
        fake_playwright = mock.Mock()
        fake_browser_type = mock.Mock()
        fake_runner.start.return_value = fake_playwright
        fake_playwright.chromium = fake_browser_type
        fake_browser_type.connect_over_cdp.side_effect = Exception("cdp unavailable")
        fake_browser_type.launch_persistent_context.side_effect = Exception("profile locked")

        with tempfile.TemporaryDirectory() as tmpdir:
            user_data_dir = Path(tmpdir)
            with mock.patch.object(playwright_driver, "sync_playwright", return_value=fake_runner):
                with self.assertRaises(RuntimeError):
                    playwright_driver.PlaywrightDriver(
                        headless=True,
                        chrome_cdp_url="http://127.0.0.1:9222",
                        chrome_user_data_dir=str(user_data_dir),
                        chrome_profile_directory="Default",
                    )

        fake_playwright.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
