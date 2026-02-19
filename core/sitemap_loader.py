import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class SelectorNode:
    id: str
    parent_selectors: List[str]
    type: str
    selector: str
    multiple: bool
    regex: Optional[str] = None
    extract_attribute: Optional[str] = None


@dataclass
class Sitemap:
    start_urls: List[str]
    selectors: List[SelectorNode]


def _normalize_selector(raw: Dict[str, Any]) -> SelectorNode:
    return SelectorNode(
        id=raw["id"],
        parent_selectors=raw.get("parentSelectors", []),
        type=raw["type"],
        selector=raw.get("selector", ""),
        multiple=bool(raw.get("multiple", False)),
        regex=raw.get("regex") or None,
        extract_attribute=raw.get("extractAttribute") or None,
    )


def load_sitemap(path: Path) -> Sitemap:
    payload = json.loads(path.read_text())
    return load_sitemap_payload(payload)


def load_sitemap_payload(payload: Dict[str, Any]) -> Sitemap:
    if "sitemaps" in payload:
        if not payload["sitemaps"]:
            raise ValueError("Sitemap JSON contains no sitemaps")
        sitemap = payload["sitemaps"][0]
    else:
        sitemap = payload

    start_urls = sitemap.get("startUrl") or sitemap.get("startUrls") or []
    if isinstance(start_urls, str):
        start_urls = [start_urls]

    selectors = sitemap.get("selectors", [])
    if not selectors:
        raise ValueError("Sitemap has no selectors")

    normalized = [_normalize_selector(selector) for selector in selectors]
    return Sitemap(start_urls=start_urls, selectors=normalized)
