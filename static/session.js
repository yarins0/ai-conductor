// Standalone live-session window. Opened by the builder as
// /static/session.html?spec=<id>; it runs that one assistant against a lead and
// streams each step. Shares the backend endpoints with the builder, none of its
// DOM — so it carries its own small `el` helper rather than pulling in app.js.
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
const leadSelectEl = document.getElementById("leadSelect");
const runBtn = document.getElementById("runBtn");
const reconnectBtn = document.getElementById("reconnectBtn");
const stepsEl = document.getElementById("steps");
const outcomeEl = document.getElementById("outcome");
const providerControlsEl = document.getElementById("providerControls");
const leadNameEl = document.getElementById("leadName");
const leadCompanyEl = document.getElementById("leadCompany");
const leadPhoneEl = document.getElementById("leadPhone");
const leadSimProfileEl = document.getElementById("leadSimProfile");
const addLeadBtn = document.getElementById("addLeadBtn");
const addLeadErrorEl = document.getElementById("addLeadError");

const specId = Number(new URLSearchParams(location.search).get("spec"));
let leads = [];
let specTools = [];         // [{name}, ...] from spec.tools, set by loadAssistant()
let providerSelects = {};   // toolName -> <select> element, built by renderProviderControls()
let currentEventSource = null;
let lastRun = null;  // { runId, leadId } — lets the Reconnect button reopen the stream

// Fetch the spec by id so the window names the assistant it's running (title +
// header). A bad/missing id fails cleanly here rather than at Run time.
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
    specTools = data.spec?.tools || [];
    renderProviderControls();
  } catch {
    nameEl.textContent = "Could not load assistant";
  }
}

// One provider <select> per tool in the spec, options from GET /api/providers.
// Called after loadAssistant() resolves spec.tools; a no-op until then.
async function renderProviderControls() {
  if (specTools.length === 0) return;

  let providersByTool = {};
  try {
    const res = await fetch("/api/providers");
    providersByTool = res.ok ? await res.json() : {};
  } catch {
    providersByTool = {};
  }

  providerControlsEl.textContent = "";
  providerSelects = {};
  specTools.forEach((tool) => {
    const options = providersByTool[tool.name] || [];
    const select = el("select", { "data-tool": tool.name }, []);
    options.forEach((provider) => {
      const option = el("option", { value: provider.id }, [provider.label]);
      if (provider.default) option.setAttribute("selected", "selected");
      select.appendChild(option);
    });
    providerSelects[tool.name] = select;
    providerControlsEl.appendChild(
      el("div", { className: "provider-row" }, [el("label", {}, [tool.name]), select])
    );
  });
}

async function loadLeads() {
  try {
    const res = await fetch("/api/leads");
    leads = res.ok ? await res.json() : [];
  } catch {
    leads = [];
  }
  leadSelectEl.textContent = "";
  leads.forEach((lead) => {
    leadSelectEl.appendChild(el("option", { value: String(lead.id) }, [`${lead.name} — ${lead.company}${lead.sim_profile ? ` (${lead.sim_profile})` : ""}`]));
  });
  runBtn.disabled = !specId || leads.length === 0;
}

async function handleRun() {
  if (!specId || leads.length === 0) return;
  const leadId = Number(leadSelectEl.value);
  const providers = Object.fromEntries(
    Object.entries(providerSelects).map(([toolName, select]) => [toolName, select.value])
  );

  stepsEl.textContent = "";
  outcomeEl.textContent = "";

  try {
    const res = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec_id: specId, lead_id: leadId, providers }),
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

// Guard: require all three text fields before hitting the API; the backend is
// the real validation boundary, so this only avoids obvious empty submits.
async function handleAddLead() {
  const name = leadNameEl.value.trim();
  const company = leadCompanyEl.value.trim();
  const phone = leadPhoneEl.value.trim();
  if (!name || !company || !phone) return;

  addLeadErrorEl.textContent = "";
  try {
    const res = await fetch("/api/leads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, company, phone, sim_profile: leadSimProfileEl.value }),
    });
    if (!res.ok) {
      const data = await res.json();
      addLeadErrorEl.textContent = data.detail || "Could not add lead.";
      return;
    }
    leadNameEl.value = "";
    leadCompanyEl.value = "";
    leadPhoneEl.value = "";
    leadSimProfileEl.value = "books";
    loadLeads();
  } catch {
    addLeadErrorEl.textContent = "Network error adding lead.";
  }
}

runBtn.addEventListener("click", handleRun);
addLeadBtn.addEventListener("click", handleAddLead);
reconnectBtn.addEventListener("click", () => {
  if (lastRun) openRunStream(lastRun.runId, lastRun.leadId);
});

stepsEl.appendChild(el("div", { className: "session-hint" }, ["Pick a lead and Run to watch this assistant work."]));

loadAssistant();
loadLeads();
