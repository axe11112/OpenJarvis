# JARVIS Roadmap

**Status:** Phases 1–10 implemented. Completed tasks are marked ✅; anything unmarked or
explicitly deferred is called out as such.

**Partially exercised against real infrastructure.** As of Phase 10, **GitHub is the only
integration that has been driven against a live system** — `GitHubSource` was pointed at the real
private target repository with a read-only token and returned real commits, branches and pull
requests. Vercel, Supabase, the production website and Telegram have **not** been reached: the
development sandbox's egress proxy blocks those hosts, and no read-only tokens were supplied. Those
integrations remain fixture-tested only (`respx`), and the browser probes remain tested against a
local fixture site with real Chromium. See §"Phase 10" below and
[`JARVIS_LIVE_SETUP.md`](JARVIS_LIVE_SETUP.md) for exactly what is still unverified.

Read [`JARVIS_ARCHITECTURE.md`](JARVIS_ARCHITECTURE.md) first; task IDs below refer to the module
layout in §2.3 of that document.

---

## How to read this

Every task is sized to be a single reviewable change: one module (or one focused edit to an existing
module) plus its tests, ending with the full marked suite green. Tasks are labelled:

- **`[new]`** — new file under `src/openjarvis/reliability/`
- **`[extend]`** — additive edit to an existing OpenJarvis module
- **`[test]`** — test-only change
- **`[docs]`** — documentation
- **`[decision]`** — needs an answer from the owner before the dependent tasks can start

**Definition of done for every code task:** new unit tests pass; `uv run ruff check src/ tests/` and
`uv run ruff format --check src/ tests/` clean; the full lane
`uv run pytest tests/ -n auto -m "not live and not cloud and not hub"` matches or beats the recorded
baseline; no existing test modified or deleted.

---

## Phase 1 — Foundation

**Goal:** understand the ground, then lay the incident and configuration substrate that every later
phase writes to.

| ID | Task | Type | Status |
|---|---|---|---|
| J1.1 | Repository analysis and architecture map → `docs/JARVIS_ARCHITECTURE.md` | `[docs]` | ✅ |
| J1.2 | Phased implementation plan → `docs/JARVIS_ROADMAP.md` | `[docs]` | ✅ |
| J1.3 | Security model → `docs/JARVIS_SECURITY.md` | `[docs]` | ✅ |
| J1.4 | Record the test baseline (see §"Baseline" below) | `[test]` | ✅ |
| J1.5 | `reliability/types.py`: `Severity`, `IncidentState`, `Incident`, `Evidence`, `RepairAttempt`, `VerificationResult`, `Signal`, `ProbeResult`, `Correlation`, `Resolution` — dataclasses with `to_dict`/`from_dict` | `[new]` | ✅ |
| J1.6 | State-machine validation: `LEGAL_TRANSITIONS` table, `Incident.transition_to()` raising `InvalidTransitionError`, append-only transition list | `[new]` | ✅ |
| J1.7 | `reliability/fingerprint.py`: stable fingerprint; normalization strips timestamps, UUIDs, hex blobs, ports, line numbers, query strings, durations | `[new]` | ✅ |
| J1.8 | `reliability/store.py`: `IncidentStore` (SQLite), five tables, `create`/`get`/`list`/`transition`/`add_evidence`/`add_attempt`/`find_by_fingerprint`/`record_occurrence` | `[new]` | ✅ |
| J1.9 | Tamper-evident incident history — **implemented as a self-chained transition log rather than an `AuditLogger` mirror**; see the deviation note below | `[new]` | ✅ |
| J1.10 | `ReliabilityConfig` + 8 nested sections in `core/config.py`; `"reliability"` added to `load_config`'s `top_sections` | `[extend]` | ✅ |
| J1.11 | Config tests: TOML overlay, `validate_config_key`, and assertions that every dangerous default is off | `[test]` | ✅ |
| J1.12 | `reliability/events.py`: event-name constants as module-level strings, **not** new `EventType` members; published by the store | `[new]` | ✅ |
| J1.13 | `cli/reliability_cmd.py` with `status`, `incident list`, `incident show`, `verify-audit`, `doctor`; registered in `cli/__init__.py` | `[new]` + `[extend]` | ✅ |
| J1.14 | `reliability/monitor.py`: `ReliabilityMonitor` registering ticks with `TaskScheduler`, with per-tick exception isolation | `[new]` | deferred to Phase 2 — there is nothing to schedule until probes exist |

