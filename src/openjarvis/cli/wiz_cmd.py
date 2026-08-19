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
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

_STATE_STYLE = {
    "READY": "bold green",
    "COMPLETE": "green",
    "HUMAN_REQUIRED": "bold magenta",
    "CANCELLED": "dim",
    "BUILDING": "cyan",
    "VERIFYING": "cyan",
}

_RISK_STYLE = {"HIGH": "bold red", "MEDIUM": "yellow", "LOW": "dim"}


def _console() -> Console:
    return Console()


def _runtime() -> Any:
    """Build the Wiz the dashboard also uses, product side included."""
    from openjarvis.core.config import load_config
    from openjarvis.wiz.assemble import assemble
    from openjarvis.wiz.runtime import build_wiz

    config = load_config()
    return build_wiz(config=config, product=assemble(config=config))


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
                row["title"][:60],
                f"[{_STATE_STYLE.get(row['state'], '')}]{row['state']}[/]",
                f"[{_RISK_STYLE.get(row['risk'], '')}]{row['risk']}[/]",
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
