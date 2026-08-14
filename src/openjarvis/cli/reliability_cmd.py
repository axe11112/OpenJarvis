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


def _probe_dir(config: Any) -> Any:
    """Resolve the probe-spec directory, falling back to the config dir."""
    from pathlib import Path

    from openjarvis.core.paths import get_config_dir

    configured = getattr(config.reliability.probes, "directory", "")
    if configured:
        return Path(configured)
    return get_config_dir() / "reliability" / "probes"


def _evidence_dir(config: Any) -> str:
    """Resolve the evidence-artifact directory."""
    from openjarvis.core.paths import get_config_dir

    configured = getattr(config.reliability.probes, "evidence_dir", "")
    return configured or str(get_config_dir() / "reliability" / "evidence")


def _build_executor(config: Any) -> Any:
    """Build a :class:`ProbeExecutor` wired from user config."""
    from openjarvis.reliability.probes.executor import ProbeExecutor

    rc = config.reliability
    browser_options: dict[str, Any] = {
        "headless": config.tools.browser.headless,
        "viewport": (
            config.tools.browser.viewport_width,
            config.tools.browser.viewport_height,
        ),
    }
    if getattr(rc.probes, "browser_executable_path", ""):
        browser_options["executable_path"] = rc.probes.browser_executable_path

    return ProbeExecutor(
        base_url=rc.site.base_url,
        evidence_dir=_evidence_dir(config),
        runner_options={
            "browser": browser_options,
            "http": {"verify_ssrf": not rc.probes.allow_private_targets},
        },
    )


def _build_sources(config: Any) -> list:
    """Build the enabled signal sources from config."""
    from openjarvis.reliability.sources.github import GitHubSource
    from openjarvis.reliability.sources.supabase import SupabaseSource
    from openjarvis.reliability.sources.vercel import VercelSource

    rc = config.reliability
    sources: list = []
    if rc.vercel.enabled and rc.vercel.project_id:
        sources.append(
            VercelSource(
                project_id=rc.vercel.project_id,
                team_id=rc.vercel.team_id,
                token_env=rc.vercel.token_env,
            )
        )
    if rc.supabase.enabled and rc.supabase.project_ref:
        sources.append(
            SupabaseSource(
                project_ref=rc.supabase.project_ref,
                token_env=rc.supabase.token_env,
                allow_production_writes=rc.supabase.allow_production_writes,
            )
        )
    if rc.github.enabled and rc.github.repo:
        sources.append(
            GitHubSource(
                repo=rc.github.repo,
                token_env=rc.github.token_env,
                base_branch=rc.github.base_branch,
                branch_prefix=rc.github.branch_prefix,
                allow_push_to_default_branch=rc.policy.allow_push_to_default_branch,
                protected_paths=list(rc.policy.protected_paths),
            )
        )
    return sources


def _build_notifier(config: Any) -> Any:
    """Build the notification router, falling back to console output."""
    from openjarvis.reliability.notify import (
        ConsoleNotifier,
        NotificationRouter,
        TelegramNotifier,
    )
    from openjarvis.reliability.types import Severity

    rc = config.reliability
    notifier: Any = ConsoleNotifier()
    if rc.notify.enabled and rc.notify.channel == "telegram":
        chat_ids = (config.channel.telegram.allowed_chat_ids or "").split(",")
        chat_id = chat_ids[0].strip() if chat_ids else ""
        if chat_id:
            notifier = TelegramNotifier(
                chat_id=chat_id,
                bot_token=config.channel.telegram.bot_token,
                allowed_chat_ids=config.channel.telegram.allowed_chat_ids,
            )
    return NotificationRouter(
        notifier=notifier,
        min_severity=Severity.parse(rc.notify.min_severity),
        max_per_hour=rc.notify.max_messages_per_hour,
        persona=rc.notify.persona,
    )


def _build_policy(config: Any) -> Any:
    """Build the safety policy from config."""
    from openjarvis.reliability.policy import SafetyPolicy

    rc = config.reliability
    return SafetyPolicy(
        deploy_mode=rc.policy.deploy_mode,
        auto_repair_severities=list(rc.policy.auto_repair_severities),
        auto_deploy_fix_classes=list(rc.policy.auto_deploy_fix_classes),
        protected_paths=list(rc.policy.protected_paths),
        allow_push_to_default_branch=rc.policy.allow_push_to_default_branch,
        max_attempts=rc.repair.max_attempts,
        repair_enabled=rc.repair.enabled,
    )


