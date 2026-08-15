"""The Control Center's HTTP server — loopback only, stdlib only.

Built on :mod:`http.server` rather than a web framework, for two reasons that
both matter more than convenience. It adds **no dependency**: the reliability
system already runs on a machine that must stay up, and a monitoring UI is not
worth widening that machine's supply chain. And it makes the security surface
small enough to read in one sitting — there is no routing table to
misconfigure, no static-file handler to escape from, and no middleware whose
defaults have to be remembered.

The protections are deliberately layered, because each one covers a case the
others do not:

* **Bind address.** Only a loopback address may be bound at all.
* **Peer address.** Every request's source address is re-checked, so a
  misconfigured bind cannot serve a remote client.
* **Host header.** Rejected unless it names a loopback host, which is what
  stops a page on the public internet from pointing a DNS record at 127.0.0.1
  and reading this dashboard through the operator's own browser.
* **Control token.** The two lifecycle endpoints require a header carrying a
  token minted at startup and embedded in the page. A cross-site request can be
  *sent* to a loopback port, but it cannot *read* the page to learn the token,
  so it cannot forge one of these.
* **Method.** Everything is GET except two named POSTs. There is no PUT, no
  DELETE, and no route that mutates incidents, repairs or deployments.
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

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
}

_STATIC_DIR = Path(__file__).parent / "static"

#: No external origin is reachable from the page, so nothing can be exfiltrated
#: by a crafted incident title that survived redaction.
_CSP = (
    "default-src 'none'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "connect-src 'self'; "
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
    ) -> None:
        if not _is_loopback(host):
            raise ValueError(
                f"refusing to bind {host!r}: the Control Center is local-only. "
                "Use 127.0.0.1."
            )
        self.host = host
        self.port = port
        self.service = service
        self.allow_watcher_control = allow_watcher_control
        #: Minted per process. Embedded in the page, required by the two POSTs.
        self.control_token = secrets.token_urlsafe(32)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle --------------------------------------------------------

    @property
    def url(self) -> str:
        """The address to open in a browser."""
        return f"http://{self.host}:{self.port}"

    def _build(self) -> ThreadingHTTPServer:
        handler = _make_handler(self)
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET

        class _Server(ThreadingHTTPServer):
            address_family = family
            daemon_threads = True
            allow_reuse_address = True

        httpd = _Server((self.host, self.port), handler)
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
            """Reject anything that is not a local browser talking to us."""
            peer = (self.client_address or ("",))[0]
            if not _is_loopback(peer):
                # Belt and braces: the bind should already have made this
                # impossible. If it ever happens, it is the interesting case.
                logger.warning("refusing a non-loopback client: %s", peer)
                self._error(HTTPStatus.FORBIDDEN, "local connections only")
                return False
            host = self.headers.get("Host", "")
            if not _is_loopback(host):
                # DNS rebinding: a public name resolving to 127.0.0.1 arrives
                # with its own hostname in this header.
                logger.warning("refusing a non-loopback Host header: %r", host)
                self._error(HTTPStatus.FORBIDDEN, "unexpected Host header")
                return False
            return True

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
            self._error(HTTPStatus.NOT_FOUND, "no such route")

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            """The only two state-changing routes in the whole surface.

            Both ask launchd to run a service that is named in the code. Neither
            accepts a body, an argument, or a command, so there is nothing for a
            caller to influence beyond "start" versus "restart".
            """
            if not self._guard():
                return
            path = urlparse(self.path).path.rstrip("/") or "/"

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
            try:
                html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
            except OSError:
                return self._error(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "dashboard assets are missing"
                )
            html = html.replace("__CONTROL_TOKEN__", server.control_token)
            html = html.replace(
                "__WATCHER_CONTROL__",
                "true" if server.allow_watcher_control else "false",
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
