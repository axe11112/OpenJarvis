# JARVIS Architecture

**Status:** Phases 1–9 implemented. See [`JARVIS_ROADMAP.md`](JARVIS_ROADMAP.md) for exactly
what exists, what is partial, and what is not built.

**Not yet run against real infrastructure.** Integrations are tested against recorded HTTP fixtures
and the browser probes against a local fixture site with real Chromium. No Vercel, Supabase or
GitHub credentials have been used, and no live Claude Code session has driven the repair loop,
because no target application is configured (§12).

**Scope:** How an autonomous website-reliability engineer ("JARVIS") is built *on top of*
OpenJarvis, reusing the existing primitives rather than forking or replacing them.

Companion documents:

- [`JARVIS_ROADMAP.md`](JARVIS_ROADMAP.md) — phased implementation plan
- [`JARVIS_SECURITY.md`](JARVIS_SECURITY.md) — permissions, secrets, injection defenses, deployment controls

---

## 1. Current OpenJarvis architecture

OpenJarvis is a local-first personal-AI framework organized around five primitives —
**Intelligence**, **Engine**, **Agentic Logic**, **Memory**, and **Learning** — connected by a
thread-safe `EventBus` and discovered through decorator-based registries. The canonical
description lives in [`architecture/overview.md`](architecture/overview.md); this section records only
what matters for JARVIS.

### 1.1 Composition root

`JarvisSystem` (`src/openjarvis/system/core.py`) is assembled by `SystemBuilder`
(`src/openjarvis/system/builder.py`) from a `JarvisConfig`. The builder resolves the engine, model,
memory backend, tools, channel, sandbox, scheduler, workflow engine and sessions, and groups them
into bundles (`SecurityContext`, `Observability`, `AgentRuntime`, `Scheduling` — `system/bundles.py`).
Anything JARVIS needs at runtime should be reachable from a `JarvisSystem`, not constructed ad hoc.

### 1.2 Registries

`core/registry.py` provides `RegistryBase[T]` with isolated per-subclass storage. Live registries:
`ModelRegistry`, `EngineRegistry`, `MemoryRegistry`, `AgentRegistry`, `ToolRegistry`,
`RouterPolicyRegistry`, `BenchmarkRegistry`, `ChannelRegistry`, `ConnectorRegistry`.
New components become discoverable purely by decoration — no factory edits.

### 1.3 EventBus

`core/events.py` defines `EventType` (a `str` Enum) and a synchronous pub/sub `EventBus`.
Note the established convention for subsystem-specific events: `scheduler/scheduler.py` declares
plain module-level **string** constants (`SCHEDULER_TASK_START = "scheduler_task_start"`) with the
comment *"avoids editing core EventType enum"*. JARVIS follows that convention.

### 1.4 Configuration

`core/config.py` is a single ~2 300-line module of nested `@dataclass(slots=True)` sections rolled up
into `JarvisConfig`. `load_config()` detects hardware, builds defaults, then overlays
`~/.openjarvis/config.toml` by walking a hard-coded `top_sections` tuple through the recursive
`_apply_toml_section()`. `validate_config_key()` walks the dataclass tree so `jarvis config set`
can validate dotted keys. **Adding a config section means: define the dataclass, add the field to
`JarvisConfig`, and add the section name to `top_sections`.** Nothing else.

### 1.5 Agents

`AgentRegistry`-registered classes implementing `BaseAgent` / `ToolUsingAgent` (`agents/_stubs.py`).
Relevant members:

| Agent | Role for JARVIS |
|---|---|
| `ClaudeCodeAgent` (`agents/claude_code.py`) | Wraps the Claude Agent SDK through a Node subprocess. **This is JARVIS's coding engine.** See §7 for its current defects. |
| `OperativeAgent` (`agents/operative.py`) | Persistent scheduled agent: loads session, recalls state JSON from memory, runs a tool loop, persists state. |
| `MonitorOperativeAgent` (`agents/monitor_operative.py`) | Long-horizon agent with configurable memory/compression/retrieval/decomposition strategies. |
| `AgentManager` (`agents/manager.py`) | SQLite lifecycle store: `managed_agents`, `agent_tasks`, `agent_checkpoints`, `agent_messages`, learning log, token/cost budgets. |
| `AgentExecutor` (`agents/executor.py`) | Runs a single agent tick with retry classification (`agents/errors.py`: `AgentTickError`, `EscalateError`, `FatalError`, `retry_delay`). |
| `LoopGuard` (`agents/loop_guard.py`) | Blocks degenerate tool loops (identical-call hashing, ping-pong detection, poll budgets). |

### 1.6 Operators

