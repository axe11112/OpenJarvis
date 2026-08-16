"""Who may reach the Control Center.

Until Sir Voice, the answer was "this machine, and nothing else", enforced four
times over: the bind refused a non-loopback address, the peer address was
re-checked per request, the ``Host`` header had to name a loopback host, and two
endpoints demanded a token minted at startup.

A phone cannot answer a call under that rule, so the rule widens — and this
module exists so that the widening is one reviewable object rather than four
edits scattered through a request handler. Every question about who is allowed
in is answered here, and the server asks rather than decides.

What changes is narrow and deliberate:

* The server may bind loopback **or exactly one** address: this machine's
  Tailscale IP. Never ``0.0.0.0``, never a LAN address, never a public one.
  ``0.0.0.0`` is refused explicitly rather than merely absent from an allowlist,
  because it is the value someone reaches for when a phone will not connect.
* A peer may be loopback, or an address inside Tailscale's CGNAT range —
  ``100.64.0.0/10`` and ``fd7a:115c:a1e0::/48``. Those ranges are not routable
  on the public internet, so a packet arriving from one arrived over the tailnet
  or not at all.
* The ``Host`` header may name a loopback host, the Tailscale IP, or this
  machine's MagicDNS name. This is the DNS-rebinding guard and it stays strict:
  a public name pointed at a private address still fails here.

Tailscale access is off unless a policy is built with an address, so the default
posture is unchanged and a misconfiguration cannot widen anything by accident.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from typing import FrozenSet, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "AccessPolicy",
    "LOOPBACK_HOSTS",
    "TAILSCALE_NETWORKS",
    "detect_tailscale",
    "loopback_policy",
]

#: The only hosts a loopback-only Control Center will bind to or accept.
LOOPBACK_HOSTS: FrozenSet[str] = frozenset({"127.0.0.1", "::1", "localhost", "[::1]"})

#: Tailscale's address space. Both are private by IANA assignment: the first is
#: the carrier-grade NAT range, the second Tailscale's ULA prefix. Neither is
#: reachable from the public internet, which is what makes membership of one a
#: meaningful statement about how a packet arrived.
TAILSCALE_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)

#: Bind addresses that are never acceptable, whatever else is configured.
_WILDCARDS = frozenset({"0.0.0.0", "::", "*", ""})


def _strip_port(host: str) -> str:
    """Reduce a ``Host`` header or address to a bare host."""
    cleaned = (host or "").strip().lower()
    if cleaned.startswith("["):
        return cleaned[1:].split("]", 1)[0]
    if cleaned.count(":") == 1:
        return cleaned.split(":", 1)[0]
    return cleaned


def _is_loopback_host(host: str) -> bool:
    """Whether *host* names this machine's loopback interface."""
    return bool(host) and _strip_port(host) in {"127.0.0.1", "::1", "localhost"}


