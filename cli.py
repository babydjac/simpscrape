from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import re
import shutil
from typing import Optional
from urllib.parse import urlparse

import typer

from core.pipeline import (
    PipelineConfig,
    PipelineError,
    PipelineEvent,
    crawl_records,
    normalize_bunkr_endpoints,
    parse_cli_args,
    run_universal_pipeline,
    template_sitemap,
)
from core.sitemap_loader import load_sitemap, load_sitemap_payload
from drivers.playwright_driver import PlaywrightDriver
from output.csv_writer import write_csv
from output.json_writer import write_json
from output.sqlite_writer import write_sqlite

app = typer.Typer(add_completion=False)
CPU_COUNT = max(1, os.cpu_count() or 1)
DEFAULT_CRAWL_JOBS = max(1, min(8, CPU_COUNT // 2 if CPU_COUNT > 1 else 1))
DEFAULT_DOWNLOAD_WORKERS = max(4, min(16, CPU_COUNT))
DEFAULT_RESOLVE_WORKERS = max(6, min(24, CPU_COUNT * 2))
DEFAULT_DELAY_MS = 250
DEFAULT_RETRY_DELAY = 3.0


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


def _output_path_for_url(
    base_dir: Path,
    url: str,
    format: str,
    used: dict,
) -> Path:
    fmt = format.lower()
    ext = {"json": "json", "csv": "csv", "sqlite": "sqlite"}.get(fmt, fmt)
    slug = _slug_from_url(url)
    count = used.get(slug, 0)
    used[slug] = count + 1
    if count:
        slug = f"{slug}-{count + 1}"
    return base_dir / f"{slug}.{ext}"


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
):
    records = crawl_records(
        sitemap_model=sitemap_model,
        headless=headless,
        delay_ms=delay,
        max_pages=max_pages,
        storage_state=storage_state,
        cookies_path=cookies_path,
        nav_timeout_ms=nav_timeout_ms,
        idle_timeout_ms=idle_timeout_ms,
    )

    fmt = format.lower()
    if fmt == "json":
        write_json(output, records)
    elif fmt == "csv":
        write_csv(output, records, fieldnames=_field_order(sitemap_model))
    elif fmt == "sqlite":
        write_sqlite(output, records)
    else:
        raise typer.BadParameter(f"Unsupported format: {format}")

    typer.echo(f"Wrote {len(records)} records to {output}")

@app.command()
def run(
    sitemap: Path = typer.Option(..., "--sitemap", exists=True, readable=True),
    output: Path = typer.Option(..., "--output"),
    format: str = typer.Option("json", "--format"),
    headless: bool = typer.Option(True, "--headless"),
    delay: int = typer.Option(0, "--delay"),
    max_pages: Optional[int] = typer.Option(None, "--max-pages"),
    resume: bool = typer.Option(False, "--resume"),
    debug_dom: bool = typer.Option(False, "--debug-dom"),
    storage_state: Optional[Path] = typer.Option(None, "--storage-state", exists=True, readable=True),
    cookies: Optional[Path] = typer.Option(Path(__file__).resolve().parent / "cooks.txt", "--cookies", exists=True, readable=True),
    nav_timeout: int = typer.Option(60000, "--nav-timeout"),
    idle_timeout: int = typer.Option(5000, "--idle-timeout"),
):
    if resume:
        typer.echo("Resume not implemented yet; starting fresh.")
    if debug_dom:
        typer.echo("Debug DOM not implemented yet; proceeding without DOM dumps.")

    sitemap_model = load_sitemap(sitemap)
    _run_crawl(
        sitemap_model,
        output,
        format,
        headless,
        delay,
        max_pages,
        storage_state,
        cookies,
        nav_timeout,
        idle_timeout,
    )

@app.command()
def simp(
    urls: list[str] = typer.Argument(...),
    output: Path = typer.Option(Path("simp-output.json"), "--output"),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir"),
    format: str = typer.Option("json", "--format"),
    headless: bool = typer.Option(True, "--headless"),
    delay: int = typer.Option(DEFAULT_DELAY_MS, "--delay"),
    max_pages: Optional[int] = typer.Option(None, "--max-pages"),
    jobs: int = typer.Option(4, "--jobs", min=1),
    storage_state: Optional[Path] = typer.Option(None, "--storage-state", exists=True, readable=True),
    cookies: Optional[Path] = typer.Option(Path(__file__).resolve().parent / "cooks.txt", "--cookies", exists=True, readable=True),
    nav_timeout: int = typer.Option(60000, "--nav-timeout"),
    idle_timeout: int = typer.Option(5000, "--idle-timeout"),
):
    def _crawl_one(target_url: str, out_path: Path) -> None:
        sitemap_model = load_sitemap_payload(template_sitemap(target_url))
        _run_crawl(
            sitemap_model,
            out_path,
            format,
            headless,
            delay,
            max_pages,
            storage_state,
            cookies,
            nav_timeout,
            idle_timeout,
        )

    if len(urls) == 1:
        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
            used = {}
            output_path = _output_path_for_url(output_dir, urls[0], format, used)
        else:
            output_path = output
        _crawl_one(urls[0], output_path)
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
        for target_url in urls:
            out_path = _output_path_for_url(output_dir, target_url, format, used_slugs)
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


@app.command()
def universal(
    urls: list[str] = typer.Argument(..., help="Forum thread/profile URLs to scrape"),
    workspace: Path = typer.Option(Path("universal-output"), "--workspace"),
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
    nav_timeout: int = typer.Option(60000, "--nav-timeout"),
    idle_timeout: int = typer.Option(5000, "--idle-timeout"),
    download_workers: int = typer.Option(DEFAULT_DOWNLOAD_WORKERS, "--download-workers", min=1),
    attempts: int = typer.Option(3, "--attempts", min=1),
    retry_delay: float = typer.Option(DEFAULT_RETRY_DELAY, "--retry-delay", min=0.0),
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
):
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        gallery_dl_args = parse_cli_args(gallery_args, "--gallery-args")
        yt_dlp_args_list = parse_cli_args(yt_dlp_args, "--yt-dlp-args")
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    gallery_dl_bin = gallery_dl or shutil.which("gallery-dl")
    yt_dlp_bin = yt_dlp or shutil.which("yt-dlp")
    config = PipelineConfig(
        urls=urls,
        workspace=workspace,
        metadata_dir=workspace,
        download_root=workspace / "downloads",
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
    )

    def on_event(event: PipelineEvent) -> None:
        if event.kind == "phase" and event.message:
            typer.echo(event.message)
            return
        if event.kind == "crawl_update":
            typer.echo(f"Scraped {int(event.data.get('count', 0))} records from {event.data.get('url', '')}")
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
        if event.kind == "error":
            typer.echo(f"Error: {event.message}")

    try:
        result = run_universal_pipeline(config, on_event=on_event, should_stop=None)
    except PipelineError:
        raise typer.Exit(code=1)

    typer.echo(f"Saved {result.record_count} scraped records to {result.scrape_output}")
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
def login(
    url: str = typer.Option("https://simpcity.cr/login", "--url"),
    output_state: Path = typer.Option(Path("simpcity-state.json"), "--output-state"),
):
    driver = PlaywrightDriver(headless=False)
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
