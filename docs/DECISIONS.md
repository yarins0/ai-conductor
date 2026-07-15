# Design Decisions — Voice AI Assistant Builder

Short log of the key decisions made while scoping this assignment, and why. Full detail lives in `PLAN.md` (what/when) and `CLAUDE.md` (standing implementation rules).

## Target architecture: live voice conversation, non-linear tool invocation

The intended end state was never just a scripted per-lead sequence: the launched assistant should hold a real, spoken conversation with the user and invoke reach/qualify/book as the conversation calls for them ("set up my meeting" → `book` directly), not in a fixed order. That's a substantial build on its own — real-time speech in/out, an agentic tool-calling loop instead of a linear one — so it was staged rather than attempted all at once: Phases 1–5 deliver the scripted spine (structured spec, typed tool results, simulated-then-real providers) as the reliable foundation, and Phase 6 layers the live, non-linear voice experience on top once that spine holds. The three seams below (generic schema, open Tool Registry, swappable Providers) were chosen with this staging in mind — none of them assume a linear runtime, which is what keeps Phase 6 an addition rather than a rewrite.

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

## SSE via a DB-polling generator, not in-memory pub/sub (Phase 3)

The live stream (`GET /api/runs/{id}/stream`) is an async generator that polls the persisted `RunStepRecord` rows every 0.3s and emits new ones, closing when the run reaches a terminal status. Chosen over an in-memory `asyncio.Queue` fan-out because every step is already durable (Phase 2 decision): polling durable state makes reconnect/replay free — each connection replays all steps from row zero, so a dropped-and-reopened stream rebuilds the whole view with no extra machinery. The cost (a sync DB read on the event loop each poll) is negligible at single-user scale; a queue would add subscribe-after-start replay logic and cleanup for no benefit here.

## Edit-loop fallback trigger: cap-hit or unrecoverable → whole-spec regenerate (Phase 3)

`edit_spec` falls back to the create path (whole-spec regeneration from a synthesized description) in exactly three cases: the hard iteration cap (`MAX_EDIT_ITERATIONS = 8`) is exhausted without a `finish`, the model returns a working copy that fails validation, or the Anthropic call raises mid-loop. Invalid *tool arguments* do **not** trigger the fallback — they're fed back as error strings so the model self-corrects (rule #4). This keeps the fallback for genuine dead-ends only, so a normal edit stays a targeted mutation while a stuck loop still always yields a valid spec.

## Frontend JS split out to `static/app.js` (Phase 3)

The Session panel + run/stream/edit logic pushed `static/index.html` to ~570 lines, over the project's 500-line cap. Split the `<script>` body into `static/app.js` (referenced via the existing `/static` mount). The "single static file, no build step" decision is kept in spirit — still vanilla JS, no bundler, no framework; the split is mechanical and `index.html` stays markup+CSS only.

## Phase 4 break-testing: force the failure modes, assert the existing guards (Phase 4)

Phase 4 verifies the guards Phases 1–3 built, it does not add new ones. The "break-testing pass" and the "few targeted tests" collapsed into one deliverable: three unit tests that each force a failure mode and assert the guard fires — create-path invalid LLM output → `BuilderError` (the boundary validation itself, previously only covered at the API 422 layer), a provider raising mid-run → run persists an error step and finishes `failed`, and an Anthropic error mid-edit-loop → whole-spec regeneration fallback. Plus one live cold-demo pass (404s, edit, all three lead branches) for end-to-end confidence.

## `_finalize` invalid-final-spec path left untested (Phase 4)

`edit_spec`'s `_finalize` fallback (regenerate if the edited working copy somehow fails validation) is unreachable via normal tool inputs — every mutation already re-validates in `_apply_mutation` before it's applied, so the working copy is valid by construction when `_finalize` runs. It's kept as defense-in-depth but left untested rather than contriving an unreachable state to exercise it (YAGNI on the test).

## Scripted walkthrough lives in the README, not a separate script (Phase 4)

The Phase 4 "scripted happy-path walkthrough" is a section of the README (UI steps + equivalent curl) rather than a runnable `scripts/demo.py`. A reviewer reads the walkthrough where they already are, there's nothing extra to maintain or keep in sync with the routes, and the three deterministic seeded leads already make the happy path and both failure branches reproducible on demand.

