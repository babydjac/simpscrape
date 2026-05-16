from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


DEFAULT_CHROME_CDP_URL = "http://127.0.0.1:9222"


def normalize_optional_text(value: object) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def resolve_chrome_session(
    *,
    use_chrome: bool = False,
    chrome_cdp_url: Optional[str] = None,
    chrome_user_data_dir: Optional[Path] = None,
) -> tuple[Optional[str], Optional[Path]]:
    resolved_cdp_url = normalize_optional_text(chrome_cdp_url)
    if use_chrome and not resolved_cdp_url:
        resolved_cdp_url = DEFAULT_CHROME_CDP_URL
    return resolved_cdp_url, chrome_user_data_dir


def chrome_session_enabled(
    chrome_cdp_url: Optional[str],
    chrome_user_data_dir: Optional[Path],
) -> bool:
    return bool(normalize_optional_text(chrome_cdp_url) or chrome_user_data_dir)


def suppress_saved_auth_if_using_chrome(
    storage_state: Optional[Path],
    cookies_path: Optional[Path],
    chrome_cdp_url: Optional[str],
    chrome_user_data_dir: Optional[Path],
) -> tuple[Optional[Path], Optional[Path]]:
    if chrome_session_enabled(chrome_cdp_url, chrome_user_data_dir):
        return None, None
    return storage_state, cookies_path


def default_chrome_user_data_dir() -> Optional[Path]:
    home = Path.home()
    candidates: list[Path] = []
    if sys.platform == "darwin":
        app_support = home / "Library" / "Application Support"
        candidates.extend(
            [
                app_support / "Google" / "Chrome",
                app_support / "Google" / "Chrome Beta",
                app_support / "Google" / "Chrome Dev",
                app_support / "Google" / "Chrome Canary",
                app_support / "Chromium",
            ]
        )
    elif sys.platform.startswith("win"):
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            local_root = Path(local_app_data)
            candidates.extend(
                [
                    local_root / "Google" / "Chrome" / "User Data",
                    local_root / "Google" / "Chrome Beta" / "User Data",
                    local_root / "Google" / "Chrome SxS" / "User Data",
                    local_root / "Chromium" / "User Data",
                ]
            )
    else:
        candidates.extend(
            [
                home / ".config" / "google-chrome",
                home / ".config" / "google-chrome-beta",
                home / ".config" / "google-chrome-unstable",
                home / ".config" / "chromium",
            ]
        )

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def infer_playwright_chrome_channel(chrome_user_data_dir: Optional[Path | str]) -> Optional[str]:
    raw = str(chrome_user_data_dir or "").strip().lower()
    if not raw:
        return None
    if "chrome beta" in raw or "google-chrome-beta" in raw:
        return "chrome-beta"
    if "chrome dev" in raw or "google-chrome-unstable" in raw:
        return "chrome-dev"
    if "chrome canary" in raw or "chrome sxs" in raw:
        return "chrome-canary"
    if "chromium" in raw:
        return None
    return "chrome"
