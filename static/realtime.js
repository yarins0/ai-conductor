// WebRTC client for the operator's live voice session (Surface A). Connects
// straight to OpenAI Realtime — audio never touches this app's server; the
// server's only jobs are minting the ephemeral token (app/realtime.py) and
// executing tool calls the model makes over the data channel.
//
// Shares session.js's global scope (classic <script> tags, same page): el(),
// buildResultCard/appendStepCard, openRunStream, showLeadOutcome, specId,
// chatEl are all defined there and loaded first.

const connectBtn = document.getElementById("connectBtn");
const micMuteBtn = document.getElementById("micMuteBtn");
const voiceStatusEl = document.getElementById("voiceStatus");
const specNoticeEl = document.getElementById("specNotice");
const transcriptEl = document.getElementById("transcript");
const assistantAudioEl = document.getElementById("assistantAudio");

let pc = null;
let dc = null;
let micTrack = null;
let micMuted = false;
let activeLeadId = null; // resolved via list_leads/request_lead; reach/qualify/book default to this
// call_id -> { leadId, runId } for a real (Twilio) reach call that returned
// "initiated" and is still waiting on the media bridge to resolve it.
const pendingCallCalls = new Map();

function setStatus(text) {
  voiceStatusEl.textContent = text;
}

function appendTranscript(role, text) {
  transcriptEl.appendChild(el("div", { className: `transcript-line transcript-${role}` }, [text]));
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function sendEvent(event) {
  dc.send(JSON.stringify(event));
}

// OpenAI rejects a response.create while another response is still active, and
// two things here arrive unprompted mid-speech: a click on a lead card, and a
// real call's outcome landing minutes late. Either would be dropped and left
// sitting in the conversation unanswered. Queue the request instead and flush
// it when the active response ends.
//
// If "response.created" ever stops firing, responseActive stays false and this
// degrades to sending immediately — the behaviour before this existed.
let responseActive = false;
let responseQueued = false;

function requestResponse() {
  if (responseActive) responseQueued = true;
  else sendEvent({ type: "response.create" });
}

// A user-authored text item — how a real call's outcome gets into the
// conversation, since it lands minutes after the tool call that started it was
// already answered.
function sendUserMessage(text) {
  sendEvent({
    type: "conversation.item.create",
    item: { type: "message", role: "user", content: [{ type: "input_text", text }] },
  });
  requestResponse();
}

// Every tool call must be answered on the data channel or the model stalls
// waiting for it. The response request after the output is what makes the
// assistant actually say something about the result.
function sendFunctionResult(callId, output) {
  sendEvent({
    type: "conversation.item.create",
    item: {
      type: "function_call_output",
      call_id: callId,
      output: typeof output === "string" ? output : JSON.stringify(output),
    },
  });
  requestResponse();
}

// --- Spec drift ---------------------------------------------------------------
//
// The session's instructions and tools are frozen into the ephemeral token when
// it is minted (app/realtime.py mint_token) and nothing updates them afterwards,
// while the Builder can edit the same spec in another window at any moment. The
// two then disagree, and the worse direction is the silent one: a tool ADDED
// mid-session is simply invisible to the model — no error, nothing to notice,
// just an assistant that never uses what it was given. (A tool REMOVED at least
// announces itself, since the call reaches the server and run_tool rejects it.)
//
// So: notice, and say so. This deliberately does not push a session.update to
// fix it live — the spec is the source of truth and a session quietly running a
// different one should be visible, not patched over.
//
// ponytail: polling, not SSE. A human editing a spec is not a high-frequency
// event, and 5s of staleness costs nothing against a notice that only asks the
// operator to reconnect.
const SPEC_POLL_MS = 5000;
let specVersion = null; // spec updated_at this session's token was minted from
let specPollTimer = null;

function startSpecPoll() {
  stopSpecPoll();
  specPollTimer = setInterval(checkSpecVersion, SPEC_POLL_MS);
}

function stopSpecPoll() {
  clearInterval(specPollTimer);
  specPollTimer = null;
}

async function checkSpecVersion() {
  if (!specVersion) return;
  let spec;
  try {
    const res = await fetch(`/api/specs/${specId}`);
    if (!res.ok) return; // a 404/500 is not evidence the spec changed
    spec = await res.json();
  } catch {
    return; // offline: stay quiet rather than cry wolf about an edit nobody made
  }
  if (spec.updated_at === specVersion) return;
  stopSpecPoll(); // said once — the notice stands until it's acted on
  showSpecNotice();
}

function showSpecNotice() {
  specNoticeEl.replaceChildren(
    el("div", { className: "spec-notice" }, [
      el("span", {}, [
        "This assistant was edited. The live session is still running the version it started with.",
      ]),
      el("button", { onclick: reconnect }, ["Reconnect to apply"]),
    ]),
  );
}

// The notice has to be actionable: Connect is disabled for the life of a
// session, so without this the only way to pick up an edit is reloading the page.
async function reconnect() {
  specNoticeEl.replaceChildren();
  handleDisconnect();
  await connect();
}

// --- Connect / disconnect ----------------------------------------------------

async function connect() {
  if (!specId) {
    setStatus("No assistant specified.");
    return;
  }
  connectBtn.disabled = true;
  setStatus("Connecting…");

  let tokenData;
  try {
    const res = await fetch("/api/realtime/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec_id: specId }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      // A 502 here is what a missing OPENAI_API_KEY looks like server-side —
      // its detail message is already operator-readable, shown as-is.
      setStatus(data.detail || "Could not start a realtime session.");
      connectBtn.disabled = false;
      return;
    }
    tokenData = data;
  } catch {
    setStatus("Network error — could not reach the server.");
    connectBtn.disabled = false;
    return;
  }

  let micStream;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    setStatus("Microphone permission denied.");
    connectBtn.disabled = false;
    return;
  }
  micTrack = micStream.getAudioTracks()[0];

  pc = new RTCPeerConnection();
  pc.addTrack(micTrack, micStream);
  pc.ontrack = (event) => { assistantAudioEl.srcObject = event.streams[0]; };
  pc.onconnectionstatechange = () => {
    if (["failed", "closed", "disconnected"].includes(pc.connectionState)) handleDisconnect();
  };

  dc = pc.createDataChannel("oai-events");
  dc.addEventListener("message", (event) => handleRealtimeEvent(JSON.parse(event.data)));
  dc.addEventListener("close", handleDisconnect);

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  // Verified against developers.openai.com/api/docs/guides/realtime-webrtc
  // (2026-07-15): no ?model= query param on this endpoint — the ephemeral
  // token already scopes the session to the model it was minted with.
  let answerSdp;
  try {
    const sdpResponse = await fetch("https://api.openai.com/v1/realtime/calls", {
      method: "POST",
      body: offer.sdp,
      headers: { Authorization: `Bearer ${tokenData.value}`, "Content-Type": "application/sdp" },
    });
    if (!sdpResponse.ok) throw new Error(`OpenAI WebRTC handshake failed (${sdpResponse.status}).`);
    answerSdp = await sdpResponse.text();
  } catch (error) {
    setStatus(error.message || "Could not connect to OpenAI.");
    handleDisconnect();
    return;
  }
  await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });

  connectBtn.textContent = "Connected";
  micMuteBtn.disabled = false;
  setStatus("Connected — start talking.");

  // Only now: the token minted above is what froze this session's tools, so it
  // is the baseline to compare against, and there is no session to be stale
  // until the handshake actually succeeds.
  specVersion = tokenData.spec_version;
  startSpecPoll();
}

