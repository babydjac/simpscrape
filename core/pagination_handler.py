import re
from typing import Optional, Set
from urllib.parse import parse_qs, urlencode, urlparse

from .sitemap_loader import SelectorNode


class PaginationHandler:
    def __init__(self, dom_executor):
        self.dom = dom_executor

    def get_total_pages(self) -> Optional[int]:
        nodes = self.dom.query_selector_all("a.pageNav-page")
        numbers = []
        for node in nodes:
            text = (self.dom.get_text(node) or "").strip()
            if text.isdigit():
                numbers.append(int(text))
        nodes = self.dom.query_selector_all(".pagination-content button[data-pagination]")
        for node in nodes:
            raw = (self.dom.get_attribute(node, "data-pagination") or self.dom.get_text(node) or "").strip()
            if raw.isdigit():
                numbers.append(int(raw))
        if numbers:
            return max(numbers)
        return None

    def get_next_url(
        self,
        selector: SelectorNode,
        visited: Set[str],
        max_pages: Optional[int],
        page_count: int,
    ) -> Optional[str]:
        if max_pages is not None and page_count >= max_pages:
            return None
        nodes = self.dom.query_selector_all(selector.selector)
        if not nodes:
            return self._fallback_next_url(visited)
        node = nodes[0]
        current_url = self.dom.get_page_url()
        attr_name = selector.extract_attribute or "href"
        raw_value = self.dom.get_attribute(node, attr_name)
        next_url = self._resolve_next_url(raw_value, current_url, attr_name)
        if not next_url:
            return self._fallback_next_url(visited)
        if next_url in visited:
            return None
        if not self._same_origin(next_url, current_url):
            return None
        return next_url

    def _same_origin(self, url_a: str, url_b: str) -> bool:
        a = urlparse(url_a)
        b = urlparse(url_b)
        return (a.scheme, a.netloc) == (b.scheme, b.netloc)

    def _resolve_next_url(self, raw_value: Optional[str], current_url: str, attr_name: str) -> Optional[str]:
        value = (raw_value or "").strip()
        if not value:
            return None
        if attr_name == "data-pagination" or value.isdigit():
            try:
                return self._replace_page_number(current_url, int(value))
            except ValueError:
                return None
        return self.dom.resolve_url(value, current_url)

    def _fallback_next_url(self, visited: Set[str]) -> Optional[str]:
        current_url = self.dom.get_page_url()
        total_pages = self.get_total_pages()
        current_page = self._current_page_number(current_url)
        if total_pages is None or current_page >= total_pages:
            return None
        next_url = self._replace_page_number(current_url, current_page + 1)
        if next_url in visited:
            return None
        if not self._same_origin(next_url, current_url):
            return None
        return next_url

    def _current_page_number(self, current_url: str) -> int:
        nodes = self.dom.query_selector_all(".pagination-content button.active[data-pagination]")
        for node in nodes:
            raw = (self.dom.get_attribute(node, "data-pagination") or self.dom.get_text(node) or "").strip()
            if raw.isdigit():
                return int(raw)

        nodes = self.dom.query_selector_all("a.pageNav-page.pageNav-page--current, a.pageNav-page[aria-current='page']")
        for node in nodes:
            raw = (self.dom.get_text(node) or "").strip()
            if raw.isdigit():
                return int(raw)

        parsed = urlparse(current_url)
        match = re.search(r"/page/(\d+)(?:/)?$", parsed.path)
        if match:
            return int(match.group(1))

        query = parse_qs(parsed.query)
        raw_page = query.get("page", [None])[0]
        if raw_page and raw_page.isdigit():
            return int(raw_page)
        return 1

    def _replace_page_number(self, current_url: str, page_number: int) -> str:
        parsed = urlparse(current_url)
        path = parsed.path or "/"
        path = re.sub(r"/page/\d+(?:/)?$", "", path).rstrip("/") or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)
        query.pop("page", None)
        if page_number > 1:
            base_path = "" if path == "/" else path
            path = f"{base_path}/page/{page_number}"
        query_string = urlencode(query, doseq=True)
        return parsed._replace(path=path, query=query_string).geturl()
