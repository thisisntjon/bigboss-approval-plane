const storage = {
  auth: "bigboss.authToken",
  csrf: "bigboss.csrfToken",
  deviceId: "bigboss.deviceId",
  deviceName: "bigboss.deviceName"
};

const state = {
  status: "pending",
  approvals: [],
  projects: [],
  squire: null,
  view: "approvals",
  source: null,
  reconnectTimer: null,
  pairMode: "pin",
  serverReachable: null,
  healthTimer: null,
  lastEventId: 0,
  lastMessageAt: 0,
  watchdogTimer: null,
  livenessWired: false
};

// If no event OR heartbeat arrives within this window the SSE socket is treated as
// silently dead and forcibly reconnected (E3.5). Server heartbeats every 15s.
const SSE_STALE_MS = 40000;

const els = {
  pairPanel: document.querySelector("#pair-panel"),
  queuePanel: document.querySelector("#queue-panel"),
  pairForm: document.querySelector("#pair-form"),
  pairCode: document.querySelector("#pair-code"),
  deviceName: document.querySelector("#device-name"),
  pairError: document.querySelector("#pair-error"),
  pairPin: document.querySelector("#pair-pin"),
  pairPinField: document.querySelector("#pair-pin-field"),
  pairCodeField: document.querySelector("#pair-code-field"),
  pairPinMode: document.querySelector("#pair-pin-mode"),
  pairCodeMode: document.querySelector("#pair-code-mode"),
  serverHealthDot: document.querySelector("#server-health-dot"),
  serverHealthText: document.querySelector("#server-health-text"),
  deviceLabel: document.querySelector("#device-label"),
  liveState: document.querySelector("#live-state"),
  approvalList: document.querySelector("#approval-list"),
  emptyState: document.querySelector("#empty-state"),
  summaryStrip: document.querySelector("#summary-strip"),
  demoButton: document.querySelector("#demo-button"),
  logoutButton: document.querySelector("#logout-button"),
  pendingFilter: document.querySelector("#pending-filter"),
  allFilter: document.querySelector("#all-filter"),
  approvalsViewButton: document.querySelector("#approvals-view-button"),
  portfolioViewButton: document.querySelector("#portfolio-view-button"),
  approvalsView: document.querySelector("#approvals-view"),
  portfolioView: document.querySelector("#portfolio-view"),
  portfolioMeta: document.querySelector("#portfolio-meta"),
  portfolioEmpty: document.querySelector("#portfolio-empty"),
  projectList: document.querySelector("#project-list"),
  registryRefreshButton: document.querySelector("#registry-refresh-button"),
  intelRefreshButton: document.querySelector("#intel-refresh-button"),
  squireViewButton: document.querySelector("#squire-view-button"),
  squireView: document.querySelector("#squire-view"),
  squireMeta: document.querySelector("#squire-meta"),
  squireSummary: document.querySelector("#squire-summary"),
  squireClients: document.querySelector("#squire-clients"),
  squireEmpty: document.querySelector("#squire-empty"),
  squireRefreshButton: document.querySelector("#squire-refresh-button"),
  daemonsViewButton: document.querySelector("#daemons-view-button"),
  daemonsView: document.querySelector("#daemons-view"),
  daemonsMeta: document.querySelector("#daemons-meta"),
  daemonsServices: document.querySelector("#daemons-services"),
  daemonsRegistered: document.querySelector("#daemons-registered"),
  daemonsNote: document.querySelector("#daemons-note"),
  daemonsRefreshButton: document.querySelector("#daemons-refresh-button"),
  fleetViewButton: document.querySelector("#fleet-view-button"),
  fleetView: document.querySelector("#fleet-view"),
  fleetMeta: document.querySelector("#fleet-meta"),
  fleetList: document.querySelector("#fleet-list"),
  fleetEmpty: document.querySelector("#fleet-empty"),
  fleetRefreshButton: document.querySelector("#fleet-refresh-button")
};

function token() {
  return localStorage.getItem(storage.auth);
}

function csrf() {
  return localStorage.getItem(storage.csrf);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!headers.has("Content-Type") && options.body) {
    headers.set("Content-Type", "application/json");
  }
  if (token()) {
    headers.set("Authorization", `Bearer ${token()}`);
  }
  if (["POST", "PUT", "PATCH", "DELETE"].includes((options.method || "GET").toUpperCase()) && csrf()) {
    headers.set("X-BigBoss-CSRF", csrf());
  }
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const err = new Error(data.error || `Request failed: ${response.status}`);
    err.status = response.status;  // let callers distinguish auth (401/403) from transient (5xx/network)
    throw err;
  }
  return data;
}

function showPair() {
  els.pairPanel.classList.remove("hidden");
  els.queuePanel.classList.add("hidden");
  startHealthProbe();
}

function showQueue() {
  stopHealthProbe();
  els.pairPanel.classList.add("hidden");
  els.queuePanel.classList.remove("hidden");
  els.deviceLabel.textContent = localStorage.getItem(storage.deviceName) || "Paired device";
}

