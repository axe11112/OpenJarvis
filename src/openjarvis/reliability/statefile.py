"""Writing state that has to survive the machine going away mid-write.

Every durable file JARVIS keeps exists because the process does not get to
choose when it stops. The notification ledger's own docstring says so plainly —
"the watcher restarts whenever the machine sleeps, the code updates, or launchd
decides to" — and each of those can land between a truncate and a write.

``Path.write_text`` truncates first and writes second. Interrupted in between,
it leaves a file that exists, is readable, and is not valid JSON. Every loader
here does the sensible-looking thing with that: log a warning and start from an
empty dict, because a corrupt file must not crash the watcher. The result is
that a badly-timed sleep silently erases the record of what the owner has
already been told, and they get told all of it again — which is the exact
failure the ledger was written to prevent.

So the write goes to a sibling temporary file, is flushed to disk, and is then
moved into place with ``os.replace``, which is atomic on POSIX. A reader sees
either the old file or the new one, never half of either. The directory is
fsynced afterwards so the rename itself survives a power loss, which matters on
a laptop that sleeps.

Not a general-purpose utility: it deliberately does no locking and assumes one
writer, which is what every caller here is.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = ["write_bytes_atomic", "write_json_atomic", "write_text_atomic"]


def write_bytes_atomic(
    path: Path, payload: bytes, *, mode: Optional[int] = None
) -> bool:
    """Replace *path* with *payload* atomically. ``True`` when it landed.

    Never raises: a caller that could not persist its state is in the same
    position it was in before this module existed, and none of them can do
    anything useful about it beyond logging.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("could not create the directory for %s", path)
        return False

    tmp_name = ""
    try:
        # Same directory as the target: os.replace is only atomic within a
        # filesystem, and /tmp is routinely a different one.
        handle, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        with os.fdopen(handle, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, str(path))
        tmp_name = ""
    except OSError:
        logger.exception("could not write %s", path)
        return False
    finally:
        if tmp_name:
            # The replace never happened, so the temporary file is litter.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    # The rename is durable only once the directory entry is. Failing here
    # means the data is written but the rename might not survive power loss,
    # which is not worth reporting as a failed write.
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:  # pragma: no cover - not every filesystem allows this
        pass
    return True


def write_text_atomic(path: Path, text: str, *, mode: Optional[int] = None) -> bool:
    """UTF-8 :func:`write_bytes_atomic`."""
    return write_bytes_atomic(path, text.encode("utf-8"), mode=mode)


def write_json_atomic(path: Path, payload: Any, *, mode: Optional[int] = None) -> bool:
    """Serialize *payload* and write it atomically.

    Serialization happens before the file is touched, so a payload that cannot
    be encoded leaves the previous state alone rather than truncating it.
    """
    try:
        text = json.dumps(payload, indent=2)
    except (TypeError, ValueError):
        logger.exception("could not serialize state for %s", path)
        return False
    return write_text_atomic(path, text, mode=mode)
