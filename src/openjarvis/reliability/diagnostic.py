"""The live diagnostic — check everything, claim nothing you did not verify.

Runs each integration read-only and reports a :class:`CheckResult` tree. The
governing rule is in :mod:`openjarvis.reliability.health`: a check that could
not run reports ``UNKNOWN`` or ``NOT_CONFIGURED``, never ``HEALTHY``.

Every integration call is wrapped per-capability, because real tokens have
partial scopes. A GitHub token that can read commits but not Actions must
produce ``DEGRADED`` with a named blind spot — not an exception, and certainly
not a green light.

Nothing here writes: no deployments are triggered, no branches created, no SQL
executed beyond the read-only paths the guard already permits.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from openjarvis.reliability.health import CheckResult, HealthState, aggregate
from openjarvis.reliability.target import TargetConfig, resolve_target

logger = logging.getLogger(__name__)

__all__ = ["DiagnosticReport", "LiveDiagnostic"]

#: Exceptions that mean "could not determine", as opposed to "it is broken".
#: A permission error is a gap in JARVIS's own access, not a production fault.
_UNKNOWN_MARKERS = (
    "401",
    "403",
    "404",
    "not set",
    "no token_env",
    "circuit breaker",
    # A proxy refusing the tunnel is JARVIS's network, not the target's health.
    "proxyerror",
    "no conclusion can be drawn",
    "could not be read",
)


def _classify_exception(exc: BaseException) -> tuple[HealthState, str]:
    """Map an exception to a state and a human-readable reason.

    Permission and configuration problems are ``UNKNOWN`` — JARVIS cannot see,
    which is different from having seen something broken.
    """
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if any(marker in lowered for marker in _UNKNOWN_MARKERS):
        return HealthState.UNKNOWN, text
    if "timeout" in lowered or "connect" in lowered:
        return HealthState.UNKNOWN, text
    return HealthState.FAILED, text


def _capability(
    name: str,
    fn: Callable[[], Any],
    *,
    describe: Callable[[Any], str] = lambda v: "ok",
    remediation: str = "",
) -> CheckResult:
    """Run one capability probe, converting any failure into a state.

    Never raises. A capability that blows up is a reported blind spot, not a
    crashed diagnostic.
    """
    started = time.monotonic()
    result = CheckResult(name=name)
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001 - deliberate: classify everything
        state, reason = _classify_exception(exc)
        result.state = state
        result.summary = reason[:300]
        result.remediation = remediation
        logger.info("capability %s -> %s (%s)", name, state.value, reason[:200])
    else:
        result.state = HealthState.HEALTHY
        try:
            result.summary = describe(value)
        except Exception:  # pragma: no cover - describe must never break a check
            result.summary = "ok"
        result.facts = {"value": value} if isinstance(value, (int, str)) else {}
    result.duration_seconds = time.monotonic() - started
    return result


@dataclass
class DiagnosticReport:
    """Everything one diagnostic run established."""

    target: TargetConfig
    checks: List[CheckResult] = field(default_factory=list)
    overall: CheckResult = field(default_factory=lambda: CheckResult(name="overall"))
    incidents_opened: List[str] = field(default_factory=list)
    started_at: str = ""
    duration_seconds: float = 0.0
    audit_chain_intact: Optional[bool] = None

    @property
    def exit_code(self) -> int:
        """Process exit status.

        ``0`` healthy · ``1`` something failed · ``2`` incomplete (blind spots).
        An incomplete run is deliberately not ``0``: a green exit code from a
        run that checked nothing is the same lie as a green dashboard.
        """
        if self.overall.state is HealthState.HEALTHY:
            return 0
        if self.overall.state in (HealthState.FAILED, HealthState.DEGRADED):
            return 1
        return 2

    def blind_spots(self) -> List[str]:
        """Every capability across the report that produced no verdict."""
        spots: List[str] = []
        for check in self.checks:
            if not check.state.was_checked:
                spots.append(f"{check.name}: {check.summary or check.state.value}")
            for cap_name, cap in check.capabilities.items():
                if not cap.state.was_checked:
                    spots.append(f"{check.name}.{cap_name}: {cap.summary}")
        return spots

    def to_dict(self) -> Dict[str, Any]:
        """Serialize. Contains no secrets."""
        return {
            "target": self.target.to_dict(),
            "overall": self.overall.state.value,
            "checks": [c.to_dict() for c in self.checks],
            "incidents_opened": list(self.incidents_opened),
            "blind_spots": self.blind_spots(),
            "started_at": self.started_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "audit_chain_intact": self.audit_chain_intact,
            "exit_code": self.exit_code,
        }


class LiveDiagnostic:
    """Runs the read-only checks against the configured target.

    Parameters
    ----------
    config:
        A ``JarvisConfig``.
    store:
        Optional incident store. When supplied, genuine ``FAILED`` results open
        incidents; ``UNKNOWN`` results never do.
    factories:
        Injected constructors for the integrations, so tests can supply doubles
        without patching imports.
    """

    def __init__(
        self,
        config: Any,
        *,
        store: Any = None,
        notifier: Any = None,
        factories: Optional[Dict[str, Callable[[], Any]]] = None,
        executor: Any = None,
    ) -> None:
        self._config = config
        self._store = store
        self._notifier = notifier
        self._factories = factories or {}
        self._executor = executor
        self.target = resolve_target(config)

    # -- individual checks -------------------------------------------------

    def check_configuration(self) -> CheckResult:
        """Validate the target identifiers before anything is contacted."""
        result = CheckResult(name="configuration")
        target = self.target

        if not target.is_configured:
            result.state = HealthState.NOT_CONFIGURED
            result.summary = "no target application configured"
            result.remediation = (
                "Set TARGET_REPOSITORY, TARGET_PRODUCTION_URL, VERCEL_PROJECT and "
                "SUPABASE_PROJECT_REF (or the matching [reliability] config keys)."
            )
            return result

        problems = [p for p in (target.url_problem(), target.repository_problem()) if p]
        missing = target.missing()

        result.facts = target.to_dict()
        if problems:
            result.state = HealthState.FAILED
            result.summary = "; ".join(problems)
            return result
        if missing:
            result.state = HealthState.DEGRADED
            result.summary = f"partially configured; missing {', '.join(missing)}"
            result.remediation = f"Set: {', '.join(missing)}"
            return result

        result.state = HealthState.HEALTHY
        result.summary = f"target is {target.repository or target.production_url}"
        return result

    def check_github(self) -> CheckResult:
        """Read repository state. No writes, no branch creation."""
        result = CheckResult(name="github")
        rc = self._config.reliability
        if not self.target.repository:
            return CheckResult.not_configured(
                "github",
                missing="TARGET_REPOSITORY",
                remediation="Set TARGET_REPOSITORY to owner/name.",
            )

        try:
            source = self._build("github")
        except Exception as exc:  # noqa: BLE001
            state, reason = _classify_exception(exc)
            return CheckResult(
                name="github",
                state=state,
                summary=reason[:300],
                remediation=f"Set ${rc.github.token_env} to a read-only token.",
            )

        scope_hint = (
            f"Grant the token used in ${rc.github.token_env} the matching "
            "read permission."
        )
        result.add(
            _capability(
                "repository",
                lambda: source.health(),
                describe=lambda h: "reachable" if h.reachable else h.detail,
                remediation=scope_hint,
            )
        )
        # health() returns a value rather than raising, so an unreachable repo
        # has to be promoted to a real failure explicitly.
        repo_check = result.capabilities["repository"]
        health = None
        try:
            health = source.health()
        except Exception:  # noqa: BLE001
            pass
        if health is not None and not health.reachable:
            repo_check.state = (
                HealthState.UNKNOWN
                if health.degraded or "not set" in health.detail
                else HealthState.FAILED
            )
            repo_check.summary = health.detail or "repository unreachable"

        commits: List[Dict[str, Any]] = []

        def _commits() -> List[Dict[str, Any]]:
            nonlocal commits
            commits = source.list_commits(limit=5)
            return commits

        result.add(
            _capability(
                "commits",
                _commits,
                describe=lambda c: (
                    f"latest {c[0]['sha'][:8]} — {c[0]['message'][:60]}"
                    if c
                    else "no commits in window"
                ),
                remediation=scope_hint,
            )
        )
        result.add(
            _capability(
                "branches",
                lambda: source.list_branches(limit=50),
                describe=lambda b: f"{len(b)} branch(es)",
                remediation=scope_hint,
            )
        )
        result.add(
            _capability(
                "pull_requests",
                lambda: source.list_pull_requests(limit=20),
                describe=lambda p: f"{len(p)} open",
                remediation=scope_hint,
            )
        )
        result.add(
            _capability(
                "actions",
                lambda: source.list_workflow_runs(limit=10),
                describe=self._describe_runs,
                remediation=(
                    "The token needs the Actions:read permission "
                    f"(${rc.github.token_env})."
                ),
            )
        )

        # Write access is only *needed* once automated repair is enabled. Until
        # then a read-only token is the correct configuration, and reporting the
        # absence of write as a problem would be wrong.
        if not rc.repair.enabled:
            # Recorded as a fact, not as a capability. Adding it as an
            # unconfigured capability would drag the whole integration to
            # DEGRADED for lacking something it is correct not to have — a false
            # alarm, which is the exact failure mode the health vocabulary
            # exists to prevent.
            result.facts["write_access"] = "not required (automated repair is disabled)"
        else:
            result.add(
                _capability(
                    "write_access",
                    source.permissions,
                    describe=lambda p: (
                        "Contents+Pull requests: Write"
                        if p.get("push")
                        else "read-only — a repair could not open a pull request"
                    ),
                    remediation=(
                        f"Grant the token in ${rc.github.token_env} "
                        "'Contents: Write' and 'Pull requests: Write'. "
                        "Administration and secrets scopes are NOT required."
                    ),
                )
            )
            write = result.capabilities["write_access"]
            if write.state is HealthState.HEALTHY and not (
                source.permissions().get("push")
            ):
                write.state = HealthState.FAILED

        result.derive_state()
        result.facts = {
            "repository": self.target.repository,
            "default_branch": self.target.branch,
        }
        if commits:
            result.facts["latest_commit"] = commits[0]["sha"]
        result.summary = self._summarize(result)
        return result

    @staticmethod
    def _describe_runs(runs: List[Dict[str, Any]]) -> str:
        if not runs:
            return "no recent runs"
        latest = runs[0]
        failures = sum(1 for r in runs if r.get("conclusion") == "failure")
        outcome = latest.get("conclusion") or latest.get("status")
        return (
            f"latest '{latest.get('name', '?')}' {outcome}; "
            f"{failures} failure(s) in the last {len(runs)}"
        )

    def check_vercel(self) -> CheckResult:
        """Read deployment state. Never triggers or modifies a deployment."""
        rc = self._config.reliability
        if not self.target.vercel_project:
            return CheckResult.not_configured(
                "vercel",
                missing="VERCEL_PROJECT",
                remediation="Set VERCEL_PROJECT to the Vercel project id.",
            )
        try:
            source = self._build("vercel")
        except Exception as exc:  # noqa: BLE001
            state, reason = _classify_exception(exc)
            return CheckResult(
                name="vercel",
                state=state,
                summary=reason[:300],
                remediation=f"Set ${rc.vercel.token_env} to a read-only token.",
            )

        result = CheckResult(name="vercel")
        deployments: List[Dict[str, Any]] = []

        def _deployments() -> List[Dict[str, Any]]:
            nonlocal deployments
            deployments = source.list_deployments(limit=20)
            return deployments

        result.add(
            _capability(
                "deployments",
                _deployments,
                describe=lambda d: f"{len(d)} recent deployment(s)",
            )
        )

        def _production() -> Dict[str, Any]:
            # "No production deployment found" is not good news — it usually
            # means the deployment list could not be read at all. Returning it
            # as a successful capability would be a false green.
            found = self._production_deployment(deployments)
            if found is None:
                raise LookupError(
                    "no production deployment found (the deployment list was "
                    "empty or could not be read)"
                )
            return found

        result.add(
            _capability(
                "production_deployment",
                _production,
                describe=lambda d: f"{d['state']} @ {(d['commit_sha'] or '?')[:8]}",
            )
        )
        result.add(
            _capability(
                "environment_variable_names",
                lambda: source.list_environment_variable_names(),
                describe=lambda n: f"{len(n)} name(s) (values never read)",
            )
        )

        # Runtime error retrieval is plan-dependent. Rather than guess, report
        # it as an explicit blind spot; see docs/JARVIS_ARCHITECTURE.md §11.
        result.add(
            CheckResult(
                name="runtime_errors",
                state=HealthState.NOT_CHECKED,
                summary=(
                    "not retrieved: runtime log access is plan-dependent and is "
                    "not implemented"
                ),
                remediation=(
                    "Treat runtime errors as unmonitored until this is "
                    "implemented against a plan that exposes them."
                ),
            )
        )

        result.derive_state()
        production = self._production_deployment(deployments) if deployments else None
        if production is not None:
            result.facts = {
                "production_state": production["state"],
                "production_commit": production["commit_sha"],
                "production_url": production["url"],
            }
            if production["state"] == "ERROR":
                result.state = HealthState.FAILED
        result.summary = self._summarize(result)
        return result

    @staticmethod
    def _production_deployment(
        deployments: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        for deployment in deployments:
            if deployment.get("target") == "production":
                return deployment
        return None

    def check_supabase(self) -> CheckResult:
        """Read project health. Writes remain refused by the SQL guard."""
        rc = self._config.reliability
        if not self.target.supabase_ref:
            return CheckResult.not_configured(
                "supabase",
                missing="SUPABASE_PROJECT_REF",
                remediation="Set SUPABASE_PROJECT_REF to the project ref.",
            )
        try:
            source = self._build("supabase")
        except Exception as exc:  # noqa: BLE001
            state, reason = _classify_exception(exc)
            return CheckResult(
                name="supabase",
                state=state,
                summary=reason[:300],
                remediation=f"Set ${rc.supabase.token_env} to a read-only token.",
            )

        result = CheckResult(name="supabase")
        result.add(
            _capability(
                "project",
                lambda: source.get_project(),
                describe=lambda p: p.get("status", "unknown"),
            )
        )
        result.add(
            _capability(
                "edge_functions",
                lambda: source.list_edge_functions(),
                describe=lambda f: f"{len(f)} function(s)",
            )
        )
        result.add(
            _capability(
                "migrations",
                lambda: source.list_migrations(),
                describe=lambda m: f"{len(m)} applied",
            )
        )

        # Log-derived checks swallow API errors and return empty results, so
        # "0 denials" is indistinguishable from "could not read the logs".
        # Both are reported only when a log line was actually sampled.
        def _auth() -> Dict[str, Any]:
            summary = source.auth_diagnostics(limit=50)
            if not summary.get("sampled"):
                raise LookupError(
                    "no log lines were sampled, so no conclusion can be drawn "
                    "about authentication"
                )
            return summary

        def _rls() -> str:
            sampled = source.query_logs(limit=50)
            if not sampled:
                raise LookupError(
                    "no log lines were sampled, so no conclusion can be drawn "
                    "about row-level security"
                )
            denials = source.rls_diagnostics(limit=50)
            return f"{len(denials)} denial(s) in {len(sampled)} sampled log line(s)"

        result.add(_capability("rls_diagnostics", _rls, describe=lambda d: d))
        result.add(
            _capability(
                "auth_diagnostics",
                _auth,
                describe=lambda a: (
                    f"{a['failure_count']} auth failure(s) in "
                    f"{a['sampled']} log line(s)"
                ),
            )
        )

        result.derive_state()
        project_check = result.capabilities.get("project")
        if project_check is not None and project_check.state.was_checked:
            status = (project_check.summary or "").upper()
            if status and status not in ("ACTIVE_HEALTHY", "ACTIVE", "COMING_UP"):
                result.state = HealthState.FAILED
        result.facts = {
            "writes_enabled": rc.supabase.allow_production_writes,
            "write_guard": "active",
        }
        result.summary = self._summarize(result)
        return result

    def check_website(self) -> CheckResult:
        """Reachability of the production URL: DNS, TLS, status, redirects."""
        if not self.target.production_url:
            return CheckResult.not_configured(
                "website",
                missing="TARGET_PRODUCTION_URL",
                remediation="Set TARGET_PRODUCTION_URL to the production origin.",
            )

        import httpx

        result = CheckResult(name="website")
        url = self.target.production_url

        def _fetch() -> Dict[str, Any]:
            with httpx.Client(follow_redirects=True, timeout=30.0) as client:
                response = client.get(url)
            return {
                "status": response.status_code,
                "final_url": str(response.url),
                "redirects": len(response.history),
            }

        fetched = _capability(
            "reachable",
            _fetch,
            describe=lambda d: (
                f"HTTP {d['status']} after {d['redirects']} redirect(s) "
                f"-> {d['final_url']}"
            ),
            remediation=(
                "If this host is blocked by an egress policy rather than down, "
                "run the diagnostic from a network that can reach it."
            ),
        )
        result.add(fetched)
        result.derive_state()

        # Distinguish "the site answered badly" from "we could not ask".
        if fetched.state is HealthState.HEALTHY:
            try:
                status = int(fetched.summary.split()[1])
            except (IndexError, ValueError):
                status = 0
            if status >= 500:
                result.state = HealthState.FAILED
                result.summary = f"production returned HTTP {status}"
                return result
            if status >= 400:
                result.state = HealthState.DEGRADED
                result.summary = f"production returned HTTP {status}"
                return result
        result.summary = self._summarize(result)
        return result

    def check_probes(self) -> CheckResult:
        """Run the configured probes, refusing to count placeholders as passes."""
        from openjarvis.reliability.probes.placeholder import placeholder_reasons
        from openjarvis.reliability.probes.spec import load_probes

        directory = self._probe_directory()
        specs = load_probes(directory)
        enabled = [s for s in specs if s.enabled]

        result = CheckResult(name="probes")
        result.facts = {
            "directory": str(directory),
            "total": len(specs),
            "enabled": len(enabled),
        }

        if not specs:
            result.state = HealthState.NOT_CONFIGURED
            result.summary = f"no probe specs in {directory}"
            result.remediation = (
                "Add probe specs describing real workflows of the target "
                "application. See docs/JARVIS_LIVE_SETUP.md."
            )
            return result
        if not enabled:
            result.state = HealthState.NOT_CONFIGURED
            result.summary = f"{len(specs)} spec(s) found, all disabled"
            result.remediation = (
                "Enable a probe once its selectors match the real application."
            )
            return result

        executor = self._executor or self._build_executor()
        for spec in enabled:
            reasons = placeholder_reasons(spec)
            if reasons:
                # A placeholder probe must never be able to report PASS.
                result.add(
                    CheckResult(
                        name=spec.id,
                        state=HealthState.NOT_CONFIGURED,
                        summary="placeholder probe, not run: " + "; ".join(reasons),
                        remediation=(
                            "Point the probe's selectors at the real application, "
                            "then remove the placeholder markers."
                        ),
                    )
                )
                continue

            check = CheckResult(name=spec.id)
            try:
                probe_result = executor.run(spec)
            except Exception as exc:  # noqa: BLE001
                state, reason = _classify_exception(exc)
                check.state = state
                check.summary = reason[:300]
            else:
                if probe_result.success:
                    check.state = HealthState.HEALTHY
                    check.summary = f"passed in {probe_result.duration_seconds:.2f}s"
                elif probe_result.failure_kind in (
                    "misconfigured",
                    "runner_error",
                    "blocked",
                ):
                    # JARVIS could not look. That is a blind spot, not evidence
                    # that the target is broken.
                    check.state = HealthState.UNKNOWN
                    check.summary = probe_result.error[:300]
                else:
                    check.state = HealthState.FAILED
                    check.summary = probe_result.error[:300]
                check.facts = {
                    "failure_kind": probe_result.failure_kind,
                    "final_url": probe_result.final_url,
                }
                check.detail = probe_result.probe_id
            result.add(check)

        result.derive_state()
        result.summary = self._summarize(result)
        return result

    def check_notifications(self) -> CheckResult:
        """Whether the notification channel is configured. Sends nothing."""
        import os

        rc = self._config.reliability
        if not rc.notify.enabled:
            return CheckResult(
                name="notifications",
                state=HealthState.NOT_CONFIGURED,
                summary="notifications are disabled",
                remediation="Set [reliability.notify] enabled = true.",
            )
        if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
            return CheckResult.not_configured(
                "notifications",
                missing="TELEGRAM_BOT_TOKEN",
                remediation="Set TELEGRAM_BOT_TOKEN and allowed_chat_ids.",
            )
        if not (self._config.channel.telegram.allowed_chat_ids or "").strip():
            return CheckResult.not_configured(
                "notifications",
                missing="channel.telegram.allowed_chat_ids",
                remediation="Set [channel.telegram] allowed_chat_ids.",
            )
        return CheckResult(
            name="notifications",
            state=HealthState.HEALTHY,
            summary="configured (no message sent by the diagnostic)",
        )

    def check_code_agent(self) -> CheckResult:
        """Whether a coding agent is available. Never invokes it."""
        from openjarvis.reliability.code_agent import ClaudeCliAgent

        agent = ClaudeCliAgent()
        if agent.available():
            return CheckResult(
                name="code_agent",
                state=HealthState.HEALTHY,
                summary="claude CLI available (diagnostic-only; repair disabled)",
            )
        return CheckResult(
            name="code_agent",
            state=HealthState.NOT_CONFIGURED,
            summary="the claude CLI is not on PATH",
            remediation="Install Claude Code to enable diagnostic prompts.",
        )

    # -- orchestration -----------------------------------------------------

    def run(
        self,
        *,
        include_probes: bool = True,
        open_incidents: bool = True,
    ) -> DiagnosticReport:
        """Run every read-only check and aggregate the result."""
        from openjarvis.reliability.types import now_iso

        started = time.monotonic()
        report = DiagnosticReport(target=self.target, started_at=now_iso())

        logger.info(
            "live diagnostic starting: repository=%s url=%s vercel=%s supabase=%s",
            self.target.repository or "-",
            self.target.production_url or "-",
            self.target.vercel_project or "-",
            self.target.supabase_ref or "-",
        )

        report.checks.append(self.check_configuration())
        report.checks.append(self.check_github())
        report.checks.append(self.check_vercel())
        report.checks.append(self.check_supabase())
        report.checks.append(self.check_website())
        if include_probes:
            report.checks.append(self.check_probes())
        else:
            report.checks.append(
                CheckResult(
                    name="probes",
                    state=HealthState.NOT_CHECKED,
                    summary="skipped by request",
                )
            )
        report.checks.append(self.check_notifications())
        report.checks.append(self.check_code_agent())

        report.overall = aggregate(report.checks)
        report.duration_seconds = time.monotonic() - started

        if open_incidents and self._store is not None:
            report.incidents_opened = self._open_incidents(report)

        if self._store is not None:
            try:
                report.audit_chain_intact = self._store.verify_chain()[0]
            except Exception:  # noqa: BLE001
                logger.exception("could not verify the audit chain")
                report.audit_chain_intact = None

        for spot in report.blind_spots():
            logger.warning("diagnostic blind spot — %s", spot)

        return report

    def _open_incidents(self, report: DiagnosticReport) -> List[str]:
        """Open incidents for genuine failures only.

        ``UNKNOWN`` and ``NOT_CONFIGURED`` never produce one: an incident about
        the production system because JARVIS lacks a token would be false.
        """
        from openjarvis.reliability.detector import Detector
        from openjarvis.reliability.types import Severity, Signal

        detector = Detector(
            self._store,
            environment=self.target.environment,
            notifier=self._notifier,
        )
        opened: List[str] = []
        for check in report.checks:
            failing = [
                (name, cap)
                for name, cap in check.capabilities.items()
                if cap.state is HealthState.FAILED
            ]
            if check.state is not HealthState.FAILED and not failing:
                continue
            detail = "; ".join(f"{n}: {c.summary}" for n, c in failing) or check.summary
            detection = detector.from_signal(
                Signal(
                    source=f"diagnostic:{check.name}",
                    kind="diagnostic_failure",
                    title=f"{check.name} check failed",
                    detail=detail,
                    severity=Severity.HIGH,
                    component=check.name,
                )
            )
            if detection.opened and detection.incident is not None:
                opened.append(detection.incident.id)
        return opened

    # -- construction ------------------------------------------------------

    def _build(self, key: str) -> Any:
        """Construct an integration, honouring injected factories."""
        if key in self._factories:
            return self._factories[key]()
        rc = self._config.reliability
        if key == "github":
            from openjarvis.reliability.sources.github import GitHubSource

            return GitHubSource(
                repo=self.target.repository,
                token_env=rc.github.token_env,
                base_branch=self.target.branch,
                branch_prefix=rc.github.branch_prefix,
                allow_push_to_default_branch=False,  # diagnostics never write
                protected_paths=list(rc.policy.protected_paths),
            )
        if key == "vercel":
            from openjarvis.reliability.sources.vercel import VercelSource

            return VercelSource(
                project_id=self.target.vercel_project,
                team_id=self.target.vercel_team,
                token_env=rc.vercel.token_env,
            )
        if key == "supabase":
            from openjarvis.reliability.sources.supabase import SupabaseSource

            return SupabaseSource(
                project_ref=self.target.supabase_ref,
                token_env=rc.supabase.token_env,
                allow_production_writes=False,  # diagnostics never write
            )
        raise KeyError(f"unknown integration {key!r}")

    def _probe_directory(self) -> Any:
        from pathlib import Path

        from openjarvis.core.paths import get_config_dir

        configured = getattr(self._config.reliability.probes, "directory", "")
        return Path(configured or str(get_config_dir() / "reliability" / "probes"))

    def _build_executor(self) -> Any:
        from openjarvis.reliability.probes.executor import ProbeExecutor

        rc = self._config.reliability
        browser_options: Dict[str, Any] = {
            "headless": self._config.tools.browser.headless,
            "viewport": (
                self._config.tools.browser.viewport_width,
                self._config.tools.browser.viewport_height,
            ),
        }
        if getattr(rc.probes, "browser_executable_path", ""):
            browser_options["executable_path"] = rc.probes.browser_executable_path
        return ProbeExecutor(
            base_url=self.target.production_url,
            evidence_dir=getattr(rc.probes, "evidence_dir", "") or "",
            runner_options={
                "browser": browser_options,
                "http": {"verify_ssrf": not rc.probes.allow_private_targets},
            },
        )

    @staticmethod
    def _summarize(check: CheckResult) -> str:
        """One-line summary naming both what worked and what did not."""
        ok = [n for n, c in check.capabilities.items() if c.state.is_good_news]
        blind = check.unchecked_capabilities
        failed = [
            n for n, c in check.capabilities.items() if c.state is HealthState.FAILED
        ]
        parts = []
        if ok:
            parts.append(f"ok: {', '.join(sorted(ok))}")
        if failed:
            parts.append(f"FAILED: {', '.join(sorted(failed))}")
        if blind:
            parts.append(f"not verified: {', '.join(blind)}")
        return " · ".join(parts) or check.summary
