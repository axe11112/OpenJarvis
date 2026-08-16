"""Tests for the Sir Voice transport: who may reach it, and what it exposes.

The voice engine's own tests (``test_voice.py``) ask what a sentence may cause.
These ask a different question: who may say a sentence at all, and what the HTTP
surface gives away to somebody who should not be there.
"""

from __future__ import annotations

import base64
import json

import pytest

from openjarvis.reliability.dashboard.access import (
    AccessPolicy,
    detect_tailscale,
    loopback_policy,
)
from openjarvis.reliability.voice.push import (
    PushSender,
    SubscriptionStore,
    VapidKey,
    WebPushSubscription,
    verify,
)
from openjarvis.reliability.voice.web import VoiceEndpoints

TAILNET = AccessPolicy(
    tailscale_ip="100.83.233.112", tailscale_host="mac.tail1234.ts.net"
)


# ---------------------------------------------------------------------------
# Who may reach the Control Center
# ---------------------------------------------------------------------------


class TestAccessPolicy:
    def test_the_default_is_unchanged_loopback_only(self):
        """Without Tailscale configured, nothing about the old posture moved."""
        policy = loopback_policy()
        assert policy.may_bind("127.0.0.1")
        assert policy.may_connect("127.0.0.1")
        assert policy.may_host("localhost:8765")
        assert not policy.may_bind("100.83.233.112")
        assert not policy.may_connect("100.97.243.37")
        assert not policy.may_host("mac.tail1234.ts.net")

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "*", ""])
    def test_a_wildcard_bind_is_never_allowed(self, host):
        """Even with Tailscale on. This is the 2am change that must not work."""
        assert not TAILNET.may_bind(host)
        assert not loopback_policy().may_bind(host)
        assert "every network" in TAILNET.bind_refusal(host)

    @pytest.mark.parametrize(
        "host", ["192.168.1.20", "10.0.0.5", "example.com", "8.8.8.8"]
    )
    def test_no_lan_or_public_address_may_be_bound(self, host):
        assert not TAILNET.may_bind(host)

    def test_only_this_machine_s_tailscale_address_may_be_bound(self):
        assert TAILNET.may_bind("100.83.233.112")
        # Another node on the same tailnet is still not ours to bind.
        assert not TAILNET.may_bind("100.97.243.37")

    @pytest.mark.parametrize("peer", ["100.97.243.37", "100.64.0.1", "127.0.0.1"])
    def test_tailnet_and_loopback_peers_are_admitted(self, peer):
        assert TAILNET.may_connect(peer)

    @pytest.mark.parametrize(
        "peer", ["8.8.8.8", "192.168.1.9", "172.16.0.4", "", "not-an-ip"]
    )
    def test_everything_else_is_refused(self, peer):
        """100.64.0.0/10 is not routable from the internet, so membership is a
        real statement about how the packet arrived."""
        assert not TAILNET.may_connect(peer)

    def test_the_host_header_guard_survives_the_widening(self):
        """DNS rebinding: a public name pointed at a private address."""
        assert TAILNET.may_host("mac.tail1234.ts.net")
        assert TAILNET.may_host("100.83.233.112:8765")
        assert not TAILNET.may_host("evil.example.com")
        assert not TAILNET.may_host("mac.tail1234.ts.net.evil.com")

    def test_a_policy_cannot_be_widened_after_construction(self):
        """Decided once, at startup. A request handler must not be able to
        change who is allowed in while it is serving."""
        with pytest.raises(Exception):
            TAILNET.tailscale_ip = "0.0.0.0"  # type: ignore[misc]

    def test_detection_fails_closed(self):
        """No Tailscale, a crash, or unreadable output all mean loopback only."""

        class _Proc:
            returncode = 1
            stdout = ""

        assert not detect_tailscale(runner=lambda *a, **k: _Proc()).tailscale_enabled

        def _explode(*_a, **_kw):
            raise OSError("no such binary")

        assert not detect_tailscale(runner=_explode).tailscale_enabled

        class _Garbage:
            returncode = 0
            stdout = "not json"

        assert not detect_tailscale(runner=lambda *a, **k: _Garbage()).tailscale_enabled


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------


class _Turn:
    def __init__(self, **kw):
        self.__dict__.update(
            {
                "heard": "",
                "said": "ok",
                "intent": "",
                "risk": "",
                "executed": False,
                "confirmation_id": "",
                **kw,
            }
        )

    def to_dict(self):
        return dict(self.__dict__)


class _Session:
    def __init__(self, session_id="s1"):
        self.id = session_id
        self.ended = False
        self.turns = []

    def greeting(self):
        return "Sir, I'm here."

    def audio_for(self, _text):
        return b"AUDIO" * 400

    def hear(self, wav):
        turn = _Turn(heard=wav.decode("utf-8", "ignore"), said="heard you")
        self.turns.append(turn)
        return turn

    def say(self, text):
        turn = _Turn(heard=text, said=f"you said {text}", intent="status", risk="READ")
        self.turns.append(turn)
        return turn

    def transcript(self):
        return [t.to_dict() for t in self.turns]


