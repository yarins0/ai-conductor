// Minimal DOM builder — cuts repetitive createElement/setAttribute calls below.
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

const specListEl = document.getElementById("specList");
const logEl = document.getElementById("log");
const inputEl = document.getElementById("input");
const actionBtn = document.getElementById("actionBtn");
const newAssistantBtn = document.getElementById("newAssistantBtn");
const leadSelectEl = document.getElementById("leadSelect");
const runBtn = document.getElementById("runBtn");
const reconnectBtn = document.getElementById("reconnectBtn");
const stepsEl = document.getElementById("steps");
const outcomeEl = document.getElementById("outcome");

let specs = [];
let leads = [];
let activeSpecId = null;
let currentEventSource = null;
let lastRun = null;  // { runId, leadId } — lets the Reconnect button reopen the stream

// The single composer button is mode-aware: with no active spec it creates a new
// one; with a spec active (freshly created or picked from the sidebar) it edits
// that one. Run also needs at least one lead.
function syncControls() {
  const hasSpec = activeSpecId !== null;
  actionBtn.textContent = hasSpec ? "Edit" : "Create";
  actionBtn.classList.toggle("editing", hasSpec);
  runBtn.disabled = !hasSpec || leads.length === 0;
}

// Dispatch the one composer button by mode.
function handleAction() {
  if (activeSpecId === null) handleCreate();
  else handleEdit();
}

// Reset to a blank conversation: deselect any spec, clear the log, drop back to
// Create mode. Does not touch the live session panel.
function startNewChat() {
  activeSpecId = null;
  logEl.textContent = "";
  logEl.appendChild(el("div", { className: "empty-hint", id: "emptyHint" }, ["Describe the assistant you want and hit Create to get started."]));
  renderSidebar();
  syncControls();
  inputEl.focus();
}

async function loadSpecs() {
  try {
    const res = await fetch("/api/specs");
    specs = res.ok ? await res.json() : [];
  } catch {
    specs = [];
  }
  renderSidebar();
}

function renderSidebar() {
  specListEl.textContent = "";
  if (specs.length === 0) {
    specListEl.appendChild(el("div", { className: "empty-hint" }, ["No assistants yet — create one to get started."]));
    return;
  }
  specs.forEach((spec) => {
    const item = el("div", {
      className: "spec-item" + (spec.id === activeSpecId ? " active" : ""),
      onclick: () => showSpecCard(spec, false),
    }, [
      el("div", { className: "name" }, [spec.name]),
      el("div", { className: "date" }, [new Date(spec.created_at).toLocaleString()]),
    ]);
    specListEl.appendChild(item);
  });
}

function appendUserMessage(text) {
  document.getElementById("emptyHint")?.remove();
  logEl.appendChild(el("div", { className: "msg-user" }, [text]));
  logEl.scrollTop = logEl.scrollHeight;
}

function appendError(message) {
  logEl.appendChild(el("div", { className: "msg-error" }, [message]));
  logEl.scrollTop = logEl.scrollHeight;
}

function buildSpecCard(spec) {
  const card = el("div", { className: "spec-card" });
  card.appendChild(el("h3", {}, [spec.name]));
  card.appendChild(el("div", { className: "objective" }, [spec.objective]));

  card.appendChild(el("div", { className: "label" }, ["Persona"]));
  card.appendChild(el("div", {}, [spec.persona]));

  card.appendChild(el("div", { className: "label" }, ["Instructions"]));
  const list = el("ul", {}, (spec.instructions || []).map((instr) => el("li", {}, [instr])));
  card.appendChild(list);

  card.appendChild(el("div", { className: "label" }, ["Tools"]));
  const chips = el("div", { className: "chips" }, (spec.tools || []).map((tool) => el("span", { className: "chip" }, [tool.name])));
  card.appendChild(chips);

  const details = el("details", {}, [
    el("summary", {}, ["Raw JSON"]),
    el("pre", {}, [JSON.stringify(spec, null, 2)]),
  ]);
  card.appendChild(details);

  return card;
}

// Shows a spec in the log. When it's a fresh create (fromCreate), it's appended
// as a new message; when clicked from the sidebar, it replaces the log with that spec.
function showSpecCard(entry, fromCreate) {
  if (!fromCreate) {
    activeSpecId = entry.id;
    renderSidebar();
    logEl.textContent = "";
  }
  syncControls();
  logEl.appendChild(buildSpecCard(entry.spec));
  logEl.scrollTop = logEl.scrollHeight;
}

function setLoading(isLoading) {
  actionBtn.disabled = isLoading;
  inputEl.disabled = isLoading;
  let loadingEl = document.getElementById("loadingMsg");
  if (isLoading) {
    loadingEl = el("div", { className: "msg-loading", id: "loadingMsg" }, ["Building…"]);
    logEl.appendChild(loadingEl);
    logEl.scrollTop = logEl.scrollHeight;
  } else if (loadingEl) {
    loadingEl.remove();
  }
}

async function handleCreate() {
  const description = inputEl.value.trim();
  if (!description) return;

  appendUserMessage(description);
  inputEl.value = "";
  setLoading(true);

  try {
    const res = await fetch("/api/specs/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ description }),
    });
    const data = await res.json();
    setLoading(false);

    if (res.status === 201) {
      activeSpecId = data.id;
      showSpecCard(data, true);
      await loadSpecs();
    } else {
      appendError(data.detail || "Something went wrong generating that assistant.");
    }
  } catch {
    setLoading(false);
    appendError("Network error — could not reach the server.");
  }
}

