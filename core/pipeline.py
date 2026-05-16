from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
import re
import shlex
import time
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlparse

from core.browser_config import chrome_session_enabled
from core.crawl_controller import CrawlConfig, CrawlController
from core.dom_executor import DomExecutor
from core.link_resolvers import ResolvedLink, build_password_hints, resolve_links_bulk
from core.sitemap_loader import load_sitemap_payload
from core.universal_downloader import (
    DEFAULT_USER_AGENT,
    DownloadConfig,
    DownloadResult,
    classify_download_output_path,
    download_urls,
    looks_like_direct_media_url,
)
from core.url_discovery import discover_urls, normalized_host
from drivers.playwright_driver import PlaywrightDriver
from output.json_writer import write_json

DEFAULT_BUNKR_ENDPOINTS: tuple[Optional[str], ...] = ("/api/_001_v2", "/api/_001", None)
_BUNKR_HOST_PATTERN = re.compile(r"(?:^|\.)(?:bunkr|bunkrr|bunkrrr|bunkr-cache)\.", re.IGNORECASE)


class PipelineStage(str, Enum):
    CRAWL = "crawl"
    DISCOVERY = "discovery"
    RESOLVE = "resolve"
    DOWNLOAD = "download"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True)
class PipelineEvent:
    stage: PipelineStage
    kind: str
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineConfig:
    urls: list[str]
    workspace: Path
    metadata_dir: Path
    download_root: Path
    include_source_hosts: bool = False
    no_download: bool = False
    headless: bool = True
    delay_ms: int = 500
    max_pages: Optional[int] = None
    crawl_jobs: int = 2
    storage_state: Optional[Path] = None
    cookies: Optional[Path] = None
    nav_timeout_ms: int = 60000
    idle_timeout_ms: int = 5000
    emit_browser_preview: bool = False
    download_workers: int = 4
    attempts: int = 3
    retry_delay: float = 6.0
    gallery_dl_path: Optional[str] = None
    yt_dlp_path: Optional[str] = None
    gallery_args: list[str] = field(default_factory=list)
    yt_dlp_args: list[str] = field(default_factory=list)
    resolve_links: bool = True
    resolve_workers: int = 6
    bunkr_endpoints: tuple[Optional[str], ...] = DEFAULT_BUNKR_ENDPOINTS
    emit_resolve_progress: bool = False
    emit_download_progress: bool = False
    write_run_manifest: bool = True
    fail_fast: bool = False
    strict_url_validation: bool = False
    normalize_urls: bool = True
    skip_existing_downloads: bool = False
    user_agent: Optional[str] = None
    download_timeout_sec: int = 45
    # fast | balanced | deep — adjusts delays, retries, and discovery depth.
    capture_profile: str = "balanced"
    # When True, create workspace/runs/<timestamp>_<label>/ with _meta + downloads/.
    run_scoped_outputs: bool = False
    run_label: str = ""
    # Lay out files under download_root/by-host and download_root/by-type.
    structured_downloads: bool = False
    chrome_cdp_url: Optional[str] = None
    chrome_user_data_dir: Optional[Path] = None
    chrome_profile_directory: str = "Default"
    # When set, post-interstitial-dismissal storage_state is persisted here so
    # subsequent runs skip the gate. Defaults to the loaded storage_state path
    # when one is available; CLI / GUI fill this in.
    storage_state_save_path: Optional[Path] = None


@dataclass(frozen=True)
class PipelineResult:
    workspace: Path
    metadata_dir: Path
    scrape_output: Path
    links_manifest_output: Path
    host_urls_output: Path
    hosts_summary_output: Path
    resolved_links_output: Path
    crawl_failures_output: Optional[Path]
    download_results_output: Optional[Path]
    run_manifest_output: Optional[Path]
    record_count: int
    crawl_failure_count: int
    source_mentions: int
    unique_url_count: int
    host_count: int
    resolved_unique_url_count: int
    skipped_source_host_urls: int
    planned_download_count: int
    download_success_count: int
    download_failure_count: int


EventCallback = Callable[[PipelineEvent], None]
StopCallback = Callable[[], bool]


class PipelineError(RuntimeError):
    pass


class PipelineStopped(PipelineError):
    pass


def _safe_run_segment(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (value or "").strip()) or "run"


def normalize_capture_profile(raw: Optional[str]) -> str:
    key = (raw or "balanced").strip().lower()
    if key in ("fast", "balanced", "deep"):
        return key
    return "balanced"


def apply_capture_profile_mutate(config: PipelineConfig) -> bool:
    """Apply crawl/download tuning for the selected profile. Returns whether deep URL discovery is enabled."""
    profile = normalize_capture_profile(getattr(config, "capture_profile", None))
    config.capture_profile = profile
    if profile == "fast":
        config.delay_ms = max(0, min(int(config.delay_ms), 220))
        config.attempts = max(1, min(int(config.attempts), 2))
        config.retry_delay = max(0.0, min(float(config.retry_delay), 3.5))
        config.resolve_workers = max(1, min(int(config.resolve_workers), 12))
    elif profile == "deep":
        config.attempts = max(int(config.attempts), 4)
        config.retry_delay = max(float(config.retry_delay), 5.0)
        config.idle_timeout_ms = max(int(config.idle_timeout_ms), 6500)
        if config.max_pages is not None:
            config.max_pages = min(600, max(int(config.max_pages), int(config.max_pages * 2)))
    return profile == "deep"