function setPairMode(mode) {
  state.pairMode = mode;
  const pinMode = mode === "pin";
  els.pairPinMode.classList.toggle("active", pinMode);
  els.pairCodeMode.classList.toggle("active", !pinMode);
  els.pairPinField.classList.toggle("hidden", !pinMode);
  els.pairCodeField.classList.toggle("hidden", pinMode);
}

function formatPairError(error, status) {
  const message = String(error?.message || error || "");
  if (status === 404 || message.includes("Route not found")) {
    return "Wrong server or old BigBoss still running on this port. Restart serve on your PC (one instance only).";
  }
  if (message.includes("expired")) {
    return "That pair code expired. Scan a fresh QR at http://127.0.0.1:8787/pair or use your LAN PIN.";
  }
  if (message.includes("PIN")) {
    return "PIN is wrong. Check the LAN PIN printed when you run serve, or run: bigboss lan-pin";
  }
  if (message.includes("Too many failed PIN")) {
    return message;
  }
  if (!state.serverReachable) {
    return "Cannot reach BigBoss on this URL. Confirm serve is running and you are on the same WiFi.";
  }
  return message || "Pairing failed.";
}

async function probeServerHealth() {
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`health ${response.status}`);
    }
    state.serverReachable = true;
    els.serverHealthDot.className = "status-dot live";
    els.serverHealthDot.title = "Server reachable";
    els.serverHealthText.textContent = "Server reachable on this URL";
    els.serverHealthText.className = "server-health ok";
  } catch (_error) {
    state.serverReachable = false;
    els.serverHealthDot.className = "status-dot waiting";
    els.serverHealthDot.title = "Server unreachable";
    els.serverHealthText.textContent = "Cannot reach BigBoss — check serve is running and WiFi matches";
    els.serverHealthText.className = "server-health bad";
  }
}

function startHealthProbe() {
  stopHealthProbe();
  probeServerHealth();
  state.healthTimer = window.setInterval(probeServerHealth, 5000);
}

function stopHealthProbe() {
  if (state.healthTimer) {
    window.clearInterval(state.healthTimer);
    state.healthTimer = null;
  }
}

function readPairFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const pair = params.get("pair");
  if (pair) {
    els.pairCode.value = pair;
    return true;
  }
  return false;
}

async function pairDevice(event) {
  if (event?.preventDefault) {
    event.preventDefault();
  }
  els.pairError.textContent = "";
  const deviceName = els.deviceName.value.trim() || "Phone";
  const usePin = state.pairMode === "pin";
  const path = usePin ? "/api/devices/claim-pin" : "/api/devices/claim";
  const body = usePin
    ? { pin: els.pairPin.value.trim(), name: deviceName }
    : { code: els.pairCode.value.trim(), name: deviceName };
  if (usePin && body.pin.length !== 6) {
    els.pairError.textContent = "Enter the 6-digit LAN PIN from your serve terminal.";
    return;
  }
  if (!usePin && !body.code) {
    els.pairError.textContent = "Enter the pair code from /pair on your PC.";
    return;
  }
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw Object.assign(new Error(data.error || `Request failed: ${response.status}`), { status: response.status });
    }
    localStorage.setItem(storage.auth, data.auth_token);
    localStorage.setItem(storage.csrf, data.csrf_token);
    localStorage.setItem(storage.deviceId, data.device_id);
    localStorage.setItem(storage.deviceName, data.device_name);
    history.replaceState({}, "", "/");
    await boot();
  } catch (error) {
    els.pairError.textContent = formatPairError(error, error.status);
  }
}

async function loadApprovals() {
  const data = await api(`/api/approvals?status=${encodeURIComponent(state.status)}`);
  state.approvals = data.approvals || [];
  render();
}

function render() {
  els.approvalList.innerHTML = "";
  const pending = state.approvals.filter((approval) => approval.status === "pending").length;
  const high = state.approvals.filter((approval) => ["high", "critical"].includes(approval.risk_level)).length;
  els.summaryStrip.innerHTML = `
    <div><span>${pending}</span><small>pending</small></div>
    <div><span>${high}</span><small>high risk</small></div>
    <div><span>${state.approvals.length}</span><small>${state.status}</small></div>
  `;
  els.emptyState.classList.toggle("hidden", state.approvals.length > 0);
  for (const approval of state.approvals) {
    els.approvalList.appendChild(renderCard(approval));
  }
}

