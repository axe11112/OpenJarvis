"""Tests for the JARVIS Control Center and its launchd watchdog.

Two families of guarantee are checked here, and they are the two that would be
most expensive to get wrong:

*The dashboard tells the truth and gives nothing away.* Statuses come from the
real configuration and the real incident store, a probe nobody verified is never
green, and no credential value can reach a rendered response.

*The watchdog restarts crashes but never overrides an operator.* An emergency
stop refuses a start from every direction — the supervisor, the HTTP endpoint,
and the wrapper script launchd actually runs — and the dashboard cannot reach
any launchctl verb outside a four-name allowlist.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from openjarvis.core.config import JarvisConfig
from openjarvis.reliability.dashboard.model import (
    OverallStatus,
    ProbeStatus,
    build_snapshot,
    incident_detail,
    probe_card,
    probe_views,
    redact,
    safety_panel,
    wiz_message,
)
from openjarvis.reliability.dashboard.server import ControlCenterServer
from openjarvis.reliability.dashboard.service import DashboardService
from openjarvis.reliability.dashboard.supervisor import (
    SERVICE_LABEL,
    LaunchdSupervisor,
    RestartBudget,
    WatcherStatus,
    bound_log_file,
    render_plist,
    render_wrapper,
)
from openjarvis.reliability.health import CheckResult, HealthState, aggregate
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import (
    Evidence,
    EvidenceKind,
    Incident,
    IncidentState,
    ProbeResult,
    Severity,
    TrustLevel,
)

#: A value that must never appear in anything the dashboard emits. Distinctive
#: enough that a substring search is a real assertion.
FAKE_SECRET = "ghp_JARVISd0notLEAKthisTOKENvalue0123456789"


def _eventually(predicate, *, timeout: float, interval: float = 0.25) -> bool:
    """Poll *predicate* until it is true or *timeout* elapses.

    Process lifecycle is asynchronous; asserting on it with a fixed sleep is how
    a test becomes flaky on a loaded machine.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path: Path) -> JarvisConfig:
    """An isolated config that looks like the real read-only pilot."""
    cfg = JarvisConfig()
    rc = cfg.reliability
    rc.enabled = True
    rc.db_path = str(tmp_path / "reliability" / "incidents.db")
    rc.site.base_url = "https://www.example-target.com"
    rc.site.environment = "production"
    rc.probes.directory = str(tmp_path / "probes")
    rc.probes.evidence_dir = str(tmp_path / "evidence")
    rc.github.enabled = True
    rc.github.repo = "owner/Target-App"
    rc.github.token_env = "TEST_GITHUB_TOKEN"
    rc.vercel.enabled = True
    rc.vercel.project_id = "prj_test"
    rc.supabase.enabled = True
    rc.supabase.project_ref = "abcdefghijklmnop"
    rc.watch.enabled = True
    rc.watch.interval_seconds = 60
    # Every interlock in its safe position; the tests assert these survive.
    rc.repair.enabled = False
    rc.policy.deploy_mode = "never"
    rc.policy.allow_push_to_default_branch = False
    rc.supabase.allow_production_writes = False
    Path(rc.probes.directory).mkdir(parents=True, exist_ok=True)
    Path(rc.probes.evidence_dir).mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def store(config: JarvisConfig):
    """A real incident store — the dashboard must read the same one JARVIS writes."""
    opened = IncidentStore(config.reliability.db_path)
    yield opened
    opened.close()


