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
       └─ pull request ............. if PR_WRITE permits. Merging is a different question.
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

Two bugs no unit test would have produced, both now fixed with regression tests:

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

A third finding was a usability bug in the classifier: §25's own example
sentence, *"Sir, add export to reports"*, was unrecognised, because the pattern
demanded the verb come first. That is how a channel ends up feeling broken while
every test passes.

## 6. Test counts

| Suite | Result |
|---|---|
| `tests/wiz` | **595 passed** |
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
- Feature merging is off at every risk level, and unconfigurable for HIGH.
- `SECRET_ACCESS` appears in no channel ceiling.
- The pipeline contains no call that merges anything; a test greps for it.

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
   not Wiz's. Everything up to the pull request can be autonomous today. The
   merge is off, and turning it on is a sentence in a settings file with the
   risk level named.

Steps 1, 2 and 5 are configuration. Steps 3 and 4 are a morning's work with a
real deployment in front of you. Step 7 is a judgement nobody should make on
your behalf.