function renderPrioritizationDetail(pa) {
  const order = pa.order || [];
  const reasons = pa.reasons || {};
  const ideation = pa.ideation || {};
  const sections = [];

  const items = order
    .map((slug, i) => `<li><span class="rank">${i + 1}</span> <strong>${escapeHtml(slug)}</strong>${reasons[slug] ? ` — ${escapeHtml(reasons[slug])}` : ""}</li>`)
    .join("");
  if (items) sections.push(`<ol class="priority-order">${items}</ol>`);

  const synergies = (ideation.synergies || [])
    .map((s) => `<li><strong>${escapeHtml((s.projects || []).join(" + "))}</strong>: ${escapeHtml(s.insight || "")}</li>`)
    .join("");
  if (synergies) sections.push(`<div class="ideation-block"><h3>Synergies</h3><ul class="ideation-list">${synergies}</ul></div>`);

  const opps = (ideation.opportunities || [])
    .map((o) => {
      const tag = (o.projects && o.projects.length) ? ` <span class="ideation-tag">${escapeHtml(o.projects.join(", "))}</span>` : "";
      return `<li>${escapeHtml(o.idea || "")}${tag}</li>`;
    })
    .join("");
  if (opps) sections.push(`<div class="ideation-block"><h3>Opportunities</h3><ul class="ideation-list">${opps}</ul></div>`);

  const tensions = (ideation.tensions || [])
    .map((t) => `<li><strong>${escapeHtml(t.topic || "")}</strong>: ${escapeHtml(t.detail || "")}</li>`)
    .join("");
  if (tensions) sections.push(`<div class="ideation-block"><h3>Tensions</h3><ul class="ideation-list">${tensions}</ul></div>`);

  const dissent = pa.dissent || [];
  if (dissent.length) {
    sections.push(`<div class="ideation-block"><h3>Contested</h3><p class="ideation-tag-row">${dissent.map((s) => `<span class="ideation-tag">${escapeHtml(s)}</span>`).join(" ")}</p></div>`);
  }

  const brief = ideation.brief || pa.rationale || "";
  if (brief) sections.push(`<p class="ideation-brief">${escapeHtml(brief)}</p>`);

  return `<div class="priority-detail">${sections.join("")}</div>`;
}

function renderCard(approval) {
  const card = document.createElement("article");
  card.className = `approval-card risk-${approval.risk_level} status-${approval.status}`;
  const pa = approval.proposed_action || {};
  const diff = approval.diff_summary || {};
  const reasons = (approval.policy_reasons || []).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("");
  const disabled = approval.status !== "pending" ? "disabled" : "";
  const highRisk = approval.risk_level === "high";
  const body = renderActionDetail(pa);
  // HIGH-risk cards (e.g. reap_process — irreversible process kills) get no standing
  // approval: approve-once only (suppress "Run"/approve_for_run), and a note is required.
  const runButton = highRisk ? "" : `<button data-decision="approve_for_run" ${disabled}>Run</button>`;
  card.innerHTML = `
    <div class="card-head">
      <div>
        <div class="eyebrow">${escapeHtml(approval.harness)} / ${escapeHtml(shortWorkspace(approval.workspace))}</div>
        <h2>${escapeHtml(approval.title)}</h2>
      </div>
      <span class="risk-pill">${escapeHtml(approval.risk_level)}</span>
    </div>
    <p class="summary">${escapeHtml(approval.summary)}</p>
    ${body}
    <ul class="reasons">${reasons}</ul>
    ${highRisk ? `<p class="reasons blockers">High-risk — approve once only; a note is required.</p>` : ""}
    <div class="meta-row">
      <span>${escapeHtml(approval.status)}</span>
      <span>${Number(diff.files_changed || 0)} files</span>
      <span>+${Number(diff.insertions || 0)} / -${Number(diff.deletions || 0)}</span>
      <span>${escapeHtml((approval.action_hash || "").slice(0, 12))}</span>
    </div>
    <textarea rows="2" placeholder="${highRisk ? "Note required to approve" : "Instruction or note"}" ${disabled}></textarea>
    <p class="reasons blockers hidden note-required-msg">A note is required to approve this high-risk action.</p>
    <div class="button-row">
      <button class="approve" data-decision="approve_once" ${disabled}>Approve</button>
      ${runButton}
      <button data-decision="request_changes" ${disabled}>Instruct</button>
      <button class="reject" data-decision="reject" ${disabled}>Reject</button>
    </div>
  `;
  card.querySelectorAll("button[data-decision]").forEach((button) => {
    button.addEventListener("click", () => {
      const note = card.querySelector("textarea").value.trim();
      // On a high-risk approve, force a note (approvals of irreversible actions must be justified).
      if (highRisk && button.dataset.decision === "approve_once" && !note) {
        card.querySelector(".note-required-msg").classList.remove("hidden");
        return;
      }
      decide(approval.id, button.dataset.decision, note);
    });
  });
  return card;
}

function renderActionDetail(pa) {
  if (pa.kind === "council_prioritization") return renderPrioritizationDetail(pa);
  if (pa.kind === "reap_process") {
    const rows = (pa.targets || []).map((t) =>
      `<li>PID ${escapeHtml(String(t.pid))} — ${escapeHtml(t.name || "?")}${t.role ? ` (${escapeHtml(t.role)})` : ""}</li>`
    ).join("");
    return `<p class="muted">Will terminate these orphaned processes (re-validated at approval):</p><ul class="reasons">${rows}</ul>`;
  }
  if (pa.kind === "remote_command") {
    const args = pa.args && Object.keys(pa.args).length ? ` ${escapeHtml(JSON.stringify(pa.args))}` : "";
    return `<pre class="command">@${escapeHtml(pa.host || "?")}: ${escapeHtml(pa.action || "?")}${args}</pre>`;
  }
  if (pa.kind === "daemon_restart") {
    const elev = pa.restart_elevated ? " (elevated — you'll get a paste-block)" : "";
    return `<pre class="command">restart daemon: ${escapeHtml(pa.name || "?")}${escapeHtml(elev)}</pre>`;
  }
  return `<pre class="command">${escapeHtml(pa.command || pa.path || pa.kind || "action")}</pre>`;
}

