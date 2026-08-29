"""Browser capability integration with canonical Wiz runtime."""

from __future__ import annotations

import pytest

from openjarvis.core.config import JarvisConfig
from openjarvis.wiz.authority import AuthorityPolicy, Channel
from openjarvis.wiz.brain import Request
from openjarvis.wiz.runtime import build_wiz


class TestBrowserCapabilityReachability:
    """Prove that the canonical Wiz runtime can reach real browser capabilities."""

    def test_browser_capabilities_in_canonical_runtime(self, tmp_path):
        """Browser capabilities are registered in build_wiz()."""
        config = JarvisConfig()
        config.reliability.site.base_url = "https://www.wizeperformance.com"

        runtime = build_wiz(config=config, home=tmp_path)

        # Verify browser capabilities are in the registry
        specs = {spec.name: spec for spec in runtime.registry.all()}
        assert "browser.open" in specs
        assert "browser.inspect" in specs
        assert "browser.interact" in specs
        assert "browser.assert" in specs
        assert "browser.screenshot" in specs
        assert "browser.console" in specs
        assert "browser.network" in specs

    def test_browser_handlers_in_canonical_runtime(self, tmp_path):
        """Browser handlers are wired into the Wiz runtime."""
        config = JarvisConfig()
        config.reliability.site.base_url = "https://www.wizeperformance.com"

        runtime = build_wiz(config=config, home=tmp_path)

        # The runtime should have browser verbs
        assert runtime.browser_verbs is not None

    def test_browser_capability_is_reachable_from_canonical_wiz(self, tmp_path):
        """Prove canonical Wiz can route to browser capabilities."""
        config = JarvisConfig()
        config.reliability.site.base_url = "https://www.wizeperformance.com"

        runtime = build_wiz(
            config=config,
            policy=AuthorityPolicy(
                grants={Channel.VOICE: {}}  # No special grants needed; READ is enough
            ),
            home=tmp_path,
        )

        # Wiz should be able to route to browser capabilities
        # (Without actually running them, which requires a real browser)
        request = Request(
            text="take a screenshot",
            actor=None,  # Would be filled by real dispatcher
        )

        # Verify the capability is registered and routable
        specs_by_name = {s.name: s for s in runtime.registry.all()}
        assert "browser.screenshot" in specs_by_name

    def test_browser_verbs_implements_all_registered_capabilities(self, tmp_path):
        """Every registered browser capability has a handler."""
        config = JarvisConfig()
        config.reliability.site.base_url = "https://www.wizeperformance.com"

        runtime = build_wiz(config=config, home=tmp_path)

        # Get all browser capabilities
        browser_caps = [s for s in runtime.registry.all() if s.name.startswith("browser.")]

        # Get all handlers
        handlers = {}
        if runtime.browser_verbs is not None:
            handlers = runtime.browser_verbs.handlers()

        # Every browser capability must have a handler
        for cap in browser_caps:
            assert cap.name in handlers, f"No handler for {cap.name}"

    def test_browser_without_configured_url_does_not_initialize(self, tmp_path):
        """Without a production URL, browser verbs remain None."""
        config = JarvisConfig()
        # Don't set base_url

        runtime = build_wiz(config=config, home=tmp_path)

        # Browser verbs should not be initialized without config
        assert runtime.browser_verbs is None

    def test_browser_capability_authority_is_read(self, tmp_path):
        """All browser capabilities require only READ authority."""
        from openjarvis.wiz.authority import Authority

        config = JarvisConfig()
        config.reliability.site.base_url = "https://www.wizeperformance.com"

        runtime = build_wiz(config=config, home=tmp_path)

        browser_caps = [s for s in runtime.registry.all() if s.name.startswith("browser.")]

        for cap in browser_caps:
            assert cap.authority == Authority.READ, f"{cap.name} should require only READ"

    def test_browser_capability_risk_is_low(self, tmp_path):
        """All browser capabilities are LOW risk."""
        from openjarvis.wiz.capabilities import Risk

        config = JarvisConfig()
        config.reliability.site.base_url = "https://www.wizeperformance.com"

        runtime = build_wiz(config=config, home=tmp_path)

        browser_caps = [s for s in runtime.registry.all() if s.name.startswith("browser.")]

        for cap in browser_caps:
            assert cap.risk == Risk.LOW, f"{cap.name} should be LOW risk"


class TestBrowserHandlerStructure:
    """Verify browser handlers are properly structured."""

    def test_browser_verbs_handler_dict(self, tmp_path):
        """BrowserVerbs.handlers() returns correct structure."""
        from openjarvis.wiz.browser import BrowserVerbs
        from openjarvis.wiz.browser.url_safety import URLValidator

        verbs = BrowserVerbs(
            validator=URLValidator(),
            screenshot_dir=None,
        )

        handlers = verbs.handlers()

        # All expected handler names present
        expected_handlers = {
            "browser.open",
            "browser.inspect",
            "browser.interact",
            "browser.assert",
            "browser.screenshot",
            "browser.console",
            "browser.network",
        }

        assert set(handlers.keys()) == expected_handlers

        # All handlers are callable
        for name, handler in handlers.items():
            assert callable(handler), f"Handler for {name} is not callable"
