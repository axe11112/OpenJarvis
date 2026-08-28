"""Assembling a working Wiz: the capabilities it declares and the code behind them.

This module is the inventory. Every ability Wiz has appears here as a
:class:`~openjarvis.wiz.capabilities.CapabilitySpec` with its authority and risk
written down next to it, and as a handler registered against that name. Reading
this file tells you the complete set of things Wiz can be asked to do, which is
the property that makes the authority model reviewable.

Phase A registers only questions. Nothing here writes a file, opens a branch or
touches production — the whole vocabulary is read-only by construction, so the
dispatch path, the authority checks and the journal can run against real
requests before any verb exists that could do damage if the wiring were wrong.

Availability is probed rather than assumed, because the brief forbids claiming a
capability that is not configured. ``reliability.status`` reports itself
unavailable on a machine with no incident database instead of answering
confidently about a system it cannot see.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from openjarvis.wiz.authority import (
    CHANNEL_CEILING,
    Actor,
    Authority,
    AuthorityPolicy,
    Channel,
)
from openjarvis.wiz.brain import Request, Wiz
from openjarvis.wiz.capabilities import (
    Availability,
    CapabilityRegistry,
    CapabilitySpec,
    Risk,
)
from openjarvis.wiz.intents import RuleClassifier, default_rules
from openjarvis.wiz.journal import WizJournal

logger = logging.getLogger(__name__)

__all__ = [
    "AUTHORITY_FILENAME",
    "JOURNAL_FILENAME",
    "WizRuntime",
    "build_wiz",
    "wiz_home",
]

#: Where Wiz keeps its own state, alongside the reliability subsystem's rather
#: than inside it.
WIZ_DIRNAME = "wiz"
AUTHORITY_FILENAME = "authority.json"
JOURNAL_FILENAME = "journal.jsonl"


def wiz_home() -> Path:
    """``~/.openjarvis/wiz``, or wherever the config dir has been moved to."""
    from openjarvis.core.paths import get_config_dir

    return Path(get_config_dir()) / WIZ_DIRNAME


def _incident_db_path(config: Any = None) -> Optional[Path]:
    """Where the reliability incident database lives, if it does."""
    try:
        from openjarvis.core.paths import get_config_dir

        if config is not None:
            configured = getattr(getattr(config, "reliability", None), "db_path", "")
            if configured:
                return Path(configured).expanduser()
        return Path(get_config_dir()) / "reliability" / "incidents.db"
    except Exception:  # pragma: no cover - defensive
        return None


# ---------------------------------------------------------------------------
# Availability probes — asked of the machine, not of a config flag
# ---------------------------------------------------------------------------


def _always_ready() -> Availability:
    return Availability.ready()


def _incident_store_available(config: Any = None) -> Availability:
    path = _incident_db_path(config)
    if path is None:
        return Availability.missing("I cannot work out where incidents are stored")
    if not path.exists():
        return Availability.missing(
            "there is no incident database yet, so I have nothing to report on"
        )
    return Availability.ready(str(path))


def incident_probe_for(
    store_factory: Optional[Callable[[], Any]],
) -> Callable[[], Availability]:
    """An availability probe that agrees with the store the handler will use.

    The probe and the handler must consult the same source. When they do not,
    "I have no incident database" and "here are your incidents" can both be
    true in one process — which is how the Phase A tests came to pass only on a
    machine that happened to have a live database at the default path, while
    injecting a store that had nothing to do with it.

    So an injected factory *is* the answer: if it produces a store, the
    capability is available; if it raises, it is not. When nothing is injected
    the default path check stands, because there is no factory to ask yet.
    """
    if store_factory is None:
        return lambda: _incident_store_available(None)

    def probe() -> Availability:
        try:
            store = store_factory()
        except FileNotFoundError:
            # The ordinary case, and the one an exception message is worst at
            # explaining: there simply is no database yet. Its ``str()`` is
            # whatever the raiser happened to pass — sometimes a path,
            # sometimes a word — and it reached the operator's phone verbatim
            # as "I cannot do that here: none."
            return Availability.missing(
                "there is no incident database yet, so I have nothing to report on"
            )
        except Exception as exc:
            detail = str(exc).strip()
            return Availability.missing(
                f"the incident store cannot be opened ({detail})"
                if detail
                else "the incident store cannot be opened"
            )
        WizRuntime._close(store)
        return Availability.ready("the incident store is reachable")

    return probe


def claude_cli_available(executable: str = "claude") -> Availability:
    """Whether the coding engine is present.

    Registered as a probe rather than checked once at import, so that an
    operator who installs the CLI does not have to restart Wiz to be told the
    truth about what it can do.
    """
    if shutil.which(executable) is None:
        return Availability.missing(
            f"the '{executable}' CLI is not on PATH, so I cannot write code"
        )
    return Availability.ready(f"{executable} is installed")


# ---------------------------------------------------------------------------
# The runtime
# ---------------------------------------------------------------------------


class WizRuntime:
    """A configured Wiz, plus the pieces the handlers need.

    Handlers are bound methods here rather than closures so that each one is a
    named, testable function, and so that the dependency it uses — the incident
    store, the policy — is visible at the call site.
    """

    def __init__(
        self,
        *,
        policy: AuthorityPolicy,
        registry: CapabilityRegistry,
        journal: Optional[WizJournal] = None,
        store_factory: Optional[Callable[[], Any]] = None,
        config: Any = None,
        product: Any = None,
        watcher_status: Optional[Callable[[], Any]] = None,
        ledger: Any = None,
        voice_probe: Optional[Callable[[], Any]] = None,
        scheduler: Any = None,
    ) -> None:
        self.policy = policy
        self.registry = registry
        self.journal = journal
        self.config = config
        self.product = product
        self._store_factory = store_factory
        # Every one of these is optional, and is used only by ``wiz.health``
        # (§8 — Wiz's own health, kept apart from the product's). ``None`` is
        # an honest "not configured here" rather than a lie about what was
        # checked; see :mod:`openjarvis.wiz.health`.
        self._watcher_status = watcher_status
        self._ledger = ledger
        self._voice_probe = voice_probe
        self._scheduler = scheduler

        from openjarvis.wiz.product import product_intent_rules

        # The product rules are loaded whether or not the product side is
        # assembled, for the same reason its capabilities are declared: an
        # operator who says "add a download button" to an unconfigured machine
        # has been understood, and the useful answer names the missing target
        # rather than pretending the sentence was gibberish. The capability's
        # availability probe is what refuses; the classifier's job is only to
        # work out what was meant.
        rules = list(default_rules()) + list(product_intent_rules())

        self.wiz = Wiz(
            registry=registry,
            policy=policy,
            journal=journal,
            classifier=RuleClassifier(rules),
        )
        for name, handler in self._handlers().items():
            self.wiz.register(name, handler)

    def _handlers(self) -> Dict[str, Callable[[Request], Any]]:
        handlers: Dict[str, Callable[[Request], Any]] = {
            "wiz.capabilities": self.describe_capabilities,
            "wiz.authority": self.describe_authority,
            "wiz.health": self.describe_health,
            "reliability.status": self.reliability_status,
            "reliability.incidents": self.reliability_incidents,
        }
        if self.product is not None:
            # Registered only when the product side is actually assembled, so
            # that a machine without it says "not built here" rather than
            # declaring a verb with no handler behind it.
            handlers.update(self.product.handlers())
        return handlers

    # -- handlers ----------------------------------------------------------

    def describe_capabilities(self, request: Request) -> Dict[str, Any]:
        """What Wiz can do *here*, split by whether it is configured.

        Both halves are returned. Telling the operator only about what works
        hides the reason a thing they expected is missing; telling them about
        everything as though it worked would be the pretending the brief
        forbids.
        """
        specs = self.registry.describe()
        return {
            "configured": [s for s in specs if s["configured"]],
            "unavailable": [s for s in specs if not s["configured"]],
            "implemented": self.wiz.verbs(),
        }

    def describe_authority(self, request: Request) -> Dict[str, Any]:
        """What each channel is allowed to do, and what it could never do."""
        return {
            "granted": self.policy.to_mapping(),
            "ceiling": {
                channel.value: sorted(a.value for a in authorities)
                for channel, authorities in sorted(
                    CHANNEL_CEILING.items(), key=lambda kv: kv[0].value
                )
            },
            "asking_channel": request.actor.channel.value,
        }

    def describe_health(self, request: Request) -> Dict[str, Any]:
        """Wiz's own health, kept separate from the website's.

        The distinction matters and the audit called it out: "the site is fine"
        and "I am fine" are different claims, and an assistant that conflates
        them will eventually report a healthy site it has lost the ability to
        see. See :mod:`openjarvis.wiz.health` for the full report this
        summarises; nothing below reads an incident, a probe result, or the
        site's own uptime.
        """
        from openjarvis.wiz.health import build_wiz_health

        channel = getattr(self.config, "channel", None)
        telegram = getattr(channel, "telegram", None)

        workspace = getattr(getattr(self.product, "pipeline", None), "workspace", None)
        report = build_wiz_health(
            journal=self.journal,
            registry=self.registry,
            watcher_status=self._watcher_status,
            product=self.product,
            ledger=self._ledger,
            telegram_bot_token=str(getattr(telegram, "bot_token", "") or ""),
            telegram_allowed_chat_ids=str(
                getattr(telegram, "allowed_chat_ids", "") or ""
            ),
            voice_probe=self._voice_probe,
            scheduler=self._scheduler,
            disk_path=getattr(workspace, "root", None),
        )
        chain_ok: Optional[bool] = None
        chain_break: Optional[int] = None
        if self.journal is not None:
            chain_ok, chain_break = self.journal.verify()

        return {
            # Kept for callers of the previous shape.
            "authority_policy": "loaded",
            "capabilities_declared": len(self.registry),
            "capabilities_implemented": len(self.wiz.verbs()),
            "journal": {
                "enabled": self.journal is not None,
                "intact": chain_ok,
                "first_break_at": chain_break,
            },
            "coding_engine": claude_cli_available().detail,
            # The full report.
            "overall": report.overall.value,
            "checks": report.to_dict()["checks"],
            "troubles": report.troubles(),
        }

    def reliability_status(self, request: Request) -> Dict[str, Any]:
        """A summary of what the reliability subsystem currently believes."""
        store = self._store()
        if store is None:
            return {"available": False, "detail": "no incident store"}
        try:
            open_incidents = [
                inc
                for inc in store.list(limit=200)
                if getattr(inc, "state", None)
                and str(getattr(inc.state, "value", inc.state)).upper()
                not in {"RESOLVED", "CLOSED", "CANCELLED"}
            ]
            return {
                "available": True,
                "open": len(open_incidents),
                "incidents": [self._brief(inc) for inc in open_incidents[:5]],
            }
        finally:
            self._close(store)

    def reliability_incidents(self, request: Request) -> Dict[str, Any]:
        """The most recent incidents, whatever state they are in."""
        store = self._store()
        if store is None:
            return {"available": False, "detail": "no incident store"}
        try:
            limit = int(request.arguments.get("limit", 10))
            return {
                "available": True,
                "incidents": [self._brief(inc) for inc in store.list(limit=limit)],
            }
        finally:
            self._close(store)

    # -- plumbing ----------------------------------------------------------

    @staticmethod
    def _brief(incident: Any) -> Dict[str, Any]:
        state = getattr(incident, "state", "")
        severity = getattr(incident, "severity", "")
        return {
            "id": getattr(incident, "id", ""),
            "title": getattr(incident, "title", ""),
            "component": getattr(incident, "component", ""),
            "state": getattr(state, "value", state),
            "severity": getattr(severity, "value", severity),
        }

    def _store(self) -> Any:
        if self._store_factory is None:
            return None
        try:
            return self._store_factory()
        except Exception:
            logger.exception("the incident store could not be opened")
            return None

    @staticmethod
    def _close(store: Any) -> None:
        close = getattr(store, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover - defensive
                logger.debug("closing the incident store failed", exc_info=True)


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


def default_capabilities(
    config: Any = None,
    *,
    incident_probe: Optional[Callable[[], Availability]] = None,
) -> List[CapabilitySpec]:
    """Everything Wiz declares in Phase A.

    All ``READ``. All ``LOW`` risk. Adding a verb that writes anything means
    adding it here with the authority it truly needs, which is a diff a reviewer
    can see.
    """
    reliability_probe = incident_probe or (lambda: _incident_store_available(config))
    return [
        CapabilitySpec(
            name="wiz.capabilities",
            summary="list what I can and cannot do here",
            authority=Authority.READ,
            risk=Risk.LOW,
            probe=_always_ready,
        ),
        CapabilitySpec(
            name="wiz.authority",
            summary="explain what I am allowed to do, and from where",
            authority=Authority.READ,
            risk=Risk.LOW,
            probe=_always_ready,
        ),
        CapabilitySpec(
            name="wiz.health",
            summary="report my own health, separately from the website's",
            authority=Authority.READ,
            risk=Risk.LOW,
            probe=_always_ready,
        ),
        CapabilitySpec(
            name="reliability.status",
            summary="report whether the site is currently in trouble",
            authority=Authority.READ,
            risk=Risk.LOW,
            probe=reliability_probe,
        ),
        CapabilitySpec(
            name="reliability.incidents",
            summary="list recent incidents",
            authority=Authority.READ,
            risk=Risk.LOW,
            probe=reliability_probe,
        ),
    ]


def _feature_owner_notifier(config: Any, root: Path) -> Optional[Any]:
    """Build the feature pipeline's owner notifier from live config, or None.

    Reuses the exact channel construction ``jarvis wiz listen`` already uses
    for the reliability side — same bot token, same allowlist, same
    ``TelegramNotifier`` — rather than a second transport. ``None`` when
    notifications are off or no Telegram credentials are configured, the
    same "declared, not disowned" shape every optional collaborator here
    takes: absence is silence, not an error.
    """
    rc = getattr(config, "reliability", None)
    notify = getattr(rc, "notify", None)
    if not getattr(notify, "enabled", False):
        return None
    telegram = getattr(getattr(config, "channel", None), "telegram", None)
    bot_token = str(getattr(telegram, "bot_token", "") or "")
    allowed_chat_ids = str(getattr(telegram, "allowed_chat_ids", "") or "")
    if not bot_token or not allowed_chat_ids:
        return None

    from openjarvis.reliability.notify import TelegramNotifier
    from openjarvis.wiz.features.notify import FeatureOwnerNotifier

    chat_id = allowed_chat_ids.split(",")[0].strip()
    transport = TelegramNotifier(
        chat_id=chat_id, bot_token=bot_token, allowed_chat_ids=allowed_chat_ids
    )
    return FeatureOwnerNotifier(
        send=lambda text: transport.send(text),
        ledger_path=root / "feature_notify_ledger.json",
        persona=bool(getattr(notify, "persona", True)),
    )


def build_wiz(
    *,
    home: Optional[Path] = None,
    policy: Optional[AuthorityPolicy] = None,
    config: Any = None,
    store_factory: Optional[Callable[[], Any]] = None,
    journal: Optional[WizJournal] = None,
    product: Any = None,
    watcher_status: Optional[Callable[[], Any]] = None,
    ledger: Any = None,
    voice_probe: Optional[Callable[[], Any]] = None,
    scheduler: Any = None,
) -> WizRuntime:
    """Build the Wiz the CLI and the dashboard both use.

    Every dependency is injectable so that tests never touch the operator's real
    journal or incident database — and so that a test proving "voice cannot
    merge" is proving it about the same object production runs.

    *product* is the assembled product-development side — the pipeline and the
    memory, wrapped in :class:`~openjarvis.wiz.product.ProductVerbs`. Passing
    ``None`` gives a Wiz that can answer questions and nothing else, which is
    the right shape on a machine with no engineering target configured.
    """
    root = Path(home) if home is not None else wiz_home()
    resolved_policy = policy or AuthorityPolicy.load(root / AUTHORITY_FILENAME)
    resolved_journal = journal or WizJournal(root / JOURNAL_FILENAME)

    if store_factory is None:

        def store_factory() -> Any:  # type: ignore[misc]
            from openjarvis.reliability.store import IncidentStore

            path = _incident_db_path(config)
            if path is None or not path.exists():
                raise FileNotFoundError("no incident database")
            return IncidentStore(path)

    from openjarvis.wiz.product import product_capabilities

    specs = default_capabilities(
        config, incident_probe=incident_probe_for(store_factory)
    )
    if product is not None:
        # One journal for the whole assistant. A feature pipeline writing to a
        # second, unchained file would give the audit trail a hole exactly where
        # the code-writing happens.
        pipeline = getattr(product, "pipeline", None)
        if pipeline is not None and getattr(pipeline, "journal", None) is None:
            pipeline.journal = resolved_journal
        postship = getattr(pipeline, "postship", None)
        if postship is not None and getattr(postship, "journal", None) is None:
            postship.journal = resolved_journal
        if pipeline is not None and getattr(pipeline, "owner_notifier", None) is None:
            notifier = _feature_owner_notifier(config, root)
            if notifier is not None:
                pipeline.owner_notifier = notifier

    # Declared whether or not the product side is assembled, and *unavailable*
    # when it is not.
    #
    # The alternative — leaving the verbs out entirely — was what shipped first,
    # and it made "add a download button" on an unconfigured machine come back
    # as "I did not recognise that as something I know how to do". That sentence
    # is wrong in the way that matters: Wiz understood the request perfectly and
    # the actual problem was a missing engineering target. Declaring the verb
    # and failing its availability probe is what turns that into "I cannot do
    # that here: no engineering target is configured", which is both true and
    # actionable. §5: never claim a capability that is not configured — and
    # never disown one that is merely unconfigured.
    specs.extend(
        product_capabilities(
            pipeline_available=lambda: _product_available(product),
            memory_available=lambda: _memory_available(product),
        )
    )

    return WizRuntime(
        policy=resolved_policy,
        registry=CapabilityRegistry(specs),
        journal=resolved_journal,
        store_factory=store_factory,
        config=config,
        product=product,
        watcher_status=watcher_status,
        ledger=ledger,
        voice_probe=voice_probe,
        scheduler=scheduler,
    )


def _product_available(product: Any) -> Availability:
    """Whether Wiz can actually build something right now.

    Four separate things have to be true, and the answer names which one is
    missing rather than saying "unavailable": an operator whose ``claude`` CLI
    has logged out deserves to be told that, not to be told Wiz cannot build
    features.
    """
    if product is None:
        return Availability.missing("no engineering target is configured")
    pipeline = getattr(product, "pipeline", None)
    if pipeline is None:
        return Availability.missing("no engineering target is configured")

    profile = getattr(pipeline, "profile", None)
    if profile is None or not getattr(profile, "complete", False):
        return Availability.missing(
            "the target repository has no test command, so I could not prove "
            "anything I built"
        )

    engineer = getattr(pipeline, "engineer", None)
    if engineer is not None:
        try:
            if not engineer.available():
                return Availability.missing(
                    "the 'claude' CLI is not available, and it is the only thing "
                    "here that writes code"
                )
        except Exception as exc:  # pragma: no cover - defensive
            return Availability.missing(f"I cannot reach the coding engine: {exc}")

    return Availability.ready(f"target '{profile.name}' is ready to build")


def _memory_available(product: Any) -> Availability:
    if product is None or getattr(product, "pipeline", None) is None:
        return Availability.missing("no engineering target is configured")
    return Availability.ready()


def operator(channel: Channel, actor_id: str = "operator") -> Actor:
    """The operator, arriving on *channel*, having been authenticated.

    A convenience for call sites that have already done the authenticating —
    the dashboard after its token check, the CLI by virtue of being a shell on
    this machine. Channels that have *not* authenticated must build their own
    :class:`Actor` with ``authenticated=False`` rather than reaching for this.
    """
    return Actor(actor_id=actor_id, channel=channel, authenticated=True)