**Exit criteria — met.** An incident can be created, transitioned through every legal state,
persisted, listed and inspected from the CLI, and its history verified — with no network, no browser
and no model involved. 146 new tests; the full suite matches baseline.

**Deviation from the plan (J1.9).** `AuditLogger.log()` takes a `SecurityEvent`
(`findings`/`content_preview`/`action_taken`) and its `SecurityEventType` enum has four scan-oriented
members. Mirroring incident transitions through it would mean adding reliability concepts to a
deliberately narrow security taxonomy. Instead `incident_transitions` carries its own
`row_hash`/`prev_hash` chain using the same construction, verified by `IncidentStore.verify_chain()`
and exposed as `jarvis reliability verify-audit`. Same guarantee, no security enum widened. See
`JARVIS_ARCHITECTURE.md` §2.10.

**Also deferred from J1.14:** the monitor skeleton. Writing a scheduler wrapper with no probes to run
would have been speculative structure; it moves to J2.11 where it has a real caller.

---

## Phase 2 — Website monitoring

**Goal:** answer "does the website actually work?", with evidence good enough to hand to an engineer.

| ID | Task | Type | Depends on |
|---|---|---|---|
| J2.1 | `[decision]` Probe format: declarative TOML (recommended) vs native `@playwright/test` specs | `[decision]` | J1.1 |
| J2.2 | `probes/spec.py`: `ProbeSpec` dataclass + TOML loader + schema validation with actionable errors, mirroring `operators/loader.py` | `[new]` | J2.1 |
| J2.3 | `probes/_stubs.py`: `BaseProbe` ABC, `ProbeRegistry`, `ProbeResult` contract | `[new]` | J1.5 |
| J2.4 | `probes/http.py`: `HttpProbe` — status, latency, redirect chain, body assertions; reuses `security/ssrf.py` | `[new]` | J2.3 |
| J2.5 | `probes/browser.py` part 1: isolated `BrowserContext` per run, step interpreter (`goto`/`click`/`fill`/`select`/`wait_for`/`press`/`screenshot`), honouring `BrowserConfig` | `[new]` | J2.2, J2.3 |
| J2.6 | `probes/browser.py` part 2: evidence capture — `console` listener (JS errors), `requestfailed` listener, `response` listener for 4xx/5xx, final URL vs expected (unexpected-redirect detection) | `[new]` | J2.5 |
| J2.7 | `probes/browser.py` part 3: artifacts — screenshot on failure, Playwright trace when `trace_on_failure`, HAR optional; written under `~/.openjarvis/reliability/evidence/<incident-id>/` with a retention setting | `[new]` | J2.6 |
| J2.8 | Credential resolution: `value_from` → env-var lookup inside the probe process; credentials are excluded from `Evidence` by construction and asserted absent in tests | `[new]` | J2.5 |
| J2.9 | Flake suppression: N-of-M confirmation (`confirm_runs`) before an incident opens; single failures recorded as occurrences only | `[new]` | J1.7, J1.8 |
| J2.10 | Severity assignment: declared probe severity, escalated by observed impact (site unreachable, 5xx on a critical path) | `[new]` | J2.4, J2.6 |
| J2.11 | Wire probes into `ReliabilityMonitor` with per-probe schedules and jittered start times so probes don't stampede | `[new]` | J1.12, J2.5 |
| J2.12 | Ship example probe specs under `configs/reliability/probes/`: `homepage.toml`, `login.toml`, `signup.toml`, `dashboard.toml` | `[new]` | J2.2 |
| J2.13 | `[extend]` Add a `browser` pytest marker to `pyproject.toml`, excluded from the default CI lane like `live`/`cloud`/`hub` | `[extend]` | — |
| J2.14 | `[test]` Local fixture site (`http.server`, random port) with broken variants: JS error, failed XHR, wrong redirect, 500, slow response | `[test]` | J2.6, J2.13 |
| J2.15 | `[test]` Probe-spec parsing, step interpretation, evidence extraction, flake suppression, severity escalation — all without a browser where possible | `[test]` | J2.9, J2.10 |
| J2.16 | `jarvis reliability probe run <id>` and `probe list` CLI commands | `[extend]` | J1.13, J2.5 |

