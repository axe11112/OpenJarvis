"""Tests for probe spec parsing and validation."""

from __future__ import annotations

import pytest

from openjarvis.reliability.probes.spec import (
    ProbeSpecError,
    load_probe,
    load_probes,
    parse_probe,
)
from openjarvis.reliability.types import Severity

_MINIMAL = {
    "probe": {
        "id": "homepage",
        "steps": [{"action": "goto", "url": "/"}],
    }
}

_LOGIN = {
    "probe": {
        "id": "auth-login",
        "name": "Login → dashboard",
        "component": "authentication",
        "severity": "critical",
        "credentials": {
            "email": "JARVIS_TEST_USER_EMAIL",
            "password": "JARVIS_TEST_USER_PASSWORD",
        },
        "schedule": {"type": "interval", "value": "600"},
        "retry": {"attempts": 2, "confirm_runs": 2, "backoff_seconds": 5},
        "assertions": {"no_console_errors": True, "max_http_status": 399},
        "steps": [
            {"action": "goto", "url": "/login"},
            {
                "action": "fill",
                "selector": "[data-testid=email]",
                "value_from": "email",
            },
            {
                "action": "fill",
                "selector": "[data-testid=password]",
                "value_from": "password",
            },
            {"action": "click", "selector": "[data-testid=submit]"},
        ],
        "expect": [
            {"kind": "url", "matches": "/dashboard"},
            {"kind": "visible", "selector": "[data-testid=dashboard-root]"},
        ],
    }
}


class TestParsing:
    def test_minimal_spec(self):
        spec = parse_probe(_MINIMAL)
        assert spec.id == "homepage"
        assert spec.runner == "browser"
        assert spec.severity is Severity.MEDIUM
        assert spec.component == "homepage"  # defaults to the id
        assert spec.enabled is True

    def test_full_spec(self):
        spec = parse_probe(_LOGIN)
        assert spec.severity is Severity.CRITICAL
        assert spec.component == "authentication"
        assert spec.schedule_value == "600"
        assert spec.retry.attempts == 2
        assert spec.assertions.no_console_errors is True
        assert spec.assertions.max_http_status == 399
        assert len(spec.steps) == 4
        assert len(spec.expect) == 2

    def test_mutating_defaults_false(self):
        assert parse_probe(_MINIMAL).mutating is False

    def test_display_name_falls_back_to_id(self):
        assert parse_probe(_MINIMAL).display_name == "homepage"
        assert parse_probe(_LOGIN).display_name == "Login → dashboard"


class TestValidation:
    def test_missing_id(self):
        with pytest.raises(ProbeSpecError, match="missing required key 'id'"):
            parse_probe({"probe": {"steps": [{"action": "goto", "url": "/"}]}})

    def test_unknown_action(self):
        with pytest.raises(ProbeSpecError, match="unknown action 'teleport'"):
            parse_probe({"probe": {"id": "x", "steps": [{"action": "teleport"}]}})

    def test_unknown_expectation_kind(self):
        with pytest.raises(ProbeSpecError, match="unknown kind 'vibes'"):
            parse_probe(
                {
                    "probe": {
                        "id": "x",
                        "steps": [{"action": "goto", "url": "/"}],
                        "expect": [{"kind": "vibes"}],
                    }
                }
            )

    def test_unknown_runner(self):
        with pytest.raises(ProbeSpecError, match="unknown runner 'carrier-pigeon'"):
            parse_probe({"probe": {"id": "x", "runner": "carrier-pigeon"}})

    def test_unknown_severity(self):
        with pytest.raises(ProbeSpecError, match="Unknown severity"):
            parse_probe({"probe": {"id": "x", "severity": "apocalyptic"}})

    def test_click_needs_selector(self):
        with pytest.raises(ProbeSpecError, match="needs a selector"):
            parse_probe({"probe": {"id": "x", "steps": [{"action": "click"}]}})

    def test_goto_needs_url(self):
        with pytest.raises(ProbeSpecError, match="needs a url"):
            parse_probe({"probe": {"id": "x", "steps": [{"action": "goto"}]}})

    def test_fill_needs_a_value_source(self):
        with pytest.raises(ProbeSpecError, match="needs 'value' or 'value_from'"):
            parse_probe(
                {"probe": {"id": "x", "steps": [{"action": "fill", "selector": "#a"}]}}
            )

    def test_visible_expectation_needs_selector(self):
        with pytest.raises(ProbeSpecError, match="needs a selector"):
            parse_probe(
                {
                    "probe": {
                        "id": "x",
                        "steps": [{"action": "goto", "url": "/"}],
                        "expect": [{"kind": "visible"}],
                    }
                }
            )

    def test_http_runner_needs_url(self):
        with pytest.raises(ProbeSpecError, match="http runner needs a 'url'"):
            parse_probe({"probe": {"id": "x", "runner": "http"}})

    def test_browser_runner_needs_steps(self):
        with pytest.raises(ProbeSpecError, match="needs at least one step"):
            parse_probe({"probe": {"id": "x"}})

    def test_undeclared_credential_is_rejected(self):
        """A typo in value_from must fail at load time, not at 3am in production."""
        with pytest.raises(
            ProbeSpecError, match="not declared in \\[probe.credentials\\]"
        ):
            parse_probe(
                {
                    "probe": {
                        "id": "x",
                        "credentials": {"email": "EMAIL_ENV"},
                        "steps": [
                            {
                                "action": "fill",
                                "selector": "#p",
                                "value_from": "passwrod",
                            }
                        ],
                    }
                }
            )