class FakeLaunchctl:
    """A recorder standing in for ``launchctl``.

    Records every argv it is handed, so a test can assert not just what the
    supervisor did but that it could not have done anything else.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.state = "not running"
        self.pid = 0
        self.exit_code = 0
        self.print_returncode = 0
        self.kickstart_returncode = 0

    def __call__(self, argv):
        self.calls.append(list(argv))
        sub = argv[1] if len(argv) > 1 else ""
        if sub == "print":
            body = (
                f"{argv[2]} = {{\n"
                f"\tstate = {self.state}\n"
                + (f"\tpid = {self.pid}\n" if self.pid else "")
                + f"\tlast exit code = {self.exit_code}\n}}\n"
            )
            return subprocess.CompletedProcess(
                argv, self.print_returncode, stdout=body, stderr=""
            )
        if sub == "kickstart":
            if self.kickstart_returncode == 0:
                self.state = "running"
                self.pid = 4242
            return subprocess.CompletedProcess(
                argv, self.kickstart_returncode, stdout="", stderr="refused"
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


@pytest.fixture
def launchctl() -> FakeLaunchctl:
    return FakeLaunchctl()


@pytest.fixture
def supervisor(config: JarvisConfig, tmp_path: Path, launchctl: FakeLaunchctl):
    """A supervisor wired to the fake launchctl and an installed-looking agent."""
    sup = LaunchdSupervisor(
        config,
        runner=launchctl,
        home=tmp_path / "home",
        jarvis_dir=tmp_path / "jarvis",
        uid=501,
        platform_name="Darwin",
    )
    sup.plist_path().parent.mkdir(parents=True, exist_ok=True)
    sup.plist_path().write_text("<plist/>", encoding="utf-8")
    return sup


@pytest.fixture
def service(config: JarvisConfig, store, supervisor):
    """A dashboard service that runs no probes and recovers nothing by itself."""
    built = DashboardService(
        config,
        store=store,
        supervisor=supervisor,
        probe_verification="none",
        auto_recover=False,
    )
    yield built
    # The store fixture closes it; do not double-close.


def _incident(store: IncidentStore, **overrides) -> Incident:
    fields = dict(
        fingerprint="fp-" + overrides.get("component", "web"),
        severity=Severity.HIGH,
        component="website",
        title="Homepage returns 500",
        summary="The landing page fails to render.",
        source="probe",
        probe_id="homepage",
    )
    fields.update(overrides)
    return store.create(Incident(**fields))


def _healthy_report(names=("website", "github")):
    checks = [
        CheckResult(name=name, state=HealthState.HEALTHY, summary="ok")
        for name in names
    ]

    class _Report:
        def __init__(self):
            self.checks = checks
            self.overall = aggregate(checks)

        def blind_spots(self):
            return []

    return _Report()


def _degraded_report():
    good = CheckResult(name="website", state=HealthState.HEALTHY, summary="ok")
    blind = CheckResult(
        name="github",
        state=HealthState.UNKNOWN,
        summary="403: the token cannot read Actions",
    )
    checks = [good, blind]

    class _Report:
        def __init__(self):
            self.checks = checks
            self.overall = aggregate(checks)

        def blind_spots(self):
            return ["github: 403: the token cannot read Actions"]

    return _Report()


# ---------------------------------------------------------------------------
# Binding and transport security
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["192.168.1.20", "example.com"])
def test_refuses_to_bind_anything_but_loopback(service, host):
    """A non-loopback bind is refused at construction, not at request time."""
    with pytest.raises(ValueError, match="local-only"):
        ControlCenterServer(service, host=host, port=0)


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_a_wildcard_bind_is_refused_by_name(service, host):
    """The dangerous one gets its own message.

    ``0.0.0.0`` is what someone types when a phone will not connect, and on a
    laptop it means "answer on every café network I ever join". The refusal says
    so rather than repeating the generic line.
    """
    with pytest.raises(ValueError, match="every network this machine joins"):
        ControlCenterServer(service, host=host, port=0)


def test_binds_loopback_and_serves(live_server):
    """The happy path: 127.0.0.1 binds and answers."""
    status, _, body = live_server.get("/api/snapshot")
    assert status == 200
    assert json.loads(body)["target"]["url"] == "https://www.example-target.com"


def test_rejects_a_non_loopback_host_header(live_server):
    """DNS rebinding: a public name pointed at 127.0.0.1 must not be served."""
    status, _, body = live_server.get(
        "/api/snapshot", headers={"Host": "dashboard.evil.example"}
    )
    assert status == 403
    assert "Host" in json.loads(body)["error"]


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_mutating_http_methods_are_refused(live_server, method):
    """The surface is read-only apart from two named POSTs."""
    status, _, _ = live_server.request(method, "/api/snapshot")
    assert status == 405


def test_security_headers_are_present(live_server):
    """A strict CSP means redacted-but-hostile text still cannot phone home."""
    _, headers, _ = live_server.get("/")
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"


def test_static_assets_are_an_allowlist_not_a_directory(live_server):
    """There is no path to traverse, so traversal cannot be spelled."""
    for attempt in (
        "/static/../../../../etc/passwd",
        "/static/service.py",
        "/static/index.html",
    ):
        status, _, _ = live_server.get(attempt)
        assert status == 404, attempt
    assert live_server.get("/static/app.js")[0] == 200


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def test_no_credential_value_reaches_any_response(
    live_server, store, config, monkeypatch
):
    """The whole surface is swept for a credential value that is really set.

    The variable is one the configuration names, the value is distinctive, and
    an incident is seeded whose evidence contains it — so this fails if any
    route grew a path from the environment or from stored evidence to the page.
    """
    monkeypatch.setenv("TEST_GITHUB_TOKEN", FAKE_SECRET)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_SECRET)

    incident = _incident(store)
    store.add_evidence(
        incident,
        Evidence(
            kind=EvidenceKind.LOG,
            summary=f"upstream said Authorization: Bearer {FAKE_SECRET}",
            content=f"GET /api\nAuthorization: Bearer {FAKE_SECRET}\n",
            source="probe",
            trust=TrustLevel.EXTERNAL,
        ),
    )

    for path in (
        "/",
        "/static/app.js",
        "/api/snapshot",
        "/api/watcher",
        "/api/watcher/logs?stream=stderr",
        f"/api/incidents/{incident.id}",
    ):
        status, _, body = live_server.get(path)
        assert status == 200, path
        assert FAKE_SECRET not in body, f"credential value leaked from {path}"


def test_env_var_names_are_shown_but_values_never_are(config, monkeypatch, tmp_path):
    """Naming a variable is how JARVIS reports a credential. It stops there."""
    monkeypatch.setenv("TEST_GITHUB_TOKEN", FAKE_SECRET)
    sup = LaunchdSupervisor(
        config,
        runner=FakeLaunchctl(),
        home=tmp_path / "home",
        jarvis_dir=tmp_path / "jarvis",
        uid=501,
        platform_name="Darwin",
    )
    assert "TEST_GITHUB_TOKEN" in sup.required_env_names()
    assert FAKE_SECRET not in json.dumps(sup.status().to_dict())


def test_redact_withholds_text_it_cannot_scan(monkeypatch):
    """The failure mode of a redaction step must be less text, never more."""
    assert redact("") == ""
    assert redact("nothing sensitive here") == "nothing sensitive here"

    import openjarvis.security.boundary as boundary
    import openjarvis.security.credential_stripper as stripper

    def _explode(*_args, **_kwargs):
        raise RuntimeError("scanner unavailable")

    monkeypatch.setattr(boundary, "BoundaryGuard", _explode)
    monkeypatch.setattr(stripper, "CredentialStripper", _explode)
    assert redact(f"token {FAKE_SECRET}") == "[withheld: content could not be scanned]"


def test_incident_detail_drops_host_paths(store):
    """Artifact and worktree paths are host paths; the browser gets neither."""
    incident = _incident(store)
    store.add_evidence(
        incident,
        Evidence(
            kind=EvidenceKind.SCREENSHOT,
            summary="failure screenshot",
            artifact_path="/Users/someone/.openjarvis/evidence/x/failure.png",
            trust=TrustLevel.TRUSTED,
        ),
    )
    payload = incident_detail(
        store.get(incident.id), store.transitions_for(incident.id)
    )
    rendered = json.dumps(payload)
    assert "artifact_path" not in rendered
    assert "/Users/someone" not in rendered
    assert payload["evidence"][0]["has_artifact"] is True


# ---------------------------------------------------------------------------
# Status reflects real state
# ---------------------------------------------------------------------------


def test_safety_panel_reads_the_real_config(config):
    """No interlock value is a constant chosen by the UI."""
    panel = safety_panel(config, stop_flag_engaged=False)
    values = {row.label: row.value for row in panel.rows}
    assert values["Automatic repair"] == "OFF"
    assert values["Production deployment"] == "OFF"
    assert values["Default branch push"] == "OFF"
    assert values["Automatic PR merge"] == "OFF"
    assert values["Supabase writes"] == "OFF"
    assert values["Deploy mode"] == "never"
    assert values["Emergency stop"] == "not engaged"

    # Flip the real switches; the panel must follow, and mark them dangerous.
    config.reliability.repair.enabled = True
    config.reliability.policy.allow_push_to_default_branch = True
    config.reliability.supabase.allow_production_writes = True
    flipped = {
        r.label: (r.value, r.dangerous)
        for r in safety_panel(config, stop_flag_engaged=True).rows
    }
    assert flipped["Automatic repair"] == ("ON", True)
    assert flipped["Default branch push"] == ("ON", True)
    assert flipped["Supabase writes"] == ("ON", True)
    assert flipped["Emergency stop"] == ("ENGAGED", True)


def test_snapshot_reflects_configuration_not_defaults(config, store):
    """Target identity comes from the resolved target, not from a template."""
    snapshot = build_snapshot(config, incidents=[], specs=[], report=_healthy_report())
    assert snapshot.target_repository == "owner/Target-App"
    assert snapshot.target_url == "https://www.example-target.com"
    assert snapshot.target_name == "Target App"
    assert snapshot.environment == "production"
    assert snapshot.monitoring_enabled is True


class TestEngineeringSection:
    """The Control Center's Wiz half, read through an injected callable —
    never a direct import. This module keeps a production website alive;
    reliability must never depend on the assistant layered around it, so
    what it accepts here is a plain zero-argument callable, never anything
    typed to openjarvis.wiz. See test_dependency_direction.py, and
    openjarvis.wiz.dashboard_snapshot for the callable's real
    implementation and its own dedicated, wiz-side tests.
    """

    def test_no_callable_is_reported_unavailable_not_omitted(self, config, store):
        service = DashboardService(config, store=store, probe_verification="none")
        snapshot = service.snapshot()
        assert snapshot.engineering == {
            "available": False,
            "detail": "no engineering target configured",
        }

    def test_the_callables_return_value_passes_through_verbatim(self, config, store):
        payload = {"available": True, "metrics": {"sample_size": 3}}
        service = DashboardService(
            config, store=store, probe_verification="none", wiz_snapshot=lambda: payload
        )
        assert service.snapshot().engineering == payload

    def test_a_raising_callable_does_not_break_the_rest_of_the_snapshot(
        self, config, store
    ):
        def broken():
            raise RuntimeError("gone")

        service = DashboardService(
            config, store=store, probe_verification="none", wiz_snapshot=broken
        )
        snapshot = service.snapshot()
        assert snapshot.engineering == {
            "available": False,
            "detail": "could not read Wiz state",
        }
        # The rest of the page still renders — a broken Wiz side must never
        # take down the Wize side.
        assert snapshot.target_repository == "owner/Target-App"

    def test_reliability_dashboard_service_never_imports_wiz(self):
        import ast
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "openjarvis"
            / "reliability"
            / "dashboard"
            / "service.py"
        )
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            module = (
                getattr(node, "module", None)
                if isinstance(node, ast.ImportFrom)
                else None
            )
            names = (
                [a.name for a in node.names]
                if isinstance(node, (ast.Import, ast.ImportFrom))
                else []
            )
            assert module != "openjarvis.wiz" and not (module or "").startswith(
                "openjarvis.wiz."
            )
            assert not any(
                n == "openjarvis.wiz" or n.startswith("openjarvis.wiz.") for n in names
            )


def test_incidents_come_from_the_store(service, store):
    """Not a cache, not a copy: the store the watcher writes."""
    first = _incident(store, title="Login broken", component="authentication")
    second = _incident(
        store, fingerprint="fp-db", component="database", title="Query timeout"
    )
    ids = {i.id for i in service.snapshot().incidents}
    assert {first.id, second.id} <= ids

    # A third incident written after the snapshot shows up on the next read,
    # which is what "reads the live store" actually means.
    third = _incident(store, fingerprint="fp-ci", component="ci", title="CI red")
    assert third.id in {i.id for i in service.snapshot().incidents}


def test_open_and_resolved_incidents_are_distinguishable(service, store):
    """Open first, resolved marked, counts separated."""
    open_one = _incident(store, title="Still broken")
    resolved = _incident(store, fingerprint="fp-2", title="Was broken")
    store.transition(resolved, IncidentState.RESOLVED, reason="probe passed again")

    snapshot = service.snapshot()
    by_id = {i.id: i for i in snapshot.incidents}
    assert by_id[open_one.id].is_open is True
    assert by_id[resolved.id].is_open is False
    assert by_id[resolved.id].state == "RESOLVED"
    assert snapshot.open_incident_count == 1
    assert snapshot.resolved_incident_count == 1
    # Open incidents sort ahead of resolved ones.
    assert snapshot.incidents[0].id == open_one.id


def test_empty_incident_state_renders(service, live_server):
    """A system that has never failed is a normal state, not an error path."""
    snapshot = service.snapshot()
    assert snapshot.incidents == []
    assert snapshot.open_incident_count == 0
    status, _, body = live_server.get("/api/snapshot")
    assert status == 200
    assert json.loads(body)["incidents"] == []


def test_healthy_state_says_all_systems_operational(config):
    """The one green sentence, and the conditions it requires."""
    snapshot = build_snapshot(config, incidents=[], specs=[], report=_healthy_report())
    assert snapshot.overall == OverallStatus.HEALTHY.value
    assert snapshot.wiz["headline"] == "All systems operational."


def test_degraded_blind_spot_state_renders(config):
    """A check that could not reach a verdict degrades; it never goes green."""
    snapshot = build_snapshot(config, incidents=[], specs=[], report=_degraded_report())
    assert snapshot.overall == OverallStatus.DEGRADED.value
    assert snapshot.blind_spots
    assert "blind spot" in snapshot.wiz["headline"]
    github = next(c for c in snapshot.cards if c.key == "github")
    assert github.state == HealthState.UNKNOWN.value


def test_an_open_incident_makes_the_system_failed(config, store):
    """A HIGH open incident is a production failure, and Wiz names it."""
    incident = _incident(store, severity=Severity.HIGH)
    snapshot = build_snapshot(
        config,
        incidents=store.list(limit=50),
        specs=[],
        report=_healthy_report(),
    )
    assert snapshot.overall == OverallStatus.FAILED.value
    assert incident.id in snapshot.wiz["headline"]
    assert "automatic repair is disabled" in snapshot.wiz["detail"]


def test_before_the_first_cycle_nothing_is_claimed(config):
    """No report yet means UNVERIFIED, never HEALTHY."""
    snapshot = build_snapshot(config, incidents=[], specs=[], report=None)
    assert snapshot.overall == OverallStatus.UNVERIFIED.value


def test_wiz_never_contradicts_a_dead_watcher():
    """An offline watcher outranks every cheerful branch."""
    message = wiz_message(
        overall=OverallStatus.HEALTHY.value,
        incidents=[],
        blind_spots=[],
        repair_enabled=False,
        emergency_stop=False,
        monitoring_enabled=True,
        watcher_status="OFFLINE",
    )
    assert "not running" in message["headline"]
    assert message["mood"] == "alert"


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _spec(**overrides):
    from openjarvis.reliability.probes.spec import ProbeSpec

    fields = dict(id="homepage", name="Homepage", component="website", runner="http")
    fields.update(overrides)
    return ProbeSpec(**fields)


def test_a_probe_never_observed_is_not_verified_not_green(tmp_path):
    """The rule the whole reliability system is built on, applied to a table."""
    views = probe_views([_spec()], [], evidence_root=tmp_path)
    assert views[0].status == ProbeStatus.NOT_VERIFIED.value
    assert "no record" in views[0].reason


class TestADisabledProbeIsAParkedDecisionNotABlindSpot:
    """A disabled spec used to report NOT_VERIFIED, which put it in the blind
    spots, held the probe card DEGRADED and dragged overall down with it. One
    deliberately parked probe was enough to make the Control Center amber
    permanently — and a dashboard that can never go green is one nobody reads.

    What must stay true: it is still listed, it keeps its history, it is still
    not a pass, and an incident it already opened still counts.
    """

    def test_it_reports_disabled_rather_than_not_verified(self, tmp_path):
        views = probe_views([_spec(enabled=False)], [], evidence_root=tmp_path)
        assert views[0].status == ProbeStatus.DISABLED.value
        assert views[0].enabled is False
        assert "disabled in its spec" in views[0].reason

    def test_it_is_still_listed_and_keeps_its_history(self, tmp_path):
        """Parked, not deleted. Whatever it last observed stays readable."""
        (tmp_path / "homepage" / "run-1").mkdir(parents=True)
        views = probe_views([_spec(enabled=False)], [], evidence_root=tmp_path)
        assert len(views) == 1
        assert views[0].last_run
        assert "last observed" in views[0].reason

    def test_it_does_not_make_the_card_degraded(self, tmp_path):
        """The regression, stated directly."""
        views = probe_views(
            [_spec(enabled=False), _spec(id="api", url="https://real.example.net/x")],
            [],
            evidence_root=tmp_path,
            verified={"api": ProbeResult(probe_id="api", success=True)},
        )
        card = probe_card(views, directory=tmp_path)
        assert card.state == HealthState.HEALTHY.value
        assert card.blind_spots == []
        assert "1 disabled" in card.summary

    def test_it_is_not_counted_in_the_denominator(self, tmp_path):
        views = probe_views(
            [_spec(enabled=False), _spec(id="api", url="https://real.example.net/x")],
            [],
            evidence_root=tmp_path,
            verified={"api": ProbeResult(probe_id="api", success=True)},
        )
        facts = {
            f["label"]: f["value"] for f in probe_card(views, directory=tmp_path).facts
        }
        assert facts["configured"] == "1"
        assert facts["passing"] == "1"
        assert facts["not verified"] == "0"
        assert facts["disabled"] == "1"

    def test_it_stays_out_of_the_snapshot_blind_spots(self, tmp_path, config):
        """Blind spots drive Wiz's message as well as the card."""
        snapshot = build_snapshot(
            config,
            incidents=[],
            specs=[_spec(enabled=False)],
            evidence_root=tmp_path,
            probe_directory=tmp_path,
        )
        assert not any("homepage" in spot for spot in snapshot.to_dict()["blind_spots"])

    def test_a_genuinely_unobserved_probe_is_still_a_blind_spot(self, tmp_path):
        """The fix must not launder real gaps. Enabled and never run stays amber."""
        views = probe_views(
            [_spec(), _spec(id="api", url="https://real.example.net/x")],
            [],
            evidence_root=tmp_path,
            verified={"api": ProbeResult(probe_id="api", success=True)},
        )
        card = probe_card(views, directory=tmp_path)
        assert card.state == HealthState.DEGRADED.value
        assert card.blind_spots

    def test_disabling_a_probe_does_not_clear_its_open_incident(self, store, tmp_path):
        """Otherwise `enabled = false` becomes a way to silence a failure."""
        incident = _incident(store, probe_id="homepage")
        views = probe_views(
            [_spec(enabled=False)], store.list(limit=10), evidence_root=tmp_path
        )
        assert views[0].status == ProbeStatus.FAIL.value
        assert views[0].incident_id == incident.id
        card = probe_card(views, directory=tmp_path)
        assert card.state == HealthState.FAILED.value

    def test_a_fleet_that_is_entirely_disabled_is_not_green(self, tmp_path):
        """Nothing is watching. That is a configuration state, not health."""
        views = probe_views(
            [_spec(enabled=False), _spec(id="api", enabled=False)],
            [],
            evidence_root=tmp_path,
        )
        card = probe_card(views, directory=tmp_path)
        assert card.state == HealthState.NOT_CONFIGURED.value
        assert "all 2 probe(s) are disabled" in card.summary

    def test_disabled_rows_sort_last(self, tmp_path):
        views = probe_views(
            [_spec(id="parked", enabled=False), _spec(id="live")],
            [],
            evidence_root=tmp_path,
        )
        assert [v.id for v in views] == ["live", "parked"]


