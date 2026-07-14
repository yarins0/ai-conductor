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
- [ ] Real-time audio pipeline: speech-to-text in, text-to-speech out, wired to a live session
- [ ] Agentic tool-calling loop over the conversation (replaces the linear `Runtime` for this mode): the model decides which registered tool to invoke and when, from open-ended user speech (e.g. "set up my meeting" → `book` directly)
- [ ] Reconcile with the existing linear `Runtime`: either a second execution mode alongside it, or a generalization that covers both — decide once the agentic loop's shape is concrete
- [ ] Frontend: live voice UI (mic capture, playback, turn-taking) in the session view
**Status**: planned; not yet started. Picks up in the next working session against the real Twilio/HubSpot/Google Calendar providers wired in Phase 5.

## Build-Time Unknowns
_Measurements taken during development, not design decisions. Measured via `scripts/measure/measure_unknowns.py` against `claude-sonnet-5`._
- **Invalid-output rate of whole-spec generation — measured 0/8 (0%).** ✅ All descriptions (incl. two adversarial/off-topic) produced schema-valid specs; the boundary guard never had to fire. **Decision: the stretch-goal repair attempt is not warranted** — clean-fail + validation is enough (see DECISIONS).
- **Edit agent loop termination — 0/8 fallbacks; 2 LLM turns for normal edits, 3 for the maximal "start over" case; cap = 8.** ✅ The loop finishes cleanly well under the cap. `MAX_EDIT_ITERATIONS = 8` is a pure safety net (generous headroom), not doing real work on realistic requests.
- **Streamed step latency — resolved by construction, no measurement needed.** ✅ The run loop has no LLM in it; every step is a fixed 0.5s provider sleep, so pacing is deterministic and reads as "live" without any deliberate pacing added.

## Out of Scope (for now)
- Live voice conversation + non-linear tool invocation — the target end state (see Context); staged as Phase 6, after the scripted spine and real providers landed first
- Multiple live assistant archetypes in the demo — schema supports it; demo shows one (pending answer to Q1)
- Learning / compounding across runs (the "gets smarter" loop)
- Auth, multi-user, Postgres, queue-based workers
- A committed orchestration framework for Phase 6 — the Phase 3 edit loop deliberately stayed a bounded `while` over the Anthropic SDK (not LangGraph/LangChain) so the iteration-cap-and-recovery judgment was ours to show in review; Phase 6's agentic tool-calling loop may reuse that pattern or reach for a graph-based framework (e.g. LangGraph) if the non-linear branching outgrows a hand-rolled loop — decided when that phase starts, not before.