class _Sessions:
    def __init__(self, allow=True):
        self.allow = allow
        self.live = {}

    def start(self):
        if not self.allow:
            return None
        session = _Session(f"s{len(self.live) + 1}")
        self.live[session.id] = session
        return session

    def get(self, session_id):
        return self.live.get(session_id)

    def end(self, session_id, _reason=""):
        return self.live.pop(session_id, None) is not None


class _Confirmations:
    def __init__(self):
        self.items = []

    def pending(self):
        return self.items

    def approve(self, _id):
        return True

    def decline(self, _id):
        return True


def _endpoints(**kw):
    return VoiceEndpoints(
        sessions=kw.pop("sessions", None) or _Sessions(),
        confirmations=kw.pop("confirmations", None) or _Confirmations(),
        **kw,
    )


class TestVoiceRoutes:
    def test_answering_returns_a_session_and_audio(self):
        api = _endpoints()
        status, payload = api.handle_post("/api/voice/answer", b"", {})
        assert status == 200
        assert payload["session"]
        assert base64.b64decode(payload["audio"])

    def test_too_many_calls_is_refused_not_queued(self):
        api = _endpoints(sessions=_Sessions(allow=False))
        status, _ = api.handle_post("/api/voice/answer", b"", {})
        assert status == 429

    def test_an_utterance_for_a_dead_session_asks_the_page_to_reconnect(self):
        """The commonest real failure: the phone slept and the call was reaped."""
        api = _endpoints()
        status, payload = api.handle_post(
            "/api/voice/utterance", b"RIFF", {"id": ["gone"]}
        )
        assert status == 409
        assert payload["reconnect"] is True

    def test_an_oversized_utterance_is_refused_before_transcription(self):
        api = _endpoints()
        status, _ = api.handle_post(
            "/api/voice/utterance", b"x" * 2_000_000, {"id": ["s1"]}
        )
        assert status == 413

    def test_typed_text_takes_the_same_authority_path_as_speech(self):
        api = _endpoints()
        _, answer = api.handle_post("/api/voice/answer", b"", {})
        status, payload = api.handle_post(
            "/api/voice/text",
            json.dumps({"text": "what's the status"}).encode(),
            {"id": [answer["session"]]},
        )
        assert status == 200 and payload["intent"] == "status"

    def test_empty_or_malformed_text_is_refused(self):
        api = _endpoints()
        _, answer = api.handle_post("/api/voice/answer", b"", {})
        for body in (b"{}", b"not json", json.dumps({"text": "  "}).encode()):
            status, _ = api.handle_post(
                "/api/voice/text", body, {"id": [answer["session"]]}
            )
            assert status == 400

    def test_an_unknown_route_is_a_404_not_a_crash(self):
        api = _endpoints()
        assert api.handle_get("/api/voice/nope", {})[0] == 404
        assert api.handle_post("/api/voice/nope", b"", {})[0] == 404

    def test_the_transcript_survives_a_reload(self):
        api = _endpoints()
        _, answer = api.handle_post("/api/voice/answer", b"", {})
        api.handle_post(
            "/api/voice/text",
            json.dumps({"text": "hello"}).encode(),
            {"id": [answer["session"]]},
        )
        status, payload = api.handle_get(
            "/api/voice/session", {"id": [answer["session"]]}
        )
        assert status == 200 and payload["live"] is True
        assert len(payload["transcript"]) == 1

    def test_a_session_that_never_existed_is_not_an_error(self):
        status, payload = _endpoints().handle_get("/api/voice/session", {"id": ["x"]})
        assert status == 200 and payload["live"] is False


class TestIncomingCalls:
    def test_ringing_records_a_call_the_dashboard_can_show(self):
        api = _endpoints()
        api.ring(incident_id="INC-1", reason="post_merge_failure", detail="prod is red")
        status, payload = api.handle_get("/api/voice/pending", {})
        assert status == 200
        assert payload["call"]["incident_id"] == "INC-1"

    def test_answering_clears_the_waiting_call(self):
        api = _endpoints()
        api.ring(incident_id="INC-1", reason="x", detail="y")
        api.handle_post("/api/voice/answer", b"", {})
        assert api.handle_get("/api/voice/pending", {})[1]["call"] is None

    def test_a_call_waits_even_when_the_push_never_arrives(self):
        """A phone that was off still finds the call when it opens Sir."""
        api = _endpoints()
        api.ring(incident_id="INC-1", reason="x", detail="y")
        assert api.handle_get("/api/voice/pending", {})[1]["call"] is not None