## Repair stretch-goal cut on measured 0% invalid rate (build-time unknowns)

The Phase 1 plan gated a one-shot repair attempt (feed the validation error back once) on the measured invalid-output rate of whole-spec generation. Measured it via `scripts/measure/measure_unknowns.py`: 0/8 descriptions — including a prompt-injection and an unmappable "taxes and pizza" prompt — produced an invalid spec (the off-topic ones came back as valid empty-tool specs, not validation failures). At a 0% rate the repair path would be dead code, so it's **cut, not deferred**: the existing boundary guard (clean `BuilderError`, never persist an invalid spec) is the whole story. The same run confirmed the edit loop's `MAX_EDIT_ITERATIONS = 8` cap is pure headroom (edits finish in 2–3 LLM turns, 0 fallbacks) — left as-is, a safety net that correctly never trips on realistic requests.

## Live session moved from an in-page panel to its own window (UI)

The live session (lead picker + Run + streamed steps + outcome) was a right-hand column inside the builder; it's now a standalone page (`static/session.html` + `session.js`) opened by a "Launch assistant" button in the builder header, per window with `?spec=<id>`. Rationale: the builder (author a spec) and the runtime view (watch it act) are two different jobs — separating them lets you run an assistant in one window while editing in another, and each window names the assistant it's running (fetched by id) so multiple live sessions stay legible. The run/stream JS was relocated verbatim (same endpoints, same reconnect-replay behavior), not rewritten; the session window carries its own small `el` helper rather than importing the builder's `app.js`.

## Phase 5 planned as additive, not a replacement — simulated stays the default (Phase 5)

Phase 5 (real providers, user-editable leads) deviates from the original 3-day scope, which listed both under *Out of Scope*. It is planned as strictly **additive** because the test suite depends on the simulated providers and the three seeded demo leads (`test_tools.py`, `test_store.py::test_seed_leads_creates_one_per_sim_profile`, `test_runtime.py` all read `sim_profile`). Consequences: real providers register **behind a mode flag that defaults to simulated**, so CI and a cold demo never need live credentials; the seeded leads stay and user-added leads coexist with them; and `sim_profile` is **gated, not dropped** — kept nullable and demo-only (populated for seeded leads, null for real ones, ignored by real providers) rather than removed, since removing it would break the tests and the simulated path it drives.

## Provider registry is per-tool, keyed by provider id, with a marked default (Phase 5)

The seam went from one provider per tool (`dict[str, Provider]`, last-write-wins) to `dict[str, dict[str, Provider]]` (tool → provider id → Provider) plus small `DEFAULT_PROVIDER` / `PROVIDER_LABELS` maps, so a run can pick which provider fulfills each service. `get_provider(name, provider_id=None)` resolves the default when no id is passed — deliberately, so the single-arg `get_provider("reach")` still returns the default instance the tests monkeypatch and the Runtime uses when no selection is sent. Chosen over a flat `(tool, id)` key so the default lookup and the dropdown payload (`list_providers()`) fall out of one nested dict.

## Real providers are credential-gated stubs, not live integrations (Phase 5)

Each real adapter (`app/providers_real.py`: Twilio reach, HubSpot qualify, Google Calendar book) declares its required env vars; `check_credentials()` raises `ProviderConfigError` when any is missing, and `execute()` is a marked integration point that raises `NotImplementedError` until wired. This demonstrates the seam and the hard-fail behavior without shipping telephony/CRM/calendar calls a 3-day demo can't actually exercise — selecting a real provider without credentials fails cleanly; wiring the real SDK later touches only `execute()`.

## Credential check is a preflight at run creation, not inside the run (Phase 5)

