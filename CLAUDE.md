# CLAUDE.md

Guidance for working in this repo. Read before making changes. Keep changes aligned with the decisions and rules below — they are settled, not open for relitigation unless explicitly revisited.

## Project

A two-agent voice AI assistant builder.

1. **Builder** — a conversational meta-agent. The user describes the assistant they want in natural language; the Builder generates (and edits) a structured **Assistant Spec**.
2. **Assistant** — the thing that gets built. It runs a per-lead sequence: **reach → qualify → book**, invoking tools to do so.

**Context:** This is a take-home for an AI-engineering role at Alta, a coordinated multi-agent GTM company. The assistant here is a scoped clone of Alta's voice/calling agent ("Alex"-style: runs voice conversations, scores intent, books meetings). The Builder is a second agent. Build in Alta's idiom: **coordinated agents sharing one context layer, that actually *act*.**

**Constraints:** 3-day build. Prioritize a reliable end-to-end demo and visible engineering judgment over completeness. Optimize for what reads well in a live review.

## Golden rules

- **Ship the reliable spine first, layer the flex on top.** The demo must never be able to break.
- **Action-oriented, not chat-oriented.** Agents *do* things and surface outcomes (a call happening, an intent score, a booked slot) — not just produce text. This is the whole point for Alta.
- **The Assistant Spec is the single source of truth.** It is written by the Builder, saved by the Spec Store, read by the Runtime.
- **Keep it simple.** The three open product questions are handled by seams (below). Do not hardcode around them, and do not over-build the seams either.

## Decision log

`docs/DECISIONS.md` is the running log of *why* — keep it current as the project moves, not just at kickoff.

- Whenever a non-trivial decision gets made or changed during the build — an architecture choice, a tradeoff, a deviation from `docs/PLAN.md`, a resolution of one of the three seams, something cut or deferred — append an entry to `docs/DECISIONS.md` in the same turn as the change, not as cleanup later.
- Match the existing format: a short heading naming the decision, then 1–3 sentences on what was chosen and why (the tradeoff, not a feature description).
- Skip it for routine implementation detail that doesn't reflect a choice — only log things a reviewer would otherwise have to ask "why did you do it this way?" about.
- Never rewrite or delete a past entry to make it look like the final call was obvious in hindsight — append a new entry if a decision is later reversed, and say so.

## Stack

- **Python 3.11+**, **FastAPI** (async), served as a **single service** (monolith).
- **SQLModel + SQLite** for persistence (single file; no external DB service).
- **LLM** for the Builder — structured output for create, tool-calling for edit.
- **Light frontend** (keep minimal): a Builder chat panel + a live session view. Talks to the backend over REST + SSE.

### Commands (confirm/adjust as the repo takes shape)
```bash
# setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# run backend (dev)
uvicorn app.main:app --reload

# run tests
pytest
```

## Architecture

| Component | Responsibility |
|---|---|
| Builder | NL → spec (structured output); edit spec (tool-calling loop) |
| Assistant Spec | Config artifact: objective, behavior, allowed tools (Pydantic models) |
| Spec Store | Persist / update specs (SQLite) |
| Context Store ("Company Brain") | Shared state: leads, call outcomes, qualification, booked slots (SQLite) |
| Runtime | Instantiate assistant from spec; drive reach → qualify → book; emit step events |
| Tool Registry | Open set of tools an assistant may invoke |
| Provider Layer | Fulfills each tool; simulated now, real integrations later |
| Event Stream | Stream live run progress to the frontend (SSE) |
| Frontend | Builder chat + live session view |

### The three seams (absorb the open product questions)
- **Generic schema** — the Spec models describe assistants in general; one demo archetype is just one instance. *(Absorbs: "one type or many?")*
- **Open Tool Registry** — reach/qualify/book are *registered* tools, not hardcoded steps; adding tools = registering more. *(Absorbs: "more tools?")*
- **Swappable Providers** — each tool is fulfilled by a Provider behind a fixed interface; simulated and real are interchangeable. *(Absorbs: "real or simulated?")*

Defaults until answered: one demo archetype, the three-tool set, simulated providers.

## Settled decisions

- **Spec:** Pydantic-modeled JSON. The JSON Schema derived from these models is the contract for the LLM.
- **Builder:** *create* = whole-spec structured output (one validated call; also the reliability fallback). *edit* = tool-calling agent loop (incremental mutations). Build create first.
- **Runtime:** `asyncio` tasks in FastAPI + SSE. **No Celery/Redis/queue.**
- **Persistence:** SQLite via SQLModel. ORM keeps SQLite → Postgres a trivial later swap.

## Implementation rules (must follow)

These bake in the known failure modes — treat them as non-negotiable throughout, not as a later hardening pass.

1. **Single schema, no duplicate.** Derive the LLM's JSON schema from the Pydantic models. Never hand-maintain a second copy in a prompt — they will drift.
2. **Validate at the boundary.** Every LLM-produced spec is validated against the schema; on failure, fail cleanly with a clear message — never persist an invalid spec. A **one**-shot repair attempt (feed the validation error back once) is a stretch goal, added only if the measured invalid-output rate warrants it.
3. **Typed tool results; branch, don't assume.** Every tool returns a typed result (success / failure / outcome). The Runtime branches on it. Never assume a step succeeded — a "no answer" call is a normal outcome, not a crash.
4. **Guard the edit loop.** The edit agent loop must have a **hard iteration cap** and must catch bad/invalid tool arguments and feed them back for self-correction rather than throwing. This is the most likely thing to break first — build the guards from line one.
5. **Persist run state as it progresses.** Write each step's state to the Context Store as it completes so the live session view can rebuild on SSE reconnect. Run state must not live only in the stream.
6. **Explicit not-found handling.** Missing spec/lead lookups return a clean not-found, never a 500. Last-write-wins is acceptable for concurrent edits at this scale.
7. **Write outcomes back to the Company Brain.** Every run writes lead status, intent/qualification result, and booked slot back to the Context Store. Accumulation *is* the differentiator — make it visible and inspectable.
8. **Providers are integration adapters.** Simulated now, but shaped so telephony/calendar/CRM are obvious drop-ins. A base class with `execute()` is enough — **do not** build plugin discovery or config-driven provider loading (KISS).

## Out of scope (do not build)

- Real telephony/voice, calendar, or CRM integrations (seam is ready; simulated for the demo)
- Multiple live assistant archetypes in the demo (schema supports it; demo shows one)
- Tools beyond reach / qualify / book (registry is open; only three registered)
- Learning / compounding across runs
- Auth, multi-user, Postgres, queue-based workers

## Definition of done (demo)

Fresh clone → documented setup steps → **describe → generate → edit → run**, with steps streaming live and outcomes persisted and inspectable in the Company Brain. The README explains the architecture, the three seams, the key tradeoffs (esp. create-vs-edit mechanism and sync-vs-async), and what would come next.
