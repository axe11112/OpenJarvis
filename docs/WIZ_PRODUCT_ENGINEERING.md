# Wiz — the product-development side

**Status:** Phase C, D and E implemented. Verified against a real repository
with the real `claude` CLI.
**Branch:** `claude/wiz-autonomous-product-38qxc5`
**Date:** 2026-08-19

This document records what exists, what it refuses to do, and — the part that
matters most — exactly what is still missing before the operator can say
"build X" and later hear "it's live" without doing engineering work themselves.

`docs/WIZ_ARCHITECTURE_AUDIT.md` remains the record of Phases A and B: the
authority model, the capability registry, the feature request domain, the
Claude Code adapter and the worktree isolation. This document is what was built
on top.

---

## 1. The path a request takes

```
"Add a download button"     Control Center · CLI · Telegram · Sir Voice
  │
  ├─ intake adapter ................ identity, channel, authenticated flag
  ├─ intent classification ......... deterministic rules; names a verb or refuses
  ├─ capability lookup ............. unregistered name → no handler exists
  ├─ availability probe ............ claude CLI? target? browser? — asked, not assumed
  ├─ authority decision ............ deny-by-default, clamped by channel ceiling
  │
  ├─ feature.request (SAFE_ACTION) . a row is written. Nothing else happens.
  │
  └─ feature.build (CODE_WRITE) .... a separate verb, a separate authority
       │
       ├─ UNDERSTANDING ............ deterministic; no model involved
       ├─ PLANNING ................. claude CLI, tools = Read/Grep/Glob, in a worktree
       ├─ risk re-decided .......... paths mentioned + words + agent's opinion (max)
       ├─ HIGH? .................... stops for an approval bound to this plan
       ├─ acceptance contract ...... derived from the request, not from the plan
       ├─ BUILDING ................. claude CLI, write-enabled, in the worktree
       ├─ diff read FROM GIT ....... never from the agent's account of itself
       ├─ risk re-decided again .... on the real diff; HIGH here stops before any push
       ├─ TESTING .................. the target's own gates, from its own package.json
       ├─ PREVIEWING ............... branch pushed; preview matched on the exact SHA
       ├─ VERIFYING ................ contract checked in a browser, desktop and phone
       ├─ READY .................... every criterion checked and passed
       ├─ pull request ............. if PR_WRITE permits. Merging is a different question.
       │
       └─ (after a merge somebody authorised)
            ├─ PRODUCTION_VERIFYING . same contract, same browser, the production deployment
            ├─ COMPLETE ............. production agreed
            └─ handed to reliability  production did not — as an incident, one way only
```

Any failure between BUILDING and VERIFYING feeds its exact text back into a new
Claude session, up to a bounded number of attempts. The operator hears nothing
until the attempts run out.

## 2. What decides "done"

Not the agent. `wiz/features/acceptance.py` turns the request into typed
criteria, and the type names who checks each one:

| Kind | Checked by |
|---|---|
| `GATE` | the target's own lint / typecheck / test / build command |
| `CONTENT`, `INTERACTION`, `VIEWPORT`, `CONSOLE`, `NETWORK` | a real browser, on the preview, at two screen sizes |
| `ENDPOINT`, `UNAUTHORIZED` | an HTTP request |
| `MANUAL` | a person — and **never counted as passed** |

Three properties, each a test:

- A criterion nobody checked did not pass. `unchecked` counts against the
  verdict, so a verifier that is broken reports failure rather than success.
- A contract containing a `MANUAL` criterion cannot reach `READY` on machine
  evidence. It stops and says which part needs a person.
- The contract is derived from the **request**, never from the plan. A plan is
  prose a model wrote about a codebase; reading it is how a Python change
  acquired three browser criteria it could never satisfy (§7 below).

## 3. Two authorities, deliberately separate

`feature.request` is `SAFE_ACTION`; `feature.build` is `CODE_WRITE`. "I would
like X" and "go and build X" are not the same sentence, and a channel may be
allowed to say only the first. Voice and Telegram can ask; whether they can
cause a coding session is a separate grant.

Shipping is a third question again. `wiz/features/shipping.py` shares **no
field** with the reliability merge settings — turning on automatic repair of
production does not turn on automatic merging of features, and a test asserts
the two configuration surfaces are disjoint, because the way that would fail is
not a decision to conflate them but a refactor noticing two similar booleans.

Shipped defaults:

| Setting | Default |
|---|---|
| `create_pull_request` | **on**, still subject to `PR_WRITE` |
| `merge_low_risk` | off |
| `merge_medium_risk` | off |
| merging a HIGH-risk feature | **not configurable at all** |

## 4. What was run, for real

Two pilots against real git repositories, with the installed `claude` CLI
(2.1.235), real worktrees, real gates and real commits.

