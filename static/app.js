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
const launchBtn = document.getElementById("launchBtn");

let specs = [];
let activeSpecId = null;

// The single composer button is mode-aware: with no active spec it creates a new
// one; with a spec active (freshly created or picked from the sidebar) it edits
// that one. Launch (open the live session) also needs an active spec.
function syncControls() {
  const hasSpec = activeSpecId !== null;
  actionBtn.textContent = hasSpec ? "Edit" : "Create";
  actionBtn.classList.toggle("editing", hasSpec);
  launchBtn.disabled = !hasSpec;
}

// Open the live session for the active spec in its own window. The session page
// reads the id from the query string and names the assistant it's running.
function launchAssistant() {
  if (activeSpecId === null) return;
  window.open(`/static/session.html?spec=${activeSpecId}`, "_blank");
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

actionBtn.addEventListener("click", handleAction);
newAssistantBtn.addEventListener("click", startNewChat);
launchBtn.addEventListener("click", launchAssistant);
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

logEl.appendChild(el("div", { className: "empty-hint", id: "emptyHint" }, ["Describe the assistant you want and hit Create to get started."]));

loadSpecs();
