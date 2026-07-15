// Standalone live-session window. Opened by the builder as
// /static/session.html?spec=<id>. The actual conversation is driven by
// realtime.js (WebRTC); this file owns only what's shared between a scripted
// step and a live tool call: the assistant header, the step-card renderer, and
// the run-stream reader that a real (Twilio) reach call polls for its result.
function el(tag, props, children) {
  const node = document.createElement(tag);
  Object.entries(props || {}).forEach(([key, value]) => {
    if (key === "className") node.className = value;
    else if (key === "onclick") node.addEventListener("click", value);
    else node.setAttribute(key, value);
  });
  (children || []).forEach((child) => {
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  });
  return node;
}

const nameEl = document.getElementById("assistantName");
const stepsEl = document.getElementById("steps");
const outcomeEl = document.getElementById("outcome");

const specId = Number(new URLSearchParams(location.search).get("spec"));

// Fetch the spec by id so the window names the assistant it's running (title +
// header). A bad/missing id fails cleanly here rather than at connect time.
async function loadAssistant() {
  if (!specId) {
    nameEl.textContent = "No assistant specified";
    return;
  }
  try {
    const res = await fetch(`/api/specs/${specId}`);
    if (!res.ok) {
      nameEl.textContent = "Assistant not found";
      return;
    }
    const data = await res.json();
    nameEl.textContent = data.name;
    document.title = `Live session — ${data.name}`;
  } catch {
    nameEl.textContent = "Could not load assistant";
  }
}

// Renders the call the assistant actually had with the lead. `reach` returns the
// transcript so the conversation is inspectable rather than just asserted.
function renderCallTranscript(transcript) {
  return el(
    "div",
    { className: "call-transcript" },
    transcript.map((entry) =>
      el("div", { className: `call-line call-${entry.speaker}` }, [
        el("span", { className: "call-speaker" }, [entry.speaker === "assistant" ? "Assistant" : "Lead"]),
        el("span", { className: "call-text" }, [entry.text]),
      ])
    )
  );
}

// One tool result, rendered identically whether it came back instantly (sim
// path) or arrived later over the run stream (a real Twilio call). realtime.js
// reuses this so every tool action reads the same way in #steps.
function buildResultCard(tool, result) {
  const transcript = result.data && result.data.transcript;
  // A transcript-bearing summary carries the whole dialogue after its first line
  // (the model is fed the summary, so it must contain the call). Onscreen that
  // would be an unreadable blob, so show the headline and render the
  // structured transcript underneath instead.
  const headline = transcript ? result.summary.split("\n")[0] : result.summary;
  const children = [
    el("div", { className: "step-head" }, [
      el("span", { className: "step-tool" }, [tool]),
      el("span", { className: "step-outcome" }, [result.outcome]),
    ]),
    el("div", { className: "step-summary" }, [headline]),
  ];
  if (transcript) children.push(renderCallTranscript(transcript));
  return el("div", { className: "step-card" }, children);
}

function appendStepCard(data) {
  stepsEl.appendChild(buildResultCard(data.tool, data.result));
  stepsEl.scrollTop = stepsEl.scrollHeight;
}

// Opens the run's SSE stream and appends each new step to #steps as it lands —
// used for a real (Twilio) reach call, which returns "initiated" immediately
// and resolves minutes later when the bridge writes the actual reach step.
// `onStep(data)` runs per step and returns true once it's the one being
// awaited. The server self-terminates the stream after ~60s (MAX_STREAM_POLLS
// in app/main.py) even though a real call can run longer, so a `done` without
// the awaited step found reopens the stream rather than giving up.
//
// `skip` is the step count already known (and already rendered directly by the
// caller) before this stream opened — every reconnect replays all persisted
// steps from the start, so without this the earlier steps would double up.
async function openRunStream(runId, { onStep } = {}) {
  let resolved = false;
  let skip = 0;
  try {
    const res = await fetch(`/api/runs/${runId}`);
    if (res.ok) skip = (await res.json()).steps.length;
  } catch {
    skip = 0;
  }

  function open() {
    let index = 0;
    const source = new EventSource(`/api/runs/${runId}/stream`);
    source.addEventListener("message", (event) => {
      const data = JSON.parse(event.data);
      if (data.type === "step") {
        const position = index++;
        if (position < skip) return;
        // Advance past everything rendered so a reconnect's full replay can't
        // double-render steps that arrived during the previous stream.
        skip = position + 1;
        appendStepCard(data);
        if (onStep && onStep(data)) resolved = true;
      } else if (data.type === "done") {
        source.close();
        if (!resolved) open();
      }
    });
  }
  open();
}

// After qualify/book, read the lead back so the Company-Brain write-back
// (status + intent score + booked slot) is visible, not just what was said.
async function showLeadOutcome(leadId) {
  outcomeEl.textContent = "";
  let lead = null;
  try {
    const res = await fetch(`/api/leads/${leadId}`);
    if (res.ok) lead = await res.json();
  } catch {
    lead = null;
  }
  if (!lead) return;

  outcomeEl.appendChild(el("div", { className: "outcome-badge outcome-" + lead.status }, [lead.status.toUpperCase()]));
  if (lead.intent_score !== null && lead.intent_score !== undefined) {
    outcomeEl.appendChild(el("div", { className: "outcome-line" }, [`Intent score: ${lead.intent_score}`]));
  }
  if (lead.booked_slot) {
    outcomeEl.appendChild(el("div", { className: "outcome-line" }, [`Booked: ${lead.booked_slot}`]));
  }
}

stepsEl.appendChild(el("div", { className: "session-hint" }, ["Hit Connect and start talking — the assistant takes it from there."]));

loadAssistant();
