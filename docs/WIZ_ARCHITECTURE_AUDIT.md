# Wiz — Architecture & Inventory Audit

**Status:** audit only. No feature code was written for this report.
**Date:** 2026-08-16
**Branch audited:** `claude/jarvis-autonomous-engineer-2yveid` (31 commits ahead of `main`, 0 behind).
**Scope:** the whole fork — 716 Python modules, ~183k LOC, 618 test files.

---

## 0. What "upstream" means here

There is no second git remote. `origin` is the fork `axe11112/OpenJarvis`; `main` carries the
upstream OpenJarvis history (latest upstream commit `a97c64c6`, authors Elliot Slusky et al.).
All of our work sits on the feature branch and has never been merged back.

**Ours** (fork-only, 31 commits, all authored by Claude on this branch):

- `src/openjarvis/reliability/**` — the entire reliability subsystem
- `src/openjarvis/server/reliability_routes.py`, `server/reliability_dashboard.py`
- `src/openjarvis/cli/reliability_cmd.py` (1,947 lines)
- `docs/JARVIS_*.md` (7 documents)
- `tests/reliability/**` (37 test modules)

**Upstream** — everything else. This matters, because the answer to "should we build X?" is
almost always "no, upstream already has it." The fork is a small, safety-critical addition on
top of a very large assistant framework we have barely touched.

The single most useful finding of this audit: **we have been building a reliability engineer
next to a personal assistant, without connecting them.** Most of the 40 requested features are
integration work, not new construction.

---

## 1. Existing capabilities we can reuse (as-is)

| Area | Where | Notes |
|---|---|---|
| Deep research | `agents/deep_research.py` (`DeepResearchAgent`), `tools/web_search.py`, `server/research_router.py`, `cli/deep_research_setup_cmd.py`, `DeepResearchConfig` | Source extraction already implemented (`_extract_sources`). `ddgs` (free) is **installed**; Tavily is not. |
| Scheduled tasks | `scheduler/scheduler.py` (`TaskScheduler`), `scheduler/store.py` (`SchedulerStore`) | Already has name, cron, enabled/paused status, `last_run`, `next_run`, run logs, failure capture — exactly the §10 field list. |
| Skills | `skills/` — manager, loader, parser, executor, `security.py` (`TrustTier`, `validate_capabilities`, `has_dangerous_capabilities`), dependency, importer, index, overlay, tool_adapter | 30+ bundled skills in `skills/data/*.toml`. Manifests already declare capabilities. |
| MCP | `mcp/` — client, loader, protocol, server, transport; `tools/mcp_adapter.py`; `MCPConfig` | Trust-boundary handling is the gap, not the protocol. |
| Messaging | `channels/` — 33 channels incl. Telegram, Slack, Discord, Matrix, Signal, iMessage | Telegram is **already configured** (`channel.telegram.allowed_chat_ids` set) and used by reliability notifications. |
| Coding agent | `reliability/repair.py`, `workspace.py`, `code_agent.py`, `scope.py`; `agents/claude_code.py` | §9 is *already built*, and built more safely than a generic coding agent would be. |
| Audit chain | `security/audit.py` (`AuditLogger`, hash chain, `verify_chain`) + `reliability/store.py` (independent chain over incident transitions) | Two chains exist. See §9 conflicts. |
| Approval store | `tools/approval_store.py` (`pending_actions`: payload, `permission_key`, tier, status, `expires_at`) + `server/approval_routes.py` | §32 is ~60% built already. |
| Retrieval | `connectors/` — `hybrid_search.py` (BM25 FTS + vector fusion), `retriever.py` (`TwoStageRetriever`, ColBERT rerank), `chunker.py`, `embeddings.py`, `embedding_store.py`, `store.py` (`KnowledgeStore`) | Vector path needs Ollama, which is **not installed**. BM25/FTS path works today. |
| Calendar / email | `connectors/gcalendar.py`, `gmail.py`, `gmail_imap.py`, `outlook.py`, `google_auth.py`, `oauth.py` | Code exists; **no account configured**. Must report NOT CONFIGURED, never pretend. |
| Cost / tracing | `traces/` (collector, store, analyzer), `telemetry.db`, `analytics/`, `server/cost_calculator.py`, `savings.py` | |
| Injection defense | `security/injection_scanner.py`, `taint.py`, `credential_stripper.py` (`wrap_tool_output`), `guardrails.py` | Already used by `reliability/briefing.py`. |

