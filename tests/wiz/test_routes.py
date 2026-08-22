"""The Control Center's API: every button goes through the same door."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from openjarvis.server import wiz_routes  # noqa: E402
from openjarvis.wiz.authority import Authority, AuthorityPolicy, Channel  # noqa: E402
from openjarvis.wiz.journal import WizJournal  # noqa: E402
from openjarvis.wiz.memory import ProductMemory  # noqa: E402
from openjarvis.wiz.product import ProductVerbs  # noqa: E402
from openjarvis.wiz.runtime import build_wiz  # noqa: E402
from tests.wiz.test_product import FakePipeline  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A server whose Wiz is entirely local to this test."""
    pipeline = FakePipeline()
    pipeline.approvals = None
    pipeline.queue = None
    pipeline.clock = lambda: "2026-08-19T10:00:00+00:00"
    product = ProductVerbs(
        pipeline=pipeline,
        memory=ProductMemory(tmp_path / "memory.db"),
        runner=lambda feature_id: None,
    )
    runtime = build_wiz(
        home=tmp_path,
        policy=AuthorityPolicy(
            grants={
                Channel.CONTROL_CENTER: frozenset(
                    {Authority.READ, Authority.SAFE_ACTION, Authority.CODE_WRITE}
                )
            }
        ),
        journal=WizJournal(tmp_path / "j.jsonl"),
        store_factory=lambda: (_ for _ in ()).throw(FileNotFoundError("none")),
        product=product,
    )

    wiz_routes.reset_state()
    monkeypatch.setattr(wiz_routes, "_runtime", lambda: runtime)
    # Nothing in a test may start a real pipeline thread.
    started = []
    monkeypatch.setattr(
        wiz_routes,
        "_start",
        lambda feature_id: (
            started.append(feature_id) or {"started": True, "message": "working on it"}
        ),
    )

    app = FastAPI()
    app.include_router(wiz_routes.router)
    test_client = TestClient(app)
    test_client.started = started
    test_client.runtime = runtime
    test_client.pipeline = pipeline
    yield test_client
    wiz_routes.reset_state()


