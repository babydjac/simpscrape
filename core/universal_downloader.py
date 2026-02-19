from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from hashlib import sha1
from pathlib import Path
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence
import mimetypes
import os
import shutil
import subprocess
import time
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen


DIRECT_EXTENSIONS = {
    ".3gp",
    ".aac",
    ".avif",
    ".gif",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".png",
    ".svg",
    ".ts",
    ".wav",
    ".webm",
    ".webp",
}
IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".webp",
}
VIDEO_EXTENSIONS = {
    ".3gp",
    ".aac",
    ".avi",
    ".flv",
    ".m4a",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ogg",
    ".ts",
    ".wav",
    ".webm",
    ".wmv",
}

BUNKR_MARKERS = ("bunkr", "bnkr")
PERCENT_PATTERN = re.compile(r"(\d{1,3}(?:\.\d+)?)%")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
ProgressCallback = Callable[[Dict[str, Any]], None]


@dataclass
class DownloadConfig:
    download_root: Path
    workers: int = 4
    attempts: int = 3
    retry_delay: float = 6.0
    gallery_dl_path: Optional[str] = None
    yt_dlp_path: Optional[str] = None
    gallery_dl_args: Sequence[str] = ()
    yt_dlp_args: Sequence[str] = ()
    bunkr_endpoints: Sequence[Optional[str]] = ("/api/_001_v2", "/api/_001", None)
    user_agent: str = DEFAULT_USER_AGENT


@dataclass
class DownloadResult:
    url: str
    host: str
    success: bool
    method: str
    attempts: int
    code: Optional[int] = None
    detail: Optional[str] = None
    output_path: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


