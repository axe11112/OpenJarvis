"""One underlying problem, however many probes noticed it.

An incident is what a *probe* saw. An outage is what *went wrong*. Those are
different objects, and conflating them is why an owner gets five messages when
one deployment breaks: the homepage probe opens one incident, the login probe
opens another, sign-up a third, and each carries its own fingerprint, its own
ledger entry and its own escalation.

This module introduces the second object. Every incident is assigned an
**owner-facing outage identity** — a key that answers "which problem is this?"
rather than "which check noticed?". Deduplication, escalation and success
messages all key on that identity, so five failing probes produce one message
and the five incidents survive intact underneath it.

Correlation is deliberately conservative, because the failure mode of getting
this wrong is the one thing worse than noise: hiding a real, separate problem
inside a group the owner has already been told about. Three rules keep it
honest.

**Families never merge.** A failing database is not a failing website, an
auth *security* failure is not an availability failure, and an external
provider having an outage is not either of them. The family is derived from
what the incident says about itself — its source, its component and the kind of
failure observed — and two incidents in different families are never the same
outage no matter how closely they coincide.

**Only availability groups across components.** "The site is not answering" is
a claim about the site, and several probes making it at once are corroborating
one fact. Everything else — a wrong assertion on a page that loaded, a 401, a
console error — is a claim about one component, and groups only with itself.
The exception, and it is evidence-based rather than a hunch, is when a shared
failing production deployment has been established for both: a deployment is a
single artifact, so two components failing on the same broken artifact are one
problem whatever shape their failures take.

**Time is a constraint, not a signal.** Coincidence alone never groups
anything. Two incidents that pass every other rule still have to overlap; two
that overlap but fail any other rule stay separate.

The result is recorded, not inferred at read time: the registry persists to
disk beside the incident database, so the outage an incident belongs to
survives a watcher restart, a sleeping laptop, and the new incident IDs that a
flapping check produces every few minutes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from openjarvis.reliability.statefile import write_json_atomic
from openjarvis.reliability.types import Incident, Severity, now_iso

logger = logging.getLogger(__name__)

__all__ = [
    "GROUPING_FAMILIES",
    "Outage",
    "OutageRegistry",
    "classify_family",
    "deployment_identity",
    "failure_shape",
    "outage_key",
    "outages_path",
]


# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------

#: The web surface: pages and flows served by one deployment of one site.
#:
#: Membership is by component name because that is what a probe declares.
#: Anything unrecognised stays out — an unknown component becomes its own
#: family, which costs an extra message and never hides a separate problem.
_WEB_SURFACE = (
    "website",
    "site",
    "frontend",
    "landing",
    "homepage",
    "home",
    "marketing",
    "web",
    "app",
    "dashboard",
    "login",
    "auth",
    "authentication",
    "signin",
    "sign-in",
    "signup",
    "sign-up",
    "register",
    "onboarding",
    "checkout",
    "billing",
    "account",
    "profile",
    "api",
    "backend",
    "deployment",
    "vercel",
)

#: Components that are their own thing, and must never be folded into the site.
_DATABASE = ("database", "supabase", "postgres", "db", "sql")
_PROVIDER = (
    "stripe",
    "provider",
    "upstream",
    "third-party",
    "thirdparty",
    "external",
    "openai",
    "anthropic",
    "sendgrid",
    "resend",
    "twilio",
)
_CI = ("ci", "actions", "github", "workflow", "build", "pipeline")

#: Failure kinds that mean "the thing did not serve". Several probes reporting
#: one of these at the same moment are describing one outage.
_AVAILABILITY_KINDS = frozenset(
    {
        "timeout",
        "navigation",
        "network_failure",
        "network",
        "unreachable",
        "dns",
        "tls",
        "connection",
        "server_error",
    }
)

#: Failure kinds that mean "it served, and the content was wrong". A claim
#: about one component until a shared deployment says otherwise.
_CONTRACT_KINDS = frozenset(
    {"assertion", "console_error", "content", "selector", "regex", "schema"}
)

#: Words in a title or summary that mark a failure as security-relevant.
#: A security failure is never grouped with an availability outage: "the site
#: is down" and "the site let somebody in who should not be" ask completely
#: different things of the owner, and burying the second inside the first is
#: exactly the mistake this module must not make.
_SECURITY_MARKERS = (
    "unauthorized",
    "unauthenticated",
    "authorization bypass",
    "auth bypass",
    "bypass",
    "privilege",
    "escalation",
    "leaked",
    "leak",
    "exposed",
    "credential",
    "token",
    "session fixation",
    "csrf",
    "xss",
    "sql injection",
    "injection",
    "rls",
    "row level security",
    "permission",
    "forbidden access",
    "data exposure",
)

#: Families whose members may span several components. Only one, and the
#: reason is in the module docstring: availability is a claim about the site,
#: and everything else is a claim about a component.
GROUPING_FAMILIES = frozenset({"site_availability"})

#: An incident joins an existing outage only if it is observed within this
#: long of the outage's most recent activity. Fifteen minutes is roughly three
#: watcher cycles: long enough that probes on different schedules corroborate,
#: short enough that a fresh break tomorrow is a fresh outage.
DEFAULT_JOIN_WINDOW = timedelta(minutes=15)

#: How long an outage with no new activity stays in the registry. Keeps the
#: file small without ever expiring one that is still being talked about.
DEFAULT_RETENTION = timedelta(days=7)


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _matches(haystack: str, needles: Sequence[str]) -> bool:
    return any(needle and needle in haystack for needle in needles)


def _is_security(incident: Incident) -> bool:
    """Whether this failure is about security rather than availability."""
    metadata = getattr(incident, "metadata", None) or {}
    declared = _lower(metadata.get("failure_class"))
    if declared in ("security", "auth_security"):
        return True
    kind = _lower(metadata.get("failure_kind"))
    if kind in ("security", "auth_bypass", "unauthorized_access", "injection"):
        return True
    text = f"{_lower(incident.title)} {_lower(incident.summary)}"
    return _matches(text, _SECURITY_MARKERS)


def failure_shape(incident: Incident) -> str:
    """``"availability"``, ``"contract"`` or ``"other"``.

    The distinction decides whether an incident may group with a *different*
    component. "Did not serve" is a claim about the site; "served the wrong
    thing" is a claim about one page.

    An HTTP status is read where one was recorded: a 5xx or a connection
    failure is unavailability, a 4xx is the endpoint answering — which is a
    contract or an auth question, not an outage of the site.
    """
    metadata = getattr(incident, "metadata", None) or {}
    kind = _lower(metadata.get("failure_kind"))
    status = 0
    try:
        status = int(metadata.get("http_status") or 0)
    except (TypeError, ValueError):
        status = 0

    if kind == "http_error" or status:
        if status >= 500 or status == 0:
            return "availability"
        return "contract"
    if kind == "duration":
        # A budget overrun is the observer's most ambiguous signal. It is
        # handled by MonitorHealth before it ever gets here; for grouping it
        # counts as availability, because a site that is too slow to load is
        # down as far as a user is concerned.
        return "availability"
    if kind in _AVAILABILITY_KINDS:
        return "availability"
    if kind in _CONTRACT_KINDS:
        return "contract"
    return "other"


def classify_family(incident: Incident) -> str:
    """Which family of problem this incident belongs to.

    Families are the outer boundary of correlation: two incidents in different
    families are never the same outage. Returning an unrecognised component's
    own name is deliberate — an unknown thing gets its own family, and an extra
    message, rather than being folded into the site because nothing else fit.
    """
    if _is_security(incident):
        return "auth_security"

    source = _lower(getattr(incident, "source", ""))
    component = _lower(getattr(incident, "component", ""))

    if source == "supabase" or _matches(component, _DATABASE):
        return "database"
    if _matches(component, _PROVIDER):
        return "external_provider"
    if source == "github" or _matches(component, _CI):
        return "ci"
    if source == "vercel" or _matches(component, _WEB_SURFACE):
        if failure_shape(incident) == "availability":
            return "site_availability"
        # A page that served the wrong thing is a claim about that page. The
        # family is shared so that a *shared deployment* can still merge two of
        # them — see ``_may_join_locked``, where a family outside
        # :data:`GROUPING_FAMILIES` needs either the same component or one
        # established artifact before it will span two.
        return "site_contract"
    # An unrecognised component keeps its name in the family, so it can never
    # be merged with anything on any evidence. An extra message is the right
    # answer to "I do not know what this is".
    return f"component:{component or 'unknown'}"


def deployment_identity(incident: Incident) -> str:
    """The production artifact this incident is about, when one is known.

    Read from the correlation the analysis stage recorded, then from metadata
    the sources fill in. Returns ``""`` when nothing established it, which is
    the common case and is treated as *unknown* rather than as *matching*.
    """
    correlation = getattr(incident, "correlation", None)
    for value in (
        getattr(correlation, "deployment_id", ""),
        getattr(correlation, "commit_sha", ""),
    ):
        text = str(value or "").strip()
        if text:
            return text
    metadata = getattr(incident, "metadata", None) or {}
    for key in ("deployment_id", "deployment_sha", "production_sha", "commit_sha"):
        text = str(metadata.get(key) or "").strip()
        if text:
            return text
    return ""


def _same_deployment(left: str, right: str) -> Optional[bool]:
    """``True``/``False`` when both are known, ``None`` when one is not.

    SHAs get compared on their first seven characters, because a deployment id
    and an abbreviated SHA for the same artifact are written differently in
    different places and refusing to match them would defeat the strongest
    piece of evidence available.
    """
    if not left or not right:
        return None
    a, b = left.lower(), right.lower()
    if a == b:
        return True
    if re.fullmatch(r"[0-9a-f]{7,40}", a) and re.fullmatch(r"[0-9a-f]{7,40}", b):
        return a[:7] == b[:7]
    return False


def _parse(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def outage_key(
    *, family: str, environment: str, deployment: str, opened_at: str
) -> str:
    """A stable, opaque identifier for one underlying problem.

    Includes ``opened_at`` so that the same family breaking again next week is
    a new outage rather than a resurrection of the one the owner was told about
    — the registry, not the key, is what makes an *ongoing* outage stable.
    """
    parts = [family, environment, deployment.lower(), opened_at]
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"otg_{digest[:12]}"


# ---------------------------------------------------------------------------
# The outage record
# ---------------------------------------------------------------------------


@dataclass
class Outage:
    """One underlying problem, and every incident that is evidence of it."""

    key: str
    family: str
    environment: str = "production"
    deployment: str = ""
    opened_at: str = field(default_factory=now_iso)
    last_seen_at: str = field(default_factory=now_iso)
    resolved_at: str = ""
    severity: Severity = Severity.MEDIUM
    incident_ids: List[str] = field(default_factory=list)
    fingerprints: List[str] = field(default_factory=list)
    components: List[str] = field(default_factory=list)
    probes: List[str] = field(default_factory=list)
    #: Human-readable reasons this incident joined, kept so the Control Center
    #: can show why five probes became one message.
    notes: List[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        """Whether this outage has been recorded as over."""
        return bool(self.resolved_at)

    @property
    def grouped(self) -> bool:
        """Whether more than one probe or component is implicated."""
        return len(self.probes) > 1 or len(self.components) > 1

    def subjects(self) -> List[str]:
        """The components involved, in the order they were first seen."""
        return list(self.components)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "key": self.key,
            "family": self.family,
            "environment": self.environment,
            "deployment": self.deployment,
            "opened_at": self.opened_at,
            "last_seen_at": self.last_seen_at,
            "resolved_at": self.resolved_at,
            "severity": self.severity.value,
            "incident_ids": list(self.incident_ids),
            "fingerprints": list(self.fingerprints),
            "components": list(self.components),
            "probes": list(self.probes),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Outage":
        """Deserialize from a plain dict."""
        return cls(
            key=str(d.get("key") or ""),
            family=str(d.get("family") or ""),
            environment=str(d.get("environment") or "production"),
            deployment=str(d.get("deployment") or ""),
            opened_at=str(d.get("opened_at") or now_iso()),
            last_seen_at=str(d.get("last_seen_at") or now_iso()),
            resolved_at=str(d.get("resolved_at") or ""),
            severity=Severity.parse(d.get("severity") or Severity.MEDIUM),
            incident_ids=[str(i) for i in (d.get("incident_ids") or [])],
            fingerprints=[str(i) for i in (d.get("fingerprints") or [])],
            components=[str(i) for i in (d.get("components") or [])],
            probes=[str(i) for i in (d.get("probes") or [])],
            notes=[str(i) for i in (d.get("notes") or [])],
        )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def outages_path(config: Any) -> Path:
    """Where the outage registry lives.

    Beside the incident database and the notification ledger, so the watcher
    and the Control Center cannot disagree about which problems are the same
    problem.
    """
    from openjarvis.core.paths import get_config_dir

    configured = getattr(getattr(config, "reliability", None), "db_path", "")
    if configured:
        return Path(configured).expanduser().parent / "outages.json"
    return get_config_dir() / "reliability" / "outages.json"


@dataclass
class OutageRegistry:
    """Assigns incidents to outages, and remembers the assignment.

    Parameters
    ----------
    path:
        Where to persist. ``None`` keeps it in memory, which is right for tests
        and wrong in production: the whole point is that a restart does not
        re-split one outage into five.
    join_window:
        How recently an outage must have been active for a new incident to join
        it.
    retention:
        How long a quiet outage stays on disk.
    """

    path: Optional[Path] = None
    join_window: timedelta = DEFAULT_JOIN_WINDOW
    retention: timedelta = DEFAULT_RETENTION
    _outages: Dict[str, Outage] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _stamp: tuple = field(default=(0, 0.0), repr=False)

    def __post_init__(self) -> None:
        self._load()

    def _refresh_locked(self) -> None:
        """Re-read when another process has written to the registry.

        The watcher assigns outages; the Control Center reads them; a CLI
        command may do either. Three processes, one file — a stale snapshot
        here would re-split a group that another process had already merged.
        """
        if self.path is None:
            return
        if self._current_stamp() != self._stamp:
            self._load()

    def _current_stamp(self) -> tuple:
        if self.path is None:
            return (0, 0.0)
        try:
            stat = self.path.stat()
        except OSError:
            return (0, 0.0)
        return (stat.st_size, stat.st_mtime)

    # -- the question ------------------------------------------------------

    def assign(self, incident: Incident) -> Outage:
        """Return the outage *incident* belongs to, creating one if needed.

        Idempotent: an incident already assigned returns its existing outage
        without widening anything, so replaying a watcher cycle cannot inflate
        a group.
        """
        with self._lock:
            self._refresh_locked()
            existing = self._find_assigned_locked(incident)
            if existing is not None:
                self._touch_locked(existing, incident)
                self._save_locked()
                return existing

            family = classify_family(incident)
            candidate = self._best_candidate_locked(incident, family)
            if candidate is not None:
                self._join_locked(candidate, incident, family)
                self._save_locked()
                return candidate

            outage = Outage(
                key=outage_key(
                    family=family,
                    environment=str(incident.environment or "production"),
                    deployment=deployment_identity(incident),
                    opened_at=str(incident.created_at or now_iso()),
                ),
                family=family,
                environment=str(incident.environment or "production"),
                deployment=deployment_identity(incident),
                opened_at=str(incident.created_at or now_iso()),
                last_seen_at=str(
                    incident.last_seen_at or incident.created_at or now_iso()
                ),
                severity=incident.severity,
            )
            self._add_member_locked(outage, incident)
            outage.notes.append(
                f"{incident.id or incident.fingerprint} opened this outage ({family})"
            )
            self._outages[outage.key] = outage
            self._prune_locked()
            self._save_locked()
            logger.info("outage %s opened for %s (%s)", outage.key, incident.id, family)
            return outage

    def key_for(self, incident: Incident) -> str:
        """The outage key for *incident*, assigning one if necessary."""
        return self.assign(incident).key

    def get(self, key: str) -> Optional[Outage]:
        """The outage with *key*, or ``None``."""
        with self._lock:
            self._refresh_locked()
            outage = self._outages.get(key)
            return Outage.from_dict(outage.to_dict()) if outage else None

    def for_incident(self, incident: Incident) -> Optional[Outage]:
        """The outage *incident* is already assigned to, without creating one."""
        with self._lock:
            self._refresh_locked()
            found = self._find_assigned_locked(incident)
            return Outage.from_dict(found.to_dict()) if found else None

    def open_outages(self) -> List[Outage]:
        """Every outage not yet recorded as over, newest first."""
        with self._lock:
            self._refresh_locked()
            live = [o for o in self._outages.values() if not o.resolved]
        return sorted(live, key=lambda o: o.last_seen_at, reverse=True)

    def all_outages(self) -> List[Outage]:
        """Every outage still retained, newest first."""
        with self._lock:
            self._refresh_locked()
            everything = list(self._outages.values())
        return sorted(everything, key=lambda o: o.last_seen_at, reverse=True)

    # -- changes -----------------------------------------------------------

    def resolve(self, key: str, *, at: str = "") -> Optional[Outage]:
        """Record that an outage is over.

        The record stays: a resolved outage is what stops a success message
        being sent twice, and what makes the same family breaking again
        tomorrow a genuinely new outage.
        """
        with self._lock:
            outage = self._outages.get(key)
            if outage is None:
                return None
            if not outage.resolved:
                outage.resolved_at = at or now_iso()
                outage.notes.append("resolved")
                self._save_locked()
            return Outage.from_dict(outage.to_dict())

    def reopen(self, key: str) -> Optional[Outage]:
        """Undo a resolution, for an outage that came back inside the window."""
        with self._lock:
            outage = self._outages.get(key)
            if outage is None:
                return None
            outage.resolved_at = ""
            outage.last_seen_at = now_iso()
            self._save_locked()
            return Outage.from_dict(outage.to_dict())

    def acknowledge(self, key: str, *, note: str = "") -> Optional[Outage]:
        """Record that the owner has answered an escalation about this outage.

        Shown in Control Center so an operator can see that "Fix it" arrived
        and what it was taken to mean. It grants nothing on its own — see
        :class:`~openjarvis.reliability.owner_commands.OwnerCommands` for the
        one thing that message is allowed to change.
        """
        with self._lock:
            outage = self._outages.get(key)
            if outage is None:
                return None
            outage.notes.append(
                note or "the owner acknowledged this and asked me to continue"
            )
            self._save_locked()
            return Outage.from_dict(outage.to_dict())

    def forget(self, key: str) -> None:
        """Drop an outage entirely. For tests and for operator cleanup."""
        with self._lock:
            self._outages.pop(key, None)
            self._save_locked()

    # -- internals ---------------------------------------------------------

    def _find_assigned_locked(self, incident: Incident) -> Optional[Outage]:
        recorded = str(
            (getattr(incident, "metadata", None) or {}).get("outage_key") or ""
        )
        if recorded and recorded in self._outages:
            return self._outages[recorded]
        identity = incident.id or incident.fingerprint
        for outage in self._outages.values():
            if identity and identity in outage.incident_ids:
                return outage
            if incident.fingerprint and incident.fingerprint in outage.fingerprints:
                return outage
        return None

    def _best_candidate_locked(
        self, incident: Incident, family: str
    ) -> Optional[Outage]:
        """The open outage this incident should join, if any."""
        best: Optional[Outage] = None
        best_at: Optional[datetime] = None
        for outage in self._outages.values():
            if not self._may_join_locked(outage, incident, family):
                continue
            seen = _parse(outage.last_seen_at)
            if best is None or (
                seen is not None and (best_at is None or seen > best_at)
            ):
                best, best_at = outage, seen
        return best

    def _may_join_locked(self, outage: Outage, incident: Incident, family: str) -> bool:
        """Every rule that has to hold before two things are one problem."""
        if outage.resolved:
            return False
        if outage.family != family:
            return False
        if outage.environment != str(incident.environment or "production"):
            return False

        deployment = deployment_identity(incident)
        same_deployment = _same_deployment(outage.deployment, deployment)
        if same_deployment is False:
            # Two different production artifacts are two different problems,
            # whatever else they have in common.
            return False

        observed = _parse(incident.created_at) or _parse(incident.last_seen_at)
        active = _parse(outage.last_seen_at)
        if observed is None or active is None:
            return False
        if abs((observed - active).total_seconds()) > self.join_window.total_seconds():
            return False

        component = _lower(incident.component)
        if component and component in [_lower(c) for c in outage.components]:
            # Same component, same family, overlapping: the same problem seen
            # again. Always allowed regardless of grouping rules.
            return True

        if family not in GROUPING_FAMILIES and same_deployment is not True:
            # Non-grouping families never span components on coincidence
            # alone. A shared, established deployment is the one exception,
            # and it is evidence rather than proximity.
            return False

        return True

    def _join_locked(self, outage: Outage, incident: Incident, family: str) -> None:
        before = len(outage.components)
        self._add_member_locked(outage, incident)
        deployment = deployment_identity(incident)
        if not outage.deployment and deployment:
            outage.deployment = deployment
        if len(outage.components) > before:
            outage.notes.append(
                f"{incident.id or incident.fingerprint} "
                f"({incident.component or 'unknown'}) joined: same {family}"
                + (f" on deployment {outage.deployment}" if outage.deployment else "")
            )
        logger.info(
            "outage %s: %s (%s) is the same problem",
            outage.key,
            incident.id,
            incident.component,
        )

    def _add_member_locked(self, outage: Outage, incident: Incident) -> None:
        identity = incident.id or incident.fingerprint
        if identity and identity not in outage.incident_ids:
            outage.incident_ids.append(identity)
        if incident.fingerprint and incident.fingerprint not in outage.fingerprints:
            outage.fingerprints.append(incident.fingerprint)
        component = str(incident.component or "").strip()
        if component and component not in outage.components:
            outage.components.append(component)
        probe = str(incident.probe_id or "").strip()
        if probe and probe not in outage.probes:
            outage.probes.append(probe)
        self._touch_locked(outage, incident)

    def _touch_locked(self, outage: Outage, incident: Incident) -> None:
        seen = str(incident.last_seen_at or incident.updated_at or now_iso())
        current = _parse(outage.last_seen_at)
        observed = _parse(seen)
        if observed is not None and (current is None or observed > current):
            outage.last_seen_at = seen
        if incident.severity.rank > outage.severity.rank:
            outage.severity = incident.severity
        # An incident arriving is activity, so a resolved outage that is being
        # written to again is a resolved outage that came back.
        if outage.resolved and incident.is_open:
            outage.resolved_at = ""
            outage.notes.append("reopened: the problem is being observed again")

    def _prune_locked(self) -> None:
        cutoff = datetime.now(timezone.utc) - self.retention
        for key, outage in list(self._outages.items()):
            seen = _parse(outage.last_seen_at)
            if seen is not None and seen < cutoff:
                self._outages.pop(key, None)

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries = raw.get("outages", raw) if isinstance(raw, dict) else {}
            self._outages = {
                str(key): Outage.from_dict(value)
                for key, value in (entries or {}).items()
                if isinstance(value, dict)
            }
        except Exception:  # noqa: BLE001 - a corrupt registry costs a duplicate
            logger.warning("could not read the outage registry at %s", self.path)
            self._outages = {}  # ...never a crash, and never a hidden outage.
        self._stamp = self._current_stamp()

    def _save_locked(self) -> None:
        if self.path is None:
            return
        try:
            write_json_atomic(
                self.path,
                {"outages": {k: v.to_dict() for k, v in self._outages.items()}},
            )
            self._stamp = self._current_stamp()
        except Exception:  # noqa: BLE001
            logger.exception("could not write the outage registry at %s", self.path)


def group_incidents(
    incidents: Iterable[Incident], *, registry: Optional[OutageRegistry] = None
) -> Tuple[OutageRegistry, Dict[str, List[Incident]]]:
    """Assign every incident in *incidents* to an outage.

    Returns the registry used and a mapping of outage key to the incidents in
    it, in the order they were supplied. Exists for the replay tool and the
    Control Center, both of which want the whole picture at once.
    """
    registry = registry or OutageRegistry()
    grouped: Dict[str, List[Incident]] = {}
    for incident in sorted(incidents, key=lambda i: str(i.created_at or "")):
        outage = registry.assign(incident)
        grouped.setdefault(outage.key, []).append(incident)
    return registry, grouped