`operators/` is OpenJarvis's existing pattern for **declaratively defined autonomous agents on a
schedule**. An `OperatorManifest` (`operators/types.py`) is loaded from TOML
(`operators/data/*.toml` — e.g. `system_monitor.toml`) and carries: id, tools, system prompt,
`schedule_type`/`schedule_value`, `metrics`, and `required_capabilities`. `OperatorManager`
(`operators/manager.py`) does discover / activate / pause / resume / `run_once` / `collect_metrics`,
delegating scheduling to `TaskScheduler`. This is the closest existing analogue to a JARVIS monitor
and the model for JARVIS probe manifests.

### 1.7 Scheduling

`scheduler/scheduler.py` — `TaskScheduler` polls a `SchedulerStore` (SQLite, with run logs) on a
background daemon thread and executes due tasks (`cron` | `interval` | `once`) against a
`JarvisSystem`. `scheduler/tools.py` exposes schedule management as agent tools.
Cron support requires the `scheduler` extra (`croniter`).

### 1.8 Tools

`ToolRegistry`-registered `BaseTool` subclasses (`tools/_stubs.py`) with a `ToolSpec` carrying
`required_capabilities`, `requires_confirmation`, `timeout_seconds`, `cost_estimate`.
`ToolExecutor` dispatches with EventBus instrumentation. Relevant existing tools:

- `tools/browser.py` — Playwright tools (`browser_navigate`, plus click/fill/screenshot/etc.) built on a
  **module-level shared `_BrowserSession`** using the *sync* API and a single lazily-created page.
- `tools/browser_axtree.py` — accessibility-tree extraction.
- `tools/git_tool.py` — `git_status` / `git_diff` / `git_commit` / `git_log` via subprocess, output capped at 50 KB.
- `tools/http_request.py` — httpx with SSRF checks, 1 MB body cap, 5-redirect cap.
- `tools/shell_exec.py`, `tools/file_read.py`, `tools/file_write.py`, `tools/apply_patch.py`.
- `tools/approval_store.py` — SQLite pending-action approvals with tiers (`trivial`/`low`/`medium`/`high`)
  and remembered decisions.

### 1.9 Skills