@dataclass(frozen=True)
class AccessPolicy:
    """The complete answer to "may this connection happen".

    Frozen on purpose: the policy is decided once, at startup, from
    configuration, and a request handler must not be able to widen it while
    serving.
    """

    #: This machine's Tailscale address. Empty means Tailscale access is off and
    #: the Control Center behaves exactly as it always has.
    tailscale_ip: str = ""
    #: This machine's MagicDNS name, e.g. ``mac.tailnet.ts.net``. Accepted in
    #: the ``Host`` header so a browser can use the name rather than the number.
    tailscale_host: str = ""

    @property
    def tailscale_enabled(self) -> bool:
        """Whether anything beyond loopback is permitted at all."""
        return bool(self.tailscale_ip)

    # -- binding ----------------------------------------------------------

    def may_bind(self, host: str) -> bool:
        """Whether the server may listen on *host*.

        A wildcard is refused before anything else. Binding ``0.0.0.0`` on a
        laptop puts the Control Center on every café network it ever joins, and
        it is exactly the change someone makes at 2am when a phone will not
        connect.
        """
        cleaned = _strip_port(host)
        if cleaned in _WILDCARDS:
            return False
        if cleaned in LOOPBACK_HOSTS or _is_loopback_host(cleaned):
            return True
        return self.tailscale_enabled and cleaned == self.tailscale_ip.lower()

    def bind_refusal(self, host: str) -> str:
        """Why *host* was refused, for an error a human has to act on."""
        cleaned = _strip_port(host)
        if cleaned in _WILDCARDS:
            return (
                f"refusing to bind {host!r}: that listens on every network this "
                "machine joins. Use 127.0.0.1, or this machine's Tailscale "
                "address for phone access."
            )
        if self.tailscale_enabled:
            return (
                f"refusing to bind {host!r}: the Control Center listens on "
                f"127.0.0.1 or {self.tailscale_ip} only."
            )
        return (
            f"refusing to bind {host!r}: the Control Center is local-only. "
            "Use 127.0.0.1, or configure Tailscale access."
        )

    # -- peers ------------------------------------------------------------

    def may_connect(self, peer: str) -> bool:
        """Whether a request from *peer* may be served."""
        cleaned = _strip_port(peer)
        if not cleaned:
            return False
        if _is_loopback_host(cleaned):
            return True
        if not self.tailscale_enabled:
            return False
        try:
            address = ipaddress.ip_address(cleaned)
        except ValueError:
            return False
        return any(address in network for network in TAILSCALE_NETWORKS)

    # -- the Host header --------------------------------------------------

    def may_host(self, header: str) -> bool:
        """Whether *header* is a name this server answers to.

        The rebinding guard. A page on the public internet can resolve its own
        hostname to a private address and make the browser issue requests here;
        what it cannot do is change the ``Host`` header the browser sends.
        """
        cleaned = _strip_port(header)
        if not cleaned:
            return False
        if _is_loopback_host(cleaned):
            return True
        if not self.tailscale_enabled:
            return False
        if cleaned == self.tailscale_ip.lower():
            return True
        return bool(
            self.tailscale_host
        ) and cleaned == self.tailscale_host.lower().rstrip(".")

    def describe(self) -> str:
        """One line for the startup banner and the diagnostic."""
        if not self.tailscale_enabled:
            return "loopback only (127.0.0.1)"
        return (
            f"loopback and Tailscale only ({self.tailscale_ip}"
            + (f", {self.tailscale_host}" if self.tailscale_host else "")
            + "); not reachable from the public internet"
        )


def loopback_policy() -> AccessPolicy:
    """The historical posture: this machine and nothing else."""
    return AccessPolicy()


def detect_tailscale(runner: Optional[object] = None) -> AccessPolicy:
    """Build a policy from the local Tailscale daemon, if it is running.

    Returns a loopback-only policy when Tailscale is absent, logged out, or
    unreadable. Failing closed matters more here than being helpful: an
    exception swallowed into "assume it is fine" would widen access on the
    strength of a command that did not work.
    """
    import json
    import shutil
    import subprocess

    binary = shutil.which("tailscale") or (
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    )
    try:
        proc = (runner or subprocess.run)(  # type: ignore[operator]
            [binary, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:  # noqa: BLE001 - no Tailscale is a normal state
        logger.debug("tailscale status unavailable; staying loopback-only")
        return loopback_policy()

    if getattr(proc, "returncode", 1) != 0:
        return loopback_policy()
    try:
        status = json.loads(getattr(proc, "stdout", "") or "{}")
        self_node = status.get("Self") or {}
        addresses = [str(a) for a in (self_node.get("TailscaleIPs") or [])]
        ipv4 = next((a for a in addresses if ":" not in a), "")
        name = str(self_node.get("DNSName") or "").rstrip(".")
    except Exception:  # noqa: BLE001
        logger.warning("could not parse tailscale status; staying loopback-only")
        return loopback_policy()

    if not ipv4:
        return loopback_policy()
    return AccessPolicy(tailscale_ip=ipv4, tailscale_host=name)
