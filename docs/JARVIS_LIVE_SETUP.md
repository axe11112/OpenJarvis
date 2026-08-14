# JARVIS Live Setup

How to point JARVIS at a real application, read-only, and prove it works.

**Everything in this phase is read-only.** JARVIS will not modify code, open
pull requests, deploy, roll back, or write to your database. Those are separate
opt-in flags, all off by default, and the diagnostic prints their state on every
run so you can confirm it.

Companion documents: [`JARVIS_ARCHITECTURE.md`](JARVIS_ARCHITECTURE.md) ·
[`JARVIS_SECURITY.md`](JARVIS_SECURITY.md) · [`JARVIS_ROADMAP.md`](JARVIS_ROADMAP.md)

---

## 1. What you need

| Thing | Why | Where it goes |
|---|---|---|
| Repository (`owner/name`) | Correlating failures with commits | `TARGET_REPOSITORY` |
| Production URL | Browser and HTTP probes | `TARGET_PRODUCTION_URL` |
| Vercel project + team id | Deployment state | `VERCEL_PROJECT`, `VERCEL_TEAM` |
| Supabase project ref | Backend health | `SUPABASE_PROJECT_REF` |
| GitHub read token | Commits, PRs, Actions | `GITHUB_READONLY_TOKEN` |
| Vercel read token | Deployments, build logs | `VERCEL_READONLY_TOKEN` |
| Supabase read token | Project health, logs | `SUPABASE_READONLY_TOKEN` |
| Telegram bot token | Notifications (optional) | `TELEGRAM_BOT_TOKEN` |
| Test account | Authenticated probes (optional) | `JARVIS_TEST_EMAIL`, `JARVIS_TEST_PASSWORD` |

Identifiers may also live in `~/.openjarvis/config.toml`; environment variables
win, so you can run a one-off diagnostic against staging without editing config.

**Credentials only ever live in environment variables.** Nothing in this
document, in `config.toml`, or in any JARVIS record holds a secret value —
config stores the *name* of the variable to read.

---

## 2. Configuration

```toml
[reliability]
enabled = true

[reliability.site]
base_url = "https://www.example.com"
environment = "production"

[reliability.github]
enabled = true
repo = "owner/name"
base_branch = "main"
token_env = "GITHUB_READONLY_TOKEN"

[reliability.vercel]
enabled = true
project_id = "prj_..."
team_id = "team_..."
token_env = "VERCEL_READONLY_TOKEN"

[reliability.supabase]
enabled = true
project_ref = "abcdefgh"
token_env = "SUPABASE_READONLY_TOKEN"
allow_production_writes = false     # leave this false

[reliability.probes]
directory = "~/.openjarvis/reliability/probes"
evidence_dir = "~/.openjarvis/reliability/evidence"

[reliability.repair]
enabled = false                     # leave this false for now

[reliability.policy]
deploy_mode = "pr_only"
allow_push_to_default_branch = false
```

---

## 3. Permissions — what each token may do

Grant the **minimum**. JARVIS degrades gracefully when a scope is missing: it
reports that capability as `UNKNOWN` rather than failing or pretending.

### GitHub

A fine-grained personal access token scoped to the **one** repository:

| Permission | Needed for | Required? |
|---|---|---|
| `Contents: Read` | Commits, changed files, branches | yes |
| `Pull requests: Read` | Correlating failures with PRs | yes |
| `Actions: Read` | CI status and failed workflows | recommended |
| `Metadata: Read` | Repository reachability | yes |

Without `Actions: Read` the diagnostic reports
`github DEGRADED — not verified: actions`. That is correct behaviour, not a bug.

**Do not grant write permissions during this phase.** JARVIS has no code path
that writes without `[reliability.repair] enabled = true`, and there is no
method anywhere in the codebase that merges a pull request.

### Vercel

A read-only access token. JARVIS reads deployments, their state, and build
logs. It enumerates environment-variable **names** only — there is deliberately
no method on `VercelSource` that returns a value, so a leaked JARVIS token
cannot be used to harvest your application's secrets through it.

JARVIS never triggers, promotes, cancels or deletes a deployment.

### Supabase

A Management API token with read access. JARVIS reads project status,
migrations, Edge Functions and logs.

**Never give JARVIS the `service_role` key.** For database reads, use a role
restricted to `SELECT` on the schemas needed for diagnostics.

The SQL guard refuses `DROP`, `TRUNCATE`, `DELETE`/`UPDATE` without a `WHERE`,
row-level-security changes, `GRANT`/`REVOKE`, role changes, the `auth` schema
and vault secrets — **regardless of configuration**. There is no flag that
permits them.

### Telegram

Create a bot with `@BotFather`, then set `TELEGRAM_BOT_TOKEN` and
`[channel.telegram] allowed_chat_ids` to your own chat id. Messages pass
through the outbound redaction guard before they are sent.

---

## 4. Playwright

```bash
uv sync --extra browser
uv run playwright install chromium
```

If your image ships a pinned browser, point JARVIS at it instead:

```toml
[reliability.probes]
browser_executable_path = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
```

or set `JARVIS_BROWSER_EXECUTABLE`.

---

## 5. Test accounts

Authenticated probes need a **dedicated, non-privileged account**. Never the
owner's account.

```bash
export JARVIS_TEST_EMAIL='jarvis-probe+prod@yourdomain.example'
export JARVIS_TEST_PASSWORD='...'
```

Give it a name that makes its rows obvious in production data. The password is
resolved inside the probe runner, is scrubbed from every captured string by the
credential redactor, and has no field in `Evidence` capable of holding it.

