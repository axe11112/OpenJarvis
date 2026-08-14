# JARVIS — 24/7 Autonomous Reliability

How JARVIS runs continuously, unattended, and what it refuses to do while it is
running.

> **The autonomous endpoint is a pull request, never a deployment.** JARVIS
> cannot merge, cannot deploy to production, cannot push to the default branch,
> and cannot write to the production database. These are not settings to be
> careful with — the watcher **refuses to start** if the configuration would
> allow any of them alongside automatic repair.

Companion documents: [`JARVIS_REPAIR_LOOP.md`](JARVIS_REPAIR_LOOP.md) ·
[`JARVIS_LIVE_SETUP.md`](JARVIS_LIVE_SETUP.md) ·
[`JARVIS_SECURITY.md`](JARVIS_SECURITY.md) ·
[`JARVIS_ARCHITECTURE.md`](JARVIS_ARCHITECTURE.md)

---

## 1. The loop

```
        ┌──────────────── every interval_seconds ────────────────┐
        │                                                        │
   HEALTH CHECK ──▶ COMPARE WITH PREVIOUS ──▶ DEDUPLICATE ──▶ CLASSIFY
        │                     │                    │             │
        │                     │                    │             ▼
        │                     │                    │      flapping? ──▶ HUMAN_REQUIRED
        │                     │                    │             │
        │                     │                    │             ▼
        │                     │                    │      admission gate
        │                     │                    │      (concurrency,
        │                     │                    │       cooldown, stop)
        │                     │                    │             │
        │                     ▼                    │             ▼
        │            recovered? ──▶ RECOVERED_EXTERNALLY   REPAIR LOOP
        │                                                        │
        └────────────────────── WAIT ◀───────────────── PR + NOTIFY
```

The repair loop itself — worktree, agent, scope, checks, preview, verification —
is unchanged from Phase 12 and documented in
[`JARVIS_REPAIR_LOOP.md`](JARVIS_REPAIR_LOOP.md). This document covers what
surrounds it.

---

## 2. Running it

```bash
jarvis reliability watch        # 24/7 loop in the foreground
jarvis reliability watch --once # one pass over every check, then exit
```

Startup order, every time:

1. **Check the stop flag.** If an emergency stop is in effect, refuse (exit 3).
2. **Check the configuration.** If repair could reach production, refuse (exit 2).
3. **Print every safety interlock.**
4. **Run crash recovery** — park anything left mid-repair.
5. **Begin checking.**

```
JARVIS

  Monitoring               ON
  Automatic repair         OFF
  Production deployment    OFF
  Default branch push      OFF
  Automatic PR merge       OFF
  Supabase writes          OFF
  Deploy mode              pr_only
  Maximum repair attempts  3
  Concurrent repairs       1
  Check interval           60s
```

### Other commands

```bash
jarvis reliability status          # configuration and open-incident summary
jarvis reliability incidents       # incidents plus the safety posture
jarvis reliability incidents --open
jarvis reliability repair <id>     # one explicit repair attempt
jarvis reliability report <id>     # post-incident report
jarvis reliability stop            # emergency stop
```

---

## 3. Startup safety

The watcher refuses to start on any of these **combinations**:

| Combination | Why it is refused |
|---|---|
| repair + `allow_push_to_default_branch` | a repair could rewrite `main` with no review |
| repair + `deploy_mode` other than `pr_only`/`never` | a repair could reach production without a human |
| repair + `allow_production_writes` | a repair could modify live data |
| repair + no `workspace` | there is nowhere safe to work |

Each flag is individually defensible; the pairs are not. That is exactly the
kind of mistake that survives review and is discovered during an incident, so it
is checked at the only moment where refusing is cheap.

Every problem is reported, not just the first — one message should tell the
operator everything that is wrong.

---

## 4. Deduplication

One active incident per fingerprint. A homepage failing every minute for an hour
is one problem, not sixty:

```
10:00 homepage failed   → INC-0042 opened
10:01 homepage failed   → INC-0042, occurrence 2
10:02 homepage failed   → INC-0042, occurrence 3
```

A **new** incident is opened only when the previous one is no longer open and the
same failure returns, or when the fingerprint materially changes. The
fingerprint normalises timestamps, UUIDs, hex blobs, ports, line numbers and
durations, so cosmetic variation in an error message does not split one problem
into many.

### The pending-pull-request hold

Deduplication alone is not enough. A verified repair opens a pull request — and
because nothing merges automatically, **production is still broken**. The probe
keeps failing, the incident is resolved, so the next failure is genuinely a new
incident, and JARVIS would repair it again.

Before this was fixed, a single sustained outage produced **six pull requests in
six ticks**. The repair gate therefore holds a cooldown keyed on the
*fingerprint* (not just the incident id) after any repair that opened a pull
request: `pending_pr_cooldown_seconds`, one hour by default.

---

## 5. Flapping

A check that alternates is not a check that is failing:

```
PASS FAIL PASS FAIL PASS FAIL
```

