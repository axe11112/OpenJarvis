# JARVIS Repair Loop

How JARVIS takes a detected production failure, gives a coding agent the right
context, fixes it in isolation, proves the fix works, and opens a pull request —
without touching production.

> **The repair loop is disabled by default and is not enabled in this
> repository.** Section 12 gives the exact change required to turn it on. It has
> been proven end to end against a controlled fixture repository; it has *not*
> been run against live infrastructure. Section 13 is explicit about what that
> leaves unproven.

Companion documents: [`JARVIS_RELIABILITY.md`](JARVIS_RELIABILITY.md) (the 24/7
loop that drives this one) · [`JARVIS_ARCHITECTURE.md`](JARVIS_ARCHITECTURE.md) ·
[`JARVIS_SECURITY.md`](JARVIS_SECURITY.md) ·
[`JARVIS_LIVE_SETUP.md`](JARVIS_LIVE_SETUP.md)

---

## 1. The one rule

JARVIS is not successful because the coding agent says "Fixed."
JARVIS is successful only when JARVIS itself proves "Fixed."

Everything below follows from that. The agent's own account of its work is
recorded as an *assertion* and given authority nowhere: not over the state
machine, not over the pull request, not over the diff. The only thing that can
move an incident to `RESOLVED` is re-running the reproduction that opened it and
watching it pass.

---

## 2. Architecture

```
                    ┌──────────────┐
   incident ───────▶│  RepairLoop  │
                    └──────┬───────┘
                           │
   ┌───────────────────────┼────────────────────────────┐
   ▼                       ▼                            ▼
briefing.py           workspace.py                  policy.py
sanitize + fence      isolated git worktree         gates, all default closed
   │                       │
   │                       ▼
   │                  code_agent.py ── claude CLI, headless, scrubbed env
   │                       │
   │                       ▼
   │                   scope.py ──── blast-radius guard → HUMAN_REQUIRED
   │                       │
   │                       ▼
   │                  checks.py ──── lint · typecheck · tests · build
   │                       │
   │                       ▼
   │                commit + push incident branch
   │                       │
   │                       ▼
   │                 preview deployment (Vercel)
   │                       │
   ▼                       ▼
                     verify.py ◀── re-runs the ORIGINAL probe spec
                           │
              ┌────────────┴─────────────┐
         passed                      failed
              │                          │
              ▼                          ▼
      github.create_pull_request    retry with evidence
      (never merged, never          → after max_attempts:
       deployed)                       HUMAN_REQUIRED
```

Modules, and what is new in Phase 12:

| Module | Role | Phase |
|---|---|---|
| `repair.py` | Orchestrates the loop | 6, rewired in 12 |
| `briefing.py` | Redaction, injection fencing, task text | 6 |
| `code_agent.py` | Drives the `claude` CLI headlessly | 6, hardened in 12 |
| `verify.py` | Independent verification | 6 |
| `policy.py` | Safety gates | 6 |
| **`workspace.py`** | **Isolated git worktrees** | **12** |
| **`scope.py`** | **Blast-radius control** | **12** |
| **`checks.py`** | **Lint, typecheck, tests, build** | **12** |

---

## 3. Worktree isolation

The coding agent never sees the operator's checkout. Every attempt gets its own
git worktree:

```
<repair.workspace>                    the checkout JARVIS cuts from — read-only
  └── .git/worktrees/…
<repair.worktree_root>/INC-00042/     the agent's sandbox
                                      branch jarvis/incident-INC-00042
                                      forked from a recorded base commit
```

A worktree rather than a clone because it shares the object database: creating
one is nearly free and needs no network round trip, which matters when the loop
may make three attempts. Not a bare directory, because the agent needs real git
history to diagnose a regression and JARVIS needs a real diff to audit.

The base ref is resolved to an immutable SHA **before** the agent runs. Branching
from `main` twenty minutes apart can otherwise mean two different trees, and the
audit log would not be able to say which one a repair was based on.

Each attempt starts from a **fresh** worktree at the base commit, not from the
previous attempt's output. Attempt two is a new try informed by evidence, not an
edit on top of a failed change.

**Cleanup:** a verified repair's worktree is removed. A failed one is kept
(`keep_failed_worktrees = true`) so a human can see exactly what the agent did.

---

## 4. Branch naming and provenance

```
jarvis/incident-INC-00042
```

