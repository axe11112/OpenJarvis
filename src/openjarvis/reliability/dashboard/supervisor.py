"""launchd supervision for ``jarvis reliability watch``.

JARVIS should survive a reboot, a crash and a closed terminal without anybody
watching it. On macOS the right supervisor for that is already installed:
``launchd``. This module writes a LaunchAgent for one specific service, and
gives the Control Center a *very* narrow way to ask launchd about it and to
start it again.

Three constraints shape everything here.

**The dashboard must never be able to run a command.** Every invocation in this
module is a fixed ``argv`` list, built from constants plus one label that is
validated against :data:`SERVICE_LABEL`. There is no shell, no string
interpolation of caller input, and no way to express an action that is not
``print``, ``kickstart``, ``bootstrap`` or ``bootout`` of that one service. A
dashboard endpoint that could pass an argument through to ``launchctl`` would
be a remote shell wearing a monitoring badge.

**A deliberate stop stays a stop.** The emergency stop is the one control an
operator reaches for when something is badly wrong, and a watchdog that quietly
undoes it is worse than no watchdog. It is honoured twice: the supervisor
refuses to issue a start while the flag exists, and the wrapper script re-checks
the flag at launch and exits ``0`` so that launchd's ``KeepAlive`` does not
treat the refusal as a crash worth retrying.

**Restart loops are bounded.** launchd's own ``ThrottleInterval`` paces crash
restarts; :class:`RestartBudget` paces the dashboard-initiated ones, so a
failing watcher cannot be hammered by a browser tab left open on a refresh
timer.

Credentials are never written into the plist. A LaunchAgent lives in a
predictable, readable location, so the plist carries a ``PATH`` and nothing
else; secrets go in a ``0600`` environment file that the wrapper sources.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "LaunchdSupervisor",
    "RestartBudget",
    "SERVICE_LABEL",
    "WatcherState",
    "WatcherStatus",
    "bound_log_file",
    "render_plist",
    "render_wrapper",
]

#: The one service this module will ever act on. Not a parameter: making it one
#: would turn a lifecycle button into a way to control arbitrary launchd jobs.
SERVICE_LABEL = "ai.openjarvis.reliability.watch"

#: Exit status ``jarvis reliability watch`` uses for "the emergency stop is
#: engaged, I refuse to run". Translated to 0 by the wrapper so launchd does not
#: read a deliberate refusal as a crash.
STOPPED_EXIT_CODE = 3

#: Seconds launchd waits before respawning a crashed watcher.
THROTTLE_INTERVAL_SECONDS = 30

#: Logs are truncated in place once they pass this, keeping the tail. Truncation
#: rather than rotation because launchd holds an append-mode descriptor on the
#: file: renaming it would leave every later line going to an orphaned inode
#: that nothing ever reads.
MAX_LOG_BYTES = 5 * 1024 * 1024
KEEP_LOG_BYTES = 1 * 1024 * 1024

#: How often the wrapper's background bounder re-checks the log sizes.
LOG_BOUND_INTERVAL_SECONDS = 600

#: How long the bounder sleeps between liveness checks on its parent. Bounds how
#: long an orphan can survive a ``kill -9`` of the wrapper, which is the one case
#: where the wrapper's EXIT trap never runs.
LOG_BOUND_TICK_SECONDS = 15


class WatcherStatus(str, Enum):
    """What the watcher process is doing, as far as launchd will say.

    ``STOPPED_BY_OPERATOR`` is deliberately not a flavour of ``OFFLINE``. They
    look identical from the process table and mean opposite things: one is a
    fault to recover from, the other is an instruction to leave alone.
    """

    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    STARTING = "STARTING"
    STOPPED_BY_OPERATOR = "STOPPED_BY_OPERATOR"
    ERROR = "ERROR"


@dataclass(slots=True)
class WatcherState:
    """A reading of the watcher service."""

    status: WatcherStatus = WatcherStatus.OFFLINE
    label: str = SERVICE_LABEL
    pid: int = 0
    detail: str = ""
    last_exit_code: Optional[int] = None
    service_installed: bool = False
    supervisor_supported: bool = False
    plist_path: str = ""
    stdout_log: str = ""
    stderr_log: str = ""
    may_start: bool = False
    start_blocked_reason: str = ""
    checked_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize. Contains no secrets — paths, states and a PID only."""
        return {
            "status": self.status.value,
            "label": self.label,
            "pid": self.pid,
            "detail": self.detail,
            "last_exit_code": self.last_exit_code,
            "service_installed": self.service_installed,
            "supervisor_supported": self.supervisor_supported,
            "plist_path": self.plist_path,
            "stdout_log": self.stdout_log,
            "stderr_log": self.stderr_log,
            "may_start": self.may_start,
            "start_blocked_reason": self.start_blocked_reason,
            "checked_at": self.checked_at,
        }