`skills/` implements the [agentskills.io](https://agentskills.io/specification) standard: parser, loader,
index, dependency resolution, `security.py`, and `tool_adapter.py` that exposes every skill as a tool.
Skills are how *procedural knowledge* is packaged; JARVIS uses them for reusable diagnosis playbooks,
not for infrastructure access.

### 1.10 MCP

`mcp/` has both a client (`client.py`, `transport.py`, `loader.py`) and a server (`server.py`).
External MCP servers are discovered by `SystemBuilder._discover_external_mcp()` and adapted into tools
via `tools/mcp_adapter.py`. This is the supported extension point for third-party integrations.

### 1.11 Memory, sessions, traces, telemetry

`memory/` (service, store, extractor) over pluggable backends; `sessions/` (session + compression);
`traces/` (`TraceStore` SQLite, `TraceCollector`, `TraceAnalyzer`); `telemetry/` (metrics store and
aggregator). JARVIS writes to traces/telemetry rather than inventing a parallel record.

### 1.12 Security

`security/` is unusually complete and is the backbone of the JARVIS safety story:

| Module | What it gives JARVIS |
|---|---|
| `injection_scanner.py` | Regex prompt-injection detection with `ThreatLevel` scoring |
| `credential_stripper.py` | Redacts API keys/tokens from text; `wrap_tool_output()` fences tool output in `<tool_result>` tags |
| `scanner.py` | `SecretScanner`, `PIIScanner` |
| `boundary.py` | `BoundaryGuard` — scans content at device exit points in `redact`/`warn`/`block` mode |
| `audit.py` | `AuditLogger` — append-only SQLite with a Merkle hash chain, EventBus-subscribed |
| `capabilities.py` | RBAC: `Capability` enum, `CapabilityGrant`, `AgentPolicy`, `CapabilityPolicy` (supports deny-by-default) |
| `taint.py` | Information-flow labels (`PII`, `SECRET`, `USER_PRIVATE`, `EXTERNAL`) with a per-tool `SINK_POLICY` |
| `ssrf.py` | Private-IP / cloud-metadata blocking |
| `rate_limiter.py` | Token-bucket throttling |
| `severity_policy.py` | `ThreatLevel` → `block`/`warn`/`sanitize`/`log` |
| `subprocess_sandbox.py`, `file_policy.py`, `signing.py` | Process/file/artifact controls |

`SecurityConfig` also ships named profiles (`personal`, `shared`, `server`) via `apply_security_profile()`.

### 1.13 Channels

35 `ChannelRegistry` adapters. `channels/telegram.py` (`TelegramChannel`) is a native Bot API adapter
with long polling, `allowed_chat_ids` gating, and EventBus publication. `TelegramChannelConfig`
already exists in `core/config.py`, and `TELEGRAM_BOT_TOKEN` is already in the
`TOOL_CREDENTIALS` map in `core/credentials.py`. **Telegram notification is a wiring job, not a build job.**

### 1.14 Workflow

`workflow/` is a DAG engine (`WorkflowGraph`, `WorkflowEngine`) with `agent`/`tool`/`condition`/
`parallel`/`loop`/`transform` node types, topological execution, `ThreadPoolExecutor` parallelism and
`LoopGuard` integration.

### 1.15 CLI, server, dashboard

`cli/` is a Click group; commands are registered in `cli/__init__.py` with `cli.add_command(...)`.
`server/` is a FastAPI app with routers for agents, analytics, approvals, connectors, digests,
webhooks, plus SSE/WebSocket bridges. `frontend/` is a React + Vite + Tailwind/shadcn SPA, and
`desktop/` wraps it in Tauri. A JARVIS dashboard is a new page and a new router, not a new app.

### 1.16 Testing and deployment

582 test files under `tests/`, mirroring the package layout, run with
`pytest -n auto -m "not live and not cloud and not hub"`. Custom markers are declared in
`pyproject.toml`. `respx` is the established HTTP-mocking library. CI (`.github/workflows/ci.yml`)
runs ruff check + ruff format + the marked pytest lane, and builds the Rust extension with
`maturin develop` first. Deployment units already exist for systemd, launchd, Docker and Windows
(`deploy/`).

---

## 2. Proposed JARVIS architecture

### 2.1 Design stance

JARVIS is **a new subsystem, not a new framework**. It contributes:

1. Signal acquisition that OpenJarvis has no equivalent for (real browser workflow probes, Vercel,
   Supabase, GitHub CI).
2. An incident model and state machine.
3. A repair loop that treats `ClaudeCodeAgent` as an untrusted-but-capable contractor and verifies
   its work independently.

Everything else — scheduling, config, events, registries, audit, capabilities, injection scanning,
secrets, channels, tracing, CLI, server, dashboard — is existing OpenJarvis machinery.

### 2.2 Naming decision

The package will be **`src/openjarvis/reliability/`**, not `src/openjarvis/jarvis/`
(`openjarvis.jarvis` is self-referential and collides conceptually with `JarvisSystem`,
`JarvisConfig` and the `jarvis` CLI binary). The **product-facing name remains JARVIS**:
the CLI group is `jarvis reliability`, notifications are signed "JARVIS", and the dashboard is titled
JARVIS. Config section: `[reliability]`.

### 2.3 Module layout

```
src/openjarvis/reliability/
    __init__.py
    types.py          Incident, IncidentState, Severity, Evidence, RepairAttempt,
                      VerificationResult, Signal, ProbeResult
    store.py          IncidentStore — SQLite, append-only transition log
    fingerprint.py    Stable incident fingerprints (dedup / flake suppression)

    probes/
        _stubs.py     BaseProbe ABC, ProbeRegistry
        spec.py       ProbeSpec + TOML loader (mirrors operators/loader.py)
        http.py       HttpProbe — cheap status/latency check
        browser.py    BrowserProbe — Playwright workflow runner with evidence capture

    sources/
        _stubs.py     BaseSignalSource ABC (poll() -> list[Signal])
        vercel.py     Vercel REST (read-only)
        supabase.py   Supabase Management API + PostgREST/Auth health (read-only)
        github.py     GitHub REST (commits, PRs, Actions)

    correlate.py      Failure -> candidate deployment / commit / changed files
    briefing.py       Incident -> sanitized Claude Code task text
    repair.py         RepairLoop — branch, invoke Claude Code, test, verify, retry, PR
    verify.py         Independent verification against a preview deployment
    policy.py         SafetyPolicy — what may be auto-fixed / auto-deployed
    notify.py         Notifier ABC + TelegramNotifier + message templates
    monitor.py        ReliabilityMonitor — registers probes/sources with TaskScheduler

configs/reliability/probes/*.toml     Declarative workflow probes (shipped examples)
~/.openjarvis/reliability/probes/     User probe specs
~/.openjarvis/reliability/incidents.db
~/.openjarvis/reliability/evidence/<incident-id>/   screenshots, traces, HAR
```

### 2.4 Probe specs: why TOML, not `.spec.ts`

The brief sketches `tests/website/login.spec.ts`. We recommend **declarative TOML manifests executed
by the Python Playwright sync API**, because:

- It matches the existing `operators/data/*.toml` manifest pattern the brief asks us to follow.
- It keeps probes in-process, so evidence (console errors, failed requests, screenshots, traces) and
  incident creation happen in one place with no cross-language result parsing.
- It avoids a second runtime and a second dependency tree (`@playwright/test`, Node) alongside the
  Python `playwright` extra the repo already declares.
- Probe steps are then *data*, which makes them safe to store, diff, version and render in the dashboard.

Escape hatch: a `runner = "playwright-test"` field on a `ProbeSpec` may shell out to
`npx playwright test <file>` for workflows too complex to express declaratively, parsing the JSON
reporter output. This keeps the door open without making Node mandatory.

Sketch of a probe spec:

```toml
[probe]
id = "auth-login"
name = "Login → dashboard"
component = "authentication"
severity = "critical"
schedule = { type = "interval", value = "600" }

[probe.retry]
attempts = 2
confirm_runs = 2          # N-of-M confirmation before an incident opens
backoff_seconds = 30

[probe.credentials]
email_env = "JARVIS_TEST_USER_EMAIL"       # env var NAMES, never values
password_env = "JARVIS_TEST_USER_PASSWORD"

[[probe.steps]]
action = "goto"
url = "/login"

[[probe.steps]]
action = "fill"
selector = "input[name=email]"
value_from = "email_env"

[[probe.steps]]
action = "click"
selector = "button[type=submit]"

[[probe.expect]]
kind = "url"
matches = "/dashboard"

[[probe.expect]]
kind = "visible"
selector = "[data-testid=dashboard-root]"

[probe.assertions]
no_console_errors = true
no_failed_requests = true
max_http_status = 399
```

### 2.5 Incident model

```
Incident
  id                INC-00042              (monotonic, human-quotable)
  fingerprint       stable hash of (component, probe_id, failure_kind, normalized_error)
  created_at / updated_at
  severity          CRITICAL | HIGH | MEDIUM | LOW
  environment       production | preview | staging
  source            probe | vercel | supabase | github
  component         free-form, e.g. "authentication"
  state             see state machine below
  title / summary
  evidence[]        typed, each with a trust label (trusted | external)
  repro_steps[]     derived from the ProbeSpec that failed
  correlation       { deployment_id, commit_sha, pr_number, changed_files[] }
  attempts[]        RepairAttempt: n, branch, claude_task_hash, diff_stat,
                    test_result, verification_result, outcome
  transitions[]     append-only (from, to, at, actor, reason)
  resolution        root_cause, fix_summary, pr_url, deployed_at
```

State machine (exactly the states in the brief):

```
DETECTED ─► INVESTIGATING ─► REPRODUCING ─► FIXING ─► TESTING ─► VERIFYING ─► RESOLVED
                                  │            ▲                     │
                                  │            └── retry (< max) ────┤
                                  │                                  ▼
                                  └──────────────────────────────► FAILED
                                                                     │
                                              HUMAN_REQUIRED ◄───────┘
                                                     │
                                               ROLLED_BACK
```

Transitions are validated (illegal transitions raise), written to the incident's transition log,
and mirrored to `AuditLogger` so the Merkle chain covers the whole autonomous decision trail.

### 2.6 Severity → action policy

| Severity | Examples | Notify | Auto-investigate | Auto-repair | Auto-deploy |
|---|---|---|---|---|---|
| `CRITICAL` | site down, login/signup broken, data-integrity or security issue | immediate | yes | only if `policy.allow_critical_repair` **and** the fix class is allowlisted | **never** by default |
| `HIGH` | major feature broken, dashboard down, key API failing | immediate | yes | yes | no (PR only) |
| `MEDIUM` | isolated feature failure, recurring frontend error | batched | yes | yes | no (PR only) |
| `LOW` | minor UI issue, console warning, perf nit | digest | no | no (issue only) | no |

Severity is a property of the *probe spec* (declared) refined by *observed impact* (e.g. an HTTP 500
on `/` escalates). The mapping is data in `[reliability.policy]`, not code.

### 2.7 Data flow

```
                ┌──────────────── TaskScheduler (existing) ────────────────┐
                │                                                          │
       ┌────────▼─────────┐   ┌──────────────┐   ┌──────────┐   ┌────────┐ │
       │ BrowserProbe     │   │ VercelSource │   │ Supabase │   │ GitHub │ │
       │ HttpProbe        │   │              │   │  Source  │   │ Source │ │
       └────────┬─────────┘   └──────┬───────┘   └────┬─────┘   └───┬────┘ │
                │  ProbeResult        │  Signal        │ Signal      │      │
                └─────────┬───────────┴────────────────┴─────────────┘      │
                          ▼                                                 │
                   flake suppression (N-of-M) + fingerprint dedup           │
                          ▼                                                 │
                   IncidentStore  ──►  DETECTED  ──►  Notifier (Telegram)   │
                          ▼                                                 │
                   correlate.py: deployment ↔ commit ↔ PR ↔ changed files   │
                          ▼                                                 │
                   briefing.py: redact secrets, fence external text,        │
                                scan for injection, build Claude task       │
                          ▼                                                 │
                   policy.py gate  ──► (blocked) ──► HUMAN_REQUIRED         │
                          ▼                                                 │
                   repair.py: isolated branch ──► ClaudeCodeAgent           │
                          ▼                                                 │
                   test runner (repo's own suite)                           │
                          ▼                                                 │
                   preview deployment (Vercel)                              │
                          ▼                                                 │
                   verify.py: RE-RUN the original failing ProbeSpec         │
                          ▼                                                 │
                 PASS ──► PR (default) or deploy (only if policy allows)    │
                 FAIL ──► attempt n+1 with new evidence, up to max_attempts │
                          └────────► HUMAN_REQUIRED ──► Notifier            │
                                                                            │
       every transition ──► AuditLogger (Merkle) + EventBus + TraceStore ───┘
```

The single most important property: **the arrow from Claude Code to "resolved" does not exist.**
Verification is performed by re-running the *same probe spec that detected the failure*, against a
freshly built preview deployment, by code Claude did not write during the repair. Claude's own claim
of success is recorded as an assertion, never as evidence.

### 2.8 Reuse map

| Need | Reused as-is | Extended | New |
|---|---|---|---|
| Scheduling | `TaskScheduler`, `SchedulerStore` | — | thin probe registration in `monitor.py` |
| Declarative agents | `OperatorManifest` pattern | — | `ProbeSpec` (same shape, different schema) |
| Config | `load_config`, `_apply_toml_section`, `validate_config_key` | add `ReliabilityConfig` + one entry in `top_sections` | — |
| Events | `EventBus` | module-level event-name constants (scheduler precedent) | — |
| Coding engine | `ClaudeCodeAgent` | **must be repaired first — see §7** | `briefing.py` context builder |
| Repair safety | `LoopGuard`, `agents/errors.py` retry classification | — | `RepairLoop` attempt cap |
| Secrets | `core/credentials.py`, `cli/vault_cmd.py`, `CredentialStripper`, `SecretScanner` | add JARVIS env-var names to `TOOL_CREDENTIALS` | — |
| Injection defense | `InjectionScanner`, `taint.py`, `wrap_tool_output()` | — | `<untrusted_external_data>` fencing in `briefing.py` |
| Audit | `AuditLogger`'s Merkle-chain *approach* | — | self-chained transition log (see §2.11) |
| Permissions | `CapabilityPolicy`, `Capability` | new capability labels for infra reads/writes | — |
| Network safety | `ssrf.py`, `rate_limiter.py` | — | per-source circuit breaker |
| Browser | `playwright` extra | — | `BrowserProbe` (see §2.9 — the existing tools are unsuitable) |
| Git/GitHub | `git_tool.py`, `github_notifications` connector | — | `sources/github.py` (commits/PRs/Actions) |
| Notifications | `TelegramChannel`, `TelegramChannelConfig` | — | `Notifier` abstraction + templates |
| Dashboard | FastAPI `server/`, React `frontend/` | new router + page | — |
| CLI | Click group registration | one `cli.add_command(...)` line | `cli/reliability_cmd.py` |
| Tests | pytest layout, `respx`, marker convention | one new `browser` marker | `tests/reliability/` |
| Deployment | `deploy/systemd`, `deploy/launchd`, Docker | — | — |

### 2.9 Why `tools/browser.py` is reused but not sufficient

The existing browser tools are built for *interactive agent* use, not for reliability probing:

- A single module-level `_BrowserSession` with one shared page — concurrent probes would collide.
- No `page.on("console")` / `page.on("requestfailed")` / `page.on("response")` listeners, so JavaScript
  errors, failed requests and HTTP error codes are invisible.
- No tracing (`context.tracing.start/stop`), no HAR, no screenshot-on-failure.
- No isolated `BrowserContext` per run, so cookies/auth state leak between probes.
- No per-probe timeout or navigation-redirect assertions.

`BrowserProbe` therefore creates its own `BrowserContext` per run with listeners attached, and the
existing tools remain untouched for agent use. The `playwright` dependency and the `BrowserConfig`
section (`headless`, `timeout_ms`, viewport) are shared.

### 2.10 Why the transition log is self-chained rather than mirrored to `AuditLogger`

The original plan (roadmap J1.9) was to mirror every incident transition into
`security/audit.py`'s `AuditLogger`. Implementation showed that to be the wrong shape:

- `AuditLogger.log()` takes a `SecurityEvent`, whose fields are `findings: List[ScanFinding]`,
  `content_preview` and `action_taken` — a scan-result record, not a state-change record.
- Its `SecurityEventType` enum has exactly four members (`secret_detected`, `pii_detected`,
  `sensitive_file_blocked`, `tool_blocked`). Recording incident transitions would mean adding
  reliability concepts to a deliberately narrow security taxonomy, in a module JARVIS is otherwise
  careful not to modify.

So `incident_transitions` carries its own `row_hash`/`prev_hash` chain using the same construction
`AuditLogger` uses, verified by `IncidentStore.verify_chain()` and surfaced as
`jarvis reliability verify-audit`. The guarantee is identical — an edited or deleted transition is
detectable — without widening a security enum. Deleting an incident deliberately does **not** delete
its transition history: the audit trail outlives the record it describes.

`AuditLogger` is still the right home for *security* events JARVIS generates (capability denials,
secret-scanner findings on outbound briefings); those go through it unchanged in Phase 6.

### 2.11 Configuration sketch

```toml
[reliability]
enabled = false                      # opt-in; default off

[reliability.site]
base_url = "https://example.com"
environment = "production"

[reliability.probes]
directory = ""                       # defaults to ~/.openjarvis/reliability/probes
default_interval_seconds = 300
confirm_runs = 2                     # N-of-M before opening an incident

[reliability.vercel]
enabled = false
team_id = ""
project_id = ""
token_env = "VERCEL_READONLY_TOKEN"  # NAME of the env var, never the value
poll_interval_seconds = 120

[reliability.supabase]
enabled = false
project_ref = ""
token_env = "SUPABASE_READONLY_TOKEN"
allow_production_writes = false      # hard gate; see JARVIS_SECURITY.md

[reliability.github]
enabled = false
repo = "owner/name"
token_env = "GITHUB_READONLY_TOKEN"
base_branch = "main"
branch_prefix = "jarvis/incident-"

[reliability.repair]
enabled = false
max_attempts = 3
workspace = ""                       # checkout JARVIS may modify
test_command = ""                    # repo's own test command
require_preview_verification = true

[reliability.policy]
deploy_mode = "pr_only"              # pr_only | auto_deploy_allowlisted | never
allow_push_to_default_branch = false
auto_repair_severities = ["HIGH", "MEDIUM"]
auto_deploy_fix_classes = []         # empty = nothing deploys itself

[reliability.notify]
channel = "telegram"
persona = true                       # JARVIS voice on user-facing messages
```

`token_env` fields hold **variable names**, so no credential ever enters `config.toml`, the incident
DB, a log line, or a Claude prompt.

---

## 3. Components we will reuse

Unchanged, imported as-is: `core/registry.py`, `core/events.py`, `core/paths.py`, `core/credentials.py`,
`scheduler/*`, `agents/loop_guard.py`, `agents/errors.py`, `security/*` (all of it),
`channels/telegram.py`, `tools/http_request.py`, `tools/git_tool.py`, `traces/*`, `telemetry/*`,
`sessions/*`, `workflow/*`, `deploy/*`.

---

## 4. Components we will extend

Additive-only edits, each a single well-scoped change:

1. `core/config.py` — add `ReliabilityConfig` (+ nested sections) and one `top_sections` entry.
2. `core/credentials.py` — add JARVIS env-var names to `TOOL_CREDENTIALS` for `jarvis connect`/doctor.
3. `security/capabilities.py` — add labels: `infra:read`, `infra:deploy`, `db:read`, `db:write`,
   `repo:read`, `repo:write`.
4. `cli/__init__.py` — one `cli.add_command(reliability, "reliability")` line.
5. `server/app.py` — mount a `reliability_routes` router (Phase 9 only).
6. `pyproject.toml` — a `browser` marker for pytest; no new runtime dependency beyond the existing
   `browser` extra.
7. `mkdocs.yml` — nav entries for the three JARVIS documents.
8. `agents/claude_code.py` + `claude_code_runner/` — **repair, see §7.**

---

## 5. Components we must NOT modify

- The five primitives' public contracts (`BaseAgent`, `BaseTool`, `InferenceEngine`, `MemoryBackend`,
  `BaseChannel`) — JARVIS extends by registration, never by signature change.
