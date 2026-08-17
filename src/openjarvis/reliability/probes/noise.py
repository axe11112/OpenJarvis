"""Named, vetted noise profiles a probe can opt into by name.

Some frameworks emit errors *as part of working correctly*.  A router that
speculatively prefetches the page it thinks you will visit next has to cancel
that fetch when you go somewhere else, and a cancelled fetch is reported by the
browser as a failed request — and, if application code was awaiting it, as a
JavaScript error too.  On a healthy site this happens on most page loads.

There are two bad ways to deal with that, and this module exists to avoid both.

*Globally* teaching the runner about Next.js would mean every probe against
every application silently stops reporting a class of error, whether or not it
runs Next.js.  A monitoring system that decides on its own what not to look at
is worth very little.

*Leaving each author to write their own regex* is what we did first, and the
failure mode is worse than it looks.  The message that has to be matched here
is ``"Failed to fetch RSC payload for ... TypeError: Failed to fetch"``, and
the obvious regex somebody reaches for under time pressure is ``Failed to
fetch`` — which also mutes a genuinely broken API call, a dead image host and a
CORS misconfiguration, on the one assertion that would have caught them.  The
regex is the dangerous part, and it should be written once, reviewed, and
tested — not retyped per probe.

So: the *knowledge* lives here, the *decision* lives in the probe spec.  A
probe opts in by name::

    [probe.assertions]
    no_console_errors = true
    ignore_known_noise = ["nextjs_rsc_prefetch"]

Nothing is filtered unless a spec asks for it, an unknown name is a spec error
rather than a silent no-op, and the patterns below are narrow enough to state
plainly what they will and will not hide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

__all__ = ["KNOWN_NOISE_PROFILES", "NoiseProfile", "resolve_noise_profiles"]


@dataclass(frozen=True, slots=True)
class NoiseProfile:
    """A named set of patterns for one framework's benign self-inflicted noise.

    Attributes
    ----------
    description:
        What the profile covers, shown by ``jarvis reliability probe show``.
    console_patterns:
        Regexes matched against console-message and uncaught-exception text.
    request_patterns:
        Regexes matched against ``"METHOD URL reason"`` for failed requests.
    """

    description: str
    console_patterns: List[str] = field(default_factory=list)
    request_patterns: List[str] = field(default_factory=list)


#: The Next.js App Router's speculative RSC prefetching.
#:
#: The router issues a ``?_rsc=`` fetch for links it expects the visitor to
#: follow, and aborts it when they navigate elsewhere first.  Chromium reports
#: the abort as ``net::ERR_ABORTED``; when the router was awaiting the response
#: it additionally logs ``Failed to fetch RSC payload for <url>. Falling back to
#: browser navigation.`` and then *does* fall back, so the visitor still gets
#: the page.
#:
#: Both patterns are deliberately over-specified:
#:
#: * The console pattern requires the whole distinctive sentence, anchored at
#:   the start of the message, *including* the "Falling back to browser
#:   navigation" clause — that clause is the router stating it recovered.  A
#:   bare ``TypeError: Failed to fetch`` from application code does not match,
#:   and neither does an RSC error that did *not* recover.
#: * The request pattern requires all three of: a ``?_rsc=`` query parameter,
#:   and the failure reason being exactly ``net::ERR_ABORTED``.  A ``?_rsc=``
#:   request that fails with a connection reset, a 5xx, a DNS failure or a TLS
#:   error is still reported, as is any failure of any URL without ``?_rsc=``.
#:
#: What this profile will NOT hide: uncaught exceptions, failed ``/api/*``
#: calls, broken images, scripts or stylesheets, HTTP error statuses (a
#: different assertion entirely), or any "Failed to fetch" that is not the
#: router's own recovered prefetch.
_NEXTJS_RSC_PREFETCH = NoiseProfile(
    description=(
        "Next.js App Router speculative RSC prefetches that the router "
        "cancels itself and recovers from by falling back to a full "
        "browser navigation."
    ),
    console_patterns=[
        r"^Failed to fetch RSC payload for \S+\. "
        r"Falling back to browser navigation\.",
    ],
    request_patterns=[
        r"\?_rsc=\S* net::ERR_ABORTED$",
    ],
)


#: Every profile a probe spec may name.  Adding one is a deliberate act: it
#: needs the same justification as the entry above — evidence that the noise is
#: emitted by a healthy application, and patterns narrow enough that a real
#: failure of the same shape still gets through.
KNOWN_NOISE_PROFILES: Dict[str, NoiseProfile] = {
    "nextjs_rsc_prefetch": _NEXTJS_RSC_PREFETCH,
}


def resolve_noise_profiles(names: List[str]) -> tuple[List[str], List[str]]:
    """Expand profile names into ``(console_patterns, request_patterns)``.

    Raises
    ------
    KeyError
        If a name is not a known profile.  Callers turn this into a spec
        error: a typo must fail loudly, because the quiet version of this bug
        is a probe that reports success while checking less than its author
        believes it is.
    """
    console: List[str] = []
    requests: List[str] = []
    for name in names:
        profile = KNOWN_NOISE_PROFILES[name]
        console.extend(profile.console_patterns)
        requests.extend(profile.request_patterns)
    return console, requests