function handleDisconnect() {
  stopSpecPoll();
  setStatus("Disconnected.");
  connectBtn.disabled = false;
  connectBtn.textContent = "Connect";
  micMuteBtn.disabled = true;
  micMuteBtn.textContent = "Mute mic";
  micMuted = false;
  micTrack?.stop();
  pc?.close();
  micTrack = null;
  pc = null;
  dc = null;
}

function toggleMicMute() {
  if (!micTrack) return;
  micMuted = !micMuted;
  micTrack.enabled = !micMuted;
  micMuteBtn.textContent = micMuted ? "Unmute mic" : "Mute mic";
}

// --- Realtime event handling --------------------------------------------------

function handleRealtimeEvent(event) {
  if (event.type === "response.created") {
    responseActive = true;
  } else if (event.type === "response.output_audio_transcript.done") {
    appendTranscript("assistant", event.transcript);
  } else if (event.type === "conversation.item.input_audio_transcription.completed") {
    appendTranscript("you", event.transcript);
  } else if (event.type === "response.done") {
    responseActive = false;
    handleResponseDone(event);
    // A tool call dispatched above answers asynchronously and queues its own
    // request, so flushing here cannot race it.
    if (responseQueued) {
      responseQueued = false;
      requestResponse();
    }
  }
}

