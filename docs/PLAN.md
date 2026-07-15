# PLAN.md — Voice AI Assistant Builder

## Revision Notes
Folded three edge cases (identified during Stage 3 stress-testing) directly into the phases so this document stands alone: typed tool-result branching + not-found handling → Phase 2; persisted run state for SSE reconnect → Phase 3. Previously these lived only in CLAUDE.md.

Post-review revision: boundary validation moved from Phase 4 into Phase 1 (guards ship with the feature, not as later hardening); the LLM repair attempt demoted to a stretch goal gated on measured invalid-output rate; edit-path fallback to whole-spec regeneration made an explicit Phase 3 task; Phase 4 reworded from building error handling to break-testing it.

## Context
A platform with two coordinated agents. A conversational **Builder** turns a user's natural-language description into a structured **Assistant Specification**, and edits an existing spec the same way. The target end state is a live, spoken conversation with the launched **Assistant**: the user talks to it directly, and it invokes **Tools** (reach / qualify / book, fulfilled by swappable **Providers**) non-linearly, based on what the conversation calls for ("set up my meeting"), rather than a fixed order. The 3-day build delivers this in stages: the scripted per-lead reach → qualify → book sequence ships first as the reliable spine, with the live two-way voice layer and non-linear tool invocation following once that spine is proven. Both agents read from and write to a shared **Context Store** (a small "Company Brain") so specs, lead data, and call outcomes accumulate in one place. The build is a take-home meant to demonstrate product judgment and a convincing end-to-end demo for an AI-engineering role at a multi-agent GTM company — not a production system. Assumed scale: single user, single machine, a handful of demo leads.

## Decisions Made
- **Stack: Python / FastAPI, single service (monolith) + light frontend** — one language, one deploy, fastest path to a working demo; nothing here needs to be distributed.
- **Spec representation: Pydantic-modeled JSON** — validation and typing for free, and the auto-generated JSON Schema doubles as the contract handed to the Builder's LLM. Modeled generically (open-ended `tools` list, generic fields) so one demo archetype is just one instance.
- **Builder mechanism: hybrid** — *create* uses whole-spec structured output (one validated LLM call); this also serves as the reliability fallback. The *edit* path uses a tool-calling agent loop (incremental mutations), chosen deliberately as the agentic capability most relevant to a multi-agent shop. A-spine ships first so the demo can't break; B layers on top.
- **Runtime: async in-process** — `asyncio` tasks inside FastAPI with server-sent events streaming each step to the UI. No Celery/Redis/workers (YAGNI at this scale); structured so a step *could* be dispatched to a worker later.
- **Persistence: SQLite via SQLModel** — file-backed, no external service, real inspectable tables for specs, leads, and outcomes (which makes the "Company Brain" story demonstrable). ORM keeps a SQLite → Postgres swap trivial if it ever went to production.
- **Three open product questions absorbed by seams** — generic schema (assistant types), open Tool Registry (additional tools), and simulated Providers behind fixed interfaces (real vs. simulated). Defaults pending answers: one demo archetype, the reach/qualify/book tool set, simulated providers. Concrete answers only decide which concretions to implement, not structure.

## Stack
Python + FastAPI (async) backend as a single service, SQLite via SQLModel for persistence, an LLM for the Builder (structured output + tool-calling), and a light frontend (chat panel for the Builder plus a live session view) communicating over REST + SSE.

## System Components

| Component | Responsibility | Key Dependencies |
|---|---|---|
| Builder | Turns NL into a spec (structured output) and edits a spec (tool-calling loop) | LLM, Assistant Spec, Spec Store |
| Assistant Spec | The config artifact: objective, behavior, allowed tools | Pydantic |
| Spec Store | Persists and updates specs | SQLite / SQLModel |
| Context Store ("Company Brain") | Shared state: leads, call outcomes, qualification results, booked slots | SQLite / SQLModel |
| Runtime | Instantiates an assistant from a spec; drives reach → qualify → book; emits step events | asyncio, Tool Registry, Context Store |
| Tool Registry | Open set of tools an assistant may invoke | Provider Layer |
| Provider Layer | Fulfills each tool; simulated now, real integrations later (telephony, calendar, CRM) | (simulated) |
| Event Stream | Streams live run progress to the frontend | FastAPI SSE |
| Frontend | Builder chat + live session view of a run and its outcomes | REST + SSE |

## Implementation Phases