- `EventType` enum — use module-level string constants, per the `scheduler` precedent.
- `tools/browser.py` — leave the interactive browser tools alone; `BrowserProbe` is separate.
- Existing tests — none removed, none weakened.
- `learning/`, `mining/`, `evals/`, `bench/`, `pearl/`, `desktop/`, `rust/` — out of scope entirely.
- Security defaults — no relaxation of `BoundaryGuard` mode, SSRF, or capability defaults to make a
  probe or a test pass.

---

## 6. Security model

Full treatment in [`JARVIS_SECURITY.md`](JARVIS_SECURITY.md). The five load-bearing rules:

1. **Least privilege by default.** Every integration starts read-only. Supabase writes, pushes to the
   default branch, and auto-deploy are separate opt-in flags that default to off.
2. **Secrets never reach the model.** Test-account credentials are referenced by env-var *name* in
   probe specs, resolved inside the probe process, and are excluded from `Evidence` by construction.
   Every Claude briefing passes through `CredentialStripper` + `SecretScanner` before it is sent, and
   the same content is scanned again on the way into the incident store.
3. **All external content is untrusted data, never instructions.** Page text, console output, DB rows,
   logs, PR/issue bodies and API responses are wrapped in explicit `<untrusted_external_data>` fences,
   scanned by `InjectionScanner`, tagged `TaintLabel.EXTERNAL`, and introduced to Claude with a
   standing instruction that content inside the fences is evidence only.