If no test account exists, JARVIS reports authenticated testing as **untested**
rather than passing.

---

## 6. Probes

The specs in `configs/reliability/probes/` are **examples with invented
selectors**. JARVIS detects them and refuses to run them, reporting
`NOT_CONFIGURED` — a placeholder probe can never report `PASS`.

To write a real probe, copy one and point it at markup you have actually
looked at. Document, in the spec:

- purpose and component
- URL and actions
- expected result
- severity
- whether it is safe against production
- whether it requires authentication
- **whether it creates or modifies data** — set `mutating = true`, which keeps
  it disabled until you opt in

Start with unauthenticated read-only probes: the homepage and a health
endpoint. Add authenticated workflows only once a test account exists.

---

## 7. Running it

### Validate configuration — contacts nothing

```bash
jarvis reliability doctor
```

Reports each credential as configured or missing **by variable name**. Exits
non-zero if anything required is absent. Add `--json` for machine-readable
output.

### Full read-only diagnostic

```bash
jarvis reliability live-diagnostic
```

Runs, in order: configuration → GitHub → Vercel → Supabase → website → probes →
notifications → coding agent. Then aggregates, opens incidents for genuine
failures only, and verifies the audit chain.

| Flag | Effect |
|---|---|
| `--no-probes` | Integrations only, no browser |
| `--no-incidents` | Report only; never write an incident |
| `--notify` | Send a Telegram summary |
| `--json` | Machine-readable output |

**Exit codes:** `0` healthy · `1` something failed or is degraded ·
`2` incomplete. A run that checked nothing never exits `0`.

### Other commands

```bash
jarvis reliability notify-test            # one test message
jarvis reliability probe list             # what is configured
jarvis reliability probe run <id>         # run one probe now
jarvis reliability incident show <id>     # full incident with history
jarvis reliability analyze <id>           # diagnostic-only Claude prompt
jarvis reliability verify-audit           # check the hash chain
```

---

## 8. Health states — reading the output

JARVIS distinguishes six states, because "we checked and it is fine" must never
be confused with "we could not check":

| State | Meaning |
|---|---|
| 🟢 `HEALTHY` | The check ran and passed |
| 🟡 `DEGRADED` | Ran; some capabilities work, some do not |
| 🔴 `FAILED` | Ran, and the thing is broken |
| ⚪ `UNKNOWN` | Attempted, no verdict — a 403, a timeout, a missing scope |
| ⚫ `NOT_CONFIGURED` | No credentials or identifiers; nothing attempted |
| ⚫ `NOT_CHECKED` | Deliberately skipped |

Only `FAILED` opens an incident. A missing token is JARVIS's problem, not your
site's, and never produces an incident claiming production is broken.

Every run ends with a **"Not verified"** list. Read it: those are blind spots,
not passes.

---

## 9. What JARVIS can and cannot do

**Can (this phase):** read commits/PRs/Actions; read deployments and build
logs; read Supabase project state, migrations, functions and logs; fetch the
production URL; run browser probes; open incidents; send notifications; build a
read-only analysis prompt.

**Cannot (this phase, by configuration):** modify code, create branches, open or
merge pull requests, deploy, promote, roll back, or write to the database.

**Cannot, ever, by construction:** merge a pull request (no such method
exists); `DROP`/`TRUNCATE`/disable RLS/`GRANT` (refused by the guard regardless
of configuration); read a Vercel environment-variable value (no such method);
modify `.github/workflows/**` (refused by the path guard).

---

## 10. Turning JARVIS off

```toml
[reliability]
enabled = false
```

Or stop the process: JARVIS only acts while `jarvis reliability watch` or the
daemon is running. `live-diagnostic` and `doctor` are one-shot and exit.

To revoke access entirely, delete the tokens at the provider. JARVIS holds no
copy — it reads environment variables at call time.

---

## 11. Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| `github DEGRADED — not verified: actions` | Token lacks `Actions: Read` | Add the scope, or accept CI as unmonitored |
| `MissingTokenError: $X is not set` | Variable absent from the process | Export it in the shell that runs JARVIS |
| `ProxyError: 403 Forbidden` | Your network blocks the host | Run from a network that can reach it; this is not a site failure and JARVIS will not open an incident for it |
| `no log lines were sampled` | Log query returned nothing | Check the Supabase token scope; "0 problems" from an unreadable log is reported as `UNKNOWN`, not healthy |
| `placeholder probe, not run` | Selectors are still the examples | Point them at your real markup |
| `no production deployment found` | Deployment list empty or unreadable | Check `VERCEL_PROJECT`/`VERCEL_TEAM` and the token |
| Everything `NOT_CONFIGURED` | No credentials in this shell | `jarvis reliability doctor` names each missing variable |
| Audit chain `BROKEN` | The incident history was modified | Treat as a serious internal problem; preserve the database and investigate |

---

## 12. Before you enable repair

Do not set `[reliability.repair] enabled = true` until:

- [ ] `live-diagnostic` exits `0`, or its blind spots are ones you accept
- [ ] Real probes exist and pass against production
- [ ] `jarvis reliability analyze <id>` has produced a diagnosis you judge to be
      accurate on a real incident
- [ ] `[reliability.repair] workspace` points at a checkout of the target
- [ ] `test_command` runs the target's own suite
- [ ] `deploy_mode` is `pr_only` and `allow_push_to_default_branch` is `false`

The repair loop is implemented and tested but has never been run against a live
Claude Code session. Enabling it is a separate, deliberate decision.
