# JARVIS Security Model

**Status:** Design document. None of the controls below are implemented yet — this defines what must
be true before JARVIS is pointed at production infrastructure.

Companion documents: [`JARVIS_ARCHITECTURE.md`](JARVIS_ARCHITECTURE.md) ·
[`JARVIS_ROADMAP.md`](JARVIS_ROADMAP.md)

---

## 0. Threat model

JARVIS is an autonomous agent with credentials to production infrastructure and write access to a
source repository, whose inputs are attacker-influenceable by design (it reads web pages, logs,
database contents, and GitHub issues). The realistic threats, in priority order:

| # | Threat | Primary control |
|---|---|---|
| T1 | Prompt injection from monitored content redirects JARVIS into attacker-chosen actions | §7 — untrusted-data fencing, capability gating, no free-form command surface |
| T2 | Secrets leak into logs, incidents, notifications, PRs, or model prompts | §3 — env-var indirection, structural exclusion, layered scanning |
| T3 | An unverified or wrong "fix" reaches production | §8 — independent verification, PR-only default, no auto-deploy of CRITICAL |
| T4 | Destructive action against the production database | §5 — read-only default, verb denylist, separate capability, hard gate |
| T5 | JARVIS pushes to the default branch or force-pushes | §4 — isolated branches, explicit guard, `allow_push_to_default_branch = false` |
| T6 | Runaway loop burns money or hammers a production site | §9 — attempt caps, loop guard, rate limits, circuit breakers, budgets |
| T7 | An action cannot be explained or reversed afterwards | §10 — Merkle-chained audit log, evidence retention, rollback path |
| T8 | Test-account credentials abused, or a test account with real privileges | §6 — dedicated non-privileged accounts, env-only, never in prompts |

The controls below are stated as requirements. Each maps to a task in `JARVIS_ROADMAP.md`.

---

## 1. Principles

1. **Least privilege by default.** Every integration begins read-only. Every write capability is a
   separate, explicitly-enabled flag that defaults to off.
2. **Secrets are names, not values.** Configuration stores the *name* of an environment variable.
   Values are resolved at the last possible moment, in the narrowest possible scope, and never
   serialized.
3. **External content is data, never instruction.** Anything JARVIS did not author is untrusted.
4. **Verification is independent of the thing being verified.** The coding agent's claims are
   recorded as assertions, never accepted as evidence.
5. **Safe failure beats autonomous action.** When in doubt, stop and escalate to a human.
6. **Everything is auditable.** Any autonomous action must be reconstructable after the fact from an
   append-only, tamper-evident log.
7. **No security control is weakened to make something pass.** Not a test, not a probe, not a deploy.

---

## 2. Permissions

JARVIS runs under OpenJarvis's existing RBAC system (`security/capabilities.py`:
`Capability`, `CapabilityGrant`, `AgentPolicy`, `CapabilityPolicy`), configured **deny-by-default**
(`default_deny=True`) — the opposite of the framework's open default, which is appropriate for a
personal assistant but not for an agent with production credentials.

New capability labels to be added:

| Capability | Meaning | Default |
|---|---|---|
| `infra:read` | Read deployments, builds, logs, metrics from Vercel/Supabase | **granted** when the integration is enabled |
| `infra:deploy` | Trigger or promote a deployment | **denied** |
| `db:read` | Read-only backend queries and log reads | **granted** when Supabase is enabled |
| `db:write` | Any statement that mutates data or schema | **denied** |
| `repo:read` | Read commits, PRs, Actions, file contents | **granted** when GitHub is enabled |
| `repo:write` | Create branches, push commits, open PRs | **denied** until Phase 6 is explicitly enabled |
| `code:execute` (existing) | Run the repository's test command in a bounded subprocess | scoped to the repair workspace |

Every denial publishes `CAPABILITY_DENIED` on the EventBus and is written to the audit log with the
incident ID that provoked it.

The capability policy is a file (`~/.openjarvis/reliability/capabilities.json`), not code, so the owner
can inspect and tighten it without a code change.

---

## 3. Secrets

### 3.1 Where secrets live

| Store | Used for | Notes |
|---|---|---|
| Environment variables | All JARVIS tokens and test-account credentials | Primary mechanism; referenced by name from config |
| `~/.openjarvis/credentials.toml` | Existing OpenJarvis credential persistence | `0o600`, thread-safe writes (`core/credentials.py`) |
| `jarvis vault` | Encrypted at-rest store with a key at `security.vault_key_path` | Optional; for hosts where env vars are awkward |