### Phase 1 — Foundation (spec + create path)
**Goal**: A user can describe an assistant in chat and get a validated, persisted spec.
**Tasks**:
- [x] Define Assistant Spec Pydantic models (objective, behavior, open `tools` list); export JSON Schema
- [x] Stand up FastAPI service + SQLite/SQLModel; Spec Store CRUD
- [x] Builder *create* path: LLM structured output constrained to the schema; validate at the boundary — persist on success, fail cleanly with a clear message on invalid output (a one-shot repair attempt — feed the validation error back once — is a stretch goal, added only if the invalid-output rate warrants it)
- [x] Minimal frontend: Builder chat panel that creates a spec and displays it
**Exit criteria**: Describe an assistant in natural language → a schema-valid spec is saved and visible. ✅ Verified live (real Claude call → schema-valid spec persisted and listed).

### Phase 2 — Runtime, tools & shared context (the actions)
**Goal**: A saved assistant can run end-to-end against a lead, with outcomes written back to the Company Brain.
**Tasks**:
- [x] Tool Registry + Provider Layer interfaces; simulated providers for reach / qualify / book
- [x] Each tool returns a typed result (success / failure / outcome); Runtime branches on it rather than assuming success (e.g. "no answer" is a normal outcome, not a crash)
- [x] Runtime orchestrator: reach → qualify → book, driven by the spec's tool list
- [x] Context Store: seed demo leads; write call outcomes, intent/qualification, booked slot back after every run
- [x] Explicit not-found handling for missing spec/lead lookups (clean error, never a 500)
- [x] Name real drop-ins in code comments/README (telephony, calendar, CRM adapters)
**Exit criteria** ✅: Trigger a run for a lead → the sequence executes (including a simulated failure branch) and outcomes persist and are inspectable.