---

## 2. Existing capabilities we should extend

| # | Feature | What exists | What's missing |
|---|---|---|---|
| 0 | Identity | `reliability/notify.py` has a persona helper `_sir()` and `reliability.notify.persona` config | Templates still emit `🔧 JARVIS`, `🟢 JARVIS`, `JARVIS ALERT` headers — exactly what the brief forbids. Mechanical rename of user-facing strings; `tests/reliability/test_notify.py` asserts on them. |
| 2 | Memory | `memory/store.py` = `LocalFactStore` (flat `Fact{text, source}`), `memory/service.py` (background extraction), `memory/extractor.py`; CLI has `index`, `search`, `list`, `clear`, `stats` | No typed categories, no FACT/DECISION/PREFERENCE/INFERENCE/TEMPORARY distinction, no confidence, no timestamps as first-class provenance, **no per-item forget or correct** (only clear-all). This is the largest genuine extension. |
| 3 | Daily briefing | `agents/morning_digest.py`, `digest_store.py`, `cli/digest_cmd.py`, `server/digest_routes.py` | Add a Wize/reliability section; suppress sections with no data. |
| 4 | Notifications | `reliability/notify.py`: `NotificationRouter` with rate limiting (`max_messages_per_hour`), `min_severity`, dedup/prune, deterministic templates for alert/progress/resolved/recovered/human_required/rolled_back/merge | The new philosophy *removes* alert+progress notifications. Must be done without silently disabling CRITICAL escalation (`escalation.py`, `critical_escalation_minutes`). |
| 7 | GitHub assistant | `reliability/sources/github.py`, `correlate.py`, `connectors/github_notifications.py`, `tools/git_tool.py` | Q&A surface over the existing source. Do not build a second truth system. |
| 11 | Proactive | `agents/proactive_agent.py` (693 lines) — approval routing, dedup via seen-IDs, cooldowns, cron registration, `ProactiveConfig` | Priority model + routing "now vs next briefing". |
| 12 | Pattern detection | `reliability/fingerprint.py`, `flapping.py`, `correlate.py`, store `find_by_fingerprint` + occurrence counts | Cross-incident aggregation over a time window. Recommend-only, never auto-act. |
| 16 | Control Center | `reliability/dashboard/` — stdlib `ThreadingHTTPServer`, `model.py` (`CardView`, `ProbeView`, `IncidentView`, `SafetyPanel`), static `app.js`/`index.html`/`style.css`/`wiz.svg` | PWA manifest, service worker, mobile layout, the new views. |
| 26 | Weekly report | `reliability/report.py` | Weekly rollup + delivery. |
| 27 | Cost | `traces/` + telemetry | Attribute cost to repairs and research runs. |
| 28 | Model routing | `learning/routing/`, `intelligence/model_catalog.py` (1,024 lines), `RouterPolicyRegistry` | Task classes. Safety gates must stay deterministic. |
| 29 | Self-diagnostics | `reliability/diagnostic.py` already checks configuration, github, vercel, supabase, website, probes, notifications, code_agent, runtime_errors, audit chain | All of it is **Wize** health. Need a separate **Wiz** health axis (watcher/dashboard/scheduler/memory/voice/research alive). |
| 31 | Authority model | `security/capabilities.py` — `Capability`, `CapabilityGrant`, `AgentPolicy`, `CapabilityPolicy` | **See §5. Documented but not implemented.** |
| 32 | Approval center | `tools/approval_store.py` + `server/approval_routes.py` | SHA binding, single-use, expiry enforcement, audit. |
| 34 | Injection defense | Scanner, taint, fencing, 6 documented controls | Apply to the *new* surfaces (email, docs, research, messages) + regression tests. |
| 35–38 | Live activity / timeline / NL status | `WatchSupervisor.status()`, `RepairGate.snapshot()`, `incident_transitions` table + `transitions_for()` | Human phrasing over existing state. Deterministic — no LLM needed. |
| 14 | Voice | `speech/` — faster-whisper, openai-whisper, deepgram STT; `tts.py`, kokoro, cartesia, openai TTS | None installed. But `whisper-cli` (whisper.cpp) **is installed** at `/usr/local/bin/whisper-cli`, and `ggml-base.en` + `ggml-tiny.en` are already staged in `~/.openjarvis/voice/models/`. A whisper.cpp backend + macOS `say` backend are small additions. |

