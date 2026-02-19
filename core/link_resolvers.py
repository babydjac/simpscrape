from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
import base64
from functools import lru_cache
from hashlib import sha256
import json
import re
import socket
from typing import Any, Callable, Dict, Iterable, Optional, Sequence
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_TIMEOUT = 35
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HREF_PATTERN = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
PASSWORD_PATTERN = re.compile(
    r"(?:(?:pass(?:word|wd)?|pw|key)\s*[:=]\s*([^\s,;]+))",
    re.IGNORECASE,
)
BUNKR_ALBUM_PATTERN = re.compile(r"/a/([^/?#]+)", re.IGNORECASE)
BUNKR_SLUG_PATTERN = re.compile(r"/(?:f|v|d)/([^/?#]+)", re.IGNORECASE)
TURBO_ALBUM_PATTERN = re.compile(r"/a/([^/?#]+)", re.IGNORECASE)
TURBO_SINGLE_PATTERN = re.compile(r"/(?:v|d|embed)/([^/?#]+)", re.IGNORECASE)
GOFILE_FOLDER_PATTERN = re.compile(r"/d/([^/?#]+)", re.IGNORECASE)
REDFGIFS_USER_PATTERN = re.compile(r"/users/([^/?#]+)", re.IGNORECASE)
REDFGIFS_SINGLE_PATTERN = re.compile(
    r"(?:/ifr/|/watch/|/gifs/detail/|/gifs/watch/)([a-z0-9_-]+)|/([a-z0-9_-]+)$",
    re.IGNORECASE,
)
MEDIA_CONTENT_PREFIXES = ("video/", "image/", "audio/")
MEDIA_CONTENT_TYPES = {
    "application/octet-stream",
    "application/zip",
    "application/x-7z-compressed",
    "application/x-rar-compressed",
    "application/x-tar",
    "application/gzip",
}
BUNKR_FALLBACK_CDN_HOSTS = (
    "c2ke.scdn.st",
    "c2rm.scdn.st",
    "c3pz.scdn.st",
    "c1sp.scdn.st",
    "c2wi.scdn.st",
    "c3wi.scdn.st",
    "c3mb.scdn.st",
    "c3bc.scdn.st",
    "fries.bunkr.ru",
)
MAX_BUNKR_RELATED_SLUGS = 10


@dataclass(frozen=True)
class ResolvedLink:
    source_url: str
    source_host: str
    resolved_url: str
    resolved_host: str
    resolver: str

    def as_dict(self) -> dict:
        return asdict(self)


ProgressCallback = Callable[[dict[str, Any]], None]