**Status: complete.** All tasks ✅ except J2.1, which was decided in favour of declarative TOML.
Verified against real Chromium and the fixture site.

**Exit criteria — met.** A broken login produces an incident with reproduction steps, evidence, a
screenshot and a trace; a healthy site produces nothing and writes no artifacts.

**Design note.** The browser emits a console *error* for any failed subresource, so a missing
favicon would open an incident on essentially every real site. Those messages are filtered out of
the JavaScript-error bucket (they remain visible via `no_failed_requests`/`max_http_status`), and
`ignore_console_patterns` covers app-specific noise.

---

## Phase 3 — GitHub

**Goal:** know which code is likely responsible.

| ID | Task | Type | Depends on |
|---|---|---|---|
| J3.1 | `sources/_stubs.py`: `BaseSignalSource` ABC (`poll() -> list[Signal]`, `health()`) | `[new]` | J1.5 |
| J3.2 | `sources/github.py` reads: repo metadata, branches, commits since ts, commit detail with changed files, PR list/detail | `[new]` | J3.1 |
| J3.3 | GitHub Actions: workflow runs, failed runs, job logs (truncated, secret-scrubbed) | `[new]` | J3.2 |
| J3.4 | Shared HTTP client concerns: token from `token_env`, retry with jitter, `Retry-After` and `X-RateLimit-*` awareness, per-source circuit breaker | `[new]` | J3.2 |
| J3.5 | `correlate.py` v1: failure timestamp → commits in window → changed files → likely component; confidence score, never a hard claim | `[new]` | J3.2 |
| J3.6 | Branch management: create `jarvis/incident-<id>` from the configured base branch; **never** commit to the default branch; guard asserts `allow_push_to_default_branch` | `[new]` | J3.2 |
| J3.7 | PR creation with a structured body (incident summary, root cause, fix, evidence links, verification result) and a `jarvis` label | `[new]` | J3.6 |
| J3.8 | `[test]` `respx` fixtures for every GitHub call, including 401/403/429/5xx and pagination | `[test]` | J3.2–J3.4 |
| J3.9 | `[test]` Correlation ranking on synthetic commit histories | `[test]` | J3.5 |

**Status: complete.** All tasks ✅.

**Exit criteria — met**, against recorded HTTP fixtures. Write safety is structural: `GitHubSource`
has no merge method at all, and `_assert_writable_branch` runs on every write path.

---

## Phase 4 — Vercel

**Goal:** correlate site failures with deployments and build/runtime errors.

