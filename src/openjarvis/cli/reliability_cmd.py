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
        # expanduser because "~/.openjarvis/reliability/probes" is what
        # docs/JARVIS_LIVE_SETUP.md tells an operator to write, and TOML has no
        # notion of a home directory. Without this the path resolves relative to
        # the working directory, the probes are silently not found, and JARVIS
        # reports "no probes configured" while looking straight at them.
        return Path(configured).expanduser()
    return get_config_dir() / "reliability" / "probes"


def _evidence_dir(config: Any) -> str:
    """Resolve the evidence-artifact directory."""
    from pathlib import Path

    from openjarvis.core.paths import get_config_dir

    configured = getattr(config.reliability.probes, "evidence_dir", "")
    if configured:
        return str(Path(configured).expanduser())
    return str(get_config_dir() / "reliability" / "evidence")


def _build_executor(config: Any, *, base_url_override: str = "") -> Any:
    """Build a :class:`ProbeExecutor` wired from user config.

    ``base_url_override`` is what ``--base-url`` passed on the command line.
    It wins over both config and the environment: an operator naming a URL in
    the invocation has said something more specific than either, and silently
    probing somewhere else is the one behaviour a target override must never
    have.
    """
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

    # Through resolve_target, not rc.site.base_url directly: the diagnostic
    # already resolves the target that way, and $TARGET_PRODUCTION_URL is
    # documented as the override for pointing a one-off run at staging. Reading
    # config here and the environment there meant `probe run` could hit
    # production while `live-diagnostic` reported it had checked staging.
    from openjarvis.reliability.target import resolve_target

    return ProbeExecutor(
        base_url=(
            base_url_override
            or resolve_target(config).production_url
            or rc.site.base_url
        ),
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
                actions_token_env=rc.github.actions_token_env,
                monitor_actions=rc.github.monitor_actions,
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


def _stop_flag_path(config: Any) -> Any:
    """Where the emergency stop flag lives.

    A file rather than a signal or a socket: it survives a restart, which is the
    behaviour an operator wants from a stop they pulled in a panic. JARVIS must
    not quietly come back on because the host rebooted.
    """
    from pathlib import Path

    from openjarvis.core.config import get_config_dir

    configured = config.reliability.db_path
    if configured:
        return Path(configured).expanduser().parent / "STOPPED"
    return get_config_dir() / "reliability" / "STOPPED"


def _build_monitor(config: Any, store: Any) -> Any:
    """Assemble the full monitoring stack from config."""
    return _build_supervised_monitor(config, store)[0]


def _build_supervised_monitor(config: Any, store: Any) -> tuple:
    """Build the monitor and its supervisor together.

    They are built as a pair because each needs the other: the supervisor gates
    the monitor's repairs, and the supervisor reports the monitor's health.
    """
    from openjarvis.reliability.detector import Detector
    from openjarvis.reliability.flapping import FlappingDetector
    from openjarvis.reliability.monitor import ReliabilityMonitor
    from openjarvis.reliability.probes.spec import load_probes
    from openjarvis.reliability.watch import RepairGate, WatchSupervisor

    rc = config.reliability
    notifier = _build_notifier(config)
    specs = load_probes(_probe_dir(config))
    sources = _build_sources(config)

    detector = Detector(
        store,
        environment=rc.site.environment,
        notifier=notifier,
    )
    supervisor = WatchSupervisor(
        monitor=None,  # set below, once the monitor exists
        store=store,
        gate=RepairGate(
            max_concurrent=rc.watch.max_concurrent_repairs,
            cooldown_seconds=float(rc.watch.cooldown_seconds),
        ),
        flapping=FlappingDetector(
            window=rc.flapping.window,
            failure_threshold=rc.flapping.failure_threshold,
            min_samples=rc.flapping.min_samples,
        )
        if rc.flapping.enabled
        else FlappingDetector(window=2, failure_threshold=10**6, min_samples=10**6),
        notifier=notifier,
        interval_seconds=float(rc.watch.interval_seconds),
    )
    monitor = ReliabilityMonitor(
        detector=detector,
        executor=_build_executor(config),
        specs=specs,
        sources=sources,
        repair_loop=_build_repair_loop(config, store, sources),
        notifier=notifier,
        supervisor=supervisor,
    )
    supervisor.monitor = monitor
    return monitor, supervisor


def _build_repair_loop(config: Any, store: Any, sources: list) -> Any:
    """Build the repair loop, or ``None`` when repair is disabled."""
    rc = config.reliability
    if not rc.repair.enabled:
        return None

    from openjarvis.reliability.checks import CheckSuite
    from openjarvis.reliability.code_agent import ClaudeCliAgent
    from openjarvis.reliability.repair import RepairLoop
    from openjarvis.reliability.scope import ScopeLimits
    from openjarvis.reliability.sources.github import GitHubSource
    from openjarvis.reliability.sources.vercel import VercelSource
    from openjarvis.reliability.verify import Verifier
    from openjarvis.reliability.workspace import RepairWorkspace

    github = next((s for s in sources if isinstance(s, GitHubSource)), None)
    vercel = next((s for s in sources if isinstance(s, VercelSource)), None)

    if not rc.repair.workspace:
        raise click.ClickException(
            "[reliability.repair] enabled = true but workspace is unset. "
            "Point it at a checkout of the target repository; JARVIS cuts an "
            "isolated worktree from it for every repair and never commits to it."
        )

    def _preview(branch: str) -> str:
        if vercel is None:
            return ""
        found = vercel.find_preview_deployment(branch)
        return found["url"] if found else ""

    def _preview_build_logs(branch: str) -> str:
        """Why the preview never appeared — fed back to the coding agent."""
        if vercel is None:
            return ""
        for deployment in vercel.list_deployments(limit=20, target="preview"):
            if deployment.get("branch") == branch:
                return vercel.get_build_logs(deployment.get("id", ""))
        return ""

    return RepairLoop(
        agent=ClaudeCliAgent(
            executable=rc.repair.agent_executable,
            allowed_tools=list(rc.repair.agent_allowed_tools),
            disallowed_tools=list(rc.repair.agent_disallowed_tools),
        ),
        policy=_build_policy(config),
        verifier=Verifier(evidence_dir=_evidence_dir(config)),
        store=store,
        workspace=rc.repair.workspace,
        workspace_manager=RepairWorkspace(
            repo_path=rc.repair.workspace,
            root=_worktree_root(config),
            branch_prefix=rc.github.branch_prefix,
            keep_on_failure=rc.repair.keep_failed_worktrees,
            git_identity=(
                (rc.repair.git_author_name, rc.repair.git_author_email)
                if rc.repair.git_author_name and rc.repair.git_author_email
                else None
            ),
        ),
        test_command=rc.repair.test_command,
        checks=CheckSuite.from_config(
            test_command=rc.repair.test_command,
            lint_command=rc.repair.lint_command,
            typecheck_command=rc.repair.typecheck_command,
            build_command=rc.repair.build_command,
            timeout=rc.repair.test_timeout_seconds,
        ),
        scope_limits=ScopeLimits(
            max_files=rc.repair.max_changed_files,
            max_lines_changed=rc.repair.max_changed_lines,
        ),
        github=github,
        # Cut the worktree from the configured base branch, not from whatever
        # the workspace checkout happens to be pointing at. The default was
        # "HEAD", so a checkout left on a feature branch would silently base
        # every repair on that branch — and the resulting PR, opened against
        # base_branch, would carry someone else's unrelated commits.
        base_ref=rc.github.base_branch,
        base_branch=rc.github.base_branch,
        preview_lookup=_preview if vercel is not None else None,
        preview_logs=_preview_build_logs if vercel is not None else None,
        preview_wait_seconds=rc.repair.preview_wait_seconds,
        protected_paths=list(rc.policy.protected_paths),
        notifier=_build_notifier(config),
    )


def _worktree_root(config: Any) -> str:
    """Where isolated repair worktrees are created."""
    from pathlib import Path

    from openjarvis.core.config import get_config_dir

    configured = config.reliability.repair.worktree_root
    if configured:
        return str(Path(configured).expanduser())
    return str(get_config_dir() / "reliability" / "worktrees")


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

    # Integration reachability, without contacting anything: configuration and
    # local availability only. Whether an integration actually *works* is what
    # `live-diagnostic` answers, and it says so honestly rather than guessing.
    import shutil as _shutil

    stop_flag = _stop_flag_path(config)
    availability = Table(title="Integrations", show_header=False, box=None)
    availability.add_column("key", style="dim")
    availability.add_column("value")

    def _configured(ok: bool, yes: str = "CONFIGURED", no: str = "NOT CONFIGURED"):
        return yes if ok else f"[dim]{no}[/]"

    claude_ok = _shutil.which(rc.repair.agent_executable) is not None
    availability.add_row(
        "Claude CLI",
        "AVAILABLE" if claude_ok else "[red]UNAVAILABLE[/]",
    )
    availability.add_row("GitHub", _configured(bool(rc.github.repo)))
    availability.add_row("Vercel", _configured(bool(rc.vercel.project_id)))
    availability.add_row("Supabase", _configured(bool(rc.supabase.project_ref)))
    availability.add_row("Website", _configured(bool(rc.site.base_url)))
    availability.add_row(
        "Telegram",
        _configured(rc.notify.enabled, "CONNECTED", "DISCONNECTED"),
    )
    availability.add_row(
        "Emergency stop",
        "[bold red]ENGAGED[/]" if stop_flag.exists() else "[dim]not engaged[/]",
    )
    console.print()
    console.print(availability)

    # The four questions an operator actually has, answered unambiguously.
    safety = Table(title="Production safety", show_header=False, box=None)
    safety.add_column("key", style="dim")
    safety.add_column("value")
    safety.add_row("Production deployment", "DISABLED")
    safety.add_row("Automatic PR merge", "DISABLED")
    safety.add_row(
        "Default branch push",
        "[red]ENABLED[/]" if rc.policy.allow_push_to_default_branch else "DISABLED",
    )
    safety.add_row(
        "Supabase writes",
        "[red]ENABLED[/]" if rc.supabase.allow_production_writes else "DISABLED",
    )
    console.print()
    console.print(safety)

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
        in_repair = [
            i for i in incidents if i.state.value in ("FIXING", "TESTING", "VERIFYING")
        ]
        console.print(f"Repairs in flight: {len(in_repair)}")
        needs_recovery = [i for i in incidents if i.state.value == "RECOVERY_REQUIRED"]
        if needs_recovery:
            console.print(
                "[yellow]"
                + escape(
                    f"{len(needs_recovery)} incident(s) need manual recovery: "
                    + ", ".join(i.id for i in needs_recovery)
                )
                + "[/yellow]"
            )

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
    """Run the 24/7 monitoring loop in the foreground.

    Refuses to start in a configuration where automatic repair could reach
    production. Prints every safety interlock before the first check.
    """
    console = Console()
    config = _load_config()
    rc = config.reliability

    if not rc.site.base_url:
        console.print("[red]No site configured; set [reliability.site] base_url.[/red]")
        raise SystemExit(1)

    from openjarvis.reliability.watch import (
        UnsafeConfigurationError,
        assert_safe_to_start,
        startup_banner,
    )

    stop_flag = _stop_flag_path(config)
    if stop_flag.exists():
        console.print(
            "[bold red]JARVIS is stopped.[/bold red]\n\n"
            "An emergency stop is in effect. Nothing has been lost — incidents, "
            "evidence and audit records are intact.\n"
            f"Remove {escape(str(stop_flag))} to start again."
        )
        raise SystemExit(3)

    # Before anything else: refuse to run in a configuration that could reach
    # production. A dangerous combination is best discovered here, not at 3am.
    try:
        assert_safe_to_start(config)
    except UnsafeConfigurationError as exc:
        console.print(f"[bold red]{escape(str(exc))}[/bold red]")
        raise SystemExit(2) from exc

    console.print(escape(startup_banner(config)))

    store = _get_store(config)
    try:
        monitor, supervisor = _build_supervised_monitor(config, store)
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

        # Crash recovery: park anything that was mid-repair when we last
        # stopped. Nothing is resumed automatically.
        parked = supervisor.recover_interrupted_repairs()
        for incident in parked:
            console.print(
                f"[yellow]{escape(incident.id)} was interrupted mid-repair and "
                "now needs manual recovery (RECOVERY_REQUIRED).[/yellow]"
            )

        if once:
            ran = monitor.tick()
            console.print(f"Ran {ran} check(s).")
        else:
            try:
                supervisor.run_forever()
            except KeyboardInterrupt:
                console.print("\nStopped.")
                supervisor.stop()

        stats = monitor.health()
        console.print(
            f"Incidents opened: {stats['incidents_opened']} · "
            f"recovered: {stats['incidents_recovered']} · "
            f"recurrences: {stats['recurrences']} · "
            f"suppressed: {stats['suppressed']} · "
            f"flapping: {stats['flapping']} · "
            f"repairs deferred: {stats['repairs_deferred']}"
        )
    finally:
        store.close()


@reliability.command("incidents")
@click.option("--open", "open_only", is_flag=True, help="Only unresolved incidents.")
@click.option("--limit", default=20, show_default=True, help="How many to show.")
def reliability_incidents(open_only: bool, limit: int) -> None:
    """Show incidents alongside the state of monitoring and repair.

    The same data as ``incident list``, plus the safety context a human needs
    to act on it: is monitoring running, is repair armed, can anything reach
    production.
    """
    console = Console()
    config = _load_config()
    rc = config.reliability
    store = _get_store(config)
    try:
        header = Table(title="JARVIS", show_header=False, box=None)
        header.add_row("Monitoring", "armed" if rc.watch.enabled else "manual")
        header.add_row("Automatic repair", "ON" if rc.repair.enabled else "OFF")
        header.add_row("Production deployment", "OFF")
        header.add_row("Automatic merge", "OFF")
        header.add_row("Deploy mode", escape(rc.policy.deploy_mode))
        console.print(header)

        incidents = store.list(open_only=open_only, limit=limit)
        if not incidents:
            console.print("\nNo incidents found.")
            return

        table = Table(title="Incidents")
        for column in ("ID", "Severity", "State", "Component", "Seen", "Title"):
            table.add_column(column)
        for incident in incidents:
            table.add_row(
                incident.id,
                _severity_text(incident.severity.value),
                _state_text(incident.state.value),
                escape(incident.component),
                str(incident.occurrences),
                escape(incident.title[:60]),
            )
        console.print(table)

        needs_recovery = [i for i in incidents if i.state.value == "RECOVERY_REQUIRED"]
        if needs_recovery:
            console.print(
                "\n[yellow]"
                + escape(
                    f"{len(needs_recovery)} incident(s) were interrupted mid-repair "
                    "and will NOT resume automatically: "
                    + ", ".join(i.id for i in needs_recovery)
                )
                + "[/yellow]"
            )
    finally:
        store.close()


@reliability.command("repair")
@click.argument("incident_id")
@click.option(
    "--yes",
    is_flag=True,
    help="Skip the confirmation prompt.",
)
def reliability_repair(incident_id: str, yes: bool) -> None:
    """Run one repair attempt for an incident, explicitly.

    This is the deliberate counterpart to autonomous repair: it is how an owner
    resumes an incident parked in RECOVERY_REQUIRED, or repairs one that policy
    would not have picked up on its own. It still cannot merge, deploy, or push
    to the default branch — those refusals live in the policy, not in this
    command.
    """
    console = Console()
    config = _load_config()
    rc = config.reliability

    if not rc.repair.enabled:
        console.print(
            "[red]Automated repair is disabled. Set [reliability.repair] "
            "enabled = true to allow it.[/red]"
        )
        raise SystemExit(1)

    store = _get_store(config)
    try:
        incident = store.get(incident_id)
        if incident is None:
            console.print(f"[red]No such incident: {escape(incident_id)}[/red]")
            raise SystemExit(1)

        from openjarvis.reliability.probes.spec import load_probes

        spec = next(
            (s for s in load_probes(_probe_dir(config)) if s.id == incident.probe_id),
            None,
        )
        if spec is None:
            console.print(
                f"[red]No probe spec '{escape(incident.probe_id)}' for "
                f"{escape(incident.id)}. Verification re-runs the probe that "
                "detected the failure, so a repair cannot be verified without "
                "it.[/red]"
            )
            raise SystemExit(1)

        console.print(f"Incident: {escape(incident.id)} — {escape(incident.title)}")
        console.print(f"State: {_state_text(incident.state.value)}")
        console.print("Endpoint: a pull request. Production is never modified.")
        if not yes and not click.confirm("Start a repair attempt?", default=False):
            console.print("Cancelled.")
            return

        sources = _build_sources(config)
        loop = _build_repair_loop(config, store, sources)
        if loop is None:
            console.print("[red]Could not build the repair loop.[/red]")
            raise SystemExit(1)

        outcome = loop.run(incident, spec)
        console.print(
            f"\nResolved: {'yes' if outcome.resolved else 'no'} · "
            f"attempts: {outcome.attempts} · state: {outcome.final_state.value}"
        )
        if outcome.reason:
            console.print(f"Reason: {escape(outcome.reason)}")
        if outcome.pull_request_url:
            console.print(f"Pull request: {escape(outcome.pull_request_url)}")
        raise SystemExit(0 if outcome.resolved else 1)
    finally:
        store.close()


@reliability.command("stop")
def reliability_stop() -> None:
    """Emergency stop: block new monitoring cycles and new repairs.

    Deliberately non-destructive. Incidents, evidence, audit records, branches
    and worktrees are all left exactly as they are — stopping must be a safe
    thing to do in a panic, which means it must never delete anything.
    """
    console = Console()
    config = _load_config()
    path = _stop_flag_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"stopped_at={__import__('datetime').datetime.now().isoformat()}\n")
    console.print(
        "JARVIS STOPPED\n\n"
        "Monitoring:   OFF\n"
        "New repairs:  BLOCKED\n"
        "Production:   UNCHANGED\n\n"
        "Incidents, evidence, branches and audit records are untouched.\n"
        f"Remove {escape(str(path))} to allow JARVIS to start again."
    )