def resolve_links_bulk(
    urls: Sequence[str],
    source_hosts: Dict[str, str],
    password_hints: Optional[Dict[str, list[str]]] = None,
    workers: int = 6,
    progress_callback: Optional[ProgressCallback] = None,
) -> list[ResolvedLink]:
    if workers < 1:
        raise ValueError("workers must be >= 1")

    hints = password_hints or {}
    resolved_links: list[ResolvedLink] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_resolve_one, url, hints.get(url, []), progress_callback): url
            for url in urls
        }
        for future in as_completed(futures):
            source_url = futures[future]
            source_host = source_hosts.get(source_url, normalized_host(source_url))
            try:
                resolver, resolved_urls = future.result()
            except Exception:
                resolver = "identity"
                resolved_urls = [source_url]
            if not resolved_urls:
                resolved_urls = [source_url]
            for resolved_url in resolved_urls:
                if not resolved_url:
                    continue
                resolved_links.append(
                    ResolvedLink(
                        source_url=source_url,
                        source_host=source_host,
                        resolved_url=resolved_url,
                        resolved_host=normalized_host(resolved_url),
                        resolver=resolver,
                    )
                )

    # stable de-dup preserving first hit
    seen: set[tuple[str, str]] = set()
    deduped: list[ResolvedLink] = []
    for item in resolved_links:
        key = (item.source_url, item.resolved_url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def build_password_hints(
    records: list[dict[str, Any]],
    url_sources: Iterable[dict[str, Any]],
) -> Dict[str, list[str]]:
    post_candidates: Dict[str, set[str]] = {}
    for record in records:
        post_id = str(record.get("post_id") or "").strip()
        if not post_id:
            continue
        chunks: list[str] = []
        for field in ("post_content",):
            value = record.get(field)
            if isinstance(value, str):
                chunks.append(value)
        quotes = record.get("quotes")
        if isinstance(quotes, list):
            chunks.extend(str(item) for item in quotes if item)
        post_candidates[post_id] = _extract_password_candidates(" ".join(chunks))

    url_candidates: Dict[str, set[str]] = {}
    for source in url_sources:
        url = str(source.get("url") or "").strip()
        post_id = str(source.get("post_id") or "").strip()
        if not url:
            continue
        if not post_id or post_id not in post_candidates:
            continue
        if not post_candidates[post_id]:
            continue
        current = url_candidates.setdefault(url, set())
        current.update(post_candidates[post_id])

    return {url: sorted(values) for url, values in url_candidates.items() if values}


def normalized_host(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if "@" in host:
        host = host.split("@", 1)[1]
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def _resolve_one(
    url: str,
    password_hints: Sequence[str],
    progress_callback: Optional[ProgressCallback],
) -> tuple[str, list[str]]:
    host = normalized_host(url)
    _emit(progress_callback, event="resolve_start", url=url, host=host)

    try:
        if _is_bunkr_host(host):
            if BUNKR_ALBUM_PATTERN.search(urlparse(url).path):
                items = _resolve_bunkr_album(url, progress_callback)
                return "bunkr-album", items or [url]
            single = _resolve_bunkr_single(url)
            return ("bunkr-single", [single] if single else [url])

        if _is_turbo_host(host):
            path = urlparse(url).path
            if TURBO_ALBUM_PATTERN.search(path):
                items = _resolve_turbo_album(url, progress_callback)
                return "turbo-album", items or [url]
            single = _resolve_turbo_single(url)
            return ("turbo-single", [single] if single else [url])

        if host == "gofile.io" and GOFILE_FOLDER_PATTERN.search(urlparse(url).path):
            items = _resolve_gofile_folder(url, list(password_hints), progress_callback)
            return "gofile-folder", items or [url]

        if host.endswith("redgifs.com"):
            parsed_path = urlparse(url).path
            if REDFGIFS_USER_PATTERN.search(parsed_path):
                items = _resolve_redgifs_user(url, progress_callback)
                return "redgifs-user", items or [url]
            single = _resolve_redgifs_single(url)
            return ("redgifs-single", [single] if single else [url])

        if host == "pixeldrain.com":
            converted = _resolve_pixeldrain(url)
            return "pixeldrain", [converted] if converted else [url]
    except Exception:
        return "identity", [url]

    return "identity", [url]


def _resolve_bunkr_single(url: str) -> Optional[str]:
    parsed = urlparse(url)
    match = BUNKR_SLUG_PATTERN.search(parsed.path)
    if not match:
        return None
    slug = match.group(1)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    payload = _http_json(
        f"{origin}/api/vs",
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        body=json.dumps({"slug": slug}).encode("utf-8"),
    )
    resolved = _decode_bunkr_payload(payload)
    if not resolved:
        return None
    if _needs_bunkr_repair(resolved):
        related_hosts = _collect_related_bunkr_hosts(url, origin)
        repaired = _repair_bunkr_media_url(resolved, related_hosts)
        if repaired:
            return repaired
    return resolved


def _resolve_bunkr_album(url: str, progress_callback: Optional[ProgressCallback]) -> list[str]:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    base = f"{origin}{parsed.path}".rstrip("/")
    resolved: list[str] = []
    seen_slugs: set[str] = set()
    decoded_urls: list[str] = []

    for page in range(1, 251):
        page_url = f"{base}?page={page}"
        _emit(progress_callback, event="resolve_progress", url=url, status=f"bunkr album page {page}")
        html = _http_text(page_url)
        hrefs = _extract_hrefs(html, page_url)
        slugs = []
        for href in hrefs:
            match = BUNKR_SLUG_PATTERN.search(urlparse(href).path)
            if not match:
                continue
            slug = match.group(1)
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            slugs.append(slug)
        if not slugs:
            break
        for slug in slugs:
            payload = _http_json(
                f"{origin}/api/vs",
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                body=json.dumps({"slug": slug}).encode("utf-8"),
            )
            resolved_url = _decode_bunkr_payload(payload)
            if resolved_url:
                decoded_urls.append(resolved_url)

    candidate_hosts = [
        normalized_host(item)
        for item in decoded_urls
        if normalized_host(item) and not _needs_bunkr_repair(item)
    ]
    if not candidate_hosts:
        candidate_hosts = _collect_related_bunkr_hosts(url, origin)

    for item in decoded_urls:
        if _needs_bunkr_repair(item):
            repaired = _repair_bunkr_media_url(item, candidate_hosts)
            if repaired:
                resolved.append(repaired)
                repaired_host = normalized_host(repaired)
                if repaired_host and repaired_host not in candidate_hosts:
                    candidate_hosts.append(repaired_host)
                continue
        resolved.append(item)
    return _unique(resolved)


def _decode_bunkr_payload(payload: dict[str, Any]) -> Optional[str]:
    if not payload:
        return None
    encoded = payload.get("url")
    if not encoded:
        return None
    if not payload.get("encrypted"):
        return str(encoded)

    timestamp = int(payload.get("timestamp") or 0)
    if timestamp <= 0:
        return None
    key = f"SECRET_KEY_{timestamp // 3600}".encode("utf-8")
    binary = base64.b64decode(str(encoded))
    decoded = bytes(ch ^ key[i % len(key)] for i, ch in enumerate(binary))
    text = decoded.decode("utf-8", "ignore").strip()
    return text if text.startswith("http") else None


def _collect_related_bunkr_hosts(url: str, origin: str) -> list[str]:
    try:
        html = _http_text(url)
    except Exception:
        return []

    hrefs = _extract_hrefs(html, url)
    slugs: list[str] = []
    seen_slugs: set[str] = set()
    for href in hrefs:
        match = BUNKR_SLUG_PATTERN.search(urlparse(href).path)
        if not match:
            continue
        slug = match.group(1)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        slugs.append(slug)
        if len(slugs) >= MAX_BUNKR_RELATED_SLUGS:
            break

    hosts: list[str] = []
    for slug in slugs:
        try:
            payload = _http_json(
                f"{origin}/api/vs",
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                body=json.dumps({"slug": slug}).encode("utf-8"),
            )
        except Exception:
            continue

        resolved_url = _decode_bunkr_payload(payload)
        if not resolved_url:
            continue
        host = normalized_host(resolved_url)
        if not host or _is_bunkr_cache_host(host):
            continue
        hosts.append(host)

    return _unique(hosts)


def _repair_bunkr_media_url(url: str, candidate_hosts: Sequence[str]) -> Optional[str]:
    parsed = urlparse(url)
    if not parsed.path:
        return None

    original_host = normalized_host(url)
    fallback_hosts = [str(host).strip().lower() for host in candidate_hosts if str(host).strip()]
    fallback_hosts.extend(BUNKR_FALLBACK_CDN_HOSTS)
    fallback_hosts = [
        host
        for host in _unique(fallback_hosts)
        if host != original_host and not _is_bunkr_cache_host(host)
    ]

    if not fallback_hosts:
        return None

    scheme = parsed.scheme or "https"
    paths = _bunkr_candidate_paths(parsed.path)

    for host in fallback_hosts:
        if not _host_resolves(host):
            continue
        for path in paths:
            candidate = f"{scheme}://{host}{path}"
            if parsed.query:
                candidate = f"{candidate}?{parsed.query}"
            if _probe_media_candidate(candidate):
                return candidate

    return None


def _bunkr_candidate_paths(path: str) -> list[str]:
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return []

    variants: list[str] = []
    variants.append("/" + segments[-1])
    variants.append("/" + "/".join(segments))
    if len(segments) > 1:
        variants.append("/" + "/".join(segments[1:]))
    if len(segments) > 2:
        variants.append("/" + "/".join(segments[2:]))
    return _unique(variants)


def _probe_media_candidate(url: str) -> bool:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Range": "bytes=0-0",
        },
    )
    try:
        with urlopen(request, timeout=12) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if _is_media_content_type(content_type):
                return True
            return bool(content_type and content_type.startswith("application/"))
    except HTTPError as exc:
        # Some origins reject range probes with 416 but still indicate media content.
        if exc.code == 416:
            content_type = (exc.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            return _is_media_content_type(content_type)
        return False
    except URLError:
        return False
    except Exception:
        return False


def _is_media_content_type(content_type: str) -> bool:
    if not content_type:
        return False
    if content_type.startswith(MEDIA_CONTENT_PREFIXES):
        return True
    return content_type in MEDIA_CONTENT_TYPES


def _needs_bunkr_repair(url: str) -> bool:
    host = normalized_host(url)
    if not host:
        return False
    if _is_bunkr_cache_host(host):
        return True
    return not _host_resolves(host)


def _is_bunkr_cache_host(host: str) -> bool:
    return "bunkr-cache" in host


@lru_cache(maxsize=4096)
def _host_resolves(host: str) -> bool:
    if not host:
        return False
    try:
        socket.getaddrinfo(host, None)
        return True
    except socket.gaierror:
        return False


def _resolve_turbo_single(url: str) -> Optional[str]:
    parsed = urlparse(url)
    match = TURBO_SINGLE_PATTERN.search(parsed.path)
    if not match:
        return None
    video_id = match.group(1)
    return _resolve_turbo_id(video_id)


def _resolve_turbo_album(url: str, progress_callback: Optional[ProgressCallback]) -> list[str]:
    html = _http_text(url)
    hrefs = _extract_hrefs(html, url)
    ids: list[str] = []
    seen_ids: set[str] = set()
    for href in hrefs:
        match = TURBO_SINGLE_PATTERN.search(urlparse(href).path)
        if not match:
            continue
        value = match.group(1)
        if value in seen_ids:
            continue
        seen_ids.add(value)
        ids.append(value)

    resolved: list[str] = []
    for index, value in enumerate(ids, start=1):
        _emit(progress_callback, event="resolve_progress", url=url, status=f"turbo album item {index}/{len(ids)}")
        direct = _resolve_turbo_id(value)
        if direct:
            resolved.append(direct)
    return _unique(resolved)


def _resolve_turbo_id(video_id: str) -> Optional[str]:
    endpoints = [
        f"https://turbo.cr/api/sign?v={quote(video_id)}",
        f"https://turbo.cr/sign?v={quote(video_id)}",
    ]
    for endpoint in endpoints:
        payload = _http_json(endpoint, headers={"Accept": "application/json", "Referer": f"https://turbo.cr/embed/{video_id}"})
        if payload.get("success") and payload.get("url"):
            direct = str(payload["url"])
            filename = payload.get("original_filename")
            if filename and "fn=" not in direct:
                separator = "&" if "?" in direct else "?"
                direct = f"{direct}{separator}fn={quote(str(filename))}"
            return direct
    return f"https://turbo.cr/d/{video_id}"


def _resolve_gofile_folder(
    url: str,
    password_hints: Sequence[str],
    progress_callback: Optional[ProgressCallback],
) -> list[str]:
    match = GOFILE_FOLDER_PATTERN.search(urlparse(url).path)
    if not match:
        return []
    folder_id = match.group(1)
    website_token = _gofile_get_website_token()
    account_token = _gofile_create_account_token(website_token)

    payload = _gofile_contents(folder_id, website_token, account_token, password_hash=None)
    if payload.get("status") == "error-passwordRequired" and password_hints:
        for guess in password_hints:
            hashed = sha256(guess.encode("utf-8")).hexdigest()
            payload = _gofile_contents(folder_id, website_token, account_token, password_hash=hashed)
            if payload.get("status") == "ok":
                break

    if payload.get("status") != "ok":
        return []

    resolved: list[str] = []

    def walk(node: dict[str, Any], depth: int = 0) -> None:
        children = node.get("children") or {}
        for child in children.values():
            ctype = str(child.get("type") or "")
            if ctype == "file":
                direct = child.get("directLink") or child.get("link") or child.get("downloadLink")
                if direct:
                    resolved.append(str(direct))
            elif ctype == "folder":
                folder_code = child.get("id") or child.get("code")
                if not folder_code:
                    continue
                _emit(
                    progress_callback,
                    event="resolve_progress",
                    url=url,
                    status=f"gofile folder depth {depth + 1}",
                )
                nested = _gofile_contents(str(folder_code), website_token, account_token, password_hash=None)
                if nested.get("status") == "ok":
                    walk(nested.get("data") or {}, depth + 1)

    walk(payload.get("data") or {})
    return _unique(resolved)


def _gofile_get_website_token() -> str:
    candidates = [
        "https://gofile.io/dist/js/config.js",
        "https://gofile.io/dist/js/alljs.js",
        "https://gofile.io/dist/js/global.js",
    ]
    patterns = [
        re.compile(r'\bwt\s*=\s*"([^"]+)"'),
        re.compile(r'fetchData\.wt\s*=\s*"([^"]+)"'),
        re.compile(r'"wt"\s*:\s*"([^"]+)"'),
    ]
    for candidate in candidates:
        text = _http_text(candidate)
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                token = match.group(1).strip()
                if token:
                    return token
    raise RuntimeError("could not extract gofile website token")


def _gofile_create_account_token(website_token: str) -> str:
    payload = _http_json(
        "https://api.gofile.io/accounts",
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-website-token": website_token,
        },
        body=b"{}",
    )
    token = payload.get("data", {}).get("token")
    if not token:
        raise RuntimeError("could not create gofile account token")
    return str(token)