This usually means something intermittent — a cold start, a rate limit, one bad
node, a race. It almost never means a bug a coding agent can fix, and sending it
to one spends a Claude session on a failure that has already cleared. The
verification then "passes" for the wrong reason.

JARVIS counts **pass→fail transitions** inside a sliding window. Counting
transitions rather than failures is deliberate: ten consecutive failures is an
outage and belongs on the repair path; ten alternations is flapping.

```toml
[reliability.flapping]
window = 10             # how many recent results to remember
failure_threshold = 3   # transitions inside the window that make it flapping
min_samples = 4         # never judge before there is history to judge on
```

A flapping incident is marked, escalated to `HUMAN_REQUIRED`, and **not**
repaired. Note that the *first* failure is always treated as real — alternation
is only visible after several samples.

The confirmation tracker (`confirm_runs`) answers a narrower question — "has this
failed N times in a row?" — and cannot see alternation at all, since a strict
pass/fail/pass/fail sequence never reaches two consecutive failures. The two
mechanisms are complementary.

---

## 6. Severity

Deterministic rules, evaluated in order, first match wins. **Not** a model's
opinion: severity decides who gets woken up and whether JARVIS may touch the
code.

| Severity | Condition |
|---|---|
| `CRITICAL` | authentication unreachable or 5xx; site did not respond at all; 5xx on a critical path |
| `HIGH` | an authentication or critical-path workflow is broken; any 5xx |
| `MEDIUM` | a 4xx response |
| `LOW` | a visual or non-blocking issue |

The probe's declared severity is a **floor**, never a ceiling. Observed impact
can raise it; nothing lowers what the operator declared. Raising-only is the safe
direction — over-classifying costs a notification, under-classifying costs an
outage nobody heard about.

Every classification records the rule that fired and why, both of which reach the
incident record and the owner's notification.

---

## 7. Repair admission

Even when policy permits a repair, the gate decides whether one may start *now*:

| Gate | Default | Rationale |
|---|---|---|
| Concurrency | 1 | Two agents in two worktrees producing two PRs for one root cause is worse than a queue |
| Cooldown after failure | 300s | Otherwise a permanently broken check retries as fast as the loop spins |
| Cooldown after a PR | 3600s | Success means "a PR is open", not "production is fixed" |
| Flapping | — | Escalates instead |
| Emergency stop | — | Refuses everything |

Cooldowns are keyed on the incident id **and** the fingerprint, because a
recurring failure gets a new incident each time it returns.

---

## 8. Recovery

When a failing check passes again, JARVIS records **how** it recovered:

| Recovery type | Meaning |
|---|---|
| `VERIFIED_REPAIR` | a JARVIS repair passed independent verification |
| `RECOVERED_EXTERNALLY` | it stopped reproducing with no JARVIS involvement |
| `HUMAN_RESOLVED` | a human closed it |

The distinction is not cosmetic. Letting JARVIS take credit for every transient
failure that cleared itself would make its real effectiveness impossible to
measure — and would be exactly the kind of flattery that gets a system trusted
past its competence.

An incident that recovers is **not** repaired, and a repair already in flight is
never raced to a conclusion by the detector.

---

## 9. Crash recovery

If JARVIS dies mid-repair, the incident is found in `FIXING`, `TESTING` or
`VERIFYING` on the next start. It is moved to `RECOVERY_REQUIRED` and **never
resumed automatically**.

A process that died during `FIXING` may have left a worktree, a branch, or a
half-applied change. Starting a second coding agent on top of that is how one
outage becomes two. A restart is not evidence that the previous repair is safe to
continue.

`RECOVERY_REQUIRED` deliberately has no transition back to `FIXING`. The route
out is `INVESTIGATING`, which starts the pipeline again from the beginning with
fresh evidence — an explicit human decision, taken with
`jarvis reliability repair <id>`.

---

## 10. Notifications

| Stage | Message |
|---|---|
| Incident detected | 🔴 severity, component, incident id |
| Repair started | 🔧 attempt n/3 |
| Verified | 🟢 tests, build, browser, PR number, "Production: UNCHANGED" |
| Human required | 🚨 attempts, reason, "Production: UNCHANGED" |
| Recovered | 🟢 whether a repair was involved |

Delivery goes through the `Notifier` interface. `TelegramNotifier` and
`ConsoleNotifier` ship; `MultiNotifier` fans out to several. Messages pass the
outbound redaction guard, are rate-capped, and are deduplicated inside a window.

### Escalation

A `CRITICAL` incident still open after `critical_escalation_minutes` is raised
again, up to a bounded number of reminders. An alert that repeats forever trains
its reader to ignore it, which is worse than one that stops.

### On SMS and voice

**Not implemented, deliberately.** Every SMS and voice gateway worth relying on
is a paid third-party service. Shipping a stub would look like coverage while
providing none, which is worse than shipping nothing.

The interface is ready: a provider only has to implement `Notifier.send`. Adding
one is a small, local change once you have a gateway to point it at.

---

## 11. Emergency stop