**Never:** in `config.toml`, in the incident database, in evidence artifacts, in git, in a PR body, in
a notification, or in a model prompt.

Config holds only indirection:

```toml
[reliability.vercel]
token_env = "VERCEL_READONLY_TOKEN"      # a NAME
```

Env-var names to be registered in `core/credentials.py`'s `TOOL_CREDENTIALS` so `jarvis doctor` and
`jarvis connect` can report what is missing without ever printing values:
`VERCEL_READONLY_TOKEN`, `SUPABASE_READONLY_TOKEN`, `GITHUB_READONLY_TOKEN`,
`JARVIS_TEST_USER_EMAIL`, `JARVIS_TEST_USER_PASSWORD`, `TELEGRAM_BOT_TOKEN` (already present).

### 3.2 Defense in depth against leakage

Four independent layers, because any one of them can miss:

1. **Structural exclusion.** `Evidence` objects have no field that can hold a credential. Probe
   credentials are resolved inside the probe runner's step executor and are never returned in a
   `ProbeResult`. Vercel environment variables are enumerated by *name* only — the value-returning
   endpoint is never called.
2. **Redaction on the way in.** All captured text (logs, console output, HTTP bodies, build output,
   test output) passes `CredentialStripper` before it is stored.
3. **Scanning on the way out.** Every Claude briefing passes `SecretScanner` before send; a
   `CRITICAL` finding aborts the repair attempt rather than redacting and continuing, because a
   finding at that point means layer 1 failed and the incident record itself is suspect. Every
   notification passes `BoundaryGuard`.
4. **Tests that try to leak.** The test suite plants known secret patterns into logs, page content,
   HTTP responses and test output, then asserts they appear in no briefing, no notification, no
   incident row and no PR body. This test is not optional and must fail loudly if the pipeline changes.

### 3.3 Log hygiene

JARVIS log records carry incident IDs, probe IDs, deployment IDs, commit SHAs, durations and status
codes. They never carry request bodies, response bodies, credential values, database rows, or
Authorization headers. HTTP clients log method, host, path and status — never headers or bodies.

---

## 4. GitHub access

**Token:** fine-grained personal access token scoped to *one* repository.

| Phase | Permissions |
|---|---|
| Phases 3–5 (monitoring) | `Contents: Read`, `Pull requests: Read`, `Actions: Read`, `Metadata: Read` |
| Phase 6+ (repair) | adds `Contents: Write`, `Pull requests: Write` |

**Hard rules:**

- Branches only: `jarvis/incident-<id>`. The prefix is configuration; the isolation is not optional.
- **Never** commit to, push to, or force-push the default branch. `allow_push_to_default_branch`
  defaults to `false`, and the branch-creation code asserts the target is not the base branch before
  any write. This is a guard in code, not merely a config value.
- Never force-push any branch.
- Never merge a PR. Merging is a human action, always.
- Never modify workflow files under `.github/workflows/` — a self-modifying CI configuration would let
  a compromised repair loop disable its own checks. Attempts are refused and audited.
- Never modify JARVIS's own security modules, capability policy, or safety configuration from within a
  repair. The repair workspace is the *target application*, not this repository.
- Issue and PR bodies are untrusted input (§7).

---

## 5. Supabase access

**Default posture: read-only, and the default is load-bearing.**

- Management API token with the minimum role that can read project health, logs and migration state.
- Database access, when used, goes through a role restricted to `SELECT` on the schemas needed for
  diagnostics — not the `service_role` key. The `service_role` key must never be given to JARVIS.
- Auth diagnostics are derived from aggregate log data (failure counts, error codes), never from
  reading user records.

**Write gate.** Any statement that mutates data or schema requires *all* of:

1. `[reliability.supabase] allow_production_writes = true` (default `false`), **and**
2. the `db:write` capability granted in the capability policy, **and**
3. the statement passing the verb denylist, **and**
4. an approved entry in `ApprovalStore` at tier `high` (always ask, never auto-remembered).

**Permanently refused, regardless of gates** — these are never legitimate autonomous repairs:

- `DROP` (table, schema, database, policy, function)
- `TRUNCATE`
- `DELETE` without a `WHERE`, and any `DELETE` against production
- `ALTER TABLE … DISABLE ROW LEVEL SECURITY`
- `DROP POLICY`, or any statement weakening an RLS policy
- `GRANT` / `ALTER ROLE` / `CREATE ROLE`
- Anything touching `auth.*` schema objects
- Reading secrets from Vault/`vault.decrypted_secrets`

