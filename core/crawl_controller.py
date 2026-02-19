from dataclasses import dataclass
from typing import List, Optional, Set

from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from .data_collector import DataCollector
from .pagination_handler import PaginationHandler
from .selector_engine import SelectorEngine
from .sitemap_loader import SelectorNode, Sitemap


@dataclass
class CrawlConfig:
    delay_ms: int = 0
    max_pages: Optional[int] = None
    nav_timeout_ms: int = 60000
    idle_timeout_ms: int = 5000


class CrawlController:
    def __init__(self, dom_executor, sitemap: Sitemap, config: CrawlConfig):
        self.dom = dom_executor
        self.sitemap = sitemap
        self.config = config
        self.selector_engine = SelectorEngine(dom_executor)
        self.pagination = PaginationHandler(dom_executor)
        self.collector = DataCollector()
        self.order_seed = None
        self.order_index = 0

    def crawl(self) -> List[dict]:
        visited: Set[str] = set()
        progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
        )
        with progress:
            for start_url in self.sitemap.start_urls:
                task_id = progress.add_task("Starting", total=None)
                self._crawl_from(start_url, visited, progress, task_id)
        return self.collector.all_records()

    def _crawl_from(self, start_url: str, visited: Set[str], progress: Progress, task_id: int) -> None:
        current_url = start_url
        page_count = 0
        pagination_selector = self._get_pagination_selector()
        total_pages: Optional[int] = None
        if self.order_seed is None:
            import time
            self.order_seed = int(time.time())

        while current_url:
            if current_url in visited:
                break
            visited.add(current_url)
            try:
                self.dom.driver.goto(current_url, timeout_ms=self.config.nav_timeout_ms)
            except Exception as exc:
                progress.update(task_id, description=f"Navigation failed: {exc}")
                break
            self.dom.wait_for_idle(timeout_ms=self.config.idle_timeout_ms)
            if self.config.delay_ms:
                self.dom.driver.sleep_ms(self.config.delay_ms)

            if page_count == 0 and pagination_selector:
                total_pages = self.pagination.get_total_pages()
                if total_pages:
                    progress.update(task_id, total=total_pages)

            page_parent_ids = ["_root"]
            if pagination_selector:
                page_parent_ids.append(pagination_selector.id)
            records = self.selector_engine.extract_records(self.sitemap.selectors, page_parent_ids)
            page_fields = self.selector_engine.extract_page_fields(self.sitemap.selectors)
            for record in records:
                record.update(page_fields)
                self.order_index += 1
                record["web_scraper_order"] = f"{self.order_seed}-{self.order_index}"
                record["web_scraper_start_url"] = start_url
            self.collector.add_records(records)
            page_count += 1
            if total_pages:
                progress.update(
                    task_id,
                    advance=1,
                    description=f"Page {page_count}/{total_pages} | +{len(records)} posts",
                )
            else:
                progress.update(
                    task_id,
                    advance=1,
                    description=f"Page {page_count} | +{len(records)} posts",
                )

            if not pagination_selector:
                break
            next_url = self.pagination.get_next_url(
                pagination_selector,
                visited,
                self.config.max_pages,
                page_count,
            )
            current_url = next_url

    def _get_pagination_selector(self) -> Optional[SelectorNode]:
        for selector in self.sitemap.selectors:
            if selector.type == "Pagination":
                return selector
            if selector.id == "next_page" and selector.type in {"SelectorLink", "Pagination"}:
                return selector
        return None
