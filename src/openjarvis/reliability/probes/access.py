"""Named access profiles — how a probe gets past a deployment's front door.

Some deployments are deliberately not public.  Vercel puts every preview behind
its own SSO wall, so an unauthenticated request gets a 302 to
``vercel.com/sso-api`` and a probe ends up asserting against a login page
instead of the application.  Verifying a repair against such a preview needs a
credential, and that credential has three properties which together decide the
whole design:

* it is a **secret**, so it must never reach a spec file, a URL, an evidence
  artifact, a screenshot or a notification;
* it is only meaningful for **some targets** — sending a preview bypass to
  production is pointless at best;
* it must not become a **requirement**, or every production probe that could
  conceivably be pointed at a preview would start failing when the secret is
  absent, which is most of the time.

Hence a profile rather than a per-probe header mapping.  A probe declares *what
kind of place* it may be pointed at::

    [probe]
    access_profiles = ["vercel_preview"]

and the runner decides, per run, whether the profile applies to the URL actually
under test.  Against production the profile is inert and the secret is never
read; against a preview the header is sent and its value is registered with the
credential redactor.  The same probe therefore works in both places without
duplicating the header mapping into every spec, and without a spec author
having to remember which environment variable Vercel wants this month.

Contrast with the query-parameter form Vercel also accepts: that would need no
code at all, and it would put a shared secret into probe specs, captured URLs,
screenshots, traces and Telegram messages.  Headers are the only form that can
be kept out of the evidence.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

__all__ = [
    "KNOWN_ACCESS_PROFILES",
    "AccessProfile",
    "MissingAccessSecretError",
    "resolve_access_headers",
]


class MissingAccessSecretError(RuntimeError):
    """Raised when a target needs an access secret that is not set.

    Deliberately loud.  The quiet alternative is a probe that runs anyway,
    receives a login page, and reports the application as broken — sending
    somebody to debug an outage that is really a missing environment variable.
    """


@dataclass(frozen=True, slots=True)
class AccessProfile:
    """Credentials for reaching one class of protected deployment.

    Attributes
    ----------
    description:
        What this gets you past, for ``probe show`` and diagnostics.
    headers_from_env:
        Header name -> **name of an environment variable** holding its value.
        Never a value: this module is imported into a spec-rendering path.
    host_pattern:
        Regex matched against the target's hostname.  The profile is inert for
        any host that does not match, which is what keeps production runs
        unchanged.
    """

    description: str
    headers_from_env: Dict[str, str] = field(default_factory=dict)
    host_pattern: str = ""

    def applies_to(self, target_url: str) -> bool:
        """Whether this profile should be used for *target_url*."""
        if not self.host_pattern:
            return True
        host = (urlparse(target_url).hostname or "").lower()
        return bool(host) and bool(re.search(self.host_pattern, host))


#: Vercel Deployment Protection on preview deployments.
#:
#: Scoped to ``*.vercel.app`` because that is where previews live; a custom
#: production domain never matches, so a probe carrying this profile behaves
#: exactly as before when it runs against production. The secret is Vercel's
#: "Protection Bypass for Automation", created in project settings; it is read
#: from the environment at run time and never stored.
_VERCEL_PREVIEW = AccessProfile(
    description=(
        "Vercel preview deployments behind Vercel Authentication. Sends the "
        "Protection Bypass for Automation secret as x-vercel-protection-bypass."
    ),
    headers_from_env={
        "x-vercel-protection-bypass": "VERCEL_AUTOMATION_BYPASS_SECRET",
    },
    host_pattern=r"\.vercel\.app$",
)


#: Every profile a probe spec may name.
KNOWN_ACCESS_PROFILES: Dict[str, AccessProfile] = {
    "vercel_preview": _VERCEL_PREVIEW,
}


def resolve_access_headers(
    names: List[str], target_url: str
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Return ``(headers, secret_values)`` for *names* against *target_url*.

    Profiles that do not apply to this target contribute nothing and their
    environment variables are not even read — so pointing a preview-capable
    probe at production neither requires the secret nor touches it.

    Raises
    ------
    KeyError
        If a name is not a known profile.  Callers turn this into a spec error.
    MissingAccessSecretError
        If a profile *does* apply and its variable is unset.  Naming the
        variable, never a value.
    """
    headers: Dict[str, str] = {}
    secrets: Dict[str, str] = {}
    for name in names:
        profile = KNOWN_ACCESS_PROFILES[name]
        if not profile.applies_to(target_url):
            logger.debug(
                "access profile %r does not apply to %s; not reading its secrets",
                name,
                target_url,
            )
            continue
        missing: List[str] = []
        for header_name, env_name in profile.headers_from_env.items():
            value = os.environ.get(env_name, "")
            if not value:
                missing.append(f"{header_name} (${env_name})")
                continue
            headers[header_name] = value
            secrets[header_name] = value
        if missing:
            raise MissingAccessSecretError(
                f"{target_url} needs the '{name}' access profile "
                f"({profile.description}) but these are not set: " + ", ".join(missing)
            )
    return headers, secrets