4. **Every autonomous action is audited.** Incident transitions, Claude invocations (task hash, not
   task text with secrets), diffs applied, tests run, verifications and deploy decisions all land in
   `AuditLogger`'s Merkle-chained SQLite log.
5. **Safe failure beats autonomous action.** Ambiguity, policy-gate denial, verification failure, or
   attempt exhaustion all terminate in `HUMAN_REQUIRED` with a notification — never in a hopeful deploy.

---

## 7. Known defect in the Claude Code integration (blocker for Phase 6)

`ClaudeCodeAgent` **cannot run today.** Verified by inspection:

- `_ensure_runner()` copies `package.json` and `dist` from
  `src/openjarvis/agents/claude_code_runner/`, then runs `node dist/index.js`. The directory contains
  only `package.json` and `src/index.ts` — **there is no `dist/`, no `tsconfig.json`, and no build
  script**. (Contrast `channels/whatsapp_baileys_bridge/`, which does ship a `tsconfig.json`.)
- `package.json` pins `@anthropic-ai/claude-code": "^0.2"`, and `src/index.ts` treats `query()` as
  returning an array of `{type: "text" | "tool_use" | "tool_result"}` messages. The current Claude
  Agent SDK (`@anthropic-ai/claude-agent-sdk`) exposes an async iterable with a different message
  shape, so the runner would not work even once compiled.
