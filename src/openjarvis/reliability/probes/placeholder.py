"""Placeholder detection — a probe nobody has pointed at the real app.

The example probes shipped in ``configs/reliability/probes/`` use invented
selectors like ``input[name=email]``. They are illustrations, and against a
real application they will either fail (noise) or, worse, pass for the wrong
reason on a page that happens to match.

A placeholder probe is therefore refused rather than run, and reports
``NOT_CONFIGURED`` rather than ``PASS``. The alternative — a green tick from a
probe that was never aimed at anything — is precisely the false confidence
JARVIS exists to avoid.

Marking is explicit *and* heuristic: explicit so an author can declare intent,
heuristic so a spec copied from the examples and half-edited is still caught.
"""

from __future__ import annotations

from typing import List

from openjarvis.reliability.probes.spec import ProbeSpec

__all__ = ["PLACEHOLDER_MARKERS", "is_placeholder", "placeholder_reasons"]

#: Selector fragments that appear in the shipped examples. Their presence means
#: nobody has checked the probe against the real markup.
PLACEHOLDER_MARKERS = (
    "input[name=email]",
    "input[name=password]",
    "button[type=submit]",
    "[data-testid=dashboard-root]",
    "[data-testid=email]",
    "[data-testid=password]",
    "[data-testid=submit]",
)

#: Hosts that are obviously not a real target.
_EXAMPLE_HOSTS = ("example.com", "example.org", "localhost", "127.0.0.1")


def placeholder_reasons(spec: ProbeSpec) -> List[str]:
    """Return the reasons *spec* looks like a placeholder, or an empty list.

    An empty list means the probe is safe to run and its result can be trusted.
    """
    reasons: List[str] = []

    if spec.metadata.get("placeholder") is True:
        reasons.append("marked placeholder = true in its metadata")

    markers = {
        step.selector for step in spec.steps if step.selector in PLACEHOLDER_MARKERS
    }
    markers |= {
        expectation.selector
        for expectation in spec.expect
        if expectation.selector in PLACEHOLDER_MARKERS
    }
    if markers:
        reasons.append(
            "uses example selectors that were never pointed at the real "
            f"application ({', '.join(sorted(markers))})"
        )

    for step in spec.steps:
        if step.action == "goto" and any(host in step.url for host in _EXAMPLE_HOSTS):
            reasons.append(f"navigates to a placeholder host ({step.url})")
            break

    if spec.runner == "http" and any(host in spec.url for host in _EXAMPLE_HOSTS):
        reasons.append(f"targets a placeholder host ({spec.url})")

    return reasons


def is_placeholder(spec: ProbeSpec) -> bool:
    """Whether *spec* must not be treated as a real check."""
    return bool(placeholder_reasons(spec))
