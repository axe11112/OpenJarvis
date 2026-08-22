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

Telegram is not a monitoring console. The owner hears from JARVIS in exactly
two situations, and for one underlying problem they hear at most one of them:

1. **It is fixed** — and only if they were told it was broken. A problem they
   never heard about, that recovered on its own, produces nothing at all.
2. **JARVIS needs a specific thing from them** — a decision, an approval, a
   credential rotated, a deployment rolled back. Not "I could not fix it",
   which is a status. The message carries the exact operator action, and when
   no action can be named nothing is sent and the incident parks visibly in
   Control Center instead.

Everything else is logged and shown in the Control Center: incidents opening,
severity rising, another probe joining an outage, repairs starting, previews
building, merges landing, production verification running. Those are steps. The
owner is told outcomes.

That is a policy, not a tuning knob, and it inverts the obvious design. The
tempting version narrates — problem found, investigating, repairing, verifying,
PR opened, merged — and every message is true and the sequence is worthless. An
owner who gets six messages per incident learns to swipe them away, and the one
that mattered arrives in a stream they have trained themselves to ignore.

Copy is deterministic: assembled from the component, the outcome, the recorded
root cause and a PR number. No model writes a notification. Messages pass the
outbound redaction guard and are rate-capped.

### One problem, however many probes noticed it

An incident is what a *probe* saw. An outage is what *went wrong*. Conflating
them is how one failing deployment produced ten messages in a morning: the
homepage probe opened an incident, login opened another, sign-up a third, and
each carried its own fingerprint, its own ledger entry and its own escalation.

`openjarvis/reliability/outage.py` introduces the second object. Every incident
is assigned an owner-facing **outage identity**, and deduplication, escalation
and success messages all key on that rather than on the incident or its
fingerprint. Five failing probes produce one message; the five incidents
survive intact underneath it, with their own evidence, in the store and on the
Control Center.

Correlation is conservative, because getting it wrong hides a real problem
inside one the owner has already dismissed. Three rules:

| Rule | Effect |
| --- | --- |
| **Families never merge** | A failing database, an auth *security* failure, an external provider outage and a failing website are four different problems whatever their timing |
| **Only availability groups across components** | "The site did not answer" is a claim about the site. A wrong assertion on a page that loaded is a claim about that page, and groups only with itself — unless a *shared failing deployment* is established for both, which is evidence rather than proximity |
| **Time is a constraint, not a signal** | Coincidence alone never groups anything; failing every other rule is never rescued by overlapping |

An unrecognised component becomes its own family. An extra message is the right
answer to "I do not know what this is".

The registry persists beside the incident database (`outages.json`), so the
outage an incident belongs to survives a restart, a sleeping laptop, and the new
incident ids a flapping check produces every few minutes.

### An escalation has to ask for something

`openjarvis/reliability/owner_ask.py` assembles a structured `OwnerAsk` before
any escalation is sent: what failed, the established cause, the evidence, what
was tried, why JARVIS cannot safely continue, and one field the rest of the
system treats as a gate — **the exact operator action required**.

If that field is empty there is no escalation. Not a vaguer one, none. The
problem stays open, the investigation continues, and Control Center shows it
parked with the reason no action could be named.

| Recorded reason | What the owner is asked |
| --- | --- |
| post-merge verification failed | Revert the PR or roll production back, then tell me to continue |
| protected path | Apply the change yourself, or allow-list the path |
| a secret in the change | Rotate the credential, then tell me to continue |
| scope exceeded | Approve the larger change in Control Center |
| repair disabled | Reply "Fix it", or fix it yourself |
| a flapping check | *nothing* — the check is unreliable, which is a monitoring problem |
| an interrupted repair | *nothing* — parked for review |
| latency only / observer degraded | *nothing* — not corroborated as a production outage |
| attempts exhausted | A rollback decision, **only** when there is a decision to make |

That last row is the line this change must not cross. An exhausted repair on a
MEDIUM contract failure parks in silence. An exhausted repair while users cannot
reach the site is a decision only the owner can make, and refusing to ask it
would be hiding an outage rather than quietening one. So the ask is named when
a previous working deployment is recorded, and named without the SHA when the
outage is genuinely taking the site away from users. A failure whose kind was
never recorded counts as user-facing: "unknown" is not "harmless".