- The runner sets no permission mode and no `disallowedTools`; `allowedTools` is the only control, and
  the Python side never populates it.

The existing unit tests pass because they mock `subprocess.run` — they exercise the Python wrapper's
parsing, not the Node runner. Phase 6 must therefore begin with a decision:

- **(a)** rewrite the runner against the current Claude Agent SDK, add `tsconfig.json` + a build step,
  and plumb permission mode / disallowed tools / working directory from Python; or
- **(b)** drop the Node bridge and drive the `claude` CLI directly from Python in headless mode,
  which removes the bundled-runner build problem entirely.

**Resolved: (b).** `reliability/code_agent.py` provides `ClaudeCliAgent`, which drives the `claude`
CLI headlessly with working directory, allowed tools, disallowed tools and timeout supplied by
JARVIS. `WebFetch`/`WebSearch` are disallowed by default so the agent cannot be talked into
fetching an attacker-controlled URL by something it read in the evidence. The upstream
`ClaudeCodeAgent` is left untouched — fixing it is a separate concern for the OpenJarvis project.

Note that this path is **untested against a live Claude Code session**: the repair loop is exercised
end to end with `FakeCodeAgent`, which is what makes the "claims success but verification fails"
case testable in the first place.

---

## 8. Failure handling

| Failure | Response |
|---|---|
| Probe flake | N-of-M confirmation (`confirm_runs`) before an incident opens; single failures logged only |
| Site slow / timeout | Per-probe timeout; timeouts are a distinct `failure_kind`, not a generic error |
| Integration API 5xx / rate limit | Exponential backoff with jitter, `Retry-After` honoured, per-source circuit breaker; breaker-open marks that source `degraded` rather than failing the tick |
| Duplicate incidents | Fingerprint dedup; a repeat within the cooldown window appends an occurrence instead of opening a new incident |
| Claude produces no diff | Attempt counted, evidence "no changes produced" fed back; two consecutive no-op attempts → `HUMAN_REQUIRED` |
| Claude's fix breaks tests | Test output becomes evidence for the next attempt |
| Verification fails | New evidence (screenshot, console, network, diff of expected vs actual) returned to Claude; attempt n+1 |
| Attempts exhausted (`max_attempts`, default 3) | Stop modifying code. `HUMAN_REQUIRED`, branch preserved, full evidence bundle attached, owner notified |
| Deploy regresses production | `ROLLED_BACK` — revert via the deployment provider's promote-previous mechanism, then a new incident linked to the first |
| JARVIS itself crashes | Scheduler tick isolation: one probe's exception never stops the loop; incidents are persisted before any long-running work begins, so a restart resumes mid-flight state |
| Config missing/invalid | `enabled = false` is the default; a misconfigured integration is reported by `jarvis reliability doctor` and disabled, not guessed at |

