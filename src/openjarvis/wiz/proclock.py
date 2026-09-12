"""Compatibility re-export: :class:`ProcessLease` now lives in the core layer.

It was written here, for :meth:`FeaturePipeline.ship`. It then turned out that
the reliability repair loop needs the *same* lease — see
:func:`openjarvis.core.proclock.production_change_lease` — and ``reliability``
deliberately does not import ``wiz`` (the dependency runs the other way, and
inverting it to share a lock would be a poor trade). So the lease moved down to
:mod:`openjarvis.core.proclock`, which both layers may import.

This module stays as a re-export so existing importers keep working; new code
should import from :mod:`openjarvis.core.proclock` directly.
"""

from __future__ import annotations

from openjarvis.core.proclock import (
    PRODUCTION_CHANGE_LOCK,
    LeaseInfo,
    LeaseTimeout,
    ProcessLease,
    production_change_lease,
)

__all__ = [
    "LeaseTimeout",
    "LeaseInfo",
    "ProcessLease",
    "PRODUCTION_CHANGE_LOCK",
    "production_change_lease",
]
