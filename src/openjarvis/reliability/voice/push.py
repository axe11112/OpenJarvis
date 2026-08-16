"""Ringing a phone that is not on the tailnet's screen.

Web Push exists for one reason here: a phone in a pocket has no page open, and a
tailnet cannot wake it. Apple's and Google's push services can, so the *knock*
travels over the public internet — and nothing else does.

**The knock carries no payload.** Web Push can encrypt data end to end, and
using it would still mean incident text passing through a third party's
infrastructure. Instead the push is empty; the service worker wakes, asks the
Mac over Tailscale what is wrong, and renders the notification from that. A push
service that logged every request learns that something happened, at a time, to
a subscription — never what.

That choice also removes the entire ECDH/HKDF/AES128GCM payload-encryption
stack, leaving one cryptographic operation: an ES256 signature over a small JWT.
That is implemented here, on P-256, in about a hundred lines of modular
arithmetic, because the Control Center is deliberately dependency-free and
adding a crypto library to the machine that watches production — to sign a
sixty-byte token — is the wrong trade.

The keypair is generated once and stored at 0600 beside the other JARVIS
secrets. The public half is handed to the browser; the private half never
leaves the file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

__all__ = ["PushSender", "VapidKey", "WebPushSubscription"]

# -- NIST P-256 ------------------------------------------------------------
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

Point = Optional[Tuple[int, int]]  # None is the point at infinity


def _inv(value: int, modulus: int) -> int:
    return pow(value, -1, modulus)


def _add(p: Point, q: Point) -> Point:
    if p is None:
        return q
    if q is None:
        return p
    (x1, y1), (x2, y2) = p, q
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p == q:
        lam = (3 * x1 * x1 + _A) * _inv(2 * y1, _P) % _P
    else:
        lam = (y2 - y1) * _inv(x2 - x1, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    return (x3, (lam * (x1 - x3) - y1) % _P)


def _mul(k: int, point: Point) -> Point:
    """Double-and-add. Not constant time — see the note on threat model below.

    A timing side channel here would leak the VAPID private key to something
    able to measure this process's execution time precisely. That is a local
    attacker on the operator's own Mac, who by then has strictly better options
    than recovering a key whose only power is to ring a phone.
    """
    result: Point = None
    addend = point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def _b64(data: bytes) -> str:
    """base64url without padding, as every Web Push field expects."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


@dataclass
class VapidKey:
    """The application server's identity to a push service."""

    private: int
    public_x: int
    public_y: int

    @classmethod
    def generate(cls) -> "VapidKey":
        private = secrets.randbelow(_N - 1) + 1
        point = _mul(private, (_GX, _GY))
        assert point is not None
        return cls(private=private, public_x=point[0], public_y=point[1])

    @property
    def public_bytes(self) -> bytes:
        """Uncompressed point: ``0x04 || X || Y``."""
        return (
            b"\x04"
            + self.public_x.to_bytes(32, "big")
            + self.public_y.to_bytes(32, "big")
        )

    @property
    def application_server_key(self) -> str:
        """What the browser passes to ``pushManager.subscribe``."""
        return _b64(self.public_bytes)

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> Dict[str, str]:
        return {
            "private": _b64(self.private.to_bytes(32, "big")),
            "public": self.application_server_key,
        }

    @classmethod
    def load_or_create(cls, path: Path) -> "VapidKey":
        """Read the keypair, generating and storing one the first time.

        Written 0600 before anything is put in it, so the private half is never
        briefly world-readable on a shared machine.
        """
        try:
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                private = int.from_bytes(
                    base64.urlsafe_b64decode(raw["private"] + "=="), "big"
                )
                point = _mul(private, (_GX, _GY))
                assert point is not None
                return cls(private=private, public_x=point[0], public_y=point[1])
        except Exception:  # noqa: BLE001 - a corrupt key is replaced, not fatal
            logger.warning(
                "voice: unreadable VAPID key at %s; generating a new one", path
            )

        key = cls.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(key.to_dict(), fh)
        return key

    # -- signing ----------------------------------------------------------

    def sign(self, message: bytes) -> bytes:
        """ECDSA-P256-SHA256, as the 64-byte ``r || s`` JOSE expects."""
        digest = int.from_bytes(hashlib.sha256(message).digest(), "big")
        while True:
            k = secrets.randbelow(_N - 1) + 1
            point = _mul(k, (_GX, _GY))
            if point is None:
                continue
            r = point[0] % _N
            if r == 0:
                continue
            s = (_inv(k, _N) * (digest + r * self.private)) % _N
            if s == 0:
                continue
            # Low-s form. Not required by JOSE, but it is the convention every
            # verifier is tested against and it costs one comparison.
            if s > _N // 2:
                s = _N - s
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def jwt(self, audience: str, subject: str, *, ttl_seconds: int = 12 * 3600) -> str:
        """A signed VAPID token for one push service origin."""
        header = _b64(
            json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()
        )
        claims = _b64(
            json.dumps(
                {
                    "aud": audience,
                    "exp": int(time.time()) + ttl_seconds,
                    "sub": subject,
                },
                separators=(",", ":"),
            ).encode()
        )
        signing_input = f"{header}.{claims}".encode("ascii")
        return f"{header}.{claims}.{_b64(self.sign(signing_input))}"