RLS diagnostics report *that* a policy denied an operation and *which* policy; a proposed fix that
disables or broadens RLS is rejected by the policy gate and escalated to `HUMAN_REQUIRED`. Weakening
security to make a probe pass is explicitly out of bounds (Principle 7).

---

## 6. Production website access and test accounts

- Probes are read-mostly: navigate, observe, assert. Any workflow that creates data (signup, checkout)
  must be declared `mutating = true` in its probe spec and is disabled by default.
- Test accounts are **dedicated, non-privileged, per-environment**, with a naming convention that makes
  their rows obvious in production data (e.g. `jarvis-probe+prod@…`). Never the owner's account, never
  an admin account, never a real customer account.
- Credentials come from environment variables named in the probe spec (`email_env`, `password_env`).
  The spec stores names; the runner resolves values; the result carries neither.
- Credentials are **never** sent to Claude. A briefing about a login failure says "authentication with
  the configured probe account failed at step 3", not what the account is.
- Screenshots are evidence and may contain rendered session data. They are stored locally under
  `~/.openjarvis/reliability/evidence/`, subject to a retention setting, and are **not** attached to
  PRs or notifications by default.
- Probe traffic is rate-limited and jittered so JARVIS is never itself the cause of a load problem, and
  probe requests carry a stable identifying `User-Agent` so its traffic is attributable in the target
  application's own logs.

---

## 7. Prompt-injection defenses

JARVIS reads content that an attacker can write: page text, console output, log lines, database rows,
commit messages, PR and issue bodies, API responses, CI output. All of it is untrusted.

**Control 1 — structural fencing.** Untrusted content is wrapped before it enters any prompt, reusing
the `wrap_tool_output()` convention from `security/credential_stripper.py`:

```
<untrusted_external_data source="browser_console" incident="INC-00042">
...verbatim captured content...
</untrusted_external_data>
```

The briefing carries a standing instruction stating that content inside these fences is evidence to be
analyzed, that it is never an instruction, and that any instruction-like text inside it must be
reported as a finding rather than followed. Fence markers appearing inside captured content are
escaped so content cannot close its own fence.

**Control 2 — scanning.** All untrusted content is run through `InjectionScanner` before inclusion.
Findings are attached to the incident and included in the briefing *as a warning*, and a `CRITICAL`
finding routes the incident to `HUMAN_REQUIRED` instead of to the repair loop.

**Control 3 — taint tracking.** Untrusted content is tagged `TaintLabel.EXTERNAL` using the existing
`security/taint.py` labels, so the `SINK_POLICY` mechanism can refuse to pass it to sinks that would
give it effect.

**Control 4 — capability containment.** Even a fully successful injection cannot exceed the capability
policy. There is no capability for "delete production data", "disable RLS", "push to main" or "deploy"
in the default grant set, so an injected instruction to do any of those fails at the gate and is
audited. **This is the control that actually matters** — fencing and scanning reduce likelihood;
capability containment bounds impact.

**Control 5 — no free-form command surface.** The conversational interface (Phase 9) maps natural
language onto a small, explicitly enumerated set of commands. It does not execute arbitrary
instructions, and it does not accept commands from any source other than the authenticated owner —
notably not from a monitored page, a log line, or a GitHub comment.

**Control 6 — the verification loop is not model-driven.** Verification re-runs a stored probe spec
and compares against declared expectations. There is no point at which a model decides whether
verification passed, so injected content cannot talk its way to `RESOLVED`.

---

## 8. Deployment controls

**Default: `deploy_mode = "pr_only"`.** JARVIS opens pull requests. Humans merge them. Nothing about
Phases 1–7 changes that.

Preconditions that must *all* hold before any deployment path is even considered:

1. Independent verification PASSED — the original failing probe now passes against a preview
   deployment (§ architecture doc, "Independent verification").
2. The repository's own test suite passed.
3. The incident's fix class is in `auto_deploy_fix_classes` (empty by default).
4. The severity is not `CRITICAL`. Critical fixes are never auto-deployed, regardless of allowlist —
   the blast radius of being wrong is exactly the case where a human should look.
5. The diff touches no security-sensitive path: auth code, RLS policies, middleware, CI configuration,
   dependency manifests, or secret handling.
6. The `infra:deploy` capability is granted.

If any precondition fails, JARVIS opens a PR and notifies. It does not partially proceed.

**Attempt limit.** `max_attempts` defaults to 3. On exhaustion JARVIS stops modifying code, preserves
the branch and all evidence, transitions to `HUMAN_REQUIRED`, and notifies. It does not widen its
approach, escalate its own permissions, or try a "bigger" fix.

