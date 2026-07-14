# Design Decisions — Voice AI Assistant Builder

Short log of the key decisions made while scoping this assignment, and why. Full detail lives in `PLAN.md` (what/when) and `CLAUDE.md` (standing implementation rules).

## Handling assignment ambiguity

The brief left three things open: whether the builder supports one assistant type or many, whether more tools exist beyond call/qualify/book, and whether calling/booking should be real integrations or simulated. Rather than guess, I designed three **seams** so the system doesn't need the answers to be built correctly:

- **Generic, schema-driven Assistant Spec** — one demo archetype is just one instance of an open schema; a general builder is the same schema with more exposed.
- **Open Tool Registry** — reach/qualify/book are registered tools, not hardcoded steps. More tools = more registrations, no runtime change.
- **Swappable Provider interface** — each tool is fulfilled by a Provider; simulated and real implementations are interchangeable behind the same contract.

This meant I could commit to an architecture and start building immediately instead of blocking on a reply, and whichever way the open questions resolve, only concretions change — not structure.

## Stack: Python / FastAPI, single service

Chosen over a split-services or Node/TS setup for a 3-day build: one language, one deployment, no distributed-systems overhead the scope doesn't need.

## Spec representation: Pydantic-modeled JSON

Pydantic models (not a loose dict) give validation for free and auto-generate a JSON Schema that doubles as the contract handed to the Builder's LLM — one artifact, two jobs, and no drift between "what the model should return" and "what the system accepts."

## Builder mechanism: hybrid (whole-spec create + tool-calling edit)

*Create* uses a single structured-output call that returns a full validated spec — simple, reliable, and it doubles as a fallback for editing if needed. *Edit* uses a tool-calling agent loop (targeted mutations) instead of also regenerating the whole spec.

This was a deliberate tradeoff, not a default: whole-spec regeneration alone would have been simpler and lower-risk, but the role is at **Alta**, a company built around coordinated multi-agent systems. Building the edit path as an agent loop demonstrates the exact skill the role is hiring for, while keeping the create path as a reliable spine means the demo can't break even if the loop misbehaves. The known risk (non-termination, invalid tool arguments) is handled with a hard iteration cap and error-feedback self-correction.

## Runtime: async in-process, no queue

The call sequence (reach → qualify → book) streams progress via SSE from `asyncio` tasks inside FastAPI — no Celery/Redis. This gives the "live, acting system" feel that matters for a company whose product tagline is "AI acts" — a synchronous blocking call would undersell that — without the infrastructure cost a real queue would add for no benefit at this scale.

## Persistence: SQLite via SQLModel

Chosen over in-memory storage specifically because of the **shared context layer** (a small version of Alta's "Company Brain"): outcomes need to visibly accumulate and be inspectable across a demo, which an in-memory store loses on restart. SQLite gets real, inspectable tables with zero infrastructure; a Postgres upgrade path exists later via the ORM but isn't needed now.

## Alta-specific framing

Researched Alta's actual product (coordinated agents — Katie, Alex, Luna — sharing one "Company Brain," "AI System of Actions") and folded its idiom into the build rather than treating this as a generic take-home:
- A shared **Context Store** so the Builder and Runtime aren't isolated tools.
- Providers framed as **integration adapters** over external systems (telephony, calendar, CRM), matching how Alta actually sits on top of a company's existing stack.
- The demo's payoff is a visible **action and outcome** (a call happening, an intent score, a booked slot), not just generated text.

## Plan revision after cross-review against CLAUDE.md

A review of PLAN.md against the standing rules surfaced that some guards were scheduled as Phase 4 "hardening" despite the rule that guards ship with the feature. Three changes:

- **Boundary validation moved to Phase 1.** The create path validates LLM output and fails cleanly from day one, not in a later hardening pass. Phase 4 was reworded to *break-test* the guards built in Phases 1–3, not build them.
- **Repair loop demoted to stretch goal** (revises the earlier "one repair attempt" rule). Ship schema validation + clean failure first; add the one-shot repair (feeding the validation error back) only if the measured invalid-output rate shows it's needed. Rationale: validation is the non-negotiable part; the repair is an optimization, and structured-output modes make invalid JSON rare enough that building it speculatively may be wasted time in a 3-day box.
- **Edit fallback made an explicit task.** The "create doubles as the fallback" idea existed in prose but had no task; Phase 3 now wires the edit UI to regenerate the whole spec if the agent loop fails, so the edit demo can never dead-end.

## Builder LLM: Anthropic claude-sonnet-5 (Phase 1)

Chosen over OpenAI or a provider-switchable adapter: one SDK, first-class tool-forced structured output, and no speculative abstraction for a second provider that may never be needed. The model name lives in one constant (`app/builder.py`) so swapping models later is a one-line change.

## Spec generation mechanism: forced tool use, not structured-outputs mode (Phase 1)

The SDK's newer structured-outputs mode (`output_config.format` / `messages.parse`) requires `additionalProperties: false` on every object, which is incompatible with the spec's open `settings` dict — the very field that keeps the tool seam generic. Forced tool use (`tool_choice` pinned to a single tool whose `input_schema` is the Pydantic-derived JSON Schema) has no such restriction, and boundary validation happens on our side via `AssistantSpec.model_validate` regardless (which the implementation rules require anyway).

## Frontend: single static HTML file, vanilla JS (Phase 1)

Chosen over React/Vite: no build step, nothing to break in a live demo, and SSE in Phase 3 is covered by the native `EventSource` API. One `static/index.html` served by FastAPI keeps the "light frontend" constraint literal — the cost is componentization we don't need at this scale.

## Simulated providers are deterministic, not random (Phase 2)

Each simulated provider derives its outcome from the lead's `sim_profile` field instead of rolling dice. Randomness would look more "realistic" but violates the golden rule that the demo must never break: the three seeded leads reliably exercise the booked / no-answer / not-qualified branches on every single run, so the failure branch can be shown on demand in a live review rather than hoped for.

## Run trigger: 202 + poll now, SSE layers on in Phase 3 (Phase 2)

`POST /api/runs` starts the run as an in-process `asyncio` task and immediately returns 202 with the run id; progress is inspectable by polling `GET /api/runs/{id}`. Because every step is persisted as it completes (rule: run state never lives only in a stream), Phase 3's SSE becomes a pure push-notification layer over already-durable state — no rework, and reconnect/rebuild comes for free.

## Run steps as rows, not a JSON column (Phase 2)

Each completed step appends a `RunStepRecord` row rather than rewriting a steps-JSON blob on the run. Append-only rows match how steps actually happen, avoid read-modify-write races on a hot run, and make the run log queryable/inspectable directly in SQLite — which is the point of the Company Brain.

## What was deliberately left out (documented, not forgotten)

Real telephony/calendar/CRM integrations, multiple live assistant archetypes in the demo, tools beyond the three registered, cross-run learning, auth/multi-user, and a production-grade queue/DB. Each is a natural next step once the platform proves out — all sit cleanly behind the seams above, so extending later doesn't require rearchitecting.