// Deviation from the brief: dispatching on "response.function_call_arguments.done"
// assumed that event carries `name`, but developers.openai.com's guide only
// documents `name`/`arguments`/`call_id` together on response.done's output
// items (verified 2026-07-15; the per-item event's exact fields aren't shown
// in the guide). Reading completed calls off response.done is the
// doc-confirmed shape, so that's what this listens on — the one drift-risk
// left in this file if OpenAI's event shape changes again.
function handleResponseDone(event) {
  const items = (event.response && event.response.output) || [];
  items.filter((item) => item.type === "function_call").forEach((item) => handleFunctionCall(item));
}

async function handleFunctionCall(item) {
  const callId = item.call_id;
  let args = {};
  try {
    args = item.arguments ? JSON.parse(item.arguments) : {};
  } catch {
    args = {};
  }

  if (item.name === "list_leads") return handleListLeads(callId, args);
  if (item.name === "request_lead") return handleRequestLead(callId, args);
  if (item.name === "web_search") return handleWebSearch(callId, args);
  if (["reach", "qualify", "book"].includes(item.name)) return handleSpecTool(item.name, callId, args);
  // An unregistered function name is a client/server drift, not a
  // conversational outcome — tell the model rather than silently stalling it.
  sendFunctionResult(callId, `Unknown tool: ${item.name}`);
}

// What a lead card collapses into once a lead is chosen. Both the list and the
// picker end here, so choosing stays in the stream as the moment it happened — a
// card that vanished, or a full list left lingering, would both lose that.
function leadChosenCard(lead) {
  return el("div", { className: "step-card" }, [
    el("div", { className: "step-head" }, [el("span", { className: "step-tool" }, ["Lead selected"])]),
    el("div", { className: "step-summary" }, [`${lead.name} — ${lead.company}`]),
  ]);
}

// The most recent lead list still waiting on a choice: { card, leads }.
//
// A list is answered either way a lead can get chosen — the operator clicking a
// row, or the agent resolving one itself and calling a tool with its id — so it
// collapses on both, and a lead can only be picked once. Leaving it open after
// the fact invites a second, conflicting pick against a decision already made.
let openLeadList = null;

function collapseLeadList(leadId) {
  if (!openLeadList) return;
  const lead = openLeadList.leads.find((candidate) => candidate.id === leadId);
  // A lead the list never offered leaves it open: the list is still unanswered,
  // and there'd be no name to collapse it into anyway.
  if (!lead) return;
  openLeadList.card.replaceWith(leadChosenCard(lead));
  openLeadList = null;
}

// Renders the looked-up leads into the chat as well as answering the call: the
// model needs the list to reason about, the operator needs to see it. Asking to
// see leads only ever drew a card by accident of prompt routing — it reached
// request_lead's picker until that tool was narrowed to disambiguation.
//
// Clicking a row makes that lead active. Unlike request_lead, this call is
// already answered by then (a lookup must return promptly), so the choice can
// only reach the model as a user message.
async function handleListLeads(callId, args) {
  let leads = [];
  try {
    const res = await fetch(`/api/leads?q=${encodeURIComponent(args.query || "")}`);
    leads = res.ok ? await res.json() : [];
  } catch {
    sendFunctionResult(callId, "Could not load leads.");
    return;
  }

  sendFunctionResult(callId, leads.map((lead) => ({ id: lead.id, name: lead.name, company: lead.company, status: lead.status })));
  if (leads.length === 0) return;

  const card = el("div", { className: "step-card" }, [
    el("div", { className: "step-head" }, [el("span", { className: "step-tool" }, ["Leads"])]),
  ]);
  card.appendChild(
    el("div", { className: "lead-picker" }, leads.map((lead) =>
      el("button", {
        onclick: () => {
          activeLeadId = lead.id;
          collapseLeadList(lead.id);
          sendUserMessage(`Operator selected: ${lead.name} — ${lead.company} (lead_id ${lead.id})`);
        },
      }, [`${lead.name} — ${lead.company}${lead.status ? ` · ${lead.status}` : ""}`])
    ))
  );
  openLeadList = { card, leads };
  chatEl.appendChild(card);
  chatEl.scrollTop = chatEl.scrollHeight;
}

// Renders a picker into the chat (there's no lead dropdown anymore). The call is
// deliberately left UNANSWERED until the operator clicks: the click is the
// tool's result, so the model resumes holding the lead it asked for. Answering
// up front instead ("picker shown") ends the call, and the model — with no
// other way to learn the choice — asks the operator to say it out loud, which
// carries no id, and re-opens the picker.
let openPickerCallId = null;

