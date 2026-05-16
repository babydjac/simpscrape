from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


CHROME_BRIDGE_HOST_NAME = "com.simpscrape.auth_bridge"
BRIDGE_STATE_DIR = Path.home()
BRIDGE_META_DIR = Path.home() / ".simpscrape_chrome_bridge"
_ALLOWED_SCHEMES = {"http", "https"}
_SUPPORTED_BROWSERS = {"chrome", "chrome-for-testing", "chromium"}


def normalize_origin(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise ValueError("Origin is required.")
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Unsupported origin scheme: {parsed.scheme or '(missing)'}")
    if not parsed.hostname:
        raise ValueError("Origin must include a hostname.")
    host = parsed.hostname.lower()
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    return f"{parsed.scheme}://{netloc}"


def origin_host_slug(origin: str) -> str:
    parsed = urlparse(normalize_origin(origin))
    host = (parsed.hostname or "").lower()
    slug = "".join(ch if ch.isalnum() else "-" for ch in host).strip("-")
    return slug or "site"


def state_path_for_origin(origin: str, base_dir: Path = BRIDGE_STATE_DIR) -> Path:
    return base_dir / f"{origin_host_slug(origin)}-state.json"


def browser_native_host_manifest_dir(browser: str = "chrome") -> Path:
    key = (browser or "chrome").strip().lower()
    if key not in _SUPPORTED_BROWSERS:
        raise ValueError(
            f"Unsupported browser: {browser}. Choose from: {', '.join(sorted(_SUPPORTED_BROWSERS))}"
        )

    home = Path.home()
    if sys.platform == "darwin":
        mapping = {
            "chrome": home / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts",
            "chrome-for-testing": home / "Library" / "Application Support" / "Google" / "ChromeForTesting" / "NativeMessagingHosts",
            "chromium": home / "Library" / "Application Support" / "Chromium" / "NativeMessagingHosts",
        }
        return mapping[key]

    if sys.platform.startswith("linux"):
        mapping = {
            "chrome": home / ".config" / "google-chrome" / "NativeMessagingHosts",
            "chrome-for-testing": home / ".config" / "google-chrome-for-testing" / "NativeMessagingHosts",
            "chromium": home / ".config" / "chromium" / "NativeMessagingHosts",
        }
        return mapping[key]

    raise RuntimeError(
        "Native host auto-install is implemented for macOS and Linux in this repo. "
        "Windows requires a registry entry."
    )


def build_native_host_manifest(host_script_path: Path, extension_id: str) -> dict[str, Any]:
    host_path = host_script_path.expanduser().resolve()
    ext_id = (extension_id or "").strip()
    if not ext_id:
        raise ValueError("extension_id is required.")
    if not host_path.exists():
        raise ValueError(f"Native host launcher not found: {host_path}")
    return {
        "name": CHROME_BRIDGE_HOST_NAME,
        "description": "SimpScrape Chrome auth bridge",
        "path": str(host_path),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/"],
    }


def install_native_host_manifest(
    *,
    browser: str,
    extension_id: str,
    host_script_path: Path,
) -> Path:
    manifest_dir = browser_native_host_manifest_dir(browser)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"{CHROME_BRIDGE_HOST_NAME}.json"
    payload = build_native_host_manifest(host_script_path, extension_id)
    manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return manifest_path


def chrome_cookie_to_playwright(cookie: dict[str, Any]) -> dict[str, Any]:
    name = str(cookie.get("name") or "").strip()
    domain = str(cookie.get("domain") or "").strip()
    if not name or not domain:
        raise ValueError("Cookie must include name and domain.")

    path = str(cookie.get("path") or "/") or "/"
    secure = bool(cookie.get("secure"))
    http_only = bool(cookie.get("httpOnly"))
    expires_raw = cookie.get("expirationDate")
    expires = int(float(expires_raw)) if isinstance(expires_raw, (int, float)) else -1

    same_site_raw = str(cookie.get("sameSite") or "").strip().lower()
    same_site_map = {
        "no_restriction": "None",
        "norestriction": "None",
        "none": "None",
        "lax": "Lax",
        "strict": "Strict",
    }

    payload = {
        "name": name,
        "value": str(cookie.get("value") or ""),
        "domain": domain,
        "path": path,
        "secure": secure,
        "httpOnly": http_only,
        "expires": expires if expires > 0 else -1,
    }
    mapped_same_site = same_site_map.get(same_site_raw)
    if mapped_same_site:
        payload["sameSite"] = mapped_same_site
    return payload


def build_storage_state(origin: str, cookies: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_origin = normalize_origin(origin)
    return {
        "cookies": [chrome_cookie_to_playwright(cookie) for cookie in cookies],
        "origins": [{"origin": normalized_origin, "localStorage": []}],
    }


def write_storage_state(origin: str, cookies: list[dict[str, Any]], base_dir: Path = BRIDGE_STATE_DIR) -> Path:
    output_path = state_path_for_origin(origin, base_dir=base_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_storage_state(origin, cookies)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return output_path


def write_bridge_meta(origin: str, details: dict[str, Any], meta_dir: Path = BRIDGE_META_DIR) -> Path:
    meta_dir.mkdir(parents=True, exist_ok=True)
    index_path = meta_dir / "status.json"
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload[normalize_origin(origin)] = details
    index_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return index_path


def remove_storage_state(origin: str, base_dir: Path = BRIDGE_STATE_DIR) -> Optional[Path]:
    output_path = state_path_for_origin(origin, base_dir=base_dir)
    if output_path.exists():
        output_path.unlink()
        return output_path
    return None
