from dataclasses import dataclass
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from .data_collector import DataCollector
from .pagination_handler import PaginationHandler
from .selector_engine import SelectorEngine
from .sitemap_loader import SelectorNode, Sitemap


class AuthenticationRequiredError(RuntimeError):
    pass


class AccessChallengeError(RuntimeError):
    pass


@dataclass
class CrawlConfig:
    delay_ms: int = 0
    max_pages: Optional[int] = None
    nav_timeout_ms: int = 60000
    idle_timeout_ms: int = 5000
    page_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    emit_browser_preview: bool = False
    # When set and an interstitial was auto-dismissed, the post-dismissal
    # storage_state (cookies + localStorage) is written here so the next run
    # can skip the gate entirely.
    storage_state_save_path: Optional[Path] = None


class CrawlController:
    LOGIN_FORM_SELECTORS = (
        "form[action*='/login/login']",
        "form[action*='/login'] input[name='login']",
        "input[name='login']",
        "input[name='password']",
        "button[type='submit'][name='_xfLogin']",
    )
    ACCESS_CHALLENGE_MARKERS = (
        "checking your browser before accessing",
        "please wait a few seconds. once this check is complete",
        "ddos-guard",
    )

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
        # IMPORTANT: rich.Progress defaults to redirect_stdout=True / redirect_stderr=True,
        # which silently re-routes every write through Rich's Console renderer. When the
        # bridge is emitting JSON-per-line on stdout (GUI, CI, piped runs), that wraps
        # long JSON to ~80 cols and splices progress-bar bytes into the stream — the
        # GUI then sees malformed events and appears to hang on "Running...".
        # We disable the live progress entirely when not a TTY and never let it touch
        # the parent's stdio, regardless.
        is_tty = sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else False
        progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            redirect_stdout=False,
            redirect_stderr=False,
            disable=not is_tty,
        )
        with progress:
            for start_url in self.sitemap.start_urls:
                task_id = progress.add_task("Starting", total=None)
                self._crawl_from(start_url, visited, progress, task_id)
        return self.collector.all_records()

    def _crawl_from(self, start_url: str, visited: Set[str], progress: Progress, task_id: int) -> None:
        current_url = start_url
        page_count = 0
        total_records = len(self.collector.records)
        pagination_selector = self._get_pagination_selector()
        total_pages: Optional[int] = None
        if self.order_seed is None:
            self.order_seed = int(time.time())

        while current_url:
            if current_url in visited:
                break
            visited.add(current_url)
            self._emit_page_event(
                event="page_start",
                start_url=start_url,
                page=page_count + 1,
                url=current_url,
                total_pages=total_pages,
            )
            try:
                self.dom.driver.goto(current_url, timeout_ms=self.config.nav_timeout_ms)
            except Exception as exc:
                self._emit_page_event(
                    event="page_error",
                    start_url=start_url,
                    page=page_count + 1,
                    url=current_url,
                    error=str(exc),
                    total_pages=total_pages,
                )
                progress.update(task_id, description=f"Navigation failed: {exc}")
                break
            self._handle_interstitial_dismissal(start_url, page_count + 1, current_url)
            self.dom.wait_for_idle(
                timeout_ms=self.config.idle_timeout_ms,
                full_content_wait=(page_count == 0),
            )
            if self.config.delay_ms:
                self.dom.driver.sleep_ms(self.config.delay_ms)
            self._emit_browser_preview(
                start_url=start_url,
                page=page_count + 1,
                url=current_url,
                total_pages=total_pages,
            )

            if page_count == 0 and pagination_selector:
                total_pages = self.pagination.get_total_pages()
                if total_pages:
                    progress.update(task_id, total=total_pages)

            page_parent_ids = ["_root"]
            if pagination_selector:
                page_parent_ids.append(pagination_selector.id)
            records = self.selector_engine.extract_records(self.sitemap.selectors, page_parent_ids)
            page_fields = self.selector_engine.extract_page_fields(self.sitemap.selectors)
            auth_error = self._detect_authentication_required(current_url, page_fields, records)
            if auth_error:
                raise auth_error
            challenge_error = self._detect_access_challenge(current_url, records)
            if challenge_error:
                raise challenge_error
            for record in records:
                record.update(page_fields)
                self.order_index += 1
                record["web_scraper_order"] = f"{self.order_seed}-{self.order_index}"
                record["web_scraper_start_url"] = start_url
            self.collector.add_records(records)
            total_records += len(records)
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
            self._emit_page_event(
                event="page_complete",
                start_url=start_url,
                page=page_count,
                url=current_url,
                records_on_page=len(records),
                total_records=total_records,
                total_pages=total_pages,
            )

            if self.config.max_pages is not None and page_count >= self.config.max_pages:
                break
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

    def _emit_page_event(self, **payload: Any) -> None:
        callback = self.config.page_callback
        if callback is None:
            return
        try:
            callback(payload)
        except Exception:
            # Progress callbacks are best-effort and must not break crawling.
            return

    def _handle_interstitial_dismissal(self, start_url: str, page: int, current_url: str) -> None:
        """If the driver auto-dismissed a gate, emit an event and persist state once."""
        driver = getattr(self.dom, "driver", None)
        if driver is None:
            return
        try:
            dismissed = bool(driver.did_dismiss_interstitial())
        except Exception:
            return
        if not dismissed:
            return
        # Drain the flag so we only emit once per crawl, even if subsequent
        # pagination pages also re-encounter (and re-dismiss) the gate.
        try:
            driver.reset_dismissal_flag()
        except Exception:
            pass

        save_path = self.config.storage_state_save_path
        saved_to: Optional[str] = None
        if save_path is not None:
            try:
                exporter = getattr(driver, "export_storage_state", None)
                state = exporter() if callable(exporter) else None
                if state is not None:
                    target = Path(save_path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    saved_to = str(target)
            except Exception:
                saved_to = None

        self._emit_page_event(
            event="interstitial_dismissed",
            start_url=start_url,
            page=page,
            url=current_url,
            storage_state_saved=saved_to,
        )

    def _emit_browser_preview(self, **payload: Any) -> None:
        if not self.config.emit_browser_preview:
            return
        try:
            image_base64 = self.dom.capture_preview()
        except Exception:
            image_base64 = None
        if not image_base64:
            return
        preview_payload = dict(payload)
        preview_payload.update(
            event="page_preview",
            image_base64=image_base64,
            image_mime="image/jpeg",
        )
        self._emit_page_event(**preview_payload)

    def _detect_authentication_required(
        self,
        current_url: str,
        page_fields: Dict[str, Any],
        records: List[Dict[str, Any]],
    ) -> Optional[AuthenticationRequiredError]:
        if records:
            return None
        title = str(page_fields.get("title") or "").strip().lower()
        if title not in {"log in", "login", "sign in"}:
            return None
        if not self._looks_like_login_form():
            return None
        login_url = self._login_url_for(current_url)
        suggested_state = self._suggested_storage_state_name(current_url)
        host = urlparse(current_url).netloc or current_url
        return AuthenticationRequiredError(
            f"Authentication required for {host}. Open {login_url}, sign in, save a session with "
            f"`./simp login --url {login_url} --output-state {suggested_state}`, then rerun with "
            f"`--storage-state {suggested_state}` or set Storage State Path in the GUI."
        )

    def _looks_like_login_form(self) -> bool:
        for selector in self.LOGIN_FORM_SELECTORS:
            try:
                if self.dom.query_selector_all(selector):
                    return True
            except Exception:
                continue
        body_nodes = self.dom.query_selector_all("body")
        if not body_nodes:
            return False
        try:
            body_text = (self.dom.get_text(body_nodes[0]) or "").lower()
        except Exception:
            return False
        return (
            "you must be logged-in to do that" in body_text
            or ("password" in body_text and "log in" in body_text)
        )

    def _detect_access_challenge(
        self,
        current_url: str,
        records: List[Dict[str, Any]],
    ) -> Optional[AccessChallengeError]:
        if records:
            return None
        body_text = self._page_body_text()
        if not body_text:
            return None
        lowered = body_text.lower()
        if not any(marker in lowered for marker in self.ACCESS_CHALLENGE_MARKERS):
            return None
        host = urlparse(current_url).netloc or current_url
        origin = self._origin_url(current_url)
        suggested_state = self._suggested_storage_state_name(current_url)
        return AccessChallengeError(
            f"Access challenge detected for {host}. The page is showing a DDoS-Guard browser check / consent "
            f"screen instead of thread content, and the auto-dismiss handler did not match a button on this "
            f"gate. Try rerunning once first; the gate handler often clears stacked gates on the second pass. "
            f"If it persists, open {origin} in a visible browser, wait for the challenge to clear, then refresh "
            f"your session with `./simp login --url {origin} --output-state {suggested_state}`. "
            f"If you are scraping in headless mode, also retry with `--headless false` or disable Headless Browser in the GUI."
        )

    def _page_body_text(self) -> str:
        body_nodes = self.dom.query_selector_all("body")
        if not body_nodes:
            return ""
        try:
            return self.dom.get_text(body_nodes[0]) or ""
        except Exception:
            return ""

    def _login_url_for(self, current_url: str) -> str:
        return urljoin(current_url, "/login")

    def _origin_url(self, current_url: str) -> str:
        parsed = urlparse(current_url)
        if not parsed.scheme or not parsed.netloc:
            return current_url
        return f"{parsed.scheme}://{parsed.netloc}/"

    def _suggested_storage_state_name(self, current_url: str) -> str:
        host = (urlparse(current_url).hostname or "site").lower()
        if host.startswith("forums."):
            host = host[len("forums.") :]
        slug = re.sub(r"[^a-z0-9]+", "-", host).strip("-")
        return f"{slug or 'site'}-state.json"
