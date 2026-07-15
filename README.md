# AI Conductor — Voice AI Assistant Builder

A two-agent system: a **Builder** turns a natural-language description into a structured **Assistant Spec** (and edits it by chatting), and the assistant that spec describes then *runs* in one of two modes. The **scripted Runtime** (the reliable spine) drives a fixed per-lead sequence — **reach → qualify → book** — invoking tools fulfilled by swappable providers. The **live agent** (the target end state) holds a real spoken conversation and invokes those same tools **non-linearly**, letting the model decide what the conversation calls for. Both read from and write to a shared **Context Store** (a small "Company Brain"), so specs, leads, and outcomes from either mode accumulate in one inspectable place. Built for an AI-engineering take-home at Alta, a coordinated multi-agent GTM company — the point isn't generated text, it's agents that *act* and surface outcomes (a call happening, an intent score, a booked slot).

## Architecture

| Component | Responsibility |
|---|---|
| Builder | NL → spec (structured output); edit spec (tool-calling loop) |
| Assistant Spec | Config artifact: objective, behavior, allowed tools (Pydantic models) |
| Spec Store | Persist / update specs (SQLite) |
| Context Store ("Company Brain") | Shared state: leads, call outcomes, qualification, booked slots (SQLite) |
| Runtime (scripted) | Instantiate assistant from spec; drive reach → qualify → book; emit step events |
| Live Agent (non-linear) | Bounded tool-calling loop over a live conversation; the model picks the tool, in any order |
| Tool Registry | Open set of tools an assistant may invoke |
| Provider Layer | Fulfills each tool; simulated by default, real Twilio/HubSpot/Google adapters behind credentials |
| Event Stream | Scripted run progress over SSE; live conversation over a WebSocket |
| Frontend | Builder chat + live session view (scripted Run + spoken Talk mode) |