class TestAsking:
    def test_typing_a_sentence_records_a_request(self, client):
        response = client.post(
            "/api/wiz/features", json={"text": "Add a download button"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"]
        assert body["result"]["id"] == "FEAT-00001"
        assert body["result"]["say"].startswith("Sir, I'll work on it")

    def test_recording_and_building_are_separate(self, client):
        client.post(
            "/api/wiz/features",
            json={"text": "Add a download button", "record_only": True},
        )
        assert client.started == []

    def test_an_ordinary_request_starts_work(self, client):
        client.post("/api/wiz/features", json={"text": "Add a download button"})
        assert client.started == ["FEAT-00001"]

    def test_an_empty_request_is_rejected(self, client):
        response = client.post("/api/wiz/features", json={"text": "   "})
        assert response.status_code == 400

    def test_the_request_arrives_as_the_control_center(self, client):
        client.post("/api/wiz/features", json={"text": "Add a download button"})
        _, channel = client.pipeline.submitted[-1]
        assert channel is Channel.CONTROL_CENTER


class TestAuthorityStillApplies:
    def test_a_refusal_is_a_sentence_not_an_error(self, tmp_path, monkeypatch):
        # The operator is not unauthorised; *Wiz* is. The page has to render
        # that sentence rather than an error banner.
        pipeline = FakePipeline()
        product = ProductVerbs(pipeline=pipeline, memory=None, runner=lambda i: None)
        runtime = build_wiz(
            home=tmp_path,
            policy=AuthorityPolicy(
                grants={Channel.CONTROL_CENTER: frozenset({Authority.READ})}
            ),
            journal=WizJournal(tmp_path / "j.jsonl"),
            store_factory=lambda: (_ for _ in ()).throw(FileNotFoundError("none")),
            product=product,
        )
        wiz_routes.reset_state()
        monkeypatch.setattr(wiz_routes, "_runtime", lambda: runtime)
        app = FastAPI()
        app.include_router(wiz_routes.router)

        response = TestClient(app).post(
            "/api/wiz/features", json={"text": "Add a download button"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert "not authorised" in body["message"]
        wiz_routes.reset_state()

    def test_no_route_reaches_a_pipeline_without_the_brain(self):
        # A route that skipped dispatch would be a second authority path, and
        # the second one is always the one missing a check.
        import inspect

        source = inspect.getsource(wiz_routes)
        # The only direct pipeline use is reading the store for approvals and
        # cancellation, both of which are explicit and both of which check
        # authority themselves.
        assert "pipeline.submit(" not in source
        assert "pipeline.advance(" not in source


class TestReading:
    def test_the_queue_is_listed(self, client):
        client.post("/api/wiz/features", json={"text": "Add a download button"})
        body = client.get("/api/wiz/features").json()
        assert body["ok"]
        assert len(body["result"]["building"]) == 1

    def test_one_request_can_be_read_in_full(self, client):
        client.post("/api/wiz/features", json={"text": "Add a download button"})
        body = client.get("/api/wiz/features/FEAT-00001").json()
        assert body["result"]["feature"]["operator_request"] == "Add a download button"

    def test_an_unknown_request_is_a_404(self, client):
        assert client.get("/api/wiz/features/FEAT-99999").status_code == 404

    def test_status_says_what_is_missing(self, client):
        body = client.get("/api/wiz/status").json()
        assert body["configured"] is True
        assert "checks" in body
        assert body["shipping"]["merge_high_risk"] is False

    def test_memory_answers_a_search(self, client):
        client.post("/api/wiz/features", json={"text": "Add a coach summary"})
        body = client.get("/api/wiz/memory", params={"query": "coach"}).json()
        assert body["ok"]
        assert body["result"]["entries"]

    def test_health_is_a_separate_report_from_status(self, client):
        """§8: Wiz's own health, never a claim about Wize's."""
        body = client.get("/api/wiz/health").json()
        assert "checks" in body
        assert "overall" in body
        text = str(body).lower()
        for word in ("incident", "probe", "outage", "website"):
            assert word not in text


class TestApproval:
    def test_approving_without_an_approval_store_is_refused(self, client):
        client.post("/api/wiz/features", json={"text": "Add a download button"})
        response = client.post("/api/wiz/features/FEAT-00001/approve")
        assert response.status_code == 409
        assert "cannot record your consent" in response.json()["detail"]

    def test_approving_an_unknown_request_is_a_404(self, client):
        assert client.post("/api/wiz/features/FEAT-99999/approve").status_code == 404


class TestCancelling:
    def test_a_request_can_be_stopped(self, client):
        client.post("/api/wiz/features", json={"text": "Add a download button"})
        body = client.post("/api/wiz/features/FEAT-00001/cancel").json()
        assert body["state"] == "CANCELLED"

    def test_stopping_something_already_finished_is_not_an_error(self, client):
        client.post("/api/wiz/features", json={"text": "Add a download button"})
        client.post("/api/wiz/features/FEAT-00001/cancel")
        assert client.post("/api/wiz/features/FEAT-00001/cancel").json()["ok"]


class TestThePage:
    def test_the_page_is_served_without_a_build_step(self):
        from openjarvis.server.wiz_dashboard import router

        app = FastAPI()
        app.include_router(router)
        response = TestClient(app).get("/wiz")
        assert response.status_code == 200
        assert "Build something" in response.text

    def test_the_page_carries_no_external_dependency(self):
        # It has to work on a laptop with no network and no Node toolchain.
        from openjarvis.server.wiz_dashboard import _PAGE

        assert "http://" not in _PAGE
        assert "cdn" not in _PAGE.lower()
        assert "<script src=" not in _PAGE


class TestTheMorningSummary:
    def test_it_is_readable_and_says_whether_it_is_worth_sending(self, client):
        body = client.get("/api/wiz/morning").json()
        assert body["ok"]
        assert "Good morning" in body["text"]
        assert "worth_sending" in body["result"]

    def test_a_recorded_request_shows_up_as_work_in_progress(self, client):
        client.post("/api/wiz/features", json={"text": "Add a download button"})
        body = client.get("/api/wiz/morning").json()
        # RECEIVED is not "in progress" — nothing has started — so the honest
        # answer is that there is nothing to report yet.
        assert body["result"]["worth_sending"] is False

    def test_reading_the_summary_is_a_read(self, client):
        # A route that delivered the summary anywhere would be a POST. This one
        # is a GET, and asking twice changes nothing — the delivery decision is
        # made somewhere that holds an authority, not here.
        first = client.get("/api/wiz/morning").json()
        second = client.get("/api/wiz/morning").json()
        assert first["text"] == second["text"]
        assert client.post("/api/wiz/morning").status_code == 405