---

## 9. Testing strategy

1. **Unit, no network, default lane.** State machine legality, severity mapping, fingerprint stability,
   flake suppression, policy gating, briefing redaction (assert known secrets never survive), injection
   fencing, backoff/circuit-breaker maths, TOML probe-spec parsing and validation.
2. **HTTP integration with `respx`.** Vercel/Supabase/GitHub source clients against recorded fixtures,
   including 401/403/429/5xx and pagination. Matches the repo's existing mocking convention.
3. **Browser probes against a local fixture server.** A `tests/reliability/fixtures/` static site with
   deliberately broken variants (JS error, failed XHR, wrong redirect, 500) served by
   `http.server` on a random port. Marked with a new `browser` marker and excluded from the default CI
   lane exactly like `live`/`cloud`/`hub`, so contributors without browsers installed are unaffected.
4. **Repair loop with a fake coding agent.** A `FakeCodeAgent` implementing the same interface as
   `ClaudeCodeAgent` (following `tests/agents/fake_engine.py` precedent) lets the full
   detect → repair → verify → retry → escalate cycle be tested deterministically, including the
   "Claude claims success but verification fails" path, which is the single most important test in the
   suite.
5. **No existing test is modified or removed.** New tests live in `tests/reliability/` mirroring the
   package layout.