// Edit path: send the composer text as an instruction against the active spec.
// The backend loop self-corrects and falls back to regeneration, so this
// always returns a valid spec or a clean error.
async function handleEdit() {
  const instruction = inputEl.value.trim();
  if (!instruction || activeSpecId === null) return;

  appendUserMessage(instruction);
  inputEl.value = "";
  setLoading(true);

  try {
    const res = await fetch(`/api/specs/${activeSpecId}/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction }),
    });
    const data = await res.json();
    setLoading(false);

    if (res.ok) {
      showSpecCard(data, true);
      if (data.fallback) {
        logEl.appendChild(el("div", { className: "msg-loading" }, ["Regenerated the whole spec."]));
      }
      await loadSpecs();
    } else {
      appendError(data.detail || "Could not edit that assistant.");
    }
  } catch {
    setLoading(false);
    appendError("Network error — could not reach the server.");
  }
}

async function loadLeads() {
  try {
    const res = await fetch("/api/leads");
    leads = res.ok ? await res.json() : [];
  } catch {
    leads = [];
  }
  renderLeadSelect();
  syncControls();
}

function renderLeadSelect() {
  leadSelectEl.textContent = "";
  leads.forEach((lead) => {
    leadSelectEl.appendChild(el("option", { value: String(lead.id) }, [`${lead.name} — ${lead.company} (${lead.sim_profile})`]));
  });
}

async function handleRun() {
  if (activeSpecId === null || leads.length === 0) return;
  const leadId = Number(leadSelectEl.value);

  stepsEl.textContent = "";
  outcomeEl.textContent = "";

  try {
    const res = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec_id: activeSpecId, lead_id: leadId }),
    });
    if (!res.ok) {
      const data = await res.json();
      outcomeEl.appendChild(el("div", { className: "session-error" }, [data.detail || "Could not start the run."]));
      return;
    }
    const run = await res.json();
    openRunStream(run.id, leadId);
  } catch {
    outcomeEl.appendChild(el("div", { className: "session-error" }, ["Network error starting the run."]));
  }
}

// Opening (or reopening) the stream clears the step list on `open` and lets the
// server replay every persisted step — so a reconnect rebuilds the view from
// durable state, not a blank panel. This is the "survive a reconnect" behavior.
function openRunStream(runId, leadId) {
  if (currentEventSource) currentEventSource.close();
  lastRun = { runId, leadId };
  reconnectBtn.disabled = false;

  const source = new EventSource(`/api/runs/${runId}/stream`);
  currentEventSource = source;

  source.addEventListener("open", () => {
    stepsEl.textContent = "";
    outcomeEl.textContent = "";
  });
  source.addEventListener("message", (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "step") {
      appendStepCard(data);
    } else if (data.type === "done") {
      source.close();
      showOutcome(leadId, data.status);
    }
  });
}

function appendStepCard(data) {
  const card = el("div", { className: "step-card" }, [
    el("div", { className: "step-head" }, [
      el("span", { className: "step-tool" }, [data.tool]),
      el("span", { className: "step-outcome" }, [data.result.outcome]),
    ]),
    el("div", { className: "step-summary" }, [data.result.summary]),
  ]);
  stepsEl.appendChild(card);
  stepsEl.scrollTop = stepsEl.scrollHeight;
}

// After the run ends, read the lead back so the Company-Brain write-back
// (status + score + booked slot) is visible, not just the streamed steps.
async function showOutcome(leadId, runStatus) {
  outcomeEl.textContent = "";
  if (runStatus === "failed") {
    outcomeEl.appendChild(el("div", { className: "session-error" }, ["Run failed — see the steps above."]));
    return;
  }

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

actionBtn.addEventListener("click", handleAction);
newAssistantBtn.addEventListener("click", startNewChat);
runBtn.addEventListener("click", handleRun);
reconnectBtn.addEventListener("click", () => {
  if (lastRun) openRunStream(lastRun.runId, lastRun.leadId);
});
inputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    handleAction();
  }
});

// Collapse/expand the two side panels. Toggling `collapsed` shrinks the panel to
// a thin strip (CSS-driven); the chevron flips to point the way it will open.
function wirePanelToggle(toggleId, panelId, collapsedChar, expandedChar) {
  const toggle = document.getElementById(toggleId);
  const panel = document.getElementById(panelId);
  toggle.addEventListener("click", () => {
    const isCollapsed = panel.classList.toggle("collapsed");
    toggle.textContent = isCollapsed ? collapsedChar : expandedChar;
    toggle.title = isCollapsed ? "Expand" : "Collapse";
  });
}
wirePanelToggle("sidebarToggle", "sidebar", "›", "‹");
wirePanelToggle("sessionToggle", "session", "‹", "›");

logEl.appendChild(el("div", { className: "empty-hint", id: "emptyHint" }, ["Describe the assistant you want and hit Create to get started."]));
stepsEl.appendChild(el("div", { className: "session-hint" }, ["Pick a lead and Run an assistant to watch it work."]));

loadSpecs();
loadLeads();
