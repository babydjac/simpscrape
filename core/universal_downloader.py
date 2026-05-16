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
ARCHIVE_EXTENSIONS = {
    ".7z",
    ".bz2",
    ".gz",
    ".rar",
    ".tar",
    ".tbz2",
    ".tgz",
    ".txz",
    ".xz",
    ".zip",
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
SUBPROCESS_TIMEOUT_CODE = 124
DEFAULT_DIRECT_TIMEOUT_SEC = 45
MAX_FILENAME_CHARS = 180
CONTENT_DISPOSITION_FILENAME = re.compile(
    r"""filename\*?=(?:UTF-8''|")(.*?)(?:"|;|$)""",
    re.IGNORECASE,
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
    direct_timeout_sec: int = DEFAULT_DIRECT_TIMEOUT_SEC
    subprocess_timeout_sec: int = 240
    skip_existing: bool = False
    # When True, files go under download_root/by-host/<host>/ and mirrored by-type/.
    structured_downloads: bool = False
    # Directory that contains completed_urls/ (defaults to download_root/_meta).
    completed_marker_root: Optional[Path] = None


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
    if config.direct_timeout_sec < 1:
        raise ValueError("direct_timeout_sec must be >= 1")
    if config.subprocess_timeout_sec < 1:
        raise ValueError("subprocess_timeout_sec must be >= 1")

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
    marker_path = _completed_marker_path(config, url)

    if config.skip_existing and marker_path.exists():
        _emit_progress(
            progress_callback,
            event="skipped",
            url=url,
            host=host,
            method="skip-existing",
            percent=100.0,
            status="already downloaded",
        )
        return DownloadResult(
            url=url,
            host=host,
            success=True,
            method="skip-existing",
            attempts=0,
            code=0,
            detail="already downloaded",
        )

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
                    moved = _promote_job_outputs(job_dir, config.download_root, host, config)
                    if not moved:
                        detail = "gallery-dl finished without output files"
                    else:
                        _emit_progress(
                            progress_callback,
                            event="verify",
                            url=url,
                            host=host,
                            method="gallery-dl",
                            attempt=attempt,
                            percent=99.0,
                            status="verifying",
                        )
                        _mark_completed_download(marker_path, moved)
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
                    moved = _promote_job_outputs(job_dir, config.download_root, host, config)
                    if not moved:
                        detail = "yt-dlp finished without output files"
                    else:
                        _emit_progress(
                            progress_callback,
                            event="verify",
                            url=url,
                            host=host,
                            method="yt-dlp",
                            attempt=attempt,
                            percent=99.0,
                            status="verifying",
                        )
                        _mark_completed_download(marker_path, moved)
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
                config.direct_timeout_sec,
                progress_callback,
                d_start,
                d_end,
                attempt,
            )
            if success:
                moved = _promote_job_outputs(job_dir, config.download_root, host, config)
                if not moved:
                    detail = "direct-http finished without output files"
                else:
                    _emit_progress(
                        progress_callback,
                        event="verify",
                        url=url,
                        host=host,
                        method="direct-http",
                        attempt=attempt,
                        percent=99.0,
                        status="verifying",
                    )
                    _mark_completed_download(marker_path, moved)
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

        code, saw_file_output, output = _run_stream(
            cmd,
            line_callback=_line_callback,
            timeout_sec=config.subprocess_timeout_sec,
        )
        if code == 0:
            return True, code, None
        if code == 8 and saw_file_output:
            return True, code, None
        if code == SUBPROCESS_TIMEOUT_CODE:
            detail = f"gallery-dl timed out after {config.subprocess_timeout_sec}s"
            continue
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

    code, _, output = _run_stream(
        cmd,
        line_callback=_line_callback,
        timeout_sec=config.subprocess_timeout_sec,
    )
    if code == 0:
        return True, code, None
    if code == SUBPROCESS_TIMEOUT_CODE:
        return False, code, f"yt-dlp timed out after {config.subprocess_timeout_sec}s"
    return False, code, _tail(output)


def _try_direct_download(
    url: str,
    host: str,
    host_dir: Path,
    user_agent: str,
    timeout_sec: int,
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
        with urlopen(request, timeout=timeout_sec) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
            if not _is_downloadable_content_type(content_type) and not looks_like_direct_media_url(url):
                label = content_type or "unknown"
                return False, f"direct-http rejected content-type {label}", None
            content_disposition = response.headers.get("Content-Disposition") or ""
            filename = _filename_from_content_disposition(content_disposition) or _pick_filename(url, content_type)
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
                        bytes_read=read,
                        bytes_total=total,
                    )
            return True, None, target
    except Exception as exc:
        return False, str(exc), None


def _reset_job_dir(job_dir: Path) -> None:
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    job_dir.mkdir(parents=True, exist_ok=True)


def classify_download_output_path(path: Path) -> str:
    """Bucket name for index manifests: image | video | archive | other."""

    return _content_type_folder(path)


def _content_type_folder(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in ARCHIVE_EXTENSIONS:
        return "archive"
    guessed_type, _encoding = mimetypes.guess_type(path.name)
    if guessed_type:
        if guessed_type.startswith("image/"):
            return "image"
        if guessed_type.startswith(("video/", "audio/")):
            return "video"
        if guessed_type in (
            "application/zip",
            "application/x-7z-compressed",
            "application/x-rar-compressed",
            "application/x-tar",
            "application/gzip",
        ):
            return "archive"
    return "other"


def _promote_job_outputs(job_dir: Path, download_root: Path, host: str, config: DownloadConfig) -> list[Path]:
    files = sorted(path for path in job_dir.rglob("*") if path.is_file())
    moved: list[Path] = []
    for source_path in files:
        safe_name = _safe_filename(source_path.name)
        if config.structured_downloads:
            host_dir = download_root / "by-host" / _safe_segment(host)
            host_dir.mkdir(parents=True, exist_ok=True)
            target = _unique_path(host_dir / safe_name)
            shutil.move(str(source_path), str(target))
            moved.append(target)
            type_name = _content_type_folder(target)
            type_dir = download_root / "by-type" / type_name
            type_dir.mkdir(parents=True, exist_ok=True)
            link_name = _unique_path(type_dir / target.name)
            rel = os.path.relpath(target, link_name.parent)
            try:
                if link_name.exists() or link_name.is_symlink():
                    link_name.unlink()
                os.symlink(rel, link_name)
            except OSError:
                shutil.copy2(target, link_name)
        else:
            bucket = _media_bucket_for_path(source_path)
            target_dir = download_root / bucket
            target_dir.mkdir(parents=True, exist_ok=True)
            target = _unique_path(target_dir / safe_name)
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
    timeout_sec: int = 240,
) -> tuple[int, bool, str]:
    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
            check=False,
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        captured = (exc.stdout or "") + "\n" + (exc.stderr or "")
        return SUBPROCESS_TIMEOUT_CODE, False, captured.strip()
    except Exception as exc:
        return 1, False, str(exc)

    output = completed.stdout or ""
    output_lines = [line.rstrip("\n") for line in output.splitlines()]
    saw_file_output = False
    for text in output_lines:
        if _looks_like_file_output(text):
            saw_file_output = True
        if line_callback:
            line_callback(text)
    return completed.returncode, saw_file_output, "\n".join(output_lines)


def _looks_like_file_output(line: str) -> bool:
    if not line:
        return False
    lowered = line.lower().strip()
    if lowered.startswith("# /") or lowered.startswith("/"):
        return True
    if "destination:" in lowered or "merging formats into" in lowered:
        return True
    if "writing metadata to" in lowered or "saving to:" in lowered:
        return True
    return False


def _pick_filename(url: str, content_type: str) -> str:
    parsed = urlparse(url)
    raw_name = Path(unquote(parsed.path)).name
    if not raw_name:
        query = parse_qs(parsed.query)
        for key in ("filename", "file", "name", "download"):
            values = query.get(key, [])
            if values:
                raw_name = values[0]
                break
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
    if len(cleaned) > MAX_FILENAME_CHARS:
        stem = Path(cleaned).stem[: MAX_FILENAME_CHARS - 20]
        suffix = Path(cleaned).suffix[:16]
        cleaned = f"{stem}{suffix}"
    return cleaned


def _filename_from_content_disposition(header: str) -> Optional[str]:
    if not header:
        return None
    match = CONTENT_DISPOSITION_FILENAME.search(header)
    if not match:
        return None
    value = unquote(match.group(1).strip())
    if not value:
        return None
    return _safe_filename(Path(value).name)


def _completed_marker_path(config: DownloadConfig, url: str) -> Path:
    digest = sha1(url.encode("utf-8")).hexdigest()
    base = config.completed_marker_root if config.completed_marker_root is not None else (config.download_root / "_meta")
    marker_dir = base / "completed_urls"
    return marker_dir / f"{digest}.done"


def _mark_completed_download(marker_path: Path, moved_files: Sequence[Path]) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(str(path) for path in moved_files)
    marker_path.write_text(payload, encoding="utf-8")


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
    if "phase" not in payload:
        ev = str(payload.get("event") or "")
        payload["phase"] = {
            "start": "queued",
            "attempt": "resolving",
            "method": "fetching",
            "progress": "fetching",
            "retry": "retrying",
            "verify": "verifying",
            "success": "complete",
            "skipped": "complete",
            "failure": "failed",
        }.get(ev, "running")
    payload["timestamp_ms"] = int(time.time() * 1000)
    callback(payload)
