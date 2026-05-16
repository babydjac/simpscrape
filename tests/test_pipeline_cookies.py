import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.pipeline import _filter_storage_state_cookie_overrides, crawl_records, load_cookies


class CookieLoadingTests(unittest.TestCase):
    def test_netscape_session_cookie_expiry_zero_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cookie_file = Path(tmpdir) / "cookies.txt"
            cookie_file.write_text(
                "simpcity.cr\tFALSE\t/\tTRUE\t0\togaddgmetaprof_session\tabc123\n",
                encoding="utf-8",
            )

            cookies = load_cookies(cookie_file)

        self.assertEqual(cookies[0]["expires"], -1)

    def test_netscape_persistent_cookie_keeps_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cookie_file = Path(tmpdir) / "cookies.txt"
            cookie_file.write_text(
                "simpcity.cr\tFALSE\t/\tTRUE\t1808382424\togaddgmetaprof_user\tabc123\n",
                encoding="utf-8",
            )

            cookies = load_cookies(cookie_file)

        self.assertEqual(cookies[0]["expires"], 1808382424)

    def test_storage_state_cookies_take_precedence_over_cookie_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(
                """
                {
                  "cookies": [
                    {
                      "name": "ogaddgmetaprof_session",
                      "value": "fresh",
                      "domain": "simpcity.cr",
                      "path": "/"
                    }
                  ],
                  "origins": []
                }
                """,
                encoding="utf-8",
            )
            cookies = [
                {
                    "name": "ogaddgmetaprof_session",
                    "value": "stale",
                    "domain": ".simpcity.cr",
                    "path": "/",
                },
                {
                    "name": "unrelated",
                    "value": "kept",
                    "domain": "simpcity.cr",
                    "path": "/",
                },
            ]

            filtered = _filter_storage_state_cookie_overrides(cookies, state_file)

        self.assertEqual(
            filtered,
            [
                {
                    "name": "unrelated",
                    "value": "kept",
                    "domain": "simpcity.cr",
                    "path": "/",
                }
            ],
        )

    def test_crawl_records_prefers_saved_auth_fallback_over_profile_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
            chrome_profile = Path(tmpdir) / "chrome-profile"
            chrome_profile.mkdir()
            fake_driver = object()
            fake_dom = mock.Mock()
            fake_controller = mock.Mock()
            fake_controller.crawl.return_value = []

            with (
                mock.patch("core.pipeline.PlaywrightDriver", side_effect=[RuntimeError("chrome failed"), fake_driver]) as driver_cls,
                mock.patch("core.pipeline.DomExecutor", return_value=fake_dom),
                mock.patch("core.pipeline.CrawlController", return_value=fake_controller),
            ):
                result = crawl_records(
                    sitemap_model=mock.Mock(),
                    headless=True,
                    delay_ms=0,
                    max_pages=None,
                    storage_state=state_file,
                    cookies_path=None,
                    nav_timeout_ms=60000,
                    idle_timeout_ms=5000,
                    chrome_cdp_url="http://127.0.0.1:9222",
                    chrome_user_data_dir=chrome_profile,
                )

        self.assertEqual(result, [])
        self.assertEqual(driver_cls.call_count, 2)
        first_call = driver_cls.call_args_list[0].kwargs
        second_call = driver_cls.call_args_list[1].kwargs
        self.assertIsNone(first_call["storage_state"])
        self.assertIsNone(first_call["cookies"])
        self.assertEqual(first_call["chrome_cdp_url"], "http://127.0.0.1:9222")
        self.assertIsNone(first_call["chrome_user_data_dir"])
        self.assertEqual(second_call["storage_state"], str(state_file))
        self.assertIsNone(second_call["chrome_cdp_url"])
        self.assertIsNone(second_call["chrome_user_data_dir"])
        fake_dom.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