The prefix comes from `[reliability.github] branch_prefix`. `main` — or whatever
the default branch is — is never used as a repair branch, and cannot be: see §7.

Recorded in the audit log for every attempt: base commit (full SHA), base ref,
branch, worktree path, creation timestamp, repair commit SHA, preview URL, check
results, scope verdict, and the briefing hash. The briefing *hash* rather than
its text — the hash proves which brief was sent without copying possibly
sensitive content into a second store.

---

## 5. What the agent may and may not do

**May:** read files, modify application code, create tests, run tests, run
linters, run type checkers, run builds, inspect git history — all inside its
worktree.

**May not:**

| Prohibition | How it is enforced |
|---|---|
| Push to the default branch | `SafetyPolicy.may_push_to`, plus a branch-prefix check in `RepairWorkspace.push` |
| Merge a pull request | No such method exists anywhere in the codebase |
| Deploy to production | `deploy_mode = "pr_only"`; `may_deploy` refuses |
| Write to the production database | Supabase writes gated; SQL guard unconditional |
| Modify CI configuration | `ALWAYS_PROTECTED_PATHS`, enforced regardless of config |
| Reach the network | `WebFetch`/`WebSearch` in `agent_disallowed_tools` |
| See JARVIS's credentials | Environment scrubbed before the subprocess starts |
| Touch anything outside the worktree | Paths that escape the repository are protected unconditionally |
| Force-push | `push()` never passes `--force` |

### How the CLI is invoked

```
claude -p "<briefing text>" \
       --output-format text \
       --allowedTools Read,Edit,Write,Grep,Glob,Bash \
       --disallowedTools WebFetch,WebSearch
```

Run with `cwd` set to the worktree, with a scrubbed environment, and **as an
argv list — never through a shell**. The briefing contains attacker-influenceable
evidence text; passing it as an argument means no punctuation in it can become a
command.

`Bash` is allowed because a repair that cannot run the project's tests is not
worth much. The worktree is the blast radius, and §6 checks what came out.

---

## 6. Change-scope control

The protected-path guard asks "may this file be touched at all?". Scope control
asks a looser question: **"is this diff the shape of the repair we asked for?"**

An agent asked to fix a login redirect that returns a 400-file diff has not
necessarily touched anything forbidden. It has done something nobody intended,
and the right response is to fetch a human.

Assessed **before anything is committed or pushed**:

| Category | Examples | Result |
|---|---|---|
| Protected | `.github/workflows/**` | **stop** |
| Credential-bearing | `.env`, `*.pem`, `.npmrc`, `**/credentials.*` | **stop** |
| Infrastructure | `Dockerfile`, `docker-compose.yml`, `vercel.json`, `*.tf` | **stop** |
| Declarative security | RLS, `middleware.*`, `**/migrations/**`, `*.sql` | **stop** |
| Outside declared scope | anything not matching `expected_paths` | **stop** |
| Too large | `> max_changed_files` or `> max_changed_lines` | **stop** |
| Security-adjacent | `**/auth/**`, `**/*session*`, `**/*role*` | flag, continue |

**Why application auth code only gets flagged.** Blocking every file whose name
contains "auth" would make JARVIS unable to repair a login failure — the exact
incident class it was built for. Those changes are surfaced prominently in the
pull request, and are separately barred from automatic deployment by
`SafetyPolicy.SECURITY_SENSITIVE`. They do not abort the repair.

Every category is evaluated even after the first failure, so one escalation tells
the owner everything that is wrong rather than revealing problems one at a time.

### Path matching

Paths are normalized before any comparison: separators unified, `.` and `..`
resolved, lower-cased. Resolution is *textual*, not filesystem-based, so the
guard gives the same answer for a file that does not exist yet as for one that
does, and cannot be steered by a symlink planted in the worktree. All of these
are caught as the same protected file:

```
.github/workflows/ci.yml    ./.github/workflows/ci.yml
.github\workflows\ci.yml    a/../.github/workflows/ci.yml
.GITHUB/WORKFLOWS/CI.YML    .github/./workflows/ci.yml
```

Absolute paths and paths climbing above the repository root (`/etc/passwd`,
`../../.ssh/config`) are protected unconditionally — no pattern list makes
writing to them a legitimate repair.

---

## 7. Local checks

Run inside the worktree, cheapest first, so the feedback the agent receives names
the most fundamental problem rather than its downstream consequences:

| Check | Required? | Rationale |
|---|---|---|
| `lint_command` | advisory | A style violation is a poor reason to leave production broken |
| `typecheck_command` | required | |
| `test_command` | required | |
| `build_command` | required | A change that does not build is not shippable |

**A check that is not configured is reported as not run, never as passed.** "We
have no type checker" and "the types are fine" are different facts, and only one
of them is a reason to open a pull request.

A required failure stops the attempt before the branch is pushed — a change that
fails its own tests should never consume a build. The failure output is captured
and fed into the next attempt's briefing.

---

## 8. Preview deployment

Only after local checks pass does JARVIS commit, push the incident branch, and
wait for a Vercel preview (`preview_wait_seconds`). Verification runs against
that preview, never against production.

If no preview appears, JARVIS fetches the build logs and attaches them as
evidence, so the next attempt learns *why* the build failed rather than only
that verification did not happen. A missing preview is never treated as a pass:
`verify()` returns `passed=False` with "a repair that cannot be verified is not a
verified repair."

---

## 9. Independent verification

The centre of the system. `Verifier` re-runs **the exact probe spec that opened
the incident** against the preview and compares observed behaviour with the
spec's declared expectations.

No model is consulted, and no model output can influence the verdict — which is
also why injected content cannot talk its way to `RESOLVED`.

Generic smoke tests are not verification. If the incident was "login does not
reach the dashboard", verification performs the login and checks the dashboard.
The original failure must demonstrably stop reproducing.

An incident may reach `RESOLVED` only when all of these hold:

- the diff was within scope
- required local checks passed
- a preview deployment existed
- the original reproduction passed against it

Otherwise: **not resolved.**

---

## 10. Retry loop and states

```
DETECTED → INVESTIGATING → REPRODUCING → FIXING → TESTING → VERIFYING
                                            ▲                   │
                                            └─── failed ────────┘
                                                                │ passed
                                                                ▼
                                                            RESOLVED
```

`VERIFYING → RESOLVED` is the only automatic path to `RESOLVED`; the state
machine rejects anything else. After `max_attempts` (default 3) without a
verified fix, or on any hard stop, the incident goes to `HUMAN_REQUIRED`.

Attempt outcomes: `verified`, `verification_failed`, `no_diff`, `tests_failed`,
`agent_error`, `policy_denied`, `protected_path`, `scope_violation`,
`workspace_error`, `no_preview`.

Each retry's briefing carries the previous verification's evidence under
"Previous attempt failed verification", which is what makes attempt two a better
attempt rather than the same one again.

---

## 11. Secret handling

Three layers, plus one added in Phase 12:

1. **Structural.** `Evidence` has no field capable of holding a credential.
2. **Inbound redaction.** Everything entering a briefing passes the framework's
   shape-matching stripper (`ghp_`, `sk-`, `AKIA`) *and* an assignment rule for
   `DB_PASSWORD=hunter2`, which has no shape to match.
3. **Outbound scan.** The finished brief is scanned; a surviving `CRITICAL`
   finding *aborts* the briefing rather than redacting and continuing, because a
   survivor means layer 1 failed and the incident record itself is suspect.
4. **Agent output (Phase 12).** The agent's own summary is model output written
   after reading the application's source and running its tests, so it can repeat
   a credential it saw. It is redacted before being persisted, rendered into the
   pull-request body, or sent to the owner.

The agent's subprocess environment is scrubbed of JARVIS's own tokens
(`*_TOKEN`, `*SECRET*`, `*PASSWORD*`, Supabase/Vercel/Telegram variables). A
subprocess that cannot see a credential cannot leak it. `ANTHROPIC_API_KEY` and
`CLAUDE_CODE_OAUTH_TOKEN` survive, because without them the agent is inert.

### Prompt injection

Incident evidence includes page content, logs, database rows, GitHub issue text
and API responses — all written by whoever controls the monitored system, which
during an incident may be an attacker. All of it is wrapped in
`<untrusted_external_data>` with a standing instruction that fenced content is
evidence and never instruction. Fence markers inside the content are escaped, so
content cannot close its own fence and escape into the instruction context.
Text matching known injection patterns raises a visible warning in the brief.

The structural defence matters more than the instruction: **verification does not
consult a model**, so no injected text can produce a `RESOLVED` incident.

---

## 12. Configuration and manual enablement