def expand_run_workspace_if_needed(config: PipelineConfig, started_at_dt: datetime) -> None:
    if not bool(getattr(config, "run_scoped_outputs", False)):
        return
    base = config.workspace
    label = getattr(config, "run_label", "") or base.name or "run"
    slug = _safe_run_segment(str(label))
    stamp = started_at_dt.strftime("%Y%m%d-%H%M%S")
    run_dir = base / "runs" / f"{stamp}_{slug}"
    run_dir.mkdir(parents=True, exist_ok=True)
    config.workspace = run_dir
    config.metadata_dir = run_dir / "_meta"
    config.download_root = run_dir / "downloads"


def write_download_index_manifests(metadata_dir: Path, results: Sequence[DownloadResult]) -> None:
    metadata_dir.mkdir(parents=True, exist_ok=True)
    by_host: dict[str, dict[str, Any]] = {}
    by_type: dict[str, list[str]] = {k: [] for k in ("image", "video", "archive", "other")}
    failures: list[dict[str, Any]] = []
    for item in results:
        h = item.host or "unknown"
        slot = by_host.setdefault(h, {"success": 0, "failure": 0, "items": []})
        slot["items"].append(
            {
                "url": item.url,
                "success": item.success,
                "method": item.method,
                "output_path": item.output_path,
                "detail": item.detail,
            }
        )
        if item.success:
            slot["success"] += 1
        else:
            slot["failure"] += 1
            failures.append(
                {"url": item.url, "host": item.host, "detail": item.detail, "method": item.method}
            )
        if item.success and item.output_path:
            bucket = classify_download_output_path(Path(item.output_path))
            by_type.setdefault(bucket, []).append(str(item.output_path))

    (metadata_dir / "index_by_host.json").write_text(
        json.dumps(by_host, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    (metadata_dir / "index_by_type.json").write_text(
        json.dumps(by_type, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    (metadata_dir / "index_failures.json").write_text(
        json.dumps(failures, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )


def template_sitemap(url: str) -> dict[str, Any]:
    if is_simptown_thread_url(url):
        return _simptown_template_sitemap(url)
    return _xenforo_template_sitemap(url)


def is_simptown_thread_url(url: str) -> bool:
    host = normalized_host(url)
    return host == "simptown.su" or host.endswith(".simptown.su")


def _xenforo_template_sitemap(url: str) -> dict[str, Any]:
    return {
        "_id": "site",
        "startUrl": [url],
        "selectors": [
            {
                "id": "title",
                "parentSelectors": ["_root"],
                "type": "SelectorText",
                "selector": "h1.p-title-value",
                "multiple": False,
                "regex": "",
            },
            {
                "id": "next_page",
                "parentSelectors": ["_root", "next_page"],
                "type": "SelectorLink",
                "selector": "a.pageNav-jump--next",
                "multiple": False,
            },
            {
                "id": "posts",
                "parentSelectors": ["_root", "next_page"],
                "type": "SelectorElement",
                "selector": "div.message-inner",
                "multiple": True,
            },
            {
                "id": "post_id",
                "parentSelectors": ["posts"],
                "type": "SelectorElementAttribute",
                "selector": "article",
                "multiple": False,
                "extractAttribute": "data-content",
            },
            {
                "id": "post_date",
                "parentSelectors": ["posts"],
                "type": "SelectorElementAttribute",
                "selector": "time.u-dt",
                "multiple": False,
                "extractAttribute": "datetime",
            },
            {
                "id": "post_author",
                "parentSelectors": ["posts"],
                "type": "SelectorText",
                "selector": "a.username",
                "multiple": False,
                "regex": "",
            },
            {
                "id": "post_author_id",
                "parentSelectors": ["posts"],
                "type": "SelectorElementAttribute",
                "selector": "a.username",
                "multiple": False,
                "extractAttribute": "data-user-id",
            },
            {
                "id": "post_content",
                "parentSelectors": ["posts"],
                "type": "SelectorText",
                "selector": "div.bbWrapper",
                "multiple": False,
                "regex": "\\s*\\n\\s*",
            },
            {
                "id": "quotes",
                "parentSelectors": ["posts"],
                "type": "SelectorText",
                "selector": "blockquote.bbCodeBlock-expandContent",
                "multiple": True,
                "regex": "",
            },
            {
                "id": "links",
                "parentSelectors": ["posts"],
                "type": "SelectorElementAttribute",
                "selector": "div.bbWrapper a:not(.bbCodeBlock-sourceJump)",
                "multiple": True,
                "extractAttribute": "href",
            },
            {
                "id": "images",
                "parentSelectors": ["posts"],
                "type": "SelectorElementAttribute",
                "selector": "div.bbWrapper img.bbImage",
                "multiple": True,
                "extractAttribute": "src",
            },
            {
                "id": "videos",
                "parentSelectors": ["posts"],
                "type": "SelectorElementAttribute",
                "selector": "div.bbWrapper video source",
                "multiple": True,
                "extractAttribute": "src",
            },
            {
                "id": "iframes",
                "parentSelectors": ["posts"],
                "type": "SelectorElementAttribute",
                "selector": "div.bbWrapper iframe",
                "multiple": True,
                "extractAttribute": "src",
            },
            {
                "id": "embeds",
                "parentSelectors": ["posts"],
                "type": "SelectorElementAttribute",
                "selector": "span[data-s9e-mediaembed-iframe]",
                "multiple": True,
                "extractAttribute": "data-s9e-mediaembed-iframe",
            },
            {
                "id": "attachments_block",
                "parentSelectors": ["posts"],
                "type": "SelectorElement",
                "selector": "section.message-attachments",
                "multiple": False,
            },
            {
                "id": "attachments",
                "parentSelectors": ["attachments_block"],
                "type": "SelectorElementAttribute",
                "selector": "a",
                "multiple": True,
                "extractAttribute": "href",
            },
            {
                "id": "reaction_score",
                "parentSelectors": ["posts"],
                "type": "SelectorText",
                "selector": "div.reactionsBar span",
                "multiple": False,
                "regex": "\\d+",
            },
        ],
    }


def _simptown_template_sitemap(url: str) -> dict[str, Any]:
    return {
        "_id": "site",
        "startUrl": [url],
        "selectors": [
            {
                "id": "title",
                "parentSelectors": ["_root"],
                "type": "SelectorText",
                "selector": "h2.thread-title",
                "multiple": False,
                "regex": "",
            },
            {
                "id": "next_page",
                "parentSelectors": ["_root", "next_page"],
                "type": "SelectorLink",
                "selector": ".pagination-wrapper.pagination-pos-top .pagination-content > button[data-pagination]:last-of-type",
                "multiple": False,
                "extractAttribute": "data-pagination",
            },
            {
                "id": "posts",
                "parentSelectors": ["_root", "next_page"],
                "type": "SelectorElement",
                "selector": "div.thread-post",
                "multiple": True,
            },
            {
                "id": "post_id",
                "parentSelectors": ["posts"],
                "type": "SelectorElementAttribute",
                "selector": ".thread-post",
                "multiple": False,
                "extractAttribute": "data-postid",
            },
            {
                "id": "post_date",
                "parentSelectors": ["posts"],
                "type": "SelectorText",
                "selector": "div.t-post-content-info > div.grow",
                "multiple": False,
                "regex": "",
            },
            {
                "id": "post_author",
                "parentSelectors": ["posts"],
                "type": "SelectorText",
                "selector": "span.tp-username",
                "multiple": False,
                "regex": "",
            },
            {
                "id": "post_author_id",
                "parentSelectors": ["posts"],
                "type": "SelectorElementAttribute",
                "selector": "span.tp-username",
                "multiple": False,
                "extractAttribute": "data-user",
            },
            {
                "id": "post_content",
                "parentSelectors": ["posts"],
                "type": "SelectorText",
                "selector": "div.bbContent",
                "multiple": False,
                "regex": "\\s*\\n\\s*",
            },
            {
                "id": "quotes",
                "parentSelectors": ["posts"],
                "type": "SelectorText",
                "selector": "div.bbContent blockquote, div.bbContent .bbCodeBlock--quote",
                "multiple": True,
                "regex": "",
            },
            {
                "id": "links",
                "parentSelectors": ["posts"],
                "type": "SelectorElementAttribute",
                "selector": "div.bbContent a",
                "multiple": True,
                "extractAttribute": "href",
            },
            {
                "id": "images",
                "parentSelectors": ["posts"],
                "type": "SelectorElementAttribute",
                "selector": "div.bbContent a:has(img.bbImage)",
                "multiple": True,
                "extractAttribute": "href",
            },
            {
                "id": "videos",
                "parentSelectors": ["posts"],
                "type": "SelectorElementAttribute",
                "selector": "div.bbContent video source, div.bbContent video",
                "multiple": True,
                "extractAttribute": "src",
            },
            {
                "id": "iframes",
                "parentSelectors": ["posts"],
                "type": "SelectorElementAttribute",
                "selector": "div.bbContent iframe",
                "multiple": True,
                "extractAttribute": "src",
            },
            {
                "id": "embeds",
                "parentSelectors": ["posts"],
                "type": "SelectorElementAttribute",
                "selector": "div.bbContent [data-s9e-mediaembed-iframe]",
                "multiple": True,
                "extractAttribute": "data-s9e-mediaembed-iframe",
            },
            {
                "id": "reaction_score",
                "parentSelectors": ["posts"],
                "type": "SelectorText",
                "selector": "div.t-post-content-reactions a.reactors",
                "multiple": False,
                "regex": "\\d+",
            },
        ],
    }


def parse_cli_args(raw: str, option_name: str) -> list[str]:
    if not raw.strip():
        return []
    try:
        return shlex.split(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid value for {option_name}: {exc}") from exc


def normalize_bunkr_endpoints(raw: Optional[Sequence[str]]) -> tuple[Optional[str], ...]:
    if not raw:
        return DEFAULT_BUNKR_ENDPOINTS
    cleaned: list[Optional[str]] = []
    for item in raw:
        value = (item or "").strip()
        if not value:
            continue
        if value in cleaned:
            continue
        cleaned.append(value)
    if None not in cleaned:
        cleaned.append(None)
    return tuple(cleaned) if cleaned else DEFAULT_BUNKR_ENDPOINTS


def load_cookies(cookie_file: Path) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for line in cookie_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, _flag, path, secure, expiry, name, value = parts
        expires = int(expiry) if expiry.isdigit() else -1
        cookies.append(
            {
                "domain": domain,
                "path": path,
                "secure": secure.lower() == "true",
                "expires": expires if expires > 0 else -1,
                "name": name,
                "value": value,
            }
        )
    return cookies


def _cookie_identity(cookie: dict[str, Any]) -> tuple[str, str, str]:
    name = str(cookie.get("name") or "")
    domain = str(cookie.get("domain") or "").lstrip(".").lower()
    path = str(cookie.get("path") or "/")
    return name, domain, path


def _filter_storage_state_cookie_overrides(
    cookies: list[dict[str, Any]],
    storage_state: Optional[Path],
) -> list[dict[str, Any]]:
    if not cookies or not storage_state:
        return cookies
    try:
        payload = json.loads(storage_state.read_text(encoding="utf-8"))
    except Exception:
        return cookies
    state_cookies = payload.get("cookies") if isinstance(payload, dict) else None
    if not isinstance(state_cookies, list):
        return cookies
    protected = {
        _cookie_identity(cookie)
        for cookie in state_cookies
        if isinstance(cookie, dict) and cookie.get("name")
    }
    if not protected:
        return cookies
    return [cookie for cookie in cookies if _cookie_identity(cookie) not in protected]


def crawl_records(
    sitemap_model,
    headless: bool,
    delay_ms: int,
    max_pages: Optional[int],
    storage_state: Optional[Path],
    cookies_path: Optional[Path],
    nav_timeout_ms: int,
    idle_timeout_ms: int,
    java_script_enabled: bool = True,
    page_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    user_agent: Optional[str] = None,
    emit_browser_preview: bool = False,
    chrome_cdp_url: Optional[str] = None,
    chrome_user_data_dir: Optional[Path] = None,
    chrome_profile_directory: str = "Default",
    storage_state_save_path: Optional[Path] = None,
) -> list[dict[str, Any]]:
    cookies = load_cookies(cookies_path) if cookies_path else []
    cookies = _filter_storage_state_cookie_overrides(cookies, storage_state)
    wants_chrome_session = chrome_session_enabled(chrome_cdp_url, chrome_user_data_dir)
    has_auth_fallback = bool(storage_state or cookies)
    profile_fallback_dir: Optional[str] = None
    if chrome_user_data_dir and (not chrome_cdp_url or not has_auth_fallback):
        # When a live CDP attach is requested and we already have saved auth available,
        # prefer the saved auth fallback over launching a second browser on the real profile.
        profile_fallback_dir = str(chrome_user_data_dir)
    driver = None
    if wants_chrome_session:
        try:
            driver = PlaywrightDriver(
                headless=headless,
                storage_state=None,
                user_agent=user_agent or DEFAULT_USER_AGENT,
                cookies=None,
                java_script_enabled=java_script_enabled,
                chrome_cdp_url=chrome_cdp_url,
                chrome_user_data_dir=profile_fallback_dir,
                chrome_profile_directory=chrome_profile_directory,
            )
        except RuntimeError:
            if not has_auth_fallback:
                raise

    if driver is None:
        driver = PlaywrightDriver(
            headless=headless,
            storage_state=str(storage_state) if storage_state else None,
            user_agent=user_agent or DEFAULT_USER_AGENT,
            cookies=cookies or None,
            java_script_enabled=java_script_enabled,
            chrome_cdp_url=None if wants_chrome_session else chrome_cdp_url,
            chrome_user_data_dir=None if wants_chrome_session or chrome_user_data_dir is None else str(chrome_user_data_dir),
            chrome_profile_directory=chrome_profile_directory,
        )
    dom = DomExecutor(driver)
    config = CrawlConfig(
        delay_ms=delay_ms,
        max_pages=max_pages,
        nav_timeout_ms=nav_timeout_ms,
        idle_timeout_ms=idle_timeout_ms,
        page_callback=page_callback,
        emit_browser_preview=emit_browser_preview,
        storage_state_save_path=storage_state_save_path,
    )
    try:
        controller = CrawlController(dom, sitemap_model, config)
        return controller.crawl()
    finally:
        dom.close()


def host_summary_payload(discovery, record_count: int) -> dict[str, Any]:
    hosts = []
    for host, unique_count in sorted(
        discovery.host_unique_counts.items(),
        key=lambda item: item[1],
        reverse=True,
    ):
        hosts.append(
            {
                "host": host,
                "unique_url_count": unique_count,
                "total_mentions": discovery.host_counts.get(host, 0),
            }
        )
    return {
        "record_count": record_count,
        "source_mentions": len(discovery.sources),
        "unique_url_count": len(discovery.unique_urls),
        "host_count": len(hosts),
        "hosts": hosts,
    }


def is_bunkr_family_host(host: str) -> bool:
    value = (host or "").strip().lower()
    if not value:
        return False
    if _BUNKR_HOST_PATTERN.search(value):
        return True
    if value == "scdn.st" or value.endswith(".scdn.st"):
        return True
    if value == "b-cdn.net" or value.endswith(".b-cdn.net"):
        return True
    if value.endswith(".bunkr.ru"):
        return True
    return False


def download_host_bucket(entry: ResolvedLink) -> str:
    resolver = (entry.resolver or "").strip().lower()
    if resolver.startswith("bunkr"):
        return "bunkr"
    if is_bunkr_family_host(entry.source_host) or is_bunkr_family_host(entry.resolved_host):
        return "bunkr"
    return entry.resolved_host or "unknown"


def run_universal_pipeline(
    config: PipelineConfig,
    on_event: Optional[EventCallback] = None,
    should_stop: Optional[StopCallback] = None,
) -> PipelineResult:
    _validate_config(config)
    callback = on_event or (lambda _event: None)
    stop_callback = should_stop or (lambda: False)
    started_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    started_at = started_at_dt.isoformat()

    deep_discovery = apply_capture_profile_mutate(config)
    expand_run_workspace_if_needed(config, started_at_dt)

    config.workspace.mkdir(parents=True, exist_ok=True)
    metadata_dir = config.metadata_dir
    metadata_dir.mkdir(parents=True, exist_ok=True)

    scrape_output = metadata_dir / "scrape_records.json"
    links_manifest_output = metadata_dir / "links_manifest.json"
    host_urls_output = metadata_dir / "host_urls.json"
    hosts_summary_output = metadata_dir / "hosts_summary.json"
    resolved_links_output = metadata_dir / "resolved_links.json"
    crawl_failures_output = metadata_dir / "crawl_failures.json"
    download_results_output = metadata_dir / "download_results.json"
    run_manifest_output = metadata_dir / "run_manifest.json" if config.write_run_manifest else None

    status = "success"
    error_entries: list[dict[str, str]] = []
    crawl_failures: list[dict[str, str]] = []
    all_records: list[dict[str, Any]] = []
    source_mentions = 0
    unique_url_count = 0
    host_count = 0
    resolved_unique_url_count = 0
    skipped_source_host_urls = 0
    skipped_invalid_input_urls = 0
    skipped_duplicate_input_urls = 0
    normalized_input_url_count = 0
    planned_download_count = 0
    download_success_count = 0
    download_failure_count = 0
    discovery = None

    def emit(stage: PipelineStage, kind: str, message: str = "", **data: Any) -> None:
        callback(PipelineEvent(stage=stage, kind=kind, message=message, data=dict(data)))

    def check_stop() -> None:
        if stop_callback():
            raise PipelineStopped("Stopped by user.")

    emit(
        PipelineStage.CRAWL,
        "run_paths",
        workspace=str(config.workspace),
        metadata_dir=str(metadata_dir),
        download_root=str(config.download_root),
        capture_profile=config.capture_profile,
        structured_downloads=bool(config.structured_downloads),
        run_scoped_outputs=bool(config.run_scoped_outputs),
    )

    try:
        stage_crawl_started = time.perf_counter()
        if config.normalize_urls:
            urls, skipped_invalid_input_urls, skipped_duplicate_input_urls = _prepare_input_urls(
                config.urls,
                strict_validation=config.strict_url_validation,
            )
        else:
            urls = [item.strip() for item in config.urls if item.strip()]
            if config.strict_url_validation:
                invalid = [item for item in urls if not _is_http_url(item)]
                if invalid:
                    raise ValueError(f"Invalid URL(s): {', '.join(invalid[:3])}")
            normalized_input_url_count = len(urls)

        normalized_input_url_count = len(urls)
        emit(
            PipelineStage.CRAWL,
            "url_input_summary",
            url_count=normalized_input_url_count,
            invalid_count=skipped_invalid_input_urls,
            duplicate_count=skipped_duplicate_input_urls,
        )

        if not urls:
            raise PipelineError("No valid URLs were provided after validation.")

        emit(
            PipelineStage.CRAWL,
            "phase",
            f"Scraping {len(urls)} URL(s)...",
            url_count=len(urls),
        )

        def crawl_one(target_url: str) -> list[dict[str, Any]]:
            def on_page(payload: dict[str, Any]) -> None:
                if stop_callback():
                    return
                emit(PipelineStage.CRAWL, "crawl_page", **payload)

            sitemap_model = load_sitemap_payload(template_sitemap(target_url))
            return crawl_records(
                sitemap_model=sitemap_model,
                headless=config.headless,
                delay_ms=config.delay_ms,
                max_pages=config.max_pages,
                storage_state=config.storage_state,
                cookies_path=config.cookies,
                nav_timeout_ms=config.nav_timeout_ms,
                idle_timeout_ms=config.idle_timeout_ms,
                java_script_enabled=not is_simptown_thread_url(target_url),
                page_callback=on_page,
                user_agent=config.user_agent or DEFAULT_USER_AGENT,
                emit_browser_preview=config.emit_browser_preview,
                chrome_cdp_url=config.chrome_cdp_url,
                chrome_user_data_dir=config.chrome_user_data_dir,
                chrome_profile_directory=config.chrome_profile_directory,
                storage_state_save_path=config.storage_state_save_path,
            )

        crawl_jobs = max(1, config.crawl_jobs)
        if crawl_jobs == 1 or len(urls) == 1:
            for index, target_url in enumerate(urls, start=1):
                check_stop()
                emit(
                    PipelineStage.CRAWL,
                    "phase",
                    f"Scraping {index}/{len(urls)}: {target_url}",
                    index=index,
                    total=len(urls),
                    url=target_url,
                )
                try:
                    records = crawl_one(target_url)
                    all_records.extend(records)
                    emit(
                        PipelineStage.CRAWL,
                        "crawl_update",
                        url=target_url,
                        count=len(records),
                        records=len(all_records),
                    )
                except Exception as exc:
                    failure = {"url": target_url, "error": str(exc)}
                    crawl_failures.append(failure)
                    emit(
                        PipelineStage.CRAWL,
                        "crawl_failure",
                        f"Failed to scrape {target_url}: {exc}",
                        **failure,
                    )
                    if config.fail_fast:
                        raise PipelineError(f"Stopping after crawl failure for {target_url}.") from exc
        else:
            with ThreadPoolExecutor(max_workers=crawl_jobs) as executor:
                futures = {executor.submit(crawl_one, target_url): target_url for target_url in urls}
                for future in as_completed(futures):
                    check_stop()
                    target_url = futures[future]
                    try:
                        records = future.result()
                        all_records.extend(records)
                        emit(
                            PipelineStage.CRAWL,
                            "crawl_update",
                            url=target_url,
                            count=len(records),
                            records=len(all_records),
                        )
                    except Exception as exc:
                        failure = {"url": target_url, "error": str(exc)}
                        crawl_failures.append(failure)
                        emit(
                            PipelineStage.CRAWL,
                            "crawl_failure",
                            f"Failed to scrape {target_url}: {exc}",
                            **failure,
                        )
                        if config.fail_fast:
                            for pending in futures:
                                if pending is not future:
                                    pending.cancel()
                            raise PipelineError(f"Stopping after crawl failure for {target_url}.") from exc

        write_json(scrape_output, all_records)
        if crawl_failures:
            crawl_failures_sorted = sorted(crawl_failures, key=lambda item: (item.get("url", ""), item.get("error", "")))
            crawl_failures_output.write_text(json.dumps(crawl_failures_sorted, indent=2, ensure_ascii=True))

        if not all_records:
            if crawl_failures:
                first_err = str(crawl_failures_sorted[0].get("error", "") or "").strip()
                detail = f" {first_err[:400]}" if first_err else ""
                raise PipelineError(
                    f"No records were scraped; {len(crawl_failures)} URL(s) failed before extraction.{detail}"
                )
            raise PipelineError("No records were scraped; stopping.")

        check_stop()
        discovery = discover_urls(all_records, deep=deep_discovery)
        source_mentions = len(discovery.sources)
        unique_url_count = len(discovery.unique_urls)
        host_count = len(discovery.host_unique_counts)

        links_manifest_output.write_text(json.dumps(discovery.sources_as_dicts(), indent=2, ensure_ascii=True))
        host_urls_output.write_text(json.dumps(discovery.host_urls, indent=2, ensure_ascii=True))
        hosts_summary_output.write_text(
            json.dumps(host_summary_payload(discovery, record_count=len(all_records)), indent=2, ensure_ascii=True)
        )

        emit(
            PipelineStage.DISCOVERY,
            "discovery",
            records=len(all_records),
            hosts=host_count,
            unique_urls=unique_url_count,
            source_mentions=source_mentions,
            output_root=str(config.workspace),
            links_manifest_output=str(links_manifest_output),
            hosts_summary_output=str(hosts_summary_output),
        )

        check_stop()
        resolved_entries: list[ResolvedLink]
        if config.resolve_links:
            emit(PipelineStage.RESOLVE, "phase", "Resolving host-specific links...")

            def resolver_progress(payload: dict[str, Any]) -> None:
                if stop_callback():
                    return
                emit(PipelineStage.RESOLVE, "resolve_progress", payload=payload)

            password_hints = build_password_hints(all_records, discovery.sources_as_dicts())
            resolved_entries = resolve_links_bulk(
                urls=discovery.unique_urls,
                source_hosts=discovery.url_hosts,
                password_hints=password_hints,
                workers=max(1, config.resolve_workers),
                progress_callback=resolver_progress if config.emit_resolve_progress else None,
            )
        else:
            resolved_entries = [
                ResolvedLink(
                    source_url=url,
                    source_host=discovery.url_hosts[url],
                    resolved_url=url,
                    resolved_host=discovery.url_hosts[url],
                    resolver="identity",
                )
                for url in discovery.unique_urls
            ]

        check_stop()
        resolved_links_output.write_text(json.dumps([entry.as_dict() for entry in resolved_entries], indent=2, ensure_ascii=True))
        resolved_unique_url_count = len({entry.resolved_url for entry in resolved_entries})
        emit(
            PipelineStage.RESOLVE,
            "resolved",
            resolved_count=len(resolved_entries),
            resolved_unique_urls=resolved_unique_url_count,
            resolved_links_output=str(resolved_links_output),
        )

        source_hosts = {normalized_host(url) for url in urls if normalized_host(url)}
        download_items: list[tuple[str, str]] = []
        skipped_source_host_urls = 0
        for entry in resolved_entries:
            candidate_url = entry.resolved_url
            source_host = entry.source_host
            resolved_host = entry.resolved_host
            bucket_host = download_host_bucket(entry)
            if (
                not config.include_source_hosts
                and source_host in source_hosts
                and resolved_host in source_hosts
                and not looks_like_direct_media_url(candidate_url)
            ):
                skipped_source_host_urls += 1
                continue
            download_items.append((candidate_url, bucket_host))

        deduped_items: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for item_url, item_host in download_items:
            if item_url in seen_urls:
                continue
            seen_urls.add(item_url)
            deduped_items.append((item_url, item_host))
        download_items = deduped_items
        planned_download_count = len(download_items)

        if skipped_source_host_urls:
            emit(
                PipelineStage.DOWNLOAD,
                "phase",
                f"Skipped {skipped_source_host_urls} same-host non-media URL(s). "
                "Use --include-source-hosts to include them.",
            )

        if config.no_download:
            emit(PipelineStage.DONE, "phase", "Download step skipped (--no-download).")
            result = PipelineResult(
                workspace=config.workspace,
                metadata_dir=metadata_dir,
                scrape_output=scrape_output,
                links_manifest_output=links_manifest_output,
                host_urls_output=host_urls_output,
                hosts_summary_output=hosts_summary_output,
                resolved_links_output=resolved_links_output,
                crawl_failures_output=crawl_failures_output if crawl_failures else None,
                download_results_output=None,
                run_manifest_output=run_manifest_output,
                record_count=len(all_records),
                crawl_failure_count=len(crawl_failures),
                source_mentions=source_mentions,
                unique_url_count=unique_url_count,
                host_count=host_count,
                resolved_unique_url_count=resolved_unique_url_count,
                skipped_source_host_urls=skipped_source_host_urls,
                planned_download_count=planned_download_count,
                download_success_count=0,
                download_failure_count=0,
            )
            emit(
                PipelineStage.DONE,
                "finished",
                success=True,
                success_count=0,
                failure_count=0,
                total=0,
                output_root=str(config.workspace),
                run_manifest_output=str(run_manifest_output) if run_manifest_output else None,
            )
            return result

        if not download_items:
            emit(PipelineStage.DONE, "phase", "No URLs left after filtering; nothing to download.")
            result = PipelineResult(
                workspace=config.workspace,
                metadata_dir=metadata_dir,
                scrape_output=scrape_output,
                links_manifest_output=links_manifest_output,
                host_urls_output=host_urls_output,
                hosts_summary_output=hosts_summary_output,
                resolved_links_output=resolved_links_output,
                crawl_failures_output=crawl_failures_output if crawl_failures else None,
                download_results_output=None,
                run_manifest_output=run_manifest_output,
                record_count=len(all_records),
                crawl_failure_count=len(crawl_failures),
                source_mentions=source_mentions,
                unique_url_count=unique_url_count,
                host_count=host_count,
                resolved_unique_url_count=resolved_unique_url_count,
                skipped_source_host_urls=skipped_source_host_urls,
                planned_download_count=0,
                download_success_count=0,
                download_failure_count=0,
            )
            emit(
                PipelineStage.DONE,
                "finished",
                success=True,
                success_count=0,
                failure_count=0,
                total=0,
                output_root=str(config.workspace),
                run_manifest_output=str(run_manifest_output) if run_manifest_output else None,
            )
            return result

        emit(
            PipelineStage.DOWNLOAD,
            "download_plan",
            items=download_items,
            skipped=skipped_source_host_urls,
        )

        if not config.gallery_dl_path and not config.yt_dlp_path:
            emit(
                PipelineStage.DOWNLOAD,
                "phase",
                "gallery-dl and yt-dlp were not found; running direct HTTP fallback only.",
            )

        emit(
            PipelineStage.DOWNLOAD,
            "phase",
            f"Starting download for {len(download_items)} URL(s)...",
            total=len(download_items),
        )

        config.download_root.mkdir(parents=True, exist_ok=True)
        download_config = DownloadConfig(
            download_root=config.download_root,
            workers=max(1, config.download_workers),
            attempts=max(1, config.attempts),
            retry_delay=max(0.0, config.retry_delay),
            gallery_dl_path=config.gallery_dl_path,
            yt_dlp_path=config.yt_dlp_path,
            gallery_dl_args=config.gallery_args,
            yt_dlp_args=config.yt_dlp_args,
            bunkr_endpoints=config.bunkr_endpoints,
            user_agent=config.user_agent or DEFAULT_USER_AGENT,
            direct_timeout_sec=max(1, config.download_timeout_sec),
            skip_existing=config.skip_existing_downloads,
            structured_downloads=bool(config.structured_downloads),
            completed_marker_root=config.download_root / "_meta",
        )

        def download_progress(payload: dict[str, Any]) -> None:
            check_stop()
            if config.emit_download_progress:
                emit(PipelineStage.DOWNLOAD, "download_progress", payload=payload)

        progress_callback = download_progress if (config.emit_download_progress or should_stop is not None) else None
        results = download_urls(download_items, download_config, progress_callback=progress_callback)
        results_sorted = sorted(results, key=lambda item: (item.success is False, item.host, item.url))
        download_results_output.write_text(json.dumps([item.as_dict() for item in results_sorted], indent=2, ensure_ascii=True))
        write_download_index_manifests(metadata_dir, results_sorted)

        download_success_count = sum(1 for item in results_sorted if item.success)
        download_failure_count = len(results_sorted) - download_success_count
        failed_urls = [item.url for item in results_sorted if not item.success][:500]
        emit(
            PipelineStage.DONE,
            "finished",
            success=download_failure_count == 0,
            success_count=download_success_count,
            failure_count=download_failure_count,
            total=len(results_sorted),
            output_root=str(config.workspace),
            download_results_output=str(download_results_output),
            run_manifest_output=str(run_manifest_output) if run_manifest_output else None,
            failed_urls=failed_urls,
        )

        return PipelineResult(
            workspace=config.workspace,
            metadata_dir=metadata_dir,
            scrape_output=scrape_output,
            links_manifest_output=links_manifest_output,
            host_urls_output=host_urls_output,
            hosts_summary_output=hosts_summary_output,
            resolved_links_output=resolved_links_output,
            crawl_failures_output=crawl_failures_output if crawl_failures else None,
            download_results_output=download_results_output,
            run_manifest_output=run_manifest_output,
            record_count=len(all_records),
            crawl_failure_count=len(crawl_failures),
            source_mentions=source_mentions,
            unique_url_count=unique_url_count,
            host_count=host_count,
            resolved_unique_url_count=resolved_unique_url_count,
            skipped_source_host_urls=skipped_source_host_urls,
            planned_download_count=planned_download_count,
            download_success_count=download_success_count,
            download_failure_count=download_failure_count,
        )
    except PipelineStopped as exc:
        status = "stopped"
        error_entries.append({"stage": "pipeline", "message": str(exc)})
        emit(PipelineStage.ERROR, "error", str(exc))
        raise
    except Exception as exc:
        status = "failed"
        error_entries.append({"stage": "pipeline", "message": str(exc)})
        emit(PipelineStage.ERROR, "error", str(exc))
        raise
    finally:
        if run_manifest_output:
            ended_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
            manifest = {
                "started_at": started_at,
                "ended_at": ended_at_dt.isoformat(),
                "duration_seconds": int((ended_at_dt - started_at_dt).total_seconds()),
                "status": status,
                "workspace": str(config.workspace),
                "config": _config_manifest(config),
                "summary": {
                    "normalized_input_url_count": normalized_input_url_count,
                    "skipped_invalid_input_urls": skipped_invalid_input_urls,
                    "skipped_duplicate_input_urls": skipped_duplicate_input_urls,
                    "record_count": len(all_records),
                    "crawl_failure_count": len(crawl_failures),
                    "source_mentions": source_mentions,
                    "unique_url_count": unique_url_count,
                    "host_count": host_count,
                    "resolved_unique_url_count": resolved_unique_url_count,
                    "skipped_source_host_urls": skipped_source_host_urls,
                    "planned_download_count": planned_download_count,
                    "download_success_count": download_success_count,
                    "download_failure_count": download_failure_count,
                },
                "errors": error_entries,
                "outputs": {
                    "scrape_output": str(scrape_output),
                    "links_manifest_output": str(links_manifest_output),
                    "host_urls_output": str(host_urls_output),
                    "hosts_summary_output": str(hosts_summary_output),
                    "resolved_links_output": str(resolved_links_output),
                    "crawl_failures_output": str(crawl_failures_output) if crawl_failures else None,
                    "download_results_output": str(download_results_output)
                    if download_results_output.exists()
                    else None,
                },
            }
            run_manifest_output.write_text(json.dumps(manifest, indent=2, ensure_ascii=True))


def _validate_config(config: PipelineConfig) -> None:
    if not any(item.strip() for item in config.urls):
        raise ValueError("At least one URL is required.")
    if not config.workspace:
        raise ValueError("workspace is required")
    if not config.metadata_dir:
        raise ValueError("metadata_dir is required")
    if not config.download_root:
        raise ValueError("download_root is required")
    if config.crawl_jobs < 1:
        raise ValueError("crawl_jobs must be >= 1")
    if config.download_workers < 1:
        raise ValueError("download_workers must be >= 1")
    if config.attempts < 1:
        raise ValueError("attempts must be >= 1")
    if config.retry_delay < 0:
        raise ValueError("retry_delay must be >= 0")
    if config.resolve_workers < 1:
        raise ValueError("resolve_workers must be >= 1")
    if config.nav_timeout_ms < 1:
        raise ValueError("nav_timeout_ms must be >= 1")
    if config.idle_timeout_ms < 1:
        raise ValueError("idle_timeout_ms must be >= 1")
    if config.download_timeout_sec < 1:
        raise ValueError("download_timeout_sec must be >= 1")


def _prepare_input_urls(
    raw_urls: Sequence[str],
    strict_validation: bool,
) -> tuple[list[str], int, int]:
    normalized: list[str] = []
    seen: set[str] = set()
    invalid = 0
    duplicates = 0
    invalid_samples: list[str] = []

    for raw in raw_urls:
        value = (raw or "").strip()
        if not value:
            continue
        if not _is_http_url(value):
            invalid += 1
            if len(invalid_samples) < 3:
                invalid_samples.append(value)
            continue
        if value in seen:
            duplicates += 1
            continue
        seen.add(value)
        normalized.append(value)

    if strict_validation and invalid:
        preview = ", ".join(invalid_samples)
        suffix = "..." if invalid > len(invalid_samples) else ""
        raise ValueError(f"Invalid URL(s): {preview}{suffix}")

    return normalized, invalid, duplicates


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _config_manifest(config: PipelineConfig) -> dict[str, Any]:
    payload = asdict(config)
    for key, value in list(payload.items()):
        if isinstance(value, Path):
            payload[key] = str(value)
    return payload