def _gofile_contents(
    content_id: str,
    website_token: str,
    account_token: str,
    password_hash: Optional[str],
) -> dict[str, Any]:
    query: dict[str, str] = {}
    if password_hash:
        query["password"] = password_hash
    endpoint = f"https://api.gofile.io/contents/{quote(content_id)}"
    if query:
        endpoint += "?" + urlencode(query)
    return _http_json(
        endpoint,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {account_token}",
            "x-website-token": website_token,
        },
    )


def _resolve_redgifs_single(url: str) -> Optional[str]:
    parsed = urlparse(url)
    match = REDFGIFS_SINGLE_PATTERN.search(parsed.path)
    if not match:
        return None
    clip_id = match.group(1) or match.group(2)
    if not clip_id:
        return None
    token = _redgifs_token()
    payload = _http_json(
        f"https://api.redgifs.com/v2/gifs/{quote(clip_id)}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    urls = payload.get("gif", {}).get("urls") or payload.get("urls") or {}
    return _pick_redgifs_best(urls)


def _resolve_redgifs_user(url: str, progress_callback: Optional[ProgressCallback]) -> list[str]:
    parsed = urlparse(url)
    match = REDFGIFS_USER_PATTERN.search(parsed.path)
    if not match:
        return []
    username = match.group(1)
    token = _redgifs_token()
    resolved: list[str] = []
    page = 1
    max_pages = 200

    while page <= max_pages:
        _emit(progress_callback, event="resolve_progress", url=url, status=f"redgifs user page {page}")
        payload = _http_json(
            f"https://api.redgifs.com/v2/users/{quote(username)}/search?order=new&page={page}&count=80",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        items = payload.get("gifs") or payload.get("results") or []
        if not items:
            break
        for item in items:
            urls = item.get("urls") or item.get("gif", {}).get("urls") or {}
            chosen = _pick_redgifs_best(urls)
            if chosen:
                resolved.append(chosen)
        total_pages = int(payload.get("pages") or page)
        if page >= total_pages:
            break
        page += 1

    return _unique(resolved)


def _redgifs_token() -> str:
    payload = _http_json(
        "https://api.redgifs.com/v2/auth/temporary",
        headers={"Accept": "application/json"},
    )
    token = payload.get("token")
    if not token:
        raise RuntimeError("unable to get redgifs token")
    return str(token)


def _pick_redgifs_best(urls: dict[str, Any]) -> Optional[str]:
    if not isinstance(urls, dict):
        return None
    for key in ("hd", "hd1080", "hd720", "sd", "mp4"):
        value = urls.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    for value in urls.values():
        if isinstance(value, str) and value.startswith("http") and ".mp4" in value:
            return value
    return None


def _resolve_pixeldrain(url: str) -> Optional[str]:
    parsed = urlparse(url)
    path = parsed.path or ""
    if path.startswith("/u/"):
        file_id = path.split("/u/", 1)[1].split("/", 1)[0]
        if not file_id:
            return None
        return f"https://pixeldrain.com/api/file/{quote(file_id)}?download"
    if path.startswith("/l/"):
        list_id = path.split("/l/", 1)[1].split("/", 1)[0]
        if not list_id:
            return None
        return f"https://pixeldrain.com/api/list/{quote(list_id)}/zip"
    return url


def _extract_password_candidates(text: str) -> set[str]:
    values: set[str] = set()
    if not text:
        return values
    for match in PASSWORD_PATTERN.findall(text):
        candidate = str(match).strip().strip(".,;:()[]{}<>\"'")
        if not candidate:
            continue
        if len(candidate) > 128:
            continue
        values.add(candidate)
    return values


def _is_bunkr_host(host: str) -> bool:
    return bool(re.search(r"(?:^|\.)(?:bunkr|bunkrr|bunkrrr|bunkr-cache)\.", host))


def _is_turbo_host(host: str) -> bool:
    return host == "turbo.cr" or host.endswith(".turbo.cr")


def _http_text(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = DEFAULT_TIMEOUT) -> str:
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    request = Request(url, headers=req_headers)
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return raw.decode("utf-8", "ignore")


def _http_json(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    request = Request(url, data=body, headers=req_headers, method=method.upper())
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    text = raw.decode("utf-8", "ignore")
    return json.loads(text or "{}")


def _extract_hrefs(html: str, base_url: str) -> list[str]:
    hrefs: list[str] = []
    for match in HREF_PATTERN.findall(html):
        value = (match or "").strip()
        if not value:
            continue
        if value.startswith("#"):
            continue
        hrefs.append(urljoin(base_url, value))
    return hrefs


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _emit(callback: Optional[ProgressCallback], **payload: Any) -> None:
    if callback is None:
        return
    callback(payload)
