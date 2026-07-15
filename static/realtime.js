// WebRTC client for the operator's live voice session (Surface A). Connects
// straight to OpenAI Realtime — audio never touches this app's server; the
// server's only jobs are minting the ephemeral token (app/realtime.py) and
// executing tool calls the model makes over the data channel.
//
// Shares session.js's global scope (classic <script> tags, same page): el(),
// buildResultCard/appendStepCard, openRunStream, showLeadOutcome, specId,
// stepsEl are all defined there and loaded first.

const connectBtn = document.getElementById("connectBtn");
const micMuteBtn = document.getElementById("micMuteBtn");
const voiceStatusEl = document.getElementById("voiceStatus");
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

// A user-authored text item — how a real call's outcome gets into the
// conversation, since it lands minutes after the tool call that started it was
// already answered.
function sendUserMessage(text) {
  sendEvent({
    type: "conversation.item.create",
    item: { type: "message", role: "user", content: [{ type: "input_text", text }] },
  });
  sendEvent({ type: "response.create" });
}

// Every tool call must be answered on the data channel or the model stalls
// waiting for it. response.create after the output is what makes the
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
  sendEvent({ type: "response.create" });
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
}

function handleDisconnect() {
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
  if (event.type === "response.output_audio_transcript.done") {
    appendTranscript("assistant", event.transcript);
  } else if (event.type === "conversation.item.input_audio_transcription.completed") {
    appendTranscript("you", event.transcript);
  } else if (event.type === "response.done") {
    handleResponseDone(event);
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
  if (["reach", "qualify", "book"].includes(item.name)) return handleLeadTool(item.name, callId, args);
  // An unregistered function name is a client/server drift, not a
  // conversational outcome — tell the model rather than silently stalling it.
  sendFunctionResult(callId, `Unknown tool: ${item.name}`);
}

async function handleListLeads(callId, args) {
  try {
    const res = await fetch(`/api/leads?q=${encodeURIComponent(args.query || "")}`);
    const leads = res.ok ? await res.json() : [];
    sendFunctionResult(callId, leads.map((lead) => ({ id: lead.id, name: lead.name, company: lead.company, status: lead.status })));
  } catch {
    sendFunctionResult(callId, "Could not load leads.");
  }
}

// Renders a picker into #steps (there's no lead dropdown anymore). The call is
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
          card.remove();
          sendFunctionResult(
            callId,
            `Operator selected: ${lead.name} — ${lead.company} (lead_id ${lead.id})`
          );
        },
      }, [`${lead.name} — ${lead.company}${lead.sim_profile ? ` · ${lead.sim_profile}` : ""}`])
    ))
  );
  stepsEl.appendChild(card);
  stepsEl.scrollTop = stepsEl.scrollHeight;
}

async function handleWebSearch(callId, args) {
  try {
    const res = await fetch("/api/realtime/web-search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: args.query || "" }),
    });
    const data = await res.json().catch(() => ({}));
    sendFunctionResult(callId, res.ok ? data.result : (data.detail || "Web search failed."));
  } catch {
    sendFunctionResult(callId, "Network error running web search.");
  }
}

async function handleLeadTool(name, callId, args) {
  const leadId = args.lead_id ?? activeLeadId;
  if (!leadId) {
    sendFunctionResult(callId, "No lead selected. Use list_leads or request_lead first.");
    return;
  }
  activeLeadId = leadId;

  let response;
  try {
    response = await fetch(`/api/realtime/tools/${name}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec_id: specId, lead_id: leadId, args }),
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
  if (name === "qualify" || name === "book") showLeadOutcome(leadId);
}

// A real Twilio call only returns "initiated" from handleLeadTool above and
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