async function decide(id, decision, note) {
  await api(`/api/approvals/${encodeURIComponent(id)}/decide`, {
    method: "POST",
    body: JSON.stringify({ decision, note })
  });
  await loadApprovals();
}

async function createDemoApproval() {
  await api("/api/demo/approval", { method: "POST", body: "{}" });
  await loadApprovals();
}

function setView(view) {
  state.view = view;
  els.approvalsViewButton.classList.toggle("active", view === "approvals");
  els.portfolioViewButton.classList.toggle("active", view === "portfolio");
  els.squireViewButton.classList.toggle("active", view === "squire");
  els.daemonsViewButton.classList.toggle("active", view === "daemons");
  els.fleetViewButton.classList.toggle("active", view === "fleet");
  els.approvalsView.classList.toggle("hidden", view !== "approvals");
  els.portfolioView.classList.toggle("hidden", view !== "portfolio");
  els.squireView.classList.toggle("hidden", view !== "squire");
  els.daemonsView.classList.toggle("hidden", view !== "daemons");
  els.fleetView.classList.toggle("hidden", view !== "fleet");
  if (view === "portfolio") {
    loadProjects().catch(handleAuthFailure);
  }
  if (view === "squire") {
    loadSquire().catch(handleAuthFailure);
  }
  if (view === "daemons") {
    loadDaemons().catch(handleAuthFailure);
  }
  if (view === "fleet") {
    loadFleet().catch(handleAuthFailure);
  }
}

async function loadFleet() {
  const data = await api("/api/sessions?days=7");
  renderFleet((data && data.sessions) || []);
}

function renderFleet(sessions) {
  const harnesses = new Set(sessions.map((s) => s.harness).filter(Boolean));
  const hosts = new Set(sessions.map((s) => s.host).filter(Boolean));
  const machines = hosts.size ? ` across ${hosts.size} machine${hosts.size === 1 ? "" : "s"}` : "";
  els.fleetMeta.textContent = sessions.length
    ? `${sessions.length} session${sessions.length === 1 ? "" : "s"} from ${harnesses.size} harness${harnesses.size === 1 ? "" : "es"}${machines} (last 7d).`
    : "What every harness session across the fleet has been up to.";
  els.fleetList.innerHTML = "";
  els.fleetEmpty.classList.toggle("hidden", sessions.length > 0);
  for (const s of sessions) {
    const who = s.harness + (s.vendor ? ` / ${s.vendor}` : "");
    const host = s.host ? ` · @${s.host}` : "";
    const when = (s.created_at || "").slice(0, 16).replace("T", " ");
    const files = (s.files_touched || []).length;
    const next = (s.next_steps || []).slice(0, 3).join("; ");
    const card = document.createElement("article");
    card.className = "approval-card";
    card.innerHTML =
      `<div class="eyebrow">${escapeHtml(who)}${escapeHtml(host)} · ${escapeHtml(when)}</div>` +
      `<h3>${escapeHtml(s.project || "—")} — ${escapeHtml(s.title || "")}</h3>` +
      (s.summary ? `<p class="muted">${escapeHtml(s.summary)}</p>` : "") +
      (files ? `<p class="muted">${files} file${files === 1 ? "" : "s"} touched</p>` : "") +
      (next ? `<p class="muted">next: ${escapeHtml(next)}</p>` : "");
    els.fleetList.appendChild(card);
  }
}

async function loadDaemons() {
  const data = await api("/api/daemons");
  renderDaemons(data || {});
}

function renderDaemons(data) {
  const services = data.bigboss_services || [];
  const registered = data.registered_daemons || [];
  const up = services.filter((s) => s.listening).length;
  els.daemonsMeta.textContent = `${up}/${services.length} BigBoss services listening. Read-only.`;
  els.daemonsServices.innerHTML = "";
  for (const s of services) {
    let mark;
    if (!s.listening) mark = "DOWN";
    else if (s.health_ok === false) mark = "listening · health FAILED";
    else if (s.health_ok) mark = "UP · health ok";
    else mark = "listening";
    const card = document.createElement("article");
    card.className = "approval-card";
    card.innerHTML =
      `<div class="eyebrow">${escapeHtml(s.endpoint || "")}</div>` +
      `<h3>${escapeHtml(s.name || "")} — ${escapeHtml(mark)}</h3>` +
      `<p class="muted">${escapeHtml(s.role || "")}</p>`;
    els.daemonsServices.appendChild(card);
  }
  els.daemonsRegistered.innerHTML = "";
  for (const d of registered) {
    const card = document.createElement("article");
    card.className = "approval-card";
    const elev = d.restart_elevated ? " · elevated restart" : "";
    card.innerHTML =
      `<div class="eyebrow">registered daemon${escapeHtml(elev)}</div>` +
      `<h3>${escapeHtml(d.name || "")}</h3>` +
      `<p class="muted">${escapeHtml(d.project || "")}</p>` +
      (d.healthy_when ? `<p class="muted">healthy when: ${escapeHtml(d.healthy_when)}</p>` : "");
    els.daemonsRegistered.appendChild(card);
  }
  els.daemonsNote.textContent = data.note || "";
}

