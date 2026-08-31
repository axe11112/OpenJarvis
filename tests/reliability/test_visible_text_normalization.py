"""What "the visible text is unchanged" actually means to a real browser.

Found on FEAT-00030: a heading split across sibling <span>s by an
absolutely-positioned sr-only spacer — a legitimate, correct pattern, and
exactly the fix its own second build attempt made for a real missing-space
accessibility bug — came back from page.inner_text() with a line break where
the DOM had a plain space. The criterion, written as one line, never matched,
even though the actual rendered page was correct on screen, on both
viewports, throughout. inner_text() was already right about *which* text
counts as visible (confirmed here too: display:none and visibility:hidden
content is excluded, aria-hidden-but-rendered content is included); the only
gap was whitespace formatting, not content.
"""

from __future__ import annotations

import pytest

from openjarvis.reliability.probes.browser import BrowserProbeRunner, _normalize_rendered_text
from openjarvis.reliability.probes.spec import ProbeExpectation

SR_ONLY_CSS = """
<style>
.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border-width: 0;
}
</style>
"""


def _page(browser, html):
    page = browser.new_page()
    page.set_content(f"<html><head>{SR_ONLY_CSS}</head><body>{html}</body></html>")
    return page


@pytest.fixture
def browser(require_browser, chromium_path):
    from playwright.sync_api import sync_playwright

    kwargs = {"headless": True}
    if chromium_path:
        kwargs["executable_path"] = chromium_path
    with sync_playwright() as p:
        b = p.chromium.launch(**kwargs)
        yield b
        b.close()


class TestSrOnlySpacerDoesNotBreakTheMatch:
    """Case 1: visible text unchanged, but split by an sr-only spacer."""

    HTML = (
        '<h1 class="flex flex-col">'
        '<span>Understand more.</span>'
        '<span class="sr-only"> </span>'
        '<span>Train smarter.</span>'
        "</h1>"
    )

    def test_the_raw_inner_text_actually_has_a_line_break(self, browser):
        # Pin the root cause: this is real, not hypothesized.
        page = _page(browser, self.HTML)
        assert "\n" in page.inner_text("body")

    def test_the_criterion_still_passes(self, browser):
        page = _page(browser, self.HTML)
        expectation = ProbeExpectation(kind="text", value="Understand more. Train smarter.")
        reason = BrowserProbeRunner._check(page, expectation, "http://x/", "t")
        assert reason == "", reason


class TestAriaHiddenHelperDoesNotBreakTheMatch:
    """Case 2: visible text unchanged, with an aria-hidden helper nearby."""

    HTML = (
        '<h1><span aria-hidden="true">→ </span><span>Understand more. Train smarter.</span></h1>'
    )

    def test_the_criterion_still_passes(self, browser):
        page = _page(browser, self.HTML)
        expectation = ProbeExpectation(kind="text", value="Understand more. Train smarter.")
        reason = BrowserProbeRunner._check(page, expectation, "http://x/", "t")
        assert reason == "", reason


class TestAccessibilityContentIsStillInspectable:
    """Case 6: a criterion that legitimately wants accessibility-only
    (aria-hidden, still-rendered) text must still be able to find it —
    normalization must not have quietly started excluding it.
    """

    HTML = '<h1><span aria-hidden="true">Decorative helper text</span></h1>'

    def test_aria_hidden_but_rendered_text_is_still_found(self, browser):
        page = _page(browser, self.HTML)
        expectation = ProbeExpectation(kind="text", value="Decorative helper text")
        reason = BrowserProbeRunner._check(page, expectation, "http://x/", "t")
        assert reason == "", reason


class TestGenuinelyDifferentTextStillFails:
    """Cases 3-5: normalization must not become leniency about content."""

    def test_changed_visible_text_fails(self, browser):
        page = _page(browser, "<h1>Understand less. Train slower.</h1>")
        expectation = ProbeExpectation(kind="text", value="Understand more. Train smarter.")
        reason = BrowserProbeRunner._check(page, expectation, "http://x/", "t")
        assert reason != ""
        assert "expected" in reason

    def test_missing_visible_text_fails(self, browser):
        page = _page(browser, "<h1>Something else entirely.</h1>")
        expectation = ProbeExpectation(kind="text", value="Understand more. Train smarter.")
        reason = BrowserProbeRunner._check(page, expectation, "http://x/", "t")
        assert reason != ""

    def test_text_hidden_with_display_none_still_fails(self, browser):
        # The words exist in the DOM but are not visible — a "visible text"
        # check must still fail, the same as if they were never there.
        page = _page(
            browser,
            '<h1 style="display:none">Understand more. Train smarter.</h1>',
        )
        expectation = ProbeExpectation(kind="text", value="Understand more. Train smarter.")
        reason = BrowserProbeRunner._check(page, expectation, "http://x/", "t")
        assert reason != ""

    def test_text_hidden_with_visibility_hidden_still_fails(self, browser):
        page = _page(
            browser,
            '<h1 style="visibility:hidden">Understand more. Train smarter.</h1>',
        )
        expectation = ProbeExpectation(kind="text", value="Understand more. Train smarter.")
        reason = BrowserProbeRunner._check(page, expectation, "http://x/", "t")
        assert reason != ""

    def test_reordered_text_fails_when_order_matters(self, browser):
        page = _page(browser, "<h1>Train smarter. Understand more.</h1>")
        expectation = ProbeExpectation(kind="text", value="Understand more. Train smarter.")
        reason = BrowserProbeRunner._check(page, expectation, "http://x/", "t")
        assert reason != ""


class TestDesktopAndMobileAgree:
    """Case 7: the same DOM, checked at two viewport sizes, must match the
    same way — the flex-column layout wraps identically at both widths in
    this fixture, and the whitespace normalization is viewport-independent
    regardless (it runs on inner_text()'s output, not on layout geometry).
    """

    HTML = TestSrOnlySpacerDoesNotBreakTheMatch.HTML

    @pytest.mark.parametrize("viewport", [{"width": 1280, "height": 800}, {"width": 390, "height": 844}])
    def test_the_criterion_passes_at_both_sizes(self, browser, viewport):
        page = browser.new_page(viewport=viewport)
        page.set_content(f"<html><head>{SR_ONLY_CSS}</head><body>{self.HTML}</body></html>")
        expectation = ProbeExpectation(kind="text", value="Understand more. Train smarter.")
        reason = BrowserProbeRunner._check(page, expectation, "http://x/", "t")
        assert reason == "", reason


class TestNormalizeRenderedTextDirectly:
    """The pure function, without a browser — the substance of the fix."""

    def test_collapses_newlines_to_a_single_space(self):
        assert _normalize_rendered_text("a \nb") == "a b"

    def test_collapses_repeated_whitespace(self):
        assert _normalize_rendered_text("a    b\n\n c") == "a b c"

    def test_strips_leading_and_trailing_whitespace(self):
        assert _normalize_rendered_text("  a b  \n") == "a b"

    def test_leaves_single_spaced_text_alone(self):
        assert _normalize_rendered_text("a b c") == "a b c"