```toml
[reliability.repair]
enabled = false                      # ← the master switch

workspace = ""                       # checkout to cut worktrees from
worktree_root = ""                   # default: <config-dir>/reliability/worktrees
keep_failed_worktrees = true

test_command = ""                    # unset = not run, never "passed"
lint_command = ""
typecheck_command = ""
build_command = ""
test_timeout_seconds = 1800

max_attempts = 3
require_preview_verification = true
preview_wait_seconds = 600
request_regression_test = true

max_changed_files = 20
max_changed_lines = 800

agent = "claude_cli"
agent_executable = "claude"
agent_allowed_tools = ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]
agent_disallowed_tools = ["WebFetch", "WebSearch"]

[reliability.policy]
deploy_mode = "pr_only"              # leave this
allow_push_to_default_branch = false # leave this
```

### To enable it

Prerequisites, all of which should be true before you flip the switch:

- [ ] `jarvis reliability live-diagnostic` exits `0`, or its blind spots are ones
      you accept
- [ ] real probes exist and pass against production
- [ ] `jarvis reliability analyze <id>` has produced a diagnosis you judge
      accurate on a real incident
- [ ] `workspace` points at a checkout of the target with a push remote
- [ ] `test_command` runs the target's own suite, and it is green at HEAD
- [ ] the `claude` CLI is installed and authenticated for the JARVIS user

Then, and only then:

```toml
[reliability.repair]
enabled = true
workspace = "/path/to/target-checkout"
test_command = "npm test"
build_command = "npm run build"
```

```bash
jarvis reliability doctor          # confirm the interlocks read as you expect
jarvis reliability watch           # start monitoring with repair armed
```

`watch` refuses to start (exit 2) if this configuration could reach production —
for instance if `allow_push_to_default_branch` or a permissive `deploy_mode` were
left on. See [`JARVIS_RELIABILITY.md`](JARVIS_RELIABILITY.md) §3.

`deploy_mode` stays `pr_only` and `allow_push_to_default_branch` stays `false`.
Enabling repair grants JARVIS the ability to *propose* a fix. It grants nothing
about shipping one.

---

## 13. What is proven, and what is not

**Proven**, by `tests/reliability/test_repair_e2e.py` against a controlled
fixture repository with real git worktrees and a real test suite:

- a coding agent can modify code in isolation and reach a pull request
- a plausible-but-wrong fix — one that passes the project's own tests — is caught
  by verification, retried three times, and ends at `HUMAN_REQUIRED`
- CI edits, credential files and runaway diffs abort the repair
- the operator's checkout and the default branch are untouched in every path

**Not proven.** The end-to-end fixture stands in for two things it does not
actually use:

- **The coding agent is scripted, not the real `claude` CLI.** `ClaudeCliAgent`
  is unit-tested (argv construction, environment scrubbing, timeout handling) but
  has never driven a live Claude Code session.
- **The "preview deployment" is the worktree itself.** Verification executes the
  repaired code and checks observed behaviour — a genuine independent check — but
  it is not a Vercel preview reached over HTTP by a browser.

Neither gap can be closed from a sandbox without network access to Vercel and an
authenticated Claude CLI. They are the first things to exercise once repair is
enabled on a real machine.

---

## 14. Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| `workspace is unset` at startup | `enabled = true` without a checkout | Set `[reliability.repair] workspace` |
| `outcome: workspace_error` | Worktree could not be created or pushed | Check the checkout is a git repo with a push remote |
| `outcome: scope_violation` | The diff was too large or touched a barred category | Read the reasons in the incident; they are all listed |
| `outcome: no_preview` | No preview deployment appeared | Check Vercel is building the branch; build logs are attached as evidence |
| `outcome: no_diff` | The agent claimed a fix and changed nothing | Usually a briefing problem — read the evidence it was given |
| `outcome: tests_failed` repeatedly | The suite is red at the base commit | Verify `test_command` passes at HEAD before blaming the agent |
| Repair never starts | `enabled = false`, or severity not allowlisted | `jarvis reliability doctor`; `CRITICAL` is excluded by default |
| Worktrees accumulating | Failed repairs are kept deliberately | Inspect, then delete; `keep_failed_worktrees = false` to disable |
| `git worktree add` fails | A stale directory from a killed process | JARVIS prunes on the next attempt; otherwise `git worktree prune` |

---

## 15. Automatic merge

Off by default, and separate from everything above. The repair loop's endpoint is
still a pull request; merging is an additional capability that must be switched
on deliberately.