def test_a_placeholder_probe_can_never_pass(tmp_path):
    """Copied-from-the-examples probes are refused, exactly as the runner does."""
    spec = _spec(runner="http", url="https://example.com/health")
    views = probe_views([spec], [], evidence_root=tmp_path)
    assert views[0].status == ProbeStatus.NOT_VERIFIED.value
    assert "placeholder" in views[0].reason


def test_an_open_incident_makes_its_probe_fail(store, tmp_path):
    incident = _incident(store, probe_id="homepage")
    views = probe_views([_spec()], store.list(limit=10), evidence_root=tmp_path)
    assert views[0].status == ProbeStatus.FAIL.value
    assert views[0].incident_id == incident.id


def test_a_resolved_incident_does_not_hold_its_probe_red(store, tmp_path):
    """A closed incident is history, not a current verdict."""
    incident = _incident(store, probe_id="homepage")
    store.transition(incident, IncidentState.RESOLVED, reason="recovered")
    (tmp_path / "homepage" / "run-1").mkdir(parents=True)
    views = probe_views([_spec()], store.list(limit=10), evidence_root=tmp_path)
    assert views[0].status == ProbeStatus.PASS.value


def test_a_verified_run_that_filtered_noise_is_known_noise(tmp_path):
    """A pass that only happened after filtering is its own fact."""
    from openjarvis.reliability.types import ProbeResult

    clean = ProbeResult(probe_id="homepage", success=True, duration_seconds=0.4)
    filtered = ProbeResult(
        probe_id="homepage",
        success=True,
        duration_seconds=0.5,
        metadata={"suppressed_console_count": 2, "suppressed_request_count": 1},
    )
    spec = _spec(url="https://real-target.example.net/health")

    assert (
        probe_views([spec], [], evidence_root=tmp_path, verified={"homepage": clean})[
            0
        ].status
        == ProbeStatus.PASS.value
    )
    noisy = probe_views(
        [spec], [], evidence_root=tmp_path, verified={"homepage": filtered}
    )[0]
    assert noisy.status == ProbeStatus.KNOWN_NOISE.value
    assert "3 known-noise" in noisy.reason
    assert noisy.duration_seconds == 0.5


