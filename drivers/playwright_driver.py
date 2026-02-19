from typing import List, Optional
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


class PlaywrightDriver:
    def __init__(
        self,
        headless: bool = True,
        storage_state: str = None,
        user_agent: str = None,
        cookies: list = None,
    ):
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=headless)
        context_kwargs = {}
        if storage_state:
            context_kwargs["storage_state"] = storage_state
        if user_agent:
            context_kwargs["user_agent"] = user_agent
        self._context = self._browser.new_context(**context_kwargs)
        if cookies:
            self._context.add_cookies(cookies)
        self._page = self._context.new_page()

    def goto(self, url: str, timeout_ms: int = 60000):
        self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    def wait_for_idle(self, timeout_ms: int = 30000):
        try:
            self._page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            # Some pages keep background connections open; fall back to a lighter readiness check.
            self._page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

    def query_selector_all(self, selector: str, context=None) -> List:
        if context is None:
            return self._page.query_selector_all(selector)
        return context.query_selector_all(selector)

    def closest(self, node, selector: str):
        handle = node.evaluate_handle(
            "(el, sel) => el.closest(sel)",
            selector,
        )
        element = handle.as_element()
        return element

    def get_text(self, node) -> str:
        return node.inner_text()

    def get_attribute(self, node, name: str) -> Optional[str]:
        return node.get_attribute(name)

    def resolve_url(self, url: str, base: str) -> str:
        return urljoin(base, url)

    def get_page_url(self) -> str:
        return self._page.url

    def sleep_ms(self, delay_ms: int):
        self._page.wait_for_timeout(delay_ms)

    def close(self):
        try:
            self._context.close()
        except Exception:
            pass
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._playwright.stop()
        except Exception:
            pass
