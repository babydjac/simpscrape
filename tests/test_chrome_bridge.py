import json
import tempfile
import unittest
from pathlib import Path

from core.chrome_bridge import (
    build_native_host_manifest,
    build_storage_state,
    normalize_origin,
    origin_host_slug,
    state_path_for_origin,
    write_storage_state,
)


class ChromeBridgeTests(unittest.TestCase):
    def test_normalize_origin_defaults_to_https(self) -> None:
        self.assertEqual(normalize_origin("simpcity.cr"), "https://simpcity.cr")

    def test_origin_host_slug_uses_hostname(self) -> None:
        self.assertEqual(origin_host_slug("https://forums.example.test:8443/path"), "forums-example-test")

    def test_state_path_matches_existing_autodetect_format(self) -> None:
        self.assertEqual(state_path_for_origin("https://simpcity.cr"), Path.home() / "simpcity-cr-state.json")

    def test_build_native_host_manifest_uses_extension_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            host_path = Path(tmpdir) / "native_host.sh"
            host_path.write_text("#!/bin/sh\n", encoding="utf-8")
            manifest = build_native_host_manifest(
                host_path,
                "abcdefghijklmnopabcdefghijklmnop",
            )

        self.assertEqual(manifest["name"], "com.simpscrape.auth_bridge")
        self.assertEqual(
            manifest["allowed_origins"],
            ["chrome-extension://abcdefghijklmnopabcdefghijklmnop/"],
        )

    def test_write_storage_state_outputs_playwright_cookie_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = write_storage_state(
                "https://simpcity.cr",
                [
                    {
                        "name": "session",
                        "value": "abc123",
                        "domain": ".simpcity.cr",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "lax",
                    }
                ],
                base_dir=Path(tmpdir),
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(output.name, "simpcity-cr-state.json")
        self.assertEqual(payload["origins"][0]["origin"], "https://simpcity.cr")
        self.assertEqual(payload["cookies"][0]["name"], "session")
        self.assertEqual(payload["cookies"][0]["sameSite"], "Lax")
        self.assertEqual(payload["cookies"][0]["expires"], -1)

    def test_build_storage_state_rejects_invalid_origin(self) -> None:
        with self.assertRaises(ValueError):
            build_storage_state("file:///tmp/demo", [])


if __name__ == "__main__":
    unittest.main()