### Saying it once

The in-process dedup window and hourly cap are memory, and the watcher restarts
whenever the machine sleeps, the code updates, or launchd decides to. The owner
does not experience a restart as a fresh start; they experience it as being told
the same thing again.

So the record of what has been said is persisted next to the incident database,
keyed by the **underlying problem** and by **what it currently asks of the
owner**:

| Owner-facing state | Internal states that collapse into it |
| --- | --- |
| `fixed` | `RESOLVED` |
| `needs-you:<SEV>:<ask>` | `HUMAN_REQUIRED`, `FAILED`, `RECOVERY_REQUIRED`, `ROLLED_BACK` |
| `working:<SEV>` | everything else |

`<ask>` is a digest of the operator action, and of nothing else — not the
attempt count, not the internal reason, and deliberately not the list of
affected components. A fourth probe joining an outage the owner has already been
asked about is not a new thing to do.

Only three things speak again: it is fixed, JARVIS has stopped when it was
working, or the *action required* changed. A severity that rises without
changing what the owner must do is a state machine talking about itself.

Problem and success share one slot, so "it needs you" and "it is fixed" are two
positions of one conversation about one outage rather than two subscriptions
that do not refer to each other. A success is recorded rather than forgotten:
the repair loop, the post-merge verifier and the detector all have a claim on
"it works again", and without a record the owner reads those as three fixes for
one problem.

Two failure modes are handled deliberately in opposite directions. A ledger that
cannot be read costs a duplicate, never a missed outage — when in doubt, speak.
A *recovery* with no ledger to consult sends nothing — without a record of
having woken somebody there is no conversation to end.

The router is also built once per process. It used to be constructed separately
by the watcher, the repair loop, the merger, the post-merge verifier and two CLI
commands; each held its own in-memory snapshot of the ledger, so one component
recording "the owner has been told" did not stop another sending it again.

### Detection is never news

A CRITICAL detection does not interrupt the owner. `alert_on_critical` exists
and defaults to off, and the default matters more than the switch: an incident
opening is the system working, and a message that arrives before JARVIS knows
whether it can handle the problem is followed thirty seconds later by one that
does know. That pair — "something serious happened", then "I need your help" —
was half of the ten messages one morning produced.

A rollback is the one detection-shaped event still sent unconditionally:
production changed underneath the owner, JARVIS did it, and there is no other
way for them to find out when it matters.

### Replaying the history

```bash
jarvis reliability replay-notifications          # both counts, from the real store
jarvis reliability replay-notifications --show   # ...and every message
```

Reads the incident database, replays the recorded history through the old rules
and the new ones, and reports both counts. Nothing is sent: the transport is a
recorder and the store is only read. Over the five incidents of the morning that
prompted this work, ten messages become one.

The "before" number is a **lower bound**. The store records incidents, not
deliveries, so an incident that flapped between states during a cycle the store
never observed produced messages the replay cannot see.

### Replying to Sir

An escalation that ends "reply *Fix it* to let me continue" has to mean
something. `openjarvis/reliability/owner_commands.py` accepts exactly two
things — "Fix it" and a status question — from a chat id on the existing
`allowed_chat_ids` allowlist, with matching done by a closed phrase table and no
model in the path.

An empty allowlist authorises nobody. That is the opposite of how the outbound
channel treats an empty list, and the asymmetry is the point: "send to nobody"
loses a message, "accept from anybody" hands over a control. A negated
instruction ("don't fix it") is never interpreted at all.

"Fix it" clears the **repair cooldown** for the incidents in the current outage,
acknowledges once, and goes quiet. That is the whole of it. It does not clear
the emergency stop, raise the attempt ceiling, approve a merge, relax a
verification gate or touch production — those refuse for reasons a text message
does not answer. With two independent problems open it asks one question rather
than guessing which one "it" meant.

Off by default: `[reliability.notify] accept_owner_commands`.

### A failed build is not an alert forever

