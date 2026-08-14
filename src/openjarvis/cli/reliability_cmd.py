"""``jarvis reliability`` -- JARVIS autonomous reliability engineering.

Phase 1 surface: inspect configuration and incidents.  Probe execution, the
repair loop and notifications arrive in later phases (see
``docs/JARVIS_ROADMAP.md``).
"""

from __future__ import annotations

from typing import Any, Optional

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

_SEVERITY_STYLE = {
    "CRITICAL": "bold red",
    "HIGH": "red",
    "MEDIUM": "yellow",
    "LOW": "dim",
}

_STATE_STYLE = {
    "RESOLVED": "green",
    "FAILED": "red",
    "HUMAN_REQUIRED": "bold magenta",
    "ROLLED_BACK": "yellow",
}


def _load_config() -> Any:
    """Load the user config (imported lazily to keep CLI startup fast)."""
    from openjarvis.core.config import load_config

    return load_config()


def _get_store(config: Any = None) -> Any:
    """Build an :class:`IncidentStore` from user config."""
    from openjarvis.core.paths import get_config_dir
    from openjarvis.reliability.store import IncidentStore

    config = config or _load_config()
    db_path = getattr(config.reliability, "db_path", "") or str(
        get_config_dir() / "reliability" / "incidents.db"
    )
    return IncidentStore(db_path)


def _severity_text(value: str) -> str:
    return f"[{_SEVERITY_STYLE.get(value, 'white')}]{value}[/]"


def _state_text(value: str) -> str:
    style = _STATE_STYLE.get(value, "cyan")
    return f"[{style}]{value}[/]"


@click.group()
def reliability() -> None:
    """Monitor and repair a production web application (JARVIS)."""


@reliability.command("status")
def reliability_status() -> None:
    """Show configuration and open-incident summary."""
    from openjarvis.reliability.types import IncidentState

    console = Console()
    config = _load_config()
    rc = config.reliability

    table = Table(title="JARVIS", show_header=False, box=None)
    table.add_column("key", style="dim")
    table.add_column("value")
    table.add_row("Monitoring", "enabled" if rc.enabled else "[dim]disabled[/]")
    table.add_row("Site", rc.site.base_url or "[dim]not configured[/]")
    table.add_row("Environment", rc.site.environment)
    for name, section in (
        ("Vercel", rc.vercel),
        ("Supabase", rc.supabase),
        ("GitHub", rc.github),
        ("Repair", rc.repair),
        ("Notifications", rc.notify),
    ):
        table.add_row(name, "enabled" if section.enabled else "[dim]disabled[/]")
    table.add_row("Deploy mode", rc.policy.deploy_mode)
    table.add_row("Max repair attempts", str(rc.repair.max_attempts))
    console.print(table)

    store = _get_store(config)
    try:
        incidents = store.list(open_only=True, limit=500)
        by_state: dict[str, int] = {}
        for incident in incidents:
            by_state[incident.state.value] = by_state.get(incident.state.value, 0) + 1
        console.print()
        if not incidents:
            console.print("[green]No open incidents.[/green]")
        else:
            console.print(f"[bold]{len(incidents)} open incident(s)[/bold]")
            for state in IncidentState:
                count = by_state.get(state.value)
                if count:
                    console.print(f"  {_state_text(state.value)}: {count}")
        intact, broken_row = store.verify_chain()
        if not intact:
            console.print(
                "[bold red]Audit chain broken at transition row "
                f"{broken_row}[/bold red]"
            )
    finally:
        store.close()


@reliability.group("incident")
def incident_group() -> None:
    """Inspect incidents."""