---

## 3. Missing capabilities (genuinely new)

| # | Feature | Size | Notes |
|---|---|---|---|
| 1 | Unified Wiz brain | Medium | The design is **already written** in `docs/JARVIS_SECURITY.md` §7 Control 5: "maps natural language onto a small, explicitly enumerated set of commands… does not execute arbitrary instructions." Build that verb table, not a free-form tool-calling agent. Reuse `agents/orchestrator.py`'s structured-response parsing for intent only. |
| 8 | PR review assistant | Small | Compose `GitHubSource` + `security/scanner.py` (secret/PII) + `code_agent`. |
| 13 | Incident explanations | Small | Deterministic translation over `Incident`/`Evidence`/transitions. Prefer templates to a model. |
| 15 | Voice command safety | Small | Same authority table as §31 — a voice-allowed subset, not a second system. |
| 18 | Push notifications | Small | Web Push/VAPID; needs `pywebpush` + `py-vapid` (not installed) and a subscription store. |
| 25 | Personal search | Medium | A federation layer over hybrid_search + incidents + audit + research + GitHub + tasks, with per-result provenance. |
| 30 | Capability registry | Small | Derived from config + live checks. This is what makes "never pretend" enforceable. |
| 39 | Command palette | Small | Same orchestration as voice. |
| 24 | Home Assistant | — | Deferred (see below). |

---

## 4. Proposed architecture

```
                    ┌──────────────────────────────────────────┐
   Telegram ──┐     │              WIZ BRAIN                   │
   Voice ─────┤     │  intent  →  authority  →  dispatch       │
   Palette ───┼────▶│  (enumerated verb table, NOT free-form)  │
   Schedule ──┘     └───────┬──────────────────────────┬───────┘
                            │                          │
                    ┌───────▼────────┐        ┌────────▼─────────┐
                    │ CAPABILITY     │        │ APPROVAL CENTER  │
                    │ REGISTRY       │        │ (expiring,       │
                    │ "can I?"       │        │  SHA-bound,      │
                    └───────┬────────┘        │  single-use)     │
                            │                 └────────┬─────────┘
        ┌───────────────────┼──────────────────────────┼──────────┐
        │                   │                          │          │
  ┌─────▼─────┐  ┌──────────▼──────┐  ┌────────────┐  ┌▼────────┐ │
  │RELIABILITY│  │ ASSISTANT       │  │ KNOWLEDGE  │  │ SAFETY  │ │
  │(sacred,   │  │ research/digest │  │ memory     │  │ gates   │ │
  │ always on)│  │ github/pr/tasks │  │ docs/search│  │ (config)│ │
  └───────────┘  └─────────────────┘  └────────────┘  └─────────┘ │
        │                   │                          │          │
        └───────────────────┴──────────┬───────────────┴──────────┘
                                       ▼
                              AUDIT (hash-chained)
```

