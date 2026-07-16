# AI Conductor — Voice AI Assistant Builder

A two-agent system: a **Builder** turns a natural-language description into a structured **Assistant Spec** (and edits it by chatting), and the assistant that spec describes then *runs* live: a real spoken conversation over **OpenAI Realtime**, invoking tools — **reach, qualify, book** — **non-linearly**, letting the model decide what the conversation calls for. Voice covers two surfaces: the **operator** talks to the assistant directly over WebRTC, and `reach` can place a **real phone call** that is itself a live Realtime conversation with the lead, bridged server-side over Twilio Media Streams. A scripted, linear **Runtime** (reach → qualify → book in a fixed order) remains underneath as the tested backend spine, reachable directly over the API/SSE even though the UI now drives the live conversation. Both modes read from and write to a shared **Context Store** (a small "Company Brain"), so specs, leads, and outcomes accumulate in one inspectable place. Built for an AI-engineering take-home at Alta, a coordinated multi-agent GTM company — the point isn't generated text, it's agents that *act* and surface outcomes (a call happening, an intent score, a booked slot).

## Architecture

| Component | Responsibility |
|---|---|
| Builder | NL → spec (structured output); edit spec (tool-calling loop) |
| Assistant Spec | Config artifact: objective, behavior, allowed tools (Pydantic models) |
| Spec Store | Persist / update specs (SQLite) |
| Context Store ("Company Brain") | Shared state: leads (incl. notes), call outcomes, qualification, booked slots, persisted provider settings (SQLite) |
| Runtime (scripted) | Instantiate assistant from spec; drive reach → qualify → book; emit step events — the tested backend spine, no longer UI-driven |
| Realtime control plane | Mints ephemeral OpenAI Realtime tokens for the operator's WebRTC session; executes tool calls statelessly (`app/tool_exec.py`); bridges `web_search` to the Responses API |
| Voice bridge (Twilio) | Relays a real phone call's audio to a second Realtime session so `reach` is itself a live conversation with the lead (`app/realtime_bridge.py`) |
| Tool Registry | Open set of tools an assistant may invoke |
| Provider Layer | Fulfills each tool; simulated by default, real Twilio/HubSpot/Google adapters behind credentials; selection persists as a global setting |
| Frontend | Builder chat, live voice session (WebRTC), lead management + provider settings dialogs |