def test_probe_credentials_are_reported_by_name_only(tmp_path):
    spec = _spec(credentials={"password": "JARVIS_TEST_PASSWORD"})
    view = probe_views([spec], [], evidence_root=tmp_path)[0]
    assert view.credentials == ["JARVIS_TEST_PASSWORD"]


# ---------------------------------------------------------------------------
# Concurrency with the watcher
# ---------------------------------------------------------------------------


def test_dashboard_reads_while_the_watcher_writes(config, store, supervisor):
    """The dashboard and ``jarvis reliability watch`` share one database.

    Simulates the real arrangement: a second :class:`IncidentStore` on the same
    file, writing, while the dashboard reads through its own. The dashboard must
    neither block the writer nor miss what it wrote.
    """
    writer = IncidentStore(config.reliability.db_path)
    service = DashboardService(
        config,
        store=store,
        supervisor=supervisor,
        probe_verification="none",
        auto_recover=False,
    )
    errors: list[BaseException] = []
    written: list[str] = []

    def write_incidents() -> None:
        try:
            for index in range(12):
                incident = writer.create(
                    Incident(
                        fingerprint=f"fp-{index}",
                        severity=Severity.MEDIUM,
                        component="website",
                        title=f"Failure {index}",
                        probe_id="homepage",
                    )
                )
                written.append(incident.id)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert
            errors.append(exc)

    def read_snapshots() -> None:
        try:
            for _ in range(12):
                service.snapshot()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=write_incidents),
        threading.Thread(target=read_snapshots),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len(written) == 12
    # Everything the writer committed is visible to the reader afterwards.
    seen = {i.id for i in service.snapshot().incidents}
    assert set(written) <= seen
    writer.close()