# ---------------------------------------------------------------------------
# Restart budget
# ---------------------------------------------------------------------------


@dataclass
class RestartBudget:
    """Rate limit for dashboard-initiated starts.

    launchd already paces *crash* restarts. This paces *our* requests, because
    the dashboard recovers automatically when it sees an offline watcher, and a
    watcher that dies immediately on every start would otherwise be restarted
    once per poll for as long as a browser tab stays open.
    """

    max_starts: int = 3
    window_seconds: float = 600.0
    min_interval_seconds: float = 20.0
    clock: Callable[[], float] = time.monotonic
    _starts: List[float] = field(default_factory=list, repr=False)

    def may_start(self) -> Tuple[bool, str]:
        """Whether another start may be requested now, and why not."""
        now = self.clock()
        self._prune(now)
        if self._starts and now - self._starts[-1] < self.min_interval_seconds:
            wait = self.min_interval_seconds - (now - self._starts[-1])
            return False, f"a start was just requested; waiting {wait:.0f}s"
        if len(self._starts) >= self.max_starts:
            return (
                False,
                f"{self.max_starts} starts already requested in the last "
                f"{self.window_seconds / 60:.0f} minutes; the watcher is not "
                "staying up and needs a human",
            )
        return True, ""

    def record(self) -> None:
        """Note that a start was requested."""
        now = self.clock()
        self._prune(now)
        self._starts.append(now)

    def reset(self) -> None:
        """Forget the history — used once the watcher is confirmed online."""
        self._starts.clear()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        self._starts = [t for t in self._starts if t > cutoff]


# ---------------------------------------------------------------------------
# Log bounding
# ---------------------------------------------------------------------------


def bound_log_file(
    path: Path,
    *,
    max_bytes: int = MAX_LOG_BYTES,
    keep_bytes: int = KEEP_LOG_BYTES,
) -> bool:
    """Truncate *path* in place when it grows past *max_bytes*, keeping the tail.

    In place, rather than renaming: launchd opens the log once, in append mode,
    and keeps that descriptor for the life of the job. A rename leaves the
    running watcher writing into a file nothing will ever look at again, which
    is a worse outcome than a large log.

    Returns ``True`` when the file was truncated.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= max_bytes:
        return False
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, size - keep_bytes))
            tail = handle.read()
        # Drop the first partial line so the file still starts on a boundary.
        newline = tail.find(b"\n")
        if 0 <= newline < len(tail) - 1:
            tail = tail[newline + 1 :]
        with path.open("r+b") as handle:
            handle.write(
                b"[jarvis] earlier output was truncated to bound this log\n" + tail
            )
            handle.truncate()
    except OSError:
        logger.exception("could not bound the log file %s", path)
        return False
    return True


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_wrapper(
    *,
    working_directory: Path,
    stop_flag: Path,
    env_file: Path,
    stdout_log: Path,
    stderr_log: Path,
    command: Sequence[str] = (
        "uv",
        "run",
        "--no-sync",
        "jarvis",
        "reliability",
        "watch",
    ),
) -> str:
    """The shell wrapper launchd actually runs.

    It exists for three jobs launchd cannot do itself: honour the emergency
    stop, load credentials from a file the plist must not contain, and keep the
    logs bounded while the watcher runs.
    """
    quoted = " ".join(f'"{part}"' for part in command)
    return f"""#!/bin/bash
# Generated by `jarvis reliability service install`. Edit the environment file
# rather than this script; a reinstall overwrites it.
set -uo pipefail