async function loadProjects() {
  const data = await api("/api/registry/projects");
  state.projects = data.projects || [];
  renderProjects();
}

function renderProjects() {
  els.projectList.innerHTML = "";
  els.portfolioEmpty.classList.toggle("hidden", state.projects.length > 0);
  const pinned = state.projects.filter((project) => project.pinned).length;
  els.portfolioMeta.textContent = `${state.projects.length} projects, ${pinned} pinned. Auto-derived from local signals.`;
  for (const project of state.projects) {
    els.projectList.appendChild(renderProjectCard(project));
  }
}

function renderProjectCard(project) {
  const card = document.createElement("article");
  const dormant = daysSince(project.last_activity_at) > 30;
  const intel = project.intel && project.intel.generated_at ? project.intel : null;
  const blocked = intel && intel.blockers.length > 0;
  card.className = `project-card${project.pinned ? " pinned" : ""}${dormant ? " dormant" : ""}${blocked ? " blocked" : ""}`;
  const leftOff = whereILeftOff(project);
  const blockerItems = intel
    ? intel.blockers.map((b) => `<li>${escapeHtml(b)}</li>`).join("")
    : "";
  card.innerHTML = `
    <div class="card-head">
      <div>
        <div class="eyebrow">${escapeHtml(project.kind || "project")}${project.is_daemonized ? " / daemon" : ""}</div>
        <h2>${escapeHtml(project.name || project.slug)}</h2>
      </div>
      <button class="pin-button${project.pinned ? " active" : ""}" title="${project.pinned ? "Unpin" : "Pin to prioritize"}" aria-label="${project.pinned ? "Unpin" : "Pin"}">&#9733;</button>
    </div>
    ${intel && intel.status_line ? `<p class="summary intel-line">${escapeHtml(intel.status_line)}</p>` : project.purpose ? `<p class="summary">${escapeHtml(project.purpose)}</p>` : ""}
    ${blockerItems ? `<ul class="reasons blockers">${blockerItems}</ul>` : ""}
    <div class="meta-row">
      <span>${escapeHtml(agoLabel(project.last_activity_at))}</span>
      ${dormant ? '<span class="dormant-pill">dormant</span>' : ""}
      ${blocked ? `<span class="blocker-pill">${intel.blockers.length} blocker${intel.blockers.length > 1 ? "s" : ""}</span>` : ""}
      ${intel && intel.roadmap_now ? `<span title="Current phase">now: ${escapeHtml(intel.roadmap_now)}</span>` : ""}
      ${intel && intel.roadmap_next ? `<span title="Next milestone">next: ${escapeHtml(intel.roadmap_next)}</span>` : ""}
      ${leftOff ? `<span>${escapeHtml(leftOff)}</span>` : ""}
      ${project.git_commit_count ? `<span>${Number(project.git_commit_count)} commits</span>` : ""}
      ${intel && intel.error ? '<span class="dormant-pill" title="Last digest failed">intel stale</span>' : ""}
    </div>
  `;
  card.querySelector(".pin-button").addEventListener("click", () => {
    togglePin(project).catch(handleAuthFailure);
  });
  return card;
}

async function togglePin(project) {
  await api("/api/registry/pin", {
    method: "POST",
    body: JSON.stringify({ project_id: project.id, pinned: !project.pinned })
  });
  await loadProjects();
}

async function refreshRegistry() {
  els.registryRefreshButton.disabled = true;
  els.portfolioMeta.textContent = "Refreshing registry (scanning local signals)...";
  try {
    const data = await api("/api/registry/refresh", { method: "POST", body: "{}" });
    const summary = data.summary || {};
    await loadProjects();
    els.portfolioMeta.textContent =
      `${state.projects.length} projects: ${Number(summary.discovered || 0)} new, ${Number(summary.updated || 0)} updated.`;
  } catch (error) {
    els.portfolioMeta.textContent = `Refresh failed: ${error.message}`;
  } finally {
    els.registryRefreshButton.disabled = false;
  }
}

async function refreshIntel() {
  els.intelRefreshButton.disabled = true;
  els.portfolioMeta.textContent = "Digesting project docs via Squire (free, may take a minute)...";
  try {
    const data = await api("/api/intel/refresh", { method: "POST", body: "{}" });
    const summary = data.summary || {};
    if (!summary.squire_up) {
      els.portfolioMeta.textContent = "Squire is unreachable; intel left as-is.";
      return;
    }
    await loadProjects();
    els.portfolioMeta.textContent =
      `Intel: ${Number(summary.digested || 0)} digested, ${Number(summary.skipped_unchanged || 0)} unchanged, ${Number(summary.failed || 0)} failed.`;
  } catch (error) {
    els.portfolioMeta.textContent = `Intel refresh failed: ${error.message}`;
  } finally {
    els.intelRefreshButton.disabled = false;
  }
}