def test_the_dashboard_never_writes_to_the_incident_store(config, store, supervisor):
    """Structural: every store method that mutates is made to explode."""

    class ReadOnlyStore:
        """Wraps the real store and refuses anything that would write."""

        _FORBIDDEN = (
            "create",
            "save",
            "transition",
            "add_evidence",
            "add_attempt",
            "update_attempt",
            "record_occurrence",
            "delete",
            "next_id",
        )

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name in self._FORBIDDEN:
                raise AssertionError(f"the dashboard called {name}() on the store")
            return getattr(self._inner, name)

    _incident(store, title="Pre-existing")
    guarded = ReadOnlyStore(store)
    service = DashboardService(
        config,
        store=guarded,
        supervisor=supervisor,
        probe_verification="none",
        auto_recover=False,
    )
    snapshot = service.snapshot()
    assert snapshot.incidents
    assert service.incident(snapshot.incidents[0].id) is not None


def test_a_refresh_never_opens_an_incident(config, store, supervisor):
    """``open_incidents=False`` is the argument that keeps one writer."""
    calls: list[dict] = []

    class _Diagnostic:
        def run(self, **kwargs):
            calls.append(kwargs)
            return _healthy_report()

    service = DashboardService(
        config,
        store=store,
        supervisor=supervisor,
        probe_verification="none",
        auto_recover=False,
        diagnostic_factory=_Diagnostic,
    )
    service.refresh()
    assert calls == [{"include_probes": False, "open_incidents": False}]
    assert store.count() == 0


# ---------------------------------------------------------------------------
# The launchd watchdog
# ---------------------------------------------------------------------------


def test_the_supervisor_only_ever_names_one_service(config):
    """A label is not a parameter; a lifecycle button is not a job runner."""
    with pytest.raises(ValueError, match="only ever acts on"):
        LaunchdSupervisor(config, label="com.attacker.anything")


def test_only_four_launchctl_subcommands_are_reachable(supervisor):
    """The allowlist is the boundary, and it refuses everything else."""
    for forbidden in ("submit", "load", "unload", "remove", "asuser", "debug"):
        with pytest.raises(ValueError, match="refusing to run"):
            supervisor._launchctl(forbidden, "anything")


def test_the_supervisor_never_uses_a_shell(supervisor, launchctl):
    """Every invocation is a fixed argv list with the one allowed target."""
    supervisor.status()
    supervisor.start()
    assert launchctl.calls
    for argv in launchctl.calls:
        assert argv[0] == "launchctl"
        assert argv[1] in {"print", "kickstart", "bootstrap", "bootout"}
        # The only service string that appears anywhere is ours.
        for part in argv[2:]:
            assert SERVICE_LABEL in part or part in {"-k", str(supervisor.plist_path())}