6. **Baseline discipline.** Every phase ends with the full marked suite green
   (see §11 for the recorded baseline).

---

## 10. Deployment strategy

JARVIS adds **no new process and no new service**. It runs inside the OpenJarvis daemon that already
has systemd/launchd/Docker units under `deploy/`:

- `jarvis start` (daemon) → `TaskScheduler` → probe and source ticks.
- `jarvis reliability watch` runs the same loop in the foreground for development.
- The repair workspace is a normal git checkout on the same host; branches are pushed to the
  configured remote and PRs opened through the GitHub source client.
- Evidence artifacts live under `~/.openjarvis/reliability/evidence/` with a retention setting;
  screenshots and traces are never uploaded anywhere by default.
- The dashboard is served by the existing FastAPI app; no separate web server.

---

## 11. Cost model

Per the zero-additional-cost requirement, and stated honestly rather than assumed:

| Component | Cost |
|---|---|
| Playwright + Chromium | Free (≈400 MB one-time browser download) |
| Telegram Bot API | Free |
| GitHub REST API | Free within standard rate limits |
| Vercel REST API | Free to call. **Constraint:** log/observability retention on lower plans is short (hours), so JARVIS must snapshot deployment and error data into evidence at detection time rather than assume it can fetch it later. |
| Supabase Management API | Free to call. **Constraint:** log retention on the Free plan is roughly one day; same snapshot-at-detection rule applies. |
| Vercel preview deployments | Included on Hobby; this is what makes independent verification free. |
| **Claude Code** | **Not free.** Either an Anthropic API key (metered per token) or a Claude subscription with the `claude` CLI. This is the one unavoidable paid dependency and the reason §7 recommends driving the CLI the owner already pays for rather than adding a second metered key. |

No other paid service is introduced. Where a capability genuinely requires a paid tier (e.g. long log
retention, Vercel observability APIs), the roadmap documents the limitation instead of adding the
dependency.

---

## 12. Open decisions for the owner

1. **Target application** — production URL, Vercel team/project, Supabase project ref, and the GitHub
   repository JARVIS maintains. None are configured; the repo currently in scope is `axe11112/openjarvis`.
2. **Claude Code invocation path** — §7(a) rewrite the Node runner, or §7(b) drive the `claude` CLI.
3. **Probe format** — declarative TOML (recommended) vs native `@playwright/test` specs.
4. **Upstream vs fork** — whether these changes are intended to be contributed back to OpenJarvis
   (which constrains the diff to additive, well-tested changes) or maintained as a private fork.
5. **Test-account provisioning** — a dedicated non-privileged account per environment, with a naming
   convention that makes its rows obvious in production data.