def _build_monitor(config: Any, store: Any) -> Any:
    """Assemble the full monitoring stack from config."""
    from openjarvis.reliability.detector import Detector
    from openjarvis.reliability.monitor import ReliabilityMonitor
    from openjarvis.reliability.probes.spec import load_probes

    rc = config.reliability
    notifier = _build_notifier(config)
    specs = load_probes(_probe_dir(config))
    sources = _build_sources(config)

    detector = Detector(
        store,
        environment=rc.site.environment,
        notifier=notifier,
    )
    return ReliabilityMonitor(
        detector=detector,
        executor=_build_executor(config),
        specs=specs,
        sources=sources,
        repair_loop=_build_repair_loop(config, store, sources),
        notifier=notifier,
    )


def _build_repair_loop(config: Any, store: Any, sources: list) -> Any:
    """Build the repair loop, or ``None`` when repair is disabled."""
    rc = config.reliability
    if not rc.repair.enabled:
        return None

    from openjarvis.reliability.code_agent import ClaudeCliAgent
    from openjarvis.reliability.repair import RepairLoop
    from openjarvis.reliability.sources.github import GitHubSource
    from openjarvis.reliability.sources.vercel import VercelSource
    from openjarvis.reliability.verify import Verifier

    github = next((s for s in sources if isinstance(s, GitHubSource)), None)
    vercel = next((s for s in sources if isinstance(s, VercelSource)), None)

    def _preview(branch: str) -> str:
        if vercel is None:
            return ""
        found = vercel.find_preview_deployment(branch)
        return found["url"] if found else ""

    return RepairLoop(
        agent=ClaudeCliAgent(),
        policy=_build_policy(config),
        verifier=Verifier(evidence_dir=_evidence_dir(config)),
        store=store,
        workspace=rc.repair.workspace,
        test_command=rc.repair.test_command,
        github=github,
        preview_lookup=_preview if vercel is not None else None,
        protected_paths=list(rc.policy.protected_paths),
    )


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


@reliability.command("watch")
@click.option("--once", is_flag=True, help="Run one pass over every check and exit.")
@click.option(
    "--poll-interval", default=5.0, show_default=True, help="Seconds between ticks."
)
def reliability_watch(once: bool, poll_interval: float) -> None:
    """Run the monitoring loop in the foreground."""
    console = Console()
    config = _load_config()
    rc = config.reliability

    if not rc.site.base_url:
        console.print("[red]No site configured; set [reliability.site] base_url.[/red]")
        raise SystemExit(1)

    store = _get_store(config)
    try:
        monitor = _build_monitor(config, store)
        if not monitor.checks:
            console.print(
                "[yellow]Nothing to monitor: no enabled probes and no enabled "
                "sources.[/yellow]"
            )
            raise SystemExit(1)

        console.print(
            f"JARVIS monitoring {escape(rc.site.base_url)} "
            f"({len(monitor.checks)} check(s))."
        )
        if rc.repair.enabled:
            console.print(
                f"[yellow]Automated repair is ENABLED "
                f"(deploy_mode={escape(rc.policy.deploy_mode)}).[/yellow]"
            )
        else:
            console.print("[dim]Automated repair is disabled; monitoring only.[/dim]")

        if once:
            ran = monitor.tick()
            console.print(f"Ran {ran} check(s).")
        else:
            try:
                monitor.run_forever(poll_interval=poll_interval)
            except KeyboardInterrupt:
                console.print("\nStopped.")

        stats = monitor.health()
        console.print(
            f"Incidents opened: {stats['incidents_opened']} · "
            f"recovered: {stats['incidents_recovered']} · "
            f"recurrences: {stats['recurrences']} · "
            f"suppressed: {stats['suppressed']}"
        )
    finally:
        store.close()


@reliability.group("probe")
def probe_group() -> None:
    """Inspect and run website probes."""