async function loadSquire() {
  const data = await api("/api/squire/status");
  state.squire = data.squire || null;
  renderSquire();
}

function endpointUpLabel(up) {
  return up === null || up === undefined ? "unknown" : up ? "up" : "down";
}

// M6.5-Full: render remote-reported Squire live-activity/queue snapshots. Always show
// provenance + staleness — a snapshot is a remote self-report, NEVER independently
// verified live truth (the squire_probe honesty rule). Stale snapshots are greyed.
const SQUIRE_SNAPSHOT_STALE_S = 60;

function renderSquireActivity(activity) {
  if (!activity.length) return;
  const header = document.createElement("p");
  header.className = "muted";
  header.textContent = "Live activity (reported by the remote box)";
  els.squireClients.appendChild(header);
  for (const snap of activity) {
    const p = snap.payload || {};
    const age = snap.age_seconds;
    const stale = age === null || age === undefined || age > SQUIRE_SNAPSHOT_STALE_S;
    const ageText = age === null || age === undefined ? "age unknown" : `${age}s ago`;
    const job = p.live_job || {};
    const jobLabel = job.label || (typeof job === "string" ? job : "");
    const elapsed = job.elapsed_ms ? ` · ${Math.round(job.elapsed_ms / 1000)}s elapsed` : "";
    const queue = Array.isArray(p.queue) ? p.queue : [];
    const depth = p.depth !== undefined ? p.depth : queue.length;
    const models = Array.isArray(p.models) ? p.models : [];
    const card = document.createElement("article");
    card.className = `project-card${stale ? " squire-stale" : ""}`;
    const queueRows = queue.slice(0, 8).map((q) => {
      const cl = q.client || q.name || "?";
      const st = q.status ? ` — ${q.status}` : "";
      const pu = q.purpose ? ` (${q.purpose})` : "";
      return `<li>${escapeHtml(String(cl))}${escapeHtml(st)}${escapeHtml(pu)}</li>`;
    }).join("");
    card.innerHTML = `
      <div class="card-head">
        <div>
          <div class="eyebrow">squire activity · reported by @${escapeHtml(snap.host || "?")}</div>
          <h2>${jobLabel ? escapeHtml(String(jobLabel)) + escapeHtml(elapsed) : "idle / no job reported"}</h2>
        </div>
        <span class="risk-pill ${stale ? "squire-down" : ""}" title="How long ago this snapshot was received. A stale snapshot is not live truth.">${escapeHtml(ageText)}${stale ? " · stale" : ""}</span>
      </div>
      <div class="meta-row">
        <span>queue depth: ${Number(depth) || 0}</span>
        ${models.length ? `<span>models: ${escapeHtml(models.slice(0, 3).join(", "))}</span>` : ""}
      </div>
      ${queueRows ? `<ul class="muted">${queueRows}</ul>` : ""}
      <p class="muted"><small>Remote self-report from @${escapeHtml(snap.host || "?")} — BigBoss cannot independently verify Squire's live state.</small></p>
    `;
    els.squireClients.appendChild(card);
  }
}

