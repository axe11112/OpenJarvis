"""Tests for the JARVIS reliability dashboard API."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from openjarvis.core.config import JarvisConfig  # noqa: E402
from openjarvis.reliability.store import IncidentStore  # noqa: E402
from openjarvis.reliability.types import (  # noqa: E402
    Evidence,
    EvidenceKind,
    Incident,
    IncidentState,
    RepairAttempt,
    Severity,
    TrustLevel,
    VerificationResult,
)
from openjarvis.server import reliability_routes  # noqa: E402


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """An app wired to an isolated config and incident database."""
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    config = JarvisConfig()
    config.reliability.enabled = True
    config.reliability.site.base_url = "https://example.com"
    config.reliability.db_path = str(tmp_path / "incidents.db")
    config.reliability.probes.evidence_dir = str(evidence_dir)
    config.reliability.probes.directory = str(tmp_path / "probes")

    reliability_routes.reset_state()
    monkeypatch.setattr(reliability_routes, "_get_config", lambda: config)

    store = IncidentStore(config.reliability.db_path)
    monkeypatch.setattr(reliability_routes, "_get_store", lambda: store)

    app = FastAPI()
    app.include_router(reliability_routes.router)
    yield TestClient(app), store, config, tmp_path
    store.close()
    reliability_routes.reset_state()


def _incident(**overrides) -> Incident:
    defaults = dict(
        fingerprint="fp",
        severity=Severity.CRITICAL,
        component="authentication",
        title="Login broken",
        summary="Users bounce back to /login.",
        probe_id="auth-login",
    )
    defaults.update(overrides)
    return Incident(**defaults)


class TestHealth:
    def test_healthy_when_nothing_is_wrong(self, wired):
        client, _, _, _ = wired
        payload = client.get("/api/reliability/health").json()
        assert payload["enabled"] is True
        assert payload["surfaces"]["website"] == "healthy"
        assert payload["open_incidents"] == 0
        assert payload["audit_chain_intact"] is True

    def test_disabled_integrations_report_disabled(self, wired):
        client, _, _, _ = wired
        surfaces = client.get("/api/reliability/health").json()["surfaces"]
        assert surfaces["vercel"] == "disabled"
        assert surfaces["supabase"] == "disabled"
        assert surfaces["github"] == "disabled"

    def test_critical_incident_marks_the_surface_down(self, wired):
        client, store, _, _ = wired
        store.create(_incident())
        surfaces = client.get("/api/reliability/health").json()["surfaces"]
        assert surfaces["website"] == "down"

    def test_low_incident_marks_the_surface_degraded(self, wired):
        client, store, _, _ = wired
        store.create(_incident(severity=Severity.LOW))
        surfaces = client.get("/api/reliability/health").json()["surfaces"]
        assert surfaces["website"] == "degraded"

    def test_policy_is_reported(self, wired):
        client, _, _, _ = wired
        policy = client.get("/api/reliability/health").json()["policy"]
        assert policy["deploy_mode"] == "pr_only"
        assert policy["repair_enabled"] is False


class TestIncidents:
    def test_empty(self, wired):
        client, _, _, _ = wired
        payload = client.get("/api/reliability/incidents").json()
        assert payload["count"] == 0

    def test_list(self, wired):
        client, store, _, _ = wired
        store.create(_incident())
        payload = client.get("/api/reliability/incidents").json()
        assert payload["count"] == 1
        assert payload["incidents"][0]["id"] == "INC-00001"
        assert payload["incidents"][0]["severity"] == "CRITICAL"

    def test_filter_by_severity(self, wired):
        client, store, _, _ = wired
        store.create(_incident(severity=Severity.CRITICAL, fingerprint="a"))
        store.create(_incident(severity=Severity.LOW, fingerprint="b"))
        payload = client.get("/api/reliability/incidents?severity=low").json()
        assert payload["count"] == 1

    def test_open_only(self, wired):
        client, store, _, _ = wired
        incident = store.create(_incident())
        store.transition(incident, IncidentState.RESOLVED, reason="transient")
        assert (
            client.get("/api/reliability/incidents?open_only=true").json()["count"] == 0
        )

    def test_bad_filter_is_a_400(self, wired):
        client, _, _, _ = wired
        response = client.get("/api/reliability/incidents?severity=spicy")
        assert response.status_code == 400

    def test_detail(self, wired):
        client, store, _, _ = wired
        incident = store.create(_incident(repro_steps=["Open /login"]))
        payload = client.get(f"/api/reliability/incidents/{incident.id}").json()
        assert payload["id"] == incident.id
        assert payload["repro_steps"] == ["Open /login"]

    def test_missing_incident_is_a_404(self, wired):
        client, _, _, _ = wired
        assert client.get("/api/reliability/incidents/INC-99999").status_code == 404

    def test_history(self, wired):
        client, store, _, _ = wired
        incident = store.create(_incident())
        store.transition(incident, IncidentState.INVESTIGATING, reason="triage")
        payload = client.get(f"/api/reliability/incidents/{incident.id}/history").json()
        assert len(payload["transitions"]) == 2
        assert payload["chain_intact"] is True


class TestEvidenceArtifacts:
    def test_detail_never_exposes_host_paths(self, wired):
        """A filesystem path in an API response is an invitation."""
        client, store, _, tmp_path = wired
        artifact = tmp_path / "evidence" / "shot.png"
        artifact.write_bytes(b"\x89PNG fake")
        incident = store.create(_incident())
        store.add_evidence(
            incident,
            Evidence(
                kind=EvidenceKind.SCREENSHOT,
                summary="failure",
                artifact_path=str(artifact),
                trust=TrustLevel.TRUSTED,
            ),
        )
        payload = client.get(f"/api/reliability/incidents/{incident.id}").json()
        item = payload["evidence"][0]
        assert "artifact_path" not in item
        assert item["artifact_url"].endswith(f"/evidence/{item['id']}")

    def test_artifact_is_served(self, wired):
        client, store, _, tmp_path = wired
        artifact = tmp_path / "evidence" / "shot.png"
        artifact.write_bytes(b"\x89PNG fake")
        incident = store.create(_incident())
        evidence = store.add_evidence(
            incident,
            Evidence(
                kind=EvidenceKind.SCREENSHOT,
                artifact_path=str(artifact),
                trust=TrustLevel.TRUSTED,
            ),
        )
        response = client.get(
            f"/api/reliability/incidents/{incident.id}/evidence/{evidence.id}"
        )
        assert response.status_code == 200
        assert response.content == b"\x89PNG fake"

    def test_path_outside_the_evidence_root_is_refused(self, wired):
        """A crafted record must not be able to read arbitrary host files."""
        client, store, _, tmp_path = wired
        outside = tmp_path / "secret.txt"
        outside.write_text("private")
        incident = store.create(_incident())
        evidence = store.add_evidence(
            incident,
            Evidence(
                kind=EvidenceKind.SCREENSHOT,
                artifact_path=str(outside),
                trust=TrustLevel.TRUSTED,
            ),
        )
        response = client.get(
            f"/api/reliability/incidents/{incident.id}/evidence/{evidence.id}"
        )
        assert response.status_code == 403

    def test_traversal_attempt_is_refused(self, wired):
        client, store, _, tmp_path = wired
        incident = store.create(_incident())
        evidence = store.add_evidence(
            incident,
            Evidence(
                kind=EvidenceKind.SCREENSHOT,
                artifact_path=str(
                    tmp_path / "evidence" / ".." / ".." / "etc" / "passwd"
                ),
                trust=TrustLevel.TRUSTED,
            ),
        )
        response = client.get(
            f"/api/reliability/incidents/{incident.id}/evidence/{evidence.id}"
        )
        assert response.status_code == 403

    def test_missing_file_is_a_404(self, wired):
        client, store, _, tmp_path = wired
        incident = store.create(_incident())
        evidence = store.add_evidence(
            incident,
            Evidence(
                kind=EvidenceKind.SCREENSHOT,
                artifact_path=str(tmp_path / "evidence" / "gone.png"),
                trust=TrustLevel.TRUSTED,
            ),
        )
        response = client.get(
            f"/api/reliability/incidents/{incident.id}/evidence/{evidence.id}"
        )
        assert response.status_code == 404

    def test_unknown_evidence_is_a_404(self, wired):
        client, store, _, _ = wired
        incident = store.create(_incident())
        response = client.get(f"/api/reliability/incidents/{incident.id}/evidence/nope")
        assert response.status_code == 404


class TestProbes:
    def test_lists_specs(self, wired):
        client, _, config, tmp_path = wired
        probes = tmp_path / "probes"
        probes.mkdir()
        (probes / "home.toml").write_text(
            '[probe]\nid = "homepage"\nrunner = "http"\nurl = "/"\n', encoding="utf-8"
        )
        payload = client.get("/api/reliability/probes").json()
        assert payload["probes"][0]["id"] == "homepage"

    def test_exposes_credential_names_not_values(self, wired, monkeypatch):
        """The API surface must never carry a credential value."""
        client, _, config, tmp_path = wired
        monkeypatch.setenv("JARVIS_TEST_USER_PASSWORD", "SuperSecret123")
        probes = tmp_path / "probes"
        probes.mkdir()
        (probes / "login.toml").write_text(
            '[probe]\nid = "login"\n'
            "[probe.credentials]\n"
            'password = "JARVIS_TEST_USER_PASSWORD"\n'
            "[[probe.steps]]\n"
            'action = "fill"\nselector = "#p"\nvalue_from = "password"\n',
            encoding="utf-8",
        )
        payload = client.get("/api/reliability/probes").json()
        assert payload["probes"][0]["credentials"] == ["JARVIS_TEST_USER_PASSWORD"]
        assert "SuperSecret123" not in str(payload)

    def test_missing_directory_is_empty_not_an_error(self, wired):
        client, _, _, _ = wired
        assert client.get("/api/reliability/probes").json()["probes"] == []


class TestRepairs:
    def test_empty(self, wired):
        client, _, _, _ = wired
        assert client.get("/api/reliability/repairs").json()["count"] == 0

    def test_lists_attempts_newest_first(self, wired):
        client, store, _, _ = wired
        incident = store.create(_incident())
        store.add_attempt(
            incident,
            RepairAttempt(
                number=1,
                branch="jarvis/incident-INC-00001",
                changed_files=["app/auth.ts"],
                outcome="verification_failed",
                started_at="2026-08-14T10:00:00Z",
            ),
        )
        store.add_attempt(
            incident,
            RepairAttempt(
                number=2,
                verification=VerificationResult(passed=True, probe_id="auth-login"),
                outcome="verified",
                started_at="2026-08-14T11:00:00Z",
            ),
        )
        payload = client.get("/api/reliability/repairs").json()
        assert payload["count"] == 2
        assert payload["repairs"][0]["attempt"] == 2
        assert payload["repairs"][0]["verified"] is True
        assert payload["repairs"][1]["verified"] is False


class TestReadOnlySurface:
    def test_no_mutating_routes_exist(self, wired):
        """An HTTP endpoint that can trigger a production repair is a much
        larger attack surface than one that cannot."""
        methods = set()
        for route in reliability_routes.router.routes:
            methods |= set(getattr(route, "methods", set()))
        assert methods <= {"GET", "HEAD", "OPTIONS"}


class TestDashboardPage:
    def test_page_is_served(self):
        from openjarvis.server.reliability_dashboard import router as page_router

        app = FastAPI()
        app.include_router(page_router)
        response = TestClient(app).get("/reliability")
        assert response.status_code == 200
        assert "JARVIS" in response.text
        assert "text/html" in response.headers["content-type"]

    def test_page_escapes_api_content(self):
        """Incident titles come from external systems; the page must render
        them as text rather than markup."""
        from openjarvis.server.reliability_dashboard import _PAGE

        assert "const esc =" in _PAGE
        # Every interpolation of API data goes through esc().
        assert "${esc(i.title)}" in _PAGE
        assert "${esc(i.component)}" in _PAGE

    def test_page_is_self_contained(self):
        """No CDN, no build step: it works the moment `jarvis serve` starts."""
        from openjarvis.server.reliability_dashboard import _PAGE

        assert "http://" not in _PAGE
        assert "https://" not in _PAGE
