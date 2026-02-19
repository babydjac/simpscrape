from typing import List, Optional


class DomExecutor:
    def __init__(self, driver):
        self.driver = driver

    def query_selector_all(self, selector: str, context=None):
        return self.driver.query_selector_all(selector, context=context)

    def get_text(self, node) -> str:
        return self.driver.get_text(node)

    def get_attribute(self, node, name: str) -> Optional[str]:
        return self.driver.get_attribute(node, name)

    def closest(self, node, selector: str):
        return self.driver.closest(node, selector)

    def resolve_url(self, url: str, base: str) -> str:
        return self.driver.resolve_url(url, base)

    def get_page_url(self) -> str:
        return self.driver.get_page_url()

    def wait_for_idle(self, timeout_ms: int = 30000):
        self.driver.wait_for_idle(timeout_ms=timeout_ms)

    def close(self):
        self.driver.close()