| ID | Task | Type | Depends on |
|---|---|---|---|
| J4.1 | `sources/vercel.py`: list deployments, deployment detail, state (BUILDING/READY/ERROR/CANCELED), commit SHA and branch attribution | `[new]` | J3.1, J3.4 |
| J4.2 | Build logs for failed deployments — fetched, truncated, secret-scrubbed, stored as evidence | `[new]` | J4.1 |
| J4.3 | Runtime errors and function failures where the plan exposes them; **document the retention limitation rather than assume availability** | `[new]` | J4.1 |
| J4.4 | Deployment-failure signal → incident (severity from environment: production `CRITICAL`/`HIGH`, preview `MEDIUM`) | `[new]` | J4.1, J1.8 |
| J4.5 | `correlate.py` v2: probe failure ↔ deployment ↔ commit ↔ PR ↔ changed files, single correlation object on the incident | `[new]` | J3.5, J4.1 |
| J4.6 | Environment-variable safety: enumerate names only; **never read, log, or forward values** | `[new]` | J4.1 |
| J4.7 | Preview-deployment lookup for a branch (the substrate Phase 6 verification runs against) | `[new]` | J4.1 |
| J4.8 | `[test]` `respx` fixtures for deployments, build logs, error states, rate limits | `[test]` | J4.1–J4.3 |
| J4.9 | `[test]` Assert env-var values never appear in any evidence or serialized incident | `[test]` | J4.6 |

**Status: complete.** All tasks ✅ except J4.3 (runtime errors), which is limited by plan-dependent
log retention and is documented rather than assumed — see `JARVIS_ARCHITECTURE.md` §11.

**Exit criteria — met**, against recorded fixtures. A test asserts no method on `VercelSource`
returns an environment-variable value.

---

## Phase 5 — Supabase

**Goal:** backend visibility, strictly read-only.

| ID | Task | Type | Depends on |
|---|---|---|---|
| J5.1 | `sources/supabase.py`: project health, REST/Auth reachability probes | `[new]` | J3.1, J3.4 |
| J5.2 | Log queries (Management API), snapshot-at-detection because Free-plan retention is ~1 day | `[new]` | J5.1 |
| J5.3 | Auth diagnostics: signup/login failure rates from logs; never reads user records | `[new]` | J5.2 |
| J5.4 | Edge Function health and error surfacing | `[new]` | J5.1 |
| J5.5 | Migration/schema drift: compare applied migrations against the repo's migration directory | `[new]` | J5.1, J3.2 |
| J5.6 | RLS diagnostics: detect policy-denied errors in logs and flag the table/policy; **read-only, never proposes disabling RLS** | `[new]` | J5.2 |
| J5.7 | Write-guard: SQL verb denylist (`DROP`, `TRUNCATE`, `DELETE`, `ALTER … DISABLE ROW LEVEL SECURITY`, `GRANT`, …), gated behind `allow_production_writes` which defaults to `false` and additionally requires the `db:write` capability | `[new]` | J1.10 |
| J5.8 | `[test]` `respx` fixtures; explicit tests that every write verb is refused when the gate is off | `[test]` | J5.1–J5.7 |

**Status: complete.** All tasks ✅.

**Exit criteria — met.** The guard separates "never allowed" (DROP, TRUNCATE, RLS changes, GRANT,
auth schema, vault) from "gated" (ordinary writes), and the never-allowed set has no override.
Comment-stripping and statement-splitting defeat the obvious evasions.

---

## Phase 6 — Claude Code repair loop

**Goal:** the autonomous engineer, with verification it cannot influence.