@reliability.command("report")
@click.argument("incident_id")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def reliability_report(incident_id: str, as_json: bool) -> None:
    """Print the post-incident report for an incident."""
    import json as _json

    from openjarvis.reliability.report import build_report

    console = Console()
    config = _load_config()
    store = _get_store(config)
    try:
        incident = store.get(incident_id)
        if incident is None:
            console.print(f"[red]No such incident: {escape(incident_id)}[/red]")
            raise SystemExit(1)
        report = build_report(incident)
        if as_json:
            click.echo(_json.dumps(report.to_dict(), indent=2))
        else:
            console.print(escape(report.render()))
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

    executor = _build_executor(config, base_url_override=base_url)
    console.print(f"Running [bold]{escape(spec.id)}[/bold]...")
    result = executor.run(spec)

    if result.success:
        console.print(
            f"[green]PASS[/green] in {result.duration_seconds:.2f}s "
            f"({result.steps_completed} step(s))"
        )
        # A pass that filtered something is not the same as a pass that saw
        # nothing, and an operator who cannot tell them apart has no way to
        # notice a pattern that has quietly grown too broad.
        suppressed_console = int(result.metadata.get("suppressed_console_count", 0))
        suppressed_requests = int(result.metadata.get("suppressed_request_count", 0))
        if suppressed_console or suppressed_requests:
            parts = []
            if suppressed_console:
                parts.append(f"{suppressed_console} console error(s)")
            if suppressed_requests:
                parts.append(f"{suppressed_requests} failed request(s)")
            console.print(f"  [dim]ignored as known noise: {', '.join(parts)}[/dim]")
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
@click.option(
    "--connectivity",
    is_flag=True,
    help="Also check whether this network can reach each integration host.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def reliability_doctor(connectivity: bool, as_json: bool) -> None:
    """Validate configuration and credentials. Contacts nothing.

    Reports which credentials are *present* by environment-variable name.  It
    never reads, prints or transmits a credential value.
    """
    import json as json_module

    from openjarvis.reliability.target import (
        connectivity_report,
        credential_report,
        resolve_target,
    )

    console = Console()
    config = _load_config()
    target = resolve_target(config)
    credentials = credential_report(config)
    # Contacting nothing is the default. --connectivity opts in to unauthenticated
    # reachability checks, which is the one question a credential report cannot
    # answer: "can this machine even get there?"
    reachability = connectivity_report(target) if connectivity else []

    if as_json:
        # click.echo, not console.print_json: Rich syntax-highlights JSON, and
        # emits the escape codes whenever colour is enabled — a terminal, or
        # $FORCE_COLOR. `--json` means a machine is reading this, so
        # `jarvis reliability doctor --json | jq` must not depend on how the
        # operator's terminal is configured.
        click.echo(
            json_module.dumps(
                {
                    "target": target.to_dict(),
                    "connectivity": reachability,
                    "credentials": [c.to_dict() for c in credentials],
                    "safety": _safety_snapshot(config),
                },
                indent=2,
            )
        )
    else:
        table = Table(title="JARVIS target", show_header=False, box=None)
        table.add_column("key", style="dim")
        table.add_column("value")
        for label, value in target.to_dict().items():
            table.add_row(label, escape(str(value)) or "[dim]not set[/]")
        console.print(table)

        console.print()
        creds = Table(title="Credentials (names only — values are never read)")
        creds.add_column("Integration")
        creds.add_column("Status")
        creds.add_column("Environment variable")
        creds.add_column("Needed for", style="dim")
        for status in credentials:
            if status.present:
                mark = "[green]🟢 configured[/]"
            elif status.optional:
                mark = "[dim]⚫ optional, not set[/]"
            else:
                mark = f"[red]🔴 missing ${escape(status.env_name)}[/]"
            creds.add_row(
                status.label,
                mark,
                escape(status.env_name),
                escape(status.required_for),
            )
        console.print(creds)

        if reachability:
            net = Table(title="Network reachability (no credentials sent)")
            for column in ("Integration", "Host", "Result", "Detail"):
                net.add_column(column)
            _style = {
                "REACHABLE": "green",
                "BLOCKED": "red",
                "UNKNOWN": "yellow",
                "NOT_CONFIGURED": "dim",
            }
            for row in reachability:
                net.add_row(
                    escape(row["name"]),
                    escape(row["host"] or "—"),
                    f"[{_style.get(row['state'], 'white')}]{escape(row['state'])}[/]",
                    escape(row["detail"][:70]),
                )
            console.print(net)
            blocked = [r["name"] for r in reachability if r["state"] == "BLOCKED"]
            if blocked:
                console.print(
                    "[yellow]"
                    + escape(
                        f"{len(blocked)} host(s) unreachable from this machine: "
                        + ", ".join(blocked)
                        + ". This is a network limitation, not an application "
                        "failure — JARVIS will report those integrations BLOCKED "
                        "and will not open incidents for them."
                    )
                    + "[/yellow]\n"
                )

        console.print()
        safety = Table(title="Safety interlocks", show_header=False, box=None)
        safety.add_column("key", style="dim")
        safety.add_column("value")
        for label, value in _safety_snapshot(config).items():
            style = "green" if value in ("OFF", "pr_only", "active") else "yellow"
            safety.add_row(label, f"[{style}]{escape(str(value))}[/]")
        console.print(safety)

    problems = [
        f"missing ${c.env_name} ({c.required_for})"
        for c in credentials
        if not c.present and not c.optional
    ]
    problems += [f"missing ${name}" for name in target.missing()]
    for note in (target.url_problem(), target.repository_problem()):
        if note:
            problems.append(note)

    if problems and not as_json:
        console.print()
        for problem in problems:
            console.print(f"[red]✗[/red] {escape(problem)}")
    if problems:
        raise SystemExit(1)
    if not as_json:
        console.print("\n[green]✓[/green] Configuration and credentials look complete.")


def _safety_snapshot(config: Any) -> dict:
    """The interlocks a reader most wants confirmed, as plain strings."""
    rc = config.reliability
    return {
        "Automatic repair": "ON" if rc.repair.enabled else "OFF",
        "Deploy mode": rc.policy.deploy_mode,
        "Push to default branch": (
            "ON" if rc.policy.allow_push_to_default_branch else "OFF"
        ),
        "Supabase production writes": (
            "ON" if rc.supabase.allow_production_writes else "OFF"
        ),
        "SQL write guard": "active",
    }


def _render_check(console: Any, check: Any, *, indent: int = 0) -> None:
    """Print one check and its capabilities."""
    pad = "  " * indent
    console.print(
        f"{pad}{check.state.icon} [bold]{escape(check.name)}[/bold] "
        f"{escape(check.state.value)}"
        + (f" — {escape(check.summary)}" if check.summary else "")
    )
    for capability in check.capabilities.values():
        console.print(
            f"{pad}    {capability.state.icon} {escape(capability.name)}: "
            f"{escape(capability.summary or capability.state.value)}"
        )
        if capability.remediation and not capability.state.was_checked:
            console.print(f"{pad}       [dim]→ {escape(capability.remediation)}[/dim]")
    if check.remediation and not check.state.was_checked:
        console.print(f"{pad}    [dim]→ {escape(check.remediation)}[/dim]")


@reliability.command("live-diagnostic")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option(
    "--no-probes", is_flag=True, help="Skip browser probes (integrations only)."
)
@click.option(
    "--no-incidents", is_flag=True, help="Report only; do not open incidents."
)
@click.option("--notify/--no-notify", default=False, help="Send a Telegram summary.")
def reliability_live_diagnostic(
    as_json: bool, no_probes: bool, no_incidents: bool, notify: bool
) -> None:
    """Run every read-only check against the configured target.

    Modifies nothing: no deployments, no branches, no database writes, no code
    changes.  Exit status is 0 healthy, 1 something failed, 2 incomplete.
    """
    import json as json_module

    from openjarvis.reliability.diagnostic import LiveDiagnostic

    console = Console()
    config = _load_config()
    store = _get_store(config)
    notifier = _build_notifier(config) if notify else None

    try:
        diagnostic = LiveDiagnostic(config, store=store, notifier=notifier)
        report = diagnostic.run(
            include_probes=not no_probes, open_incidents=not no_incidents
        )

        if as_json:
            # Plain, for the same reason as `doctor --json` above: machine
            # output must not carry terminal styling.
            click.echo(json_module.dumps(report.to_dict(), indent=2))
        else:
            console.print("\n[bold]JARVIS FULL DIAGNOSTIC[/bold]\n")
            for check in report.checks:
                _render_check(console, check)
                console.print()

            console.print(
                f"[bold]Overall:[/bold] {report.overall.state.icon} "
                f"{escape(report.overall.state.value)}"
            )

            blind = report.blind_spots()
            if blind:
                console.print(
                    f"\n[yellow]Not verified ({len(blind)}) — these are blind "
                    "spots, not passes:[/yellow]"
                )
                for spot in blind:
                    console.print(f"  [yellow]•[/yellow] {escape(spot)}")

            if report.incidents_opened:
                console.print(
                    f"\n[red]Incidents opened:[/red] "
                    f"{escape(', '.join(report.incidents_opened))}"
                )
            else:
                console.print("\nIncidents opened: 0")

            if report.audit_chain_intact is True:
                console.print("Audit chain: [green]intact[/green]")
            elif report.audit_chain_intact is False:
                console.print("Audit chain: [bold red]BROKEN[/bold red]")

            console.print()
            for label, value in _safety_snapshot(config).items():
                console.print(f"[dim]{escape(label)}:[/dim] {escape(str(value))}")

        if notify and notifier is not None:
            _notify_diagnostic(notifier, report, config)
    finally:
        store.close()

    raise SystemExit(report.exit_code)


def _notify_diagnostic(notifier: Any, report: Any, config: Any) -> None:
    """Send a diagnostic summary. Redaction happens inside the router."""
    from openjarvis.reliability.types import Severity

    lines = ["🤖 JARVIS", "", "Live diagnostic complete.", ""]
    for check in report.checks:
        lines.append(f"{check.state.icon} {check.name}: {check.state.value}")
    lines += ["", f"Overall: {report.overall.state.value}"]
    if report.incidents_opened:
        lines.append(f"Incidents: {', '.join(report.incidents_opened)}")
    safety = _safety_snapshot(config)
    lines += [
        "",
        f"Automatic repair: {safety['Automatic repair']}",
        f"Production deployment: {safety['Deploy mode']}",
    ]
    notifier.notify("\n".join(lines), severity=Severity.MEDIUM)


@reliability.command("notify-test")
def reliability_notify_test() -> None:
    """Send one test notification through the configured channel."""
    from openjarvis.reliability.target import resolve_target
    from openjarvis.reliability.types import Severity

    console = Console()
    config = _load_config()
    rc = config.reliability

    if not rc.notify.enabled:
        console.print(
            "[red]Notifications are disabled "
            "([reliability.notify] enabled = false).[/red]"
        )
        raise SystemExit(1)

    target = resolve_target(config)
    safety = _safety_snapshot(config)
    message = "\n".join(
        [
            "🤖 JARVIS",
            "",
            "Connection test successful.",
            "",
            f"Target: {target.repository or target.production_url or 'not configured'}",
            f"Environment: {target.environment}",
            "",
            f"Automatic repair: {safety['Automatic repair']}",
            f"Production deployment: {safety['Deploy mode']}",
            f"Supabase writes: {safety['Supabase production writes']}",
        ]
    )
    notifier = _build_notifier(config)
    sent = notifier.notify(message, severity=Severity.MEDIUM)
    if sent:
        console.print("[green]Test notification sent.[/green]")
    else:
        console.print(
            "[red]Notification was not sent (check the channel configuration "
            "and rate limits).[/red]"
        )
        raise SystemExit(1)


@reliability.command("analyze")
@click.argument("incident_id")
@click.option("--out", default="", help="Write the prompt to a file instead of stdout.")
@click.option("--run", "invoke", is_flag=True, help="Invoke the claude CLI read-only.")
def reliability_analyze(incident_id: str, out: str, invoke: bool) -> None:
    """Build a diagnostic-only Claude Code prompt for an incident.

    The prompt asks for a root-cause analysis and explicitly forbids modifying
    files, deploying, or touching data.  Nothing is repaired by this command.
    """
    from openjarvis.reliability.analysis import build_analysis_prompt
    from openjarvis.reliability.briefing import BriefingRefusedError

    console = Console()
    config = _load_config()
    store = _get_store(config)
    try:
        incident = store.get(incident_id)
        if incident is None:
            console.print(f"[red]No such incident: {escape(incident_id)}[/red]")
            raise SystemExit(1)
        try:
            prompt = build_analysis_prompt(
                incident,
                protected_paths=list(config.reliability.policy.protected_paths),
            )
        except BriefingRefusedError as exc:
            console.print(
                f"[bold red]Refused to build a prompt:[/bold red] {escape(str(exc))}"
            )
            raise SystemExit(1) from exc
    finally:
        store.close()

    if prompt.injection_findings:
        console.print(
            "[yellow]Note: the evidence contains text matching injection "
            "patterns; it is fenced in the prompt.[/yellow]"
        )

    if out:
        from pathlib import Path

        Path(out).write_text(prompt.text, encoding="utf-8")
        console.print(
            f"Wrote the analysis prompt to {escape(out)} (hash {prompt.hash})"
        )
    elif not invoke:
        console.print(prompt.text)

    if invoke:
        from openjarvis.reliability.code_agent import ClaudeCliAgent

        workspace = config.reliability.repair.workspace
        if not workspace:
            console.print(
                "[red]Set [reliability.repair] workspace to a checkout of the "
                "target application first.[/red]"
            )
            raise SystemExit(1)
        # Read-only tool set: the agent may look, never edit.
        agent = ClaudeCliAgent(
            allowed_tools=["Read", "Grep", "Glob"],
            disallowed_tools=["Edit", "Write", "Bash", "WebFetch", "WebSearch"],
        )
        if not agent.available():
            console.print("[red]The claude CLI is not on PATH.[/red]")
            raise SystemExit(1)
        console.print("[dim]Running Claude Code in read-only analysis mode…[/dim]")
        result = agent.run(prompt.text, workspace=workspace, timeout=900)
        console.print(escape(result.claim or result.error))
        if result.changed_files:
            console.print(
                "[bold red]The agent modified files during a read-only "
                f"analysis: {escape(', '.join(result.changed_files))}[/bold red]"
            )
            raise SystemExit(1)
