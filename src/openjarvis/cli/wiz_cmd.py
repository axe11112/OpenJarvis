"""``jarvis wiz`` — asking Wiz to build something, from a terminal.

This is a thin surface on purpose. Every command here does exactly what the
Control Center button does: build a :class:`~openjarvis.wiz.brain.Request` with
``Channel.CLI`` on it and hand it to the same dispatcher. There is no CLI
pipeline and no CLI authority path, so a permission that is refused in the
dashboard is refused here for the same reason and with the same message.

``jarvis wiz build`` is the one that does work, and it is deliberately two
steps: it records the request, prints the identifier, and then runs it. If the
authority to write code is missing, the request is still recorded — the operator
gets "I have written it down, but I am not allowed to build it", which is a
better outcome than losing what they asked for.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

logger = logging.getLogger(__name__)

_STATE_STYLE = {
    "RECEIVED": "dim",
    "UNDERSTANDING": "cyan",
    "PLANNING": "cyan",
    "APPROVED_FOR_BUILD": "cyan",
    "BUILDING": "cyan",
    "TESTING": "cyan",
    "PREVIEWING": "cyan",
    "VERIFYING": "cyan",
    "READY": "bold green",
    "MERGING": "cyan",
    "DEPLOYING": "cyan",
    "PRODUCTION_VERIFYING": "cyan",
    "COMPLETE": "green",
    "HUMAN_REQUIRED": "bold magenta",
    "CANCELLED": "dim",
}

_RISK_STYLE = {"HIGH": "bold red", "MEDIUM": "yellow", "LOW": "dim"}

#: Read with ``.get(key, _DEFAULT_STYLE)``, never ``.get(key, "")`` — an empty
#: string produces the Rich markup tag ``[]``, which Rich treats as an
#: unrecognised open tag rather than "no style". The ``[/]`` that follows then
#: has nothing valid to close and raises ``MarkupError``, which took the whole
#: ``wiz list`` table down the moment a feature sat in a state (``TESTING``,
#: ``PLANNING``, ...) this dict hadn't been kept in sync with. A default that
#: is always a real style name keeps an unmapped key cosmetic, never fatal.
_DEFAULT_STYLE = "white"


def _console() -> Console:
    return Console()


def _runtime() -> Any:
    """Build the Wiz the dashboard also uses, product side included.

    Also wires in the collaborators `wiz.health` needs to report on Wiz's own
    processes — the watcher, the notification ledger, Sir Voice — each
    optional, and each attached only when it can actually be reached from this
    process. A CLI invocation that fails to build one of them must still be
    able to answer `jarvis wiz build`; health-check plumbing is not allowed to
    be a reason the CLI itself does not start.
    """
    from openjarvis.core.config import load_config
    from openjarvis.wiz.assemble import assemble
    from openjarvis.wiz.runtime import build_wiz

    config = load_config()
    return build_wiz(
        config=config,
        product=assemble(config=config),
        watcher_status=_watcher_status_probe(config),
        ledger=_ledger(config),
        voice_probe=_voice_probe(config),
    )


def _watcher_status_probe(config: Any) -> Any:
    try:
        from openjarvis.reliability.dashboard.supervisor import LaunchdSupervisor

        supervisor = LaunchdSupervisor(config)
    except Exception:  # noqa: BLE001 - health reporting must never block startup
        return None
    return supervisor.status


def _ledger(config: Any) -> Any:
    try:
        from openjarvis.reliability.notify_ledger import NotificationLedger, ledger_path

        return NotificationLedger(path=ledger_path(config))
    except Exception:  # noqa: BLE001
        return None


def _voice_probe(config: Any) -> Any:
    """A callable snapshot of what Sir Voice can check from a bare CLI process.

    Full voice assembly — sessions, calls, push, the phone's own registration —
    lives in whichever process is actually serving calls (the reliability
    dashboard command), and rebuilding all of it here just to answer a health
    check would be the kind of always-on duplication §16 asks this project not
    to add. So this builds only the two parts that are cheap, stateless and
    genuinely answerable from any process: whether whisper.cpp and its model
    are on disk, and whether ``say`` is available. Everything this cannot see
    — the microphone, the phone, the tailnet — reports honestly as unknown
    rather than being guessed at or omitted.
    """
    from openjarvis.reliability.voice.stt import DEFAULT_MODEL, WhisperTranscriber
    from openjarvis.reliability.voice.tts import MacSpeech

    home = _voice_home(config)
    model_path = str(home / "voice" / "models" / f"ggml-{DEFAULT_MODEL}.bin")
    transcriber = WhisperTranscriber(model_path=model_path)
    speech = MacSpeech()
    if not transcriber.available and not speech.available:
        # Neither half is installed. Rather than reporting a hollow panel,
        # this is the honest "not configured" a fresh machine deserves.
        return None

    def probe() -> Any:
        from openjarvis.reliability.voice.health import VoiceHealth

        return VoiceHealth(
            transcriber=transcriber, speech=speech, normalizer=transcriber.normalizer
        ).snapshot()

    return probe


def _voice_home(config: Any) -> Any:
    from openjarvis.core.paths import get_config_dir

    return get_config_dir()


def _actor() -> Any:
    from openjarvis.wiz.authority import Channel
    from openjarvis.wiz.runtime import operator

    # A shell on this machine is the operator: whoever has it already has the
    # files. What the CLI does *not* get is a higher ceiling than a shell
    # deserves — it stops at PR_WRITE, so merging still goes through the
    # dashboard and stays in the audit trail.
    return operator(Channel.CLI)


def _handle(capability: str = "", text: str = "", **arguments: Any) -> Any:
    from openjarvis.wiz.brain import Request

    runtime = _runtime()
    if capability.startswith(("feature.", "product.")) and runtime.product is None:
        # The verb genuinely does not exist here, and saying only that is
        # accurate and useless. The operator needs to know what to do next.
        _explain_unconfigured()
    request = Request(text=text, actor=_actor(), arguments=arguments)
    return runtime.wiz.handle(request, capability=capability or None)


def _explain_unconfigured() -> None:
    """Say why there is nothing to build, and exit."""
    from openjarvis.wiz.assemble import describe

    console = _console()
    report = describe()
    missing = [c for c in report["checks"] if not c["ok"]]
    console.print(
        "[yellow]I have no engineering target configured, so there is nothing "
        "I could build.[/yellow]"
    )
    for check in missing[:3]:
        console.print(f"  [dim]{check['name']}: {check['detail'] or 'missing'}[/dim]")
    console.print("\n[dim]Run `jarvis wiz status` for the whole picture.[/dim]")
    raise SystemExit(1)


def _build_owner_door_for_watch(config: Any, telegram_channel: Any) -> Any:
    """Build the shared Telegram owner door for ``jarvis reliability watch``.

    Registered into reliability_cmd below rather than that module importing
    wiz itself: reliability must survive a bug in this optional convenience
    feature, and a static import would not (see test_dependency_direction.py).
    Returns ``None`` when Wiz has no owner door to offer.
    """
    from openjarvis.cli.reliability_cmd import _DummyNotifier
    from openjarvis.wiz.assemble import assemble
    from openjarvis.wiz.owner_channel import TelegramOwnerDoor, build_owner_door
    from openjarvis.wiz.runtime import build_wiz

    wiz_runtime = build_wiz(config=config, product=assemble(config=config))
    door = build_owner_door(config, runtime=wiz_runtime)
    if door is None:
        return None
    return TelegramOwnerDoor(door=door, notifier=_DummyNotifier(telegram_channel))


from openjarvis.cli.reliability_cmd import register_owner_door_factory  # noqa: E402

register_owner_door_factory(_build_owner_door_for_watch)


@click.group(help="Ask Wiz to build things, and see what it has built.")
def wiz() -> None:
    """Wiz — the product-development half of JARVIS."""


@wiz.command("build")
@click.argument("description", nargs=-1, required=True)
@click.option("--title", default="", help="A short name for this request.")
@click.option(
    "--urgent", is_flag=True, default=False, help="Queue ahead of normal requests."
)
@click.option(
    "--record-only",
    is_flag=True,
    default=False,
    help="Write the request down without starting work.",
)
def build(description: tuple, title: str, urgent: bool, record_only: bool) -> None:
    """Ask for something to be built: ``jarvis wiz build "add a download button"``."""
    console = _console()
    text = " ".join(description).strip()

    recorded = _handle(
        capability="feature.request",
        text=text,
        title=title,
        priority="P2" if urgent else "P3",
    )
    if not recorded.handled:
        console.print(f"[red]{recorded.message}[/red]")
        raise SystemExit(1)

    result = recorded.result
    if not result.get("recorded"):
        detail = result.get("detail", "nothing was recorded")
        console.print(f"[yellow]{detail}[/yellow]")
        raise SystemExit(1)

    feature_id = result["id"]
    console.print(f"[green]{result['say']}[/green]")
    if record_only:
        return

    built = _handle(capability="feature.build", feature_id=feature_id)
    if not built.handled:
        # The request survives. "Written down but not allowed to build" is a
        # far better outcome than losing what the operator asked for.
        console.print(f"[yellow]{built.message}[/yellow]")
        console.print(f"[dim]{feature_id} is recorded and waiting.[/dim]")
        raise SystemExit(1)

    say = built.result.get("say") or ""
    state = built.result.get("state", "?")
    console.print(say or f"[dim]{feature_id} is {state}.[/dim]")


@wiz.command("list")
def list_features() -> None:
    """Show what Wiz is building, what is ready, and what needs you."""
    console = _console()
    outcome = _handle(capability="feature.list")
    if not outcome.handled:
        console.print(f"[red]{outcome.message}[/red]")
        raise SystemExit(1)

    result = outcome.result
    if not result.get("available"):
        console.print(f"[yellow]{result.get('detail', 'nothing to show')}[/yellow]")
        return

    for heading, key in (
        ("Building", "building"),
        ("Ready", "ready"),
        ("Needs you", "waiting_for_you"),
    ):
        rows = result.get(key) or []
        if not rows:
            continue
        table = Table(title=heading, title_justify="left", header_style="dim")
        table.add_column("ID")
        table.add_column("What")
        table.add_column("State")
        table.add_column("Risk")
        table.add_column("Tries", justify="right")
        for row in rows:
            table.add_row(
                row["id"],
                escape(row["title"][:60]),
                f"[{_STATE_STYLE.get(row['state'], _DEFAULT_STYLE)}]{row['state']}[/]",
                f"[{_RISK_STYLE.get(row['risk'], _DEFAULT_STYLE)}]{row['risk']}[/]",
                str(row.get("attempts", 0)),
            )
        console.print(table)

    if not any(result.get(k) for k in ("building", "ready", "waiting_for_you")):
        console.print("[dim]Nothing in progress.[/dim]")


@wiz.command("show")
@click.argument("feature_id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Raw record.")
def show(feature_id: str, as_json: bool) -> None:
    """Everything about one request."""
    console = _console()
    outcome = _handle(capability="feature.status", feature_id=feature_id)
    if not outcome.handled or not outcome.result.get("available"):
        message = outcome.message or outcome.result.get("detail", "not found")
        console.print(f"[red]{message}[/red]")
        raise SystemExit(1)

    feature = outcome.result["feature"]
    if as_json:
        click.echo(json.dumps(feature, indent=2, sort_keys=True))
        return

    console.print(
        Panel(
            feature["operator_request"],
            title=f"{feature['id']} — {feature['title']}",
            subtitle=f"{feature['state']} · {feature['risk']} risk",
        )
    )

    if feature.get("acceptance"):
        console.print("\n[bold]This is only done if[/bold]")
        for line in feature["acceptance"]:
            console.print(f"  · {line}")

    for attempt in feature.get("attempts", []):
        mark = "[green]passed[/green]" if attempt["succeeded"] else "[red]failed[/red]"
        console.print(
            f"\n[bold]Attempt {attempt['number']}[/bold] {mark} — "
            f"{len(attempt['changed_files'])} file(s), "
            f"{attempt['lines_changed']} line(s)"
        )
        if attempt.get("failure"):
            first = attempt["failure"].strip().split("\n")[0]
            console.print(f"  [dim]{first[:160]}[/dim]")

    if feature.get("preview_url"):
        console.print(f"\nPreview: {feature['preview_url']}")
    if feature.get("pr_url"):
        console.print(f"Pull request: {feature['pr_url']}")

    verification = (feature.get("metadata") or {}).get("verification") or {}
    if verification.get("summary"):
        console.print(f"\n[bold]Verification[/bold] {verification['summary']}")
    for outstanding in verification.get("awaiting_a_person") or []:
        console.print(f"  [yellow]needs you: {outstanding}[/yellow]")


@wiz.command("search")
@click.argument("query", nargs=-1, required=True)
def search(query: tuple) -> None:
    """Search what we have built, decided and learned."""
    console = _console()
    outcome = _handle(capability="product.search", query=" ".join(query))
    if not outcome.handled:
        console.print(f"[red]{outcome.message}[/red]")
        raise SystemExit(1)
    result = outcome.result
    if not result.get("available"):
        console.print(f"[yellow]{result.get('detail', '')}[/yellow]")
        return
    console.print(result.get("say") or "[dim]Nothing found.[/dim]")


@wiz.command("recent")
@click.option("--day", default="", help="A calendar day, YYYY-MM-DD.")
@click.option("--limit", default=10, show_default=True)
def recent(day: str, limit: int) -> None:
    """What was built recently."""
    console = _console()
    outcome = _handle(capability="product.recent", day=day, limit=limit)
    if not outcome.handled:
        console.print(f"[red]{outcome.message}[/red]")
        raise SystemExit(1)
    console.print(outcome.result.get("say") or "[dim]Nothing to report.[/dim]")


@wiz.command("status")
def status() -> None:
    """What Wiz can and cannot do here, and why."""
    console = _console()

    from openjarvis.core.config import load_config
    from openjarvis.wiz.assemble import describe

    report = describe()
    table = Table(title="Can I build?", title_justify="left", header_style="dim")
    table.add_column("")
    table.add_column("Check")
    table.add_column("Detail", overflow="fold")
    for check in report["checks"]:
        mark = "[green]yes[/green]" if check["ok"] else "[red]no[/red]"
        table.add_row(mark, check["name"], check["detail"] or "—")
    console.print(table)

    console.print(
        f"\nBuild: {'[green]yes[/green]' if report['can_build'] else '[red]no[/red]'}"
        f"   Verify: "
        f"{'[green]yes[/green]' if report['can_verify'] else '[red]no[/red]'}"
    )

    shipping = report["shipping"]
    console.print("\n[bold]What I may do when something is ready[/bold]")
    console.print(
        f"  open a pull request: {'yes' if shipping['create_pull_request'] else 'no'}"
    )
    console.print(
        f"  merge a LOW-risk feature: "
        f"{'[yellow]yes[/yellow]' if shipping['merge_low_risk'] else 'no'}"
    )
    console.print(
        f"  merge a MEDIUM-risk feature: "
        f"{'[yellow]yes[/yellow]' if shipping['merge_medium_risk'] else 'no'}"
    )
    console.print("  merge a HIGH-risk feature: no [dim](never; always yours)[/dim]")

    # What Wiz is *allowed* to do, which is a different question from what it
    # can do, and the operator deserves both.
    outcome = _handle(capability="wiz.authority")
    if outcome.handled:
        console.print("\n[bold]What each channel may cause[/bold]")
        granted = outcome.result["granted"]
        for channel in sorted(granted):
            authorities = ", ".join(sorted(granted[channel])) or "nothing"
            console.print(f"  {channel}: {authorities}")

    _ = load_config  # imported for parity with the other commands' lazy loading


@wiz.command("dashboard")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address.")
@click.option("--port", default=8765, show_default=True, help="Port to listen on.")
@click.option(
    "--open/--no-open",
    "open_browser",
    default=False,
    show_default=True,
    help="Open the Control Center in the default browser once it is serving.",
)
@click.option(
    "--probe-verification",
    type=click.Choice(["none", "http", "all"]),
    default="http",
    show_default=True,
    help="How much of the probe fleet the dashboard runs itself for a real verdict.",
)
@click.option(
    "--watcher-control/--no-watcher-control",
    default=True,
    show_default=True,
    help="Offer Start/Restart buttons for the launchd watcher service.",
)
@click.option(
    "--auto-recover/--no-auto-recover",
    default=True,
    show_default=True,
    help="Ask launchd to start the watcher when the dashboard finds it offline.",
)
@click.option(
    "--tailscale/--no-tailscale",
    "use_tailscale",
    default=False,
    show_default=True,
    help="Also listen on this machine's Tailscale address.",
)
@click.option(
    "--voice/--no-voice",
    "enable_voice",
    default=False,
    show_default=True,
    help="Mount the Sir Voice call routes and the installable phone app.",
)
@click.option("--cert", "certfile", default="", help="TLS certificate.")
@click.option("--key", "keyfile", default="", help="TLS private key.")
def dashboard(
    host: str,
    port: int,
    open_browser: bool,
    probe_verification: str,
    watcher_control: bool,
    auto_recover: bool,
    use_tailscale: bool,
    enable_voice: bool,
    certfile: str,
    keyfile: str,
) -> None:
    """Serve the one Control Center — Wize's health and Wiz's, together.

    The identical server ``jarvis reliability dashboard`` runs — same
    incidents, same probes, same safety interlocks — with one addition: an
    "engineering" section built from the same WizRuntime `jarvis wiz` itself
    runs on. Not a second dashboard; the same process, reading one more
    thing. See ``run_control_center`` in reliability_cmd.py, which both
    commands call.
    """
    from openjarvis.cli.reliability_cmd import run_control_center
    from openjarvis.core.config import load_config
    from openjarvis.wiz.assemble import assemble
    from openjarvis.wiz.dashboard_snapshot import build_engineering_snapshot
    from openjarvis.wiz.runtime import build_wiz

    config = load_config()
    try:
        runtime = build_wiz(config=config, product=assemble(config=config))
    except Exception:  # noqa: BLE001 - the Control Center must still start
        logger.exception("could not assemble Wiz for the Control Center")
        runtime = None

    run_control_center(
        config,
        host=host,
        port=port,
        open_browser=open_browser,
        probe_verification=probe_verification,
        watcher_control=watcher_control,
        auto_recover=auto_recover,
        use_tailscale=use_tailscale,
        enable_voice=enable_voice,
        certfile=certfile,
        keyfile=keyfile,
        wiz_snapshot=lambda: build_engineering_snapshot(runtime),
    )


@wiz.command("ask")
@click.argument("question", nargs=-1, required=True)
def ask(question: tuple) -> None:
    """Say anything to Wiz and let it work out which verb you meant."""
    console = _console()
    outcome = _handle(text=" ".join(question))
    if not outcome.handled:
        console.print(f"[yellow]{outcome.message}[/yellow]")
        raise SystemExit(1)
    result = outcome.result
    if isinstance(result, dict) and result.get("say"):
        console.print(result["say"])
    else:
        click.echo(json.dumps(result, indent=2, sort_keys=True, default=str))


@wiz.command("morning")
@click.option("--force", is_flag=True, default=False, help="Print it even if empty.")
def morning(force: bool) -> None:
    """What Wiz would tell you this morning.

    Prints nothing when there is nothing worth saying, unless asked. A summary
    that arrives every day saying "nothing happened" is one nobody reads by the
    end of the second week, and by then it is the one carrying the sentence that
    mattered.
    """
    console = _console()

    from openjarvis.wiz.briefing import compose

    runtime = _runtime()
    store = getattr(getattr(runtime, "product", None), "pipeline", None)
    memory = getattr(getattr(runtime, "product", None), "memory", None)

    def reliability_status():
        outcome = _handle(capability="reliability.status")
        return outcome.result if outcome.handled else {"available": False}

    briefing = compose(
        store=getattr(store, "store", None),
        memory=memory,
        reliability=reliability_status,
        site_name="Wize",
    )
    if briefing.worth_sending or force:
        console.print(briefing.render())


@wiz.command("doctor")
@click.option(
    "--json", "as_json", is_flag=True, default=False, help="Machine-readable."
)
def doctor(as_json: bool) -> None:
    """Wiz's own health — never a statement about Wize's.

    `jarvis reliability doctor` answers "is the website OK". This answers "is
    Wiz itself OK" — the watcher process, the coding tool, the audit trail,
    the notification ledger, Sir Voice, the scheduler. A green answer here says
    nothing about production; a red one here can exist while the site is
    perfectly healthy, and that is the point of keeping the two apart.
    """
    console = _console()
    outcome = _handle(capability="wiz.health")
    if not outcome.handled:
        console.print(f"[red]{outcome.message}[/red]")
        raise SystemExit(1)

    result = outcome.result
    if as_json:
        console.print(json.dumps(result, indent=2, sort_keys=True))
        return

    style = {
        "HEALTHY": "green",
        "DEGRADED": "yellow",
        "NOT_CONFIGURED": "dim",
        "UNKNOWN": "dim",
        "BLOCKED": "red",
        "FAILED": "bold red",
        "NOT_CHECKED": "dim",
    }
    table = Table(title="Wiz health (not Wize's)", header_style="dim")
    table.add_column("check")
    table.add_column("state")
    table.add_column("detail", overflow="fold")
    for check in result.get("checks", []):
        state = check["state"]
        table.add_row(
            check["name"],
            f"[{style.get(state, 'white')}]{state}[/]",
            escape(check.get("summary") or check.get("detail") or ""),
        )
    console.print(table)
    overall = result.get("overall", "NOT_CHECKED")
    console.print("")
    console.print(f"Overall: [{style.get(overall, 'white')}]{overall}[/]")


@wiz.command("say")
@click.argument("message", nargs=-1, required=True)
@click.option(
    "--chat-id",
    default="",
    help="Answer as though this chat id had sent it. Defaults to the first "
    "configured owner chat, so the allowlist is exercised rather than bypassed.",
)
def say_to_wiz(message: tuple, chat_id: str) -> None:
    """Ask Wiz something the way the phone would, and print the reply.

    The same door, the same allowlist, the same dispatcher and the same
    rendering that a Telegram message goes through — with the network taken
    out. What this prints is what Sir would send.

    Useful for the thing that is otherwise hard to check: whether an English
    sentence reaches the verb the operator expected. A sentence that classifies
    as nothing prints the refusal, which is the honest answer and the one worth
    seeing before it is a silence on a phone.
    """
    console = _console()
    text = " ".join(message).strip()

    from openjarvis.core.config import load_config
    from openjarvis.wiz.owner_channel import build_owner_door

    config = load_config()
    allowed = (config.channel.telegram.allowed_chat_ids or "").split(",")
    who = chat_id or (allowed[0].strip() if allowed else "")

    door = build_owner_door(config, runtime=_runtime(), commands=None, outages=None)
    if door is None:
        console.print(
            "[yellow]Owner commands are switched off.[/yellow]\n"
            "[dim]Set [reliability.notify] enabled and accept_owner_commands to "
            "turn the door on.[/dim]"
        )
        raise SystemExit(1)

    reply = door.receive(chat_id=who, text=text)
    if not reply.text:
        console.print(
            "[dim](nothing would be sent — "
            f"{'unlisted chat' if not reply.authorized else 'no reply needed'})[/dim]"
        )
        return
    console.print(escape(reply.text))
    console.print(
        f"\n[dim]{escape(reply.route)}"
        + (f" · {escape(reply.capability)}" if reply.capability else "")
        + "[/dim]"
    )


@wiz.command("listen")
def listen() -> None:
    """Answer the owner on Telegram until interrupted.

    One door for both halves: "Fix it" reaches the reliability side when
    something is actually failing, and everything else reaches the dispatcher.

    Run this **or** the reliability watcher's own narrow listener, not both:
    Telegram refuses a second long-poll on one bot token, so the second one to
    start simply will not receive anything.
    """
    console = _console()

    from openjarvis.core.config import load_config
    from openjarvis.reliability.notify_ledger import ledger_path  # noqa: F401
    from openjarvis.reliability.outage import OutageRegistry, outages_path
    from openjarvis.reliability.owner_commands import OwnerCommands
    from openjarvis.wiz.owner_channel import TelegramOwnerDoor, build_owner_door

    config = load_config()
    rc = config.reliability

    if not (rc.notify.enabled and rc.notify.accept_owner_commands):
        console.print(
            "[yellow]Owner commands are switched off.[/yellow]\n"
            "[dim]Set [reliability.notify] enabled and accept_owner_commands "
            "first. An inbound control path should exist because you turned it "
            "on.[/dim]"
        )
        raise SystemExit(1)

    outages = OutageRegistry(path=outages_path(config))
    commands = OwnerCommands(
        allowed_chat_ids=config.channel.telegram.allowed_chat_ids,
        outages=outages,
        persona=rc.notify.persona,
        # No gate here: `jarvis wiz listen` is not the watcher and holds no
        # repair slot. "Fix it" from this process records the acknowledgement
        # and the watcher picks the work up on its own cadence.
        gate=None,
    )
    door = build_owner_door(
        config, runtime=_runtime(), commands=commands, outages=outages
    )
    if door is None:  # pragma: no cover - guarded above
        raise SystemExit(1)

    from openjarvis.reliability.notify import TelegramNotifier

    chat_ids = (config.channel.telegram.allowed_chat_ids or "").split(",")
    transport = TelegramNotifier(
        chat_id=chat_ids[0].strip() if chat_ids else "",
        bot_token=config.channel.telegram.bot_token,
        allowed_chat_ids=config.channel.telegram.allowed_chat_ids,
    )
    listener = TelegramOwnerDoor(door=door, notifier=transport)
    if not listener.start():
        console.print("[red]Could not start listening.[/red]")
        raise SystemExit(2)

    console.print("Listening for you on Telegram. Ctrl-C to stop.")
    try:
        import threading

        threading.Event().wait()
    except KeyboardInterrupt:
        console.print("\nStopped.")
    finally:
        listener.stop()


# ---------------------------------------------------------------------------
# Operator recovery verbs
#
# FeaturePipeline grew five operator verbs -- reopen_for_planning,
# reopen_for_deploy, reopen_for_owner_authorized_rebuild,
# approve_manual_acceptance and reverify_against_current_base -- each written to
# break a specific lifecycle deadlock, each tested, and none of them reachable
# from anything an owner can actually run. Every reference to them outside their
# own definitions was a docstring cross-reference. So the deadlocks they close
# stayed closed only for someone willing to open a Python REPL against the live
# feature store, which is the opposite of what a recovery path is for.
#
# They live on the CLI rather than on the dashboard on purpose. These are the
# most privileged verbs in the product -- one of them spends a fresh Claude
# session past an exhausted attempt budget, another records the owner's personal
# acceptance of a criterion no machine could measure -- and the dashboard is a
# network-reachable surface whose authority is deliberately narrow. The CLI is
# the owner at their own machine, which is the right ceiling for these.
#
# None of them reimplement anything: each is a thin call onto the canonical
# pipeline method, so the state machine, the journal and the approval store stay
# the single source of truth.
# ---------------------------------------------------------------------------


def _pipeline_or_exit() -> Any:
    """The assembled feature pipeline, or explain why there is not one."""
    runtime = _runtime()
    if runtime.product is None:
        _explain_unconfigured()
    return runtime.product.pipeline


def _report_feature(feature: Any, *, did: str) -> None:
    """Say what happened, in the terms the owner asked in."""
    console = _console()
    console.print(f"[green]{feature.id}: {did}[/green]")
    console.print(f"[dim]It is now {feature.state.value}.[/dim]")


@wiz.command("reopen")
@click.argument("feature_id")
@click.option(
    "--for",
    "target",
    type=click.Choice(["planning", "deploy", "rebuild"]),
    required=True,
    help=(
        "planning: never wrote code, try planning again. "
        "deploy: the diff is fine, the infrastructure was not -- re-run the "
        "checks with no new Claude session. "
        "rebuild: spend one fresh Claude session past an exhausted budget."
    ),
)
@click.option(
    "--reason",
    default="",
    help="Why. Required for --for rebuild, which is a one-off owner decision.",
)
def reopen(feature_id: str, target: str, reason: str) -> None:
    """Restart a stopped feature, choosing exactly how much to spend on it.

    The three are deliberately separate verbs rather than one "retry", because
    they cost different things and are right in different situations.
    """
    console = _console()
    pipeline = _pipeline_or_exit()

    if target == "rebuild" and not reason.strip():
        # No default, deliberately: this is the only verb that spends an
        # attempt past max_attempts, and it is once per feature. An owner who
        # cannot say why probably wants --for deploy.
        console.print(
            "[red]--reason is required for --for rebuild: it spends a fresh "
            "Claude session past the attempt budget, once per feature.[/red]"
        )
        raise SystemExit(1)

    try:
        if target == "planning":
            feature = pipeline.reopen_for_planning(feature_id, reason=reason)
            did = "reopened for planning"
        elif target == "deploy":
            feature = pipeline.reopen_for_deploy(feature_id, reason=reason)
            did = "reopened for the check suite, with no new Claude session"
        else:
            feature = pipeline.reopen_for_owner_authorized_rebuild(
                feature_id, reason=reason
            )
            did = "granted one owner-authorized rebuild"
    except Exception as exc:  # noqa: BLE001 - the refusal is the useful output
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    _report_feature(feature, did=did)


@wiz.command("accept")
@click.argument("feature_id")
@click.option(
    "--reason", required=True, help="What you checked, and what you concluded."
)
def accept(feature_id: str, reason: str) -> None:
    """Accept the outstanding items only a person can judge, and let it finish.

    For a feature whose automated checks all passed and whose only remaining
    gap is something structurally unmeasurable -- a layout that has no selector
    to compile against, typically. It cannot paper over a failed automated
    check: the approval is bound to this feature's current outstanding
    awaiting-a-person items at its current verified head SHA, so it stops
    matching the moment either changes.
    """
    console = _console()
    pipeline = _pipeline_or_exit()
    try:
        feature = pipeline.approve_manual_acceptance(feature_id, reason=reason)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc
    _report_feature(feature, did="accepted the outstanding items you judged")


@wiz.command("refresh-base")
@click.argument("feature_id")
@click.option(
    "--expected-head-sha",
    required=True,
    help=(
        "The commit you mean, in full. Required so this cannot act on a "
        "feature that moved between you reading it and running this."
    ),
)
@click.option("--reason", required=True, help="Why the base is being refreshed.")
def refresh_base(feature_id: str, expected_head_sha: str, reason: str) -> None:
    """Re-verify a stalled feature against a base branch that has moved.

    For the feature that was READY, and then main moved underneath it. Merges
    the current base into the existing commit and re-verifies; it never calls
    the coding engine and never spends a build attempt, so it is safe to run
    repeatedly as main keeps moving. A textual conflict stops for a person
    rather than guessing.
    """
    console = _console()
    pipeline = _pipeline_or_exit()
    try:
        feature = pipeline.reverify_against_current_base(
            feature_id, expected_head_sha=expected_head_sha, reason=reason
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc
    _report_feature(feature, did="re-verified against the current base")


@wiz.command("reconcile")
@click.argument("feature_id")
@click.option("--reason", default="", help="What you know about the interruption.")
def reconcile(feature_id: str, reason: str) -> None:
    """Work out what really happened to a feature that was mid-ship.

    For the feature that was merging, deploying or being checked in production
    when something died. Nothing else in the system will look at it again:
    ``ship`` refuses it because it is not READY, and crash recovery skips those
    states deliberately. This establishes the truth from GitHub rather than
    assuming it -- if the merge landed and production is good it finishes the
    feature, and if it cannot tell it says so and leaves it for you.

    Also the way back in for a feature whose merge landed but whose production
    check did not agree, once whatever was wrong has been fixed.
    """
    console = _console()
    pipeline = _pipeline_or_exit()
    try:
        feature = pipeline.reconcile_after_ship(feature_id, reason=reason)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc
    _report_feature(feature, did="reconciled against what production actually says")


@wiz.command("authority")
@click.option("--json", "as_json", is_flag=True, default=False, help="Raw report.")
def authority(as_json: bool) -> None:
    """What each channel is allowed to do here, and whether Wiz could ship.

    Read-only, and deliberately narrow: it loads the authority policy and
    nothing else -- no feature store, no git checkout, no engineering target, no
    network. Safe to run on the live machine before changing anything, and it
    prints no secrets because the policy holds none: it is a map from channel to
    permitted consequence.

    Run this before enabling a build that enforces the channel ceiling. The
    ceiling is enforced at both gates (``feature.build`` needs CODE_WRITE,
    shipping needs PRODUCTION_CHANGE), and ``AuthorityPolicy.default()``
    deliberately grants neither -- writing code is an opt-in an owner makes on
    purpose, not what happens when a config file is missing. Without this
    command the first sign of that would be a refused build.
    """
    import json as _json
    from pathlib import Path

    from openjarvis.wiz.authority import (
        CHANNEL_CEILING,
        Authority,
        AuthorityPolicy,
        Channel,
    )
    from openjarvis.wiz.runtime import AUTHORITY_FILENAME, wiz_home

    console = _console()
    path = Path(wiz_home()) / AUTHORITY_FILENAME
    policy = AuthorityPolicy.load(path)

    def _granted(channel: Channel) -> set:
        return set(policy.granted_to(channel))

    def _holders(authority_kind: Authority) -> list:
        return sorted(
            channel.value
            for channel in Channel
            if authority_kind in _granted(channel)
        )

    # "Structurally incapable" means the ceiling forbids it, so no configuration
    # can grant it -- a stronger statement than "it is not granted today".
    def _ceiling_forbids(channel: Channel, authority_kind: Authority) -> bool:
        return authority_kind not in CHANNEL_CEILING.get(channel, frozenset())

    build_ok = Authority.CODE_WRITE in _granted(Channel.CONTROL_CENTER)
    ship_ok = Authority.PRODUCTION_CHANGE in _granted(Channel.CONTROL_CENTER)

    report = {
        "policy_path": str(path),
        "policy_file_present": path.exists(),
        "using_defaults": not path.exists(),
        "granted": policy.to_mapping(),
        "ceiling": {
            channel.value: sorted(a.value for a in authorities)
            for channel, authorities in sorted(
                CHANNEL_CEILING.items(), key=lambda kv: kv[0].value
            )
        },
        "code_write": _holders(Authority.CODE_WRITE),
        "pr_write": _holders(Authority.PR_WRITE),
        "production_change": _holders(Authority.PRODUCTION_CHANGE),
        "telegram_cannot_change_production": _ceiling_forbids(
            Channel.TELEGRAM, Authority.PRODUCTION_CHANGE
        ),
        "voice_cannot_change_production": _ceiling_forbids(
            Channel.VOICE, Authority.PRODUCTION_CHANGE
        ),
        "control_center_can_build": build_ok,
        "control_center_can_change_production": ship_ok,
        "feature_build_allowed": build_ok,
        "feature_ship_allowed": ship_ok,
    }

    if as_json:
        click.echo(_json.dumps(report, indent=2, sort_keys=True))
        return

    console.print(f"[bold]Authority policy[/bold] {path}")
    if not path.exists():
        console.print(
            "[yellow]No authority.json here, so the built-in defaults apply.[/yellow]"
        )
        console.print(
            "[dim]The defaults grant no CODE_WRITE, PR_WRITE or "
            "PRODUCTION_CHANGE to anyone, deliberately.[/dim]"
        )
    console.print()

    console.print("[bold]Granted[/bold]")
    granted = report["granted"]
    for channel in sorted(c.value for c in Channel):
        allowed = granted.get(channel) or []
        shown = ", ".join(allowed) if allowed else "nothing"
        console.print(f"  {channel:<16} {shown}")
    console.print()

    console.print("[bold]Who can do what[/bold]")
    for label, key in (
        ("write code", "code_write"),
        ("open pull requests", "pr_write"),
        ("change production", "production_change"),
    ):
        holders = report[key]
        text = ", ".join(holders) if holders else "[yellow]nobody[/yellow]"
        console.print(f"  {label:<20} {text}")
    console.print()

    console.print("[bold]Ceilings that no configuration can lift[/bold]")
    for label, key in (
        ("Telegram", "telegram_cannot_change_production"),
        ("Voice", "voice_cannot_change_production"),
    ):
        if report[key]:
            console.print(
                f"  [green]{label} can never change production.[/green]"
            )
        else:
            console.print(
                f"  [red]{label} is NOT structurally barred from changing "
                f"production — the ceiling has been weakened.[/red]"
            )
    console.print()

    console.print("[bold]Would Wiz work right now[/bold]")
    console.print(
        "  build a feature   "
        + ("[green]yes[/green]" if build_ok else "[yellow]no[/yellow]")
    )
    console.print(
        "  ship a feature    "
        + ("[green]yes[/green]" if ship_ok else "[yellow]no[/yellow]")
    )
    if not (build_ok and ship_ok):
        missing = []
        if not build_ok:
            missing.append("CODE_WRITE")
        if not ship_ok:
            missing.append("PRODUCTION_CHANGE")
        console.print()
        console.print(
            "[yellow]control_center is missing "
            + " and ".join(missing)
            + ", so feature work will be refused.[/yellow]"
        )
        console.print(
            f"[dim]Grant it deliberately in {path} — this command will not "
            f"edit it for you.[/dim]"
        )


@wiz.command("approve-ship")
@click.argument("feature_id")
@click.option(
    "--reason", required=True, help="What you checked, and why this may merge."
)
def approve_ship(feature_id: str, reason: str) -> None:
    """Consent to merging one HIGH-risk feature, once.

    Grants nothing on its own and changes no policy. The approval is bound to
    this feature, its current verified commit, its current risk tier and the
    merge action, and it is consumed on use -- so a new commit, a re-rated risk
    or a different feature all leave it matching nothing.

    HIGH-risk features are never shipped automatically; that is unchanged. This
    exists so that when a person does decide, the decision is recorded as
    specifically as the MEDIUM-risk one already was, rather than being a bare
    yes that nothing could later tie to anything.
    """
    console = _console()
    pipeline = _pipeline_or_exit()
    try:
        feature = pipeline.approve_high_risk_ship(feature_id, reason=reason)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc
    _report_feature(feature, did="approved for a HIGH-risk merge, once")
