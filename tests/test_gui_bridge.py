import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gui_bridge


class GuiBridgeAuthTests(unittest.TestCase):
    def test_uses_saved_storage_state_setting(self) -> None:
        old_settings = gui_bridge._SETTINGS_FILE
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                state_file = root / "custom-state.json"
                state_file.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
                settings_file = root / "settings.json"
                settings_file.write_text(
                    f'{{"storage_state": "{state_file}"}}',
                    encoding="utf-8",
                )
                gui_bridge._SETTINGS_FILE = settings_file

                self.assertEqual(gui_bridge._resolve_storage_state_path({}), state_file)
        finally:
            gui_bridge._SETTINGS_FILE = old_settings

    def test_autodetects_storage_state_from_url_host(self) -> None:
        old_settings = gui_bridge._SETTINGS_FILE
        old_repo_root = gui_bridge.REPO_ROOT
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                state_file = root / "auth-example-test-state.json"
                state_file.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
                gui_bridge._SETTINGS_FILE = root / "missing-settings.json"
                gui_bridge.REPO_ROOT = root

                resolved = gui_bridge._resolve_storage_state_path(
                    {"urls": ["https://auth.example.test/threads/demo.1/"]}
                )

                self.assertEqual(resolved, state_file)
        finally:
            gui_bridge._SETTINGS_FILE = old_settings
            gui_bridge.REPO_ROOT = old_repo_root

    def test_keeps_storage_state_available_when_live_chrome_is_requested(self) -> None:
        old_settings = gui_bridge._SETTINGS_FILE
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                state_file = root / "custom-state.json"
                state_file.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
                settings_file = root / "settings.json"
                settings_file.write_text(
                    f'{{"storage_state": "{state_file}"}}',
                    encoding="utf-8",
                )
                gui_bridge._SETTINGS_FILE = settings_file

                resolved = gui_bridge._resolve_storage_state_path({"useChrome": True})

                self.assertEqual(resolved, state_file)
        finally:
            gui_bridge._SETTINGS_FILE = old_settings

    def test_converts_netscape_cookies_to_storage_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cookies_file = root / "session.txt"
            cookies_file.write_text(
                "\n".join(
                    [
                        "# Netscape HTTP Cookie File",
                        "simpcity.cr\tFALSE\t/\tFALSE\t0\tsession\tabc123",
                    ]
                ),
                encoding="utf-8",
            )

            with mock.patch.object(gui_bridge.Path, "home", return_value=root):
                output = gui_bridge._resolve_storage_state_from_cookies(
                    {"urls": ["https://simpcity.cr/threads/demo.1/"]},
                    cookies_file,
                )

            self.assertEqual(output, root / "simpcity-cr-state.json")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["cookies"][0]["name"], "session")
            self.assertEqual(payload["origins"][0]["origin"], "https://simpcity.cr")


if __name__ == "__main__":
    unittest.main()