@probe_group.command("list")
def probe_list() -> None:
    """List the probe specs JARVIS can run."""
    from openjarvis.reliability.probes.spec import ProbeSpecError, load_probes

    console = Console()
    config = _load_config()
    directory = _probe_dir(config)
    try:
        specs = load_probes(directory, strict=True)
    except ProbeSpecError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise SystemExit(1) from exc

    if not specs:
        console.print(f"[dim]No probe specs in {escape(str(directory))}.[/dim]")
        console.print(
            "[dim]Copy the examples from configs/reliability/probes/ to get "
            "started.[/dim]"
        )
        return

    table = Table(title=f"Probes ({directory})")
    table.add_column("ID", style="bold")
    table.add_column("Runner")
    table.add_column("Severity")
    table.add_column("Component")
    table.add_column("Schedule")
    table.add_column("Enabled")
    for spec in specs:
        table.add_row(
            spec.id,
            spec.runner,
            _severity_text(spec.severity.value),
            escape(spec.component),
            f"{spec.schedule_type}:{spec.schedule_value}",
            "[green]yes[/]" if spec.enabled else "[dim]no[/]",
        )
    console.print(table)


@probe_group.command("show")
@click.argument("probe_id")
def probe_show(probe_id: str) -> None:
    """Show a probe's workflow as human-readable reproduction steps."""
    from openjarvis.reliability.probes.spec import load_probes

    console = Console()
    config = _load_config()
    specs = {s.id: s for s in load_probes(_probe_dir(config))}
    spec = specs.get(probe_id)
    if spec is None:
        console.print(f"[red]No such probe: {escape(probe_id)}[/red]")
        raise SystemExit(1)

    console.print(
        Panel(
            escape(spec.description or spec.expectation_summary()),
            title=f"{spec.id} — {escape(spec.display_name)}",
        )
    )
    meta = Table(show_header=False, box=None)
    meta.add_column("key", style="dim")
    meta.add_column("value")
    meta.add_row("Runner", spec.runner)
    meta.add_row("Component", escape(spec.component))
    meta.add_row("Severity", _severity_text(spec.severity.value))
    meta.add_row("Schedule", f"{spec.schedule_type}:{spec.schedule_value}")
    meta.add_row("Enabled", "yes" if spec.enabled else "no")
    meta.add_row("Mutating", "yes" if spec.mutating else "no")
    if spec.credentials:
        # Names only — this is the whole point of the indirection.
        meta.add_row("Credentials", ", ".join(sorted(spec.credentials.values())))
    console.print(meta)

    console.print("\n[bold]Steps[/bold]")
    for index, step in enumerate(spec.repro_steps(), start=1):
        console.print(f"  {index}. {escape(step)}")


@probe_group.command("run")
@click.argument("probe_id")
@click.option("--base-url", default="", help="Override the configured site URL.")
def probe_run(probe_id: str, base_url: str) -> None:
    """Run one probe now and report what happened."""
    from openjarvis.reliability.probes.executor import escalate_severity
    from openjarvis.reliability.probes.spec import load_probes

    console = Console()
    config = _load_config()
    specs = {s.id: s for s in load_probes(_probe_dir(config))}
    spec = specs.get(probe_id)
    if spec is None:
        console.print(f"[red]No such probe: {escape(probe_id)}[/red]")
        raise SystemExit(1)

    if base_url:
        config.reliability.site.base_url = base_url
    if not config.reliability.site.base_url and not spec.url.startswith("http"):
        console.print(
            "[red]No site configured; set [reliability.site] base_url or pass "
            "--base-url.[/red]"
        )
        raise SystemExit(1)

    executor = _build_executor(config)
    console.print(f"Running [bold]{escape(spec.id)}[/bold]...")
    result = executor.run(spec)

    if result.success:
        console.print(
            f"[green]PASS[/green] in {result.duration_seconds:.2f}s "
            f"({result.steps_completed} step(s))"
        )
        return

    severity = escalate_severity(spec, result)
    console.print(
        f"[red]FAIL[/red] ({escape(result.failure_kind)}) "
        f"→ severity {_severity_text(severity.value)}"
    )
    console.print(f"  {escape(result.error)}")
    if result.final_url:
        console.print(f"  Final URL: {escape(result.final_url)}")
    if result.evidence:
        console.print("\n[bold]Evidence[/bold]")
        for item in result.evidence:
            location = f" → {escape(item.artifact_path)}" if item.artifact_path else ""
            console.print(f"  [{item.kind.value}] {escape(item.summary)}{location}")
    raise SystemExit(2)


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
