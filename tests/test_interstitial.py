"""Unit tests for `core.interstitial.dismiss`.

These tests exercise the rule-matching and click-loop logic against fake
`page` objects rather than launching a real browser. That keeps CI fast and
deterministic, and avoids requiring Playwright + a network in the test runner.

A separate, opt-in integration test exercises a real Playwright Chromium
against local HTML fixtures via `file://` — it's gated on
`SIMPSCRAPE_RUN_PLAYWRIGHT_TESTS=1` because the bundled Chromium isn't always
present in CI sandboxes.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.interstitial import DEFAULT_RULES, Rule, dismiss


class FakeLocator:
    def __init__(
        self,
        *,
        present: bool = True,
        visible: bool = True,
        click_raises: bool = False,
    ) -> None:
        self._present = present
        self._visible = visible
        self._click_raises = click_raises
        self.click_calls = 0

    @property
    def first(self) -> "FakeLocator":
        return self

    def count(self) -> int:
        return 1 if self._present else 0

    def is_visible(self) -> bool:
        return self._visible

    def click(self, timeout: int = 1500) -> None:
        self.click_calls += 1
        if self._click_raises:
            raise RuntimeError("simulated click failure")


class FakePage:
    """Minimal stand-in for `playwright.sync_api.Page`.

    Drives the `dismiss` loop: each iteration consumes one entry from
    `body_snippets`. After the snippet is consumed, the next selector lookup
    that succeeds simulates the gate going away (because `dismiss` re-reads
    the snippet on the next loop).
    """

    def __init__(
        self,
        url: str,
        body_snippets: list[str],
        title: str = "",
        selector_responses: dict[str, FakeLocator] | None = None,
    ) -> None:
        self.url = url
        self._snippets = list(body_snippets)
        self._title = title
        self._selectors = selector_responses or {}
        self.locator_calls: list[str] = []
        self.load_state_calls = 0
        self.timeout_calls = 0

    def title(self) -> str:
        return self._title

    def locator(self, selector: str) -> FakeLocator:
        self.locator_calls.append(selector)
        if selector == "body":
            snippet = self._snippets.pop(0) if self._snippets else ""
            return _BodyLocator(snippet)
        return self._selectors.get(selector, FakeLocator(present=False))

    def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        self.load_state_calls += 1

    def wait_for_timeout(self, ms: int) -> None:
        self.timeout_calls += 1


class _BodyLocator:
    def __init__(self, text: str) -> None:
        self._text = text

    def inner_text(self, timeout: int = 750) -> str:
        return self._text


class InterstitialMatchingTests(unittest.TestCase):
    def test_simpcity_dmca_rule_matches_and_clicks(self) -> None:
        page = FakePage(
            url="https://simpcity.cr/threads/123",
            body_snippets=[
                "Welcome — please confirm you understand our DMCA policy. I agree to continue.",
                "thread content with no gate now",
            ],
            title="DMCA Notice",
            selector_responses={
                "button:has-text('I agree')": FakeLocator(present=True, visible=True),
            },
        )

        matched = dismiss(page, DEFAULT_RULES)

        self.assertEqual(matched, "simpcity_dmca")
        self.assertGreaterEqual(page.load_state_calls, 1)

    def test_returns_none_when_no_gate_present(self) -> None:
        page = FakePage(
            url="https://example.com/page",
            body_snippets=["Just a normal page with content and no agree buttons."],
        )

        matched = dismiss(page, DEFAULT_RULES)

        self.assertIsNone(matched)

    def test_xenforo_notice_rule_clicks_dismiss_link(self) -> None:
        # XF rule is host- and text-agnostic, so it activates whenever the
        # selector exists. Use a non-simpcity host to exercise the generic
        # path.
        page = FakePage(
            url="https://forum.example/threads/abc",
            body_snippets=[
                "Some forum content with a sticky banner notice.",
                "Notice gone, content visible.",
            ],
            selector_responses={
                "a.notice-dismiss": FakeLocator(present=True, visible=True),
            },
        )

        matched = dismiss(page, DEFAULT_RULES)

        self.assertEqual(matched, "xenforo_notice")

    def test_stacked_gates_loop_dismisses_both(self) -> None:
        # First snippet has age + DMCA wording (matches simpcity_dmca rule
        # when host is simpcity), second snippet still has DMCA wording but
        # the selector is now absent, third snippet is clean. The loop should
        # click once and then return.
        clicker = FakeLocator(present=True, visible=True)
        page = FakePage(
            url="https://simpcity.cr/threads/123",
            body_snippets=[
                "you must be 18 — I agree and continue",
                "thread content visible",
            ],
            selector_responses={
                "button:has-text('I agree')": clicker,
            },
        )

        matched = dismiss(page, DEFAULT_RULES)

        self.assertEqual(matched, "simpcity_dmca")
        self.assertEqual(clicker.click_calls, 1)

    def test_invisible_button_is_skipped(self) -> None:
        page = FakePage(
            url="https://simpcity.cr/threads/123",
            body_snippets=[
                "DMCA notice — I agree and continue",
                "DMCA notice — I agree and continue",
                "DMCA notice — I agree and continue",
            ],
            selector_responses={
                "button:has-text('I agree')": FakeLocator(present=True, visible=False),
            },
        )

        matched = dismiss(page, DEFAULT_RULES)

        # Selector exists but is invisible, so dismiss never fires for that
        # rule. No other selector matches our fake page either.
        self.assertIsNone(matched)

    def test_click_failure_falls_through_to_next_selector(self) -> None:
        first = FakeLocator(present=True, visible=True, click_raises=True)
        second = FakeLocator(present=True, visible=True)
        page = FakePage(
            url="https://simpcity.cr/threads/123",
            body_snippets=[
                "DMCA — I agree to continue",
                "thread content visible",
            ],
            selector_responses={
                "button:has-text('I agree')": first,
                "button:has-text('I acknowledge')": second,
            },
        )

        matched = dismiss(page, DEFAULT_RULES)

        self.assertEqual(matched, "simpcity_dmca")
        self.assertEqual(first.click_calls, 1)
        self.assertEqual(second.click_calls, 1)

    def test_custom_rule_with_explicit_host_pattern(self) -> None:
        custom = Rule(
            name="my_site_age_gate",
            host_patterns=("example.org",),
            text_patterns=("are you 18",),
            selectors=("button#yes",),
        )
        clicker = FakeLocator(present=True, visible=True)
        page = FakePage(
            url="https://example.org/intro",
            body_snippets=["are you 18 or older?", "main page"],
            selector_responses={"button#yes": clicker},
        )

        matched = dismiss(page, [custom])

        self.assertEqual(matched, "my_site_age_gate")
        self.assertEqual(clicker.click_calls, 1)

    def test_custom_rule_skipped_when_host_does_not_match(self) -> None:
        custom = Rule(
            name="my_site_age_gate",
            host_patterns=("example.org",),
            text_patterns=("are you 18",),
            selectors=("button#yes",),
        )
        clicker = FakeLocator(present=True, visible=True)
        page = FakePage(
            url="https://other.example/intro",
            body_snippets=["are you 18 or older?"],
            selector_responses={"button#yes": clicker},
        )

        matched = dismiss(page, [custom])

        self.assertIsNone(matched)
        self.assertEqual(clicker.click_calls, 0)


@unittest.skipUnless(
    os.environ.get("SIMPSCRAPE_RUN_PLAYWRIGHT_TESTS") == "1",
    "Playwright integration test is opt-in to keep default test runs hermetic.",
)
class InterstitialPlaywrightFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        from playwright.sync_api import sync_playwright  # noqa: F401  (import-time check)

    def _serve_html(self, html: str) -> str:
        tmp = Path(tempfile.mkstemp(suffix=".html")[1])
        tmp.write_text(html, encoding="utf-8")
        self.addCleanup(tmp.unlink, missing_ok=True)
        return tmp.as_uri()

    def test_dmca_button_click_reveals_content(self) -> None:
        from playwright.sync_api import sync_playwright

        url = self._serve_html(
            """
            <!doctype html>
            <html><head><title>DMCA Notice</title></head>
            <body>
              <div id="gate">
                <p>By continuing you confirm you understand our DMCA policy.</p>
                <button id="agree" onclick="document.getElementById('gate').remove();
                  document.getElementById('content').hidden=false;">I agree</button>
              </div>
              <article id="content" hidden>thread content visible</article>
            </body></html>
            """
        )
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                context = browser.new_context()
                page = context.new_page()
                page.goto(url)
                matched = dismiss(page, DEFAULT_RULES)
                self.assertIsNotNone(matched)
                self.assertTrue(page.locator("#content").is_visible())
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
