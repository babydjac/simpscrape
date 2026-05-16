from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from html import unescape
import base64
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
ENCODED_URL_PATTERN = re.compile(r"https?%3A%2F%2F[^\s\"'<>\\]+", re.IGNORECASE)

MEDIA_FIELDS = ("links", "images", "videos", "iframes", "embeds", "attachments")
TEXT_FIELDS = ("post_content", "quotes")
# Extra string fields scanned only in "deep" capture mode (broader URL harvesting).
DEEP_TEXT_FIELDS = ("title", "signature", "user_title", "custom_fields")


@dataclass(frozen=True)
class UrlSource:
    url: str
    host: str
    source_field: str
    post_id: Optional[str]
    post_author: Optional[str]
    post_date: Optional[str]
    thread_url: Optional[str]


@dataclass
class DiscoveryResult:
    sources: List[UrlSource]
    unique_urls: List[str]
    url_hosts: Dict[str, str]
    host_counts: Dict[str, int]
    host_unique_counts: Dict[str, int]
    host_urls: Dict[str, List[str]]

    def sources_as_dicts(self) -> List[Dict[str, Any]]:
        return [asdict(source) for source in self.sources]


def discover_urls(records: List[Dict[str, Any]], *, deep: bool = False) -> DiscoveryResult:
    sources: List[UrlSource] = []
    unique_urls: List[str] = []
    seen_urls: set[str] = set()
    url_hosts: Dict[str, str] = {}
    host_counts: Counter[str] = Counter()
    host_unique_counts: Counter[str] = Counter()
    host_urls: Dict[str, List[str]] = defaultdict(list)

    for record in records:
        base_url = _record_base_url(record)
        for source_field in MEDIA_FIELDS:
            raw_value = record.get(source_field)
            for raw_url in _iter_candidate_urls(raw_value, allow_regex_fallback=True):
                normalized = _normalize_url(raw_url, base_url)
                if not normalized:
                    continue
                host = normalized_host(normalized)
                source = UrlSource(
                    url=normalized,
                    host=host,
                    source_field=source_field,
                    post_id=_to_optional_str(record.get("post_id")),
                    post_author=_to_optional_str(record.get("post_author")),
                    post_date=_to_optional_str(record.get("post_date")),
                    thread_url=_to_optional_str(record.get("web_scraper_start_url")),
                )
                sources.append(source)
                host_counts[host] += 1
                if normalized not in seen_urls:
                    seen_urls.add(normalized)
                    unique_urls.append(normalized)
                    url_hosts[normalized] = host
                    host_unique_counts[host] += 1
                    host_urls[host].append(normalized)

        for source_field in TEXT_FIELDS:
            raw_value = record.get(source_field)
            for raw_url in _iter_candidate_urls(raw_value, allow_regex_fallback=True):
                normalized = _normalize_url(raw_url, base_url)
                if not normalized:
                    continue
                host = normalized_host(normalized)
                source = UrlSource(
                    url=normalized,
                    host=host,
                    source_field=source_field,
                    post_id=_to_optional_str(record.get("post_id")),
                    post_author=_to_optional_str(record.get("post_author")),
                    post_date=_to_optional_str(record.get("post_date")),
                    thread_url=_to_optional_str(record.get("web_scraper_start_url")),
                )
                sources.append(source)
                host_counts[host] += 1
                if normalized not in seen_urls:
                    seen_urls.add(normalized)
                    unique_urls.append(normalized)
                    url_hosts[normalized] = host
                    host_unique_counts[host] += 1
                    host_urls[host].append(normalized)

        if deep:
            for source_field in DEEP_TEXT_FIELDS:
                raw_value = record.get(source_field)
                for raw_url in _iter_candidate_urls(raw_value, allow_regex_fallback=True):
                    normalized = _normalize_url(raw_url, base_url)
                    if not normalized:
                        continue
                    host = normalized_host(normalized)
                    source = UrlSource(
                        url=normalized,
                        host=host,
                        source_field=f"deep:{source_field}",
                        post_id=_to_optional_str(record.get("post_id")),
                        post_author=_to_optional_str(record.get("post_author")),
                        post_date=_to_optional_str(record.get("post_date")),
                        thread_url=_to_optional_str(record.get("web_scraper_start_url")),
                    )
                    sources.append(source)
                    host_counts[host] += 1
                    if normalized not in seen_urls:
                        seen_urls.add(normalized)
                        unique_urls.append(normalized)
                        url_hosts[normalized] = host
                        host_unique_counts[host] += 1
                        host_urls[host].append(normalized)

    return DiscoveryResult(
        sources=sources,
        unique_urls=unique_urls,
        url_hosts=url_hosts,
        host_counts=dict(host_counts),
        host_unique_counts=dict(host_unique_counts),
        host_urls={host: sorted(urls) for host, urls in host_urls.items()},
    )


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


