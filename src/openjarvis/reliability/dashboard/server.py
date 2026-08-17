"""The Control Center's HTTP server — private-network only, stdlib only.

Built on :mod:`http.server` rather than a web framework, for two reasons that
both matter more than convenience. It adds **no dependency**: the reliability
system already runs on a machine that must stay up, and a monitoring UI is not
worth widening that machine's supply chain. And it makes the security surface
small enough to read in one sitting — there is no routing table to
misconfigure, no static-file handler to escape from, and no middleware whose
defaults have to be remembered.

The protections are deliberately layered, because each one covers a case the
others do not:

* **Bind address.** Loopback, or — when Sir Voice is serving a phone — this
  machine's Tailscale address and nothing else. A wildcard bind is refused
  outright.
* **Peer address.** Every request's source address is re-checked against the
  same policy, so a misconfigured bind cannot serve a client that policy would
  not have admitted.
* **Host header.** Rejected unless it names a host this server answers to. This
  is what stops a page on the public internet from pointing a DNS record at a
  private address and reading this dashboard through the operator's own browser.
* **Control token.** Every state-changing endpoint requires a header carrying a
  token minted at startup and embedded in the page. A cross-site request can be
  *sent*, but it cannot *read* the page to learn the token, so it cannot forge
  one.
* **Method.** Everything is GET except named POSTs. There is no PUT, no DELETE,
  and no route that mutates incidents, repairs or deployments.

Who counts as "allowed" lives in
:mod:`~openjarvis.reliability.dashboard.access`, in one object, so the answer
can be read and changed in one place rather than inferred from four.

With no access policy supplied this is loopback-only and byte-for-byte the
server it has always been.
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
import ssl
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from openjarvis.reliability.dashboard.access import AccessPolicy

logger = logging.getLogger(__name__)

__all__ = ["ControlCenterServer", "LOOPBACK_HOSTS", "serve"]

#: The only hosts this server will bind to or accept a ``Host`` header for.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "[::1]"})

#: Static assets, by exact name. An allowlist rather than a directory walk:
#: there is then no path to traverse, however a request is spelled.
_STATIC = {
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "wiz.svg": "image/svg+xml",
    "voice.js": "text/javascript; charset=utf-8",
    "voice.css": "text/css; charset=utf-8",
    "sw.js": "text/javascript; charset=utf-8",
    "manifest.webmanifest": "application/manifest+json; charset=utf-8",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
}

#: Largest request body accepted anywhere. One utterance, plus slack for the
#: base64 and JSON around it.
_MAX_VOICE_BODY = 2_000_000

#: Pages served from the root, by exact name. Same allowlist discipline as
#: static assets: there is no path to traverse because there is no path.
_PAGES = {"index.html", "voice.html"}

_STATIC_DIR = Path(__file__).parent / "static"

#: No external origin is reachable from the page, so nothing can be exfiltrated
#: by a crafted incident title that survived redaction.
_CSP = (
    "default-src 'none'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    # Sir's replies arrive as audio the page turns into a blob: URL. Same
    # origin in every practical sense — a blob is minted by this page from
    # bytes this server sent — and without it the phone stays silent.
    "media-src 'self' blob:; "
    "worker-src 'self'; "
    "manifest-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)


def _is_loopback(host: str) -> bool:
    """Whether *host* names this machine's loopback interface."""
    if not host:
        return False
    cleaned = host.strip().lower()
    # Strip a port, and the brackets IPv6 authorities carry.
    if cleaned.startswith("["):
        cleaned = cleaned[1:].split("]", 1)[0]
    elif cleaned.count(":") == 1:
        cleaned = cleaned.split(":", 1)[0]
    return cleaned in {"127.0.0.1", "::1", "localhost"}


