# PLAN.md — Voice AI Assistant Builder

## Revision Notes
Folded three edge cases (identified during Stage 3 stress-testing) directly into the phases so this document stands alone: typed tool-result branching + not-found handling → Phase 2; persisted run state for SSE reconnect → Phase 3. Previously these lived only in CLAUDE.md.

Post-review revision: boundary validation moved from Phase 4 into Phase 1 (guards ship with the feature, not as later hardening); the LLM repair attempt demoted to a stretch goal gated on measured invalid-output rate; edit-path fallback to whole-spec regeneration made an explicit Phase 3 task; Phase 4 reworded from building error handling to break-testing it.

## Context
A platform with two coordinated agents. A conversational **Builder** turns a user's natural-language description into a structured **Assistant Specification**, and edits an existing spec the same way. A **Runtime** then instantiates an assistant from that spec and runs it through a per-lead sequence — reach → qualify → book — invoking **Tools** fulfilled by swappable **Providers**. Both agents read from and write to a shared **Context Store** (a small "Company Brain") so specs, lead data, and call outcomes accumulate in one place. The build is a 3-day take-home meant to demonstrate product judgment and a convincing end-to-end demo for an AI-engineering role at a multi-agent GTM company — not a production system. Assumed scale: single user, single machine, a handful of demo leads.

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

## Build-Time Unknowns
_Measurements to take during development, not design decisions:_
- Invalid-output rate of whole-spec generation — decides whether the stretch-goal repair attempt is worth adding (boundary validation + clean failure exists from Phase 1 regardless)
- Does the edit agent loop terminate cleanly on realistic requests, or is the hard step cap doing real work? (observe iteration counts)
- Is streamed step latency smooth enough to read as "live," or do steps need deliberate pacing? (measure end-to-end step timing)

## Out of Scope (for now)
- Real telephony/voice, calendar, and CRM integrations — provider seam is ready; simulated for the demo (pending answer to Q3)
- Multiple live assistant archetypes in the demo — schema supports it; demo shows one (pending answer to Q1)
- Tools beyond reach / qualify / book — registry is open; only three registered (pending answer to Q2)
- Learning / compounding across runs (the "gets smarter" loop)
- Auth, multi-user, Postgres, queue-based workers
- Agent frameworks (LangGraph / LangChain) — considered and rejected: the edit loop is a bounded `while` over the Anthropic SDK with an iteration cap and error-feedback recovery (~40–60 lines we control and can explain in review). A framework would hide exactly the judgment the loop is meant to demonstrate, and add version churn and demo-time failure modes for orchestration this scope doesn't need. The registry seam keeps LangGraph a clean later swap if branching/durable runs ever warrant it.
