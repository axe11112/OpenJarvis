# JARVIS Control Center

A local, read-only web view of the running reliability system, plus a launchd
watchdog that keeps the watcher alive without a terminal window.

```bash
jarvis reliability dashboard
```

```
JARVIS Control Center
http://127.0.0.1:8765
```

---

## 1. What it is, and what it deliberately is not

The Control Center is a **visualization layer**. It owns no monitoring logic and
no state of its own. Every value on the screen is read back out of the existing
system:

| On screen | Read from |
|---|---|
| Overall status, surface cards | `LiveDiagnostic`, run with `open_incidents=False` |
| Incidents, evidence, transitions, audit chain | `IncidentStore` — the same database `jarvis reliability watch` writes |
| Probe rows | the probe specs on disk, plus what the watcher left in the evidence directory |
| Target identity | `resolve_target()` |
| Safety interlocks | `config.reliability`, field by field |
| Watcher process state | `launchctl print` for one named service |

It is safe to run beside `jarvis reliability watch`. It never writes to the
incident database, never starts a repair, and never touches the emergency stop.
There is no repair button, no deploy button, no merge button, and no way to
change an incident's state — closing an incident is an audited action and stays
on the command line.

The **only** two actions it can take are asking launchd to start or restart the
watcher service, covered in §4.

## 2. Honesty rules carried over from the reliability core

A dashboard is where "we did not check" most easily turns into green. Two rules
from `openjarvis.reliability.health` are carried through unchanged:

- **Not-checked is never green.** A check that could not reach a verdict keeps
  its `UNKNOWN` / `BLOCKED` / `NOT_CONFIGURED` state and is counted as a *blind
  spot*, not a pass. Before the first refresh cycle completes, the overall
  status reads `UNVERIFIED` rather than `HEALTHY`.
- **Only an observed failure is a failure.** A missing token makes JARVIS blind;
  it does not make the target broken. That leaves the system `DEGRADED`, not
  `FAILED`.

Probe rows use four states:

| State | Means |
|---|---|
| `FAIL` | An incident attributed to this probe is open |
| `PASS` | JARVIS ran it and no incident is open for it |
| `KNOWN_NOISE` | It passed, but only after a noise profile filtered something out — reported when a suppression was actually counted, never inferred from the spec |
| `NOT_VERIFIED` | Disabled, still a placeholder, or JARVIS has no record of running it |

### Probe verification modes

`--probe-verification` decides how much the dashboard runs *itself*:

| Mode | Behaviour |
|---|---|
| `none` | Runs nothing. Rows are inferred from incidents and evidence on disk. |
| `http` (default) | Runs the `http` probes. They are single requests, and they leave no artefact on disk — without this they would read `NOT_VERIFIED` forever. |
| `all` | Also drives the browser probes, alongside the watcher's. Opt in deliberately. |

No mode opens an incident: the executor is built without a store, so anything
the dashboard observes is displayed and discarded. The watcher remains the only
writer.

## 3. Security

For this first version the dashboard is local-only and read-only:

- **Bind address.** Only a loopback address may be bound; anything else is
  refused at construction with a clear message.
- **Peer address.** Every request's source address is re-checked.
- **`Host` header.** Rejected unless it names a loopback host. This is what
  stops a public DNS name pointed at `127.0.0.1` from reading the dashboard
  through your own browser.
- **Control token.** The two lifecycle endpoints require a header carrying a
  token minted at startup and embedded in the page. A cross-site request can be
  *sent* to a loopback port but cannot *read* the page, so it cannot forge one.
- **Method.** Everything is `GET` except two named `POST`s. `PUT`, `DELETE` and
  `PATCH` return 405.
- **Static assets** are an allowlist of three filenames, so there is no path to
  traverse.
- **CSP** is `default-src 'none'` with no external origin reachable.
- **Redaction.** Every piece of free text — incident titles, evidence, log tails
  — goes through `BoundaryGuard(mode="redact")`, the same boundary the
  notification router uses. Artifact and worktree filesystem paths are dropped.
  Credentials appear only as environment-variable *names*.

Run `jarvis reliability dashboard --no-watcher-control` to remove the two
lifecycle endpoints entirely; they then return 404 rather than being merely
hidden.

## 4. The launchd watchdog (macOS)

```bash
jarvis reliability service install --working-directory ~/OpenJarvis
jarvis reliability service status
```

This installs a LaunchAgent that starts `uv run --no-sync jarvis reliability
watch` at login and after a reboot, and restarts it if it exits unexpectedly.

**Files it writes**