function renderSquire() {
  const squire = state.squire;
  if (!squire) return;
  const endpoints = squire.endpoints || [];
  const upLabel = endpointUpLabel(squire.up);
  els.squireMeta.textContent =
    `${endpoints.length} endpoint${endpoints.length === 1 ? "" : "s"}. Last probe: ${squire.last_health_at ? agoLabel(squire.last_health_at) : "never"}. Window: ${squire.window_days}d.`;
  const totals = squire.totals || {};
  els.squireSummary.innerHTML = `
    <div><span class="${squire.up === false ? "squire-down" : ""}">${upLabel}</span><small>squire</small></div>
    <div><span>${Number(totals.calls || 0)}</span><small>calls</small></div>
    <div><span>${Number(totals.errors || 0)}</span><small>errors</small></div>
  `;
  els.squireClients.innerHTML = "";
  els.squireEmpty.classList.toggle("hidden", endpoints.length > 0);

  renderSquireActivity(squire.activity || []);

  for (const ep of endpoints) {
    const optIn = ep.auto_ok === false || (ep.auto_ok === undefined && ep.endpoint !== "squire");
    const up = ep.up;
    const card = document.createElement("article");
    card.className = `project-card${up === false ? " blocked" : ""}`;
    card.innerHTML = `
      <div class="card-head">
        <div>
          <div class="eyebrow">endpoint${optIn ? " / opt-in, rate-limited" : " / auto"}</div>
          <h2>${escapeHtml(ep.endpoint)}</h2>
        </div>
        <span class="risk-pill ${up === false ? "squire-down" : ""}">${endpointUpLabel(up)}</span>
      </div>
      <div class="meta-row">
        <span>${Number(ep.calls || 0)} calls</span>
        <span>${Number(ep.prompt_tokens || 0)} in / ${Number(ep.completion_tokens || 0)} out</span>
        <span>~${Number(ep.avg_latency_ms || 0)}ms</span>
        ${ep.errors ? `<span class="blocker-pill">${Number(ep.errors)} errors</span>` : ""}
        ${ep.last_call_at ? `<span>${escapeHtml(agoLabel(ep.last_call_at))}</span>` : ""}
        ${ep.last_health_error ? `<span class="blocker-pill" title="${escapeHtml(ep.last_health_error)}">unreachable</span>` : ""}
      </div>
      ${ep.exposure ? `
      <div class="meta-row">
        <span class="exposure-pill" title="Known off-box channel on this machine. Every send is recorded in the egress audit.">exposure: ${escapeHtml(ep.exposure)}</span>
        <span>${Number(ep.egress_sends || 0)} sends audited${ep.last_egress_at ? ` (last ${escapeHtml(agoLabel(ep.last_egress_at))})` : ""}</span>
      </div>` : ""}
    `;
    els.squireClients.appendChild(card);
  }

  const clients = squire.clients || [];
  if (clients.length) {
    const header = document.createElement("p");
    header.className = "muted";
    header.textContent = "By client";
    els.squireClients.appendChild(header);
  }
  for (const client of clients) {
    const card = document.createElement("article");
    card.className = "project-card";
    card.innerHTML = `
      <div class="card-head">
        <div>
          <div class="eyebrow">squire client</div>
          <h2>${escapeHtml(client.client)}</h2>
        </div>
      </div>
      <div class="meta-row">
        <span>${Number(client.calls || 0)} calls</span>
        <span>${Number(client.prompt_tokens || 0)} in / ${Number(client.completion_tokens || 0)} out</span>
        <span>~${Number(client.avg_latency_ms || 0)}ms</span>
        ${client.errors ? `<span class="blocker-pill">${Number(client.errors)} errors</span>` : ""}
        ${client.last_call_at ? `<span>${escapeHtml(agoLabel(client.last_call_at))}</span>` : ""}
      </div>
    `;
    els.squireClients.appendChild(card);
  }
}

function daysSince(iso) {
  if (!iso) return Infinity;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return Infinity;
  return Math.floor((Date.now() - then) / 86400000);
}

function agoLabel(iso) {
  const days = daysSince(iso);
  if (!Number.isFinite(days)) return "no activity";
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  return `${days}d ago`;
}

function whereILeftOff(project) {
  const signals = [
    ["handoff", project.last_handoff_at],
    ["session", project.last_transcript_at],
    ["commit", project.last_git_commit_at]
  ].filter(([, at]) => at);
  if (!signals.length) return "";
  signals.sort((a, b) => (a[1] < b[1] ? 1 : -1));
  const [label, at] = signals[0];
  return `${label} ${agoLabel(at)}`;
}

// Any received frame (event or heartbeat) proves the socket is alive and advances
// our replay cursor so a reconnect only re-fetches what we missed (E3.5).
function bumpLiveness(event) {
  if (event && event.lastEventId) {
    const id = parseInt(event.lastEventId, 10);
    if (!Number.isNaN(id)) state.lastEventId = id;
  }
  state.lastMessageAt = Date.now();
}

function wireLiveness() {
  if (state.livenessWired) return;
  state.livenessWired = true;
  // Reconnect the moment a backgrounded/slept tab becomes visible again — its
  // socket may have died silently without firing onerror.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && token()) {
      if (!state.source || Date.now() - state.lastMessageAt > SSE_STALE_MS) connectEvents();
    }
  });
  // Watchdog: if nothing (not even a heartbeat) arrives within the stale window,
  // treat the socket as dead and reconnect.
  state.watchdogTimer = setInterval(() => {
    if (state.source && token() && Date.now() - state.lastMessageAt > SSE_STALE_MS) {
      connectEvents();
    }
  }, 10000);
}

// Trailing debounce for SSE-triggered refetches: a burst of events (esp. the replay on a fresh
// connect) collapses into a single reload instead of one GET per event.
const _reloadTimers = {};
function scheduleReload(key, fn, delayMs = 200) {
  if (_reloadTimers[key]) return;
  _reloadTimers[key] = setTimeout(() => {
    _reloadTimers[key] = null;
    fn();
  }, delayMs);
}
function scheduleApprovalsReload() {
  scheduleReload("approvals", () => loadApprovals().catch(handleAuthFailure));
}