@dataclass
class WebPushSubscription:
    """One registered phone."""

    endpoint: str
    #: Present in a real subscription and deliberately unused: they exist to
    #: encrypt a payload, and this sender never sends one.
    keys: Dict[str, str] = field(default_factory=dict)
    added_at: str = ""

    @property
    def origin(self) -> str:
        parsed = urlparse(self.endpoint)
        return f"{parsed.scheme}://{parsed.netloc}"

    def to_dict(self) -> Dict[str, Any]:
        return {"endpoint": self.endpoint, "added_at": self.added_at}


@dataclass
class PushSender:
    """Sends empty, VAPID-signed knocks to registered phones."""

    key: VapidKey
    subject: str = "mailto:jarvis@localhost"
    #: How long a push service should hold the knock for a phone that is off.
    ttl_seconds: int = 600
    timeout_seconds: float = 10.0
    transport: Any = None

    def send(self, subscription: WebPushSubscription) -> Tuple[bool, str]:
        """Ring one phone. Returns ``(delivered, detail)``; never raises."""
        try:
            token = self.key.jwt(subscription.origin, self.subject)
        except Exception as exc:  # noqa: BLE001
            logger.exception("voice: could not sign a VAPID token")
            return False, f"could not sign: {exc}"

        headers = {
            "Authorization": f"vapid t={token}, k={self.key.application_server_key}",
            "TTL": str(self.ttl_seconds),
            "Content-Length": "0",
            # Declaring the (absent) payload's encoding keeps strict services
            # happy about an empty body.
            "Content-Encoding": "aes128gcm",
            "Urgency": "high",
        }
        try:
            status, body = (self.transport or self._post)(
                subscription.endpoint, headers, self.timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - a phone that cannot be rung
            logger.warning("voice: push failed: %s", exc)
            return False, str(exc)

        if 200 <= status < 300:
            return True, "delivered"
        if status in (404, 410):
            # The subscription is dead — the app was deleted or the browser
            # rotated it. The caller drops it rather than retrying forever.
            return False, "expired"
        return False, f"push service returned {status}: {body[:120]}"

    @staticmethod
    def _post(
        endpoint: str, headers: Dict[str, str], timeout: float
    ) -> Tuple[int, str]:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            endpoint, data=b"", headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return response.status, ""
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")


def verify(public_bytes: bytes, message: bytes, signature: bytes) -> bool:
    """Verify an ES256 signature. Present so the signer can be tested honestly.

    A signing implementation nothing ever checks is a signing implementation
    that might be producing garbage a push service silently rejects.
    """
    if len(public_bytes) != 65 or public_bytes[0] != 4 or len(signature) != 64:
        return False
    x = int.from_bytes(public_bytes[1:33], "big")
    y = int.from_bytes(public_bytes[33:], "big")
    if (y * y - (x * x * x + _A * x + _B)) % _P != 0:
        return False  # not on the curve
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not (1 <= r < _N and 1 <= s < _N):
        return False
    digest = int.from_bytes(hashlib.sha256(message).digest(), "big")
    w = _inv(s, _N)
    point = _add(_mul(digest * w % _N, (_GX, _GY)), _mul(r * w % _N, (x, y)))
    return point is not None and point[0] % _N == r


@dataclass
class SubscriptionStore:
    """Registered phones, on disk, so a restart does not lose the alarm."""

    path: Optional[Path] = None
    _items: Dict[str, WebPushSubscription] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._load()

    def add(
        self, endpoint: str, keys: Optional[Dict[str, str]] = None
    ) -> WebPushSubscription:
        from openjarvis.reliability.types import now_iso

        subscription = WebPushSubscription(
            endpoint=endpoint, keys=dict(keys or {}), added_at=now_iso()
        )
        self._items[endpoint] = subscription
        self._save()
        return subscription

    def remove(self, endpoint: str) -> bool:
        found = self._items.pop(endpoint, None) is not None
        if found:
            self._save()
        return found

    def all(self) -> List[WebPushSubscription]:
        return list(self._items.values())

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            for raw in json.loads(self.path.read_text(encoding="utf-8")):
                self._items[raw["endpoint"]] = WebPushSubscription(
                    endpoint=raw["endpoint"], added_at=raw.get("added_at", "")
                )
        except Exception:  # noqa: BLE001
            logger.warning("voice: could not read push subscriptions")

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps([s.to_dict() for s in self._items.values()], indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.exception("voice: could not persist push subscriptions")
