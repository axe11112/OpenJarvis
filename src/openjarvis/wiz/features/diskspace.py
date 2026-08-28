"""A cheap preflight against disk exhaustion.

FEAT-00007 crashed a coding session with a bare V8 "out of memory" fatal
error; the machine was actually out of *disk*, not memory — swap and Node's
own temp writes fail the same way when there is nowhere left to write. Node
does not report that honestly, so the pipeline has to check before handing it
work rather than after.

Checked with :func:`shutil.disk_usage` against the volume feature worktrees
are built on — the same statvfs-style call ``df`` uses, so it is cheap enough
to run before every step.
"""

from __future__ import annotations

import shutil

__all__ = ["DEFAULT_MIN_FREE_BYTES", "free_bytes", "has_enough_disk"]

#: Below this, a Claude session, an npm install or a `next build` is more
#: likely to crash mid-work than to finish — see FEAT-00007 and FEAT-00008.
DEFAULT_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


def free_bytes(path: str = "/") -> int:
    """Bytes free on the volume containing *path*."""
    return shutil.disk_usage(path).free


def has_enough_disk(
    path: str = "/", *, min_free_bytes: int = DEFAULT_MIN_FREE_BYTES
) -> bool:
    """Whether *path*'s volume has at least *min_free_bytes* free.

    A path that cannot be statted (removed out from under the caller, a
    permissions problem) is treated as unsafe rather than as "yes, proceed" —
    unknown is not the same as enough.
    """
    try:
        return free_bytes(path) >= min_free_bytes
    except OSError:
        return False
