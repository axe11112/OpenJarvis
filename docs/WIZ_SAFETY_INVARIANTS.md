# Wiz safety invariants

The properties Wiz's autonomy rests on, what enforces each one, and what proves
it. Written to be checked rather than believed: every invariant names the code
that enforces it and the test that fails if it stops.

Wiz is deployed as **several independent OS processes against one state
directory** — a watcher, a Control Center, a CLI invocation, an owner listener,
a recovery run. Every invariant below is therefore a claim about processes, not
about threads. A guarantee that rests only on a `threading.Lock`, an in-memory
dict, a process-local cache or Python object identity is not an invariant in
this deployment; it is a coincidence that holds until the second process
starts.

Each entry is marked:

- **enforced** — a mechanism enforces it and a test fails if that mechanism goes
- **enforced, needs owner config** — enforced in code, but inert until the owner
  configures something (and the code says so out loud)
- **not verifiable here** — depends on the live Mac, real credentials or real
  infrastructure; stated so nobody mistakes silence for proof

---

## 1. Production changes are serialised across processes

**enforced.** Two subsystems can put a commit on the production default branch:
`FeaturePipeline.ship()` merges a feature's pull request, and
`AutoMerger.merge_for()` merges a repair's. Both take the one
`production_change_lease()` at the config root — a `flock`-backed
`ProcessLease`, released by the kernel the instant a holder exits, crashes or is
killed.

The lease is held across the merge **and the production observation that
interprets it**. That span is the point. Which merge wins is not the hazard; the
hazard is that each side then reads one shared production to decide whether *its
own* change is good, so an interleaved pair can each attribute the other's
deployment to itself. A wrong `COMPLETE` is worse than a refused merge, because
nothing downstream re-checks it.

A caller that cannot get the lease does not merge. It records why, names the
holder, and leaves the pull request as the deliverable.

**Scope, precisely.** The lease covers the two paths that *perform* a production
merge. It does not cover `reverify_production()` / `jarvis wiz reconcile`, which
merges nothing and only re-reads a merge that already happened. That is a
deliberate gap and not a harmless one: the production observation it runs can
still interleave with another subsystem's deploy, so a reconciliation run during
someone else's deployment can read the wrong production. It is bounded by
requiring evidence that this pipeline performed the merge (§9), and by the fact
that it changes nothing in production itself. Closing it properly means taking
the lease there too.

- `openjarvis/core/proclock.py`, `wiz/features/pipeline.py::ship`,
  `reliability/repair.py::_merge_and_verify_production`
- `tests/reliability/test_repair.py::TestProductionChangeLease` — real
  `multiprocessing.Process`, including that both callers resolve the same lock
  file

## 2. Nothing pushes to the default branch directly

**enforced.** `GitHubSource` is constructed with
`allow_push_to_default_branch=False` unconditionally, and a coding session has
no Bash. Merging happens only through the pull-request path, behind the gates in
§3.

- `wiz/assemble.py::_shipper`, `reliability/repair.py`
- `tests/wiz/test_shipping.py`, `tests/reliability/test_repair_security.py`

## 3. A merge is bound to an exact commit, an unmoved base, and green checks

**enforced.** `evaluate_shipping()` gates on the observed head SHA matching the
verified one, the base SHA being unchanged since verification, each configured
required status context being `success`, the risk tier, and the authority model.
Every value is read fresh inside `ship()` *after* the lease is acquired, not
carried in from whatever `READY` last observed.

The base-side check has a real residual TOCTOU window: GitHub's
`expected_head_sha` guards the head only, so a base that moves between the read
and the merge is not caught server-side. `reverify_against_current_base()` is the
answer to a base that has moved, and `ship()` refuses again if it moves once
more.

- `wiz/features/shipping.py::evaluate_shipping`
- `tests/wiz/test_shipping.py`, `tests/wiz/test_pipeline.py::TestShip`

## 4. A HIGH-risk feature never merges without the owner

**enforced.** The risk tier is classified before the build and re-checked at
merge. HIGH requires `operator_approved`.

Caveat, stated plainly because it is the weakest approval binding in the system:
`operator_approved` arrives as a bare boolean in an HTTP body. It carries no
fingerprint, no TTL, and no binding to feature or SHA — unlike the MEDIUM-risk
approval, which has all three. The route is behind the dashboard's own authority
check and control token, so this is not an open door; it is an asymmetry in which
the most dangerous tier has the least-bound consent. See §19.