Four rules that make this safe:

1. **The brain never executes.** It resolves an intent to a *named verb* from a fixed table.
   Anything not in the table is refused, not improvised.
2. **Authority is checked before dispatch, deterministically.** No model output is an input to
   the authority decision.
3. **Reliability is a peer, not a dependency.** Every other box can crash without touching it.
4. **The registry is the only source of "can I".** Both the dashboard and the natural-language
   answers read from it, so they cannot disagree.

**Placement:** a new top-level `src/openjarvis/wiz/` package (brain, registry, authority,
explain, search). It depends on `reliability/` and upstream subsystems; **nothing in
`reliability/` may import from `wiz/`.** That import direction is what keeps reliability sacred,
and it is worth enforcing with a test.

---

## 5. Security / authority model — and the most important finding

`docs/JARVIS_SECURITY.md` §2 specifies a capability model:

> `infra:read`, `infra:deploy`, `db:read`, `db:write`, `repo:read`, `repo:write`, configured
> **deny-by-default**, stored at `~/.openjarvis/reliability/capabilities.json`, with every
> denial publishing `CAPABILITY_DENIED` to the audit log.

**None of it is implemented.** Grep confirms: no `infra:read`, no `repo:write`, no
`capabilities.json` anywhere in `src/`. `CAPABILITY_DENIED` exists only as an unused event enum
member and one reference in `tools/_stubs.py`.

What actually enforces safety today is a *different*, config-driven mechanism, and it works:

- boolean gates (`repair.enabled`, `merge.enabled`, `supabase.allow_production_writes`,
  `policy.allow_push_to_default_branch`, `policy.deploy_mode`)
- `reliability/scope.py` (max changed files/lines, protected paths)
- `sources/sql_guard.py` (hard SQL write guard)
- `security/boundary.py` `BoundaryGuard` + secret/PII scanners
- the repair agent's `agent_allowed_tools` / `agent_disallowed_tools`

So the risk is **not** that we are currently unsafe. The risk is that the documented model and
the implemented model have drifted, and every feature in this brief wants to hang off the
documented one. Two concrete traps:

- `CapabilityPolicy` is **open-by-default** unless constructed with `default_deny=True`
  ("if no explicit policy exists for an agent, all capabilities are granted"). Introducing the
  orchestrator on the framework default would create an authority hole on day one.
- `MCPConfig.enabled` defaults to `True` (with an empty server list). Adding MCP servers must
  not make them reachable from the repair agent's tool set.

**Recommended authority levels**, mapped onto what already exists:

| Level | Meaning | Enforced today by | Voice? |
|---|---|---|---|
| `READ` | status, incidents, diagnostics, memory search, research read | source `enabled` flags | yes |
| `SAFE_ACTION` | run diagnostic, rerun read-only probe, pause repair, emergency stop, restart watcher | `RepairGate.block()`, supervisor | yes |
| `CODE_WRITE` | worktree edits, tests | `workspace.py`, `scope.py` | no |
| `PR_WRITE` | branch + PR creation | `repair.enabled`, `branch_prefix` | no |
| `PRODUCTION_CHANGE` | merge, deploy, push to main, Supabase writes | `merge.enabled`, `deploy_mode`, `allow_production_writes`, `allow_push_to_default_branch` | **never** |
| `SECRET_ACCESS` | read/modify credentials | env-only, 0600 `reliability.env` | **never** |

**Belt and braces:** add the capability layer *in front of* the existing config gates, never
in place of them. A capability grant must not be able to turn on something a config flag has
turned off. Both must say yes.

**Prompt injection** (§34): the six controls in `JARVIS_SECURITY.md` §7 are the right ones and
Control 4 (capability containment) is explicitly called out as "the control that actually
matters". Extending Wiz to email, documents, messages and research widens the untrusted-input
surface a lot — that is precisely why the authority layer should land *before* those features,
not after.