async function connectEvents() {
  if (state.source) {
    state.source.close();
  }
  clearTimeout(state.reconnectTimer);
  wireLiveness();
  try {
    const data = await api("/api/events/token", { method: "POST", body: "{}" });
    // last_id makes the server replay only events created since we last heard —
    // bounded catch-up instead of the whole log.
    const source = new EventSource(
      `/api/events?stream_token=${encodeURIComponent(data.stream_token)}&last_id=${state.lastEventId}`
    );
    state.source = source;
    state.lastMessageAt = Date.now();
    els.liveState.textContent = "Live";
    els.liveState.classList.add("online");
    const on = (type, fn) => source.addEventListener(type, (e) => { bumpLiveness(e); if (fn) fn(e); });
    // Coalesce refetches: on a fresh connect the server replays the bounded event window, so a
    // burst of approval events would otherwise fire one full GET /api/approvals each. Debounce to one.
    on("approval.created", scheduleApprovalsReload);
    on("approval.resolved", scheduleApprovalsReload);
    on("device.paired", null);
    on("heartbeat", null);
    on("intel.updated", () => {
      if (state.view === "portfolio") scheduleReload("projects", () => loadProjects().catch(() => {}));
    });
    on("squire.health.changed", () => {
      if (state.view === "squire") scheduleReload("squire", () => loadSquire().catch(() => {}));
    });
    // M6.5-Full: remote (and local) harness sessions + Squire snapshots refresh their
    // panels live, so cross-machine activity appears without a manual reload.
    on("harness.session.reported", () => {
      if (state.view === "fleet") scheduleReload("fleet", () => loadFleet().catch(() => {}));
    });
    on("remote.snapshot.reported", () => {
      if (state.view === "squire") scheduleReload("squire", () => loadSquire().catch(() => {}));
    });
    source.onerror = () => {
      els.liveState.textContent = "Reconnecting";
      els.liveState.classList.remove("online");
      source.close();
      if (state.source === source) state.source = null;
      state.reconnectTimer = setTimeout(connectEvents, 2000);
    };
  } catch (error) {
    els.liveState.textContent = "Offline";
    els.liveState.classList.remove("online");
    state.source = null;
    state.reconnectTimer = setTimeout(connectEvents, 4000);
  }
}

function setFilter(status) {
  state.status = status;
  els.pendingFilter.classList.toggle("active", status === "pending");
  els.allFilter.classList.toggle("active", status === "all");
  loadApprovals().catch(handleAuthFailure);
}

function logout() {
  Object.values(storage).forEach((key) => localStorage.removeItem(key));
  if (state.source) {
    state.source.close();
  }
  showPair();
}

async function boot() {
  const autoPairCode = readPairFromUrl();
  if (!token()) {
    showPair();
    setPairMode("code");
    if (autoPairCode) {
      await pairDevice(new Event("autopair"));
    }
    return;
  }
  try {
    const me = await api("/api/me");
    localStorage.setItem(storage.deviceName, me.device_name);
    showQueue();
    await loadApprovals();
    await connectEvents();
  } catch (error) {
    handleAuthFailure(error);
    // Transient boot failure (server restarting) — keep the paired UI up and let reconnect refill,
    // instead of stranding the operator on the pairing screen. (401/403 already logged out above.)
    if (!(error && (error.status === 401 || error.status === 403))) {
      showQueue();
      connectEvents();
    }
  }
}

function handleAuthFailure(error) {
  console.warn(error);
  // Only de-pair on a GENUINE auth failure (revoked device / bad token). A transient error — a 5xx, a
  // network blip, or the server briefly down during a restart — must NOT wipe the device pairing, or a
  // refresh mid-hiccup kicks the operator to the pairing screen. Keep credentials; show a soft state and
  // let the SSE watchdog + a refresh recover.
  if (error && (error.status === 401 || error.status === 403)) {
    logout();
    return;
  }
  const live = document.querySelector("#live-state");
  if (live) live.textContent = "Reconnecting…";
}

function shortWorkspace(workspace) {
  if (!workspace) return "workspace";
  const parts = workspace.replaceAll("\\", "/").split("/").filter(Boolean);
  return parts[parts.length - 1] || workspace;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

els.pairForm.addEventListener("submit", pairDevice);
els.pairPinMode.addEventListener("click", () => setPairMode("pin"));
els.pairCodeMode.addEventListener("click", () => setPairMode("code"));
els.demoButton.addEventListener("click", createDemoApproval);
els.logoutButton.addEventListener("click", logout);
els.pendingFilter.addEventListener("click", () => setFilter("pending"));
els.allFilter.addEventListener("click", () => setFilter("all"));
els.approvalsViewButton.addEventListener("click", () => setView("approvals"));
els.portfolioViewButton.addEventListener("click", () => setView("portfolio"));
els.squireViewButton.addEventListener("click", () => setView("squire"));
els.daemonsViewButton.addEventListener("click", () => setView("daemons"));
els.registryRefreshButton.addEventListener("click", () => refreshRegistry());
els.intelRefreshButton.addEventListener("click", () => refreshIntel());
els.squireRefreshButton.addEventListener("click", () => loadSquire().catch(handleAuthFailure));
els.daemonsRefreshButton.addEventListener("click", () => loadDaemons().catch(handleAuthFailure));
els.fleetViewButton.addEventListener("click", () => setView("fleet"));
els.fleetRefreshButton.addEventListener("click", () => loadFleet().catch(handleAuthFailure));

boot();
