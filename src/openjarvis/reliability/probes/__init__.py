"""Probe runners — how JARVIS finds out whether the site actually works."""

from __future__ import annotations

from openjarvis.reliability.probes._stubs import (
    BaseProbeRunner,
    CredentialRedactor,
    MissingCredentialError,
    ProbeRunnerRegistry,
    resolve_credentials,
    resolve_headers,
)
from openjarvis.reliability.probes.executor import (
    ConfirmationTracker,
    ProbeExecutor,
    escalate_severity,
)
from openjarvis.reliability.probes.http import HttpProbeRunner
from openjarvis.reliability.probes.noise import (
    KNOWN_NOISE_PROFILES,
    NoiseProfile,
    resolve_noise_profiles,
)
from openjarvis.reliability.probes.spec import (
    ProbeAssertions,
    ProbeExpectation,
    ProbeRetry,
    ProbeSpec,
    ProbeSpecError,
    ProbeStep,
    load_probe,
    load_probes,
    parse_probe,
)

# The browser runner registers itself on import, but Playwright is optional, so
# a missing extra must not break `import openjarvis.reliability`.  The module
# itself imports fine without Playwright — only run() raises — so this guard
# only fires if the module is genuinely broken.
try:  # pragma: no cover - exercised only on a broken install
    from openjarvis.reliability.probes.browser import (  # noqa: F401
        BrowserProbeRunner,
        BrowserUnavailableError,
    )
except ImportError:  # pragma: no cover
    BrowserProbeRunner = None  # type: ignore[assignment]
    BrowserUnavailableError = RuntimeError  # type: ignore[misc,assignment]

__all__ = [
    "KNOWN_NOISE_PROFILES",
    "BaseProbeRunner",
    "BrowserProbeRunner",
    "BrowserUnavailableError",
    "ConfirmationTracker",
    "CredentialRedactor",
    "HttpProbeRunner",
    "MissingCredentialError",
    "NoiseProfile",
    "ProbeAssertions",
    "ProbeExecutor",
    "ProbeExpectation",
    "ProbeRetry",
    "ProbeRunnerRegistry",
    "ProbeSpec",
    "ProbeSpecError",
    "ProbeStep",
    "escalate_severity",
    "load_probe",
    "load_probes",
    "parse_probe",
    "resolve_credentials",
    "resolve_headers",
    "resolve_noise_profiles",
]