---

## 6. Implementation phases

Each phase is a separate commit, and the reliability + security suites run after every one.

**Phase 1 — Foundation** *(recommended first; see §10)*
Identity rename · authority model · capability registry · Wiz self-diagnostics · unified brain
(read/safe verbs only) · Control Center restructure.

**Phase 2 — Intelligence**
Incident explanations · pattern detection · GitHub assistant · PR review · deep research
integration · document intelligence (BM25 path) · personal search.

**Phase 3 — Personal assistant**
Daily briefing · scheduled tasks · proactive routing · weekly report · calendar/email *only if
configured*.

**Phase 4 — Voice & mobile**
PWA + manifest/service worker · Tailscale access · whisper.cpp STT backend · `say` TTS ·
two-way voice on the same verb table · push notifications.

**Phase 5 — Extensions**
Skills routing · MCP trust boundaries · Home Assistant foundation · additional messaging.

**Phase 6 — Hardening**
Injection regression tests · permission tests · failure-isolation tests · adversarial tests for
high-risk verbs · resource testing on this hardware.

---

## 7. Dependencies

**Already available (no install):** `ddgs` (free web search), `python-telegram-bot`,
`playwright`, `numpy`, `httpx`, `aiohttp`, `whisper-cli` + `ggml-tiny.en`/`ggml-base.en` models,
`/usr/bin/say`, SQLite FTS5.

**Needed, cheap:** `pywebpush` + `py-vapid` (push), `pdfplumber` (PDF docs), `croniter`
(scheduling; currently absent — `scheduler.py` has its own cron helper).

**Needed, external / user action:** Tailscale (**not installed**) for mobile access; Google
OAuth client for calendar/email; Home Assistant token (Phase 5).

**Recommended *against* on this machine:** `torch`, `faster-whisper`, `kokoro`,
`sentence-transformers`, `faiss`, `colbert-ai`, Ollama. See §8.

---

## 8. Resource impact on this Mac — the binding constraint

```
MacBookAir8,2 (2019) · Intel Core i5-8210Y @ 1.60GHz · 2 cores / 4 threads
8 GB RAM · 43 GB free of 233 GB · no usable GPU · Ollama not installed
Currently: 55% memory free, and the machine is already paging (2.4M pageouts)
```

Currently running under launchd: watcher (~26 MB RSS) + Control Center (~45 MB) + supervisor
shell. The venv is a lean 662 MB (playwright + numpy; **no torch**).

This hardware decides several designs for us:

- **No local LLM inference.** Anything needing a model goes to a cloud API — as repair already
  does via the `claude` CLI. Budget accordingly; there is no free local fallback.
- **No local embeddings.** The vector half of `hybrid_search` needs Ollama. Start document
  intelligence on the **BM25/FTS5 path only**, which is already implemented and costs nothing.
  Adding `sentence-transformers` pulls torch (~2–3 GB disk) and would be painfully slow on this
  CPU.
- **STT is viable** because whisper.cpp is native and already installed. Use `ggml-tiny.en` for
  interactive latency; `base.en` is noticeably slower on 2 cores.
- **TTS should be `/usr/bin/say`** — zero install, zero cost, instant. Kokoro needs torch.
- **Process budget: at most one new always-on process.** Each Python process costs ~25–50 MB
  RSS. Fold the scheduler into the watcher or the dashboard rather than adding a daemon per
  subsystem. On 2 cores, an indexing or research job can starve the watcher — reliability needs
  scheduling priority.
- Playwright/Chromium (probes) remains the heaviest recurring cost and is already accounted for.

Rough steady-state target after all phases: **< 150 MB RSS added, < 1 GB disk added**, provided
we hold the line on torch.

---

## 9. Conflicts with the reliability architecture