- `wiz/features/risk.py`, `wiz/features/shipping.py`, `server/wiz_routes.py`

## 5. Manual acceptance cannot mask an automated failure

**enforced.** A person may accept what no machine could measure — a layout check
with no selector to compile. They may not accept a check that ran and failed.
`_finish` tests `verification.passed` unconditionally before it looks at any
approval, and `approve_manual_acceptance()` refuses outright unless
`verification.passed` is true — the guard lives where the decision is *recorded*
as well as where it is read, because the durable record has more than one author.

`verification.complete` is never rewritten. An owner's yes is stored as a
distinct kind of evidence (`metadata["manual_acceptance"]`), so no later reader
can mistake a human's judgement for a measurement nothing took.

- `wiz/features/pipeline.py::approve_manual_acceptance`, `_finish`
- `tests/wiz/test_pipeline.py::TestManualAcceptanceLifecycle`,
  `::TestManualAcceptanceSurvivesTheProcessThatRecordedIt`

## 6. An owner's decision survives the process that recorded it; a capability does not

**enforced.** This is the distinction the approval model turns on.

An **owner authorization fact** ("I accepted these items on this commit") is
durable: written into the feature, and re-validated on every read against the
feature's *current* head SHA and *current* outstanding items. It survives a
restart and is readable by any process — necessarily, since the owner records it
from a short-lived CLI process that then exits.

A **one-time execution capability** (a bearer token from `ApprovalStore`) stays
in memory on purpose. An approval that outlives the process that issued it is
consent granted to something the operator has not seen since.

Durable is not standing. A new commit or a changed set of items stops the record
matching, exactly as it would stop a token matching.

- `wiz/approvals.py`, `wiz/features/pipeline.py::_manual_acceptance_still_valid`
- `tests/wiz/test_pipeline.py::TestManualAcceptanceSurvivesTheProcessThatRecordedIt`

## 7. Every channel has a ceiling, enforced structurally

**enforced, needs owner config.** `AuthorityPolicy` is intersected with
`CHANNEL_CEILING`, so a policy can only ever be narrower than the ceilings in
`wiz/authority.py`. Telegram and voice can never hold `PRODUCTION_CHANGE`, however
a config file asks.

`build_wiz` hands the policy to both gates that need it — the pipeline's
`CODE_WRITE` check and the shipper's `PRODUCTION_CHANGE` check. Both read their
collaborator as optional and skip when it is `None`, so the wiring is the
invariant: **a test asserts the back-fill happens, not merely that the gate works
when wired.**

`AuthorityPolicy.default()` grants no `CODE_WRITE`, `PR_WRITE` or
`PRODUCTION_CHANGE` to any channel. Feature work on a machine with no
`authority.json` is refused, deliberately — writing code is an opt-in an owner
makes on purpose. `describe_health()` reports `authority_gaps` naming each
missing grant, so this is diagnosable before a build is refused rather than
after.

- `wiz/authority.py`, `wiz/runtime.py::build_wiz`
- `tests/wiz/test_authority_is_enforced.py`

## 8. A feature's state moves only through the transition map

**enforced.** `check_transition()` rejects anything absent from
`LEGAL_TRANSITIONS`. Terminal states progress nowhere on their own, including
`HUMAN_REQUIRED`: leaving it is a distinct, separately-audited `resume_*`
primitive, never a transition a docile recovery path can make.

## 9. The window after a merge is not a dead end

**enforced.** `MERGING`, `DEPLOYING` and `PRODUCTION_VERIFYING` are where a
change is live and unproven — the most consequential place for a process to die.
`reconcile_after_ship()` (`jarvis wiz reconcile`) moves a stranded feature to
`HUMAN_REQUIRED` saying exactly what is unknown, then establishes the truth from
GitHub via `reverify_production()`: only a real `merged` answer carrying a real
merge commit counts as evidence there is anything to check. A merge that never
landed is reported, not assumed. Production that still fails does not become
`COMPLETE`.

