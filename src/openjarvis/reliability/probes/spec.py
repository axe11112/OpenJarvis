"""Declarative probe specifications, loaded from TOML.

A probe spec is *data*: it can be stored, diffed, versioned and rendered without
executing anything.  The format mirrors the operator manifests in
``openjarvis/operators/data/*.toml``.

Credentials are referenced by the **name of an environment variable**, never by
value — see ``docs/JARVIS_SECURITY.md`` §6.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from openjarvis.reliability.types import Severity

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

__all__ = [
    "ProbeAssertions",
    "ProbeExpectation",
    "ProbeRetry",
    "ProbeSpec",
    "ProbeSpecError",
    "ProbeStep",
    "load_probe",
    "load_probes",
]


class ProbeSpecError(ValueError):
    """Raised when a probe spec is malformed.

    Messages name the file and the offending key so a typo is a one-line fix
    rather than a debugging session.
    """


#: Step actions the browser runner understands.
VALID_ACTIONS = frozenset(
    {
        "goto",
        "click",
        "fill",
        "press",
        "select",
        "check",
        "uncheck",
        "wait_for",
        "wait_for_url",
        "wait_for_timeout",
        "screenshot",
    }
)

#: Expectation kinds the runner can assert.
VALID_EXPECTATIONS = frozenset(
    {"url", "visible", "hidden", "text", "not_text", "status", "title"}
)

VALID_RUNNERS = frozenset({"browser", "http"})


@dataclass(slots=True)
class ProbeStep:
    """One action in a workflow."""

    action: str
    url: str = ""
    selector: str = ""
    value: str = ""
    value_from: str = ""  # key into ``ProbeSpec.credentials``
    text: str = ""
    state: str = "visible"  # for wait_for
    timeout_ms: int = 0  # 0 = use the spec default
    label: str = ""  # human-readable description for reproduction steps

    def describe(self) -> str:
        """Return a human-readable one-liner, safe to store as a repro step.

        Never includes a credential: ``value_from`` steps describe the *source*,
        not the value.
        """
        if self.label:
            return self.label
        if self.action == "goto":
            return f"Open {self.url}"
        if self.action == "fill":
            what = (
                f"the {self.value_from} credential"
                if self.value_from
                else f"'{self.value}'"
            )
            return f"Fill {self.selector} with {what}"
        if self.action in ("click", "check", "uncheck"):
            return f"{self.action.capitalize()} {self.selector}"
        if self.action == "press":
            return f"Press {self.value} on {self.selector or 'the page'}"
        if self.action == "select":
            return f"Select '{self.value}' in {self.selector}"
        if self.action == "wait_for":
            return f"Wait for {self.selector} to be {self.state}"
        if self.action == "wait_for_url":
            return f"Wait for the URL to match {self.url}"
        if self.action == "wait_for_timeout":
            return f"Wait {self.timeout_ms}ms"
        if self.action == "screenshot":
            return "Take a screenshot"
        return self.action


@dataclass(slots=True)
class ProbeExpectation:
    """A declared expectation about the end state."""

    kind: str
    matches: str = ""
    selector: str = ""
    value: str = ""

    def describe(self) -> str:
        """Return a human-readable statement of the expectation."""
        if self.kind == "url":
            return f"the URL matches {self.matches}"
        if self.kind == "title":
            return f"the page title contains {self.matches!r}"
        if self.kind == "visible":
            return f"{self.selector} is visible"
        if self.kind == "hidden":
            return f"{self.selector} is hidden"
        if self.kind == "text":
            return f"{self.selector or 'the page'} contains {self.value!r}"
        if self.kind == "not_text":
            return f"{self.selector or 'the page'} does not contain {self.value!r}"
        if self.kind == "status":
            return f"the HTTP status is {self.matches}"
        return self.kind


@dataclass(slots=True)
class ProbeAssertions:
    """Cross-cutting health assertions applied to the whole run.

    ``no_console_errors`` asserts on genuine JavaScript errors only.  Console
    messages the browser emits for failed subresource loads are filtered out —
    they are network problems, already reported by ``no_failed_requests`` and
    ``max_http_status`` with far more detail.  Use
    ``ignore_console_patterns`` for application-specific noise on top of that.

    ``ignore_request_patterns`` is the same escape hatch for the network side.
    Some frameworks *routinely* abort requests they started on purpose — a
    router that speculatively prefetches the next page cancels the fetch when
    the user goes elsewhere, and the browser reports a cancelled request as a
    failed one.  Without a way to name that, ``no_failed_requests`` is
    unusable on those applications: it fails on every run of a perfectly
    healthy page, which is exactly the noise that trains an owner to ignore
    JARVIS.  Patterns are regexes matched against ``"METHOD URL reason"``, so
    an author can scope them by verb, by URL shape, by failure reason, or any
    combination — never a blanket "ignore network failures" switch.

    ``ignore_known_noise`` names one or more vetted profiles from
    :mod:`openjarvis.reliability.probes.noise` instead of restating their
    regexes.  It exists because those two lists have a sharp edge: the same
    framework event can surface as a console error *and* as a failed request,
    so muting it correctly means writing two patterns in two different places,
    and the intuitive shorthand for one of them (``"Failed to fetch"``) is
    broad enough to hide a genuinely broken API call.  A profile name is
    reviewed once and says what it means; the probe still has to ask for it.

    The three are additive.  A probe may name a profile *and* declare its own
    patterns; nothing is filtered that no spec asked to filter.
    """

    no_console_errors: bool = False
    no_failed_requests: bool = False
    max_http_status: int = 0  # 0 = not asserted
    max_duration_seconds: float = 0.0  # 0 = not asserted
    ignore_console_patterns: List[str] = field(default_factory=list)
    ignore_request_patterns: List[str] = field(default_factory=list)
    ignore_known_noise: List[str] = field(default_factory=list)

    def resolved_console_patterns(self) -> List[str]:
        """Author patterns plus those of every named profile.

        Kept as a method rather than folded into the field at parse time so
        that ``probe show`` can still print what the author actually wrote.
        """
        from openjarvis.reliability.probes.noise import resolve_noise_profiles

        console, _ = resolve_noise_profiles(self.ignore_known_noise)
        return [*self.ignore_console_patterns, *console]

    def resolved_request_patterns(self) -> List[str]:
        """Author patterns plus those of every named profile."""
        from openjarvis.reliability.probes.noise import resolve_noise_profiles

        _, requests = resolve_noise_profiles(self.ignore_known_noise)
        return [*self.ignore_request_patterns, *requests]


@dataclass(slots=True)
class ProbeRetry:
    """Retry and flake-suppression policy."""

    attempts: int = 1  # in-run retries before the result is believed
    confirm_runs: int = 2  # consecutive failures before an incident opens
    backoff_seconds: float = 30.0


@dataclass
class ProbeSpec:
    """A declarative workflow probe."""

    id: str
    name: str = ""
    component: str = ""
    severity: Severity = Severity.MEDIUM
    runner: str = "browser"
    enabled: bool = True
    description: str = ""
    # http runner
    url: str = ""
    method: str = "GET"
    # scheduling
    schedule_type: str = "interval"
    schedule_value: str = "300"
    # safety
    mutating: bool = False  # workflows that create data are opt-in
    # execution
    timeout_ms: int = 30000
    trace_on_failure: bool = True
    credentials: Dict[str, str] = field(default_factory=dict)
    steps: List[ProbeStep] = field(default_factory=list)
    expect: List[ProbeExpectation] = field(default_factory=list)
    assertions: ProbeAssertions = field(default_factory=ProbeAssertions)
    retry: ProbeRetry = field(default_factory=ProbeRetry)
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_path: str = ""

    @property
    def display_name(self) -> str:
        """Human-readable name, falling back to the ID."""
        return self.name or self.id

    def repro_steps(self) -> List[str]:
        """Render the workflow as human-readable reproduction steps.

        Contains no credential values by construction — see
        :meth:`ProbeStep.describe`.
        """
        steps = [step.describe() for step in self.steps]
        for expectation in self.expect:
            steps.append(f"Expect that {expectation.describe()}")
        return steps

    def expectation_summary(self) -> str:
        """One-line summary of everything this probe expects."""
        if not self.expect:
            return "the workflow completes without errors"
        return "; ".join(item.describe() for item in self.expect)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _require(data: Dict[str, Any], key: str, where: str) -> Any:
    if key not in data or data[key] in ("", None):
        raise ProbeSpecError(f"{where}: missing required key '{key}'")
    return data[key]


def _parse_step(raw: Dict[str, Any], where: str, index: int) -> ProbeStep:
    action = _require(raw, "action", f"{where} step {index}")
    if action not in VALID_ACTIONS:
        raise ProbeSpecError(
            f"{where} step {index}: unknown action '{action}'; "
            f"valid actions: {', '.join(sorted(VALID_ACTIONS))}"
        )
    step = ProbeStep(
        action=action,
        url=raw.get("url", ""),
        selector=raw.get("selector", ""),
        value=str(raw.get("value", "")),
        value_from=raw.get("value_from", ""),
        text=raw.get("text", ""),
        state=raw.get("state", "visible"),
        timeout_ms=int(raw.get("timeout_ms", 0) or 0),
        label=raw.get("label", ""),
    )
    needs_selector = {
        "click",
        "fill",
        "press",
        "select",
        "check",
        "uncheck",
        "wait_for",
    }
    if action in needs_selector and not step.selector:
        raise ProbeSpecError(
            f"{where} step {index}: action '{action}' needs a selector"
        )
    if action in ("goto", "wait_for_url") and not step.url:
        raise ProbeSpecError(f"{where} step {index}: action '{action}' needs a url")
    if action == "fill" and not step.value and not step.value_from:
        raise ProbeSpecError(
            f"{where} step {index}: action 'fill' needs 'value' or 'value_from'"
        )
    return step


def _parse_expectation(raw: Dict[str, Any], where: str, index: int) -> ProbeExpectation:
    kind = _require(raw, "kind", f"{where} expectation {index}")
    if kind not in VALID_EXPECTATIONS:
        raise ProbeSpecError(
            f"{where} expectation {index}: unknown kind '{kind}'; "
            f"valid kinds: {', '.join(sorted(VALID_EXPECTATIONS))}"
        )
    expectation = ProbeExpectation(
        kind=kind,
        matches=str(raw.get("matches", "")),
        selector=raw.get("selector", ""),
        value=str(raw.get("value", "")),
    )
    if kind in ("visible", "hidden") and not expectation.selector:
        raise ProbeSpecError(
            f"{where} expectation {index}: kind '{kind}' needs a selector"
        )
    if kind in ("url", "status", "title") and not expectation.matches:
        raise ProbeSpecError(
            f"{where} expectation {index}: kind '{kind}' needs 'matches'"
        )
    if kind in ("text", "not_text") and not expectation.value:
        raise ProbeSpecError(
            f"{where} expectation {index}: kind '{kind}' needs 'value'"
        )
    return expectation


def _parse_noise_profiles(assertions_raw: Dict[str, Any], where: str) -> List[str]:
    """Validate ``ignore_known_noise`` against the profile registry.

    An unknown name is rejected rather than ignored.  The quiet version of
    this typo is a probe that looks like it suppresses framework noise, does
    not, and gets muted by its owner the third time it cries wolf — or the
    reverse, a probe whose author believes a check is active when the name
    guarding it never matched anything.
    """
    from openjarvis.reliability.probes.noise import KNOWN_NOISE_PROFILES

    names = list(assertions_raw.get("ignore_known_noise", []) or [])
    unknown = [name for name in names if name not in KNOWN_NOISE_PROFILES]
    if unknown:
        raise ProbeSpecError(
            f"{where}: unknown noise profile(s) {', '.join(sorted(unknown))!r} in "
            f"ignore_known_noise; known profiles: "
            f"{', '.join(sorted(KNOWN_NOISE_PROFILES))}"
        )
    return names


def parse_probe(data: Dict[str, Any], *, where: str = "<dict>") -> ProbeSpec:
    """Build a :class:`ProbeSpec` from parsed TOML data."""
    probe = data.get("probe", data)
    if not isinstance(probe, dict):
        raise ProbeSpecError(f"{where}: [probe] must be a table")

    probe_id = _require(probe, "id", where)
    runner = probe.get("runner", "browser")
    if runner not in VALID_RUNNERS:
        raise ProbeSpecError(
            f"{where}: unknown runner '{runner}'; "
            f"valid runners: {', '.join(sorted(VALID_RUNNERS))}"
        )

    try:
        severity = Severity.parse(probe.get("severity", "MEDIUM"))
    except ValueError as exc:
        raise ProbeSpecError(f"{where}: {exc}") from exc

    schedule = probe.get("schedule", {}) or {}
    retry_raw = probe.get("retry", {}) or {}
    assertions_raw = probe.get("assertions", {}) or {}

    spec = ProbeSpec(
        id=probe_id,
        name=probe.get("name", ""),
        component=probe.get("component", "") or probe_id,
        severity=severity,
        runner=runner,
        enabled=bool(probe.get("enabled", True)),
        description=probe.get("description", ""),
        url=probe.get("url", ""),
        method=str(probe.get("method", "GET")).upper(),
        schedule_type=schedule.get("type", "interval"),
        schedule_value=str(schedule.get("value", "300")),
        mutating=bool(probe.get("mutating", False)),
        timeout_ms=int(probe.get("timeout_ms", 30000)),
        trace_on_failure=bool(probe.get("trace_on_failure", True)),
        credentials=dict(probe.get("credentials", {}) or {}),
        assertions=ProbeAssertions(
            no_console_errors=bool(assertions_raw.get("no_console_errors", False)),
            no_failed_requests=bool(assertions_raw.get("no_failed_requests", False)),
            max_http_status=int(assertions_raw.get("max_http_status", 0) or 0),
            max_duration_seconds=float(
                assertions_raw.get("max_duration_seconds", 0.0) or 0.0
            ),
            ignore_console_patterns=list(
                assertions_raw.get("ignore_console_patterns", []) or []
            ),
            ignore_request_patterns=list(
                assertions_raw.get("ignore_request_patterns", []) or []
            ),
            ignore_known_noise=_parse_noise_profiles(assertions_raw, where),
        ),
        retry=ProbeRetry(
            attempts=int(retry_raw.get("attempts", 1) or 1),
            confirm_runs=int(retry_raw.get("confirm_runs", 2) or 2),
            backoff_seconds=float(retry_raw.get("backoff_seconds", 30.0) or 30.0),
        ),
        metadata=dict(probe.get("metadata", {}) or {}),
    )

    spec.steps = [
        _parse_step(raw, where, index)
        for index, raw in enumerate(probe.get("steps", []) or [], start=1)
    ]
    spec.expect = [
        _parse_expectation(raw, where, index)
        for index, raw in enumerate(probe.get("expect", []) or [], start=1)
    ]

    # Cross-field validation
    if spec.runner == "http" and not spec.url:
        raise ProbeSpecError(f"{where}: the http runner needs a 'url'")
    if spec.runner == "browser" and not spec.steps:
        raise ProbeSpecError(f"{where}: the browser runner needs at least one step")

    declared = set(spec.credentials)
    for index, step in enumerate(spec.steps, start=1):
        if step.value_from and step.value_from not in declared:
            raise ProbeSpecError(
                f"{where} step {index}: value_from '{step.value_from}' is not "
                f"declared in [probe.credentials] "
                f"(declared: {', '.join(sorted(declared)) or 'none'})"
            )
    return spec


def load_probe(path: str | Path) -> ProbeSpec:
    """Load a single probe spec from a TOML file."""
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise ProbeSpecError(f"{path}: cannot read probe spec ({exc})") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ProbeSpecError(f"{path}: invalid TOML ({exc})") from exc

    spec = parse_probe(data, where=str(path))
    spec.source_path = str(path)
    return spec


def load_probes(directory: str | Path, *, strict: bool = False) -> List[ProbeSpec]:
    """Load every ``*.toml`` probe spec in *directory*, sorted by ID.

    A malformed spec is logged and skipped so one typo cannot take the whole
    monitor offline.  Pass ``strict=True`` to raise instead — the CLI does, so
    an author sees their mistake immediately.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []

    specs: List[ProbeSpec] = []
    seen: Dict[str, str] = {}
    for path in sorted(directory.glob("*.toml")):
        try:
            spec = load_probe(path)
        except ProbeSpecError:
            if strict:
                raise
            logger.exception("Skipping malformed probe spec %s", path)
            continue
        if spec.id in seen:
            message = (
                f"{path}: duplicate probe id '{spec.id}' "
                f"(already defined in {seen[spec.id]})"
            )
            if strict:
                raise ProbeSpecError(message)
            logger.warning("%s", message)
            continue
        seen[spec.id] = str(path)
        specs.append(spec)
    return sorted(specs, key=lambda s: s.id)
