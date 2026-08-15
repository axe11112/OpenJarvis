"""JARVIS Control Center — a local, read-only view of the reliability system.

This package is a **visualization layer**. It owns no monitoring logic, no
incident state and no probe state of its own: every value it renders is read
back out of the existing reliability system —
:class:`~openjarvis.reliability.store.IncidentStore`, the probe specs on disk,
the resolved :class:`~openjarvis.reliability.target.TargetConfig`, and
:class:`~openjarvis.reliability.diagnostic.LiveDiagnostic`.

Three properties are structural rather than incidental:

* **Read-only.** No route mutates anything. There is no repair button, no
  deploy button and no incident transition, because an HTTP surface that can
  reach production is a far larger risk than one that cannot.
* **Local-only.** The server refuses to bind anywhere but a loopback address
  and rejects requests whose ``Host`` header is not loopback, so a browser on
  another machine — or a DNS-rebinding page in this one — cannot reach it.
* **Non-interfering.** It never writes to the incident database, never starts a
  repair and never touches the emergency stop, so it is safe to run alongside
  ``jarvis reliability watch``.
"""

from openjarvis.reliability.dashboard.model import (
    CardView,
    IncidentView,
    OverallStatus,
    ProbeStatus,
    ProbeView,
    SafetyPanel,
    SafetyRow,
    Snapshot,
    build_snapshot,
    redact,
    wiz_message,
)
from openjarvis.reliability.dashboard.server import (
    LOOPBACK_HOSTS,
    ControlCenterServer,
    serve,
)
from openjarvis.reliability.dashboard.service import DashboardService

__all__ = [
    "CardView",
    "ControlCenterServer",
    "DashboardService",
    "IncidentView",
    "LOOPBACK_HOSTS",
    "OverallStatus",
    "ProbeStatus",
    "ProbeView",
    "SafetyPanel",
    "SafetyRow",
    "Snapshot",
    "build_snapshot",
    "redact",
    "serve",
    "wiz_message",
]