| ID | Task | Type | Depends on |
|---|---|---|---|
| J6.0 | `[decision]` **Blocker.** `ClaudeCodeAgent` cannot run today: `claude_code_runner/` has no `dist/`, no `tsconfig.json`, no build script, pins a stale SDK, and its `query()` usage does not match the current SDK. Choose: (a) rewrite the Node runner and add a build step, or (b) drive the `claude` CLI headlessly from Python. See `JARVIS_ARCHITECTURE.md` §7. | `[decision]` | — |
| J6.1 | Implement the chosen path with permission mode, `disallowedTools`, working directory and timeout plumbed from config | `[new]`/`[extend]` | J6.0 |
| J6.2 | `briefing.py` v1: render the structured incident brief (severity, environment, component, detected-at, problem, expected, actual, reproduction, infra context, console, network, commit, files, task) | `[new]` | J1.5, J4.5 |
| J6.3 | `briefing.py` v2 — **sanitization**: `CredentialStripper` + `SecretScanner` over every field; env-var values and test-account credentials excluded structurally; unit tests assert planted secrets never survive | `[new]` | J6.2 |
| J6.4 | `briefing.py` v3 — **injection fencing**: all external text wrapped in `<untrusted_external_data>`, scanned by `InjectionScanner`, tagged `TaintLabel.EXTERNAL`; a standing instruction states that fenced content is evidence, never instruction | `[new]` | J6.3 |
| J6.5 | `policy.py`: `SafetyPolicy.may_attempt_repair(incident)` / `may_deploy(incident, verification)`; denial reasons are audited and drive `HUMAN_REQUIRED` | `[new]` | J1.10 |
| J6.6 | `repair.py` v1: isolated branch → briefing → coding agent → capture diff; no diff produced is a first-class outcome | `[new]` | J3.6, J6.1, J6.4, J6.5 |
| J6.7 | `repair.py` v2: run the repo's own test command in a bounded subprocess, capture output as evidence | `[new]` | J6.6 |
| J6.8 | `repair.py` v3: push branch, await the Vercel preview deployment, handle build failure as evidence | `[new]` | J6.7, J4.7 |
| J6.9 | `verify.py`: **re-run the exact ProbeSpec that opened the incident** against the preview URL; compare expected vs actual; PASS/FAIL is decided here and nowhere else | `[new]` | J2.5, J6.8 |
| J6.10 | Retry loop: on FAIL, feed the new evidence back as attempt n+1 up to `max_attempts` (default 3); on exhaustion stop editing code and move to `HUMAN_REQUIRED` with the branch preserved | `[new]` | J6.9 |
| J6.11 | Success path: PR by default (`deploy_mode = "pr_only"`); auto-deploy only for explicitly allowlisted fix classes and never for `CRITICAL` | `[new]` | J3.7, J6.5, J6.9 |
| J6.12 | `LoopGuard` integration on the coding agent; token/cost budget via `AgentManager` budget fields | `[extend]` | J6.6 |
| J6.13 | `[test]` `FakeCodeAgent` (mirrors `tests/agents/fake_engine.py`) driving the whole loop deterministically: happy path, no-diff, tests-fail, **claims-success-but-verification-fails**, attempts-exhausted, policy-denied | `[test]` | J6.6–J6.11 |
| J6.14 | `[test]` Assert the loop never marks an incident `RESOLVED` on Claude's assertion alone | `[test]` | J6.13 |

**Status: complete**, with J6.0 resolved in favour of option (b): `ClaudeCliAgent` drives the
`claude` CLI headlessly, so the broken bundled Node runner is bypassed rather than repaired.

**Exit criteria — met in test**, using `FakeCodeAgent` rather than a live Claude Code session (no
target application is configured). `test_agent_claims_success_but_verification_fails` covers the
deliberately-wrong-fix case: three confident claims, three failed verifications, escalation to
HUMAN_REQUIRED.

---

## Phase 7 — Notifications

**Goal:** the owner always knows what JARVIS is doing, and hears about escalation immediately.

| ID | Task | Type | Depends on |
|---|---|---|---|
| J7.1 | `notify.py`: `Notifier` ABC (`send(message, severity, incident)`) so SMS/voice/email can be added later | `[new]` | J1.5 |
| J7.2 | `TelegramNotifier` over the existing `TelegramChannel` + `TelegramChannelConfig`; `allowed_chat_ids` enforced | `[new]` | J7.1 |
| J7.3 | Message templates: alert / reproduced / attempt n-of-m / resolved / human-required / rolled-back — with the JARVIS voice on user-facing text and technical precision preserved | `[new]` | J7.2 |
| J7.4 | Outbound redaction: every notification passes `BoundaryGuard` before send | `[new]` | J7.2 |
| J7.5 | Notification policy: severity-based routing (immediate vs batched vs digest), dedup, and a rate cap so an incident storm cannot spam the owner | `[new]` | J7.3 |
| J7.6 | `[extend]` Add JARVIS env-var names to `TOOL_CREDENTIALS` in `core/credentials.py` for `jarvis connect`/`jarvis doctor` | `[extend]` | — |
| J7.7 | `[test]` Template rendering, redaction, routing, dedup and rate capping with a fake channel | `[test]` | J7.3–J7.5 |