class TestReproSteps:
    def test_reads_as_instructions(self):
        steps = parse_probe(_LOGIN).repro_steps()
        assert steps[0] == "Open /login"
        assert steps[3] == "Click [data-testid=submit]"
        assert steps[-1] == "Expect that [data-testid=dashboard-root] is visible"

    def test_never_names_a_credential_value(self):
        """Reproduction steps go into incidents and PRs — they describe the
        credential's *source*, never its value."""
        steps = parse_probe(_LOGIN).repro_steps()
        joined = " ".join(steps)
        assert "the email credential" in joined
        assert "the password credential" in joined
        assert "JARVIS_TEST_USER_PASSWORD" not in joined

    def test_custom_label_wins(self):
        spec = parse_probe(
            {
                "probe": {
                    "id": "x",
                    "steps": [
                        {"action": "goto", "url": "/", "label": "Open the homepage"}
                    ],
                }
            }
        )
        assert spec.repro_steps()[0] == "Open the homepage"

    def test_expectation_summary(self):
        assert "matches /dashboard" in parse_probe(_LOGIN).expectation_summary()


class TestLoading:
    def _write(self, tmp_path, name, body):
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_load_probe(self, tmp_path):
        path = self._write(
            tmp_path,
            "homepage.toml",
            """
[probe]
id = "homepage"
severity = "high"
[[probe.steps]]
action = "goto"
url = "/"
""",
        )
        spec = load_probe(path)
        assert spec.id == "homepage"
        assert spec.severity is Severity.HIGH
        assert spec.source_path == str(path)

    def test_load_probe_invalid_toml(self, tmp_path):
        path = self._write(tmp_path, "bad.toml", "this is not = = toml")
        with pytest.raises(ProbeSpecError, match="invalid TOML"):
            load_probe(path)

    def test_load_probe_missing_file(self, tmp_path):
        with pytest.raises(ProbeSpecError, match="cannot read probe spec"):
            load_probe(tmp_path / "nope.toml")

    def test_load_probes_sorted(self, tmp_path):
        for probe_id in ("zulu", "alpha", "mike"):
            self._write(
                tmp_path,
                f"{probe_id}.toml",
                f'[probe]\nid = "{probe_id}"\n'
                '[[probe.steps]]\naction = "goto"\nurl = "/"\n',
            )
        assert [s.id for s in load_probes(tmp_path)] == ["alpha", "mike", "zulu"]

    def test_load_probes_missing_dir(self, tmp_path):
        assert load_probes(tmp_path / "nope") == []

    def test_one_bad_spec_does_not_break_the_others(self, tmp_path):
        """A typo in one probe must not take the whole monitor offline."""
        self._write(
            tmp_path,
            "good.toml",
            '[probe]\nid = "good"\n[[probe.steps]]\naction = "goto"\nurl = "/"\n',
        )
        self._write(tmp_path, "bad.toml", '[probe]\nid = "bad"\n')
        specs = load_probes(tmp_path)
        assert [s.id for s in specs] == ["good"]

    def test_strict_mode_raises(self, tmp_path):
        self._write(tmp_path, "bad.toml", '[probe]\nid = "bad"\n')
        with pytest.raises(ProbeSpecError):
            load_probes(tmp_path, strict=True)

    def test_duplicate_ids_rejected_in_strict_mode(self, tmp_path):
        for name in ("a.toml", "b.toml"):
            self._write(
                tmp_path,
                name,
                '[probe]\nid = "dup"\n[[probe.steps]]\naction = "goto"\nurl = "/"\n',
            )
        with pytest.raises(ProbeSpecError, match="duplicate probe id"):
            load_probes(tmp_path, strict=True)

    def test_duplicate_ids_skipped_when_lenient(self, tmp_path):
        for name in ("a.toml", "b.toml"):
            self._write(
                tmp_path,
                name,
                '[probe]\nid = "dup"\n[[probe.steps]]\naction = "goto"\nurl = "/"\n',
            )
        assert len(load_probes(tmp_path)) == 1


class TestShippedExamples:
    def test_bundled_specs_are_valid(self):
        """Every spec shipped in the repo must parse."""
        from pathlib import Path

        directory = (
            Path(__file__).resolve().parents[2] / "configs" / "reliability" / "probes"
        )
        if not directory.is_dir():
            pytest.skip("no bundled probe specs")
        specs = load_probes(directory, strict=True)
        assert specs, "expected at least one bundled probe spec"
        assert len({s.id for s in specs}) == len(specs)