STOP_FLAG="{stop_flag}"
ENV_FILE="{env_file}"
STDOUT_LOG="{stdout_log}"
STDERR_LOG="{stderr_log}"
MAX_LOG_BYTES={MAX_LOG_BYTES}
KEEP_LOG_BYTES={KEEP_LOG_BYTES}
BOUND_TICK_SECONDS={LOG_BOUND_TICK_SECONDS}
BOUND_TICKS={LOG_BOUND_INTERVAL_SECONDS // LOG_BOUND_TICK_SECONDS}

# A deliberate stop stays a stop. Exit 0 — with KeepAlive/SuccessfulExit=false
# a non-zero exit would have launchd retry this every ThrottleInterval, quietly
# fighting the operator who engaged it.
if [ -e "$STOP_FLAG" ]; then
  echo "[jarvis-watch] emergency stop engaged ($STOP_FLAG); not starting." >&2
  exit 0
fi

bound_log() {{
  local file="$1"
  [ -f "$file" ] || return 0
  local size
  size=$(wc -c < "$file" 2>/dev/null | tr -d ' ') || return 0
  if [ "${{size:-0}}" -gt "$MAX_LOG_BYTES" ]; then
    # Rewrite through the same inode: launchd holds an append-mode descriptor
    # on it, so a rename would orphan every later line.
    tail -c "$KEEP_LOG_BYTES" "$file" > "$file.trim" 2>/dev/null \\
      && cat "$file.trim" > "$file" \\
      && rm -f "$file.trim"
  fi
}}

bound_log "$STDOUT_LOG"
bound_log "$STDERR_LOG"

# Keep bounding while the watcher runs; a supervised process can stay up for
# weeks, and bounding only at startup would not bound anything.
#
# Three details that look fussy and are not.
#
# Its output goes to /dev/null, so the bounder does not hold this job's stdout
# and stderr open — otherwise anything reading them waits on a background sleep
# rather than on the watcher.
#
# `set -m` puts it in its own process group, so the trap takes the sleep down
# with the subshell rather than orphaning it.
#
# And it re-checks that this script is still alive on every tick, because the
# trap does *not* run when the wrapper is SIGKILLed — which is exactly what
# happens when something kills a wedged watcher. Without this, every hard kill
# leaves a sleeping bash behind forever, and a crash-restart loop leaks one per
# crash.
PARENT=$$
set -m
(
  while :; do
    for _ in $(seq 1 "$BOUND_TICKS"); do
      sleep "$BOUND_TICK_SECONDS"
      kill -0 "$PARENT" 2>/dev/null || exit 0
    done
    bound_log "$STDOUT_LOG"
    bound_log "$STDERR_LOG"
  done
) >/dev/null 2>&1 &
BOUNDER=$!
set +m
trap 'kill -TERM -"$BOUNDER" 2>/dev/null || kill -TERM "$BOUNDER" 2>/dev/null' EXIT

# Rust/Cargo and any credentials, from a 0600 file the plist does not contain.
if [ -r "$HOME/.cargo/env" ]; then
  # shellcheck disable=SC1091
  . "$HOME/.cargo/env"
fi
if [ -r "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

cd "{working_directory}" || exit 1
{quoted}
status=$?

# {STOPPED_EXIT_CODE} is "the emergency stop is engaged" — a refusal, not a crash.
if [ "$status" -eq {STOPPED_EXIT_CODE} ]; then
  echo "[jarvis-watch] watcher refused to start: emergency stop engaged." >&2
  exit 0
fi
exit "$status"
"""


def render_plist(
    *,
    label: str,
    wrapper: Path,
    working_directory: Path,
    stdout_log: Path,
    stderr_log: Path,
    path_env: str,
) -> str:
    """The LaunchAgent property list.

    ``KeepAlive`` with ``SuccessfulExit = false`` is the restart policy: respawn
    when the watcher dies unexpectedly, leave it alone when it exits cleanly.
    Together with the wrapper's exit-code translation that is what makes a
    crash recoverable and an emergency stop final.

    Contains no credential. ``PATH`` is the only environment variable set here;
    everything sensitive is sourced by the wrapper from a ``0600`` file.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_xml_escape(label)}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{_xml_escape(str(wrapper))}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{_xml_escape(str(working_directory))}</string>

    <!-- Start at login, and again after a reboot. -->
    <key>RunAtLoad</key>
    <true/>

    <!-- Restart on an unexpected exit only. A clean exit (which is what the
         wrapper reports when the emergency stop is engaged) is left alone. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <!-- Backoff between respawns, so a watcher that cannot start does not
         spin. -->
    <key>ThrottleInterval</key>
    <integer>{THROTTLE_INTERVAL_SECONDS}</integer>

    <key>ProcessType</key>
    <string>Background</string>

    <key>StandardOutPath</key>
    <string>{_xml_escape(str(stdout_log))}</string>
    <key>StandardErrorPath</key>
    <string>{_xml_escape(str(stderr_log))}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{_xml_escape(path_env)}</string>
    </dict>
</dict>
</plist>
"""


#: Directories prepended to the agent's PATH. launchd starts jobs with a
#: minimal PATH, so uv, cargo and Homebrew all have to be named explicitly or
#: the wrapper fails with "uv: command not found" and nothing says why.
_PATH_CANDIDATES = (
    "{home}/.cargo/bin",
    "{home}/.local/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def default_path_env(home: Optional[Path] = None) -> str:
    """A PATH that can find uv, cargo and Homebrew from a launchd job."""
    base = str(home or Path.home())
    parts: List[str] = []
    for template in _PATH_CANDIDATES:
        entry = template.format(home=base)
        if entry not in parts:
            parts.append(entry)
    # Anything already on the installing shell's PATH, appended rather than
    # prepended so the explicit entries win.
    for entry in os.environ.get("PATH", "").split(":"):
        if entry and entry not in parts:
            parts.append(entry)
    return ":".join(parts)


# ---------------------------------------------------------------------------
# The supervisor
# ---------------------------------------------------------------------------


#: Every launchctl subcommand this module may issue. Anything not listed here
#: cannot be reached, whatever a caller passes.
_ALLOWED_SUBCOMMANDS = frozenset({"print", "kickstart", "bootstrap", "bootout"})

_PID_RE = re.compile(r"^\s*pid\s*=\s*(\d+)", re.MULTILINE)
_STATE_RE = re.compile(r"^\s*state\s*=\s*(.+)$", re.MULTILINE)
_EXIT_RE = re.compile(r"^\s*last exit (?:code|status)\s*=\s*(-?\d+)", re.MULTILINE)


class LaunchdSupervisor:
    """Ask launchd about the watcher, and ask it to start the watcher.

    The surface is intentionally tiny: :meth:`status`, :meth:`start` and
    :meth:`restart`. There is no ``stop``, because stopping JARVIS is what the
    emergency stop is for and it should go through the audited path rather than
    through a browser button.
    """

    def __init__(
        self,
        config: Any,
        *,
        label: str = SERVICE_LABEL,
        budget: Optional[RestartBudget] = None,
        runner: Optional[
            Callable[[Sequence[str]], "subprocess.CompletedProcess"]
        ] = None,
        home: Optional[Path] = None,
        jarvis_dir: Optional[Path] = None,
        uid: Optional[int] = None,
        platform_name: str = "",
    ) -> None:
        if label != SERVICE_LABEL:
            raise ValueError(
                f"refusing to supervise {label!r}: this supervisor only ever "
                f"acts on {SERVICE_LABEL}"
            )
        self._config = config
        self._label = label
        self._budget = budget or RestartBudget()
        self._runner = runner or self._run
        self._home = home or Path.home()
        self._jarvis_dir_override = jarvis_dir
        self._uid = os.getuid() if uid is None else uid
        self._platform = platform_name or platform.system()

    # -- locations --------------------------------------------------------

    @property
    def label(self) -> str:
        """The supervised service's launchd label."""
        return self._label

    @property
    def budget(self) -> RestartBudget:
        """The dashboard-initiated restart budget."""
        return self._budget

    def plist_path(self) -> Path:
        """Where the LaunchAgent lives."""
        return self._home / "Library" / "LaunchAgents" / f"{self._label}.plist"

    def wrapper_path(self) -> Path:
        """Where the generated wrapper script lives."""
        return self._jarvis_dir() / "reliability" / "watch-supervised.sh"

    def env_file_path(self) -> Path:
        """The ``0600`` file holding the watcher's credential environment."""
        return self._jarvis_dir() / "reliability" / "watch.env"

    def log_dir(self) -> Path:
        """Where the supervised watcher's output is written."""
        return self._jarvis_dir() / "logs"

    def stdout_log(self) -> Path:
        """Standard output of the supervised watcher."""
        return self.log_dir() / "watch.stdout.log"

    def stderr_log(self) -> Path:
        """Standard error of the supervised watcher."""
        return self.log_dir() / "watch.stderr.log"

    def _jarvis_dir(self) -> Path:
        if self._jarvis_dir_override is not None:
            return self._jarvis_dir_override
        from openjarvis.core.paths import get_config_dir

        return get_config_dir()

    def stop_flag(self) -> Path:
        """The emergency stop flag, resolved the same way the watcher does."""
        from openjarvis.reliability.watch import stop_flag_path

        return stop_flag_path(self._config)

    def stop_engaged(self) -> bool:
        """Whether an operator has engaged the emergency stop."""
        try:
            return self.stop_flag().exists()
        except OSError:  # pragma: no cover - defensive
            return False

    # -- capability -------------------------------------------------------

    def supported(self) -> bool:
        """Whether this machine can be supervised by launchd at all."""
        return self._platform == "Darwin" and shutil.which("launchctl") is not None

    def installed(self) -> bool:
        """Whether the LaunchAgent file is present."""
        return self.plist_path().is_file()

    # -- launchctl --------------------------------------------------------

    def _service_target(self) -> str:
        return f"gui/{self._uid}/{self._label}"

    def _domain_target(self) -> str:
        return f"gui/{self._uid}"

    @staticmethod
    def _run(argv: Sequence[str]) -> "subprocess.CompletedProcess":
        """Execute *argv* with no shell and no inherited stdin."""
        return subprocess.run(  # noqa: S603 - fixed argv, never shell
            list(argv),
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            check=False,
        )

    def _launchctl(self, subcommand: str, *args: str) -> "subprocess.CompletedProcess":
        """Run one allowlisted ``launchctl`` subcommand.

        The allowlist is the security boundary: the dashboard reaches this
        method through named actions only, and even a caller that got a string
        through cannot name a subcommand that is not one of four.
        """
        if subcommand not in _ALLOWED_SUBCOMMANDS:
            raise ValueError(f"refusing to run `launchctl {subcommand}`")
        argv = ["launchctl", subcommand, *args]
        logger.debug("running %s", " ".join(argv))
        return self._runner(argv)

    # -- status -----------------------------------------------------------

    def status(self) -> WatcherState:
        """Read the watcher's current state.

        The emergency stop is checked first and wins over everything launchd
        would say: a stopped watcher whose stop was deliberate must never be
        reported as a fault, or the recovery path will keep trying to undo it.
        """
        from openjarvis.reliability.types import now_iso

        state = WatcherState(
            label=self._label,
            supervisor_supported=self.supported(),
            service_installed=self.installed(),
            plist_path=str(self.plist_path()),
            stdout_log=str(self.stdout_log()),
            stderr_log=str(self.stderr_log()),
            checked_at=now_iso(),
        )

        if self.stop_engaged():
            state.status = WatcherStatus.STOPPED_BY_OPERATOR
            state.detail = (
                f"an emergency stop is engaged ({self.stop_flag()}); "
                "nothing will start it until the flag is removed"
            )
            state.start_blocked_reason = state.detail
            return state

        if not state.supervisor_supported:
            state.status = WatcherStatus.OFFLINE
            state.detail = (
                "launchd supervision is only available on macOS; run "
                "`jarvis reliability watch` yourself on this platform"
            )
            state.start_blocked_reason = state.detail
            return state

        if not state.service_installed:
            state.status = WatcherStatus.OFFLINE
            state.detail = (
                "the LaunchAgent is not installed; run "
                "`jarvis reliability service install`"
            )
            state.start_blocked_reason = state.detail
            return state

        try:
            completed = self._launchctl("print", self._service_target())
        except Exception as exc:  # noqa: BLE001 - a broken launchctl is a state
            state.status = WatcherStatus.ERROR
            state.detail = f"could not query launchd: {type(exc).__name__}: {exc}"
            return state

        if completed.returncode != 0:
            # The plist exists but the job is not loaded into the domain — the
            # usual cause is a reboot without login, or a manual `bootout`.
            state.status = WatcherStatus.OFFLINE
            state.detail = "the service is installed but not loaded into launchd"
            state.may_start, state.start_blocked_reason = self._budget.may_start()
            return state

        text = completed.stdout or ""
        pid_match = _PID_RE.search(text)
        state_match = _STATE_RE.search(text)
        exit_match = _EXIT_RE.search(text)
        launchd_state = (state_match.group(1).strip() if state_match else "").lower()
        if exit_match:
            state.last_exit_code = int(exit_match.group(1))

        if pid_match and "running" in launchd_state:
            state.pid = int(pid_match.group(1))
            state.status = WatcherStatus.ONLINE
            state.detail = f"supervised by launchd as pid {state.pid}"
            self._budget.reset()
            return state

        if "spawn scheduled" in launchd_state or "waiting" in launchd_state:
            state.status = WatcherStatus.STARTING
            state.detail = f"launchd reports '{launchd_state}'"
            return state

        if state.last_exit_code not in (None, 0):
            state.status = WatcherStatus.ERROR
            state.detail = (
                f"the watcher exited with status {state.last_exit_code}; "
                f"see {self.stderr_log()}"
            )
        else:
            state.status = WatcherStatus.OFFLINE
            state.detail = f"launchd reports '{launchd_state or 'not running'}'"
        state.may_start, state.start_blocked_reason = self._budget.may_start()
        return state

    # -- lifecycle --------------------------------------------------------

    def start(self) -> Tuple[bool, str]:
        """Ask launchd to start the watcher. Returns ``(ok, message)``."""
        return self._kickstart(restart=False)

    def restart(self) -> Tuple[bool, str]:
        """Ask launchd to stop and start the watcher."""
        return self._kickstart(restart=True)

    def _kickstart(self, *, restart: bool) -> Tuple[bool, str]:
        action = "restart" if restart else "start"

        if self.stop_engaged():
            # The one refusal that matters most. An operator engaged this on
            # purpose; a button in a browser does not get to overrule it.
            return False, (
                "refused: an emergency stop is engaged. Remove "
                f"{self.stop_flag()} to allow JARVIS to run again."
            )
        if not self.supported():
            return False, "refused: launchd supervision needs macOS"
        if not self.installed():
            return False, (
                "refused: the LaunchAgent is not installed. Run "
                "`jarvis reliability service install` first."
            )

        allowed, reason = self._budget.may_start()
        if not allowed:
            return False, f"refused: {reason}"

        args = ["-k", self._service_target()] if restart else [self._service_target()]
        try:
            completed = self._launchctl("kickstart", *args)
        except Exception as exc:  # noqa: BLE001
            return False, f"could not reach launchd: {type(exc).__name__}: {exc}"

        self._budget.record()
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:300]
            return False, f"launchd refused the {action}: {detail or 'unknown error'}"
        return True, f"launchd was asked to {action} {self._label}"

    # -- installation -----------------------------------------------------

    def install(
        self,
        *,
        working_directory: Path,
        capture_env: bool = True,
        command: Sequence[str] = (
            "uv",
            "run",
            "--no-sync",
            "jarvis",
            "reliability",
            "watch",
        ),
        load: bool = True,
    ) -> Dict[str, Any]:
        """Write the wrapper, the environment file and the LaunchAgent.

        Returns a report naming every file written and every environment
        variable captured — **by name**. No value is returned, logged or
        printed anywhere in this path.
        """
        if not self.supported():
            raise RuntimeError("launchd supervision needs macOS with launchctl")

        self.log_dir().mkdir(parents=True, exist_ok=True)
        self.wrapper_path().parent.mkdir(parents=True, exist_ok=True)
        self.plist_path().parent.mkdir(parents=True, exist_ok=True)

        captured = self._write_env_file(capture=capture_env)

        wrapper = render_wrapper(
            working_directory=working_directory,
            stop_flag=self.stop_flag(),
            env_file=self.env_file_path(),
            stdout_log=self.stdout_log(),
            stderr_log=self.stderr_log(),
            command=command,
        )
        self.wrapper_path().write_text(wrapper, encoding="utf-8")
        self.wrapper_path().chmod(0o700)

        plist = render_plist(
            label=self._label,
            wrapper=self.wrapper_path(),
            working_directory=working_directory,
            stdout_log=self.stdout_log(),
            stderr_log=self.stderr_log(),
            path_env=default_path_env(self._home),
        )
        self.plist_path().write_text(plist, encoding="utf-8")
        self.plist_path().chmod(0o644)

        loaded = False
        message = "not loaded (--no-load)"
        if load:
            # bootout first so a reinstall replaces the running definition
            # instead of failing with "service already loaded".
            self._launchctl("bootout", self._service_target())
            completed = self._launchctl(
                "bootstrap", self._domain_target(), str(self.plist_path())
            )
            loaded = completed.returncode == 0
            message = (
                "loaded into launchd"
                if loaded
                else (completed.stderr or completed.stdout or "").strip()[:300]
            )

        return {
            "plist": str(self.plist_path()),
            "wrapper": str(self.wrapper_path()),
            "env_file": str(self.env_file_path()),
            "stdout_log": str(self.stdout_log()),
            "stderr_log": str(self.stderr_log()),
            "working_directory": str(working_directory),
            "captured_env_names": captured,
            "loaded": loaded,
            "message": message,
        }

    def uninstall(self) -> Dict[str, Any]:
        """Unload the service and remove the LaunchAgent.

        The environment file is left alone: it is the operator's, it holds
        credentials, and deleting it as a side effect of unloading a service
        would be a surprise with a painful recovery.
        """
        unloaded = False
        if self.supported():
            completed = self._launchctl("bootout", self._service_target())
            unloaded = completed.returncode == 0
        removed = False
        if self.plist_path().is_file():
            self.plist_path().unlink()
            removed = True
        return {
            "unloaded": unloaded,
            "plist_removed": removed,
            "env_file_kept": str(self.env_file_path()),
        }

    # -- environment ------------------------------------------------------

    def required_env_names(self) -> List[str]:
        """Environment variables the watcher needs, by name.

        Read out of the configuration's ``*_token_env`` fields rather than
        hard-coded, so renaming a variable in ``config.toml`` cannot leave the
        supervised watcher looking for the old one.
        """
        rc = self._config.reliability
        names = [
            rc.github.token_env,
            getattr(rc.github, "actions_token_env", ""),
            rc.vercel.token_env,
            rc.supabase.token_env,
        ]
        if rc.notify.enabled:
            names.append("TELEGRAM_BOT_TOKEN")
        names += [
            "JARVIS_TEST_EMAIL",
            "JARVIS_TEST_USER_EMAIL",
            "JARVIS_TEST_PASSWORD",
            "JARVIS_TEST_USER_PASSWORD",
            "ANTHROPIC_API_KEY",
        ]
        seen: List[str] = []
        for name in names:
            if name and name not in seen:
                seen.append(name)
        return seen

    def _write_env_file(self, *, capture: bool) -> List[str]:
        """Create or update the ``0600`` environment file.

        Returns the names captured. An existing file is never overwritten —
        an operator who has curated it should not lose that to a reinstall —
        but missing names are appended so a newly configured integration does
        not silently go without its token.
        """
        path = self.env_file_path()
        existing_names: List[str] = []
        existing_text = ""
        if path.is_file():
            existing_text = path.read_text(encoding="utf-8")
            for line in existing_text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    existing_names.append(stripped.split("=", 1)[0].strip())

        added: List[str] = []
        lines: List[str] = []
        if not existing_text:
            lines.append(
                "# JARVIS watcher environment. Mode 0600 — this file holds\n"
                "# credential VALUES and is sourced by the launchd wrapper.\n"
                "# The LaunchAgent plist deliberately contains none of them.\n"
            )
        for name in self.required_env_names():
            if name in existing_names:
                continue
            value = os.environ.get(name, "") if capture else ""
            if capture and value:
                lines.append(f"{name}={value}")
                added.append(name)
            else:
                lines.append(f"# {name}=")

        text = existing_text
        if lines:
            if text and not text.endswith("\n"):
                text += "\n"
            text += "\n".join(lines) + "\n"
        path.write_text(text, encoding="utf-8")
        # Written before anything else could read it; 0600 is the whole reason
        # credentials live here rather than in the plist.
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return added

    def bound_logs(self) -> None:
        """Keep the supervised logs inside their size cap."""
        for path in (self.stdout_log(), self.stderr_log()):
            bound_log_file(path)

    def tail_log(self, *, stream: str = "stderr", lines: int = 40) -> str:
        """Return the last *lines* of one supervised log, redacted.

        ``stream`` is validated against a two-name allowlist rather than being
        turned into a path, so this cannot be aimed at an arbitrary file.
        """
        from openjarvis.reliability.dashboard.model import redact

        if stream not in ("stdout", "stderr"):
            raise ValueError(f"unknown log stream {stream!r}")
        path = self.stdout_log() if stream == "stdout" else self.stderr_log()
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 64 * 1024))
                text = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        return redact("\n".join(text.splitlines()[-lines:]))