**Request flow:** describe an assistant in chat → **generate** (whole-spec structured-output call, validated and persisted) → **edit** (tool-calling agent loop mutates the spec incrementally) → **run** it, either **scripted** (Runtime drives the spec's tools in order, streaming each step over SSE) or **live** (a spoken, non-linear conversation over a WebSocket — see [Live voice mode](#live-voice-mode-non-linear-tool-invocation)) → outcomes (lead status, intent score, booked slot) written back to the Company Brain from either mode.

## The three seams

The brief left three questions open. Rather than guess, the system is built around seams that absorb each one without hardcoding an answer:

- **Generic schema** — the Assistant Spec is schema-driven, not hardcoded to one archetype. *Absorbs: one assistant type, or many?*
- **Open Tool Registry** — reach/qualify/book are registered tools, not hardcoded steps; adding a tool means registering it, not touching the Runtime. *Absorbs: more tools beyond the three?*
- **Swappable Providers** — each tool is fulfilled by a Provider behind a fixed interface; simulated and real implementations are interchangeable. *Absorbs: real integrations or simulated?*

Defaults used for the demo: one archetype, the reach/qualify/book tool set, simulated providers.

## Setup & run

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy secrets\.env.example secrets\.env   # then put your ANTHROPIC_API_KEY in secrets\.env  (cp on macOS/Linux)
python -m app.main
```

Open **http://localhost:8123**. Override the port with the `PORT` env var. Reset the demo data by deleting `db/ai_conductor.db` — it reseeds the 3 demo leads on next boot.

## Demo walkthrough

1. **Describe** an assistant in the Builder chat — a schema-valid spec is generated and shown.
2. **Edit** it via chat, e.g. "make the persona warmer and add booking" — the edit agent loop mutates the spec in place.
3. **Pick a lead and Run** — watch each step (reach, qualify, book) stream live.
4. **See the outcome** on the lead — status, intent score, booked slot.

The three seeded demo leads exercise every branch. Their `sim_profile` sets who picks up and how that lead behaves on the phone — not the outcome itself, which is earned:

| Lead | Company | `sim_profile` | Outcome |
|---|---|---|---|
| Dana Reyes | Northwind Analytics | `books` | reach (has a real need, warms up) → qualify scores the call high → book → **booked** |
| Marcus Chen | Fieldstone Logistics | `no_answer` | nobody picks up → stops, **unreachable** |
| Priya Nair | Havenlight CRM | `not_qualified` | reach (no budget, not their call) → qualify scores it low → stops, **not_qualified** |

**`reach` places a call that actually happens.** Rather than returning a canned "they picked up," the simulated provider runs a real bounded conversation between two agents — your assistant, speaking from its spec, and the lead, played by a second model whose stance comes from `sim_profile` (`app/sim_lead.py`) — and returns the transcript, which renders inside the reach step so you can read exactly what was said. `qualify` then scores *that transcript* for intent and cites what the lead said, so the number in the Company Brain is judged from the call rather than hardcoded. This is the whole point of the exercise made literal: coordinated agents that act, with the evidence inspectable. Simulated leads are still LLM calls, so a run costs tokens; the call is capped at four exchanges and the lead runs on the cheap model.

Equivalent curl:

```bash
# Generate a spec
curl -X POST localhost:8123/api/specs/generate -H "Content-Type: application/json" \
  -d "{\"description\": \"An assistant that calls inbound leads, qualifies interest, and books a demo.\"}"

# Edit a spec (spec_id from the response above)
curl -X POST localhost:8123/api/specs/1/edit -H "Content-Type: application/json" \
  -d "{\"instruction\": \"make the persona warmer and add booking\"}"

# List leads (to get a lead_id)
curl localhost:8123/api/leads

# Trigger a run (returns 202 + run id immediately)
curl -X POST localhost:8123/api/runs -H "Content-Type: application/json" \
  -d "{\"spec_id\": 1, \"lead_id\": 1}"

# Watch it live (SSE)
curl -N localhost:8123/api/runs/1/stream
```

## Live voice mode (non-linear tool invocation)

The scripted run above is the reliable spine. The **live agent** is the target end state: instead of a fixed reach → qualify → book order, you have a real spoken conversation with the assistant and it invokes the same tools *non-linearly* — say "just set up a meeting" and it calls `book` directly; say "actually, am I even a fit?" and it qualifies first.

**Try it:** launch an assistant, click **Talk**, allow the mic, and speak. The assistant replies out loud, and each tool it invokes appears as a step card and is written back to the Company Brain — the same lead status / intent score / booked slot as a scripted run. A typed-input box drives the exact same loop if you'd rather type (or aren't on Chrome). Runs credential-free on the simulated providers.

**How it works.** The browser does speech I/O with the **Web Speech API** (`SpeechRecognition` in, `SpeechSynthesis` out) — audio never leaves the page. Each user turn is sent as text over a WebSocket (`/api/live/{spec_id}`) to a bounded tool-calling loop (`app/live_agent.py`) that reuses the Phase 3 edit-loop pattern: the model decides which registered tool to call, the loop runs it through the **same** `get_provider().execute()` primitive as the scripted Runtime, feeds the typed result back so the model can narrate it, and persists the step + lead outcome. The linear Runtime is untouched — the live agent is a sibling mode, so the spine can't regress.

**Design choice & limits.** Web Speech is the pragmatic *demo* pipeline, not a production one: it's Chrome/Edge only, turn-taking is serialized (no barge-in — the mic is muted while the assistant speaks to avoid self-hearing), and TTS voice quality is browser-dependent. The **production upgrade path** is a realtime voice API (e.g. OpenAI Realtime / Gemini Live) or an STT→LLM→TTS pipeline (e.g. Deepgram/Whisper + ElevenLabs/Cartesia) over WebRTC for low latency and barge-in — a drop-in for the browser speech layer, leaving the agentic loop and provider seam unchanged.

## Key tradeoffs

**Create vs. edit.** *Create* is a single whole-spec structured-output call — the reliable spine, and also the fallback if editing fails. *Edit* is a bounded tool-calling agent loop (`set_objective`, `set_persona`, `add_tool`, `finish`, …) instead of also just regenerating the whole spec. This was a deliberate choice, not the simpler default: the role is at a company built on coordinated multi-agent systems, so the edit path demonstrates that skill directly. It's guarded with a hard iteration cap (`MAX_EDIT_ITERATIONS = 8`), error-feedback self-correction on invalid tool arguments, and a fallback to whole-spec regeneration on cap-hit or an unrecoverable error — so the edit demo can never dead-end.

**Sync vs. async.** The reach → qualify → book sequence runs as `asyncio` tasks inside FastAPI and streams over SSE — no Celery/Redis/queue. This gives the "live, acting system" feel without infrastructure cost the scale doesn't need. Every step is persisted to SQLite as it completes, so SSE is a push layer over durable state, not the source of truth — a dropped connection reconnects and replays the full run from row zero for free.

## What I'd do next

- **Production voice pipeline** — swap the browser Web Speech layer for a realtime voice API or an STT→LLM→TTS pipeline over WebRTC (low latency, barge-in, cross-browser); the agentic loop and provider seam stay unchanged.
- **Close the real-provider loops** — a Twilio status-callback webhook to turn `initiated` into a real `answered`/`no_answer`; the Google Calendar and HubSpot adapters are already wired behind credentials.
- **Multiple live assistant archetypes** — the schema already supports it; the demo just shows one.
- **Cross-run learning** — compounding intelligence from accumulated outcomes in the Company Brain.

Each sits cleanly behind an existing seam, so none of it requires rearchitecting.

## Tests

```bash
pytest
```

52 tests covering spec validation, the tool registry, the spec/context stores, the scripted Runtime's branches (booked / no-answer / not-qualified / provider-error), the live agent loop (tool invocation, write-back, and provider-failure resilience, with the LLM mocked), the Builder create-path boundary validation and edit loop (self-correction + fallback), and the API including SSE replay, the live WebSocket handshake/turn plumbing, and 404 handling.