**Pilot 1 — a feature, start to finish.** Request: *"Add a render_footer
function to the report module that returns a dash rule, with a test."*
Wiz opened a worktree at `fa9c5ed`, ran a read-only planning session, classified
the change MEDIUM, ran a write-enabled session, read the diff from git, ran the
target's pytest suite (passed), and committed `9f40e635` as the configured git
identity. Claude wrote both the function and its test. The feature then stopped
at `HUMAN_REQUIRED` with *"I have no way to see a preview of this, so I cannot
prove it works"* — correct, because that fixture has no preview provider.

**Pilot 2 — the iterative loop.** Same machinery, with the first gate run failed
on purpose to a specific, realistic message. Wiz recorded the failure on the
attempt, started a **second real Claude session**, and handed it the exact text.
The second session's context contained:

```
# What has already been tried
Do not repeat an approach listed here. If the same fix looks right again,
the diagnosis is probably wrong.

## Attempt 1
Claude reported: All 6 tests pass, including the frozen `test_contract.py` cases.
Failed because: tests/test_contract.py::test_a_session_with_no_metres_key_...
                FAILED - KeyError: 'metres'
```

Attempt 2 passed the real suite (6 tests) and committed `a279795f`.

Note what attempt 1 *claimed*: "All 6 tests pass." It was wrong, and it was
recorded as a claim rather than believed. That is the whole reason the diff is
read from git and the gates are run by Wiz.

**Not yet run for real:** the Vercel preview and the Playwright verification.
Neither a Vercel project nor Playwright is available in this environment. Both
are covered by tests against the same interfaces the production objects
implement, but neither has met a real deployment.

## 5. What the pilots found

Four bugs no unit test would have produced, all now fixed with regression tests:

**A backend change was given browser criteria it could never satisfy.** UI
detection and quoted-label extraction both read the plan. A plan mentions
"render", quotes identifiers and talks about the page a change is near. Three
`CONTENT` criteria were derived against a route that does not exist — and one of
them was `s phrasing (`, because a single quote was being treated as a
delimiter rather than an apostrophe. A false positive here is worse than a false
negative: the feature can never reach `READY` and looks broken.

**Every request in a swim-training product was HIGH risk.** Bare `session` was
in the risk word list, and "sessions" is the domain's most common noun. An
approval that appears on everything is one the operator learns to click through
without reading, which is worse than not asking. `session` now needs an auth
qualifier; the *path* patterns are unchanged and still catch
`src/lib/auth/session.ts` whatever the request called it.

Re-running the pilots after clearing their state by hand — which is the shape a
crash has — found a third: a worktree directory removed out from under git
leaves git's registration behind, the branch then counts as checked out
somewhere, and the feature could never be retried. It also surfaced the raw git
command line reaching the operator, which is precise and useless to somebody who
is not reading the code.

A fourth was a usability bug in the classifier: §25's own example
sentence, *"Sir, add export to reports"*, was unrecognised, because the pattern
demanded the verb come first. That is how a channel ends up feeling broken while
every test passes.

## 5b. After the merge

`wiz/features/postship.py` covers the window the incident machine already names
for a reason: a change is live and unproven, and the operator has already been
told it is done. Production is checked by the same contract, the same browser
and the same exact-SHA matching the preview was — a feature that passed one bar
and was spared the other would make the first bar meaningless. Production
screenshots are filed apart from the preview's so the two can be compared.

The handover is the point of the module. `reliability` must never import `wiz`,
so it cannot be a callback registered the other way: it is a plain data object
and a handler. The shipped handler opens an incident through reliability's own
store, fingerprinted on the *feature* so repeated failures group into one
incident — which is what the other side's "no second automatic merge after a
post-merge failure" gate depends on.

Nothing there reverts anything. Undoing a change that is live in front of users
is a judgement about the product, and it belongs to a person rather than to the
thing that just built the change and would very much like it to have worked.

## 5c. The morning summary

`wiz/briefing.py` composes it and cannot deliver it — a test checks the imports,
because that is where a delivery capability would have to come from. Approvals
go above the good news, because the one thing in a morning summary with a
deadline is the feature that has been waiting since yesterday. "Wize is healthy"
is a claim about probes that ran, so a reliability subsystem that cannot answer
produces no health line rather than a reassuring one.

`worth_sending` is the field that matters: a summary arriving every day saying
"nothing happened" is one nobody reads by the end of the second week, and by
then it is the one carrying the sentence that mattered.

## 6. Test counts

| Suite | Result |
|---|---|
| `tests/wiz` | **641 passed** |
| `tests/reliability` | 1504 passed, 17 failed — **identical at the branch base** |
| `reliability` importing `wiz` | **0**, enforced by a test that parses imports |

The 17 reliability failures are environmental and pre-existing: macOS `launchd`
(this is Linux), a missing `ffmpeg`, and the Rust extension that `make test`
builds first. Verified by running the same suite at commit `b0d4c84`, before any
of this work, and getting the same 17.