| Path | What |
|---|---|
| `~/Library/LaunchAgents/ai.openjarvis.reliability.watch.plist` | the LaunchAgent. Contains a `PATH` and no credential. |
| `~/.openjarvis/reliability/watch-supervised.sh` | the wrapper launchd runs |
| `~/.openjarvis/reliability/watch.env` | mode `0600`; credential values live here |
| `~/.openjarvis/logs/watch.stdout.log` | standard output |
| `~/.openjarvis/logs/watch.stderr.log` | standard error |

**Credentials.** A LaunchAgent is a readable file in a predictable place, so no
secret is written into it. `install` captures the variables the configuration
names into the `0600` environment file, and reports which names it captured —
never a value. If your tokens are not exported in the installing shell, fill the
file in by hand; every required name is already listed there, commented out.

**Restart policy.** `KeepAlive` is `{ SuccessfulExit: false }` — respawn on an
unexpected exit, leave a clean one alone — with a 30-second `ThrottleInterval`
as backoff.

**The emergency stop is honoured three times over.** `jarvis reliability stop`
writes a `STOPPED` flag, and:

1. The supervisor refuses to issue a start while the flag exists.
2. The HTTP endpoint returns 409 with the reason.
3. The wrapper re-checks the flag at launch and exits `0`, so `KeepAlive` does
   not read a deliberate refusal as a crash worth retrying. The watcher's own
   exit status `3` ("stopped") is translated to `0` for the same reason.

A deliberate operator stop stays a stop. Note that the flag blocks *new* starts
and *new* repairs; a watcher already running when you engage it keeps running
until it is stopped — see `jarvis reliability stop`.

**Logs are bounded.** Once a log passes 5 MB it is truncated in place, keeping
the last 1 MB. In place rather than renamed: launchd holds an append-mode
descriptor on the file, and a rename would leave every later line going to an
orphaned inode. The wrapper re-checks every 10 minutes while the watcher runs,
and the dashboard re-checks on each refresh cycle.

**Restart loops are bounded twice.** launchd's `ThrottleInterval` paces crash
restarts. A `RestartBudget` paces dashboard-initiated ones — at most three in
ten minutes, and never two within twenty seconds — so a watcher that will not
stay up cannot be hammered by a browser tab left open on a refresh timer. When
the budget is exhausted the dashboard says so plainly: the watcher needs a
human.

**Self-recovery.** When the dashboard finds the watcher unexpectedly offline it
asks launchd to start it, then reports `STARTING` until launchd confirms. Turn
this off with `--no-auto-recover`. launchd remains the primary watchdog; this is
a secondary path for the case where launchd's job was booted out of the domain.

### Commands

```bash
jarvis reliability service install [--working-directory DIR] [--no-capture-env] [--no-load]
jarvis reliability service status [--json]
jarvis reliability service start
jarvis reliability service restart
jarvis reliability service logs [--stream stdout|stderr] [--lines N]
jarvis reliability service uninstall     # leaves watch.env alone; it holds credentials
```

`launchctl` is only ever reached through a four-verb allowlist (`print`,
`kickstart`, `bootstrap`, `bootout`) applied to one hard-coded service label.
There is no shell, no interpolation of caller input, and no way to name a
different job — a lifecycle button must not be a remote shell wearing a
monitoring badge.

### Watcher states

| State | Means |
|---|---|
| `ONLINE` | launchd reports it running, with a PID |
| `STARTING` | launchd has scheduled a spawn |
| `OFFLINE` | not running, and it exited cleanly or was never loaded |
| `ERROR` | not running after a non-zero exit — see the stderr log |
| `STOPPED_BY_OPERATOR` | an emergency stop is engaged. Not a fault, and nothing will try to recover it. |

`OFFLINE` and `STOPPED_BY_OPERATOR` are deliberately separate: they look
identical from the process table and mean opposite things.

## 5. On other platforms

launchd supervision is macOS-only. `service install` refuses elsewhere and says
so. The dashboard itself is portable; see `deploy/` for systemd units.

## 6. API

Every route is `GET` unless marked otherwise.

| Route | Returns |
|---|---|
| `/` | the page |
| `/api/snapshot` | the whole read model |
| `/api/incidents/<id>` | one incident: evidence, history, transitions, repair attempts, audit |
| `/api/watcher` | watcher process state |
| `/api/watcher/logs?stream=stdout\|stderr` | redacted log tail |
| `POST /api/watcher/start` | ask launchd to start the service |
| `POST /api/watcher/restart` | ask launchd to restart the service |

The two `POST`s require the `X-JARVIS-Control` header and return 409 with an
explanation when an emergency stop is engaged.
