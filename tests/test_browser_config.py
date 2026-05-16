import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core.browser_config as browser_config
from core.browser_config import (
    DEFAULT_CHROME_CDP_URL,
    default_chrome_user_data_dir,
    infer_playwright_chrome_channel,
    resolve_chrome_session,
    suppress_saved_auth_if_using_chrome,
)
from core.pipeline import PipelineConfig, _config_manifest


class BrowserConfigTests(unittest.TestCase):
    def test_chrome_flag_defaults_to_local_cdp_url(self) -> None:
        resolved_cdp_url, resolved_user_data_dir = resolve_chrome_session(use_chrome=True)

        self.assertEqual(resolved_cdp_url, DEFAULT_CHROME_CDP_URL)
        self.assertIsNone(resolved_user_data_dir)

    def test_saved_auth_is_disabled_when_using_chrome(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_file = root / "state.json"
            cookies_file = root / "cookies.txt"
            state_file.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
            cookies_file.write_text("", encoding="utf-8")

            storage_state, cookies_path = suppress_saved_auth_if_using_chrome(
                state_file,
                cookies_file,
                DEFAULT_CHROME_CDP_URL,
                None,
            )

        self.assertIsNone(storage_state)
        self.assertIsNone(cookies_path)

    def test_config_manifest_stringifies_chrome_profile_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            payload = _config_manifest(
                PipelineConfig(
                    urls=["https://example.com/thread/1"],
                    workspace=root / "workspace",
                    metadata_dir=root / "meta",
                    download_root=root / "downloads",
                    chrome_user_data_dir=root / "chrome-profile",
                )
            )

        self.assertEqual(payload["chrome_user_data_dir"], str(root / "chrome-profile"))

    def test_default_chrome_user_data_dir_detects_mac_beta_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            beta_dir = home / "Library" / "Application Support" / "Google" / "Chrome Beta"
            beta_dir.mkdir(parents=True)

            with mock.patch.object(browser_config.sys, "platform", "darwin"):
                with mock.patch("core.browser_config.Path.home", return_value=home):
                    resolved = default_chrome_user_data_dir()

        self.assertEqual(resolved, beta_dir)

    def test_infers_playwright_channel_for_beta_profile(self) -> None:
        beta_dir = Path("/Users/test/Library/Application Support/Google/Chrome Beta")

        self.assertEqual(infer_playwright_chrome_channel(beta_dir), "chrome-beta")


if __name__ == "__main__":
    unittest.main()