def test_watcher_reports_online_when_launchd_says_running(supervisor, launchctl):
    launchctl.state = "running"
    launchctl.pid = 3131
    state = supervisor.status()
    assert state.status is WatcherStatus.ONLINE
    assert state.pid == 3131


def test_watcher_reports_offline_when_it_is_not_running(supervisor, launchctl):
    launchctl.state = "not running"
    assert supervisor.status().status is WatcherStatus.OFFLINE


def test_watcher_reports_error_after_a_bad_exit(supervisor, launchctl):
    """A crash and a clean stop are different facts and get different words."""
    launchctl.state = "not running"
    launchctl.exit_code = 1
    state = supervisor.status()
    assert state.status is WatcherStatus.ERROR
    assert state.last_exit_code == 1


def test_watcher_reports_starting_while_launchd_spawns(supervisor, launchctl):
    launchctl.state = "spawn scheduled"
    assert supervisor.status().status is WatcherStatus.STARTING


def test_a_start_brings_a_dead_watcher_back(supervisor, launchctl):
    """The recovery path, end to end against the fake."""
    launchctl.state = "not running"
    assert supervisor.status().status is WatcherStatus.OFFLINE
    ok, message = supervisor.start()
    assert ok, message
    assert supervisor.status().status is WatcherStatus.ONLINE


def test_restart_uses_kickstart_k(supervisor, launchctl):
    supervisor.restart()
    kick = [c for c in launchctl.calls if c[1] == "kickstart"]
    assert kick and "-k" in kick[0]


# -- the emergency stop -----------------------------------------------------


def _engage_stop(config: JarvisConfig) -> Path:
    from openjarvis.reliability.watch import stop_flag_path

    flag = stop_flag_path(config)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("stopped by the operator", encoding="utf-8")
    return flag


def test_an_emergency_stop_is_reported_as_such_not_as_a_fault(
    config, supervisor, launchctl
):
    """OFFLINE and STOPPED_BY_OPERATOR must never be conflated."""
    _engage_stop(config)
    launchctl.state = "not running"
    state = supervisor.status()
    assert state.status is WatcherStatus.STOPPED_BY_OPERATOR
    assert state.may_start is False


def test_an_emergency_stop_refuses_every_start(config, supervisor, launchctl):
    """A deliberate operator stop remains a stop, from every direction."""
    _engage_stop(config)
    for action in (supervisor.start, supervisor.restart):
        ok, message = action()
        assert ok is False
        assert "emergency stop" in message
    # And nothing was even attempted against launchd.
    assert not [c for c in launchctl.calls if c[1] == "kickstart"]


def test_auto_recovery_will_not_override_an_emergency_stop(
    config, store, supervisor, launchctl
):
    """The dashboard's recovery path stops at the same wall."""
    _engage_stop(config)
    launchctl.state = "not running"
    service = DashboardService(
        config,
        store=store,
        supervisor=supervisor,
        probe_verification="none",
        auto_recover=True,
    )
    state = service.watcher_state()
    assert state.status is WatcherStatus.STOPPED_BY_OPERATOR
    assert not [c for c in launchctl.calls if c[1] == "kickstart"]


def test_the_http_endpoint_refuses_a_start_under_an_emergency_stop(config, live_server):
    """The refusal survives all the way out to the browser."""
    _engage_stop(config)
    status, _, body = live_server.post("/api/watcher/start")
    payload = json.loads(body)
    assert status == 409
    assert payload["ok"] is False
    assert "emergency stop" in payload["message"]