`ruff check` and `ruff format --check` pass on everything this branch touches.
Several files inherited from Phase B did not pass `ruff` and now do.

## 7. What is deliberately switched off

Nothing in this work enabled any new production authority.

- No channel is granted write authority by the shipped default policy.
- Feature merging is off at every risk level by default, and unconfigurable
  for HIGH. (§9: the *mechanism* now exists — see below — but the default
  policy still ships `merge_low_risk=False`, `merge_medium_risk=False`, and
  there is still no field that could turn HIGH on.)
- `SECRET_ACCESS` appears in no channel ceiling.
- `run` — the autonomous loop — still contains no call that merges anything.
  A feature it advances on its own stops at `READY` exactly as before.

## 8. What remains before "Sir, it's live"

Honestly, and in order of what actually blocks it:

1. **A configured target.** `~/.openjarvis/wiz/wiz.json` naming the Wize
   checkout, its base branch and its commands. `jarvis wiz status` prints
   exactly what is missing. Nothing works until this exists, and it is a file a
   person writes, on purpose, in a directory Wiz may not edit.

2. **Playwright installed.** Without a browser, no user-interface feature can
   reach `READY` — it stops at "I cannot check that this works". `uv sync
   --extra browser`.

3. **A first real Preview run.** The Vercel matching is written and tested
   against the shape `VercelSource` returns, but it has never seen a real
   deployment. The first one will find something; they always do.

4. **A first real Playwright verification.** Same. In particular, the contract
   derives `CONTENT` criteria from quoted text in the request, which will be
   too weak for most real features — the planning session's proposed criteria
   (already supported, additively) are what will make these checks bite.

5. **`PR_WRITE` granted to the Control Center**, and a GitHub token with write
   scope, before any pull request is opened.

6. **The last wire on two channels.** The Telegram and voice adapters are built
   and tested, but the live Telegram bot and the voice intent table are files
   another session owns today; each needs one call adding. Until then those two
   channels are reachable through the API and the CLI but not from the phone or
   the microphone.

7. **A deliberate decision about feature merging** — which is the operator's,
   not Wiz's. Everything up to the pull request can be autonomous today. §9
   built the mechanism this item used to be waiting on; the decision itself —
   flipping `merge_low_risk` / `merge_medium_risk` in the settings file, with
   the risk level named — is still, correctly, unmade by default.

Steps 1, 2 and 5 are configuration. Steps 3 and 4 are a morning's work with a
real deployment in front of you. Step 7 is a judgement nobody should make on
your behalf.

## 9. Shipping: the merge and post-ship wiring that did not exist

This session audited the branch against a separate, parallel Wiz rebuild
started from a clean checkout, specifically to check for exactly what §5c and
this section describe: fakes, duplicated systems, and gaps between what the
docs claimed and what the code did. Two real findings came out of it, and both
are now closed.

**Finding: a second, dead implementation is merged into this tree.**
`wiz_orchestrator.py`, `feature_executor.py`, `feature_gate_integration.py`,
`feature_shipping_authority.py`, `feature_contract.py`, `code_reviewer.py`,
`acceptance_test_executor.py`, `claude_cli_executor.py` and
`vercel_preview_tracker.py` are a complete, earlier "Priority 1–10" pipeline,
merged into this tree at `333d335` alongside the `features/` package that
superseded it. Nothing outside that group imports it — `assemble.py`,
`product.py` and every live entry point are wired to `features/*` exclusively
— and its own test (`test_wiz_orchestrator.py`) fails independently of
anything in this session's changes. It is dead, not fake: the code is real and
was once live, but no path reaches it today. Left in place rather than deleted
here, because removing ~3,000 lines and their tests is a decision worth its
own session, not a side effect of this one.

**Finding: opening a pull request was where autonomy ended, in code as well as
in policy — but the *mechanism* to go further did not exist, contrary to what
§7/§8 implied.** `FeatureShippingPolicy` already had `merge_low_risk` and
`merge_medium_risk` fields, and `evaluate_shipping` already computed a correct
`ShipDecision` from them. But nothing ever read that decision and called
`GitHubSource.merge_pull_request` — `FeatureShipper` had no merge method at
all, and `FeaturePipeline.run` stopped at `READY` with no dispatch entry for
`MERGING`, `DEPLOYING` or `PRODUCTION_VERIFYING`, despite `features/model.py`
defining legal transitions through all of them and `features/postship.py`
already containing a complete, tested, entirely unwired production verifier.
Turning merging on was never going to be "a sentence in a settings file" — the
sentence had nothing to act on.

Closed, reusing what already existed rather than building a parallel path:

- `FeatureShipper.merge_feature()` (`features/shipping.py`) is the missing
  write. It re-evaluates the shipping gates, then asks GitHub what the
  configured token can *actually* do — `GitHubSource.can_write()`, which reads
  the repository's own `permissions` block for this credential rather than
  trusting a collaborator role that can say "maintainer" while the token is
  scoped read-only — and refuses closed if that check fails, errors, or the
  merge call itself comes back 403. TOCTOU beyond that is closed by GitHub
  itself: `merge_pull_request` sends the verified commit as the expected head
  and the API refuses server-side if the branch moved.
- `FeaturePipeline.ship()` is the new verb that calls it. Deliberately not a
  step `run` reaches on its own — `run` still stops at `READY`, unchanged, and
  a test now proves that behaviourally rather than by grepping the module
  source for the string `merge_pull_request`, since that grep would no longer
  mean what it used to. `ship` reads the pull request and CI status fresh,
  calls `merge_feature`, and on a real merge drives the feature
  `MERGING → DEPLOYING → PRODUCTION_VERIFYING`, then hands off to
  `features/postship.py`'s already-built `PostShipVerifier` and `complete()`
  — `COMPLETE` if production agrees, `HUMAN_REQUIRED` (with reliability's
  incident store, unchanged) if it does not, or if no post-ship verifier is
  configured to check at all.
- `assemble.py` gained `_postship()`, reusing the same `VercelSource` (target
  `"production"` instead of `"preview"`) and the same browser verifier the
  preview stage already builds — no new client, no new credential path.
  `runtime.py` wires `postship.journal` the same deferred way `pipeline.journal`
  already was.

**Credentials, unchanged.** Both the merge path and everything upstream of it
run on `GITHUB_READONLY_TOKEN` and `VERCEL_READONLY_TOKEN` — the same
environment variables `reliability/sources/github.py` and `.../vercel.py` have
always read, resolved through `openjarvis.core.credentials`. No new
credential store, no new token file, nothing this branch's `_shipper()` and
`_preview_observer()` were not already doing. `GITHUB_READONLY_TOKEN` reads
oddly for a name that can now merge a pull request; it is the operator's
existing name for the one GitHub credential Wiz and reliability both hold, and
renaming it was out of scope here — a fine-grained token can be
"read-only" about repository content and still carry Pull requests: Write,
which is the actual grant `can_write()` checks. The token's real scopes are a
live-Mac question this cloud session cannot answer; see below.

**Verified:** `tests/wiz/test_shipping.py` (37 tests, 7 new — `can_write()`
refusing before a network call, a 403 from the merge call itself being
reported not raised, GitHub's own decline being reported not raised),
`tests/wiz/test_pipeline.py` (55 tests, 9 new, in a new `TestShip` class —
merge-and-complete, state order, production disagreeing hands off, a feature
that is not yet READY is refused, no shipper configured, a token without push
permission stays READY, a channel without `PRODUCTION_CHANGE` cannot ship,
merging without a postship verifier hands to a person, shipping twice merges
once), plus `test_postship.py`, `test_preview.py` and `test_runtime.py`
unchanged and still green — 172 tests across the six touched files, all
passing. The full `tests/wiz/` run that produced the **641 passed** figure in
§6 did not complete cleanly in this session's container (it hung during
collection with near-zero CPU use, not mid-run — consistent with §6's own note
that this environment is not the author's Mac); it was not re-confirmed here
and should be before this branch is trusted beyond the files listed above.

**Still true, and now testable rather than assumed:** merging remains off by
default (`FeatureShippingPolicy()` still ships `merge_low_risk=False`), still
requires `Authority.PRODUCTION_CHANGE`, which no channel but
`Channel.CONTROL_CENTER` can ever hold, and still cannot be configured for
HIGH risk. What changed is that turning it on now does something.

**What this session could not do, and why:** prove any of the above against
the real `axe11112/Wize-Performance` repository or a real Vercel deployment.
This is a cloud container with a proxy-injected `GITHUB_TOKEN` and no
`GITHUB_READONLY_TOKEN` / `VERCEL_READONLY_TOKEN` in its environment — the
credential the *service* on the real Mac actually runs with, per
`docs/…` commit `2dd16a7`'s own hard-won lesson, is not something a cloud
session can read out of thin air, and building a second credential path to
work around that was explicitly the wrong move. `can_write()` and
`merge_pull_request` were exercised against a `FakeGitHub` double whose
behaviour is asserted, not against GitHub's API. The real proof — does the
Mac's actual `GITHUB_READONLY_TOKEN` have Pull requests: Write, does a real
merge land, does `VercelSource` find the resulting production deployment by
SHA — needs that machine, that token, and a real LOW-risk feature request. If
it turns out the token lacks the grant, that is not a bug in this session's
code: `merge_feature` is built to say so exactly, by name, rather than fail
silently or fall back to a wider credential.