`_poll_source` called `source.poll()` with no `since` for as long as it existed,
so every cycle re-reported every failed deployment still in the API's newest
page. Four production deployments cancelled within fifteen seconds of each other
on 15 August — superseded seventeen seconds later by a READY one, and followed by
nine more successful production deployments — were still being re-reported two
days later, 328 occurrences each, holding four HIGH incidents open against a
completely healthy site.

Two bounds now, because one is not enough:

* a **watermark** per source, which is what `since` was always for, stops a
  failure being reported twice within a process;
* an **age cutoff** in the source (six hours by default, configurable, zero to
  disable) is what survives a restart, where a watermark cannot help.

A deployment with an unreadable timestamp is still reported: unknown age must not
become a silent drop.

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

[reliability.notify]
enabled = false
channel = "telegram"
min_severity = "MEDIUM"
max_messages_per_hour = 20
alert_on_critical = false      # a detection is not an outcome — see §10
accept_owner_commands = false  # "Fix it" from the owner's chat

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

## 15b. Fail-closed, and what changed

Several safety checks were written so that the thing that decides *whether* to
check could quietly answer "no". Since a refusal is expressed by a check
failing, a check that never ran was indistinguishable from one that passed.
Each of these is now a refusal, and each has a test that fails if the
precondition comes back.

| Was | Is |
|---|---|
| `base_unchanged` was skipped when neither base SHA was known — which is what a failed GitHub read produces | Unknown refuses. Not knowing where the base is, is not evidence it has not moved |
| `original_reproduction` was skipped when the incident had no `probe_id` — every incident opened from a Vercel or Supabase signal | Refuses. A detection that cannot be re-run cannot be shown to be fixed by re-running it |
| A gate added inside an `if` could vanish silently | `gates_complete` enumerates the expected gates and refuses, naming any that did not run |
| Auth, session, permission, role and dependency-manifest changes were auto-merged | Refused. The pull request body already promised the owner these are never deployed automatically, and `may_deploy` already honoured it; merge now does too |
| Post-merge verification reported success with an unloadable probe fleet, or with no reproduction supplied | Both refuse. A fleet that could not be loaded is unknown, not empty |
| A broken secret or injection scanner returned "clean" | Falls back to the same pattern tables; if even those are unreachable, the briefing is refused |
| `BoundaryGuard` silently passes text through when its scanners are missing, and does not raise | `CredentialStripper` now always runs after it. If nothing can redact, the body is withheld |
| Durable state was written with `write_text`, which truncates first | Written atomically. A sleep mid-write no longer empties the notification ledger and re-announces everything |
| Call suppression lived only in memory | Persisted beside the incident database, in wall-clock, so a restart does not ring the owner again |
| `MERGED` counted as a closed, human-free success | Excluded from the rate and reported as `merged_awaiting_production` |

None of these makes JARVIS repair less. Every one of them makes it *claim*
less: the repair still runs, the pull request is still opened, and the only
step that now waits more often is the last unattended one.

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

**Proven as of Phase 15**, by `tests/reliability/test_claude_live.py` — run with
`-m live_claude`, against the **real** `claude` CLI:

- a real headless `claude -p` process repairs a real bug in a real worktree and
  reaches a pull request, given a briefing that describes symptoms only and never
  names the file;
- the worktree's branch, HEAD and clean status are verified before it starts;
- JARVIS reads the resulting diff from git, and records a real commit SHA;
- verification retains final authority — wired to a reproduction that cannot
  pass, no amount of correct work by a real agent produces `RESOLVED`;
- the agent cannot read JARVIS's credentials, and nothing outside the worktree
  changes.

**Still not proven.** One gap remains, and it is a network gap rather than a
design gap:

- **The "preview deployment" is the worktree**, not a Vercel build reached over
  HTTP by a browser. Closing it needs a reachable Vercel API and production host.

Until that is closed, treat "JARVIS repairs code with a real coding agent" as
demonstrated, and "JARVIS verifies against a real preview deployment" as not.

---

## 17. Activation runbook

The order matters. Each step is cheap to undo and proves one thing.

### Step 1 — credentials, contacting nothing

```bash
jarvis reliability doctor
```

