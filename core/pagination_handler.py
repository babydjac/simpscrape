from typing import Optional, Set
from urllib.parse import urlparse

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
            return None
        node = nodes[0]
        href = self.dom.get_attribute(node, selector.extract_attribute or "href")
        if not href:
            return None
        next_url = self.dom.resolve_url(href, self.dom.get_page_url())
        if next_url in visited:
            return None
        if not self._same_origin(next_url, self.dom.get_page_url()):
            return None
        return next_url

    def _same_origin(self, url_a: str, url_b: str) -> bool:
        a = urlparse(url_a)
        b = urlparse(url_b)
        return (a.scheme, a.netloc) == (b.scheme, b.netloc)