**Status: complete.** All tasks ✅.

**Exit criteria — met**, with a fake channel rather than a live bot. The 50-incident storm test
asserts the cap holds; CRITICAL and human-required messages bypass it deliberately.

---

## Phase 8 — Autonomous operation

**Goal:** run continuously and safely, unattended.

| ID | Task | Type | Depends on |
|---|---|---|---|
| J8.1 | Continuous scheduling profile: website 5 min, critical workflows 10 min, Vercel/Supabase/GitHub polling at configured intervals, full diagnostic hourly | `[new]` | J2.11, J4.1, J5.1 |
| J8.2 | Global rate/politeness budget across probes and sources so JARVIS never hammers the site or an API | `[new]` | J3.4 |
| J8.3 | Circuit breakers per source with `degraded` reporting instead of incident spam | `[new]` | J3.4 |
| J8.4 | Rollback: detect post-deploy regression, revert via the provider's promote-previous, transition to `ROLLED_BACK`, open a linked incident | `[new]` | J4.1, J6.11 |
| J8.5 | Auto-deploy allowlist by fix class (e.g. dependency pin bump, obviously-scoped config fix) — opt-in, never `CRITICAL`, always after verification | `[new]` | J6.11 |
| J8.6 | `correlate.py` v3: recurrence detection, flapping detection, cross-signal clustering | `[new]` | J4.5 |
| J8.7 | Post-incident report generation and retention/cleanup of evidence artifacts | `[new]` | J1.8 |
| J8.8 | Crash resilience: mid-flight incidents resume on restart; in-flight branches reconciled | `[new]` | J1.8, J6.10 |
| J8.9 | `[test]` Long-running simulation over a fake clock: flapping, storms, breaker trips, restart mid-repair | `[test]` | J8.1–J8.8 |

**Status: partially complete.** J8.1–J8.3 and J8.8 are ✅ (cadence, jitter, tick isolation, circuit
breakers, restart-safe persistence). **J8.4 (rollback), J8.5 (auto-deploy allowlist execution),
J8.6 (flapping/clustering) and J8.7 (post-incident reports) are NOT implemented** — the policy gate
for auto-deploy exists and refuses correctly, but nothing acts on an allow verdict, and there is no
rollback executor.

**Exit criteria — not met.** A sustained run against a live target has not happened, because no
target is configured.

---

## Phase 9 — JARVIS interface

**Goal:** make the state legible. Deliberately last — the engine must work first.

| ID | Task | Type | Depends on |
|---|---|---|---|
| J9.1 | `server/reliability_routes.py`: health summary, incident list/detail, evidence artifacts, recent repairs, test results | `[extend]` | J1.8 |
| J9.2 | SSE/WebSocket live updates over the existing bridges | `[extend]` | J9.1 |
| J9.3 | Frontend page: system status tiles (Website / Vercel / Supabase / GitHub), active incidents, last repair, tests, deployments | `[new]` | J9.1 |
| J9.4 | Incident detail view: timeline, evidence gallery, diff, verification result, audit trail | `[new]` | J9.3 |
| J9.5 | Conversational interface: "JARVIS, what's the status?", "investigate incident 42", "hold all deploys" — natural-language commands mapped to a small, explicitly enumerated command surface (**not** free-form execution) | `[new]` | J9.1 |
| J9.6 | Voice — explicitly deferred; the `speech/` primitive exists, but this is out of scope until Phase 9 lands | `[docs]` | J9.5 |
| J9.7 | `[test]` Route tests with the existing FastAPI test client; frontend build passes | `[test]` | J9.1–J9.4 |