Reports every credential **by variable name**, never by value:
`CONFIGURED` · `MISSING` · `INVALID` · `BLOCKED` · `UNKNOWN`.

Least privilege, and nothing more:

| Integration | Grant | Never grant |
|---|---|---|
| GitHub (monitoring) | `Contents: Read`, `Pull requests: Read`, `Actions: Read`, `Metadata: Read` | anything write |
| GitHub (repair only) | additionally `Contents: Write`, `Pull requests: Write` | Administration, secrets, branch protection |
| Vercel | read-only access token | project settings, env-var writes |
| Supabase | Management API, read | **the `service_role` key, ever** |

Write access is checked by **reading the repository's reported permissions**, not
by attempting a write. A capability probe that creates a branch to find out
whether it can create branches is not a probe.

While repair is disabled, write access is reported as *not required* — a fact,
not a blind spot, so it does not drag the integration to `DEGRADED`.

### Step 2 — the coding agent, before any real target

```bash
which claude && claude --version
uv run pytest -m live_claude
```

This drives the **real** `claude` CLI against a throwaway repository with a
deterministic bug. It proves the agent runs, that JARVIS reads the diff from git
rather than from the agent's account, that the guards fire on unsafe output, and
that only verification can resolve an incident. No production system is involved.

### Step 3 — read-only against the real target

```bash
jarvis reliability live-diagnostic
```

Exit `0` healthy · `1` failed or degraded · `2` incomplete. A run that checked
nothing never exits `0`.

### Step 4 — one probe

Start with the homepage. Selectors must come from markup you have actually
looked at — the shipped example specs are detected as placeholders and refused,
so a probe with invented selectors can never report `PASS`.

### Step 5 — supervised watch

```bash
jarvis reliability watch --once   # one pass
jarvis reliability watch          # continuous, repair still off
```

### Step 6 — arm repair

Only after steps 1–5. See [`JARVIS_REPAIR_LOOP.md`](JARVIS_REPAIR_LOOP.md) §12.
`watch` refuses to start (exit 2) if repair is combined with any production
reach.

### Running under systemd

`deploy/systemd/jarvis-reliability.service`:

```bash
sudo cp deploy/systemd/jarvis-reliability.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jarvis-reliability
journalctl -u jarvis-reliability -f
```

Three deliberate choices in that unit:

- `ExecStartPre` runs `doctor`, so a bad configuration fails at boot rather than
  at 3am.
- `Restart=on-abnormal`, not `on-failure`. Exit 2 (unsafe configuration) and
  exit 3 (emergency stop) are decisions, not crashes; restarting would fight the
  operator.
- Nothing resumes an interrupted repair. On restart, incidents left in
  `FIXING`/`TESTING`/`VERIFYING` go to `RECOVERY_REQUIRED`.

**Not Linux?** Use the platform's own supervisor — `launchd` on macOS (see
`deploy/launchd/`), or a Windows service wrapper. The three properties above are
what any of them must preserve; the unit file is not special.

### Emergency stop, verified

```bash
jarvis reliability stop
jarvis reliability watch     # exits 3, refuses to start
```

The stop is a **file**, so it survives a reboot and a `systemctl restart`. That
is the point: a stop pulled in a panic must not be undone by a host reboot.

---

## 18. Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| Exit 2, "refuses to start" | Repair is combined with a production reach | Read the listed problems; each names both flags |
| Exit 3, "JARVIS is stopped" | An emergency stop is in effect | Remove the `STOPPED` file |
| Incidents in `RECOVERY_REQUIRED` | JARVIS died mid-repair | Inspect the worktree and branch, then `jarvis reliability repair <id>` |
| A check escalates as flapping repeatedly | The check is genuinely intermittent | Fix the check, or raise `failure_threshold` |
| Repairs are deferred | Concurrency or a cooldown | `jarvis reliability incidents`; a pending PR holds for an hour by design |
| One outage, many incidents | The fingerprint is unstable | Check the probe's error text for unnormalised variable content |
| No repair ever starts | `[reliability.repair] enabled = false`, or severity not allowlisted | `CRITICAL` is excluded from auto-repair by default |
