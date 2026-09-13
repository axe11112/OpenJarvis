# Taking this branch to the dedicated Mac

Nothing in this document has been performed. It was written in a cloud
session that never touched the production Mac, never read `~/.openjarvis`
there, never held a production credential, and never contacted Telegram,
Supabase, Vercel or the live GitHub tokens. Every command below is one the
**owner** runs, on that machine, in this order, reading the output before
moving to the next.

It contains no secrets and asks for none. Where a secret is involved, the
step names the file to edit and stops.

---

## What actually changes on that machine

Read this before the steps. Two of these change behaviour the machine has
today, and one of those two is the reason for the preflight in step 9.

| # | Change | What it costs on the Mac |
|---|--------|--------------------------|
| 1 | **Local gates get a named environment.** A check no longer inherits this process's environment. | **The most likely thing to break.** A `npm test` or `next build` that silently depended on an inherited variable now fails. Step 9 finds this before the watcher runs; step 10 fixes it. |
| 2 | **A feature run can be refused.** `run()` claims the machine first, and refuses while a production change is in flight or the one code slot is busy. | A request can now come back having done nothing, journalled `feature.queue_deferred`. Nothing retries it automatically — it is re-run by asking again. |
| 3 | **A HIGH-risk merge needs a bound approval.** The bare "operator approved" boolean no longer merges one. | Approving a HIGH-risk ship is now `jarvis wiz approve-ship <id> --reason "..."`, once per commit. The Control Center's own Ship button mints the approval itself and is unaffected. |
| 4 | **Repair cooldowns survive a restart**, and repair admission is shared between processes. | Restarting the watcher no longer clears a pending-pull-request cooldown. That was never a supported way to clear one; `Fix it` from the phone still is. |
| 5 | **Repair admission keeps state on disk**, in `admission/` beside the incident database. | New directory. Nothing to migrate; it is created on first use. |
| 6 | **Two ledgers gain a `.lock` sibling and are written atomically.** | New files beside `telegram_seen.json` and `feature_notify_ledger.json`. Both formats stay readable by the current code (see Rollback). |
| 7 | **The seen-message ledger is bounded** to the newest 2000 entries. | An existing larger file is trimmed on its first write. Telegram stops redelivering after a day, so nothing that could still arrive is forgotten. |
| 8 | **The incident store gains a `version` column** for compare-and-swap. | Applied on first open. Additive, so the current code still reads the migrated file. |
| 9 | **`jarvis reliability resume` exists**, and `jarvis wiz check-env`, `jarvis wiz approve-ship`. | New read-only or explicitly-confirmed commands. Lifting an emergency stop no longer means composing an `rm` against a path you worked out. |

---

## The steps

### 1. Confirm the starting point

On the Mac, with nothing changed yet:

```
jarvis reliability status
jarvis wiz list
jarvis wiz status
```

All read-only. Write down: how many incidents are open, whether any feature
is in `FIXING`, `TESTING`, `VERIFYING`, `MERGING`, `DEPLOYING` or `MERGED`,
and whether the emergency stop is already engaged. If a feature is in
`MERGING` or `MERGED`, **stop here** and let it finish or resolve it first —
step 3 will not interrupt it safely, and neither will anything else.

### 2. Pick a window when production is quiet

Not a formality. Steps 3–13 leave the site unmonitored, and the only honest
way to do that is deliberately, while nothing is wrong. If an incident is
open now, close it out first.

### 3. Engage the emergency stop

```
jarvis reliability stop
jarvis reliability status
```

This blocks new repairs and new monitoring cycles. It only ever removes
capability, which is why it is the first thing that changes. Confirm the
status output says it is engaged before continuing.

### 4. Wait for what is in flight to finish

Re-run `jarvis reliability status` and `jarvis wiz list` until nothing is in
a working state. Then take the supervised watcher down:

```
jarvis reliability service status
jarvis reliability service uninstall
jarvis reliability service status
```

`uninstall` unloads the LaunchAgent and removes its plist; it deliberately
keeps the environment file, which holds credentials. There is no
`service stop`, and that is on purpose — the supervisor has no stop, because
stopping JARVIS is what step 3's audited emergency stop is for. This step is
not about stopping JARVIS working; it is about not swapping the code under a
live Python process. Step 11 puts the LaunchAgent back.

Do not skip this while the watcher is running. Two processes, one on the old
code and one on the new, are exactly the pair the cross-process leases added
on this branch are built to survive — but surviving that is not a reason to
create it on purpose.