async function handleRequestLead(callId, args) {
  if (openPickerCallId) {
    sendFunctionResult(callId, "A picker is already open — the operator has not picked yet.");
    return;
  }

  let leads = [];
  try {
    const res = await fetch(`/api/leads?q=${encodeURIComponent(args.query || "")}`);
    leads = res.ok ? await res.json() : [];
  } catch {
    leads = [];
  }
  if (leads.length === 0) {
    sendFunctionResult(callId, "No leads matched — ask the operator who to work.");
    return;
  }

  openPickerCallId = callId;
  const card = el("div", { className: "step-card" }, [
    el("div", { className: "step-head" }, [el("span", { className: "step-tool" }, ["Pick a lead"])]),
  ]);
  card.appendChild(
    el("div", { className: "lead-picker" }, leads.map((lead) =>
      el("button", {
        onclick: () => {
          activeLeadId = lead.id;
          openPickerCallId = null;
          card.replaceWith(leadChosenCard(lead));
          sendFunctionResult(
            callId,
            `Operator selected: ${lead.name} — ${lead.company} (lead_id ${lead.id})`
          );
        },
      }, [`${lead.name} — ${lead.company}${lead.sim_profile ? ` · ${lead.sim_profile}` : ""}`])
    ))
  );
  chatEl.appendChild(card);
  chatEl.scrollTop = chatEl.scrollHeight;
}

// A search leaves a card like any other tool action: it reached outside the
// system and whatever it found is what the assistant's next sentence rests on,
// so the operator should see the finding rather than only hear a claim about it.
// Rendered before the result is sent, so the card is up before the model speaks.
async function handleWebSearch(callId, args) {
  const query = args.query || "";
  let ok = false;
  let output;
  try {
    const res = await fetch("/api/realtime/web-search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    const data = await res.json().catch(() => ({}));
    ok = res.ok;
    // An empty result reads as a failed search to the operator but would reach
    // the model as nothing at all, leaving it to invent a finding.
    output = ok ? (data.result || "The search returned nothing.") : (data.detail || "Web search failed.");
  } catch {
    output = "Network error running web search.";
  }

  appendStepCard({
    tool: "web_search",
    result: { outcome: ok ? "searched" : "error", summary: `"${query}" — ${output}` },
  });
  sendFunctionResult(callId, output);
}

// A tool from the spec: reach, qualify, or book. Sent with the resolved lead, or
// with none — book holds plain time on the operator's own calendar, and which
// tools can do that is the server's to know (Provider.requires_lead), not a list
// duplicated here. A tool that does need a lead and wasn't given one comes back
// as a needs_lead result the model hears and acts on, same as any other outcome.
async function handleSpecTool(name, callId, args) {
  const leadId = args.lead_id ?? activeLeadId;
  if (leadId) {
    activeLeadId = leadId;
    // Acting on a lead is the agent answering the list itself, without a click.
    collapseLeadList(leadId);
  }

  let response;
  try {
    response = await fetch(`/api/realtime/tools/${name}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec_id: specId, lead_id: leadId ?? null, args }),
    });
  } catch {
    sendFunctionResult(callId, "Network error running that tool.");
    return;
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    sendFunctionResult(callId, data.detail || `Could not run ${name}.`);
    return;
  }

  const { run_id, result } = await response.json();
  appendStepCard({ tool: name, result });

  if (result.outcome === "initiated") {
    sendFunctionResult(callId, "Call started — it runs on its own; the transcript will arrive when it ends.");
    pendingCallCalls.set(callId, { leadId, runId: run_id });
    awaitRealCall(callId, run_id);
    return;
  }

  sendFunctionResult(callId, result.summary);
  // Only when there was a lead: showLeadOutcome clears the panel before it
  // fetches, so calling it for a leadless book would wipe the outcome of the
  // lead the operator is actually working.
  if (leadId && (name === "qualify" || name === "book")) showLeadOutcome(leadId);
}

// A real Twilio call only returns "initiated" from handleSpecTool above and
// resolves minutes later via the media bridge. Wait on the run stream for the
// actual reach step, then narrate it to the model so it can tell the operator
// how the call went.
function awaitRealCall(callId, runId) {
  openRunStream(runId, {
    onStep: (data) => {
      if (data.tool !== "reach") return false;
      pendingCallCalls.delete(callId);
      sendUserMessage(`Call finished: ${data.result.summary}`);
      return true;
    },
  });
}

connectBtn.addEventListener("click", connect);
micMuteBtn.addEventListener("click", toggleMicMute);