**It refuses a merge this pipeline did not perform.** That is the line
`reconcile_external_merge()` exists to hold, and its own docstring says reusing
`reverify_production()` for an unauthorised merge "would let an unauthorized
merge complete through the same quiet path as an ordinary flaky retry". The
evidence required is the feature's own durable `history`: `MERGING` is a state
only `ship()` puts a feature into. A merge that arrived any other way — a coding
session's shell running `gh pr merge`, the real FEAT-00030 case — needs the
external-merge path, which demands the pull-request number explicitly, an
explicit owner acknowledgement, and stamps `shipping_path` so the history is
never erased.

A refusal that did nothing raises rather than returning an unchanged feature; a
refusal *after* the strand transition is the honest end of the call, and is
journalled as `feature.reconcile_incomplete`.

- `wiz/features/pipeline.py::reconcile_after_ship`, `wiz/features/postship.py`
- `tests/wiz/test_pipeline.py::TestAnInterruptedShipCanBeReconciled`

## 10. Every stop a feature can reach has a way out, reachable by the owner

**enforced.** Five operator verbs exist for the lifecycle's dead ends, and each
has a command: `jarvis wiz reopen --for planning|deploy|rebuild`,
`jarvis wiz accept`, `jarvis wiz refresh-base`, `jarvis wiz reconcile`.

The invariant is *reachability*, and it is tested as such: a test fails if a verb
exists on `FeaturePipeline` with no command that can run it, and a second fails
if a new verb is added and wired to nothing. An operator verb nothing can call is
a deadlock, not a recovery path — however well implemented and tested it is.

- `cli/wiz_cmd.py`
- `tests/wiz/test_cli_recovery_verbs.py::TestTheVerbsAreReachable`

## 11. A moved base is reconciled, never guessed

**enforced.** `reverify_against_current_base()` merges the current base into the
existing commit. It never rebases, never force-pushes over history, never calls
the coding engine, and never spends a build attempt — so it is safe to run
repeatedly as the base keeps moving. Stale evidence and the manual-acceptance
record are dropped, because both are bound to the superseded commit. A textual
conflict stops for a person.

## 12. Worktrees are owned across processes, and destruction fails closed

**enforced.** A worktree's path is derived from the incident or feature id, so two
processes reaching the same one would collide. `create()` claims it with
`git worktree lock`, stamping the owning pid and host into git's own lock reason
where any process can read it.

Removal asks before deleting. A claim naming a live process, or any process on
another host, is refused and left alone with its branch. A claim naming a dead
process on *this* host is broken — the only case that is safe, and the one that
stops fail-closed from becoming fail-forever. A lock this module did not write
is never broken at all.

`git worktree remove` runs with `check=True` and the directory is deleted only
when git agreed, or when git's own answer says the path is not a working tree
(the stale-directory case). Every other refusal leaves the tree in place and
says why. **This subsystem has destroyed real work twice. Unexplained is not the
same as unwanted.**

- `reliability/workspace.py::_remove_path`, `_may_break_lock`
- `tests/reliability/test_workspace.py::TestWorktreeOwnershipIsCrossProcess`,
  `::TestDestructiveCleanupFailsClosed` — real git throughout

## 13. The journal is append-only, chained, and honest about damage

**enforced.** `WizJournal.record()` holds a cross-process `ProcessLease`, so
sequence numbers are gapless across processes. Each entry hashes its
predecessor.

The invariant that matters is the second one: **a damaged journal reports itself
damaged.** `verify()` treats an unparseable line as a break and says where.
Sequence numbers are issued past the highest one anywhere in the file, never past
the last *readable* one, so a torn line cannot cause the chain to fork. An append
after a crash that left a line without its newline does not weld onto it.

Appending to a damaged journal is still allowed: one torn line must not silence
the audit trail permanently, now that the damage is reported and no longer
compounds.

- `wiz/journal.py::_scan`, `verify`, `_tail_locked`
- `tests/wiz/test_journal.py::TestCorruptionIsReportedNotHidden`,
  `::TestManyProcessesAtOnce` — 8 processes, 100 entries each

## 14. Feature state is written with compare-and-swap

**enforced.** `FeatureStore.save()` rejects a write whose durable revision has
moved, rather than last-writer-wins. See §19 for the caller-side gap.

## 15. An owner is told once, and never silently not at all

**enforced, at-least-once.** The ledger is written **after** a send succeeds, at
every recording site in both notifiers. Recording first meant one transient
failure (Telegram down for a moment) was written down as "told them", and every
retry then saw the entry and gave up — permanently, for the message whose entire
purpose is to say the system needs a person.

