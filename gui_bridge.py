#!/usr/bin/env python3
"""Bridge between Electron GUI and Python pipeline.

Reads a JSON config from stdin, runs the universal pipeline,
and streams JSON events to stdout for Electron to consume.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

# Piped stdout is block-buffered by default — Electron would batch JSON until the buffer fills.
os.environ.setdefault("PYTHONUNBUFFERED", "1")
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

from core.pipeline import (
    PipelineConfig,
    PipelineError,
    PipelineEvent,
    load_cookies,
    parse_cli_args,
    run_universal_pipeline,
)
from core.browser_config import resolve_chrome_session
from core.chrome_bridge import normalize_origin, write_storage_state


REPO_ROOT = Path(__file__).resolve().parent
_DOWNLOADS_AUTH_COOKIES = Path.home() / "Downloads" / "e9ca2c61-8d73-4fdf-81a4-190a29bcaf36.txt"
_DEFAULT_COOKIES_FILE = REPO_ROOT / "cooks.txt"
_SETTINGS_FILE = Path.home() / ".simpscrape_gui_settings.json"


def _slug_folder_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (value or "").strip()) or "performer"


def _resolve_cookies_path(raw: dict[str, Any]) -> Optional[Path]:
    """Match CLI/Tk behavior: use explicit path from JSON, else repo cooks.txt if present."""
    explicit = (raw.get("cookies") or raw.get("cookiesPath") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.exists() else None
    if _DOWNLOADS_AUTH_COOKIES.exists():
        return _DOWNLOADS_AUTH_COOKIES
    return _DEFAULT_COOKIES_FILE if _DEFAULT_COOKIES_FILE.exists() else None


def _primary_origin(raw: dict[str, Any]) -> str:
    urls = raw.get("urls", [])
    if isinstance(urls, list):
        for value in urls:
            try:
                return normalize_origin(str(value))
            except Exception:
                continue
    return "https://simpcity.cr"


def _resolve_storage_state_from_cookies(raw: dict[str, Any], cookies: Optional[Path]) -> Optional[Path]:
    if not cookies or cookies.suffix.lower() != ".txt":
        return None
    try:
        parsed = load_cookies(cookies)
    except Exception:
        return None
    if not parsed:
        return None
    try:
        return write_storage_state(_primary_origin(raw), parsed, base_dir=Path.home())
    except Exception:
        return None


def _existing_path(value: Any) -> Optional[Path]:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.exists():
        return path
    if not path.is_absolute():
        repo_path = REPO_ROOT / path
        if repo_path.exists():
            return repo_path
    return None


def _saved_storage_state_path() -> Optional[Path]:
    try:
        payload = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return _existing_path(payload.get("storage_state") or payload.get("storageState"))


def _state_names_for_urls(urls: list[str]) -> list[str]:
    names: list[str] = []
    for url in urls:
        try:
            host = urlparse(url).hostname or ""
        except Exception:
            host = ""
        slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
        if slug and f"{slug}-state.json" not in names:
            names.append(f"{slug}-state.json")
    return names


def _resolve_storage_state_save_path(raw: dict[str, Any], loaded_state: Optional[Path]) -> Path:
    """Where to persist the post-dismissal storage_state. Defaults to the same
    path the state was loaded from, falling back to a per-host file under
    ~/.simpscrape so a fresh user still benefits from the gate-cleared cookies.
    """
    if loaded_state is not None:
        return loaded_state
    explicit = (raw.get("storageState") or raw.get("storage_state") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    urls = raw.get("urls", [])
    if not isinstance(urls, list):
        urls = []
    candidate_names = _state_names_for_urls([str(url) for url in urls])
    name = candidate_names[0] if candidate_names else "simpscrape-state.json"
    return Path.home() / ".simpscrape" / name


def _resolve_storage_state_path(raw: dict[str, Any]) -> Optional[Path]:
    explicit = _existing_path(raw.get("storageState") or raw.get("storage_state"))
    if explicit:
        return explicit

    saved = _saved_storage_state_path()
    if saved:
        return saved

    urls = raw.get("urls", [])
    if not isinstance(urls, list):
        urls = []
    candidate_names = _state_names_for_urls([str(url) for url in urls])
    candidate_names.extend(["simpcity-cr-state.json", "simpcity-state.json"])
    seen: set[str] = set()
    for name in candidate_names:
        if name in seen:
            continue
        seen.add(name)
        for base in (Path.home(), REPO_ROOT):
            path = base / name
            if path.exists():
                return path
    return None


def emit(payload: dict[str, Any]) -> None:
    try:
        sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        sys.exit(0)


def build_config(raw: dict[str, Any]) -> PipelineConfig:
    urls = raw.get("urls", [])
    performer = (raw.get("performer") or "performer").strip() or "performer"
    base_dir = Path(raw.get("downloadsDir", str(Path.home() / "Downloads")))
    collection_root = base_dir / _slug_folder_name(performer)

    chrome_cdp_url, chrome_user_data_dir = resolve_chrome_session(
        use_chrome=bool(raw.get("useChrome") or raw.get("chrome")),
        chrome_cdp_url=str(raw.get("chromeCdpUrl") or raw.get("chrome_cdp_url") or "").strip() or None,
        chrome_user_data_dir=_existing_path(raw.get("chromeUserDataDir") or raw.get("chrome_user_data_dir")),
    )

    cookies = _resolve_cookies_path(raw)
    storage_state = _resolve_storage_state_path(raw)
    if storage_state is None:
        storage_state = _resolve_storage_state_from_cookies(raw, cookies)
    storage_state_save_path = _resolve_storage_state_save_path(raw, storage_state)

    gallery_args = parse_cli_args(raw.get("galleryArgs", "--no-colors"), "gallery-args")
    yt_dlp_args = parse_cli_args(raw.get("ytDlpArgs", "--no-warnings --ignore-errors"), "yt-dlp-args")

    capture_profile = str(raw.get("captureProfile") or raw.get("capture_profile") or "balanced").strip().lower()
    if capture_profile not in ("fast", "balanced", "deep"):
        capture_profile = "balanced"

    return PipelineConfig(
        urls=urls,
        workspace=collection_root,
        metadata_dir=collection_root / "_meta",
        download_root=collection_root,
        run_scoped_outputs=True,
        run_label=performer,
        structured_downloads=False,
        capture_profile=capture_profile,
        include_source_hosts=bool(raw.get("includeSourceHosts", False)),
        no_download=False,
        headless=bool(raw.get("headless", True)),
        delay_ms=int(raw.get("delay", 250)),
        max_pages=int(raw["maxPages"]) if raw.get("maxPages") else None,
        crawl_jobs=int(raw.get("crawlJobs", 2)),
        nav_timeout_ms=int(raw.get("navTimeout", 60000)),
        idle_timeout_ms=int(raw.get("idleTimeout", 5000)),
        emit_browser_preview=bool(raw.get("browserPreview", True)),
        download_workers=int(raw.get("downloadWorkers", 8)),
        attempts=int(raw.get("attempts", 3)),
        retry_delay=float(raw.get("retryDelay", 3.0)),
        storage_state=storage_state,
        cookies=cookies,
        gallery_dl_path=shutil.which("gallery-dl"),
        yt_dlp_path=shutil.which("yt-dlp"),
        gallery_args=list(gallery_args),
        yt_dlp_args=list(yt_dlp_args),
        resolve_links=bool(raw.get("resolveLinks", True)),
        resolve_workers=int(raw.get("resolveWorkers", 12)),
        emit_resolve_progress=True,
        emit_download_progress=True,
        skip_existing_downloads=True,
        strict_url_validation=False,
        chrome_cdp_url=chrome_cdp_url,
        chrome_user_data_dir=chrome_user_data_dir,
        chrome_profile_directory=str(raw.get("chromeProfileDirectory") or raw.get("chrome_profile_directory") or "Default").strip() or "Default",
        storage_state_save_path=storage_state_save_path,
    )


def on_event(event: PipelineEvent) -> None:
    payload: dict[str, Any] = {
        "type": event.kind,
        "stage": event.stage.value if event.stage else "unknown",
        "message": event.message,
    }
    payload.update(event.data)
    emit(payload)


def main() -> None:
    emit({"type": "log", "stage": "system", "level": "info", "message": "Bridge started."})
    raw_input = sys.stdin.readline().strip()
    if not raw_input:
        emit({"type": "error", "message": "No config received on stdin."})
        sys.exit(1)

    try:
        raw_config = json.loads(raw_input)
    except json.JSONDecodeError as exc:
        emit({"type": "error", "message": f"Invalid JSON config: {exc}"})
        sys.exit(1)

    try:
        config = build_config(raw_config)
    except Exception as exc:
        emit({"type": "error", "message": f"Config error: {exc}"})
        sys.exit(1)

    emit({"type": "log", "stage": "system", "level": "info", "message": f"Starting pipeline for {len(config.urls)} URL(s)..."})
    if config.chrome_cdp_url and config.chrome_user_data_dir:
        emit(
            {
                "type": "log",
                "stage": "system",
                "level": "info",
                "message": (
                    "Using Chrome session config: "
                    f"CDP {config.chrome_cdp_url} with profile path "
                    f"{config.chrome_user_data_dir} ({config.chrome_profile_directory})"
                ),
            }
        )
    elif config.chrome_cdp_url:
        emit({"type": "log", "stage": "system", "level": "info", "message": f"Using live Chrome session: {config.chrome_cdp_url}"})
    elif config.chrome_user_data_dir:
        emit({"type": "log", "stage": "system", "level": "info", "message": f"Using Chrome profile: {config.chrome_user_data_dir} ({config.chrome_profile_directory})"})
    if config.storage_state:
        emit({"type": "log", "stage": "system", "level": "info", "message": f"Using storage state: {config.storage_state}"})

    try:
        result = run_universal_pipeline(config, on_event=on_event)
        emit({
            "type": "finished",
            "success": result.download_failure_count == 0,
            "records": result.record_count,
            "successCount": result.download_success_count,
            "failureCount": result.download_failure_count,
            "outputRoot": str(result.workspace),
            "metadataDir": str(result.metadata_dir),
            "downloadRoot": str(result.workspace / "downloads"),
        })
    except PipelineError:
        # run_universal_pipeline already emitted stage error via on_event before re-raising.
        pass
    except Exception as exc:
        emit({"type": "error", "message": str(exc)})


if __name__ == "__main__":
    main()
