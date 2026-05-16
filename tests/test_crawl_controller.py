import unittest

from core.crawl_controller import (
    AccessChallengeError,
    AuthenticationRequiredError,
    CrawlConfig,
    CrawlController,
)
from core.sitemap_loader import SelectorNode, Sitemap


class _FakeDom:
    def __init__(self, selector_map=None, body_text="", preview_image="preview-bytes") -> None:
        self.selector_map = selector_map or {}
        self.body_text = body_text
        self.preview_image = preview_image

    def query_selector_all(self, selector: str, context=None):
        if selector == "body":
            return [self.body_text] if self.body_text else []
        return list(self.selector_map.get(selector, []))

    def get_text(self, node) -> str:
        return str(node)

    def capture_preview(self) -> str:
        return self.preview_image


class CrawlControllerTests(unittest.TestCase):
    def _controller(self, dom: _FakeDom) -> CrawlController:
        sitemap = Sitemap(
            start_urls=["https://forums.socialmediagirls.com/threads/whorella.379890/"],
            selectors=[
                SelectorNode(
                    id="title",
                    parent_selectors=["_root"],
                    type="SelectorText",
                    selector="h1.p-title-value",
                    multiple=False,
                    regex="",
                    extract_attribute=None,
                )
            ],
        )
        return CrawlController(dom, sitemap, CrawlConfig())

    def test_detects_login_wall_and_suggests_storage_state(self) -> None:
        controller = self._controller(_FakeDom({"input[name='password']": [object()]}))

        error = controller._detect_authentication_required(
            "https://forums.socialmediagirls.com/threads/whorella.379890/",
            {"title": "Log in"},
            [],
        )

        self.assertIsInstance(error, AuthenticationRequiredError)
        message = str(error)
        self.assertIn("https://forums.socialmediagirls.com/login", message)
        self.assertIn("socialmediagirls-com-state.json", message)
        self.assertIn("--storage-state", message)

    def test_does_not_flag_normal_zero_record_page_without_login_form(self) -> None:
        controller = self._controller(_FakeDom())

        error = controller._detect_authentication_required(
            "https://forums.socialmediagirls.com/threads/whorella.379890/",
            {"title": "Whorella"},
            [],
        )

        self.assertIsNone(error)

    def test_detects_ddos_guard_access_challenge(self) -> None:
        controller = self._controller(
            _FakeDom(body_text="Checking your browser before accessing simpcity.cr. Please wait a few seconds.")
        )

        error = controller._detect_access_challenge(
            "https://simpcity.cr/threads/little_paradise-edwiina-edwina-ts-trans_doll.26042/",
            [],
        )

        self.assertIsInstance(error, AccessChallengeError)
        message = str(error)
        self.assertIn("DDoS-Guard", message)
        self.assertIn("https://simpcity.cr/", message)
        self.assertIn("simpcity-cr-state.json", message)
        self.assertIn("--headless false", message)

    def test_does_not_flag_access_challenge_when_records_exist(self) -> None:
        controller = self._controller(
            _FakeDom(body_text="Checking your browser before accessing simpcity.cr.")
        )

        error = controller._detect_access_challenge(
            "https://simpcity.cr/threads/little_paradise-edwiina-edwina-ts-trans_doll.26042/",
            [{"post_id": "post-1"}],
        )

        self.assertIsNone(error)

    def test_emits_browser_preview_when_enabled(self) -> None:
        events = []
        controller = CrawlController(
            _FakeDom(preview_image="abc123"),
            Sitemap(
                start_urls=["https://simpcity.cr/threads/example.1/"],
                selectors=[
                    SelectorNode(
                        id="title",
                        parent_selectors=["_root"],
                        type="SelectorText",
                        selector="h1.p-title-value",
                        multiple=False,
                        regex="",
                        extract_attribute=None,
                    )
                ],
            ),
            CrawlConfig(emit_browser_preview=True, page_callback=events.append),
        )

        controller._emit_browser_preview(
            start_url="https://simpcity.cr/threads/example.1/",
            page=1,
            url="https://simpcity.cr/threads/example.1/",
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "page_preview")
        self.assertEqual(events[0]["image_base64"], "abc123")
        self.assertEqual(events[0]["image_mime"], "image/jpeg")


if __name__ == "__main__":
    unittest.main()
