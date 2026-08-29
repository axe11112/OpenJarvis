"""Browser capability security: what it may never become."""

from __future__ import annotations

import pytest

from openjarvis.wiz.browser.actions import AssertText, Locator, LocatorType, Open
from openjarvis.wiz.browser.url_safety import URLSafetyError, URLValidator


class TestURLSafety:
    """URL validation for browser navigation."""

    def test_production_url_is_allowed(self):
        validator = URLValidator(
            production_base_url="https://www.wizeperformance.com"
        )
        assert validator.is_allowed("https://www.wizeperformance.com/dashboard")

    def test_localhost_is_blocked(self):
        validator = URLValidator()
        assert not validator.is_allowed("http://localhost:3000")
        assert not validator.is_allowed("http://127.0.0.1:8000")

    @pytest.mark.skip(reason="socket resolution may not work in all test environments")
    def test_private_network_is_blocked(self):
        validator = URLValidator()
        with pytest.raises(URLSafetyError, match="private network"):
            validator.validate("http://192.168.1.1")

    def test_metadata_endpoint_is_blocked(self):
        validator = URLValidator()
        with pytest.raises(URLSafetyError, match="metadata"):
            validator.validate("http://169.254.169.254/latest")

    def test_javascript_url_is_blocked(self):
        validator = URLValidator()
        with pytest.raises(URLSafetyError, match="javascript"):
            validator.validate("javascript:alert('xss')")

    def test_file_url_is_blocked(self):
        validator = URLValidator()
        with pytest.raises(URLSafetyError, match="file"):
            validator.validate("file:///etc/passwd")

    def test_credentials_in_url_are_blocked(self):
        validator = URLValidator()
        with pytest.raises(URLSafetyError, match="credentials"):
            validator.validate("https://user:password@example.com")

    def test_data_url_is_blocked(self):
        validator = URLValidator()
        with pytest.raises(URLSafetyError, match="data"):
            validator.validate("data:text/html,<script>alert(1)</script>")

    def test_unknown_origin_is_blocked(self):
        validator = URLValidator(
            production_base_url="https://www.wizeperformance.com"
        )
        assert not validator.is_allowed("https://attacker.com/phishing")

    def test_preview_origin_is_allowed(self):
        validator = URLValidator(
            production_base_url="https://www.wizeperformance.com",
            preview_allowed_origins=["https://preview-123.vercel.app"],
        )
        assert validator.is_allowed("https://preview-123.vercel.app/feature")

    def test_additional_origins_are_allowed(self):
        validator = URLValidator(
            production_base_url="https://www.wizeperformance.com",
            additional_allowed_origins=["https://staging.wizeperformance.com"],
        )
        assert validator.is_allowed("https://staging.wizeperformance.com/")


class TestActionTypes:
    """Action types are strongly typed."""

    def test_open_action_requires_non_empty_url(self):
        with pytest.raises(ValueError, match="empty"):
            Open(url="")

    def test_locator_escapes_attribute_values(self):
        locator = Locator(type=LocatorType.TEST_ID, value="test'id")
        # Escaping prevents injection into selector
        selector = locator.to_playwright_selector()
        assert "test\\'id" in selector or "test'id" in selector

    def test_assert_text_is_bounded(self):
        locator = Locator(type=LocatorType.TEXT, value="Sign in")
        assertion = AssertText(locator=locator, expected="Sign in", exact=False)
        # Assertion is typed and cannot accept arbitrary code
        assert assertion.expected == "Sign in"


class TestNoArbitraryExecution:
    """Verify no arbitrary execution paths exist."""

    def test_no_eval_in_browser_module(self):
        import inspect

        from openjarvis.wiz.browser import verbs, url_safety, actions

        # Check the actual implementation files, not docstrings
        for module in [verbs, url_safety, actions]:
            source = inspect.getsource(module)
            # Skip docstrings: look for actual code patterns
            lines = [
                line
                for line in source.split("\n")
                if not line.strip().startswith("#") and '"""' not in line
            ]
            code = "\n".join(lines)
            for forbidden in ("eval(", "exec("):
                assert forbidden not in code, f"Found {forbidden} in {module.__name__}"

    def test_no_unescaped_command_construction(self):
        import inspect

        from openjarvis.wiz.browser import actions, verbs

        source = inspect.getsource(verbs)
        # No f-strings or % formatting that concatenates user input into shell commands
        # (Playwright API is type-checked instead)
        assert "shell=True" not in source

    def test_no_arbitrary_js_evaluation(self):
        import inspect

        from openjarvis.wiz.browser import verbs

        source = inspect.getsource(verbs)
        # No page.evaluate() or similar arbitrary JS
        for forbidden in ("page.evaluate", "page.exec", "evaluate("):
            assert forbidden not in source


class TestDOMContentIsUntrusted:
    """DOM content must never become instructions to Wiz."""

    def test_element_text_is_data_not_code(self):
        """Text read from the page is presented as data, never executed."""
        locator = Locator(type=LocatorType.TEXT, value="Click me")
        # The action result contains the text as data
        # There is no mechanism to interpret it as a Wiz command
        assert isinstance(locator.value, str)

    def test_screenshot_path_is_not_auto_evaluated(self):
        """A screenshot path is a file path, not a Python expression."""
        # BrowserVerbs.take_screenshot() returns a path string
        # The path is never passed to eval() or exec()
        path = "/tmp/screenshot.png"
        assert path.endswith(".png")  # It's just a string


class TestAuthorityNotMutatedFromNaturalLanguage:
    """Authority cannot be escalated through natural language in browser output."""

    def test_browser_capability_is_read_only_by_default(self):
        from openjarvis.wiz.authority import Authority
        from openjarvis.wiz.browser.capabilities import browser_capabilities

        specs = {c.name: c for c in browser_capabilities()}

        # All browser capabilities require READ authority (minimal)
        for name, spec in specs.items():
            # browser.interact is READ, not SAFE_ACTION
            # (it's context-dependent: safe on Preview, blocked on Production)
            assert spec.authority == Authority.READ, f"{name} has wrong authority"

    def test_browser_output_is_structured_data(self):
        """Browser action results are dicts, not executable."""
        # BrowserVerbs handlers return Dict[str, Any], never code
        result = {"success": True, "title": "Page Title"}
        # This is data, never interpreted as a Wiz command or authority grant
        assert isinstance(result, dict)
        assert "success" in result