### 5. Back up the state directory

```
tar -czf ~/openjarvis-backup-$(date +%Y%m%d-%H%M).tgz -C ~ .openjarvis
ls -lh ~/openjarvis-backup-*.tgz
```

This is the rollback for steps 7 and 8 of the table above. Keep it until the
machine has run for a week on the new code. It contains configuration and
credentials — treat it like a credential: keep it on this machine, do not
copy it anywhere, and delete it when it is no longer needed.

### 6. Fetch the branch into a scratch checkout

Not into the installed one. The scratch checkout is where the suite runs and
where the preflights run, so that nothing installed changes until step 11.

```
git -C /tmp clone --branch claude/opus5-ultracode-hardening-vjcihx \
    <this repository> openjarvis-candidate
cd /tmp/openjarvis-candidate
git log --oneline -20
```

Read the commit list. Every commit on this branch is one of the changes in
the table; if there is one you do not recognise, resolve that before
installing it.

### 7. Run the suite on the Mac

```
cd /tmp/openjarvis-candidate
uv sync
uv run maturin develop      # the Rust extension is mandatory; without it ~70 tests fail
uv run pytest -q
```

Expected: everything passes except tests that need a live network, plus the
three in `tests/wiz/test_telegram_engineering_target.py` **if** the scratch
checkout has no engineering target configured. Those three assert the real
machine's configuration, so on the Mac, with `~/.openjarvis` present, they
should pass — and if they fail there, that is a real finding about the
configuration, not an environment artefact. Do not continue past a failure
you cannot explain.

### 8. Preflight: authority

```
cd /tmp/openjarvis-candidate
uv run jarvis wiz authority
```

Read-only: it loads the authority policy and nothing else — no feature
store, no checkout, no network — and prints no secrets, because the policy
holds none. It answers whether `control_center` actually has `CODE_WRITE`
and `PRODUCTION_CHANGE` on this machine, and confirms the ceilings Telegram
and Voice can never exceed.

If it reports that Telegram or Voice is *not* structurally barred from
changing production, **stop**. That is a weakened ceiling, and nothing below
is safe until it is understood.

This command will not edit `authority.json`, and neither should anything
else: widening authority is a separate decision, made on purpose, not part
of an upgrade.

### 9. Preflight: what the gates will see

```
cd /tmp/openjarvis-candidate
uv run jarvis wiz check-env
```

This is the step that exists because of change #1. It runs no check, creates
no worktree, touches no network, and prints names only — never values. For
each of the four gates it reports what would be inherited and what would be
withheld.

Read the withheld list against what the build actually needs. The question
for each name is not "is it a secret" but "does `npm run build` read it". A
private registry token, a project-specific flag a script branches on, a
proxy setting a corporate network needs — those are the candidates.

### 10. Name what the build genuinely needs

Only if step 9 found something. Edit, by hand, on the Mac:

`~/.openjarvis/wiz/wiz.json` → the target's `check_env_pass_through`, a list
of variable **names**:

```json
"check_env_pass_through": ["NPM_TOKEN"]
```

Names, never values: the value still comes from the machine's environment.
Add only what the build demonstrably needs. Re-run step 9 and confirm each
name has moved from `withheld` to `inherited`.

Nothing in this repository will edit that file for you, and nothing should:
which of this machine's secrets a build may see is the owner's decision, and
the whole point of change #1 is that it is now made in writing rather than
by proximity.

### 11. Install the new code

With the LaunchAgent uninstalled and the emergency stop still engaged. Update
the installed checkout the way this machine is normally updated — fetch the
branch, check it out, `uv sync`, `uv run maturin develop`. Do not merge it
into the canonical engineering branch as part of this; that is a separate
decision, made after the machine has run on it.

Then put the LaunchAgent back, but do not start anything yet:

```
jarvis reliability service install --working-directory <the installed checkout> --no-load
jarvis reliability service status
```

`install` captures the credential environment variables named by the
configuration into a 0600 file the wrapper sources — which is precisely why
change #1 exists: that is the environment a check used to inherit in full.

### 12. Smoke test, still stopped

```
jarvis reliability doctor
jarvis wiz doctor
jarvis wiz status
jarvis wiz check-env
jarvis wiz authority
jarvis reliability verify-audit
```

Then one real, read-only probe run:

```
jarvis reliability probe list
jarvis reliability probe run <name>
```

`verify-audit` matters here specifically: it checks the incident transition
log's hash chain across the store migration in change #8. A chain that
verifies after the migration is the evidence that the migration did not
disturb history.

