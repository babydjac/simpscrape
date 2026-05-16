import base64
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from core.browser_config import infer_playwright_chrome_channel
from core.interstitial import DEFAULT_RULES, dismiss as dismiss_interstitial


# Locale / timezone / viewport pinned to a common North-American Chrome to
# reduce how often anti-bot gates trigger on automated contexts. Only applied
# on the non-CDP code paths; CDP attaches inherit the live Chrome already.
_DEFAULT_LOCALE = "en-US"
_DEFAULT_TIMEZONE = "America/Los_Angeles"
_DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
_WEBDRIVER_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
)


class PlaywrightDriver:
    _CHALLENGE_TERMS = (
        "checking your browser before accessing",
        "please wait a few seconds. once this check is complete",
        "ddos-guard",
    )

    def __init__(
        self,
        headless: bool = True,
        storage_state: str = None,
        user_agent: str = None,
        cookies: list = None,
        java_script_enabled: bool = True,
        chrome_cdp_url: str = None,
        chrome_user_data_dir: str = None,
        chrome_profile_directory: str = None,
    ):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._close_context = True
        self._close_browser = True
        self._interstitial_dismissed: bool = False
        self._supports_storage_export: bool = False
        try:
            self._playwright = sync_playwright().start()
            browser_type = self._playwright.chromium
            cdp_attach_error = None
            if chrome_cdp_url:
                try:
                    self._browser = browser_type.connect_over_cdp(chrome_cdp_url)
                except Exception as exc:
                    cdp_attach_error = exc
                else:
                    contexts = list(self._browser.contexts)
                    if contexts:
                        self._context = contexts[0]
                    else:
                        self._context = self._browser.new_context()
                    self._page = self._context.new_page()
                    self._close_context = False
                    self._close_browser = False
                    return

            if chrome_user_data_dir:
                user_data_dir = Path(chrome_user_data_dir).expanduser()
                if not user_data_dir.exists():
                    raise RuntimeError(f"Chrome user data dir not found: {user_data_dir}")
                launch_args = []
                if chrome_profile_directory:
                    launch_args.append(f"--profile-directory={chrome_profile_directory}")
                context_kwargs = {
                    "headless": headless,
                    "java_script_enabled": java_script_enabled,
                }
                channel = infer_playwright_chrome_channel(user_data_dir)
                if channel:
                    context_kwargs["channel"] = channel
                if user_agent:
                    context_kwargs["user_agent"] = user_agent
                if launch_args:
                    context_kwargs["args"] = launch_args
                try:
                    self._context = browser_type.launch_persistent_context(str(user_data_dir), **context_kwargs)
                except Exception as exc:
                    if cdp_attach_error is not None:
                        raise RuntimeError(
                            "Unable to attach to Chrome via CDP and could not fall back to the Chrome profile. "
                            f"CDP URL: {chrome_cdp_url}. Profile: {user_data_dir} ({chrome_profile_directory or 'Default'}). "
                            "If Chrome is already open, close it and retry, or restart Chrome with "
                            "--remote-debugging-port=9222."
                        ) from exc
                    raise RuntimeError(
                        "Unable to launch Chrome with the requested profile. "
                        "Close Chrome and retry, or attach to a running Chrome instance with "
                        "--chrome-cdp-url http://127.0.0.1:9222."
                    ) from exc
                if cookies:
                    self._context.add_cookies(cookies)
                self._page = self._context.new_page()
                self._browser = getattr(self._context, "browser", None)
                self._close_browser = False
                # Persistent contexts can serialize storage_state; CDP attach can't.
                self._supports_storage_export = True
                self._apply_stealth(self._context)
                return

            if cdp_attach_error is not None:
                raise RuntimeError(
                    "Unable to attach to Chrome via CDP. "
                    f"Checked {chrome_cdp_url}. Start Chrome with --remote-debugging-port=9222 "
                    "or pass --chrome-user-data-dir instead."
                ) from cdp_attach_error

            self._browser = browser_type.launch(headless=headless)
            context_kwargs = {
                "java_script_enabled": java_script_enabled,
                "locale": _DEFAULT_LOCALE,
                "timezone_id": _DEFAULT_TIMEZONE,
                "viewport": dict(_DEFAULT_VIEWPORT),
            }
            if storage_state:
                context_kwargs["storage_state"] = storage_state
            if user_agent:
                context_kwargs["user_agent"] = user_agent
            self._context = self._browser.new_context(**context_kwargs)
            if cookies:
                self._context.add_cookies(cookies)
            self._page = self._context.new_page()
            self._supports_storage_export = True
            self._apply_stealth(self._context)
        except Exception:
            self._stop_playwright()
            raise

    def _apply_stealth(self, context) -> None:
        """Best-effort patch of obvious automation tells. Never raises."""
        try:
            context.add_init_script(_WEBDRIVER_INIT_SCRIPT)
        except Exception:
            pass

    def goto(self, url: str, timeout_ms: int = 60000):
        self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            matched = dismiss_interstitial(self._page, DEFAULT_RULES)
        except Exception:
            matched = None
        if matched:
            self._interstitial_dismissed = True

    def did_dismiss_interstitial(self) -> bool:
        return self._interstitial_dismissed

    def reset_dismissal_flag(self) -> None:
        self._interstitial_dismissed = False

    def export_storage_state(self) -> Optional[dict[str, Any]]:
        """Return the current context's storage_state, or None if unavailable.

        CDP-attached contexts share a live Chrome session and don't support
        a synchronous storage_state() export here, so we skip them.
        """
        if not self._supports_storage_export or self._context is None:
            return None
        try:
            return self._context.storage_state()
        except Exception:
            return None

    # One OR-selector so we never burn N× full timeouts trying siblings.
    _FORUM_POST_MARKERS = (
        "div.message-inner, article.message--post, article.message, "
        "article[data-content], .block-body--main, div.thread-post"
    )

    def wait_for_idle(self, timeout_ms: int = 30000, *, full_content_wait: bool = True):
        """Wait for thread content. Full wait on first page; light wait on pagination."""
        budget = max(500, int(timeout_ms))

        if not full_content_wait:
            # Follow-up pages: same template, posts swap in quickly — keep this tight.
            t_net = min(800, max(250, budget // 6))
            try:
                self._page.wait_for_load_state("networkidle", timeout=t_net)
            except PlaywrightTimeoutError:
                pass
            t_dom = min(500, max(200, budget // 8))
            try:
                self._page.wait_for_load_state("domcontentloaded", timeout=t_dom)
            except PlaywrightTimeoutError:
                pass
            t_posts = min(1400, max(350, budget // 2))
            if not self._wait_for_forum_posts(t_posts):
                self._page.wait_for_timeout(120)
            return

        # First page: need real content before scrape (fixes empty XenForo extractions).
        phase_net = min(3200, max(700, budget // 2))
        try:
            self._page.wait_for_load_state("networkidle", timeout=phase_net)
        except PlaywrightTimeoutError:
            pass
        phase_dom = min(1800, max(350, budget // 5))
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=phase_dom)
        except PlaywrightTimeoutError:
            pass
        forum_timeout = max(900, min(6500, budget))
        if self._wait_for_forum_posts(forum_timeout):
            return
        if self._looks_like_access_challenge():
            self._wait_for_challenge_resolution(max(4000, min(15000, budget * 2)))
            if self._wait_for_forum_posts(max(900, min(5000, budget))):
                return
        self._page.wait_for_timeout(min(350, max(120, budget // 30)))

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

    def capture_preview(self) -> str:
        image_bytes = self._page.screenshot(
            type="jpeg",
            quality=45,
            full_page=False,
            animations="disabled",
            scale="css",
        )
        return base64.b64encode(image_bytes).decode("ascii")

    def _wait_for_forum_posts(self, timeout_ms: int) -> bool:
        try:
            self._page.wait_for_selector(
                self._FORUM_POST_MARKERS,
                timeout=timeout_ms,
                state="attached",
            )
            return True
        except PlaywrightTimeoutError:
            return False

    def _looks_like_access_challenge(self) -> bool:
        body_text = ""
        try:
            body_text = self._page.locator("body").inner_text(timeout=750)
        except Exception:
            body_text = ""
        title = ""
        try:
            title = self._page.title()
        except Exception:
            title = ""
        combined = f"{title}\n{body_text}".lower()
        return any(term in combined for term in self._CHALLENGE_TERMS)

    def _wait_for_challenge_resolution(self, timeout_ms: int) -> None:
        try:
            self._page.wait_for_function(
                """({ postSelector, challengeTerms }) => {
                    if (document.querySelector(postSelector)) {
                        return true;
                    }
                    const text = `${document.title}\n${document.body?.innerText || ""}`.toLowerCase();
                    return !challengeTerms.some((term) => text.includes(term));
                }""",
                arg={"postSelector": self._FORUM_POST_MARKERS, "challengeTerms": list(self._CHALLENGE_TERMS)},
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            return

    def close(self):
        try:
            self._page.close()
        except Exception:
            pass
        try:
            if self._close_context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._close_browser and self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            self._stop_playwright()
        except Exception:
            pass

    def _stop_playwright(self):
        if self._playwright is None:
            return
        try:
            self._playwright.stop()
        finally:
            self._playwright = None