### Phase 3 — Live experience + edit path (the flex)
**Goal**: The demo feels alive, and assistants can be edited by chatting.
**Tasks**:
- [x] SSE streaming of each run step to a live session view (`GET /api/runs/{id}/stream`; simulated providers' 0.5s step delay gives the live pacing — no artificial pacing added)
- [x] Persist run state to the Context Store as each step completes, so the session view can rebuild from storage on stream drop/reconnect rather than relying on the stream alone (already satisfied in Phase 2; the stream replays persisted rows on every connect)
- [x] Builder *edit* path: tool-calling agent loop (`set_objective`, `set_persona`, `set_name`, `add_tool`, `remove_tool`, `set_instructions`, `finish`) with a hard step cap (`MAX_EDIT_ITERATIONS = 8`) and error-feedback recovery on invalid tool arguments
- [x] Edit fallback: if the agent loop fails (cap hit, invalid final spec, or LLM error), regenerate the whole spec via the create path — the edit demo never dead-ends
- [x] Frontend: right-hand Session panel with lead picker + Run, live step stream, reconnect control, and final lead outcome badge
**Exit criteria** ✅: Edit an existing assistant via chat *and* watch a run stream step-by-step to completion, surviving a simulated reconnect. (SSE live/replay/branching/404 verified live via curl; edit loop covered by stubbed unit tests — normal edit, invalid-arg recovery, cap→fallback.)

### Phase 4 — Hardening & Launch
**Goal**: Robust enough to demo cold, and legible to a reviewer.
**Tasks**:
- [x] Break-testing pass: force invalid LLM output, provider failures, and a non-terminating edit request; verify each fails the way Phases 1–3 built it to (clean validation error, outcome branch, step cap + create-path fallback) — this phase verifies guards, it does not build them
- [x] Seed a clean demo dataset and a scripted happy-path walkthrough (3 deterministic leads already seeded; walkthrough lives in the README)
- [x] README: architecture, the three seams, decisions + tradeoffs (esp. A-create/B-edit and sync-vs-async), and "what I'd do next" (real providers, multi-archetype, learning/compounding)
- [x] A few targeted tests around spec validation and the runtime sequence
**Exit criteria** ✅: Fresh clone → documented steps → full describe → generate → edit → run demo works. (47 tests passing; 404s + full booked branch re-verified live; break-test guard paths covered by the new tests.)

### Phase 5 — Per-tool selectable providers, user leads & sim_profile gating
**Staged delivery note**: real integrations and user-editable leads were always the target (see Context); this phase is where they land, once the scripted spine from Phases 1–4 was proven stable. Logged in DECISIONS. **Hard constraint**: the simulated providers and the three seeded demo leads stay and remain the **default** — the test suite depends on them (`test_tools.py`, `test_store.py::test_seed_leads_creates_one_per_sim_profile`, `test_runtime.py`), and keeping simulated-default means CI and a cold demo never need live credentials. Everything below is **additive**, not a replacement.

**Goal**: An assistant can run against real telephony/CRM/calendar integrations and against user-added leads, without breaking the simulated demo path or the tests.

**Tasks**:
- [x] **5a — Per-tool selectable providers + credential preflight.** The registry became nested (`TOOL_REGISTRY: dict[str, dict[str, Provider]]`, tool → provider id → Provider) with a marked default per tool; `get_provider(name, provider_id=None)` resolves the default when no id is passed (keeps the single-arg call the tests rely on). Both the simulated provider (id `sim`, default) and the real credential-gated adapter (Twilio reach, HubSpot qualify, Google Calendar book) for each tool live together in the `app/providers/` package — one module per service (`reach.py` / `qualify.py` / `book.py`), each registering its two providers at import — so every tool exposes ≥2 providers. The session window renders one dropdown per tool (`GET /api/providers`) and sends a per-run `providers` selection map into `POST /api/runs`; `runtime.py` threads it through and still branches on `result.outcome` only. A **credential preflight** in `create_run` resolves the selected provider for each tool and calls `check_credentials()` **before** any run row is created — a missing credential or unknown provider returns 400 and no run/stream starts. Real adapters' `execute()` is a marked integration point (raises until wired); preflight blocks a creds-less selection so it never runs in the demo.
- [x] **5b — User-editable leads, alongside the seeded fixtures.** New `POST /api/leads` (name / company / phone; `sim_profile` optional, demo-only) with boundary validation via `CreateLeadRequest`; `db.create_lead()` mirrors `create_run`. "Add lead" form in the session window (`static/session.html` / `session.js`) refreshes `loadLeads()` after. The idempotent seed and the three demo leads are untouched; user leads coexist with them.
- [x] **5c — Contain the `sim_profile` leak (gate, don't drop).** `LeadRecord.sim_profile` became `str | None = None` — an explicitly optional, demo-only annotation the simulated providers read and real providers ignore, rather than a required field forced onto every lead. No separate `DEMO_MODE` flag: the per-tool provider selection + credential preflight already is the sim-vs-real switch. Seeded demo leads still set it (tests depend on it); user-added real leads leave it null.

**Exit criteria** ✅: All 47 tests stay green unchanged and the cold demo runs credential-free on the simulated default; picking a real provider without its env vars returns a clean 400 and creates no run (verified end-to-end: `Twilio Voice needs TWILIO_ACCOUNT_SID`, run count unchanged); a user-added lead with no `sim_profile` persists as `null`.

### Phase 6 — Live voice conversation & non-linear tool invocation
**Goal**: The end state described in Context — the user talks to a launched assistant directly (real speech in/out), and the assistant invokes reach / qualify / book non-linearly based on what the conversation calls for, rather than the fixed Phase 2 sequence.
**Tasks**:
- [x] Real-time audio pipeline: **browser Web Speech API** — `SpeechRecognition` (STT) in, `SpeechSynthesis` (TTS) out, in `static/voice.js`. Audio never leaves the browser; the backend runs the text loop over a WebSocket. Chrome-only, serialized turn-taking (no barge-in); a typed-input fallback drives the same loop everywhere. See DECISIONS.
- [x] Agentic tool-calling loop over the conversation (`app/live_agent.py`, `LiveSession.handle_turn`): a bounded `while` over the Anthropic SDK — the model decides which registered tool to invoke and when, from open-ended user speech. Modeled on `builder.edit_spec()`; reuses `get_provider().execute()` and persists steps + lead write-back to the Company Brain exactly like a scripted run.
- [x] Reconcile with the existing linear `Runtime`: **second execution mode alongside it** — a WebSocket endpoint (`/api/live/{spec_id}`) drives `LiveSession`; `runtime.execute_run` is untouched. Chosen over generalizing one runtime so the reliable spine can't regress. See DECISIONS.
- [x] Frontend: live voice UI in the session view — a "Talk" button, mic toggle, transcript, and typed fallback in `static/session.html` + `static/voice.js`, reusing the existing layout/theme, provider dropdowns, and step-card renderer.
**Status**: **shipped.** Runs credential-free on the simulated providers (no Twilio/HubSpot/Google needed for the live demo). Backend covered by `tests/test_live_agent.py` (loop logic, mocked LLM) and the WebSocket tests in `tests/test_api.py`; the live spoken turn is a manual check (needs a mic + `ANTHROPIC_API_KEY`).

**Superseded by Phase 7**: the Web Speech + Claude live-loop pipeline described above (`app/live_agent.py`, `app/tools_manifest.py`, `static/voice.js`, `/api/live`) was replaced by OpenAI Realtime and deleted; see DECISIONS.

### Phase 7 — Real voice (OpenAI Realtime), lead/settings management
**Goal**: Replace the Web Speech + Claude live-loop demo pipeline with real voice on both surfaces (operator ↔ assistant, assistant ↔ lead), and give the builder page the lead/provider management the earlier phases deferred.
**Tasks**:
- [x] Leads: `email` / `notes` fields, `PATCH`/`DELETE /api/leads/{id}`, `GET /api/leads?q=` search, and a Manage-leads dialog on the builder page (create/edit/delete, notes editable anytime and fed into every future call's instructions).
- [x] Persisted per-tool provider settings (`SettingsRecord`, `GET`/`PUT /api/settings`) as the baseline default for every session, with a Settings dialog on the builder page; an explicit per-request selection still wins.
- [x] Realtime control plane for the operator surface (`app/realtime.py`): `POST /api/realtime/token` mints an ephemeral WebRTC session token scoped to the spec's tools; `POST /api/realtime/tools/{tool_name}` executes one tool call statelessly (`app/tool_exec.py`, run found-or-created per spec/lead pair); `POST /api/realtime/web-search` bridges the Realtime session's function calling to the Responses API's built-in web_search tool.
- [x] WebRTC session UI replacing Web Speech: the session window connects directly to OpenAI over WebRTC (audio never touches our server for this surface), the standalone Run button is removed in favor of a live conversation that picks tools non-linearly, and lead selection goes mid-conversation (`list_leads` / `request_lead` functions) instead of a pre-run dropdown — a hybrid with the builder still able to preset a lead.
- [x] Twilio Media Streams bridge for real phone calls (`app/realtime_bridge.py`): `TwilioReachProvider` points a placed call at `/twilio/voice/{run_id}`, which connects Twilio's Media Stream to a second OpenAI Realtime session server-side; both legs speak `g711_ulaw`, so the bridge is a byte-for-byte relay with no audio conversion. A status callback (`/twilio/status/{run_id}`) resolves the busy/no-answer/failed cases the async `Calls.json` response can't.
**Exit criteria**: A spec with `reach`/`qualify`/`book` runs entirely by voice — talk to the assistant over WebRTC, it picks a lead and calls a tool non-linearly, and (with `PUBLIC_BASE_URL` + Twilio credentials) `reach` places a real phone call that itself is a live voice conversation, both outcomes written back to the Company Brain the same as the scripted spine.

## Build-Time Unknowns
_Measurements taken during development, not design decisions. Measured via `scripts/measure/measure_unknowns.py` against `claude-sonnet-5`._
- **Invalid-output rate of whole-spec generation — measured 0/8 (0%).** ✅ All descriptions (incl. two adversarial/off-topic) produced schema-valid specs; the boundary guard never had to fire. **Decision: the stretch-goal repair attempt is not warranted** — clean-fail + validation is enough (see DECISIONS).
- **Edit agent loop termination — 0/8 fallbacks; 2 LLM turns for normal edits, 3 for the maximal "start over" case; cap = 8.** ✅ The loop finishes cleanly well under the cap. `MAX_EDIT_ITERATIONS = 8` is a pure safety net (generous headroom), not doing real work on realistic requests.
- **Streamed step latency — resolved by construction, no measurement needed.** ✅ The run loop has no LLM in it; every step is a fixed 0.5s provider sleep, so pacing is deterministic and reads as "live" without any deliberate pacing added.

## Out of Scope (for now)
- ~~Live voice conversation + non-linear tool invocation~~ — **shipped in Phase 6** (see above).
- Multiple live assistant archetypes in the demo — schema supports it; demo shows one (pending answer to Q1)
- Learning / compounding across runs (the "gets smarter" loop)
- Auth, multi-user, Postgres, queue-based workers
- Barge-in / interrupt handling in live voice, WebRTC, streaming partial transcripts, server-side audio — Phase 6 uses full-turn text over a WebSocket with browser-side speech; production upgrade path noted in the README.
- **Resolved (Phase 6):** the "committed orchestration framework" question — Phase 6's agentic loop **reused the hand-rolled bounded `while`** over the Anthropic SDK (the Phase 3 edit-loop pattern), not LangGraph/LangChain. The non-linear branching stayed well within a hand-rolled loop, so the iteration-cap-and-recovery judgment remained ours to show in review.
