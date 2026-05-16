from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import re
import shutil
from typing import Iterable, Optional
from urllib.parse import urlparse

import typer

from core.browser_config import DEFAULT_CHROME_CDP_URL, resolve_chrome_session
from core.chrome_bridge import (
    browser_native_host_manifest_dir,
    install_native_host_manifest,
)
from core.pipeline import (
    PipelineConfig,
    PipelineError,
    PipelineEvent,
    crawl_records,
    is_simptown_thread_url,
    load_cookies,
    normalize_bunkr_endpoints,
    parse_cli_args,
    run_universal_pipeline,
    template_sitemap,
)
from core.sitemap_loader import load_sitemap, load_sitemap_payload
from drivers.playwright_driver import PlaywrightDriver
from output.csv_writer import write_csv
from output.json_writer import write_json
from output.jsonl_writer import write_jsonl
from output.sqlite_writer import write_sqlite

app = typer.Typer(add_completion=False)
CPU_COUNT = max(1, os.cpu_count() or 1)
DEFAULT_CRAWL_JOBS = max(1, min(8, CPU_COUNT // 2 if CPU_COUNT > 1 else 1))
DEFAULT_DOWNLOAD_WORKERS = max(4, min(16, CPU_COUNT))
DEFAULT_RESOLVE_WORKERS = max(6, min(24, CPU_COUNT * 2))
DEFAULT_DELAY_MS = 250
DEFAULT_RETRY_DELAY = 3.0
ALLOWED_OUTPUT_FORMATS = ("json", "jsonl", "csv", "sqlite")
REPO_ROOT = Path(__file__).resolve().parent
CHROME_EXTENSION_DIR = REPO_ROOT / "chrome_extension"
CHROME_NATIVE_HOST_SCRIPT = REPO_ROOT / "chrome_bridge" / "native_host.sh"


def _field_order(sitemap_model) -> list:
    order = ["web_scraper_order", "web_scraper_start_url"]
    for selector in sitemap_model.selectors:
        if selector.type == "SelectorElement":
            continue
        if selector.type == "SelectorLink":
            order.append(selector.id)
            order.append(f"{selector.id}-href")
            continue
        order.append(selector.id)
    return order


def _slug_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    if not path:
        return "site"
    parts = [part for part in path.split("/") if part]
    slug = parts[-1]
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", slug).strip("-")
    return slug or "site"


def _resolve_universal_state_save_path(
    explicit_state: Optional[Path],
    urls: list[str],
) -> Path:
    """Where to persist post-dismissal storage_state for `universal`.

    If the user passed `--storage-state PATH`, save back to that exact file so
    repeat runs benefit from the gate-cleared cookies. Otherwise pick a
    per-host file under ~/.simpscrape so a fresh user still gets persistence.
    """
    if explicit_state is not None:
        return explicit_state
    host = ""
    for url in urls:
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        if host:
            break
    slug = re.sub(r"[^a-z0-9]+", "-", host).strip("-") or "simpscrape"
    return Path.home() / ".simpscrape" / f"{slug}-state.json"


def _output_path_for_url(
    base_dir: Path,
    url: str,
    format: str,
    used: dict,
) -> Path:
    fmt = _normalize_format(format)
    ext = {"json": "json", "jsonl": "jsonl", "csv": "csv", "sqlite": "sqlite"}[fmt]
    slug = _slug_from_url(url)
    count = used.get(slug, 0)
    used[slug] = count + 1
    if count:
        slug = f"{slug}-{count + 1}"
    return base_dir / f"{slug}.{ext}"


def _normalize_format(value: str) -> str:
    fmt = (value or "").strip().lower()
    if fmt not in ALLOWED_OUTPUT_FORMATS:
        raise typer.BadParameter(f"Unsupported format: {value}. Choose from: {', '.join(ALLOWED_OUTPUT_FORMATS)}")
    return fmt


def _ensure_can_write(path: Path, overwrite: bool) -> None:
    if path.exists() and path.is_dir():
        raise typer.BadParameter(f"Expected a file path, got directory: {path}")
    if path.exists() and not overwrite:
        raise typer.BadParameter(f"Refusing to overwrite existing file: {path}. Use --overwrite.")


def _read_url_lines(path: Path) -> list[str]:
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        token = raw.strip()
        if not token or token.startswith("#"):
            continue
        lines.append(token)
    return lines


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _collect_and_validate_urls(values: Iterable[str]) -> tuple[list[str], int, int]:
    deduped: list[str] = []
    seen: set[str] = set()
    skipped_invalid = 0
    skipped_duplicates = 0
    for item in values:
        value = item.strip()
        if not value:
            continue
        if not _is_http_url(value):
            skipped_invalid += 1
            continue
        if value in seen:
            skipped_duplicates += 1
            continue
        seen.add(value)
        deduped.append(value)
    return deduped, skipped_invalid, skipped_duplicates


def _default_output_for_format(path: Path, fmt: str) -> Path:
    if path != Path("simp-output.json"):
        return path
    ext = {"json": "json", "jsonl": "jsonl", "csv": "csv", "sqlite": "sqlite"}[fmt]
    return Path(f"simp-output.{ext}")


def _run_crawl(
    sitemap_model,
    output: Path,
    format: str,
    headless: bool,
    delay: int,
    max_pages: Optional[int],
    storage_state: Optional[Path],
    cookies_path: Optional[Path],
    nav_timeout_ms: int,
    idle_timeout_ms: int,
    chrome_cdp_url: Optional[str],
    chrome_user_data_dir: Optional[Path],
    chrome_profile_directory: str,
    overwrite: bool,
):
    output.parent.mkdir(parents=True, exist_ok=True)
    _ensure_can_write(output, overwrite=overwrite)
    java_script_enabled = not any(is_simptown_thread_url(url) for url in sitemap_model.start_urls)
    records = crawl_records(
        sitemap_model=sitemap_model,
        headless=headless,
        delay_ms=delay,
        max_pages=max_pages,
        storage_state=storage_state,
        cookies_path=cookies_path,
        nav_timeout_ms=nav_timeout_ms,
        idle_timeout_ms=idle_timeout_ms,
        java_script_enabled=java_script_enabled,
        chrome_cdp_url=chrome_cdp_url,
        chrome_user_data_dir=chrome_user_data_dir,
        chrome_profile_directory=chrome_profile_directory,
    )

    fmt = _normalize_format(format)
    if fmt == "json":
        write_json(output, records)
    elif fmt == "jsonl":
        write_jsonl(output, records)
    elif fmt == "csv":
        write_csv(output, records, fieldnames=_field_order(sitemap_model))
    elif fmt == "sqlite":
        write_sqlite(output, records)

    typer.echo(f"Wrote {len(records)} records to {output}")

@app.command()
def run(
    sitemap: Path = typer.Option(..., "--sitemap", exists=True, readable=True),
    output: Path = typer.Option(..., "--output"),
    format: str = typer.Option("json", "--format", help="Output format: json, jsonl, csv, sqlite."),
    headless: bool = typer.Option(True, "--headless"),
    delay: int = typer.Option(0, "--delay"),
    max_pages: Optional[int] = typer.Option(None, "--max-pages"),
    resume: bool = typer.Option(False, "--resume"),
    debug_dom: bool = typer.Option(False, "--debug-dom"),
    storage_state: Optional[Path] = typer.Option(None, "--storage-state", exists=True, readable=True),
    cookies: Optional[Path] = typer.Option(Path(__file__).resolve().parent / "cooks.txt", "--cookies", exists=True, readable=True),
    chrome: bool = typer.Option(
        False,
        "--chrome",
        help=f"Attach to a live Chrome session at {DEFAULT_CHROME_CDP_URL} and reuse its logged-in state.",
    ),
    chrome_cdp_url: str = typer.Option(
        "",
        "--chrome-cdp-url",
        help="DevTools/CDP URL for a running Chrome instance, e.g. http://127.0.0.1:9222.",
    ),
    chrome_user_data_dir: Optional[Path] = typer.Option(
        None,
        "--chrome-user-data-dir",
        exists=True,
        file_okay=False,
        readable=True,
        help="Launch against a persistent Chrome user data directory instead of Playwright Chromium.",
    ),
    chrome_profile_directory: str = typer.Option(
        "Default",
        "--chrome-profile-directory",
        help="Chrome profile directory name inside the user data dir, e.g. Default or Profile 1.",
    ),
    nav_timeout: int = typer.Option(60000, "--nav-timeout"),
    idle_timeout: int = typer.Option(5000, "--idle-timeout"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Allow replacing existing output file."),
):
    if resume:
        typer.echo("Resume not implemented yet; starting fresh.")
    if debug_dom:
        typer.echo("Debug DOM not implemented yet; proceeding without DOM dumps.")

    sitemap_model = load_sitemap(sitemap)
    normalized_format = _normalize_format(format)
    resolved_chrome_cdp_url, resolved_chrome_user_data_dir = resolve_chrome_session(
        use_chrome=chrome,
        chrome_cdp_url=chrome_cdp_url,
        chrome_user_data_dir=chrome_user_data_dir,
    )
    try:
        _run_crawl(
            sitemap_model,
            output,
            normalized_format,
            headless,
            delay,
            max_pages,
            storage_state,
            cookies,
            nav_timeout,
            idle_timeout,
            resolved_chrome_cdp_url,
            resolved_chrome_user_data_dir,
            chrome_profile_directory,
            overwrite,
        )
    except Exception as exc:
        typer.echo(f"Failed: {exc}")
        raise typer.Exit(code=1)

@app.command()
def simp(
    urls: list[str] = typer.Argument(None),
    urls_file: Optional[Path] = typer.Option(
        None,
        "--urls-file",
        exists=True,
        readable=True,
        help="Optional text file with one URL per line (# comments allowed).",
    ),
    output: Path = typer.Option(Path("simp-output.json"), "--output"),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir"),
    format: str = typer.Option("json", "--format", help="Output format: json, jsonl, csv, sqlite."),
    headless: bool = typer.Option(True, "--headless"),
    delay: int = typer.Option(DEFAULT_DELAY_MS, "--delay"),
    max_pages: Optional[int] = typer.Option(None, "--max-pages"),
    jobs: int = typer.Option(4, "--jobs", min=1),
    storage_state: Optional[Path] = typer.Option(None, "--storage-state", exists=True, readable=True),
    cookies: Optional[Path] = typer.Option(Path(__file__).resolve().parent / "cooks.txt", "--cookies", exists=True, readable=True),
    chrome: bool = typer.Option(
        False,
        "--chrome",
        help=f"Attach to a live Chrome session at {DEFAULT_CHROME_CDP_URL} and reuse its logged-in state.",
    ),
    chrome_cdp_url: str = typer.Option(
        "",
        "--chrome-cdp-url",
        help="DevTools/CDP URL for a running Chrome instance, e.g. http://127.0.0.1:9222.",
    ),
    chrome_user_data_dir: Optional[Path] = typer.Option(
        None,
        "--chrome-user-data-dir",
        exists=True,
        file_okay=False,
        readable=True,
        help="Launch against a persistent Chrome user data directory instead of Playwright Chromium.",
    ),
    chrome_profile_directory: str = typer.Option(
        "Default",
        "--chrome-profile-directory",
        help="Chrome profile directory name inside the user data dir, e.g. Default or Profile 1.",
    ),
    nav_timeout: int = typer.Option(60000, "--nav-timeout"),
    idle_timeout: int = typer.Option(5000, "--idle-timeout"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Allow replacing existing output file(s)."),
):
    combined_urls: list[str] = list(urls or [])
    if urls_file:
        combined_urls.extend(_read_url_lines(urls_file))
    normalized_urls, skipped_invalid, skipped_duplicates = _collect_and_validate_urls(combined_urls)
    if not normalized_urls:
        raise typer.BadParameter("No valid http(s) URLs were provided.")
    if skipped_invalid:
        typer.echo(f"Skipped {skipped_invalid} invalid URL(s).")
    if skipped_duplicates:
        typer.echo(f"Skipped {skipped_duplicates} duplicate URL(s).")

    normalized_format = _normalize_format(format)
    output = _default_output_for_format(output, normalized_format)
    resolved_chrome_cdp_url, resolved_chrome_user_data_dir = resolve_chrome_session(
        use_chrome=chrome,
        chrome_cdp_url=chrome_cdp_url,
        chrome_user_data_dir=chrome_user_data_dir,
    )

    def _crawl_one(target_url: str, out_path: Path) -> None:
        sitemap_model = load_sitemap_payload(template_sitemap(target_url))
        _run_crawl(
            sitemap_model,
            out_path,
            normalized_format,
            headless,
            delay,
            max_pages,
            storage_state,
            cookies,
            nav_timeout,
            idle_timeout,
            resolved_chrome_cdp_url,
            resolved_chrome_user_data_dir,
            chrome_profile_directory,
            overwrite,
        )

    if len(normalized_urls) == 1:
        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
            used = {}
            output_path = _output_path_for_url(output_dir, normalized_urls[0], normalized_format, used)
        else:
            output_path = output
        try:
            _crawl_one(normalized_urls[0], output_path)
        except Exception as exc:
            typer.echo(f"Failed: {exc}")
            raise typer.Exit(code=1)
        return

    if output_dir is None:
        if output == Path("simp-output.json"):
            output_dir = Path("simp-output")
        elif output.exists() and output.is_dir():
            output_dir = output
        elif output.suffix == "":
            output_dir = output
        else:
            raise typer.BadParameter(
                "Multiple URLs require --output-dir or a directory path via --output."
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    used_slugs: dict[str, int] = {}
    failures: list[str] = []

    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {}
        for target_url in normalized_urls:
            out_path = _output_path_for_url(output_dir, target_url, normalized_format, used_slugs)
            _ensure_can_write(out_path, overwrite=overwrite)
            futures[executor.submit(_crawl_one, target_url, out_path)] = target_url

        for future in as_completed(futures):
            target_url = futures[future]
            try:
                future.result()
            except Exception as exc:
                failures.append(target_url)
                typer.echo(f"Failed: {target_url} ({exc})")

    if failures:
        typer.echo(f"{len(failures)} crawl(s) failed.")
        raise typer.Exit(code=1)
    typer.echo(f"All {len(normalized_urls)} URL(s) completed successfully.")


@app.command()
def universal(
    urls: list[str] = typer.Argument(..., help="Forum thread/profile URLs to scrape"),
    workspace: Path = typer.Option(Path("universal-output"), "--workspace"),
    metadata_dir: Optional[Path] = typer.Option(
        None,
        "--metadata-dir",
        help="Directory for metadata files (default: workspace).",
    ),
    download_root: Optional[Path] = typer.Option(
        None,
        "--download-root",
        help="Directory for downloaded media (default: workspace/downloads).",
    ),
    no_download: bool = typer.Option(
        False,
        "--no-download",
        help="Scrape + discover links only (skip download step).",
    ),
    include_source_hosts: bool = typer.Option(
        False,
        "--include-source-hosts",
        help="Also download links from the same host as the source forum.",
    ),
    headless: bool = typer.Option(True, "--headless"),
    delay: int = typer.Option(DEFAULT_DELAY_MS, "--delay"),
    max_pages: Optional[int] = typer.Option(None, "--max-pages"),
    crawl_jobs: int = typer.Option(DEFAULT_CRAWL_JOBS, "--crawl-jobs", min=1),
    storage_state: Optional[Path] = typer.Option(None, "--storage-state", exists=True, readable=True),
    cookies: Optional[Path] = typer.Option(Path(__file__).resolve().parent / "cooks.txt", "--cookies", exists=True, readable=True),
    chrome: bool = typer.Option(
        False,
        "--chrome",
        help=f"Attach to a live Chrome session at {DEFAULT_CHROME_CDP_URL} and reuse its logged-in state.",
    ),
    chrome_cdp_url: str = typer.Option(
        "",
        "--chrome-cdp-url",
        help="DevTools/CDP URL for a running Chrome instance, e.g. http://127.0.0.1:9222.",
    ),
    chrome_user_data_dir: Optional[Path] = typer.Option(
        None,
        "--chrome-user-data-dir",
        exists=True,
        file_okay=False,
        readable=True,
        help="Launch against a persistent Chrome user data directory instead of Playwright Chromium.",
    ),
    chrome_profile_directory: str = typer.Option(
        "Default",
        "--chrome-profile-directory",
        help="Chrome profile directory name inside the user data dir, e.g. Default or Profile 1.",
    ),
    nav_timeout: int = typer.Option(60000, "--nav-timeout"),
    idle_timeout: int = typer.Option(5000, "--idle-timeout"),
    download_workers: int = typer.Option(DEFAULT_DOWNLOAD_WORKERS, "--download-workers", min=1),
    attempts: int = typer.Option(3, "--attempts", min=1),
    retry_delay: float = typer.Option(DEFAULT_RETRY_DELAY, "--retry-delay", min=0.0),
    download_timeout: int = typer.Option(45, "--download-timeout", min=5, help="Direct HTTP timeout in seconds."),
    user_agent: str = typer.Option("", "--user-agent", help="Override User-Agent for direct HTTP downloads."),
    gallery_dl: Optional[str] = typer.Option(None, "--gallery-dl", help="Path to gallery-dl binary"),
    yt_dlp: Optional[str] = typer.Option(None, "--yt-dlp", help="Path to yt-dlp binary"),
    gallery_args: str = typer.Option("--no-colors", "--gallery-args"),
    yt_dlp_args: str = typer.Option("--no-warnings --ignore-errors", "--yt-dlp-args"),
    resolve_links: bool = typer.Option(
        True,
        "--resolve-links/--no-resolve-links",
        help="Apply host-specific URL resolvers before download.",
    ),
    resolve_workers: int = typer.Option(DEFAULT_RESOLVE_WORKERS, "--resolve-workers", min=1),
    bunkr_endpoint: Optional[list[str]] = typer.Option(
        None,
        "--bunkr-endpoint",
        help="Repeatable bunkr API endpoint override (defaults include /api/_001_v2 and /api/_001).",
    ),
    skip_existing: bool = typer.Option(
        False,
        "--skip-existing",
        help="Skip URLs that already completed in this download folder.",
    ),
    capture_profile: str = typer.Option(
        "balanced",
        "--capture-profile",
        help="Crawl/download preset: fast, balanced, or deep.",
    ),
    run_scoped: bool = typer.Option(
        False,
        "--run-scoped",
        help="Write outputs under workspace/runs/<timestamp>_<label>/ instead of the workspace root.",
    ),
    run_label: str = typer.Option(
        "run",
        "--run-label",
        help="Folder label when using --run-scoped (default: run).",
    ),
    structured_downloads: bool = typer.Option(
        False,
        "--structured-downloads",
        help="Organize downloads into by-host/ and by-type/ subtrees.",
    ),
    no_manifest: bool = typer.Option(
        False,
        "--no-manifest",
        help="Skip writing run_manifest.json.",
    ),
    fail_fast: bool = typer.Option(
        False,
        "--fail-fast",
        help="Abort pipeline after first crawl failure.",
    ),
    strict_url_validation: bool = typer.Option(
        False,
        "--strict-url-validation",
        help="Fail on invalid input URLs instead of skipping them.",
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Print resolve/download progress events."),
):
    workspace.mkdir(parents=True, exist_ok=True)
    effective_metadata_dir = metadata_dir or workspace
    effective_download_root = download_root or (workspace / "downloads")
    effective_metadata_dir.mkdir(parents=True, exist_ok=True)
    effective_download_root.mkdir(parents=True, exist_ok=True)
    try:
        gallery_dl_args = parse_cli_args(gallery_args, "--gallery-args")
        yt_dlp_args_list = parse_cli_args(yt_dlp_args, "--yt-dlp-args")
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    gallery_dl_bin = gallery_dl or shutil.which("gallery-dl")
    yt_dlp_bin = yt_dlp or shutil.which("yt-dlp")
    resolved_chrome_cdp_url, resolved_chrome_user_data_dir = resolve_chrome_session(
        use_chrome=chrome,
        chrome_cdp_url=chrome_cdp_url,
        chrome_user_data_dir=chrome_user_data_dir,
    )
    storage_state_save_path = _resolve_universal_state_save_path(storage_state, urls)
    config = PipelineConfig(
        urls=urls,
        workspace=workspace,
        metadata_dir=effective_metadata_dir,
        download_root=effective_download_root,
        include_source_hosts=include_source_hosts,
        no_download=no_download,
        headless=headless,
        delay_ms=delay,
        max_pages=max_pages,
        crawl_jobs=crawl_jobs,
        storage_state=storage_state,
        cookies=cookies,
        nav_timeout_ms=nav_timeout,
        idle_timeout_ms=idle_timeout,
        download_workers=download_workers,
        attempts=attempts,
        retry_delay=retry_delay,
        gallery_dl_path=gallery_dl_bin,
        yt_dlp_path=yt_dlp_bin,
        gallery_args=gallery_dl_args,
        yt_dlp_args=yt_dlp_args_list,
        resolve_links=resolve_links,
        resolve_workers=resolve_workers,
        bunkr_endpoints=normalize_bunkr_endpoints(bunkr_endpoint),
        emit_resolve_progress=verbose,
        emit_download_progress=verbose,
        write_run_manifest=not no_manifest,
        fail_fast=fail_fast,
        strict_url_validation=strict_url_validation,
        skip_existing_downloads=skip_existing,
        user_agent=user_agent.strip() or None,
        download_timeout_sec=download_timeout,
        capture_profile=capture_profile,
        run_scoped_outputs=run_scoped,
        run_label=run_label,
        structured_downloads=structured_downloads,
        chrome_cdp_url=resolved_chrome_cdp_url,
        chrome_user_data_dir=resolved_chrome_user_data_dir,
        chrome_profile_directory=chrome_profile_directory,
        storage_state_save_path=storage_state_save_path,
    )

    def on_event(event: PipelineEvent) -> None:
        if event.kind == "phase" and event.message:
            typer.echo(event.message)
            return
        if event.kind == "crawl_update":
            typer.echo(f"Scraped {int(event.data.get('count', 0))} records from {event.data.get('url', '')}")
            return
        if event.kind == "crawl_page":
            page_event = str(event.data.get("event", "")).strip().lower()
            page = int(event.data.get("page", 0) or 0)
            total_pages = event.data.get("total_pages")
            records_on_page = int(event.data.get("records_on_page", 0) or 0)
            total_records = int(event.data.get("total_records", 0) or 0)
            if page_event == "interstitial_dismissed":
                saved_to = event.data.get("storage_state_saved")
                suffix = f" Session saved to {saved_to}." if saved_to else ""
                typer.echo(f"Auto-dismissed click-to-verify gate on page {page}.{suffix}")
                return
            if page_event == "page_complete":
                if isinstance(total_pages, int) and total_pages > 0:
                    typer.echo(
                        f"Page {page}/{total_pages} scraped (+{records_on_page} records, total {total_records})."
                    )
                else:
                    typer.echo(f"Page {page} scraped (+{records_on_page} records, total {total_records}).")
            return
        if event.kind == "crawl_failure":
            typer.echo(event.message)
            return
        if event.kind == "discovery":
            typer.echo(
                "Discovered "
                f"{int(event.data.get('source_mentions', 0))} source mentions and "
                f"{int(event.data.get('unique_urls', 0))} unique URLs across "
                f"{int(event.data.get('hosts', 0))} hosts."
            )
            typer.echo(f"Saved link manifest to {event.data.get('links_manifest_output', '')}")
            typer.echo(f"Saved host summary to {event.data.get('hosts_summary_output', '')}")
            return
        if event.kind == "resolved":
            typer.echo(
                f"Resolved {int(event.data.get('resolved_count', 0))} source links into "
                f"{int(event.data.get('resolved_unique_urls', 0))} unique downloadable links. "
                f"Manifest: {event.data.get('resolved_links_output', '')}"
            )
            return
        if event.kind == "url_input_summary":
            invalid_count = int(event.data.get("invalid_count", 0))
            duplicate_count = int(event.data.get("duplicate_count", 0))
            if invalid_count:
                typer.echo(f"Skipped {invalid_count} invalid URL(s).")
            if duplicate_count:
                typer.echo(f"Skipped {duplicate_count} duplicate URL(s).")
            return
        if verbose and event.kind == "resolve_progress":
            payload = dict(event.data.get("payload", {}))
            status = str(payload.get("status") or payload.get("event") or "").strip()
            if status:
                typer.echo(f"[resolve] {status}")
            return
        if verbose and event.kind == "download_progress":
            payload = dict(event.data.get("payload", {}))
            status = str(payload.get("status") or payload.get("event") or "").strip()
            url = str(payload.get("url", "")).strip()
            if status:
                typer.echo(f"[download] {status} {url}".strip())
            return
        if event.kind == "error":
            typer.echo(f"Error: {event.message}")

    try:
        result = run_universal_pipeline(config, on_event=on_event, should_stop=None)
    except PipelineError:
        raise typer.Exit(code=1)

    typer.echo(f"Saved {result.record_count} scraped records to {result.scrape_output}")
    if result.run_manifest_output:
        typer.echo(f"Run manifest: {result.run_manifest_output}")
    if result.crawl_failures_output:
        typer.echo(
            f"Recorded {result.crawl_failure_count} crawl failure(s) in {result.crawl_failures_output}"
        )

    if result.download_results_output:
        total_downloads = result.download_success_count + result.download_failure_count
        typer.echo(
            f"Download complete: {result.download_success_count}/{total_downloads} succeeded, "
            f"{result.download_failure_count} failed. Results: {result.download_results_output}"
        )
    if result.download_failure_count:
        raise typer.Exit(code=1)


@app.command()
def chrome_bridge_info(
    browser: str = typer.Option(
        "chrome",
        "--browser",
        help="Target browser for the native messaging host location: chrome, chrome-for-testing, chromium.",
    ),
):
    try:
        manifest_dir = browser_native_host_manifest_dir(browser)
    except Exception as exc:
        typer.echo(f"Failed: {exc}")
        raise typer.Exit(code=1)

    typer.echo(f"Chrome extension folder: {CHROME_EXTENSION_DIR}")
    typer.echo(f"Native host launcher: {CHROME_NATIVE_HOST_SCRIPT}")
    typer.echo(f"Native host manifest directory ({browser}): {manifest_dir}")
    typer.echo("Load the unpacked extension from the folder above, copy its extension ID, then run:")
    typer.echo("./simp chrome-bridge-install --extension-id YOUR_EXTENSION_ID")


@app.command()
def chrome_bridge_install(
    extension_id: str = typer.Option(
        ...,
        "--extension-id",
        help="Extension ID shown on chrome://extensions after loading the unpacked extension.",
    ),
    browser: str = typer.Option(
        "chrome",
        "--browser",
        help="Target browser for the native messaging host location: chrome, chrome-for-testing, chromium.",
    ),
):
    if not CHROME_EXTENSION_DIR.exists():
        typer.echo(f"Failed: extension folder not found: {CHROME_EXTENSION_DIR}")
        raise typer.Exit(code=1)
    if not CHROME_NATIVE_HOST_SCRIPT.exists():
        typer.echo(f"Failed: native host launcher not found: {CHROME_NATIVE_HOST_SCRIPT}")
        raise typer.Exit(code=1)

    try:
        manifest_path = install_native_host_manifest(
            browser=browser,
            extension_id=extension_id,
            host_script_path=CHROME_NATIVE_HOST_SCRIPT,
        )
    except Exception as exc:
        typer.echo(f"Failed: {exc}")
        raise typer.Exit(code=1)

    typer.echo(f"Installed native host manifest: {manifest_path}")
    typer.echo(f"Load unpacked extension from: {CHROME_EXTENSION_DIR}")
    typer.echo("Then open the extension popup on a logged-in site and click `Track This Site`.")
    typer.echo("SimpScrape will auto-detect the generated ~/host-state.json file for matching URLs.")


@app.command()
def login(
    url: str = typer.Option("https://simpcity.cr/login", "--url"),
    output_state: Path = typer.Option(Path("simpcity-state.json"), "--output-state"),
    cookies: Optional[Path] = typer.Option(
        None,
        "--cookies",
        exists=True,
        readable=True,
        help="Import a Netscape cookies.txt file instead of opening a browser.",
    ),
    chrome: bool = typer.Option(
        False,
        "--chrome",
        help=f"Attach to a live Chrome session at {DEFAULT_CHROME_CDP_URL} and export that logged-in state.",
    ),
    chrome_cdp_url: str = typer.Option(
        "",
        "--chrome-cdp-url",
        help="DevTools/CDP URL for a running Chrome instance, e.g. http://127.0.0.1:9222.",
    ),
    chrome_user_data_dir: Optional[Path] = typer.Option(
        None,
        "--chrome-user-data-dir",
        exists=True,
        file_okay=False,
        readable=True,
        help="Launch against a persistent Chrome user data directory instead of Playwright Chromium.",
    ),
    chrome_profile_directory: str = typer.Option(
        "Default",
        "--chrome-profile-directory",
        help="Chrome profile directory name inside the user data dir, e.g. Default or Profile 1.",
    ),
):
    output_state.parent.mkdir(parents=True, exist_ok=True)
    resolved_chrome_cdp_url, resolved_chrome_user_data_dir = resolve_chrome_session(
        use_chrome=chrome,
        chrome_cdp_url=chrome_cdp_url,
        chrome_user_data_dir=chrome_user_data_dir,
    )
    if cookies:
        driver = PlaywrightDriver(headless=True, cookies=load_cookies(cookies))
        try:
            driver._context.storage_state(path=str(output_state))
            typer.echo(f"Saved storage state to {output_state}")
        finally:
            driver.close()
        return

    driver = PlaywrightDriver(
        headless=False,
        chrome_cdp_url=resolved_chrome_cdp_url,
        chrome_user_data_dir=str(resolved_chrome_user_data_dir) if resolved_chrome_user_data_dir else None,
        chrome_profile_directory=chrome_profile_directory,
    )
    try:
        driver.goto(url)
        typer.echo("Login in the opened browser, then press Enter here to save session state.")
        input()
        driver._context.storage_state(path=str(output_state))
        typer.echo(f"Saved storage state to {output_state}")
    except KeyboardInterrupt:
        typer.echo("Login cancelled.")
    finally:
        driver.close()



if __name__ == "__main__":
    app()