def _record_base_url(record: Dict[str, Any]) -> str:
    raw = record.get("web_scraper_start_url")
    if raw is None:
        return ""
    return str(raw)


def _iter_candidate_urls(value: Any, allow_regex_fallback: bool) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_candidate_urls(item, allow_regex_fallback=allow_regex_fallback)
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_candidate_urls(item, allow_regex_fallback=allow_regex_fallback)
        return

    text = unescape(str(value)).replace("\\/", "/").strip()
    if not text:
        return

    yielded = False
    for token in _extract_urls_from_text(text):
        yielded = True
        yield token

    if not yielded and allow_regex_fallback:
        maybe_url = _clean_token(text)
        if maybe_url and _looks_like_urlish_value(maybe_url):
            yield maybe_url


def _extract_urls_from_text(text: str) -> Iterable[str]:
    for match in URL_PATTERN.findall(text):
        cleaned = _clean_token(match)
        if cleaned:
            yield cleaned

    for encoded in ENCODED_URL_PATTERN.findall(text):
        decoded = unquote(encoded)
        cleaned = _clean_token(decoded)
        if cleaned:
            yield cleaned


def _clean_token(token: str) -> Optional[str]:
    value = token.strip()
    if not value:
        return None
    value = value.strip("<>'\"")
    while value and value[-1] in ".,;:!?)]}":
        if value[-1] == ")" and value.count("(") >= value.count(")"):
            break
        value = value[:-1]
    return value or None


def _normalize_url(raw_url: str, base_url: str) -> Optional[str]:
    url = _clean_token(unescape(raw_url))
    if not url:
        return None

    lowered = url.lower()
    if lowered.startswith(("javascript:", "data:", "mailto:", "tel:")):
        return None

    if url.startswith("//"):
        url = "https:" + url
    elif base_url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = urljoin(base_url, url)

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    redirect_target = _decode_redirect_target(parsed)
    if redirect_target:
        url = redirect_target
        parsed = urlparse(url)

    host = normalized_host(url)
    if not _is_valid_host(host):
        return None
    if parsed.fragment:
        parsed = parsed._replace(fragment="")
        url = parsed.geturl()
    return url


def _to_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decode_redirect_target(parsed_url) -> Optional[str]:
    path = (parsed_url.path or "").lower()
    if not _is_redirect_wrapper_path(path):
        return None
    query = parse_qs(parsed_url.query)
    for key in ("to", "url", "u", "target", "dest", "destination"):
        decoded = _decode_redirect_query_value(query.get(key, [None])[0])
        if decoded:
            return decoded
    return None


def _is_redirect_wrapper_path(path: str) -> bool:
    if not path:
        return False
    return (
        "redirect" in path
        or "link-confirmation" in path
        or path == "/goto"
        or path.startswith("/goto/")
        or path == "/out"
        or path.endswith("/out/")
    )


def _decode_redirect_query_value(raw_value: Optional[str]) -> Optional[str]:
    if not raw_value:
        return None
    decoded_query = unquote(str(raw_value)).strip()
    if decoded_query.startswith(("http://", "https://")):
        return decoded_query

    # Some forums store outbound targets as base64 and may hand query parsers spaces in place of '+'.
    normalized = decoded_query.replace(" ", "+")
    padded = normalized + "=" * (-len(normalized) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            decoded_base64 = decoder(padded.encode("utf-8")).decode("utf-8", "ignore").strip()
        except Exception:
            continue
        if decoded_base64.startswith(("http://", "https://")):
            return decoded_base64
    return None


def _is_valid_host(host: str) -> bool:
    if not host:
        return False
    if "*" in host:
        return False
    if " " in host:
        return False
    return bool(re.fullmatch(r"[a-z0-9.-]+", host))


def _looks_like_urlish_value(value: str) -> bool:
    if not value:
        return False
    if any(ch.isspace() for ch in value):
        return False
    if value.startswith(("http://", "https://", "//", "/")):
        return True
    return bool(re.match(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}($|/)", value))