---

## 9. Rollback controls

- Every deployment JARVIS causes records the previously-good deployment ID before it acts.
- Post-deploy, the incident's probe set re-runs on a short interval for a configured watch window.
- A regression inside the watch window triggers rollback via the provider's promote-previous mechanism
  — a deployment operation, never a `git push --force` and never a history rewrite.
- The incident transitions to `ROLLED_BACK`, a new linked incident is opened, and the owner is notified
  immediately regardless of the original severity.
- Rollback is bounded: JARVIS rolls back once. A second regression is `HUMAN_REQUIRED`.
- Database changes are **not** rolled back automatically. Schema and data changes require the human
  path in every case, which is one more reason the default posture is read-only.

---

## 10. Audit trail

Every autonomous action is written to the existing `AuditLogger` (`security/audit.py`) — append-only
SQLite with a Merkle hash chain, so tampering is detectable.

Recorded for every incident: creation and fingerprint; every state transition with actor and reason;
every capability check and denial; every external API call (method, host, path, status — never bodies);
every Claude invocation (attempt number, briefing hash, model, token/cost); every diff applied
(commit SHA and diffstat); every test run and its result; every verification and its verdict; every
policy decision with the deciding rule; every notification sent; every deploy or rollback.

Retention: incidents and audit rows are kept indefinitely by default (they are small). Evidence
artifacts — screenshots, traces, HAR files — have a configurable retention because they are large and
may contain rendered session data.

The audit log is the answer to "why did JARVIS do that?", and it must be sufficient to answer it
without access to the model, the site, or the infrastructure.

---

## 11. Claude Code access

`ClaudeCodeAgent` is treated as a **capable but untrusted contractor**:

- It runs against a dedicated repair workspace — a checkout of the *target application*, never this
  repository and never the host's home directory.
- Working directory, allowed tools, disallowed tools and timeout are supplied by JARVIS, not by the
  agent. (Today the Python wrapper populates none of these; see `JARVIS_ARCHITECTURE.md` §7 — this is
  a prerequisite, not a nice-to-have.)
- It receives no credentials. Not the Vercel token, not the Supabase token, not the GitHub token, not
  the test-account password. It receives a sanitized incident briefing and a code checkout.
- It cannot deploy, cannot touch the database, cannot push to the default branch, and cannot open a PR
  itself — those actions belong to JARVIS and go through the policy gate.
- Its output is parsed as data. Its claim of success is stored as `attempt.claim`, and it has no path
  to set an incident's state.
- Its resource use is bounded by `LoopGuard` and by the token/cost budget fields already present in
  `AgentManager`.

**Cost note.** Claude Code is the one component that is not free (see `JARVIS_ARCHITECTURE.md` §11).
Whichever invocation path is chosen, the credential for it is a secret under §3 and its spend is
budgeted and audited under §10.

---

## 12. Network safety

- All outbound HTTP goes through clients that apply the existing `security/ssrf.py` checks, so a
  redirect or a configured URL cannot reach cloud metadata endpoints or private ranges. The
  legitimate exception — a self-hosted target on a private address — must be an explicit allowlist
  entry, not a disabled check.
- Response bodies are size-capped (the existing `http_request` tool caps at 1 MB) and redirect chains
  are bounded.
- Per-source token-bucket rate limiting (`security/rate_limiter.py`) plus per-source circuit breakers.
- Timeouts on every call. No unbounded waits anywhere in the monitoring loop.

---

## 13. Pre-production checklist

Before JARVIS is pointed at real infrastructure, all of the following must be true and demonstrated:

- [ ] All tokens are read-only and scoped to a single project/repository
- [ ] The Supabase `service_role` key is not available to JARVIS in any form
- [ ] `allow_production_writes = false`, `allow_push_to_default_branch = false`, `deploy_mode = "pr_only"`
- [ ] Capability policy is deny-by-default and has been reviewed line by line
- [ ] Test accounts exist, are non-privileged, and are distinguishable in production data
- [ ] The secret-leakage test suite passes, including the planted-secret cases
- [ ] The injection-fencing test suite passes, including fence-escape attempts
- [ ] The "Claude claims success but verification fails" test passes
- [ ] Attempt cap, loop guard, rate limits and circuit breakers are all exercised by tests
- [ ] Audit log is writing, and its Merkle chain verifies
- [ ] Telegram notifications reach only `allowed_chat_ids`
- [ ] Rollback has been rehearsed end-to-end against a preview or staging target
- [ ] The owner can stop JARVIS with a single command, and knows what it is