**Status: partially complete.** J9.1, J9.3, J9.4 and J9.7 are ✅ as a read-only API plus a
self-contained dashboard page at `/reliability`. **J9.2 (live SSE/WebSocket updates) and J9.5
(conversational interface) are NOT implemented**; the page polls every 30s instead. J9.6 (voice)
remains explicitly out of scope.

**Design note.** The dashboard is a server-rendered page rather than a React route, so it works
without a Node toolchain or `npm run build`. It reads the same endpoints a React page would, so
porting it into the SPA later needs no API change.

**Security note.** The API surface is GET-only, asserted by a test: an HTTP endpoint that can
trigger a production repair is a far larger attack surface than one that cannot. Evidence artifacts
are served through a path-validated endpoint so a crafted incident record cannot read arbitrary
host files.

---

## Phase 10 — Live integration and production readiness

Connect JARVIS to the real target application read-only, and prove the monitoring and diagnosis
pipeline works against a real system. **No repair capability is enabled by this phase.**

| ID | Task | Type | Status |
|---|---|---|---|
| J10.1 | `health.py` — six-state vocabulary (`HEALTHY`/`DEGRADED`/`FAILED`/`UNKNOWN`/`NOT_CONFIGURED`/`NOT_CHECKED`) so "we could not check" is never rendered as green | `[new]` | ✅ |
| J10.2 | `target.py` — resolve the target from config with environment overrides; report credentials **by variable name only** | `[new]` | ✅ |
| J10.3 | `diagnostic.py` — read-only `live-diagnostic` across configuration, GitHub, Vercel, Supabase, website, probes, notifications and the coding agent | `[new]` | ✅ |
| J10.4 | `probes/placeholder.py` — detect the shipped example selectors and refuse to run them, so a placeholder can never report `PASS` | `[new]` | ✅ |
| J10.5 | `analysis.py` — diagnosis-only Claude prompt that forbids modification, branching, deploying and migrations | `[new]` | ✅ |
| J10.6 | Rewrite `doctor`; add `live-diagnostic`, `notify-test` and `analyze` to the CLI | `[extend]` | ✅ |
| J10.7 | Assignment-based secret redaction in briefings (`PASSWORD=hunter2`, not just token shapes) | `[extend]` | ✅ |
| J10.8 | `blocked` failure kind — a proxy/egress failure is JARVIS's problem, never evidence about the target | `[extend]` | ✅ |
| J10.9 | [`JARVIS_LIVE_SETUP.md`](JARVIS_LIVE_SETUP.md) — env vars, least-privilege scopes, probes, health states, troubleshooting, and the pre-repair checklist | `[docs]` | ✅ |
| J10.10 | Live verification against the real target | `[test]` | **partial — GitHub only** |

**Status: complete as implementation; partial as live verification.**

**What was genuinely exercised.** `GitHubSource` against the real private target repository with a
read-only token: health, commits, 36 branches and the open pull request all returned real data.
`actions` returned `403` because the token lacks `Actions: Read` — reported as `UNKNOWN`, and the
integration as a whole as `DEGRADED`. That is the intended behaviour and it is what surfaced the
gap.

**What was not.** Vercel, Supabase, the production website and Telegram were unreachable from the
development sandbox (`api.vercel.com`, `api.supabase.com`, `api.telegram.org`, the Supabase project
host and the production domain all return `403` at the egress proxy), and no read-only tokens for
them were supplied. They are reported `NOT_CONFIGURED`/`UNKNOWN` — **not** healthy.

**Three false-health defects were found by running the diagnostic for real**, each of which had
been reporting green from an absence of evidence:

1. `vercel.production_deployment` reported `HEALTHY` with "no production deployment found" — an
   empty list read as success. Now raises, yielding `UNKNOWN`.