```bash
jarvis reliability stop
```

```
JARVIS STOPPED

Monitoring:   OFF
New repairs:  BLOCKED
Production:   UNCHANGED
```

Deliberately non-destructive. Incidents, evidence, audit records, branches and
worktrees are all left exactly as they are — stopping must be a safe thing to do
in a panic, which means it must never delete anything.

The stop is a **file**, not a signal, so it survives a restart. JARVIS must not
quietly come back on because the host rebooted. Remove the `STOPPED` file next to
the incident database to allow it to start again.

---

## 12. Cost control

Claude sessions are the expensive part. Five mechanisms bound them:

1. **Deduplication** — one incident per fingerprint, not one per failed check.
2. **Confirmation** — N consecutive failures before an incident opens at all.
3. **Flapping detection** — intermittent checks escalate instead.
4. **Concurrency limit** — one repair at a time.
5. **Cooldowns** — after a failure, and after a pull request is already open.

JARVIS never runs a coding agent per failed HTTP request. Failures are aggregated
into an incident first, and the incident is what gets repaired.

---

## 13. Audit

Every automated action is recorded, and the incident transition log is a
hash chain verified by `jarvis reliability verify-audit`.

Events: `WATCH_STARTED` · `WATCH_STOPPED` · `TICK_START` · `TICK_END` ·
`INCIDENT_OPENED` · `INCIDENT_DEDUPED` · `INCIDENT_RECURRENCE` ·
`INCIDENT_TRANSITION` · `REPAIR_ATTEMPT_START` · `REPAIR_ATTEMPT_END` ·
`VERIFICATION` · `PR_CREATED` · `POLICY_DENIED` · `FLAPPING_DETECTED` ·
`RECOVERY_REQUIRED` · `RECOVERED_EXTERNALLY` · `HUMAN_REQUIRED` ·
`REPORT_GENERATED`

Secrets never enter any of them: evidence has no field capable of holding a
credential, and every free-text field passes redaction on the way in and out.

---

## 14. Post-incident reports

```bash
jarvis reliability report INC-0042
```

Built from the incident record alone — no model, no narration. Contains the
timeline from the transition log, detection and acknowledgement times, attempts,
root cause, changed files, regression tests, check results, preview URL,
verification verdict, pull request, recovery type, and an explicit
"Production deployment: Not performed".

---

## 15. Configuration

```toml
[reliability.watch]
enabled = false            # unattended operation
interval_seconds = 60
max_concurrent_repairs = 1
cooldown_seconds = 300
recover_on_start = true

[reliability.flapping]
enabled = true
window = 10
failure_threshold = 3
min_samples = 4

[reliability.notification]
enabled = false
min_severity = "MEDIUM"
critical_escalation_minutes = 5
providers = ["telegram"]

[reliability.repair]
enabled = false            # the master switch — see JARVIS_REPAIR_LOOP.md §12
max_attempts = 3

[reliability.policy]
deploy_mode = "pr_only"
allow_push_to_default_branch = false
```

Every dangerous option defaults off, and the watcher refuses to start if any of
them is combined with automatic repair.

---

## 16. What is proven, and what is not

**Proven** by `tests/reliability/test_watch_e2e.py`, driving the real monitor,
detector, store, git worktrees and project test suite:

- a detected failure reaches a pull request, with notifications at every stage
- a plausible-but-wrong fix fails verification three times and reaches
  `HUMAN_REQUIRED` with no pull request
- a failure that clears itself is recorded `RECOVERED_EXTERNALLY` and never
  repaired
- an alternating check is escalated, not repaired — while a sustained outage
  still is
- a persistent failure produces one incident, and one pull request, not one per
  tick
- the default branch is byte-identical after every scenario

**Not proven.** The same two gaps as Phase 12, unchanged:

- **The coding agent is scripted, not the real `claude` CLI.** `ClaudeCliAgent`
  is unit-tested but has never driven a live session.
- **The "preview deployment" is the worktree**, not a Vercel build reached over
  HTTP by a browser.

Both need network access and an authenticated CLI. Until they are closed, treat
"JARVIS can repair software autonomously" as demonstrated against a fixture and
unproven against production.

---

## 17. Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| Exit 2, "refuses to start" | Repair is combined with a production reach | Read the listed problems; each names both flags |
| Exit 3, "JARVIS is stopped" | An emergency stop is in effect | Remove the `STOPPED` file |
| Incidents in `RECOVERY_REQUIRED` | JARVIS died mid-repair | Inspect the worktree and branch, then `jarvis reliability repair <id>` |
| A check escalates as flapping repeatedly | The check is genuinely intermittent | Fix the check, or raise `failure_threshold` |
| Repairs are deferred | Concurrency or a cooldown | `jarvis reliability incidents`; a pending PR holds for an hour by design |
| One outage, many incidents | The fingerprint is unstable | Check the probe's error text for unnormalised variable content |
| No repair ever starts | `[reliability.repair] enabled = false`, or severity not allowlisted | `CRITICAL` is excluded from auto-repair by default |
