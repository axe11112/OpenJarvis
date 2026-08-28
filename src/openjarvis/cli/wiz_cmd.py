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