| # | Conflict | Resolution |
|---|---|---|
| 1 | **Loopback vs mobile.** The Control Center hard-binds to loopback *and* validates the `Host` header against DNS rebinding. A phone is not loopback. | Put Tailscale Serve in front, terminating on the tailnet and proxying to `127.0.0.1`. **Do not relax `_is_loopback` or the Host check.** This satisfies §17 without weakening anything. |
| 2 | **Mutation surface.** The dashboard is deliberately read-mostly — two POST routes, everything else refused. Memory writes, approvals and voice commands would multiply that. | One audited command endpoint behind the existing `X-JARVIS-Control` token + CSRF, dispatching the verb table — not a scattering of new routes. |
| 3 | **Two audit chains** already exist (`security/audit.py`, `reliability/store.py`). A third would fragment the record. | Assistant actions → `security.AuditLogger`. Incident lifecycle stays in the reliability store. No new chain. |
| 4 | **`CapabilityPolicy` is open-by-default.** | Always construct with `default_deny=True`; assert it in a test. |
| 5 | **LLM in the loop.** §1 risks becoming an execution path into repair/merge. `JARVIS_SECURITY.md` §7 Control 6 is explicit that verification is not model-driven. | The brain dispatches READ/SAFE verbs directly; everything above that produces an approval request, never an action. |
| 6 | **Quiet notifications vs escalation.** Removing alert/progress notifications (§4) touches the same router as `critical_escalation_minutes`. | Suppress the routine templates only; keep CRITICAL escalation and prove it with a test. |
| 7 | **Identity rename touches asserted strings** in `tests/reliability/test_notify.py`. | Mechanical, but update tests in the same commit. |
| 8 | **MCP reachability.** `MCPConfig.enabled` is `True` by default. | MCP tools must never enter the repair agent's allowed-tool set; keep the toolsets disjoint. |
| 9 | **Scheduled tasks inheriting authority.** `TaskScheduler._execute_task` runs generic tasks. | Bind every task to an authority level at creation; default READ. §10 of the brief requires this. |
| 10 | **Resource contention** on 2 cores (see §8). | Reliability gets priority; heavy jobs are niced and serialized. |

None of these require weakening the reliability model. #1 and #5 are the two where a careless
implementation would, so they deserve explicit tests.

---

## 10. What I recommend implementing first

**Phase 1, in this order:**

1. **Identity (§0).** Small, entirely user-facing, zero risk — rename the `JARVIS`/`ALERT`
   headers in `notify.py` to plain language, keep JARVIS internally. Good first commit because
   it is visible and provably harmless.
2. **Authority model (§31).** Implement the capability labels our own security document already
   specifies, deny-by-default, layered *in front of* the existing config gates. Wire
   `CAPABILITY_DENIED` to the audit log.
3. **Capability registry (§30).** Derived from config + live checks. This is the mechanism that
   makes "never claim a capability that isn't configured" enforceable rather than aspirational,
   and both the dashboard and the natural-language answers must read from it.
4. **Wiz self-diagnostics (§29).** Split Wiz health from Wize health so a broken optional
   subsystem is visibly *not* a production problem.
5. **Unified brain (§1)**, read/safe verbs only, on the enumerated verb table.
6. **Control Center restructure (§16)** to surface 3–5.

**Why this order:** items 2 and 3 are load-bearing for all 40 features — every later feature
either asks "am I allowed?" or "is this configured?". They are also the two places where a
mistake is dangerous rather than merely annoying, so they should be built while the surface
area is still small. Items 1 and 4 are cheap, visible wins that make the system feel like Wiz
immediately.

**What I recommend deferring:** Home Assistant (§24, no hardware configured), calendar/email
(§19–20, no accounts connected — build the registry entry that says NOT CONFIGURED, not the
integration), and anything requiring torch.

**Standing constraint for every phase:** implementing a feature never enables new
production-changing authority. Repair-to-PR stays on; deploy, push-to-main, merge and Supabase
writes stay off until explicitly turned on, one at a time, with evidence.
