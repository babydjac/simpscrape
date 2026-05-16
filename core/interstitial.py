"""Generic click-through interstitial dismisser.

A small data-driven dismisser for the kind of consent / DMCA / age / "click to
continue" gates that block forum scrapers from reaching real content. The
dismisser runs after every navigation and clicks the first matching rule's
button, then loops a few times to handle stacked gates (e.g. DDoS-Guard ->
DMCA -> age).

Designed to be defensive: never raises, never blocks the calling driver, and
caps total wall-time so a misbehaving page can't stall a crawl.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence
from urllib.parse import urlparse


@dataclass(frozen=True)
class Rule:
    """A single interstitial-dismissal rule.

    `host_patterns` and `text_patterns` are AND-ed: at least one host pattern
    must match the page host (substring, lowercased) AND at least one text
    pattern must appear in the page body text/title (substring, lowercased)
    for the rule to be considered. An empty pattern list means "always match
    this dimension" (e.g. a host-agnostic generic rule).

    `selectors` is tried in order; the first one that resolves to a visible,
    enabled element gets clicked.
    """
    name: str
    selectors: Sequence[str]
    host_patterns: Sequence[str] = field(default_factory=tuple)
    text_patterns: Sequence[str] = field(default_factory=tuple)
    post_click_wait_ms: int = 2500
    click_timeout_ms: int = 1500


# Cap how much body text we sample for matching. Forum threads can be huge
# and inner_text() on a fully-rendered page is the slowest call here.
_BODY_SNIPPET_BYTES = 4096
_MAX_LOOPS = 3


# Order matters: more specific rules first, generic fallback last. The DDoS-
# Guard challenge is text-only ("checking your browser..."), so it has no
# selector — it just teaches the loop to wait through it without clicking,
# letting the existing wait_for_idle path resolve it.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        name="simpcity_dmca",
        host_patterns=("simpcity.",),
        text_patterns=(
            "dmca",
            "i agree",
            "i acknowledge",
            "continue to site",
            "you must be 18",
            "adult content",
        ),
        selectors=(
            "button:has-text('I agree')",
            "button:has-text('I acknowledge')",
            "button:has-text('Continue')",
            "a:has-text('I agree')",
            "a:has-text('I acknowledge')",
            "a:has-text('Continue')",
            "input[type='submit'][value*='Agree' i]",
            "input[type='submit'][value*='Continue' i]",
            "button[name='_xfClick'][value='dismiss']",
        ),
    ),
    Rule(
        name="xenforo_notice",
        host_patterns=(),
        text_patterns=(),
        selectors=(
            "a.notice-dismiss",
            "button.notice-dismiss",
            "button[name='_xfClick'][value='dismiss']",
            ".notice .button--notice",
        ),
        post_click_wait_ms=600,
    ),
    Rule(
        name="generic_consent",
        host_patterns=(),
        text_patterns=(
            "i agree",
            "i acknowledge",
            "i accept",
            "i am over 18",
            "enter site",
            "continue to site",
            "click to continue",
            "agree and continue",
        ),
        selectors=(
            "button:has-text('I agree')",
            "button:has-text('I accept')",
            "button:has-text('I acknowledge')",
            "button:has-text('Continue')",
            "button:has-text('Enter')",
            "a:has-text('I agree')",
            "a:has-text('I accept')",
            "a:has-text('Continue')",
            "a:has-text('Enter')",
            "input[type='submit'][value*='Agree' i]",
            "input[type='submit'][value*='Accept' i]",
            "input[type='submit'][value*='Continue' i]",
            "input[type='submit'][value*='Enter' i]",
        ),
    ),
)


def _host_of(page) -> str:
    try:
        return (urlparse(page.url).hostname or "").lower()
    except Exception:
        return ""


def _body_snippet(page) -> str:
    """Cheap-ish body-text sample. Keeps timeout small so we never block."""
    try:
        body = page.locator("body").inner_text(timeout=750)
    except Exception:
        body = ""
    try:
        title = page.title()
    except Exception:
        title = ""
    combined = f"{title}\n{body}"
    if len(combined) > _BODY_SNIPPET_BYTES:
        combined = combined[:_BODY_SNIPPET_BYTES]
    return combined.lower()


def _rule_matches(rule: Rule, host: str, snippet: str) -> bool:
    if rule.host_patterns:
        if not any(pattern in host for pattern in rule.host_patterns):
            return False
    if rule.text_patterns:
        if not any(pattern in snippet for pattern in rule.text_patterns):
            return False
    return True


def _try_click(page, rule: Rule) -> bool:
    """Try each selector in the rule; return True on the first successful click."""
    for selector in rule.selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            if not locator.is_visible():
                continue
            locator.click(timeout=rule.click_timeout_ms)
        except Exception:
            continue
        return True
    return False


def dismiss(page, rules: Sequence[Rule] = DEFAULT_RULES) -> Optional[str]:
    """Dismiss any matching interstitial. Returns the matched rule name, or None.

    Loops up to `_MAX_LOOPS` times so stacked gates (e.g. DDoS-Guard then DMCA
    then age) all get cleared in one navigation. Never raises — failure modes
    (page closed, navigation in flight, locator desync) all degrade to a
    return value of None.
    """
    last_match: Optional[str] = None
    for _ in range(_MAX_LOOPS):
        host = _host_of(page)
        snippet = _body_snippet(page)
        if not snippet:
            return last_match
        clicked = False
        for rule in rules:
            if not _rule_matches(rule, host, snippet):
                continue
            if not rule.selectors:
                # Text-only rule (e.g. challenge waiter) — nothing to click.
                continue
            if _try_click(page, rule):
                last_match = rule.name
                clicked = True
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=rule.post_click_wait_ms)
                except Exception:
                    pass
                try:
                    page.wait_for_timeout(150)
                except Exception:
                    pass
                break
        if not clicked:
            return last_match
    return last_match