def _tls_context(certfile: str, keyfile: str) -> "ssl.SSLContext":
    """A TLS context for the certificate Tailscale issued.

    TLS 1.2 is the floor. The certificate is a real, publicly-trusted one from
    Tailscale's ACME integration, which is what lets a phone treat this origin
    as secure — and a secure origin is not decoration here: without it a browser
    refuses microphone access and Web Push outright.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return context


class ControlCenterServer:
    """A local HTTP server serving one read-only dashboard.

    Parameters
    ----------
    service:
        The :class:`~openjarvis.reliability.dashboard.service.DashboardService`
        that answers every request.
    host, port:
        Where to listen. A non-loopback *host* is refused rather than bound.
    allow_watcher_control:
        Whether the two launchd lifecycle endpoints are mounted at all. When
        ``False`` they return 404, so the capability is absent rather than
        merely hidden.
    """

    def __init__(
        self,
        service: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        allow_watcher_control: bool = True,
        access: Optional["AccessPolicy"] = None,
        certfile: str = "",
        keyfile: str = "",
        voice: Any = None,
    ) -> None:
        from openjarvis.reliability.dashboard.access import loopback_policy

        self.access = access or loopback_policy()
        if not self.access.may_bind(host):
            raise ValueError(self.access.bind_refusal(host))
        self.host = host
        self.port = port
        self.service = service
        self.allow_watcher_control = allow_watcher_control
        self.certfile = certfile
        self.keyfile = keyfile
        #: Optional :class:`~openjarvis.reliability.voice.web.VoiceEndpoints`.
        #: ``None`` — the default — means the voice routes are absent rather
        #: than merely disabled: they 404 like any other unknown path.
        self.voice = voice
        #: Minted per process. Embedded in the page, required by every POST.
        self.control_token = secrets.token_urlsafe(32)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

        if self._serving_beyond_loopback and not self.tls:
            # A phone will refuse the microphone on this origin, and any token
            # in the page crosses the tailnet in the clear. The tailnet is
            # encrypted, so this is not a disaster — but it is not what was
            # asked for, and a silent downgrade is how it stays that way.
            logger.warning(
                "Control Center is reachable from %s over plain HTTP; "
                "microphone access and Web Push will not work on a phone. "
                "Issue a certificate with `tailscale cert %s`.",
                self.host,
                self.access.tailscale_host or self.host,
            )

    # -- lifecycle --------------------------------------------------------

    @property
    def tls(self) -> bool:
        """Whether this server terminates TLS."""
        return bool(self.certfile and self.keyfile)

    @property
    def _serving_beyond_loopback(self) -> bool:
        return not _is_loopback(self.host)

    @property
    def url(self) -> str:
        """The address to open in a browser."""
        scheme = "https" if self.tls else "http"
        host = self.host
        if self.tls and self.access.tailscale_host and self._serving_beyond_loopback:
            # The certificate is issued for the MagicDNS name; reaching the same
            # server by IP would be a certificate error, not a security event,
            # but it looks like one to whoever is holding the phone.
            host = self.access.tailscale_host
        return f"{scheme}://{host}:{self.port}"

    def _build(self) -> ThreadingHTTPServer:
        handler = _make_handler(self)
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET

        class _Server(ThreadingHTTPServer):
            address_family = family
            daemon_threads = True
            allow_reuse_address = True

        httpd = _Server((self.host, self.port), handler)
        if self.tls:
            httpd.socket = _tls_context(self.certfile, self.keyfile).wrap_socket(
                httpd.socket, server_side=True
            )
        self.port = httpd.server_address[1]
        return httpd

    def serve_forever(self) -> None:
        """Block, serving until :meth:`shutdown`."""
        self._httpd = self._build()
        try:
            self._httpd.serve_forever(poll_interval=0.5)
        finally:
            self._httpd.server_close()

    def start_background(self) -> None:
        """Serve on a daemon thread — used by tests and by ``--open``."""
        self._httpd = self._build()
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
            name="jarvis-control-center-http",
        )
        self._thread.start()

    def shutdown(self) -> None:
        """Stop serving and close the socket."""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def _make_handler(server: ControlCenterServer) -> type:
    """Build a handler class bound to *server*."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "JARVISControlCenter"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # -- plumbing ---------------------------------------------------

        def log_message(self, fmt: str, *args: Any) -> None:
            """Route access logs through logging, at debug level.

            The default writes to stderr unconditionally, which would bury the
            one line the operator actually needs — the URL to open.
            """
            logger.debug("control-center %s", fmt % args)

        def _send(
            self,
            status: HTTPStatus,
            body: bytes,
            content_type: str,
            *,
            extra: Optional[Dict[str, str]] = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", _CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _error(self, status: HTTPStatus, message: str) -> None:
            self._json(status, {"error": message, "status": int(status)})

        # -- guards -----------------------------------------------------

        def _guard(self) -> bool:
            """Reject anything that is not an allowed browser talking to us.

            Still two independent checks, still both mandatory — only the
            definition of "allowed" moved into
            :class:`~openjarvis.reliability.dashboard.access.AccessPolicy`. With
            no Tailscale configured this is bit-for-bit the old behaviour.
            """
            peer = (self.client_address or ("",))[0]
            if not server.access.may_connect(peer):
                # Belt and braces: the bind should already have made this
                # impossible. If it ever happens, it is the interesting case.
                logger.warning("refusing a client outside the allowed range: %s", peer)
                self._error(HTTPStatus.FORBIDDEN, "private network connections only")
                return False
            host = self.headers.get("Host", "")
            if not server.access.may_host(host):
                # DNS rebinding: a public name resolving to a private address
                # arrives with its own hostname in this header.
                logger.warning("refusing an unexpected Host header: %r", host)
                self._error(HTTPStatus.FORBIDDEN, "unexpected Host header")
                return False
            return True

        def _read_body(self, limit: int) -> bytes:
            """Read at most *limit* bytes of request body.

            Bounded because the largest body this server accepts is an utterance,
            and an unbounded read on a machine that has to stay up is a way to
            stop it staying up.
            """
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return b""
            return self.rfile.read(min(length, limit))

        def _authorized(self) -> bool:
            """Whether a state-changing request carries the control token."""
            supplied = self.headers.get("X-JARVIS-Control", "")
            if not supplied or not secrets.compare_digest(
                supplied, server.control_token
            ):
                self._error(HTTPStatus.FORBIDDEN, "missing or invalid control token")
                return False
            return True

        # -- routing ----------------------------------------------------

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
            """Serve headers only, through the same routing as GET."""
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            """Every read route."""
            if not self._guard():
                return
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/":
                return self._serve_index()
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/") :])
            if path == "/api/snapshot":
                return self._json(HTTPStatus.OK, server.service.snapshot().to_dict())
            if path == "/api/watcher":
                return self._json(
                    HTTPStatus.OK, server.service.watcher_state().to_dict()
                )
            if path == "/api/watcher/logs":
                return self._serve_watcher_log(parsed.query)
            if path.startswith("/api/incidents/"):
                return self._serve_incident(path[len("/api/incidents/") :])
            if path == "/voice":
                return self._serve_page("voice.html")
            if path == "/manifest.webmanifest":
                return self._serve_static("manifest.webmanifest")
            if path == "/sw.js":
                # Served from the root on purpose: a service worker may only
                # control pages at or below its own path, and one delivered from
                # /static/ could never wake the call screen.
                return self._serve_static("sw.js")
            if path.startswith("/api/voice/"):
                if server.voice is None:
                    return self._error(HTTPStatus.NOT_FOUND, "voice is not enabled")
                status, payload = server.voice.handle_get(path, parse_qs(parsed.query))
                return self._json(HTTPStatus(status), payload)
            self._error(HTTPStatus.NOT_FOUND, "no such route")

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            """The only two state-changing routes in the whole surface.

            Both ask launchd to run a service that is named in the code. Neither
            accepts a body, an argument, or a command, so there is nothing for a
            caller to influence beyond "start" versus "restart".
            """
            if not self._guard():
                return
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path.startswith("/api/voice/"):
                if server.voice is None:
                    return self._error(HTTPStatus.NOT_FOUND, "voice is not enabled")
                if not self._authorized():
                    return
                body = self._read_body(_MAX_VOICE_BODY)
                status, payload = server.voice.handle_post(
                    path,
                    body,
                    parse_qs(parsed.query),
                    device=self.headers.get("User-Agent", "")[:200],
                )
                return self._json(HTTPStatus(status), payload)

            if not server.allow_watcher_control:
                return self._error(HTTPStatus.NOT_FOUND, "watcher control is disabled")
            if path not in ("/api/watcher/start", "/api/watcher/restart"):
                return self._error(HTTPStatus.NOT_FOUND, "no such route")
            if not self._authorized():
                return

            # Drain any body so the connection stays usable, and ignore it.
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, 4096))

            if path.endswith("/start"):
                ok, message = server.service.start_watcher()
            else:
                ok, message = server.service.restart_watcher()
            self._json(
                HTTPStatus.OK if ok else HTTPStatus.CONFLICT,
                {
                    "ok": ok,
                    "message": message,
                    "watcher": server.service.watcher_state().to_dict(),
                },
            )

        def do_PUT(self) -> None:  # noqa: N802 - stdlib naming
            """Refused. The dashboard has no update semantics."""
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "read-only")

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib naming
            """Refused. Nothing here can delete anything."""
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "read-only")

        def do_PATCH(self) -> None:  # noqa: N802 - stdlib naming
            """Refused."""
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "read-only")

        # -- handlers ---------------------------------------------------

        def _serve_index(self) -> None:
            self._serve_page("index.html")

        def _serve_page(self, name: str) -> None:
            """Serve one of the two HTML pages, with the token substituted in.

            The token reaches the browser only by being baked into a page this
            server decided to send. A cross-origin page can make a browser issue
            a request here, but it cannot read the response, so it never learns
            the token and every POST it forges is refused.
            """
            if name not in _PAGES:
                return self._error(HTTPStatus.NOT_FOUND, "no such page")
            try:
                html = (_STATIC_DIR / name).read_text(encoding="utf-8")
            except OSError:
                return self._error(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "dashboard assets are missing"
                )
            html = html.replace("__CONTROL_TOKEN__", server.control_token)
            html = html.replace(
                "__WATCHER_CONTROL__",
                "true" if server.allow_watcher_control else "false",
            )
            html = html.replace(
                "__VOICE_ENABLED__", "true" if server.voice is not None else "false"
            )
            self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")

        def _serve_static(self, name: str) -> None:
            content_type = _STATIC.get(name)
            if content_type is None:
                return self._error(HTTPStatus.NOT_FOUND, "no such asset")
            try:
                body = (_STATIC_DIR / name).read_bytes()
            except OSError:
                return self._error(HTTPStatus.NOT_FOUND, "no such asset")
            self._send(HTTPStatus.OK, body, content_type)

        def _serve_incident(self, incident_id: str) -> None:
            if not incident_id or "/" in incident_id:
                return self._error(HTTPStatus.NOT_FOUND, "no such incident")
            payload = server.service.incident(incident_id)
            if payload is None:
                return self._error(HTTPStatus.NOT_FOUND, "no such incident")
            self._json(HTTPStatus.OK, payload)

        def _serve_watcher_log(self, query: str) -> None:
            stream = (parse_qs(query).get("stream") or ["stderr"])[0]
            if stream not in ("stdout", "stderr"):
                return self._error(HTTPStatus.BAD_REQUEST, "unknown stream")
            supervisor = server.service.supervisor
            self._json(
                HTTPStatus.OK,
                {
                    "stream": stream,
                    # Redacted inside tail_log, like every other piece of text
                    # this server emits.
                    "lines": supervisor.tail_log(stream=stream, lines=60),
                },
            )

    return Handler


def serve(
    service: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_watcher_control: bool = True,
) -> Tuple[ControlCenterServer, None]:
    """Build a server and block, serving it. Returns only on shutdown."""
    control = ControlCenterServer(
        service,
        host=host,
        port=port,
        allow_watcher_control=allow_watcher_control,
    )
    control.serve_forever()
    return control, None