`POST /api/runs` resolves the selected provider for every tool and calls `check_credentials()` **before** creating the run — a missing credential or unknown provider returns 400 and no run/stream is ever started. This is distinct from the in-run `unknown_tool` branch (a provider that resolves but a tool that doesn't), which stays as a safety net for direct `execute_run` calls. Rationale: a doomed run should never appear in the Company Brain or open a dead SSE stream; failing at the boundary keeps the run log honest.

## `sim_profile` gated by nullability, not a DEMO_MODE flag (Phase 5)

`LeadRecord.sim_profile` became `str | None = None` — an explicitly optional, demo-only annotation the simulated providers read and real providers ignore, rather than a required field the domain model forced onto every lead. No separate `DEMO_MODE` env flag was added: the per-tool provider selection plus credential preflight already *is* the sim-vs-real switch, so a flag would be redundant machinery. Seeded demo leads still set the profile (tests depend on it); user-added real leads may omit it. (Caveat: an existing dev `ai_conductor.db` created the column `NOT NULL`; the throwaway file must be recreated once for the nullable schema — `seed_leads()` repopulates.)

## Providers organized one module per tool, not split sim-vs-real (Phase 5)

The initial cut split providers by kind — `app/providers.py` (all simulated) and `app/providers_real.py` (all real). Reorganized into an `app/providers/` package with one module per service (`reach.py` / `qualify.py` / `book.py`), each holding that tool's simulated *and* real adapter side by side. Rationale: a reviewer (or a future integrator wiring up Twilio) reads everything about one service in one place, and adding a tool is one new file, not edits to two parallel files that would drift. The shared credential-gated base (`CredentialGatedProvider`) moved to `app/tools.py` next to `Provider`, so each service module just subclasses it. `STEP_DELAY_SECONDS` stays a package-level attribute so all three simulated providers share one monkeypatchable knob (the tests patch `app.providers.STEP_DELAY_SECONDS`). Importing `app.providers` still registers everything as a side effect — the external contract is unchanged.

## Provider contract + registry moved out of app/tools.py into app/providers/base.py (Phase 5)

Follow-up to the reorg above: `app/tools.py` had become *only* provider/registry code (the `Provider` hierarchy, `CredentialGatedProvider`, `ToolResult`, `ProviderConfigError`, and the `register/get/list` registry), so it was misnamed and misplaced. Moved all of it into `app/providers/base.py` and deleted `tools.py`; consumers now import from `app.providers`. It had to move as a unit, not piecewise: leaving the registry in `tools.py` while moving the `Provider` class into the package would make `tools.py` import from `app.providers`, whose `__init__` imports the service modules, which import `register_provider` back from the still-loading `tools.py` — a cycle. Keeping the whole contract+registry in `base.py` (which depends only on `app.db`) preserves a one-directional `base ← service modules` graph. The service modules import from `app.providers.base` (not the package) to avoid re-entering the package `__init__` mid-registration.

## What was deliberately left out (documented, not forgotten)

Real telephony/calendar/CRM integrations, multiple live assistant archetypes in the demo, tools beyond the three registered, cross-run learning, auth/multi-user, and a production-grade queue/DB. Each is a natural next step once the platform proves out — all sit cleanly behind the seams above, so extending later doesn't require rearchitecting.

## Real SDK wiring un-scoped: execute() now calls Twilio/HubSpot/Google Calendar for real (post-Phase 5)

The "credential-gated stubs, not live integrations" entry above scoped every real provider's `execute()` as a `NotImplementedError` stub for the 3-day demo. That call is reversed here, at the user's request with budget available to actually exercise the integrations. Twilio and HubSpot are plain `httpx` REST calls (HTTP Basic Auth and a Bearer token respectively — no new dependency); Google Calendar adds one new dependency, `google-auth`, solely to mint a short-lived OAuth token from a service-account key file, since hand-rolling that signing would be a security anti-pattern. Two real-world ceilings are worth flagging: Twilio's outcome is `"initiated"`, not `"answered"`/`"no_answer"`, because that distinction only arrives later via a status-callback webhook this project doesn't implement (the Runtime treats any non-`no_answer` outcome as "continue," so this doesn't change branching); and HubSpot's `intent_score` is read from a portal-specific custom contact property, since real predictive lead scoring is a paid HubSpot feature not reachable via a plain API call, falling back to a fixed qualifying score when the property doesn't exist.

**Migration caveat actually hit, and fixed.** The dev `ai_conductor.db`'s `leadrecord.sim_profile` column was still `NOT NULL` (created before Phase 5 made it nullable) — the first real (non-seeded) lead insert failed with an `IntegrityError`. Fixed by migrating the table in place (SQLite has no `ALTER COLUMN`, so: create a new table with the nullable column, copy all rows, drop and rename) rather than deleting the dev database, since that would have destroyed real spec/lead history built up across sessions. A timestamped backup was taken first. First live run through the real Twilio + HubSpot providers (against a real phone number and a real HubSpot portal) confirmed the full reach → qualify → book path end-to-end: real call placed, real HubSpot contact created and read back, lead status/intent score/booked slot all persisted correctly.

## Live voice as a second execution mode alongside the linear Runtime, not a generalization (Phase 6)

Phase 6's non-linear/live mode is a **sibling** of `runtime.execute_run`, not a rewrite of it: a WebSocket endpoint (`/api/live/{spec_id}`) drives a new `app/live_agent.py` (`LiveSession.handle_turn`), and the linear Runtime is left byte-for-byte untouched. Chosen over generalizing one runtime to cover both modes because the scripted spine is the demo's reliability guarantee ("the demo must never break") — a generalization would put the proven path at risk to serve the experimental one. The two modes share everything that matters *below* the control flow: the same tool primitive (`get_provider().execute()` → `ToolResult`), the same `db.add_run_step` + `update_lead_outcome` write-back, and the same run row — so a live conversation accumulates in the Company Brain identically to a scripted run, with no duplicated persistence logic. The loop itself is the Phase 3 edit-loop pattern (bounded `while` over the Anthropic SDK, tool results fed back, hard iteration cap), so no orchestration framework was added.

## Live voice pipeline: browser Web Speech API, not a realtime voice service (Phase 6)

Speech-to-text and text-to-speech run in the browser via the Web Speech API (`SpeechRecognition` + `SpeechSynthesis`, `static/voice.js`); the backend only ever sees text turns over a WebSocket. Chosen over a hosted realtime voice API (OpenAI Realtime / Gemini Live) or a server-side STT+TTS pipeline: zero new dependencies, no API keys, no audio-streaming infrastructure, and — the deciding factor — it keeps the interesting engineering (the model's non-linear tool-choice reasoning) in *our* loop rather than outsourcing it to a vendor's hosted agent. Known ceilings, accepted for a take-home demo and named in the README with their upgrade path: Chrome/Edge only, serialized turn-taking (no barge-in), and browser-dependent TTS voice quality. A typed-input fallback drives the exact same backend loop, so the conversation still works if STT misbehaves or the browser is unsupported. Two guards make the serialized turn-taking reliable: the mic is stopped while TTS speaks (or it transcribes the assistant's own voice), and a server-sent `turn_done` event gates re-listening until a full turn — which may chain several tool calls and spoken fragments — completes.

## Model-authored tool args ride in via the settings dict, no provider signature change (Phase 6)

The live agent lets the model author tool arguments from the conversation (e.g. a requested meeting time), but providers keep their `execute(lead, settings)` signature: the model's arguments are merged into the per-tool `settings` dict at call time (`{**spec_settings, **model_args}`). Chosen over adding a separate arguments parameter to every provider because `settings` is already the open per-tool config seam — the simulated providers ignore the extra keys, and real ones (e.g. Twilio's `twiml_url`) can read them, with no change to the Provider contract or the existing Runtime. The LLM-facing tool schemas (`app/tools_manifest.py`) are hand-written rather than derived, since a provider's open `settings` dict has no per-tool argument schema to derive from; only the tools present in a spec are exposed to the model, keeping the "allowed tools = the spec's tool list" seam intact.

## `requests` added to requirements.txt (Phase 6 incidental)

`app/providers/book.py` imports `google.auth.transport.requests`, whose transport requires the `requests` package — but `requests` was not in `requirements.txt` (google-auth does not depend on it), so a fresh clone's `pip install -r requirements.txt` would fail to import the app at startup. Added `requests` to close the gap. A latent break from the post-Phase-5 real-SDK wiring, surfaced now because the live-voice WebSocket handler imports the full provider chain.

## `reach` places a call that actually happens: two agents, not a canned outcome (post-Phase 6)

`SimulatedReachProvider` used to sleep and return a hardcoded `"they picked up"`. That made the demo's central claim hollow — the assistant truthfully reported what its tool returned, but nothing had happened, there was no conversation to inspect, and `qualify` then scored a call that did not exist with a constant `85`. Reversed here: `app/sim_lead.py` runs a real bounded conversation between two agents — the assistant, speaking from its own spec (the artifact the Builder wrote), and the lead, played by a second model whose stance comes from `lead.sim_profile` — and `reach` returns the transcript. `sim_profile` still decides *who picks up*, so the no-answer branch stays deterministic for the demo; what gets *said* is no longer scripted. Cost/latency are managed by putting the cheap model on the lead side (it holds a persona, not reasoning) and capping the call at 4 exchanges, which is a real bound on spend, not just on transcript length.

Chosen over the alternative reading of the same complaint — swapping the browser Web Speech layer for a hosted realtime speech-to-speech model — because the two address different surfaces. Web Speech governs *operator ↔ assistant*; the missing conversation was *assistant ↔ lead*. No realtime-API swap would have produced a lead conversation, so the entry above ("browser Web Speech API, not a realtime voice service") stands unreversed. The lead call is deliberately text-only: a transcript is more inspectable in a review than audio, and this is the Alta thesis stated plainly — coordinated agents that actually act, with the accumulation visible in the Brain.

Two consequences worth naming. The spec now reaches providers through the open `settings` dict (`{**tool.settings, "spec": spec}`, injected by both `Runtime` and `LiveSession`) — the same seam the model-authored-args entry above established, so again no Provider signature change; a `reach` with no spec returns an error rather than inventing an outcome, since a canned success is exactly what this removed. And `reach`'s `summary` is now multi-line, carrying the dialogue, because the live agent is fed `result.summary` as its tool result — a headline alone would leave the assistant narrating a call it could not read.

## `qualify` scores the transcript; the intent score is earned, not hardcoded (post-Phase 6)

With a real call to judge, `SimulatedQualifyProvider` now scores intent from the transcript via forced tool use (the same structured-output mechanism as the Builder's create path), returning a 0-100 score plus a one-sentence reason citing what the lead said. The transcript reaches it the same way the spec reaches `reach`: threaded through `settings` by the caller, mirroring how the Runtime already carried `intent_score` across tools. Without a transcript — `qualify` called standalone, which the live agent may legitimately do — it falls back to the old `sim_profile`-derived score, so the tool still works in isolation. The model's number is treated as untrusted input and clamped to 0-100 (implementation rule #2); a malformed score becomes an error `ToolResult`, never a crash.

**Test consequence, flagged because it bit during the build.** `reach` doing real work meant `tests/test_runtime.py` and `tests/test_tools.py` silently started making live API calls — they passed, but against the real Anthropic API, at real cost, taking 71s. Both are now driven by a shared fake Anthropic client (`tests/conftest.py`, extracted from the private copy in `test_live_agent.py` now that four test modules need it); the suite is back to ~3s and free. One test's meaning shifted and the comment says so: `test_not_qualified_lead_stops_after_qualify` no longer follows from the lead's `sim_profile`, because once a transcript exists the *score* drives that branch.

## One assistant, two system prompts, named for their audience (post-Phase 6)

`live_agent._system_prompt` opened with "You are {name}, a voice assistant on a live phone call" — true in Phase 6, when the only conversation was the operator's, but wrong once `reach` started holding actual lead calls: the operator conversation is *about* leads, not *with* one. Renamed to `_operator_system_prompt` and reframed to say so explicitly, as the sibling of `sim_lead._assistant_system_prompt` (the same assistant, on a call with a lead). The naming is the point — with two conversations in play, a prompt named for nothing in particular is how they get conflated.

The reframing also scopes the spec's `objective` and `instructions` as lead-work rules rather than rules for the conversation at hand. Applied verbatim, an instruction like "never promise a discount" or an objective like "book a 20-minute intro call" reads as something to pursue *with whoever is talking* — i.e. the assistant pitching its own operator. Same spec fields, still no second copy of them; only the framing differs by audience.