2. `supabase.rls_diagnostics` and `auth_diagnostics` reported `HEALTHY` with "0 denials" when the
   log query had silently failed for want of a token. Now raise when nothing was sampled.
3. Probes reported `FAILED` for proxy-blocked URLs and opened a false incident claiming production
   was down. Now a distinct `blocked` failure kind, excluded from detection.

This is the phase's main argument for itself: the architecture existing is not the same as the
production system working, and only a real run distinguishes them.

---

## Cross-cutting, every phase

- No existing test removed or weakened.
- No security default relaxed to make anything pass.
- No new paid dependency; limitations documented instead (see `JARVIS_ARCHITECTURE.md` §11).
- Every dangerous capability opt-in and audited.
- Every module small enough to review in one sitting.

---

## Baseline

Recorded on this branch (`claude/jarvis-autonomous-engineer-2yveid`, at `a97c64c`) before any JARVIS
implementation, on Python 3.11 with `uv sync --extra dev --extra framework-comparison --extra server`
followed by `uv run maturin develop --manifest-path rust/crates/openjarvis-python/Cargo.toml`:

```
uv run pytest tests/ -n auto -q -m "not live and not cloud and not hub"

4 failed, 7323 passed, 56 skipped, 242 warnings in 53.21s
```

The 4 failures are **environmental, not repository defects** — all four attempt real outbound network
calls that the sandbox's egress proxy blocks:

| Test | Cause |
|---|---|
| `tests/tools/test_web_search.py::TestWebSearchTool::test_execute_no_api_key` | falls through to live DuckDuckGo/Brave/Mojeek; `ConnectError` |
| `tests/tools/test_web_search.py::TestWebSearchTool::test_execute_tavily_error` | same fallback path |
| `tests/tools/test_web_search.py::TestWebSearchTool::test_execute_import_error` | same fallback path |
| `tests/server/test_connectors_router.py::test_connect_granola_invalid_key_returns_400_keeps_existing` | proxy returns `403 Forbidden` instead of the expected upstream 401 |

**Effective baseline for JARVIS work: 7323 passed, 0 genuine failures.** Every phase must match this.

After Phase 1: **7465 passed** (+142 tests).
After Phase 9: **7929 passed, 56 skipped**, same 4 environmental failures — 606 net new tests, no
regressions. The `browser` lane adds 12 more, run separately with `-m browser`.
After Phase 10: **8031 passed, 56 skipped**, same 4 environmental failures — 708 net new tests, no
regressions.

Running the suite **unmarked** (without `-m "not live and not cloud and not hub"`) adds 20 further
failures on top of those 4, in `tests/connectors/test_new_connectors_live.py`,
`tests/engine/test_gemma_cpp.py`, `tests/evals/datasets/`, `tests/evals/test_dataset_splits_integration.py`
and `tests/skills/test_integration_live.py`. These need connector credential files, a local Gemma
binary and HuggingFace Hub downloads; they were confirmed to fail identically with this branch's
changes stashed. The marked lane above is the one to compare against.

Two environmental notes for anyone reproducing it:

1. **The Rust extension is mandatory, and `uv sync` removes it.** Without `maturin develop`, 127 tests
   fail with `ModuleNotFoundError: No module named 'openjarvis_rust'` — `security/scanner.py`,
   `security/injection_scanner.py`, `security/rate_limiter.py`, `security/capabilities.py`,
   `tools/git_tool.py` and the SQLite storage backend all import it through
   `openjarvis._rust_bridge`. Because `uv sync` uninstalls the editable `openjarvis-rust` package,
   the build must be re-run after every sync. `make test` encodes the correct order
   (`build` then `test`); a bare `pytest` does not.
2. `--extra framework-comparison` is required or `tests/evals/comparison/` fails to collect
   (`polars` missing). CI installs it; `make setup` installs it.