class TestPushRegistration:
    def test_a_browser_subscription_is_stored(self, tmp_path):
        store = SubscriptionStore(path=tmp_path / "subs.json")
        api = _endpoints(subscriptions=store, push=None)
        status, payload = api.handle_post(
            "/api/voice/subscribe",
            json.dumps({"endpoint": "https://web.push.apple.com/abc"}).encode(),
            {},
        )
        assert status == 200 and payload["count"] == 1

    @pytest.mark.parametrize(
        "endpoint", ["http://evil.example.com/x", "file:///etc/passwd", "", "ftp://x"]
    )
    def test_only_https_push_endpoints_are_accepted(self, endpoint, tmp_path):
        api = _endpoints(subscriptions=SubscriptionStore(path=tmp_path / "s.json"))
        status, _ = api.handle_post(
            "/api/voice/subscribe", json.dumps({"endpoint": endpoint}).encode(), {}
        )
        assert status == 400

    def test_subscriptions_survive_a_restart(self, tmp_path):
        path = tmp_path / "subs.json"
        SubscriptionStore(path=path).add("https://web.push.apple.com/abc")
        assert len(SubscriptionStore(path=path).all()) == 1

    def test_only_the_public_key_is_ever_served(self, tmp_path):
        key = VapidKey.load_or_create(tmp_path / "vapid.json")
        api = _endpoints(push=PushSender(key=key))
        status, payload = api.handle_get("/api/voice/push-key", {})
        assert status == 200 and payload["key"] == key.application_server_key
        # The private half must not appear anywhere in the response.
        assert str(key.private) not in json.dumps(payload)


# ---------------------------------------------------------------------------
# VAPID
# ---------------------------------------------------------------------------


class TestVapid:
    def test_signatures_verify(self):
        key = VapidKey.generate()
        for i in range(10):
            message = f"message {i}".encode()
            assert verify(key.public_bytes, message, key.sign(message))

    def test_a_tampered_message_does_not_verify(self):
        key = VapidKey.generate()
        assert not verify(key.public_bytes, b"other", key.sign(b"hello"))

    def test_another_key_does_not_verify(self):
        key, other = VapidKey.generate(), VapidKey.generate()
        assert not verify(other.public_bytes, b"hello", key.sign(b"hello"))

    def test_the_public_key_is_an_uncompressed_p256_point(self):
        public = VapidKey.generate().public_bytes
        assert len(public) == 65 and public[0] == 0x04

    def test_the_jwt_has_the_shape_a_push_service_expects(self):
        key = VapidKey.generate()
        header, claims, _ = key.jwt("https://web.push.apple.com", "mailto:a@b").split(
            "."
        )
        assert json.loads(base64.urlsafe_b64decode(header + "==")) == {
            "typ": "JWT",
            "alg": "ES256",
        }
        payload = json.loads(base64.urlsafe_b64decode(claims + "=="))
        assert payload["aud"] == "https://web.push.apple.com"
        assert payload["sub"] == "mailto:a@b"
        assert payload["exp"] > 0

    def test_the_key_is_stored_private_and_reused(self, tmp_path):
        path = tmp_path / "vapid.json"
        first = VapidKey.load_or_create(path)
        assert oct(path.stat().st_mode)[-3:] == "600"
        assert VapidKey.load_or_create(path).private == first.private

    def test_a_corrupt_key_file_is_replaced_rather_than_fatal(self, tmp_path):
        path = tmp_path / "vapid.json"
        path.write_text("{ not json")
        assert VapidKey.load_or_create(path).private > 0

    def test_the_knock_carries_no_payload(self, tmp_path):
        """Incident detail must never transit a third-party push service."""
        sent = {}

        def _transport(endpoint, headers, timeout):
            sent["endpoint"] = endpoint
            sent["headers"] = headers
            return 201, ""

        key = VapidKey.load_or_create(tmp_path / "v.json")
        sender = PushSender(key=key, transport=_transport)
        delivered, _ = sender.send(
            WebPushSubscription(endpoint="https://web.push.apple.com/abc")
        )
        assert delivered
        assert sent["headers"]["Content-Length"] == "0"
        assert sent["headers"]["Authorization"].startswith("vapid t=")

    def test_an_expired_subscription_is_reported_for_removal(self, tmp_path):
        key = VapidKey.load_or_create(tmp_path / "v.json")
        sender = PushSender(key=key, transport=lambda *a: (410, "gone"))
        delivered, detail = sender.send(
            WebPushSubscription(endpoint="https://web.push.apple.com/abc")
        )
        assert not delivered and detail == "expired"

    def test_a_push_service_that_is_down_does_not_raise(self, tmp_path):
        def _explode(*_a):
            raise OSError("network down")

        key = VapidKey.load_or_create(tmp_path / "v.json")
        delivered, _ = PushSender(key=key, transport=_explode).send(
            WebPushSubscription(endpoint="https://web.push.apple.com/abc")
        )
        assert not delivered
