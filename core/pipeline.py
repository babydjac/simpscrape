from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
import re
import shlex
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlparse

from core.crawl_controller import CrawlConfig, CrawlController
from core.dom_executor import DomExecutor
from core.link_resolvers import ResolvedLink, build_password_hints, resolve_links_bulk
from core.sitemap_loader import load_sitemap_payload
from core.universal_downloader import DownloadConfig, download_urls, looks_like_direct_media_url
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


def template_sitemap(url: str) -> dict[str, Any]:
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
        cookies.append(
            {
                "domain": domain,
                "path": path,
                "secure": secure.lower() == "true",
                "expires": int(expiry) if expiry.isdigit() else -1,
                "name": name,
                "value": value,
            }
        )
    return cookies


def crawl_records(
    sitemap_model,
    headless: bool,
    delay_ms: int,
    max_pages: Optional[int],
    storage_state: Optional[Path],
    cookies_path: Optional[Path],
    nav_timeout_ms: int,
    idle_timeout_ms: int,
) -> list[dict[str, Any]]:
    cookies = load_cookies(cookies_path) if cookies_path else None
    driver = PlaywrightDriver(
        headless=headless,
        storage_state=str(storage_state) if storage_state else None,
        cookies=cookies,
    )
    dom = DomExecutor(driver)
    config = CrawlConfig(
        delay_ms=delay_ms,
        max_pages=max_pages,
        nav_timeout_ms=nav_timeout_ms,
        idle_timeout_ms=idle_timeout_ms,
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
    started_at = _utc_timestamp()

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
    planned_download_count = 0
    download_success_count = 0
    download_failure_count = 0
    discovery = None

    def emit(stage: PipelineStage, kind: str, message: str = "", **data: Any) -> None:
        callback(PipelineEvent(stage=stage, kind=kind, message=message, data=dict(data)))

    def check_stop() -> None:
        if stop_callback():
            raise PipelineStopped("Stopped by user.")

    try:
        urls = [item.strip() for item in config.urls if item.strip()]
        emit(
            PipelineStage.CRAWL,
            "phase",
            f"Scraping {len(urls)} URL(s)...",
            url_count=len(urls),
        )

        def crawl_one(target_url: str) -> list[dict[str, Any]]:
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

        write_json(scrape_output, all_records)
        if crawl_failures:
            crawl_failures_output.write_text(json.dumps(crawl_failures, indent=2, ensure_ascii=True))

        if not all_records:
            raise PipelineError("No records were scraped; stopping.")

        check_stop()
        discovery = discover_urls(all_records)
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
        )

        def download_progress(payload: dict[str, Any]) -> None:
            check_stop()
            if config.emit_download_progress:
                emit(PipelineStage.DOWNLOAD, "download_progress", payload=payload)

        progress_callback = download_progress if (config.emit_download_progress or should_stop is not None) else None
        results = download_urls(download_items, download_config, progress_callback=progress_callback)
        results_sorted = sorted(results, key=lambda item: (item.success is False, item.host, item.url))
        download_results_output.write_text(json.dumps([item.as_dict() for item in results_sorted], indent=2, ensure_ascii=True))

        download_success_count = sum(1 for item in results_sorted if item.success)
        download_failure_count = len(results_sorted) - download_success_count
        emit(
            PipelineStage.DONE,
            "finished",
            success=download_failure_count == 0,
            success_count=download_success_count,
            failure_count=download_failure_count,
            total=len(results_sorted),
            output_root=str(config.workspace),
            download_results_output=str(download_results_output),
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
            manifest = {
                "started_at": started_at,
                "ended_at": _utc_timestamp(),
                "status": status,
                "workspace": str(config.workspace),
                "config": _config_manifest(config),
                "summary": {
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


def _config_manifest(config: PipelineConfig) -> dict[str, Any]:
    payload = asdict(config)
    for key in ("workspace", "metadata_dir", "download_root", "storage_state", "cookies"):
        value = payload.get(key)
        if value is None:
            continue
        payload[key] = str(value)
    return payload


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