def test_the_wrapper_script_honours_the_stop_and_exits_clean(config, supervisor):
    """launchd's KeepAlive must not fight the operator.

    Runs the generated wrapper for real with the stop engaged. Exit 0 is the
    load-bearing assertion: with ``SuccessfulExit = false`` any other status
    would have launchd respawn it every ThrottleInterval.
    """
    flag = _engage_stop(config)
    supervisor.log_dir().mkdir(parents=True, exist_ok=True)
    script = supervisor.wrapper_path()
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        render_wrapper(
            working_directory=Path.cwd(),
            stop_flag=flag,
            env_file=supervisor.env_file_path(),
            stdout_log=supervisor.stdout_log(),
            stderr_log=supervisor.stderr_log(),
            # Would fail loudly if it ever ran; it must not.
            command=("false",),
        ),
        encoding="utf-8",
    )
    script.chmod(0o700)
    completed = subprocess.run(
        ["/bin/bash", str(script)], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0
    assert "emergency stop engaged" in completed.stderr


def test_the_wrapper_translates_the_stop_exit_code(config, supervisor):
    """Exit 3 from the watcher is a refusal, and must not read as a crash."""
    script = supervisor.wrapper_path()
    script.parent.mkdir(parents=True, exist_ok=True)
    supervisor.log_dir().mkdir(parents=True, exist_ok=True)
    script.write_text(
        render_wrapper(
            working_directory=Path.cwd(),
            stop_flag=supervisor.stop_flag(),
            env_file=supervisor.env_file_path(),
            stdout_log=supervisor.stdout_log(),
            stderr_log=supervisor.stderr_log(),
            command=("bash", "-c", "exit 3"),
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["/bin/bash", str(script)], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0

    # A genuine crash still propagates, or launchd would never restart anything.
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            '"bash" "-c" "exit 3"', '"bash" "-c" "exit 9"'
        ),
        encoding="utf-8",
    )
    crashed = subprocess.run(
        ["/bin/bash", str(script)], capture_output=True, text=True, timeout=60
    )
    assert crashed.returncode == 9


# -- restart policy ---------------------------------------------------------


def test_the_plist_declares_crash_restart_and_backoff(supervisor):
    """The watchdog behaviour lives in the plist; assert it is actually there."""
    plist = render_plist(
        label=SERVICE_LABEL,
        wrapper=supervisor.wrapper_path(),
        working_directory=Path.home() / "OpenJarvis",
        stdout_log=supervisor.stdout_log(),
        stderr_log=supervisor.stderr_log(),
        path_env="/usr/bin:/bin",
    )
    assert "<key>RunAtLoad</key>\n    <true/>" in plist
    assert "<key>KeepAlive</key>" in plist
    # Restart on an unexpected exit, leave a clean one alone.
    assert "<key>SuccessfulExit</key>\n        <false/>" in plist
    assert "<key>ThrottleInterval</key>" in plist


def test_the_plist_contains_no_credential(config, supervisor, monkeypatch, tmp_path):
    """A LaunchAgent is a readable file in a predictable place."""
    monkeypatch.setenv("TEST_GITHUB_TOKEN", FAKE_SECRET)
    report = supervisor.install(working_directory=tmp_path, load=False)
    plist = Path(report["plist"]).read_text(encoding="utf-8")
    assert FAKE_SECRET not in plist
    assert "PATH" in plist
    # It went into the 0600 environment file instead.
    env_file = Path(report["env_file"])
    assert FAKE_SECRET in env_file.read_text(encoding="utf-8")
    assert oct(env_file.stat().st_mode)[-3:] == "600"
    assert report["captured_env_names"] == ["TEST_GITHUB_TOKEN"] or (
        "TEST_GITHUB_TOKEN" in report["captured_env_names"]
    )


def test_the_restart_budget_stops_a_loop():
    """A watcher that will not stay up must not be hammered by a browser tab."""
    now = [0.0]
    budget = RestartBudget(
        max_starts=3, window_seconds=600, min_interval_seconds=20, clock=lambda: now[0]
    )
    for _ in range(3):
        allowed, _ = budget.may_start()
        assert allowed
        budget.record()
        now[0] += 30

    allowed, reason = budget.may_start()
    assert allowed is False
    assert "needs a human" in reason

    # The window eventually reopens, so a transient failure is not permanent.
    now[0] += 601
    assert budget.may_start()[0] is True


def test_a_start_is_rate_limited_immediately_after_another(supervisor, launchctl):
    launchctl.state = "not running"
    assert supervisor.start()[0] is True
    launchctl.state = "not running"
    ok, message = supervisor.start()
    assert ok is False
    assert "just requested" in message


def test_auto_recovery_starts_an_unexpectedly_dead_watcher(
    config, store, supervisor, launchctl
):
    """Requirement 8: opening the dashboard recovers a dead watcher."""
    launchctl.state = "not running"
    service = DashboardService(
        config,
        store=store,
        supervisor=supervisor,
        probe_verification="none",
        auto_recover=True,
    )
    state = service.watcher_state()
    assert state.status is WatcherStatus.STARTING
    assert [c for c in launchctl.calls if c[1] == "kickstart"]
    # And the next reading confirms it came up.
    assert service.watcher_state().status is WatcherStatus.ONLINE


def test_auto_recovery_can_be_switched_off(config, store, supervisor, launchctl):
    launchctl.state = "not running"
    service = DashboardService(
        config,
        store=store,
        supervisor=supervisor,
        probe_verification="none",
        auto_recover=False,
    )
    assert service.watcher_state().status is WatcherStatus.OFFLINE
    assert not [c for c in launchctl.calls if c[1] == "kickstart"]


# -- what a restart must never change ---------------------------------------


def test_a_restart_changes_no_interlock(config, live_server, launchctl):
    """Restarting the watcher must not widen anything JARVIS may do."""
    before = json.loads(live_server.get("/api/snapshot")[2])["safety"]

    status, _, body = live_server.post("/api/watcher/restart")
    assert status == 200
    assert json.loads(body)["ok"] is True

    after = json.loads(live_server.get("/api/snapshot")[2])["safety"]
    assert before == after
    values = {row["label"]: row["value"] for row in after["rows"]}
    assert values["Automatic repair"] == "OFF"
    assert values["Production deployment"] == "OFF"
    assert values["Default branch push"] == "OFF"
    assert values["Automatic PR merge"] == "OFF"
    assert values["Supabase writes"] == "OFF"
    assert config.reliability.repair.enabled is False
    assert config.reliability.policy.allow_push_to_default_branch is False
    assert config.reliability.supabase.allow_production_writes is False


def test_there_is_no_repair_deploy_or_incident_mutation_route(live_server):
    """The capabilities that are absent, asserted as absent."""
    for path in (
        "/api/repair",
        "/api/incidents/INC-00001/repair",
        "/api/incidents/INC-00001/close",
        "/api/deploy",
        "/api/config",
    ):
        assert live_server.post(path)[0] == 404, path


def test_lifecycle_endpoints_need_the_control_token(live_server):
    """A cross-site POST can be sent to a loopback port; it cannot be read."""
    status, _, body = live_server.post(
        "/api/watcher/start", headers={"X-JARVIS-Control": "wrong"}
    )
    assert status == 403
    assert "control token" in json.loads(body)["error"]

    status, _, _ = live_server.post("/api/watcher/start", headers={}, with_token=False)
    assert status == 403


def test_watcher_control_can_be_removed_entirely(service, config):
    """``--no-watcher-control`` makes the capability absent, not hidden."""
    server = ControlCenterServer(
        service, host="127.0.0.1", port=0, allow_watcher_control=False
    )
    server.start_background()
    try:
        client = _Client(server)
        assert client.post("/api/watcher/start")[0] == 404
        # Reads still work.
        assert client.get("/api/snapshot")[0] == 200
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


def test_logs_are_bounded_in_place(tmp_path):
    """Truncation keeps the tail and the inode, so an open fd keeps working."""
    log = tmp_path / "watch.stdout.log"
    log.write_text("\n".join(f"line {i}" for i in range(200_000)), encoding="utf-8")
    original_inode = log.stat().st_ino
    size_before = log.stat().st_size

    assert bound_log_file(log, max_bytes=50_000, keep_bytes=10_000) is True

    assert log.stat().st_size < size_before
    assert log.stat().st_ino == original_inode, "rotation must not orphan the inode"
    text = log.read_text(encoding="utf-8")
    assert "truncated" in text
    assert "line 199999" in text, "the most recent output must survive"

    # A small file is left completely alone.
    assert bound_log_file(log, max_bytes=10_000_000) is False


def test_the_log_bounder_does_not_survive_a_killed_wrapper(config, supervisor):
    """A hard kill skips the EXIT trap, so the bounder has to notice by itself.

    Found the hard way: SIGKILLing a wedged watcher left a sleeping bash behind,
    and a crash-restart loop leaked one per crash. The bounder now re-checks its
    parent on every tick.
    """
    script = supervisor.wrapper_path()
    script.parent.mkdir(parents=True, exist_ok=True)
    supervisor.log_dir().mkdir(parents=True, exist_ok=True)
    text = render_wrapper(
        working_directory=Path.cwd(),
        stop_flag=supervisor.stop_flag(),
        env_file=supervisor.env_file_path(),
        stdout_log=supervisor.stdout_log(),
        stderr_log=supervisor.stderr_log(),
        command=("sleep", "60"),
    )
    # Tick fast enough to assert inside a test's patience.
    text = text.replace("BOUND_TICK_SECONDS=15", "BOUND_TICK_SECONDS=1")
    script.write_text(text, encoding="utf-8")

    # The bounder is a subshell of the wrapper, so it carries the wrapper's
    # command line. The script path is unique to this test's tmp_path, which
    # makes it a precise handle on exactly these processes and nothing else.
    def running() -> list[str]:
        found = subprocess.run(
            ["pgrep", "-f", str(script)], capture_output=True, text=True
        )
        return [pid for pid in found.stdout.split() if pid]

    proc = subprocess.Popen(
        ["/bin/bash", str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert _eventually(lambda: len(running()) >= 2, timeout=15), (
            "the bounder never started"
        )

        proc.kill()  # SIGKILL: the EXIT trap does not run
        proc.wait(timeout=10)

        assert _eventually(lambda: not running(), timeout=25), (
            f"orphans survived the kill: {running()}"
        )
    finally:
        for pid in running():
            try:
                os.kill(int(pid), 9)
            except (ProcessLookupError, PermissionError, ValueError):
                pass


def test_the_wrapper_names_explicit_log_paths(supervisor):
    """Requirement 11: the operator can find the logs without guessing."""
    assert supervisor.stdout_log().name == "watch.stdout.log"
    assert supervisor.stderr_log().name == "watch.stderr.log"
    assert supervisor.stdout_log().parent.name == "logs"


def test_log_tailing_is_restricted_to_two_named_streams(supervisor):
    with pytest.raises(ValueError, match="unknown log stream"):
        supervisor.tail_log(stream="../../../etc/passwd")


def test_log_tail_endpoint_rejects_an_unknown_stream(live_server):
    assert live_server.get("/api/watcher/logs?stream=%2Fetc%2Fpasswd")[0] == 400


# ---------------------------------------------------------------------------
# HTTP client helper
# ---------------------------------------------------------------------------


class _Client:
    """A tiny stdlib HTTP client, so the tests add no dependency either."""

    def __init__(self, server: ControlCenterServer) -> None:
        self._server = server

    def request(self, method, path, *, headers=None, with_token=True):
        merged = {"Host": f"127.0.0.1:{self._server.port}"}
        if method == "POST" and with_token:
            merged["X-JARVIS-Control"] = self._server.control_token
        merged.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self._server.port, timeout=30)
        try:
            conn.request(method, path, headers=merged)
            response = conn.getresponse()
            body = response.read().decode("utf-8", errors="replace")
            return response.status, dict(response.getheaders()), body
        finally:
            conn.close()

    def get(self, path, *, headers=None):
        return self.request("GET", path, headers=headers)

    def post(self, path, *, headers=None, with_token=True):
        return self.request("POST", path, headers=headers, with_token=with_token)


@pytest.fixture
def live_server(service):
    """A Control Center actually listening on a loopback port."""
    server = ControlCenterServer(service, host="127.0.0.1", port=0)
    server.start_background()
    try:
        yield _Client(server)
    finally:
        server.shutdown()


def test_the_module_never_reads_an_environment_value(tmp_path):
    """Structural check on the read model: no ``os.environ[...]`` reads.

    ``model.py`` renders everything the browser sees. The only environment
    access it is allowed is a presence test, and this catches a future edit that
    reaches for a value instead.
    """
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "openjarvis"
        / "reliability"
        / "dashboard"
        / "model.py"
    ).read_text(encoding="utf-8")
    assert "os.environ[" not in source
    assert "os.getenv" not in source
    # The one permitted form, which returns a bool.
    assert 'os.environ.get(name, "")' in source


def test_os_import_is_used_only_for_presence():
    """Companion to the source check above, exercised rather than grepped."""
    from openjarvis.reliability.dashboard.model import environ_has

    os.environ["JARVIS_DASHBOARD_PRESENCE_TEST"] = "something"
    try:
        assert environ_has("JARVIS_DASHBOARD_PRESENCE_TEST") is True
        assert environ_has("JARVIS_DASHBOARD_ABSENT_TEST") is False
    finally:
        del os.environ["JARVIS_DASHBOARD_PRESENCE_TEST"]


# ---------------------------------------------------------------------------
# Handover and autonomy — what the owner reads when Sir has stopped
# ---------------------------------------------------------------------------


def test_an_escalated_incident_carries_its_handover_to_the_page(tmp_path):
    """The page must show what was tried, not just that something was."""
    from openjarvis.reliability.dashboard.model import incident_detail
    from openjarvis.reliability.playbook import build_handover
    from openjarvis.reliability.types import Incident, RepairAttempt, Severity

    incident = Incident(
        fingerprint="fp",
        severity=Severity.HIGH,
        component="authentication",
        title="Login redirects back to /login",
        id="INC-00042",
    )
    incident.attempts.append(RepairAttempt(number=1, strategy="recent_change"))
    incident.metadata["handover"] = build_handover(
        incident, reason="attempts exhausted", max_attempts=3
    ).to_dict()

    payload = incident_detail(incident, [])
    assert payload["handover"]["what_failed"]
    assert payload["handover"]["what_is_needed"]
    assert any("recent change" in line for line in payload["handover"]["tried"])


def test_an_incident_with_no_handover_gets_no_empty_panel():
    from openjarvis.reliability.dashboard.model import incident_detail
    from openjarvis.reliability.types import Incident, Severity

    incident = Incident(
        fingerprint="fp", severity=Severity.LOW, component="site", title="slow"
    )
    assert "handover" not in incident_detail(incident, [])