```toml
[reliability.merge]
enabled = false               # the master switch
method = "squash"             # squash | merge | rebase
require_status_checks = true  # refuse if CI is red, pending, or absent
delete_branch_on_merge = false
```

### The gates

A merge happens only when **every** gate passes. They are evaluated in full
rather than short-circuiting, so the audit record shows all of them:

| Gate | Refuses when |
|---|---|
| `merge_enabled` | `[reliability.merge] enabled = false` |
| `incident_state` | The incident is HUMAN_REQUIRED, RECOVERY_REQUIRED, FAILED or ROLLED_BACK |
| `not_flapping` | The probe alternates pass/fail, so a green verification proves nothing |
| `attempt_recorded` / `verified` | Nothing was verified — **the agent's claim is never read** |
| `verified_sha_known` | The attempt recorded no commit SHA |
| `scope` / `no_protected_paths` / `no_secret_like_paths` | The recorded scope verdict refused, or the diff touched a protected or secret-like path |
| `check_lint`, `check_typecheck`, `check_tests`, `check_build` | Any local check failed **or never ran** |
| `preview_deployment` | No preview deployment was recorded for the attempt |
| `original_reproduction` | The probe that was re-run is not the one that opened the incident |
| `pr_belongs_to_incident` | The PR is not the one recorded on this incident |
| `pr_head_is_incident_branch` | The head branch is not `<branch_prefix><incident id>` |
| `pr_base_is_default_branch` | The PR targets something other than the configured base |
| `pr_open` / `pr_not_draft` | The PR is closed, already merged, or a draft |
| `no_conflicts` | GitHub reports conflicts, `blocked`, or has not decided yet |
| `head_sha_unchanged` | The PR head is not the commit that was verified |
| `base_unchanged` | The base branch moved since verification |
| `status_checks` | CI is failing, pending, reported nothing at all, or **could not be read** |

Note that lint is **advisory for opening a pull request and blocking for merging
one**. A human reviewing a PR can weigh a style violation against an outage;
nobody is going to review this one.

### The token must be able to *see* CI

`status_checks` distinguishes four failing conditions, and one of them is about
JARVIS rather than about the repository:

| `state` | Meaning |
|---|---|
| `failure` / `pending` | CI reported, and it is not green |
| `none` | Nothing reported. "No CI ran" is not "CI passed" |
| `unreadable` | **The token is not permitted to look** |

A GitHub fine-grained token needs two repository permissions beyond those the
repair loop uses, or both CI endpoints return 403:

- **Commit statuses: Read** — for `/commits/{sha}/status`
- **Checks: Read** — for `/commits/{sha}/check-runs`

Without them `combined_status` reports `unreadable` and names the missing
permissions, and the merge is refused. **Do not respond by setting
`require_status_checks = false`.** That reads a credential problem as evidence
about CI and disables a working control on the strength of a misread — measured
on a real repository whose Vercel status was green the entire time and simply
could not be seen. Grant the permission instead.

Third-party CI counts: Vercel publishes a combined-status context named `Vercel`
that goes `failure` when a preview build breaks, so a repository with no GitHub
Actions workflows can still have a meaningful `status_checks` gate.

### Time-of-check / time-of-use

`head_sha_unchanged` compares the freshly re-read PR head against the verified
commit, and the merge call then passes that same SHA to GitHub as the `sha`
parameter. GitHub refuses with 409 if the head moved in between, so the check and
the use are one server-side operation. Verifying commit A and merging commit B is
not narrowed, it is impossible.

### Audit trail

Every decision — **including every refusal** — is appended to the incident's
hash-chained transition log via `IncidentStore.record_audit`, and the full
gate-by-gate account is attached as evidence. `jarvis reliability verify-audit`
covers it. A refusal is the entry worth keeping: it is the evidence the gates are
load-bearing.

Telegram is notified immediately *before* a merge (while intervention is still
possible) and after it, whichever way it went.

---

## 16. What this deliberately still does not do

No automatic production deployment — there is no code anywhere in this codebase
that can trigger one. No automatic rollback. No voice interface, no
conversational dashboard, no SMS.

Merging is not deploying, but on a repository wired for deploy-on-merge the two
are the same event. Treat `[reliability.merge] enabled` as production authority.

The goal remains: detect a real problem, give the coding agent the right context,
fix it in isolation, prove the fix works, open a pull request. Merging that pull
request without a human is an opt-in, and deploying it is still nobody's job but
the human's.