@incident_group.command("list")
@click.option("--state", default=None, help="Filter by incident state.")
@click.option("--severity", default=None, help="Filter by severity.")
@click.option("--open", "open_only", is_flag=True, help="Only unresolved incidents.")
@click.option("--limit", default=20, show_default=True, help="Maximum rows.")
def incident_list(
    state: Optional[str],
    severity: Optional[str],
    open_only: bool,
    limit: int,
) -> None:
    """List incidents, newest first."""
    from openjarvis.reliability.types import IncidentState, Severity

    console = Console()
    store = _get_store()
    try:
        incidents = store.list(
            state=IncidentState.parse(state) if state else None,
            severity=Severity.parse(severity) if severity else None,
            open_only=open_only,
            limit=limit,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc
    finally:
        store.close()

    if not incidents:
        console.print("[dim]No incidents found.[/dim]")
        return

    table = Table(title="Incidents")
    table.add_column("ID", style="bold")
    table.add_column("Severity")
    table.add_column("State")
    table.add_column("Component")
    table.add_column("Title")
    table.add_column("Seen", justify="right")
    for incident in incidents:
        table.add_row(
            incident.id,
            _severity_text(incident.severity.value),
            _state_text(incident.state.value),
            escape(incident.component),
            escape(incident.title),
            str(incident.occurrences),
        )
    console.print(table)


@incident_group.command("show")
@click.argument("incident_id")
def incident_show(incident_id: str) -> None:
    """Show one incident in full, including its audited history."""
    console = Console()
    store = _get_store()
    try:
        incident = store.get(incident_id)
        if incident is None:
            console.print(f"[red]No such incident: {incident_id}[/red]")
            raise SystemExit(1)

        console.print(
            Panel(
                f"{_severity_text(incident.severity.value)}  "
                f"{_state_text(incident.state.value)}\n\n"
                + escape(incident.summary or incident.title),
                title=f"{incident.id} — {escape(incident.title)}",
            )
        )

        meta = Table(show_header=False, box=None)
        meta.add_column("key", style="dim")
        meta.add_column("value")
        meta.add_row("Component", escape(incident.component))
        meta.add_row("Environment", escape(incident.environment))
        meta.add_row("Source", escape(incident.source))
        meta.add_row("Fingerprint", incident.fingerprint)
        meta.add_row("Detected", incident.created_at)
        meta.add_row("Last seen", incident.last_seen_at)
        meta.add_row("Occurrences", str(incident.occurrences))
        meta.add_row("Attempts", f"{incident.attempts_used}")
        if incident.correlation.commit_sha:
            meta.add_row("Likely commit", incident.correlation.commit_sha)
        if incident.correlation.deployment_id:
            meta.add_row("Deployment", incident.correlation.deployment_id)
        console.print(meta)

        if incident.repro_steps:
            console.print("\n[bold]Reproduction[/bold]")
            for index, step in enumerate(incident.repro_steps, start=1):
                console.print(f"  {index}. {escape(step)}")

        if incident.evidence:
            console.print("\n[bold]Evidence[/bold]")
            evidence_table = Table(box=None)
            evidence_table.add_column("Kind", style="dim")
            evidence_table.add_column("Trust")
            evidence_table.add_column("Summary")
            for item in incident.evidence:
                trust = (
                    "[yellow]external[/]" if item.is_external else "[green]trusted[/]"
                )
                evidence_table.add_row(item.kind.value, trust, escape(item.summary))
            console.print(evidence_table)

        if incident.attempts:
            console.print("\n[bold]Repair attempts[/bold]")
            attempt_table = Table(box=None)
            attempt_table.add_column("#")
            attempt_table.add_column("Branch")
            attempt_table.add_column("Files")
            attempt_table.add_column("Tests")
            attempt_table.add_column("Verified")
            attempt_table.add_column("Outcome")
            for attempt in incident.attempts:
                attempt_table.add_row(
                    str(attempt.number),
                    escape(attempt.branch),
                    str(len(attempt.changed_files)),
                    "—" if attempt.tests_passed is None else str(attempt.tests_passed),
                    "[green]yes[/]" if attempt.verified else "[red]no[/]",
                    escape(attempt.outcome),
                )
            console.print(attempt_table)

        console.print("\n[bold]History[/bold]")
        for transition in incident.transitions:
            reason = f" — {escape(transition.reason)}" if transition.reason else ""
            console.print(
                f"  {transition.at}  "
                f"{transition.from_state.value} → {transition.to_state.value}"
                f"  [dim]({escape(transition.actor)}){reason}[/dim]"
            )
    finally:
        store.close()


@reliability.command("verify-audit")
def reliability_verify_audit() -> None:
    """Verify the incident transition log's hash chain."""
    console = Console()
    store = _get_store()
    try:
        intact, broken_row = store.verify_chain()
    finally:
        store.close()
    if intact:
        console.print("[green]Audit chain intact.[/green]")
    else:
        console.print(f"[bold red]Audit chain broken at row {broken_row}.[/bold red]")
        raise SystemExit(1)


@reliability.command("doctor")
def reliability_doctor() -> None:
    """Report what is configured, what is missing, and what is unsafe."""
    import os

    console = Console()
    config = _load_config()
    rc = config.reliability

    problems: list[str] = []
    warnings: list[str] = []

    if not rc.enabled:
        warnings.append("Monitoring is disabled ([reliability] enabled = false).")
    if not rc.site.base_url:
        problems.append("No site configured ([reliability.site] base_url).")

    for label, section in (
        ("Vercel", rc.vercel),
        ("Supabase", rc.supabase),
        ("GitHub", rc.github),
    ):
        if not section.enabled:
            continue
        env_name = section.token_env
        if not env_name:
            problems.append(f"{label} is enabled but names no token_env.")
        elif not os.environ.get(env_name):
            problems.append(f"{label} is enabled but ${env_name} is not set.")

    if rc.repair.enabled and not rc.repair.workspace:
        problems.append(
            "Repair is enabled but [reliability.repair] workspace is unset."
        )
    if rc.repair.enabled and not rc.repair.test_command:
        warnings.append(
            "Repair is enabled with no test_command; fixes will not be tested."
        )

    if rc.policy.allow_push_to_default_branch:
        warnings.append(
            "allow_push_to_default_branch is TRUE — JARVIS may write to the "
            "default branch."
        )
    if rc.supabase.allow_production_writes:
        warnings.append(
            "allow_production_writes is TRUE — Supabase is no longer read-only."
        )
    if rc.policy.deploy_mode != "pr_only":
        warnings.append(f"deploy_mode is '{rc.policy.deploy_mode}', not 'pr_only'.")

    for problem in problems:
        console.print(f"[red]✗[/red] {escape(problem)}")
    for warning in warnings:
        console.print(f"[yellow]![/yellow] {escape(warning)}")
    if not problems and not warnings:
        console.print("[green]✓[/green] Configuration looks consistent.")
    if problems:
        raise SystemExit(1)
