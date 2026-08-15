"""A tiny web application with deliberately broken variants.

Gives the probe tests something real to fail against: a JavaScript error, a
failed XHR, a wrong redirect, a 500, and a slow response — each reachable at its
own path so a probe can target exactly one failure mode.

Served by :mod:`http.server` on an ephemeral port, so the tests need no network
and no external service.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

__all__ = ["FixtureSite"]

_LOGIN_PAGE = """<!doctype html>
<html><head><title>Sign in</title></head><body>
  <h1>Sign in</h1>
  <form id="login" method="POST" action="/session">
    <input name="email" type="email" data-testid="email">
    <input name="password" type="password" data-testid="password">
    <button type="submit" data-testid="submit">Sign In</button>
  </form>
</body></html>
"""

_DASHBOARD_PAGE = """<!doctype html>
<html><head><title>Dashboard</title></head><body>
  <div data-testid="dashboard-root">
    <h1>Dashboard</h1>
    <p>Welcome back.</p>
  </div>
</body></html>
"""

_JS_ERROR_PAGE = """<!doctype html>
<html><head><title>Broken</title></head><body>
  <h1>Broken</h1>
  <script>window.notAFunction();</script>
</body></html>
"""

_XHR_FAIL_PAGE = """<!doctype html>
<html><head><title>Data</title></head><body>
  <div data-testid="dashboard-root">Loading</div>
  <script>
    fetch('/api/broken').then(function (r) {
      document.querySelector('[data-testid=dashboard-root]').textContent =
        r.ok ? 'ok' : 'api-error';
    });
  </script>
</body></html>
"""

_SLOW_PAGE = """<!doctype html>
<html><head><title>Slow</title></head><body><h1>Slow</h1></body></html>
"""

# Reproduces, with no Next.js involved, exactly what the Next.js App Router
# does to a probe: it starts a speculative `?_rsc=` prefetch, cancels it when
# the visitor goes elsewhere, and — because it was awaiting the response —
# logs its own console error before recovering with a full navigation.
#
# The console text is the real one, captured from
# https://www.wizeperformance.com/dashboard on 2026-08-15 (INC-00001), down to
# the wording of the sentence the pattern keys on. Chromium turns the abort
# into a genuine `net::ERR_ABORTED` requestfailed event, so this exercises both
# channels the profile has to cover, through a real browser.
_RSC_ABORT_PAGE = """<!doctype html>
<html><head><title>Prefetch</title></head><body>
  <h1 id="ready">Prefetch</h1>
  <script>
    var url = '/rsc-slow?_rsc=1obve';
    var controller = new AbortController();
    fetch(url, {signal: controller.signal}).catch(function (err) {
      console.error(
        'Failed to fetch RSC payload for ' + location.origin + url +
        '. Falling back to browser navigation.', err);
    });
    setTimeout(function () { controller.abort(); }, 100);
  </script>
</body></html>
"""

# The same benign prefetch abort, plus a genuinely broken API call that fails
# at the transport layer (port 1 refuses connections). A probe that names the
# RSC profile must still fail on this one — that is the whole point of scoping
# the pattern to `?_rsc=` and `net::ERR_ABORTED` rather than to "Failed to
# fetch".
_RSC_ABORT_PLUS_REAL_FAILURE_PAGE = """<!doctype html>
<html><head><title>Prefetch</title></head><body>
  <h1 id="ready">Prefetch</h1>
  <script>
    var url = '/rsc-slow?_rsc=1obve';
    var controller = new AbortController();
    fetch(url, {signal: controller.signal}).catch(function (err) {
      console.error(
        'Failed to fetch RSC payload for ' + location.origin + url +
        '. Falling back to browser navigation.', err);
    });
    setTimeout(function () { controller.abort(); }, 100);
    fetch('http://127.0.0.1:1/api/profile').catch(function () {});
  </script>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    """Routes for the fixture site."""

    # Silence per-request logging so pytest output stays readable.
    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
        return

    def _send(self, status: int, body: str, content_type: str = "text/html") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?")[0]
        if path in ("/", "/login"):
            self._send(200, _LOGIN_PAGE)
        elif path == "/dashboard":
            self._send(200, _DASHBOARD_PAGE)
        elif path == "/js-error":
            self._send(200, _JS_ERROR_PAGE)
        elif path == "/xhr-fail":
            self._send(200, _XHR_FAIL_PAGE)
        elif path == "/api/broken":
            self._send(500, '{"error":"boom"}', "application/json")
        elif path == "/boom":
            self._send(500, "<h1>Internal Server Error</h1>")
        elif path == "/rsc-prefetch-abort":
            self._send(200, _RSC_ABORT_PAGE)
        elif path == "/rsc-prefetch-abort-and-broken-api":
            self._send(200, _RSC_ABORT_PLUS_REAL_FAILURE_PAGE)
        elif path == "/rsc-slow":
            # Slow enough that the page's AbortController always wins the race,
            # so the request is genuinely in flight when it is cancelled.
            time.sleep(2.0)
            self._send(200, "{}", "application/json")
        elif path == "/slow":
            time.sleep(1.5)
            self._send(200, _SLOW_PAGE)
        elif path == "/redirect-loop-home":
            self._redirect("/login")
        elif path == "/api/health":
            self._send(200, '{"status":"ok"}', "application/json")
        else:
            self._send(404, "<h1>Not Found</h1>")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        if path == "/session":
            # The bug under test: authentication "succeeds" but the user is
            # bounced back to /login instead of reaching /dashboard.
            if self.server.broken_login:  # type: ignore[attr-defined]
                self._redirect("/login")
            else:
                self._redirect("/dashboard")
        elif path == "/echo":
            self._send(200, body, "text/plain")
        else:
            self._send(404, "<h1>Not Found</h1>")


class FixtureSite:
    """Context manager that serves the fixture site on an ephemeral port.

    Set :attr:`broken_login` to flip the login workflow between working and
    broken without restarting the server, so one test can assert both.
    """

    def __init__(self, *, broken_login: bool = False) -> None:
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._broken_login = broken_login

    @property
    def base_url(self) -> str:
        """Root URL of the running server."""
        if self._server is None:
            raise RuntimeError("FixtureSite is not started")
        host, port = self._server.server_address[:2]
        return (
            f"http://127.0.0.1:{port}"
            if host in ("0.0.0.0", "127.0.0.1")
            else f"http://{host}:{port}"
        )

    @property
    def broken_login(self) -> bool:
        """Whether ``POST /session`` bounces back to ``/login``."""
        return self._broken_login

    @broken_login.setter
    def broken_login(self, value: bool) -> None:
        self._broken_login = value
        if self._server is not None:
            self._server.broken_login = value  # type: ignore[attr-defined]

    def start(self) -> "FixtureSite":
        """Start serving in a daemon thread."""
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.broken_login = self._broken_login  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop serving and release the port."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "FixtureSite":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