Everything in this step is read-only except the probe, which only observes
the site. The emergency stop is still engaged, so nothing can repair,
merge, or deploy even if a probe finds something.

### 13. Lift the stop and start the watcher

In this order:

```
jarvis reliability resume        # asks before it lifts; there is no flag to skip the question
jarvis reliability status
jarvis reliability service start
jarvis reliability service status
```

`resume` only removes the stop. It reopens no incident, clears no cooldown,
starts no repair and launches nothing — which is why the watcher is started
separately, on the line after. The supervisor refuses to start while a stop
is engaged, so the two must happen in this order, and the refusal is the
safe direction: it is the stop doing its job.

Then watch one full check interval. Look for:

- the startup banner naming the same configuration it named before,
- a check cycle completing,
- no `feature.queue_deferred` for a feature you expected to run,
- no gate failing with the "variables were not inherited" note that change #1
  adds to a failed check's output. That note is the signal that step 10 is
  not finished — it names a count and points at `check_env_pass_through`.

### 14. The first day

Ask three questions, in this order, and use the ordinary read-only commands:

1. **Did anything get told to a person twice?** Change #6 touches both
   notification ledgers. A duplicate is the documented failure mode
   (delivery is at-least-once on purpose); a *flood* of duplicates is not,
   and means a ledger is not being written. Check for a
   `feature_notify_ledger.json.corrupt` or the seen ledger being rewritten
   from empty.
2. **Did a repair start that should not have?** `jarvis reliability incidents`.
   Change #4 makes cooldowns durable, so the shape to look for is the
   opposite: a repair that should have started and did not, because a
   cooldown from before the restart is still in force. That is correct
   behaviour, and it is cleared with `Fix it` from the phone, not by
   restarting and not by `resume` — `resume` lifts the emergency stop and
   nothing else.
3. **Is anything stuck in `RECEIVED` that you asked for?** Change #2 means a
   deferred run leaves it there with a journalled reason. `jarvis wiz show
   <id>` names the reason; asking again runs it.

---

## Rollback

Per change, because they do not roll back together:

- **Code** (changes #1, #2, #3): check out the previous revision and
  reinstall. Nothing on disk needs reverting for these three.
- **`admission/` and the `.lock` files** (#5, #6): harmless to leave. The
  previous code ignores them.
- **`telegram_seen.json`** (#6, #7): the new format is a list of
  `[chat, message_id, when]`; the previous code reads the first two elements
  and ignores the third, so no revert is needed. What does not roll back is
  the trim in #7 — entries beyond the newest 2000 are gone. They were older
  than anything Telegram will redeliver, and the backup from step 5 has them.
- **`feature_notify_ledger.json`** (#6): gains an `at` field per entry, which
  the previous code ignores. No revert needed.
- **The incident store** (#8): the added column is additive and the previous
  code does not read it. If the store must be restored anyway, it is in the
  step 5 backup. **Restoring it loses every incident recorded since**, so
  restore only for a real corruption, never as tidying.

A rollback that needs the backup is a rollback that needs the watcher
stopped first — steps 3 and 4 again, in that order.

---

## What this plan does not authorise

Named because the boundary matters more than the convenience:

- **No widening of authority.** Not `authority.json`, not a channel ceiling,
  not a risk threshold, not `merge_low_risk`. If this upgrade seems to need
  one, it is the wrong upgrade.
- **No merge of this branch into the canonical engineering branch.** That is
  a decision to make after the machine has run on it, not a step of
  installing it.
- **No `service_role` anywhere.** Nothing here needs it and nothing here
  should be given it.
- **No editing of a COMPLETE feature's state** to make it look consistent
  with the new code. `jarvis wiz reconcile` exists for a feature whose real
  outcome is genuinely unknown; it is not a way to tidy history.
- **No skipping step 9.** It is the only step that turns change #1 from a
  surprise into a task.

---

## What was proved, and where

Every change above has tests that fail without it. `docs/WIZ_SAFETY_INVARIANTS.md`
names each invariant, the code that enforces it, and the test that fails if the
enforcement is removed — including §19, which is the list of things that are
still *not* enforced, and §20, which is the list of things no cloud session can
prove at all: launchd supervision, real Telegram delivery, real Vercel
deployments, real GitHub merge permissions, real Supabase, and anything about
this machine's own `authority.json`.

Step 8 and step 9 exist because those two are the ones a person can check on
the machine in a minute, before trusting anything else.
