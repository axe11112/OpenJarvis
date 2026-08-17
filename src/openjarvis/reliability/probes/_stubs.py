"""Probe runner ABC, registry, and the credential redactor.

Runners turn a :class:`~openjarvis.reliability.probes.spec.ProbeSpec` into a
:class:`~openjarvis.reliability.types.ProbeResult`.  They are the only place in
JARVIS that ever holds a test-account credential in memory, and they are
responsible for making sure none of it escapes into a result.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from openjarvis.reliability.probes.spec import ProbeSpec
from openjarvis.reliability.types import Evidence, ProbeResult

logger = logging.getLogger(__name__)

__all__ = [
    "BaseProbeRunner",
    "CredentialRedactor",
    "MissingCredentialError",
    "ProbeRunnerRegistry",
    "resolve_credentials",
    "resolve_headers",
]

#: Minimum length before a credential value is worth redacting.  Redacting a
#: one- or two-character value would mangle unrelated text without adding
#: protection.
_MIN_REDACTABLE_LENGTH = 4

_REDACTION = "[REDACTED]"


class MissingCredentialError(RuntimeError):
    """Raised when a probe declares a credential whose env var is unset."""


def resolve_credentials(spec: ProbeSpec) -> Dict[str, str]:
    """Resolve a spec's declared env-var names into values.

    The returned mapping stays inside the runner.  It is never attached to a
    :class:`ProbeResult`, never logged, and never serialized.

    Raises
    ------
    MissingCredentialError
        When a declared environment variable is unset, naming the variable but
        never printing any value.
    """
    resolved: Dict[str, str] = {}
    missing: List[str] = []
    for logical_name, env_name in spec.credentials.items():
        value = os.environ.get(env_name, "")
        if not value:
            missing.append(f"{logical_name} (${env_name})")
            continue
        resolved[logical_name] = value
    if missing:
        raise MissingCredentialError(
            f"probe '{spec.id}' needs credentials that are not set: "
            + ", ".join(missing)
        )
    return resolved


def resolve_headers(spec: ProbeSpec) -> tuple[Dict[str, str], Dict[str, str]]:
    """Resolve a spec's request headers.

    Returns ``(headers, secret_values)`` — the full header mapping to send, and
    just the env-sourced values, which the caller must hand to a
    :class:`CredentialRedactor`.  The split exists so the caller cannot forget:
    a header sourced from the environment is a secret by construction, and the
    one place it would otherwise surface is a captured request URL or an error
    message quoting the request.

    Raises
    ------
    MissingCredentialError
        When a declared environment variable is unset.  Names the header and
        the variable, never a value.
    """
    headers: Dict[str, str] = dict(spec.headers)
    secrets: Dict[str, str] = {}
    missing: List[str] = []
    for header_name, env_name in spec.headers_from_env.items():
        value = os.environ.get(env_name, "")
        if not value:
            missing.append(f"{header_name} (${env_name})")
            continue
        headers[header_name] = value
        secrets[header_name] = value
    if missing:
        raise MissingCredentialError(
            f"probe '{spec.id}' needs header value(s) that are not set: "
            + ", ".join(missing)
        )
    return headers, secrets


class CredentialRedactor:
    """Removes known credential values from captured text.

    Browser and HTTP errors can quote the value that was being submitted (a
    failed ``fill`` may echo it).  Every string a runner captures passes through
    here before it becomes evidence, so a resolved credential cannot reach the
    incident store, a notification, or a model prompt.

    This is a backstop, not the primary control — the primary control is that
    ``Evidence`` has no field for a credential in the first place.
    """

    def __init__(self, values: Optional[Dict[str, str]] = None) -> None:
        self._values = sorted(
            (v for v in (values or {}).values() if len(v) >= _MIN_REDACTABLE_LENGTH),
            key=len,
            reverse=True,  # longest first, so substrings don't mask supersets
        )

    def __call__(self, text: str) -> str:
        """Return *text* with every known credential value replaced."""
        return self.redact(text)

    def redact(self, text: str) -> str:
        """Return *text* with every known credential value replaced."""
        if not text or not self._values:
            return text
        for value in self._values:
            if value in text:
                text = text.replace(value, _REDACTION)
        return text

    def redact_evidence(self, evidence: Evidence) -> Evidence:
        """Redact an evidence item's free-text fields in place."""
        evidence.summary = self.redact(evidence.summary)
        evidence.content = self.redact(evidence.content)
        return evidence


class BaseProbeRunner(ABC):
    """Executes a probe spec and reports what happened."""

    runner_id: str

    @abstractmethod
    def run(
        self,
        spec: ProbeSpec,
        *,
        base_url: str = "",
        evidence_dir: Optional[str] = None,
        **options: Any,
    ) -> ProbeResult:
        """Run *spec* once and return a :class:`ProbeResult`.

        Implementations must never raise for an ordinary probe failure — a
        failure is a result, not an exception.  Only programming errors and
        unmet preconditions (a missing credential, an unavailable browser)
        propagate.
        """


class ProbeRunnerRegistry:
    """Maps a spec's ``runner`` field to a runner implementation.

    A small local registry rather than a core ``RegistryBase`` subclass: probe
    runners are an internal detail of the reliability subsystem and do not need
    to be discoverable framework-wide.
    """

    _runners: Dict[str, type] = {}

    @classmethod
    def register(cls, key: str):
        """Class decorator registering a runner under *key*."""

        def decorator(runner_cls: type) -> type:
            cls._runners[key] = runner_cls
            return runner_cls

        return decorator

    @classmethod
    def get(cls, key: str) -> type:
        """Return the runner class registered under *key*."""
        if key not in cls._runners:
            raise KeyError(
                f"No probe runner registered for '{key}'; "
                f"available: {', '.join(sorted(cls._runners)) or 'none'}"
            )
        return cls._runners[key]

    @classmethod
    def create(cls, key: str, *args: Any, **kwargs: Any) -> BaseProbeRunner:
        """Instantiate the runner registered under *key*."""
        return cls.get(key)(*args, **kwargs)

    @classmethod
    def keys(cls) -> List[str]:
        """Return all registered runner keys."""
        return sorted(cls._runners)
