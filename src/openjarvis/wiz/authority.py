"""Authority — what Wiz is *allowed* to do, as distinct from what it can do.

The distinction is the whole point of this module. Wiz has the technical means
to merge a pull request, change production configuration and read a credential
file. Whether it may do those things on behalf of a particular request arriving
on a particular channel is a separate question with a separate answer, and that
answer is decided here by deterministic code rather than by a model.

Three properties are deliberate:

**Deny by default.** An authority that has not been granted is refused. There is
no "unknown channel gets the defaults" path, because the failure mode of such a
path is granting authority to a channel nobody thought about yet.

**Channel ceilings are structural, not configurable.** Every channel has a
hard-coded maximum authority it can *ever* carry, and configuration can only
grant a subset of it. A voice request cannot be given production authority by
editing a config file, because the ceiling is in this source file and the
policy intersects with it on every decision. §14 of the brief requires that
voice never bypass authority policy; a configurable ceiling would make that a
promise rather than a property.

**The policy cannot widen itself.** There is no ``grant()`` method, no
``add_authority()``, nothing that mutates a live policy. A policy is frozen when
it is built. Changing what Wiz may do means changing a file on disk that Wiz's
own protected-path rules forbid it from editing, and restarting. An autonomous
system that can grant itself more autonomy has no authority model at all.

Nothing here replaces the reliability interlocks. A decision that authority is
present means only "this request is permitted to *ask*". The existing gates —
``SafetyPolicy``, ``BoundaryGuard``, the SQL write guard, the merge status
contract — still run and can still refuse. Both must say yes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "Actor",
    "Authority",
    "AuthorityDecision",
    "AuthorityPolicy",
    "CHANNEL_CEILING",
    "Channel",
    "expand",
]


class Authority(str, Enum):
    """What a request is permitted to cause.

    These are levels of *consequence*, not of seniority. They are checked
    individually rather than compared, so there is no "greater than" that could
    silently promote one into another.
    """

    #: Look at things. Read code, read incidents, read status, answer questions.
    READ = "READ"

    #: Act without changing the product or its infrastructure: send a message,
    #: write a note to memory, open a worktree, run a test.
    SAFE_ACTION = "SAFE_ACTION"

    #: Modify source code, but only inside an isolated worktree on a branch.
    CODE_WRITE = "CODE_WRITE"

    #: Push a branch and open a pull request — making the work visible to
    #: GitHub and to CI, but not yet part of the product.
    PR_WRITE = "PR_WRITE"

    #: Change what is actually serving users: merge, deploy, mutate production
    #: data, alter production configuration.
    PRODUCTION_CHANGE = "PRODUCTION_CHANGE"

    #: Read or write credentials. Held apart from everything else because a
    #: system that can read secrets can impersonate every other authority.
    SECRET_ACCESS = "SECRET_ACCESS"


#: Authorities that granting a key authority also confers.
#:
#: Only the harmless direction is modelled. Being allowed to write code implies
#: being allowed to read it, because code cannot be modified unseen. Nothing
#: implies ``PRODUCTION_CHANGE`` and nothing implies ``SECRET_ACCESS``: those
#: two are always granted explicitly and on purpose, so that no future edit to
#: this table can hand them out as a side effect of something that sounded
#: routine.
_IMPLIES: Mapping[Authority, FrozenSet[Authority]] = {
    Authority.SAFE_ACTION: frozenset({Authority.READ}),
    Authority.CODE_WRITE: frozenset({Authority.READ, Authority.SAFE_ACTION}),
    Authority.PR_WRITE: frozenset(
        {Authority.READ, Authority.SAFE_ACTION, Authority.CODE_WRITE}
    ),
    Authority.PRODUCTION_CHANGE: frozenset(),
    Authority.SECRET_ACCESS: frozenset(),
    Authority.READ: frozenset(),
}


def expand(authorities: Iterable[Authority]) -> FrozenSet[Authority]:
    """Return *authorities* together with everything they imply."""
    out: set[Authority] = set()
    for authority in authorities:
        out.add(authority)
        out |= _IMPLIES.get(authority, frozenset())
    return frozenset(out)


class Channel(str, Enum):
    """Where a request arrived from.

    The channel is not a preference: it determines how much confidence there is
    that the operator really said this, and how easily an attacker could have
    said it instead.
    """

    #: The authenticated local dashboard. The operator, at their own machine,
    #: having typed into a form protected by a token and a CSRF check.
    CONTROL_CENTER = "control_center"

    #: A terminal on this machine. Whoever has the shell already has the files.
    CLI = "cli"

    #: Speech, transcribed. Subject to misrecognition, background conversation,
    #: and anything audible near the microphone.
    VOICE = "voice"

    #: A chat message, even from an allowlisted sender. Account takeover and
    #: forwarded text both look exactly like the operator typing.
    TELEGRAM = "telegram"

    #: A task Wiz runs on a timer. Nobody is present to notice a mistake.
    SCHEDULER = "scheduler"

    #: Wiz acting on its own initiative, with no request behind it at all.
    AUTONOMOUS = "autonomous"


#: The maximum authority each channel may *ever* carry.
#:
#: Configuration intersects with this table; it cannot exceed it. The reasoning
#: per channel:
#:
#: * ``CONTROL_CENTER`` is the only place a production change can be authorised,
#:   because it is the only channel that authenticates the operator and can show
#:   them exactly what they are approving before they approve it.
#: * ``CLI`` stops at ``PR_WRITE``. Someone at the shell can of course merge by
#:   hand; what they cannot do is get *Wiz* to do it for them without going
#:   through the dashboard, which is what keeps the audit trail complete.
#: * ``VOICE`` stops at ``CODE_WRITE``. Speech is the least reliable channel
#:   there is, and §14 of the brief is explicit that voice must never be able to
#:   authorise a merge or a production change. A misheard sentence should at
#:   worst produce a branch nobody asked for.
#: * ``TELEGRAM`` stops at ``CODE_WRITE`` for the same reason plus one more: the
#:   text arrives from the network, and untrusted text must never be one clever
#:   sentence away from production.
#: * ``SCHEDULER`` stops at ``PR_WRITE``, and deliberately not further: §33 of
#:   the brief requires that no scheduled task inherit production write
#:   authority. A timer that can deploy is a deploy nobody watched.
#: * ``AUTONOMOUS`` stops at ``PR_WRITE`` here. Production changes made without
#:   a human request are the exclusive business of the reliability subsystem,
#:   which has its own gates, its own verification and its own kill switch, and
#:   which does not consult this module.
#:
#: ``SECRET_ACCESS`` appears in no ceiling at all. Wiz has no capability that
#: requires it, and the way to keep it that way is to make it ungrantable.
CHANNEL_CEILING: Mapping[Channel, FrozenSet[Authority]] = {
    Channel.CONTROL_CENTER: expand(
        {Authority.PR_WRITE, Authority.PRODUCTION_CHANGE}
    ),
    Channel.CLI: expand({Authority.PR_WRITE}),
    Channel.VOICE: expand({Authority.CODE_WRITE}),
    Channel.TELEGRAM: expand({Authority.CODE_WRITE}),
    Channel.SCHEDULER: expand({Authority.PR_WRITE}),
    Channel.AUTONOMOUS: expand({Authority.PR_WRITE}),
}


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is asking, and how sure we are that it is really them."""

    actor_id: str
    channel: Channel

    #: Whether this channel verified the requester's identity for *this*
    #: request. An allowlisted Telegram sender is authenticated; an arbitrary
    #: inbound message is not. Unauthenticated actors get ``READ`` at most, no
    #: matter what the configuration says.
    authenticated: bool = False

    def __post_init__(self) -> None:
        if not self.actor_id:
            raise ValueError("an actor must be identifiable")