The trade is explicit and is the right way round: a crash between the send and
the ledger write costs at most one duplicate. **True exactly-once is impossible
against an external API like Telegram**, and this document does not claim it. A
duplicate is an annoyance; a silence is the system quietly giving up.

Dedup is keyed on a stable semantic fingerprint, so a reworded exception with the
same root cause does not escalate twice.

- `wiz/features/notify.py`, `reliability/notify.py`
- `tests/reliability/test_notify.py::TestAFailedSendIsNotRecordedAsTold`

## 16. The emergency stop stops the thing that is running

**enforced.** The stop is a flag file, so it works across processes. `RepairGate`
reads it on **every admission**, not at startup: the process that needs stopping
is the one that is busy, and it may never restart. An unreadable flag refuses
repairs — if it cannot tell, the answer is that it is stopped.

Previously only an in-process `stop()` call blocked repairs, while the Control
Center read the file and displayed "ENGAGED — new repairs are blocked". A safety
indicator reporting a guarantee nothing enforces is worse than no indicator.

- `reliability/watch.py::RepairGate`
- `tests/reliability/test_watch.py::TestTheDurableEmergencyStopActuallyStops`

## 17. Feature work defers to a production change in flight

**enforced.** `DevelopmentQueue.admit_next()` refuses while the
production-change lease is held, asked through `ProcessLease.is_held()` — which
asks the kernel. Never through `current_holder()`, which reads a record a
SIGKILLed holder leaves behind and would stall every feature for the rest of the
machine's uptime after one crash.

This is **advisory**, and the code says so. It decides "should I start now?".
Serialisation is the lease being *held* (§1), not this probe.

- `wiz/assemble.py`, `core/proclock.py::is_held`
- `tests/wiz/test_queue.py::TestProductionBusyIsConnectedToSomething`

## 18. Secrets do not survive to any egress

**enforced for the shapes it knows.** `BoundaryGuard` fails loudly rather than
running with zero scanners. The scanner covers the credentials this deployment
actually holds — Supabase JWTs (including `service_role`, which bypasses RLS),
Telegram bot tokens, Vercel tokens, bare `Bearer` values and unquoted
`KEY=value` assignments — as well as the usual providers. The Rust backend and
the pure-Python fallback carry the same pattern table, asserted by a parity test,
because a fallback that protects less than the primary reports clean with less
coverage.

Over-redaction is tested too: 40-character git SHAs, preview URLs and
`vercel_project_id` are not flagged.

- `security/scanner.py`, `rust/crates/openjarvis-security/src/scanner.rs`
- `tests/security/test_deployment_credentials.py`

---

## 19. Known gaps

Stated because a document that lists only what holds is a marketing document.

1. **`FeatureStore` CAS has no handlers.** `save()` correctly rejects a stale
   write, and no caller catches the error. A lost race in `ship()`'s post-merge
   window can leave the pull request merged while the store still says `READY`.
   §9's reconciliation is the way back, but it has to be run.
2. **`IncidentStore.transition()` has no CAS at all**, so a second process can
   overwrite a `HUMAN_REQUIRED` escalation from a stale read.
3. **Repair admission is process-local.** `RepairGate._active` is an in-memory
   dict. Two processes can each admit a repair for the same incident; §12 now
   stops them destroying each other's worktree, and §1 stops them merging at
   once, but the duplicate work itself is not prevented.
4. **HIGH-risk approval is an unbound boolean** — see §4.
5. **`ProcessLease` is not reentrant.** A second `acquire()` of the same lease in
   one process blocks against itself until the timeout. No current path nests,
   and nothing should be written that does.
6. **`reverify_production()` runs outside the production-change lease** — see
   §1. It performs no merge, but its production observation can interleave with
   another subsystem's deploy.
7. **The channel ceiling is checked against the feature's stored `source`**, not
   the actor causing the merge. Not currently reachable — only the Control
   Center route and the internal auto-ship path call `ship()` — but a future
   ship verb on a low-authority channel would inherit the wrong actor.

## 20. What this environment cannot prove

`launchd` supervision, real Telegram delivery, real Vercel deployments and
production lineage, real GitHub merge permissions, real Supabase, real Tailscale
access control, and anything about the live Mac's `authority.json`. Every
invariant above is verified against fakes, local git repositories and real
multi-process tests. None of it is a statement about production having behaved.