**Request flow:** describe an assistant in chat → **generate** (whole-spec structured-output call, validated and persisted) → **edit** (tool-calling agent loop mutates the spec incrementally) → **talk to it live** over OpenAI Realtime — see [Voice](#voice-openai-realtime) — invoking tools non-linearly, or drive the scripted spine directly over the API (`POST /api/runs` + SSE) → outcomes (lead status, intent score, booked slot) written back to the Company Brain either way.

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
copy secrets\.env.example secrets\.env   # then fill in secrets\.env  (cp on macOS/Linux)
python -m app.main
```

`secrets/.env` needs:
- **`ANTHROPIC_API_KEY`** — the Builder (spec generation/editing) and the simulated lead call (`app/sim_lead.py`).
- **`OPENAI_API_KEY`** — required for voice. The operator session talks to OpenAI directly over WebRTC once this server mints it a token; no tunnel needed for this surface.
- **`TWILIO_*` + `PUBLIC_BASE_URL`** — only needed for a *real* phone call. Twilio must be able to reach this server for its voice webhook and Media Stream, so run `ngrok http 8123` (or whatever port you're on), paste the printed `https://...` URL into `PUBLIC_BASE_URL`, and **restart the server** — `load_dotenv` only runs once at import, so a process already running keeps the old value even after you edit the file. Without these, `reach` still runs the simulated two-agent call.
- **`GOOGLE_CALENDAR_CREDENTIALS` + `GOOGLE_CALENDAR_ID`** — only needed to book on a *real* calendar; without them `book` uses the simulated one. `GOOGLE_CALENDAR_CREDENTIALS` is a path to a Google credentials JSON, and **which kind you give it decides whether the lead is actually invited**:
  - A **service-account key** books a real event, but Google refuses to let a service account invite attendees (without Domain-Wide Delegation), so the booking degrades to an attendee-less event and the result says so rather than claiming an invite it never sent. A service account also has no `primary` calendar — share a calendar with the service account's address and put *that* calendar's ID in `GOOGLE_CALENDAR_ID`.
  - **User OAuth credentials** act as you, so the invite really goes out. Write a file shaped `{"type": "authorized_user", "client_id": "...", "client_secret": "...", "refresh_token": "..."}` — mint the refresh token once against the `https://www.googleapis.com/auth/calendar.events` scope using your Google Cloud OAuth client, e.g. via the [OAuth Playground](https://developers.google.com/oauthplayground) — and leave `GOOGLE_CALENDAR_ID` at its `primary` default. The scope granted *when the token was minted* is what counts.

  Keep either file in `secrets/` — everything there but `.env.example` is gitignored.

Open **http://localhost:8123**. Override the port with the `PORT` env var. Reset the demo data by deleting `db/ai_conductor.db` — it reseeds the 3 demo leads on next boot.

## Demo walkthrough

1. **Describe** an assistant in the Builder chat — a schema-valid spec is generated and shown.
2. **Edit** it via chat, e.g. "make the persona warmer and add booking" — the edit agent loop mutates the spec in place.
3. **Launch it and talk** — the session window opens a live OpenAI Realtime conversation over WebRTC. Say who to call ("call Dana from Northwind") and the assistant resolves the lead and invokes `reach` / `qualify` / `book` non-linearly, in whatever order the conversation calls for — there's no Run button or fixed step order anymore.
4. **See the outcome** on the lead — status, intent score, booked slot — and, if `reach` ran, the transcript of the call itself.

Lead notes (set anytime from **Manage leads** on the builder page) are injected into every future call's instructions, simulated or real, so a fact like "prefers mornings" or "already declined once" carries into the assistant's phrasing without repeating it each session.

The three seeded demo leads exercise every branch. Their `sim_profile` sets who picks up and how that lead behaves on the phone — not the outcome itself, which is earned:

| Lead | Company | `sim_profile` | Outcome |
|---|---|---|---|
| Dana Reyes | Northwind Analytics | `books` | reach (has a real need, warms up) → qualify scores the call high → book → **booked** |
| Marcus Chen | Fieldstone Logistics | `no_answer` | nobody picks up → stops, **unreachable** |
| Priya Nair | Havenlight CRM | `not_qualified` | reach (no budget, not their call) → qualify scores it low → stops, **not_qualified** |

**`reach` places a call that actually happens.** Rather than returning a canned "they picked up," the simulated provider runs a real bounded conversation between two agents — your assistant, speaking from its spec, and the lead, played by a second model whose stance comes from `sim_profile` (`app/sim_lead.py`) — and returns the transcript, which renders inside the reach step so you can read exactly what was said. `qualify` then scores *that transcript* for intent and cites what the lead said, so the number in the Company Brain is judged from the call rather than hardcoded. This is the whole point of the exercise made literal: coordinated agents that act, with the evidence inspectable. Simulated leads are still LLM calls, so a run costs tokens; the call is capped at four exchanges and the lead runs on the cheap model.

**The scripted spine, over the API.** The linear Runtime (reach → qualify → book, fixed order) is still there, tested, and reachable directly — useful for automation or a quick sanity check without opening a mic:

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

## Voice (OpenAI Realtime)

Two conversations happen over voice, both on **OpenAI Realtime**:

**Operator ↔ assistant.** You talk to the assistant directly in the session window over **WebRTC** — audio never touches this server for this surface. The server's only job is what must not live in the browser: `POST /api/realtime/token` mints an ephemeral, spec-scoped session token (only the spec's own tools, plus `list_leads` / `request_lead` / `web_search`, are offered), and each tool call the model makes executes statelessly against `POST /api/realtime/tools/{tool_name}` (`app/tool_exec.py`) — the run for that (spec, lead) pair is found or created per call, so a live conversation accumulates in the Company Brain exactly like a scripted run. There's no lead dropdown before you start talking: say who you mean and the model resolves it via `list_leads`, or calls `request_lead` to have you pick if it can't.

**Assistant ↔ lead.** `reach` places the actual call. By default it's simulated — a real bounded text conversation between two models (`app/sim_lead.py`): the assistant speaking from the spec, and a lead persona driven by `sim_profile`. With Twilio credentials and `PUBLIC_BASE_URL` configured, `reach` places a *real* phone call instead, bridged server-side to a second OpenAI Realtime session (`app/realtime_bridge.py`): Twilio's Media Streams and OpenAI's Realtime API both speak G.711 mu-law natively, so the bridge is a byte-for-byte audio relay with no conversion library. Either way, the call's transcript comes back in the same shape and `qualify` scores it unchanged.

**Why Realtime, not the earlier browser Web Speech + Claude loop:** once *both* surfaces needed real voice, one vendor stack beat maintaining two audio pipelines. See `docs/DECISIONS.md` for the full tradeoff and what was deleted.

## Key tradeoffs

**Create vs. edit.** *Create* is a single whole-spec structured-output call — the reliable spine, and also the fallback if editing fails. *Edit* is a bounded tool-calling agent loop (`set_objective`, `set_persona`, `add_tool`, `finish`, …) instead of also just regenerating the whole spec. This was a deliberate choice, not the simpler default: the role is at a company built on coordinated multi-agent systems, so the edit path demonstrates that skill directly. It's guarded with a hard iteration cap (`MAX_EDIT_ITERATIONS = 8`), error-feedback self-correction on invalid tool arguments, and a fallback to whole-spec regeneration on cap-hit or an unrecoverable error — so the edit demo can never dead-end.

**Sync vs. async.** The reach → qualify → book sequence runs as `asyncio` tasks inside FastAPI and streams over SSE — no Celery/Redis/queue. This gives the "live, acting system" feel without infrastructure cost the scale doesn't need. Every step is persisted to SQLite as it completes, so SSE is a push layer over durable state, not the source of truth — a dropped connection reconnects and replays the full run from row zero for free.

## What I'd do next

- **Operator listen-in on a live phone call** — the operator and lead sessions are currently independent; there's no way to monitor or take over a real call in progress once `reach` places it.
- **Per-session provider settings** — provider selection is currently one global setting; scoping it per spec or per session would let two assistants run against different providers at once.
- **Multiple live assistant archetypes** — the schema already supports it; the demo just shows one.
- **Cross-run learning** — compounding intelligence from accumulated outcomes in the Company Brain.

Each sits cleanly behind an existing seam, so none of it requires rearchitecting.

## Tests

```bash
pytest
```

104 tests covering spec validation, the tool registry, the spec/context/settings stores (lead CRUD and search, the SQLite migration for new lead columns, persisted provider settings), the scripted Runtime's branches (booked / no-answer / not-qualified / provider-error), the Realtime control plane and stateless tool executor, the Twilio ↔ OpenAI Realtime bridge (transcript accumulation and media relay, with fake sockets — no real calls), the Builder create-path boundary validation and edit loop (self-correction + fallback), and the API including SSE replay and 404 handling. All LLM and voice-provider calls are mocked — no network, no cost.