@dataclass(frozen=True, slots=True)
class AuthorityDecision:
    """The outcome of an authority check, in a form worth writing down."""

    allowed: bool
    required: Authority
    actor: Actor
    capability: str
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True)
class AuthorityPolicy:
    """Which authorities each channel has been granted.

    Build one with :meth:`load` or by passing ``grants`` directly. Whichever is
    used, the result is frozen and intersected with :data:`CHANNEL_CEILING`, so
    a policy can only ever be narrower than the ceilings in this file.
    """

    grants: Mapping[Channel, FrozenSet[Authority]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        clamped: Dict[Channel, FrozenSet[Authority]] = {}
        for channel, granted in self.grants.items():
            ceiling = CHANNEL_CEILING.get(channel, frozenset())
            allowed = expand(granted) & ceiling
            refused = expand(granted) - ceiling
            if refused:
                # Loud, because a config asking for something impossible means
                # somebody believes Wiz can do a thing it cannot.
                logger.warning(
                    "authority %s cannot be granted to channel '%s'; "
                    "the ceiling for that channel forbids it",
                    sorted(a.value for a in refused),
                    channel.value,
                )
            clamped[channel] = allowed
        object.__setattr__(self, "grants", clamped)

    # -- decisions ---------------------------------------------------------

    def decide(self, actor: Actor, required: Authority, *, capability: str = "") -> AuthorityDecision:
        """Whether *actor* may exercise *required*.

        Deny-by-default: every path that is not an explicit grant returns a
        refusal with a reason the operator can read.
        """

        def refuse(reason: str) -> AuthorityDecision:
            return AuthorityDecision(
                allowed=False,
                required=required,
                actor=actor,
                capability=capability,
                reason=reason,
            )

        ceiling = CHANNEL_CEILING.get(actor.channel)
        if ceiling is None:
            return refuse(
                f"channel '{actor.channel}' has no ceiling defined, so it has no authority"
            )

        if required not in ceiling:
            return refuse(
                f"{required.value} can never be exercised from {actor.channel.value}"
            )

        if not actor.authenticated and required is not Authority.READ:
            return refuse(
                f"{actor.channel.value} did not authenticate this request, "
                "so it is limited to READ"
            )

        granted = self.grants.get(actor.channel, frozenset())
        if required not in granted:
            return refuse(
                f"{required.value} is not granted to {actor.channel.value}"
            )

        return AuthorityDecision(
            allowed=True,
            required=required,
            actor=actor,
            capability=capability,
            reason=f"{required.value} is granted to {actor.channel.value}",
        )

    def granted_to(self, channel: Channel) -> FrozenSet[Authority]:
        """What *channel* actually holds, after clamping."""
        return self.grants.get(channel, frozenset())

    # -- construction ------------------------------------------------------

    @classmethod
    def default(cls) -> "AuthorityPolicy":
        """The policy Wiz starts with when nothing has been configured.

        Read everywhere, safe actions from the channels that identify their
        requester, and *no* code, PR or production authority anywhere. Enabling
        Wiz to write code is a decision the operator makes on purpose; it is not
        what happens when they forget to write a config file.
        """
        return cls(
            grants={
                Channel.CONTROL_CENTER: frozenset({Authority.SAFE_ACTION}),
                Channel.CLI: frozenset({Authority.SAFE_ACTION}),
                Channel.VOICE: frozenset({Authority.READ}),
                Channel.TELEGRAM: frozenset({Authority.READ}),
                Channel.SCHEDULER: frozenset({Authority.READ}),
                Channel.AUTONOMOUS: frozenset({Authority.READ}),
            }
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Iterable[str]]) -> "AuthorityPolicy":
        """Build from ``{channel_name: [authority_name, ...]}``.

        Unknown names are refused rather than ignored. A typo in an authority
        file must not quietly become "no authority" *or* quietly become
        something else; it must be visible.
        """
        grants: Dict[Channel, FrozenSet[Authority]] = {}
        for channel_name, authority_names in raw.items():
            try:
                channel = Channel(channel_name)
            except ValueError as exc:
                raise ValueError(f"unknown channel '{channel_name}'") from exc
            resolved = set()
            for name in authority_names:
                try:
                    resolved.add(Authority(name))
                except ValueError as exc:
                    raise ValueError(
                        f"unknown authority '{name}' for channel '{channel_name}'"
                    ) from exc
            grants[channel] = frozenset(resolved)
        return cls(grants=grants)

    @classmethod
    def load(cls, path: str | Path) -> "AuthorityPolicy":
        """Read a policy from JSON, falling back to :meth:`default`.

        A missing file means "not configured yet", which is the default policy.
        An *unreadable or malformed* file is different: it means the operator
        intended something that cannot be read, and guessing would be worse than
        being restrictive. Both end at the default, but the second logs an
        error, because silently running with less authority than intended is a
        bug the operator needs to hear about.
        """
        target = Path(path)
        if not target.exists():
            return cls.default()
        try:
            raw = json.loads(target.read_text())
            return cls.from_mapping(raw.get("grants", {}))
        except (OSError, ValueError) as exc:
            logger.error(
                "authority policy at %s could not be read (%s); "
                "falling back to the default, which grants no write authority",
                target,
                exc,
            )
            return cls.default()

    def to_mapping(self) -> Dict[str, list]:
        """A JSON-safe view, for the dashboard and for tests."""
        return {
            channel.value: sorted(a.value for a in authorities)
            for channel, authorities in sorted(
                self.grants.items(), key=lambda kv: kv[0].value
            )
        }


def ceiling_for(channel: Channel) -> FrozenSet[Authority]:
    """The structural maximum for *channel*, ignoring configuration."""
    return CHANNEL_CEILING.get(channel, frozenset())