def download_urls(
    url_items: Sequence[tuple[str, str]],
    config: DownloadConfig,
    progress_callback: Optional[ProgressCallback] = None,
) -> List[DownloadResult]:
    if config.workers < 1:
        raise ValueError("workers must be >= 1")
    if config.attempts < 1:
        raise ValueError("attempts must be >= 1")
    if config.retry_delay < 0:
        raise ValueError("retry_delay must be >= 0")

    config.download_root.mkdir(parents=True, exist_ok=True)

    results: List[DownloadResult] = []
    with ThreadPoolExecutor(max_workers=config.workers) as executor:
        futures = {
            executor.submit(_download_one, url, host, config, progress_callback): (url, host)
            for url, host in url_items
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
    return results


def looks_like_direct_media_url(url: str) -> bool:
    parsed = urlparse(url)
    path = (parsed.path or "").lower()
    _, ext = os.path.splitext(path)
    if ext in DIRECT_EXTENSIONS:
        return True
    if "m3u8" in parsed.path.lower():
        return True
    query = parse_qs(parsed.query)
    for key in ("format", "ext"):
        for value in query.get(key, []):
            test_ext = "." + value.lower().strip(".")
            if test_ext in DIRECT_EXTENSIONS:
                return True
    filename_values = query.get("filename", []) + query.get("file", [])
    for value in filename_values:
        _, qext = os.path.splitext(value.lower())
        if qext in DIRECT_EXTENSIONS:
            return True
    return False


def _download_one(
    url: str,
    host: str,
    config: DownloadConfig,
    progress_callback: Optional[ProgressCallback],
) -> DownloadResult:
    tmp_root = config.download_root / "_tmp"
    job_dir = tmp_root / _safe_segment(host) / sha1(url.encode("utf-8")).hexdigest()[:16]

    last_code: Optional[int] = None
    last_detail: Optional[str] = None
    _emit_progress(
        progress_callback,
        event="start",
        url=url,
        host=host,
        method="pipeline",
        percent=1.0,
        status="starting",
    )
    try:
        for attempt in range(1, config.attempts + 1):
            _reset_job_dir(job_dir)
            attempt_start = 2.0 + ((attempt - 1) / config.attempts) * 90.0
            attempt_end = 2.0 + (attempt / config.attempts) * 90.0
            _emit_progress(
                progress_callback,
                event="attempt",
                url=url,
                host=host,
                method="pipeline",
                attempt=attempt,
                percent=attempt_start,
                status=f"attempt {attempt}/{config.attempts}",
            )

            steps: List[str] = []
            if config.gallery_dl_path:
                steps.append("gallery-dl")
            if config.yt_dlp_path:
                steps.append("yt-dlp")
            steps.append("direct-http")

            ranges: Dict[str, tuple[float, float]] = {}
            width = (attempt_end - attempt_start) / max(1, len(steps))
            current = attempt_start
            for step in steps:
                ranges[step] = (current, current + width)
                current += width

            if config.gallery_dl_path:
                g_start, g_end = ranges["gallery-dl"]
                success, code, detail = _try_gallery_dl(
                    url,
                    host,
                    job_dir,
                    config,
                    progress_callback,
                    g_start,
                    g_end,
                    attempt,
                )
                if success:
                    moved = _promote_job_outputs(job_dir, config.download_root)
                    _emit_progress(
                        progress_callback,
                        event="success",
                        url=url,
                        host=host,
                        method="gallery-dl",
                        attempt=attempt,
                        percent=100.0,
                        status="done",
                    )
                    return DownloadResult(
                        url=url,
                        host=host,
                        success=True,
                        method="gallery-dl",
                        attempts=attempt,
                        code=code,
                        output_path=str(moved[0]) if moved else None,
                    )
                last_code, last_detail = code, detail

            if config.yt_dlp_path:
                y_start, y_end = ranges["yt-dlp"]
                success, code, detail = _try_yt_dlp(
                    url,
                    host,
                    job_dir,
                    config,
                    progress_callback,
                    y_start,
                    y_end,
                    attempt,
                )
                if success:
                    moved = _promote_job_outputs(job_dir, config.download_root)
                    _emit_progress(
                        progress_callback,
                        event="success",
                        url=url,
                        host=host,
                        method="yt-dlp",
                        attempt=attempt,
                        percent=100.0,
                        status="done",
                    )
                    return DownloadResult(
                        url=url,
                        host=host,
                        success=True,
                        method="yt-dlp",
                        attempts=attempt,
                        code=code,
                        output_path=str(moved[0]) if moved else None,
                    )
                last_code, last_detail = code, detail

            d_start, d_end = ranges["direct-http"]
            success, detail, _output_path = _try_direct_download(
                url,
                host,
                job_dir,
                config.user_agent,
                progress_callback,
                d_start,
                d_end,
                attempt,
            )
            if success:
                moved = _promote_job_outputs(job_dir, config.download_root)
                _emit_progress(
                    progress_callback,
                    event="success",
                    url=url,
                    host=host,
                    method="direct-http",
                    attempt=attempt,
                    percent=100.0,
                    status="done",
                )
                return DownloadResult(
                    url=url,
                    host=host,
                    success=True,
                    method="direct-http",
                    attempts=attempt,
                    code=0,
                    output_path=str(moved[0]) if moved else None,
                )
            last_detail = detail

            if attempt < config.attempts:
                _emit_progress(
                    progress_callback,
                    event="retry",
                    url=url,
                    host=host,
                    method="pipeline",
                    attempt=attempt,
                    percent=attempt_end,
                    status=f"retrying in {config.retry_delay:.1f}s",
                )
                time.sleep(config.retry_delay)

        _emit_progress(
            progress_callback,
            event="failure",
            url=url,
            host=host,
            method="failed",
            percent=100.0,
            status=last_detail or "failed",
        )
        return DownloadResult(
            url=url,
            host=host,
            success=False,
            method="failed",
            attempts=config.attempts,
            code=last_code,
            detail=last_detail,
        )
    finally:
        _cleanup_job_dir(job_dir, tmp_root)


def _try_gallery_dl(
    url: str,
    host: str,
    host_dir: Path,
    config: DownloadConfig,
    progress_callback: Optional[ProgressCallback],
    progress_start: float,
    progress_end: float,
    attempt: int,
) -> tuple[bool, int, Optional[str]]:
    assert config.gallery_dl_path is not None
    endpoints = _gallery_endpoints_for_host(host, config.bunkr_endpoints)
    code = 1
    detail: Optional[str] = None
    for index, endpoint in enumerate(endpoints, start=1):
        label = endpoint or "default"
        _emit_progress(
            progress_callback,
            event="method",
            url=url,
            host=host,
            method="gallery-dl",
            attempt=attempt,
            endpoint=label,
            percent=progress_start,
            status=f"gallery-dl ({label})",
        )
        cmd = [config.gallery_dl_path, "-D", str(host_dir)]
        if endpoint:
            cmd += ["-o", f"extractor.bunkr.endpoint={endpoint}"]
        cmd += list(config.gallery_dl_args)
        cmd.append(url)

        endpoint_start = progress_start + ((index - 1) / max(1, len(endpoints))) * (progress_end - progress_start)
        endpoint_end = progress_start + (index / max(1, len(endpoints))) * (progress_end - progress_start)

        last_reported = -1.0

        def _line_callback(line: str) -> None:
            nonlocal last_reported
            percent = _extract_percent(line)
            if percent is None:
                return
            mapped = _map_percent(percent, endpoint_start, endpoint_end)
            if mapped < 100.0 and last_reported >= 0 and abs(mapped - last_reported) < 0.8:
                return
            last_reported = mapped
            _emit_progress(
                progress_callback,
                event="progress",
                url=url,
                host=host,
                method="gallery-dl",
                attempt=attempt,
                endpoint=label,
                percent=mapped,
                status="downloading",
            )

        code, saw_file_output, output = _run_stream(cmd, line_callback=_line_callback)
        if code == 0:
            return True, code, None
        if code == 8 and saw_file_output:
            return True, code, None
        detail = _tail(output)
    return False, code, detail


def _try_yt_dlp(
    url: str,
    host: str,
    host_dir: Path,
    config: DownloadConfig,
    progress_callback: Optional[ProgressCallback],
    progress_start: float,
    progress_end: float,
    attempt: int,
) -> tuple[bool, int, Optional[str]]:
    assert config.yt_dlp_path is not None
    cmd = [config.yt_dlp_path, "-P", str(host_dir)]
    cmd += list(config.yt_dlp_args)
    cmd.append(url)
    _emit_progress(
        progress_callback,
        event="method",
        url=url,
        host=host,
        method="yt-dlp",
        attempt=attempt,
        percent=progress_start,
        status="yt-dlp",
    )

    last_reported = -1.0

    def _line_callback(line: str) -> None:
        nonlocal last_reported
        percent = _extract_percent(line)
        if percent is None:
            return
        mapped = _map_percent(percent, progress_start, progress_end)
        if mapped < 100.0 and last_reported >= 0 and abs(mapped - last_reported) < 0.8:
            return
        last_reported = mapped
        _emit_progress(
            progress_callback,
            event="progress",
            url=url,
            host=host,
            method="yt-dlp",
            attempt=attempt,
            percent=mapped,
            status="downloading",
        )

    code, _, output = _run_stream(cmd, line_callback=_line_callback)
    if code == 0:
        return True, code, None
    return False, code, _tail(output)


def _try_direct_download(
    url: str,
    host: str,
    host_dir: Path,
    user_agent: str,
    progress_callback: Optional[ProgressCallback],
    progress_start: float,
    progress_end: float,
    attempt: int,
) -> tuple[bool, Optional[str], Optional[Path]]:
    request = Request(url, headers={"User-Agent": user_agent})
    _emit_progress(
        progress_callback,
        event="method",
        url=url,
        host=host,
        method="direct-http",
        attempt=attempt,
        percent=progress_start,
        status="direct-http",
    )
    try:
        with urlopen(request, timeout=45) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
            if not _is_downloadable_content_type(content_type) and not looks_like_direct_media_url(url):
                label = content_type or "unknown"
                return False, f"direct-http rejected content-type {label}", None
            filename = _pick_filename(url, content_type)
            target = _unique_path(host_dir / filename)
            total = int(response.headers.get("Content-Length") or 0)
            read = 0
            chunk_size = 1024 * 1024
            last_reported = -1.0
            with target.open("wb") as handle:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    handle.write(chunk)
                    read += len(chunk)
                    if total > 0:
                        mapped = _map_percent((read / total) * 100.0, progress_start, progress_end)
                    else:
                        ratio = min(95.0, (read / (16 * chunk_size)) * 100.0)
                        mapped = _map_percent(ratio, progress_start, progress_end)
                    if mapped < 100.0 and last_reported >= 0 and abs(mapped - last_reported) < 0.8:
                        continue
                    last_reported = mapped
                    _emit_progress(
                        progress_callback,
                        event="progress",
                        url=url,
                        host=host,
                        method="direct-http",
                        attempt=attempt,
                        percent=mapped,
                        status="downloading",
                    )
            return True, None, target
    except Exception as exc:
        return False, str(exc), None


def _reset_job_dir(job_dir: Path) -> None:
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    job_dir.mkdir(parents=True, exist_ok=True)


def _promote_job_outputs(job_dir: Path, download_root: Path) -> list[Path]:
    files = sorted(path for path in job_dir.rglob("*") if path.is_file())
    moved: list[Path] = []
    for source_path in files:
        bucket = _media_bucket_for_path(source_path)
        target_dir = download_root / bucket
        target_dir.mkdir(parents=True, exist_ok=True)
        target = _unique_path(target_dir / _safe_filename(source_path.name))
        shutil.move(str(source_path), str(target))
        moved.append(target)
    return moved


def _media_bucket_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "photos"
    if suffix in VIDEO_EXTENSIONS:
        return "videos"
    guessed_type, _encoding = mimetypes.guess_type(path.name)
    if guessed_type:
        if guessed_type.startswith("image/"):
            return "photos"
        if guessed_type.startswith(("video/", "audio/")):
            return "videos"
    return "videos"


def _cleanup_job_dir(job_dir: Path, tmp_root: Path) -> None:
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    current = job_dir.parent
    stop_at = tmp_root.parent
    while current != stop_at:
        if not current.exists():
            current = current.parent
            continue
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _gallery_endpoints_for_host(
    host: str,
    configured: Sequence[Optional[str]],
) -> Iterable[Optional[str]]:
    if _is_bunkr_host(host):
        if configured:
            return configured
        return ("/api/_001_v2", "/api/_001", None)
    return (None,)


def _is_bunkr_host(host: str) -> bool:
    lower = host.lower()
    return any(marker in lower for marker in BUNKR_MARKERS)


def _run_stream(
    cmd: List[str],
    line_callback: Optional[Callable[[str], None]] = None,
) -> tuple[int, bool, str]:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output_lines: List[str] = []
    saw_file_output = False
    assert proc.stdout is not None
    for line in proc.stdout:
        text = line.rstrip("\n")
        output_lines.append(text)
        if _looks_like_file_output(text):
            saw_file_output = True
        if line_callback:
            line_callback(text)
    code = proc.wait()
    return code, saw_file_output, "\n".join(output_lines)


def _looks_like_file_output(line: str) -> bool:
    return line.startswith("# /") or line.startswith("/")


def _pick_filename(url: str, content_type: str) -> str:
    parsed = urlparse(url)
    raw_name = Path(unquote(parsed.path)).name
    suffix = Path(raw_name).suffix
    if not raw_name or not suffix:
        guessed = mimetypes.guess_extension(content_type) or ""
        digest = sha1(url.encode("utf-8")).hexdigest()[:16]
        raw_name = f"file-{digest}{guessed}"
    return _safe_filename(raw_name)


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    index = 2
    while True:
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def _tail(output: str, limit: int = 400) -> Optional[str]:
    if not output:
        return None
    text = output.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _is_downloadable_content_type(content_type: str) -> bool:
    if not content_type:
        return False
    if content_type.startswith(("image/", "video/", "audio/")):
        return True
    return content_type in {
        "application/octet-stream",
        "application/pdf",
        "application/zip",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
        "application/x-tar",
        "application/gzip",
    }


def _safe_segment(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value) or "unknown"


def _safe_filename(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in value).strip()
    if not cleaned:
        cleaned = "file.bin"
    return cleaned


def _extract_percent(line: str) -> Optional[float]:
    match = PERCENT_PATTERN.search(line)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return max(0.0, min(100.0, value))


def _map_percent(value: float, start: float, end: float) -> float:
    clamped = max(0.0, min(100.0, value))
    return start + ((end - start) * (clamped / 100.0))


def _emit_progress(callback: Optional[ProgressCallback], **payload: Any) -> None:
    if callback is None:
        return
    callback(payload)
