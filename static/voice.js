// Live voice layer (Phase 6). Runs a spoken, non-linear conversation with the
// assistant over a WebSocket: the browser does speech-to-text and text-to-speech
// with the Web Speech API; the backend runs the agentic tool-calling loop.
//
// This is a separate script from session.js (the scripted-run flow). It reads the
// same lead <select> and provider dropdowns from the DOM but shares none of its
// logic, so it carries its own small `el` helper per the repo's convention.
//
// Web Speech is Chrome/Edge only. If speech recognition is unavailable, the mic
// is disabled and the typed-input box drives the exact same backend turn — so the
// conversation still works everywhere.
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

// specId and leadSelectEl are declared by session.js, which loads first on this
// page (both scripts are classic <script> tags sharing one global scope) — reused
// here rather than redeclared, which would be a SyntaxError.

const talkBtn = document.getElementById("talkBtn");
const voicePanel = document.getElementById("voicePanel");
const voiceStatusEl = document.getElementById("voiceStatus");
const micBtn = document.getElementById("micBtn");
const transcriptEl = document.getElementById("transcript");
const voiceTextEl = document.getElementById("voiceText");
const voiceSendBtn = document.getElementById("voiceSendBtn");

// Web Speech recognition is vendor-prefixed in Chrome. null when unsupported.
const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;

let socket = null;
let socketReady = false;
let recognition = null;
let recognizing = false;
let micOn = false; // user wants the mic listening
let isSpeaking = false; // TTS is mid-utterance
let awaitingTurn = false; // a user turn was sent; server hasn't signaled turn_done
const speakQueue = [];

function setStatus(text) {
  voiceStatusEl.textContent = text;
}

// --- Transcript rendering ---------------------------------------------------

function appendTranscript(role, text) {
  const line = el("div", { className: `transcript-line transcript-${role}` }, [text]);
  transcriptEl.appendChild(line);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

// buildResultCard comes from session.js (loaded first, shared global scope), so a
// tool action reads identically in the scripted and live views.
function appendActionCard(data) {
  transcriptEl.appendChild(buildResultCard(data.tool, data.result));
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

// --- Text-to-speech: speak assistant turns, one utterance at a time ---------

// The mic must never be open while the assistant is speaking or it transcribes
// its own voice. Speech is queued; listening only resumes once the queue drains
// AND the turn is done (turn_done), so it can't reopen between the assistant's
// pre-tool and post-tool fragments.
function enqueueSpeech(text) {
  speakQueue.push(text);
  if (!isSpeaking) speakNext();
}

function speakNext() {
  if (speakQueue.length === 0) {
    isSpeaking = false;
    maybeResumeListening();
    return;
  }
  isSpeaking = true;
  stopRecognition();
  const utterance = new SpeechSynthesisUtterance(speakQueue.shift());
  utterance.onend = speakNext;
  utterance.onerror = speakNext; // a failed utterance must not stall the queue
  window.speechSynthesis.speak(utterance);
}

// --- Speech-to-text ---------------------------------------------------------

function initRecognition() {
  if (!SpeechRecognitionCtor) return null;
  const instance = new SpeechRecognitionCtor();
  instance.lang = "en-US";
  instance.interimResults = false;
  instance.continuous = false;
  instance.addEventListener("result", (event) => {
    const transcript = event.results[0][0].transcript.trim();
    if (transcript) sendUserTurn(transcript);
  });
  // Chrome ends recognition after each phrase; restart if the user is still in
  // mic mode and it's safe to listen (not speaking, not mid-turn).
  instance.addEventListener("end", () => {
    recognizing = false;
    maybeResumeListening();
  });
  instance.addEventListener("error", (event) => {
    recognizing = false;
    if (event.error === "not-allowed" || event.error === "service-not-allowed") {
      micOn = false;
      updateMicButton();
      setStatus("Mic permission denied — use the text box.");
    }
  });
  return instance;
}

function startRecognition() {
  if (!recognition || recognizing) return;
  try {
    recognition.start();
    recognizing = true;
  } catch {
    // start() throws if already started — ignore, we're already listening.
  }
}

function stopRecognition() {
  if (recognition && recognizing) {
    try {
      recognition.abort();
    } catch {
      // abort() can throw if not started — safe to ignore.
    }
    recognizing = false;
  }
}

function maybeResumeListening() {
  if (micOn && !isSpeaking && !awaitingTurn && socketReady) startRecognition();
}

// --- Turn + socket handling -------------------------------------------------

function sendUserTurn(text) {
  if (!socketReady) return;
  awaitingTurn = true;
  stopRecognition(); // don't listen while the assistant is composing/speaking
  appendTranscript("you", text);
  socket.send(JSON.stringify({ type: "user", text }));
}

function handleServerEvent(data) {
  if (data.type === "ready") {
    socketReady = true;
    micBtn.disabled = !SpeechRecognitionCtor;
    voiceSendBtn.disabled = false;
    setStatus(SpeechRecognitionCtor ? "Connected — tap the mic or type." : "Connected — type to talk (mic needs Chrome).");
  } else if (data.type === "assistant") {
    appendTranscript("assistant", data.text);
    enqueueSpeech(data.text);
  } else if (data.type === "action") {
    appendActionCard(data);
  } else if (data.type === "turn_done") {
    awaitingTurn = false;
    maybeResumeListening();
  } else if (data.type === "error") {
    appendTranscript("error", data.message);
  }
}

function openSocket(leadId, providers) {
  const wsProtocol = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${wsProtocol}//${location.host}/api/live/${specId}`);
  socket.addEventListener("open", () => {
    socket.send(JSON.stringify({ type: "start", lead_id: leadId, providers }));
  });
  socket.addEventListener("message", (event) => handleServerEvent(JSON.parse(event.data)));
  socket.addEventListener("close", () => {
    socketReady = false;
    micOn = false;
    stopRecognition();
    updateMicButton();
    micBtn.disabled = true;
    voiceSendBtn.disabled = true;
    setStatus("Disconnected.");
  });
  socket.addEventListener("error", () => setStatus("Connection error."));
}

// --- Controls ---------------------------------------------------------------

function updateMicButton() {
  micBtn.textContent = micOn ? "Stop mic" : "Start mic";
  micBtn.className = micOn ? "mic-on" : "";
}

function handleMicToggle() {
  if (!SpeechRecognitionCtor) return;
  micOn = !micOn;
  updateMicButton();
  if (micOn) maybeResumeListening();
  else stopRecognition();
}

function handleSend() {
  const text = voiceTextEl.value.trim();
  if (!text || !socketReady) return;
  voiceTextEl.value = "";
  sendUserTurn(text);
}

function handleTalk() {
  if (!specId) {
    setStatus("No assistant specified.");
    return;
  }
  const leadId = Number(leadSelectEl.value);
  if (!leadId) {
    setStatus("Pick a lead first.");
    return;
  }
  const providers = {};
  document.querySelectorAll("#providerControls select[data-tool]").forEach((select) => {
    providers[select.getAttribute("data-tool")] = select.value;
  });

  transcriptEl.textContent = "";
  voicePanel.hidden = false;
  recognition = initRecognition();
  setStatus("Connecting…");
  openSocket(leadId, providers);
}

talkBtn.addEventListener("click", handleTalk);
micBtn.addEventListener("click", handleMicToggle);
voiceSendBtn.addEventListener("click", handleSend);
voiceTextEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter") handleSend();
});
