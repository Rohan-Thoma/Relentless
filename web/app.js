/* ==========================================================================
   RELENTLESS // PERSONAL AUTONOMOUS JOB HUNTING AGENT (ES MODULE, 0 NPM DEPS)
   ========================================================================== */

const STORAGE_BACKEND_URL = "relentless_backend_url";
const STORAGE_API_KEY = "relentless_api_key";
const STORAGE_BEARER = "relentless_bearer_token";
const STORAGE_USE_HEADER = "relentless_use_api_header";

const VALID_VIEWS = new Set(["dashboard", "pipeline", "studio", "okf", "eval", "config", "info"]);

const state = {
  view: "dashboard",
  subTabs: {
    dashboard: "overview",
    okf: "graph",
    eval: "overview",
    config: "profile",
  },
  backendUrl: (localStorage.getItem(STORAGE_BACKEND_URL) || "").replace(/\/+$/, ""),
  apiKey: localStorage.getItem(STORAGE_API_KEY) || "",
  bearerToken: localStorage.getItem(STORAGE_BEARER) || "",
  useApiHeader: localStorage.getItem(STORAGE_USE_HEADER) === "true",

  authenticated: false,
  authMode: "unknown",
  authPromptedOnce: false,

  systemStatus: null,
  pollTimer: null,
  pollIntervalMs: 12000,
  isPolling: false,

  // Pipeline state
  pipelineMode: "kanban", // "kanban" | "table"
  pipelineFilters: {
    stage: "",
    fit_tier: "",
    source_ats: "",
    search: "",
    limit: 100,
    offset: 0,
  },
  pipelineData: { items: [], total: 0 },

  // Studio state
  studioJobId: null,
  studioJob: null,
  studioLeftTab: "intel", // "intel" | "jd" | "events"
  studioRightTab: "pdf", // "pdf" | "shots" | "json"
  lastInFlightStage: null,
  blobUrls: [],
  pendingSubmitJobId: null,

  // OKF state
  okfData: { concepts: [], summary: {} },
  selectedOkfPath: null,
  okfLoadedOnce: false,

  // Eval & Config state
  evalData: null,
  settingsData: null,
  profileFormLoadedOnce: false,
  engineFormLoadedOnce: false,

  // Interactive tag-pill store & modal state
  tagBoxes: {},
  activeTagEdit: null,
};

// ==============================================================================
// Helpers & Security Sanitizers
// ==============================================================================
function $(id) {
  return document.getElementById(id);
}

function esc(val) {
  if (val === null || val === undefined) return "";
  return String(val)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function safeExternalUrl(raw) {
  if (!raw) return "";
  try {
    const parsed = new URL(String(raw), window.location.origin);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      return parsed.href;
    }
  } catch {
    return "";
  }
  return "";
}

function isModifiedClick(ev) {
  return ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey || ev.button !== 0;
}

function pct(val, digits = 0) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "—";
  return `${(Number(val) * 100).toFixed(digits)}%`;
}

function fmtTime(isoStr) {
  if (!isoStr) return "—";
  const d = new Date(isoStr);
  if (Number.isNaN(d.getTime())) return String(isoStr);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function toast(message, kind = "info") {
  const stack = $("toast-stack");
  if (!stack) return;
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  stack.appendChild(el);
  window.setTimeout(() => {
    el.remove();
  }, 4500);
}

function revokeBlobUrls() {
  for (const url of state.blobUrls) {
    try {
      URL.revokeObjectURL(url);
    } catch {
      // Ignore invalid blob URLs
    }
  }
  state.blobUrls = [];
}

const MONTH_SHORT_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTH_LOOKUP = {
  jan: "01",
  january: "01",
  feb: "02",
  february: "02",
  mar: "03",
  march: "03",
  apr: "04",
  april: "04",
  may: "05",
  jun: "06",
  june: "06",
  jul: "07",
  july: "07",
  aug: "08",
  august: "08",
  sep: "09",
  sept: "09",
  september: "09",
  oct: "10",
  october: "10",
  nov: "11",
  november: "11",
  dec: "12",
  december: "12",
};

function setTagBox(boxId, items) {
  const seen = new Set();
  const clean = [];
  for (const item of Array.isArray(items) ? items : []) {
    const val = String(item || "").trim();
    if (!val || seen.has(val)) continue;
    seen.add(val);
    clean.push(val);
  }
  state.tagBoxes[boxId] = clean;
  renderTagBox(boxId);
}

function getTagBox(boxId) {
  return [...(state.tagBoxes[boxId] || [])];
}

function renderTagBox(boxId) {
  const container = $(boxId);
  if (!container) return;
  const tags = state.tagBoxes[boxId] || [];
  const pillsHtml = tags
    .map(
      (tag, idx) =>
        `<button type="button" class="tag-pill" data-tag-box-id="${esc(boxId)}" data-tag-index="${idx}" title="Click to edit or remove">${esc(tag)}</button>`
    )
    .join("");
  container.innerHTML = `
    ${pillsHtml || `<span class="muted-copy" style="font-size:12px;margin-right:4px;">No items added</span>`}
    <button type="button" class="tag-add-btn" data-tag-add="${esc(boxId)}" title="Add new item">+ ADD</button>
  `;
}

function initAllTagBoxes() {
  document.querySelectorAll(".tag-box[id]").forEach((el) => {
    if (!state.tagBoxes[el.id]) state.tagBoxes[el.id] = [];
    renderTagBox(el.id);
  });
}

function openTagModal(boxId, index = null) {
  const boxEl = $(boxId);
  const label = (boxEl && boxEl.dataset.tagLabel) || "Tag";
  const isEdit = index !== null && index !== undefined;

  if (isEdit) {
    const idxNum = Number(index);
    const currentVal = (state.tagBoxes[boxId] || [])[idxNum] ?? "";
    state.activeTagEdit = { boxId, index: idxNum, label };
    $("tag-modal-title").textContent = `EDIT // ${label.toUpperCase()}`;
    $("tag-modal-field-label").textContent = `${label.toUpperCase()} VALUE`;
    $("tag-modal-input").value = currentVal;
    $("btn-tag-submit").textContent = "SAVE CHANGES";
    $("btn-tag-delete").classList.remove("hidden");
  } else {
    state.activeTagEdit = { boxId, index: null, label };
    $("tag-modal-title").textContent = `ADD // ${label.toUpperCase()}`;
    $("tag-modal-field-label").textContent = `NEW ${label.toUpperCase()} VALUE`;
    $("tag-modal-input").value = "";
    $("btn-tag-submit").textContent = "ADD TAG";
    $("btn-tag-delete").classList.add("hidden");
  }

  $("tag-modal").classList.remove("hidden");
  window.setTimeout(() => {
    const inp = $("tag-modal-input");
    if (inp) {
      inp.focus();
      inp.select();
    }
  }, 10);
}

function closeTagModal() {
  state.activeTagEdit = null;
  const modal = $("tag-modal");
  if (modal) modal.classList.add("hidden");
}

function submitTagModal(ev) {
  ev.preventDefault();
  if (!state.activeTagEdit) return;
  const { boxId, index } = state.activeTagEdit;
  const raw = $("tag-modal-input").value.trim();
  if (!raw) {
    toast("Enter a non-empty value before saving.", "warn");
    return;
  }
  const tags = [...(state.tagBoxes[boxId] || [])];
  const dupIdx = tags.findIndex((t, i) => t.toLowerCase() === raw.toLowerCase() && i !== index);
  if (dupIdx !== -1) {
    toast(`"${raw}" already exists in this list.`, "warn");
    return;
  }
  if (index === null) {
    tags.push(raw);
  } else {
    tags[index] = raw;
  }
  setTagBox(boxId, tags);
  closeTagModal();
}

function deleteActiveTag() {
  if (!state.activeTagEdit || state.activeTagEdit.index === null) return;
  const { boxId, index } = state.activeTagEdit;
  const tags = [...(state.tagBoxes[boxId] || [])];
  tags.splice(index, 1);
  setTagBox(boxId, tags);
  closeTagModal();
}

function parseSingleMonthToken(token) {
  const clean = String(token || "").trim();
  if (!clean) return { ym: "", isPresent: false, ok: true };
  if (/^(present|current|now|ongoing|till date)$/i.test(clean)) {
    return { ym: "", isPresent: true, ok: true };
  }
  const isoMatch = clean.match(/^(\d{4})-(\d{2})$/);
  if (isoMatch) {
    const m = Number(isoMatch[2]);
    if (m >= 1 && m <= 12) return { ym: `${isoMatch[1]}-${isoMatch[2]}`, isPresent: false, ok: true };
  }
  const slashMatch = clean.match(/^(\d{1,2})[/-](\d{4})$/);
  if (slashMatch) {
    const m = Number(slashMatch[1]);
    if (m >= 1 && m <= 12) return { ym: `${slashMatch[2]}-${String(m).padStart(2, "0")}`, isPresent: false, ok: true };
  }
  const wordMatch = clean.match(/^([a-zA-Z]+)\.?,?\s+(\d{4})$/);
  if (wordMatch) {
    const mm = MONTH_LOOKUP[wordMatch[1].toLowerCase()];
    if (mm) return { ym: `${wordMatch[2]}-${mm}`, isPresent: false, ok: true };
  }
  const yearMatch = clean.match(/^(\d{4})$/);
  if (yearMatch) {
    return { ym: `${yearMatch[1]}-01`, isPresent: false, ok: true };
  }
  return { ym: "", isPresent: false, ok: false };
}

function parseDateRangeToMonthInputs(rawStr) {
  const clean = String(rawStr || "").trim();
  if (!clean) return { startYm: "", endYm: "", isPresent: false, parsedCleanly: true };

  let parts = clean.split(/\s+(?:–|—|-|to)\s+/i);
  if (parts.length !== 2) {
    const tightMatch = clean.match(/^(.+?)\s*[–—-]\s*(present|current|now|ongoing|till date|\w+\.?\s+\d{4}|\d{4})$/i);
    if (tightMatch) {
      parts = [tightMatch[1], tightMatch[2]];
    }
  }

  if (parts.length === 2) {
    const left = parseSingleMonthToken(parts[0]);
    const right = parseSingleMonthToken(parts[1]);
    return {
      startYm: left.ym,
      endYm: right.isPresent ? "" : right.ym,
      isPresent: right.isPresent,
      parsedCleanly: left.ok && right.ok,
    };
  }

  const single = parseSingleMonthToken(clean);
  return {
    startYm: single.isPresent ? "" : single.ym,
    endYm: "",
    isPresent: single.isPresent,
    parsedCleanly: single.ok,
  };
}

function ymToMonYear(ym) {
  const m = String(ym || "").match(/^(\d{4})-(\d{2})$/);
  if (!m) return "";
  const idx = Number(m[2]) - 1;
  if (idx < 0 || idx > 11) return "";
  return `${MONTH_SHORT_NAMES[idx]} ${m[1]}`;
}

function formatMonthInputsToDateRange(startYm, endYm, isPresent) {
  const startLabel = ymToMonYear(startYm);
  if (isPresent) return startLabel ? `${startLabel} - Present` : "Present";
  const endLabel = ymToMonYear(endYm);
  if (startLabel && endLabel) return `${startLabel} - ${endLabel}`;
  return startLabel || endLabel || "";
}

function setSelectOrCustom(id, rawValue) {
  const el = $(id);
  if (!el) return;
  const val = rawValue === null || rawValue === undefined ? "" : String(rawValue);
  if (el.tagName !== "SELECT") {
    el.value = val;
    return;
  }
  const existingOpt = [...el.options].find((o) => o.value === val);
  if (!existingOpt && val !== "") {
    const customOpt = document.createElement("option");
    customOpt.value = val;
    customOpt.textContent = `${val} (Custom / Saved)`;
    el.appendChild(customOpt);
  }
  el.value = val;
}

function fitBadgeClass(tier) {
  if (tier === "dream_role" || tier === "strong_fit") return "badge-lime";
  if (tier === "moderate_fit") return "badge-purple";
  if (tier === "weak_fit") return "badge-amber";
  if (tier === "reject") return "badge-red";
  return "";
}

function stageBadgeClass(stage) {
  if (stage === "READY_FOR_REVIEW") return "badge-lime";
  if (stage === "APPLIED" || stage === "CALLBACK") return "badge-purple";
  if (stage === "PROCESSING" || stage === "EDITING" || stage === "SUBMITTING") return "badge-amber";
  return "";
}

// ==============================================================================
// URL Routing & Multi-Tab Sync
// ==============================================================================
function parseRouteFromUrl() {
  const params = new URLSearchParams(window.location.search);
  let targetView = (params.get("view") || "").trim().toLowerCase();
  if (!VALID_VIEWS.has(targetView)) {
    const pathSeg = window.location.pathname.replace(/^\/+|\/+$/g, "").toLowerCase();
    targetView = VALID_VIEWS.has(pathSeg) ? pathSeg : "dashboard";
  }

  const jobParam = Number(params.get("job") || 0);
  const subParam = (params.get("sub") || "").trim().toLowerCase();

  state.view = targetView;
  if (jobParam > 0) {
    state.studioJobId = jobParam;
    $("studio-jump-id").value = String(jobParam);
    $("nav-badge-studio").textContent = `#${jobParam}`;
  }
  if (subParam && state.subTabs[targetView] !== undefined) {
    state.subTabs[targetView] = subParam;
  }
}

function pushRouteToUrl({ replace = false } = {}) {
  const params = new URLSearchParams();
  if (state.view && state.view !== "dashboard") {
    params.set("view", state.view);
  }
  if (state.view === "studio" && state.studioJobId) {
    params.set("job", String(state.studioJobId));
  }
  if (state.subTabs[state.view]) {
    params.set("sub", state.subTabs[state.view]);
  }
  const qs = params.toString();
  const nextUrl = qs ? `/?${qs}` : "/";
  if (replace) {
    window.history.replaceState({}, "", nextUrl);
  } else if (window.location.pathname + window.location.search !== nextUrl) {
    window.history.pushState({}, "", nextUrl);
  }
}

// ==============================================================================
// API Client & Auth Handling
// ==============================================================================
function buildApiUrl(path) {
  if (!state.backendUrl) return path;
  return `${state.backendUrl}${path}`;
}

async function apiFetch(path, options = {}) {
  const {
    method = "GET",
    body = undefined,
    responseType = "json",
    suppressAuthModal = false,
  } = options;

  const headers = new Headers(options.headers || {});
  if (body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (state.useApiHeader && state.apiKey) {
    headers.set("X-Relentless-Api-Key", state.apiKey);
  } else if (state.bearerToken) {
    headers.set("Authorization", `Bearer ${state.bearerToken}`);
  }

  const fetchOpts = {
    method,
    headers,
    credentials: state.backendUrl ? "omit" : "same-origin",
  };
  if (body !== undefined) {
    fetchOpts.body = typeof body === "string" ? body : JSON.stringify(body);
  }

  const resp = await fetch(buildApiUrl(path), fetchOpts);

  if (resp.status === 401) {
    state.authenticated = false;
    updateConnectionBadge(false, "UNAUTHORIZED");
    if (!suppressAuthModal && !state.authPromptedOnce) {
      state.authPromptedOnce = true;
      openAuthModal();
    }
    throw new Error("Unauthorized — please authenticate in Security Access");
  }

  if (responseType === "blob") {
    if (!resp.ok) {
      let msg = `HTTP ${resp.status}`;
      try {
        const errJson = await resp.json();
        msg = errJson.error || errJson.message || msg;
      } catch {
        // Ignore non-JSON error
      }
      throw new Error(msg);
    }
    return await resp.blob();
  }

  const contentType = (resp.headers.get("content-type") || "").toLowerCase();
  const text = await resp.text();
  if (
    contentType.includes("text/html") ||
    /^\s*<!doctype\s+html/i.test(text) ||
    /^\s*<html[\s>]/i.test(text)
  ) {
    throw new Error(
      "Edge proxy returned HTML instead of JSON (check Netlify edge function /api/* routing)",
    );
  }
  let parsed = null;
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    parsed = { ok: resp.ok, raw: text };
  }

  if (!resp.ok) {
    const errMsg =
      parsed && parsed.error && parsed.message && parsed.error !== parsed.message
        ? `${parsed.error}: ${parsed.message}`
        : (parsed && (parsed.error || parsed.message)) || `HTTP ${resp.status}`;
    throw new Error(errMsg);
  }
  return parsed;
}

function updateConnectionBadge(ok, modeText) {
  const dot = $("conn-dot");
  const label = $("conn-label");
  if (!dot || !label) return;
  dot.className = `status-dot ${ok ? "dot-lime" : "dot-red"}`;
  label.textContent = ok
    ? `ONLINE // ${String(modeText || "READY").toUpperCase()}`
    : `OFFLINE // ${modeText || "LOCKED"}`;
}

function openAuthModal() {
  $("auth-backend-url").value = state.backendUrl;
  $("auth-secret-input").value = state.apiKey;
  $("auth-use-header").checked = state.useApiHeader;
  $("auth-modal").classList.remove("hidden");
}

function closeAuthModal() {
  $("auth-modal").classList.add("hidden");
}

async function checkAuthAndBoot() {
  try {
    let authRes;
    try {
      authRes = await apiFetch("/api/auth/status", { suppressAuthModal: true });
    } catch (firstErr) {
      if (state.backendUrl) {
        state.backendUrl = "";
        localStorage.removeItem(STORAGE_BACKEND_URL);
        authRes = await apiFetch("/api/auth/status", { suppressAuthModal: true });
      } else {
        throw firstErr;
      }
    }
    state.authenticated = Boolean(authRes.authenticated);
    state.authMode = authRes.mode || "session";
    if (!state.authenticated) {
      updateConnectionBadge(false, authRes.auth_required ? "AUTH REQUIRED" : "DISCONNECTED");
      openAuthModal();
      return false;
    }
    updateConnectionBadge(true, state.authMode);
    applyViewDom();
    await refreshTelemetryAndActiveView();
    scheduleNextPoll();
    return true;
  } catch (err) {
    const msg = String(err && err.message ? err.message : "");
    if (msg.toLowerCase().includes("unauthorized") || msg.includes("401")) {
      updateConnectionBadge(false, "AUTH REQUIRED");
      openAuthModal();
    } else {
      updateConnectionBadge(false, "UNREACHABLE");
      toast(`Backend connection failed: ${msg || "Network error"}`, "error");
    }
    return false;
  }
}

async function checkNetlifyProtectionExpiry() {
  if (state.backendUrl) return;
  try {
    const resp = await fetch("/", { method: "HEAD", credentials: "same-origin" });
    const raw = resp.headers.get("Netlify-Site-Protection-Expires-In");
    if (raw !== null) {
      const remaining = Number.parseInt(raw, 10);
      if (!Number.isNaN(remaining) && remaining < 60) {
        window.location.reload();
      }
    }
  } catch {
    // Not on Netlify Private Project or offline
  }
}

// ==============================================================================
// Navigation & Sub-Tab Switching
// ==============================================================================
function applyViewDom() {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === state.view);
  });
  document.querySelectorAll(".view-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `view-${state.view}`);
  });
  applySubTabDom(state.view);
}

function applySubTabDom(viewName) {
  const activeSub = state.subTabs[viewName];
  if (!activeSub) return;

  const bar = document.querySelector(`[data-subtab-group="${viewName}"]`);
  if (bar) {
    bar.querySelectorAll(".subtab-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.subtab === activeSub);
    });
  }

  const prefixMap = {
    dashboard: "dash-sub-",
    okf: "okf-sub-",
    eval: "eval-sub-",
    config: "cfg-sub-",
  };
  const prefix = prefixMap[viewName];
  if (prefix) {
    document.querySelectorAll(`[id^="${prefix}"]`).forEach((el) => {
      el.classList.toggle("active", el.id === `${prefix}${activeSub}`);
    });
  }
}

function switchView(viewName, { pushHistory = true } = {}) {
  if (!VALID_VIEWS.has(viewName)) return;
  state.view = viewName;
  applyViewDom();
  if (pushHistory) pushRouteToUrl();

  if (!state.authenticated) return;

  if (viewName === "dashboard") {
    loadDashboardPriorityJobs();
  } else if (viewName === "pipeline") {
    loadPipelineJobs();
  } else if (viewName === "studio" && state.studioJobId) {
    loadStudioJob(state.studioJobId, true);
  } else if (viewName === "okf") {
    loadOkfBrain().then(() => {
      if (state.subTabs.okf === "graph") okfGraph.ensureStarted();
    });
  } else if (viewName === "eval") {
    loadEvalData();
  } else if (viewName === "config") {
    loadConfigVault();
  }
}

function switchSubTab(groupName, subName) {
  state.subTabs[groupName] = subName;
  applySubTabDom(groupName);
  pushRouteToUrl();

  if (groupName === "okf" && subName === "graph") {
    okfGraph.ensureStarted();
  }
}

function scheduleNextPoll() {
  if (state.pollTimer) {
    window.clearTimeout(state.pollTimer);
  }
  state.pollTimer = window.setTimeout(async () => {
    if (state.authenticated) {
      await pollTick();
    }
    scheduleNextPoll();
  }, state.pollIntervalMs);
}

async function pollTick() {
  if (state.isPolling) return;
  state.isPolling = true;
  try {
    await fetchSystemStatus();
    if (state.view === "studio" && state.studioJobId) {
      await refreshStudioInBackground();
    } else if (state.view === "pipeline") {
      await loadPipelineJobs({ silent: true });
    } else if (state.view === "dashboard") {
      await loadDashboardPriorityJobs({ silent: true });
    }
  } catch {
    // Handled inside apiFetch
  } finally {
    state.isPolling = false;
  }
}

async function refreshTelemetryAndActiveView() {
  await fetchSystemStatus();
  if (state.view === "dashboard") await loadDashboardPriorityJobs();
  else if (state.view === "pipeline") await loadPipelineJobs();
  else if (state.view === "studio" && state.studioJobId) await loadStudioJob(state.studioJobId, true);
  else if (state.view === "okf") await loadOkfBrain();
  else if (state.view === "eval") await loadEvalData();
  else if (state.view === "config") await loadConfigVault();
}

// ==============================================================================
// System Telemetry & Mission Control (View 1)
// ==============================================================================
async function fetchSystemStatus() {
  const status = await apiFetch("/api/status");
  state.systemStatus = status;
  updateConnectionBadge(true, state.authMode);

  const activeJobs = status.active_jobs || {};
  const sysTasks = status.system_tasks || [];
  const activeCount = Object.keys(activeJobs).length + sysTasks.length;

  state.pollIntervalMs = activeCount > 0 ? 3000 : 12000;
  $("poll-interval-label").textContent = `${state.pollIntervalMs / 1000}s`;
  $("active-task-count").textContent = String(activeCount);
  $("last-sync-label").textContent = new Date().toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

  const isPaused = Boolean(status.is_paused);
  $("auto-dot").className = `status-dot ${isPaused ? "dot-amber" : "dot-lime"}`;
  $("auto-label").textContent = isPaused
    ? `AUTO: PAUSED (${fmtTime(status.paused_until)})`
    : "AUTO: ACTIVE";
  const topPauseBtn = $("btn-top-pause");
  if (topPauseBtn) {
    topPauseBtn.textContent = isPaused ? "▶ RESUME AUTO" : "⏸ PAUSE 24H";
    topPauseBtn.className = `btn ${isPaused ? "btn-lime" : "btn-warn"}`;
  }

  const tickerParts = [];
  for (const [jid, stageDesc] of Object.entries(activeJobs)) {
    tickerParts.push(`#${jid}: ${stageDesc}`);
  }
  for (const t of sysTasks) {
    tickerParts.push(`[SYS] ${t}`);
  }
  $("ticker-text").textContent = tickerParts.length
    ? tickerParts.join("  |  ")
    : "IDLE — ALL THREADS NOMINAL";

  const unreviewedCount = Math.max(0, (status.triage_passed || 0) - (status.human_labeled || 0));
  $("nav-badge-inbox").textContent = String(unreviewedCount);
  $("nav-badge-total").textContent = String(status.total_jobs || 0);

  if (state.view === "dashboard") {
    renderDashboardTelemetry(status);
  }
}

function renderDashboardTelemetry(status) {
  const total = status.total_jobs || 0;
  const passed = status.triage_passed || 0;
  const labeled = status.human_labeled || 0;
  const unreviewed = Math.max(0, passed - labeled);
  const stages = status.stage_counts || {};
  const outcomes = status.outcome_counts || {};

  const readyCount = stages.READY_FOR_REVIEW || 0;
  const appliedCount = stages.APPLIED || (status.action_counts && status.action_counts.APPLIED) || 0;
  const callbacks =
    (outcomes.CALLBACK || 0) + (outcomes.INTERVIEW || 0) + (outcomes.OFFER || 0);
  const passRate = total > 0 ? `${((passed / total) * 100).toFixed(1)}%` : "0%";

  $("dash-kpis").innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">TOTAL DISCOVERED</div>
      <div class="kpi-value">${total}</div>
      <div class="kpi-sub">Ingested across ATS &amp; Gmail</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">PASSED SYSTEM-1 JEV</div>
      <div class="kpi-value text-purple">${passed}</div>
      <div class="kpi-sub">Pass rate: ${passRate}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">UNREVIEWED INBOX</div>
      <div class="kpi-value">${unreviewed}</div>
      <div class="kpi-sub">Awaiting HITL triage</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">READY FOR REVIEW</div>
      <div class="kpi-value">${readyCount}</div>
      <div class="kpi-sub">Tailored PDF + Form checked</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">APPLIED</div>
      <div class="kpi-value">${appliedCount}</div>
      <div class="kpi-sub">Auto + manual submissions</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">CALLBACKS / INTERVIEWS</div>
      <div class="kpi-value text-lime">${callbacks}</div>
      <div class="kpi-sub">Positive funnel outcomes</div>
    </div>
  `;

  const activeJobs = status.active_jobs || {};
  const sysTasks = status.system_tasks || [];
  const totalInFlight = Object.keys(activeJobs).length + sysTasks.length;
  $("dash-inflight-tag").textContent = `${totalInFlight} IN-FLIGHT`;

  if (totalInFlight === 0) {
    $("dash-live-tasks").innerHTML = `<div class="muted-copy">No active Playwright or LLM tailoring workers executing right now.</div>`;
  } else {
    const items = [];
    for (const [jid, desc] of Object.entries(activeJobs)) {
      items.push(`
        <div class="task-item">
          <div>
            <span class="badge badge-amber">JOB #${esc(jid)}</span>
            <span class="mono" style="margin-left:8px;">${esc(desc)}</span>
          </div>
          <a class="btn btn-sm btn-purple" href="/?view=studio&job=${esc(jid)}" data-open-studio="${esc(jid)}">OPEN STUDIO</a>
        </div>
      `);
    }
    for (const taskName of sysTasks) {
      items.push(`
        <div class="task-item">
          <div>
            <span class="badge badge-purple">SYSTEM</span>
            <span class="mono" style="margin-left:8px;">${esc(taskName)}</span>
          </div>
          <span class="badge badge-lime">RUNNING</span>
        </div>
      `);
    }
    $("dash-live-tasks").innerHTML = items.join("");
  }

  const maxStage = Math.max(1, ...Object.values(stages));
  const stageEntries = Object.entries(stages).sort((a, b) => b[1] - a[1]);
  if (!stageEntries.length) {
    $("dash-stage-bars").innerHTML = `<div class="muted-copy">No jobs recorded yet.</div>`;
  } else {
    $("dash-stage-bars").innerHTML = stageEntries
      .map(([name, count]) => {
        const widthPct = Math.max(3, Math.round((count / maxStage) * 100));
        const fillCls = name === "READY_FOR_REVIEW" || name === "CALLBACK" ? "lime" : "";
        return `
          <div class="bar-row">
            <div class="bar-meta">
              <span>${esc(name)}</span>
              <strong>${count}</strong>
            </div>
            <div class="bar-track">
              <div class="bar-fill ${fillCls}" style="width:${widthPct}%"></div>
            </div>
          </div>
        `;
      })
      .join("");
  }
}

async function loadDashboardPriorityJobs({ silent = false } = {}) {
  try {
    const res = await apiFetch("/api/jobs?stage=UNREVIEWED&limit=8&offset=0");
    const items = res.items || [];
    const target = $("dash-priority-jobs");
    if (!items.length) {
      target.innerHTML = `<div class="muted-copy">Inbox zero! No unreviewed high-fit jobs waiting.</div>`;
      return;
    }
    target.innerHTML = renderJobsTableHtml(items);
  } catch (err) {
    if (!silent) toast(`Failed to load priority jobs: ${err.message}`, "error");
  }
}

// ==============================================================================
// Jobs Pipeline: Kanban & Table (View 2)
// ==============================================================================
async function loadPipelineJobs({ silent = false } = {}) {
  const f = state.pipelineFilters;
  const qs = new URLSearchParams();
  if (f.stage) qs.set("stage", f.stage);
  if (f.fit_tier) qs.set("fit_tier", f.fit_tier);
  if (f.source_ats) qs.set("source_ats", f.source_ats);
  if (f.search.trim()) qs.set("search", f.search.trim());
  qs.set("limit", String(f.limit));
  qs.set("offset", String(f.offset));

  try {
    const data = await apiFetch(`/api/jobs?${qs.toString()}`);
    state.pipelineData = data;
    renderPipeline();
  } catch (err) {
    if (!silent) toast(`Pipeline load failed: ${err.message}`, "error");
  }
}

function renderPipeline() {
  const { items = [], total = 0, limit = 100, offset = 0 } = state.pipelineData;
  const pageNum = Math.floor(offset / limit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / limit));

  $("pipeline-count-label").textContent = `${total} TOTAL JOBS // SHOWING ${items.length}`;
  $("page-indicator").textContent = `PAGE ${pageNum} / ${totalPages}`;
  $("btn-page-prev").disabled = offset <= 0;
  $("btn-page-next").disabled = offset + limit >= total;

  const container = $("pipeline-content");
  if (!items.length) {
    container.innerHTML = `
      <div class="hud-card empty-state">
        <div class="empty-title">NO MATCHING OPPORTUNITIES</div>
        <p>Adjust stage, fit tier, ATS source, or search query filters above.</p>
      </div>
    `;
    return;
  }

  if (state.pipelineMode === "kanban") {
    container.innerHTML = renderKanbanHtml(items);
  } else {
    container.innerHTML = `<div class="hud-card"><div class="card-body">${renderJobsTableHtml(items)}</div></div>`;
  }
}

function categorizeKanbanBucket(job) {
  const st = (job.workflow_stage || "").toUpperCase();
  const out = (job.outcome_status || "").toUpperCase();
  if (out === "CALLBACK" || out === "INTERVIEW" || out === "OFFER" || st === "CALLBACK") {
    return "CALLBACK";
  }
  if (st === "APPLIED" || (job.human_action === "APPLIED" && !st)) return "APPLIED";
  if (st === "READY_FOR_REVIEW") return "READY_FOR_REVIEW";
  if (
    job.in_flight_stage ||
    st === "PROCESSING" ||
    st === "EDITING" ||
    st === "AWAITING_EDIT_NOTE" ||
    st === "SUBMITTING"
  ) {
    return "IN_PROGRESS";
  }
  return "UNREVIEWED";
}

function renderKanbanHtml(items) {
  const cols = [
    { key: "UNREVIEWED", label: "UNREVIEWED" },
    { key: "IN_PROGRESS", label: "IN PROGRESS" },
    { key: "READY_FOR_REVIEW", label: "READY FOR REVIEW" },
    { key: "APPLIED", label: "APPLIED" },
    { key: "CALLBACK", label: "CALLBACK / OUTCOME" },
  ];

  const grouped = {
    UNREVIEWED: [],
    IN_PROGRESS: [],
    READY_FOR_REVIEW: [],
    APPLIED: [],
    CALLBACK: [],
  };
  for (const job of items) {
    grouped[categorizeKanbanBucket(job)].push(job);
  }

  return `
    <div class="kanban-board">
      ${cols
        .map((c) => {
          const cards = grouped[c.key];
          return `
            <div class="kanban-col">
              <div class="kanban-col-head">
                <span>${esc(c.label)}</span>
                <span class="badge badge-purple">${cards.length}</span>
              </div>
              <div class="kanban-col-body">
                ${
                  cards.length
                    ? cards.map((j) => renderJobCardHtml(j)).join("")
                    : `<div class="muted-copy" style="padding:10px;text-align:center;">Empty</div>`
                }
              </div>
            </div>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderJobCardHtml(job) {
  const fitVal = job.jev_fit_prob_cal ?? job.jev_fit_prob;
  const tier = job.jev_fit_tier || "unknown";
  const atsBadge = job.source_ats ? `<span class="badge">${esc(job.source_ats)}</span>` : "";
  const pdfBadge = job.has_pdf ? `<span class="badge badge-lime">PDF</span>` : "";
  const busyBadge = job.in_flight_stage
    ? `<span class="badge badge-amber">${esc(job.in_flight_stage)}</span>`
    : "";
  const blockerBadge = job.apply_blocker
    ? `<span class="badge badge-red" title="${esc(job.apply_blocker)}">BLOCKER</span>`
    : "";

  return `
    <div class="job-card">
      <div class="job-card-top">
        <span class="mono-tag">#${job.id}</span>
        <span class="badge ${fitBadgeClass(tier)}">${pct(fitVal)} // ${esc(tier)}</span>
      </div>
      <div>
        <a class="job-card-title job-title-link" href="/?view=studio&job=${job.id}" data-open-studio="${job.id}">${esc(job.title)}</a>
        <div class="job-card-company">${esc(job.company)}</div>
        <div class="muted-copy" style="font-size:11px;">${esc(job.location || "Remote / Unspecified")}</div>
      </div>
      <div class="job-card-meta">
        ${atsBadge}
        ${pdfBadge}
        ${busyBadge}
        ${blockerBadge}
      </div>
      <div class="job-card-actions">
        <a class="btn btn-sm btn-purple" href="/?view=studio&job=${job.id}" data-open-studio="${job.id}">🔬 STUDIO</a>
        <button class="btn btn-sm btn-lime" data-job-interest="${job.id}" title="Tailor Resume & Inspect Form">🎯 TAILOR</button>
        <button class="btn btn-sm btn-outline" data-job-action="SKIP_BAD_FIT" data-job-id="${job.id}" title="Skip: Bad Fit">👎</button>
        <button class="btn btn-sm btn-danger" data-job-action="SKIP_SCAM" data-job-id="${job.id}" title="Skip: Scam">🚫</button>
      </div>
    </div>
  `;
}

function renderJobsTableHtml(items) {
  return `
    <div class="hud-table-wrap">
      <table class="hud-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>ROLE &amp; COMPANY</th>
            <th>ATS</th>
            <th>JEV FIT &amp; COSINE</th>
            <th>STAGE &amp; STATUS</th>
            <th>ACTIONS</th>
          </tr>
        </thead>
        <tbody>
          ${items
            .map((j) => {
              const fitVal = j.jev_fit_prob_cal ?? j.jev_fit_prob;
              const tier = j.jev_fit_tier || "unknown";
              const st = j.in_flight_stage || j.workflow_stage || "DISCOVERED";
              const extUrl = safeExternalUrl(j.apply_url);
              return `
                <tr>
                  <td><span class="mono-tag">#${j.id}</span></td>
                  <td>
                    <a class="job-title-link" href="/?view=studio&job=${j.id}" data-open-studio="${j.id}">${esc(j.title)}</a>
                    <div style="color:var(--purple-bright);font-size:12px;">
                      ${esc(j.company)}
                      <span style="color:var(--text-muted);">— ${esc(j.location || "N/A")}</span>
                    </div>
                  </td>
                  <td><span class="badge">${esc(j.source_ats)}</span></td>
                  <td>
                    <span class="badge ${fitBadgeClass(tier)}">${pct(fitVal)} ${esc(tier)}</span>
                    <div class="mono" style="font-size:11px;color:var(--text-muted);margin-top:3px;">
                      cos: ${pct(j.cosine_sim)} | scam: ${pct(j.jev_scam_prob)}
                    </div>
                  </td>
                  <td>
                    <span class="badge ${stageBadgeClass(st)}">${esc(st)}</span>
                    ${j.has_pdf ? `<span class="badge badge-lime">PDF</span>` : ""}
                    ${j.apply_blocker ? `<div class="text-amber" style="font-size:11px;margin-top:3px;">⚠ ${esc(j.apply_blocker)}</div>` : ""}
                  </td>
                  <td>
                    <div style="display:flex;gap:5px;flex-wrap:wrap;">
                      <a class="btn btn-sm btn-purple" href="/?view=studio&job=${j.id}" data-open-studio="${j.id}">🔬 STUDIO</a>
                      <button class="btn btn-sm btn-lime" data-job-interest="${j.id}">🎯 TAILOR</button>
                      <button class="btn btn-sm btn-outline" data-job-action="CALLBACK" data-job-id="${j.id}" title="Mark Callback">⭐</button>
                      <button class="btn btn-sm btn-outline" data-job-action="SKIP_BAD_FIT" data-job-id="${j.id}" title="Skip: Bad Fit">👎</button>
                      ${
                        extUrl
                          ? `<a class="btn btn-sm btn-outline" href="${esc(extUrl)}" target="_blank" rel="noopener noreferrer" title="Open ATS URL">🔗</a>`
                          : ""
                      }
                    </div>
                  </td>
                </tr>
              `;
            })
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

// ==============================================================================
// Application Studio: Split-Screen Review & Resume Tailor (View 3)
// ==============================================================================
async function openStudioForJob(jobId) {
  state.studioJobId = Number(jobId);
  $("studio-jump-id").value = String(jobId);
  $("nav-badge-studio").textContent = `#${jobId}`;
  switchView("studio");
}

async function loadStudioJob(jobId, reloadMedia = true) {
  try {
    const job = await apiFetch(`/api/jobs/${jobId}`);
    state.studioJob = job;
    state.studioJobId = job.id;
    state.lastInFlightStage = job.in_flight_stage || null;

    $("studio-empty").classList.add("hidden");
    $("studio-workbench").classList.remove("hidden");
    $("studio-subtitle").textContent = `Active Inspection Target: #${job.id} — ${job.title} @ ${job.company}`;
    $("shot-count-tab").textContent = String((job.screenshots || []).length);

    renderStudioBanner(job);
    renderStudioLeftPane(job);
    if (reloadMedia) {
      await renderStudioRightPane(job);
    }
  } catch (err) {
    toast(`Failed to load Job #${jobId}: ${err.message}`, "error");
  }
}

async function refreshStudioInBackground() {
  if (!state.studioJobId) return;
  try {
    const job = await apiFetch(`/api/jobs/${state.studioJobId}`, { suppressAuthModal: true });
    const prevInFlight = state.lastInFlightStage;
    const nowInFlight = job.in_flight_stage || null;
    state.studioJob = job;
    state.lastInFlightStage = nowInFlight;
    $("shot-count-tab").textContent = String((job.screenshots || []).length);

    renderStudioBanner(job);
    if (state.studioLeftTab !== "jd") {
      renderStudioLeftPane(job);
    }
    if (prevInFlight && !nowInFlight) {
      toast(`Background task finished for Job #${job.id}. Reloading artifacts...`);
      await renderStudioRightPane(job);
    }
  } catch {
    // Ignore transient poll errors
  }
}

function renderStudioBanner(job) {
  const fitVal = job.jev_fit_prob_cal ?? job.jev_fit_prob;
  const stage = job.in_flight_stage || job.workflow_stage || "DISCOVERED";
  const extUrl = safeExternalUrl(job.apply_url);

  $("studio-banner").innerHTML = `
    <div class="studio-banner-info">
      <h2>#${job.id} // ${esc(job.title)} <span class="accent-purple">@ ${esc(job.company)}</span></h2>
      <div class="studio-banner-meta">
        <span>${esc(job.location || "Remote / Unspecified")}</span>
        <span class="badge">${esc(job.source_ats)}</span>
        <span class="badge ${fitBadgeClass(job.jev_fit_tier)}">FIT ${pct(fitVal)} (${esc(job.jev_fit_tier || "unknown")})</span>
        <span class="badge ${stageBadgeClass(stage)}">${esc(stage)}</span>
        ${job.human_action ? `<span class="badge badge-purple">HUMAN: ${esc(job.human_action)}</span>` : ""}
        ${job.outcome_status && job.outcome_status !== "PENDING" ? `<span class="badge badge-lime">OUTCOME: ${esc(job.outcome_status)}</span>` : ""}
        ${extUrl ? `<a class="btn btn-sm btn-outline" href="${esc(extUrl)}" target="_blank" rel="noopener noreferrer">🔗 OPEN EXTERNAL ATS</a>` : ""}
      </div>
    </div>
    <div class="studio-banner-actions">
      <button class="btn btn-purple" data-job-interest="${job.id}">🎯 START TAILOR &amp; INSPECT</button>
      <button class="btn btn-lime" data-open-submit="${job.id}">🚀 AUTO-SUBMIT NOW</button>
      <button class="btn btn-outline" data-job-action="MANUALLY_APPLIED" data-job-id="${job.id}">✅ APPLIED MANUALLY</button>
      <button class="btn btn-outline" data-job-action="CALLBACK" data-job-id="${job.id}">⭐ CALLBACK</button>
      <button class="btn btn-outline" data-job-action="INTERVIEW" data-job-id="${job.id}">🎤 INTERVIEW</button>
      <button class="btn btn-outline" data-job-action="SKIP_BAD_FIT" data-job-id="${job.id}">👎 SKIP</button>
      <button class="btn btn-danger" data-job-action="SKIP_SCAM" data-job-id="${job.id}">🚫 SCAM</button>
    </div>
  `;
}

function renderStudioLeftPane(job) {
  const container = $("studio-left-content");
  if (state.studioLeftTab === "jd") {
    const struct = job.jd_structure
      ? `<div class="kw-group"><div class="kw-group-title">PARSED JD STRUCTURE</div><pre class="terminal-pre">${esc(JSON.stringify(job.jd_structure, null, 2))}</pre></div>`
      : "";
    container.innerHTML = `
      ${struct}
      <div class="kw-group">
        <div class="kw-group-title">RAW JOB DESCRIPTION</div>
        <pre class="terminal-pre">${esc(job.jd_text || "No JD text captured for this listing.")}</pre>
      </div>
    `;
    return;
  }

  if (state.studioLeftTab === "events") {
    const applyEvents = job.apply_events || [];
    const outcomeEvents = job.outcome_events || [];
    container.innerHTML = `
      <div class="kw-group">
        <div class="kw-group-title">PLAYWRIGHT APPLY EVENTS (${applyEvents.length})</div>
        ${
          applyEvents.length
            ? `<pre class="terminal-pre">${esc(JSON.stringify(applyEvents, null, 2))}</pre>`
            : `<div class="muted-copy">No Playwright apply attempts logged yet.</div>`
        }
      </div>
      <div class="kw-group">
        <div class="kw-group-title">OUTCOME TIMELINE (${outcomeEvents.length})</div>
        ${
          outcomeEvents.length
            ? `<pre class="terminal-pre">${esc(JSON.stringify(outcomeEvents, null, 2))}</pre>`
            : `<div class="muted-copy">No callback/interview/offer outcomes recorded yet.</div>`
        }
      </div>
    `;
    return;
  }

  const sc = job.ats_scorecard || {};
  const matched = sc.matched_skills_after || [];
  const added = sc.newly_added_terms || [];
  const missing = sc.missing_jd_skills || [];
  const editNotes = job.user_edit_notes || [];

  container.innerHTML = `
    <div class="scorecard-grid">
      <div class="mini-stat">
        <div class="mini-stat-label">JEV CALIBRATED FIT</div>
        <div class="mini-stat-val text-lime">${pct(job.jev_fit_prob_cal ?? job.jev_fit_prob, 1)}</div>
      </div>
      <div class="mini-stat">
        <div class="mini-stat-label">BASELINE COSINE</div>
        <div class="mini-stat-val">${pct(job.cosine_sim, 1)}</div>
      </div>
      <div class="mini-stat">
        <div class="mini-stat-label">SCAM PROB</div>
        <div class="mini-stat-val">${pct(job.jev_scam_prob, 1)}</div>
      </div>
      <div class="mini-stat">
        <div class="mini-stat-label">MATCHED PERSONA</div>
        <div class="mini-stat-val text-purple" style="font-size:12px;">${esc(job.jev_persona || "—")}</div>
      </div>
      <div class="mini-stat">
        <div class="mini-stat-label">ATS BEFORE → AFTER</div>
        <div class="mini-stat-val text-lime">${sc.overall_before_pct !== undefined ? `${sc.overall_before_pct}% → ${sc.overall_after_pct}%` : "—"}</div>
      </div>
      <div class="mini-stat">
        <div class="mini-stat-label">IMPACT DENSITY</div>
        <div class="mini-stat-val text-purple">${sc.impact_density_pct !== undefined ? `${sc.impact_density_pct}%` : "—"}</div>
      </div>
    </div>

    ${
      job.apply_blocker
        ? `<div class="task-item" style="border-color:#782028;margin-bottom:12px;">
            <div>
              <span class="badge badge-red">APPLY BLOCKER</span>
              <span class="mono" style="margin-left:8px;">${esc(job.apply_blocker)}</span>
            </div>
            <span class="badge">${esc(job.apply_status || "BLOCKED")}</span>
          </div>`
        : ""
    }

    ${
      matched.length || added.length || missing.length
        ? `
      <div class="kw-group">
        <div class="kw-group-title">MATCHED ATS SKILLS (${matched.length})</div>
        <div class="kw-list">
          ${matched.map((k) => `<span class="badge badge-lime">${esc(k)}</span>`).join("") || `<span class="muted-copy">None</span>`}
        </div>
      </div>
      <div class="kw-group">
        <div class="kw-group-title">NEWLY INJECTED ATS TERMS (${added.length})</div>
        <div class="kw-list">
          ${added.map((k) => `<span class="badge badge-purple">${esc(k)}</span>`).join("") || `<span class="muted-copy">None</span>`}
        </div>
      </div>
      <div class="kw-group">
        <div class="kw-group-title">MISSING JD SKILLS (${missing.length})</div>
        <div class="kw-list">
          ${missing.map((k) => `<span class="badge badge-amber">${esc(k)}</span>`).join("") || `<span class="muted-copy">None</span>`}
        </div>
      </div>
    `
        : ""
    }

    <div class="kw-group">
      <div class="kw-group-title">SURGICAL DIFF &amp; RATIONALE</div>
      <pre class="terminal-pre">${esc(job.tailored_diff || "Not tailored yet. Click 'START TAILOR & INSPECT' above.")}</pre>
    </div>

    ${
      editNotes.length
        ? `<div class="kw-group">
            <div class="kw-group-title">HUMAN REVISION NOTES (${editNotes.length})</div>
            <pre class="terminal-pre">${esc(JSON.stringify(editNotes, null, 2))}</pre>
          </div>`
        : ""
    }
  `;
}

async function renderStudioRightPane(job) {
  revokeBlobUrls();
  const container = $("studio-right-content");

  if (state.studioRightTab === "json") {
    const jsonData = job.tailored_resume_json;
    container.innerHTML = jsonData
      ? `<pre class="terminal-pre">${esc(JSON.stringify(jsonData, null, 2))}</pre>`
      : `<div class="empty-state"><div class="empty-title">NO TAILORED RESUME JSON YET</div><p>Start resume tailoring for Job #${job.id} first.</p></div>`;
    return;
  }

  if (state.studioRightTab === "shots") {
    const shots = job.screenshots || [];
    if (!shots.length) {
      container.innerHTML = `
        <div class="empty-state">
          <div class="empty-title">NO PLAYWRIGHT SCREENSHOTS CAPTURED</div>
          <p>Run <strong>🎯 START TAILOR &amp; INSPECT</strong> or <strong>🚀 AUTO-SUBMIT NOW</strong> to capture browser form evidence.</p>
        </div>
      `;
      return;
    }

    container.innerHTML = `<div class="muted-copy">Loading ${shots.length} Playwright screenshots...</div>`;
    const cards = [];
    for (let i = 0; i < shots.length; i += 1) {
      try {
        const blob = await apiFetch(`/api/jobs/${job.id}/screenshot/${i}`, {
          responseType: "blob",
        });
        const objUrl = URL.createObjectURL(blob);
        state.blobUrls.push(objUrl);
        cards.push(`
          <div class="shot-card" data-lightbox-src="${objUrl}" data-lightbox-title="Job #${job.id} — Screenshot #${i + 1}">
            <img src="${objUrl}" alt="Playwright form screenshot ${i + 1}" />
            <div class="shot-caption">CAPTURE #${i + 1} // CLICK TO ZOOM</div>
          </div>
        `);
      } catch (err) {
        cards.push(`
          <div class="shot-card">
            <div class="shot-caption text-amber">Screenshot #${i + 1} unavailable: ${esc(err.message)}</div>
          </div>
        `);
      }
    }
    container.innerHTML = `<div class="shot-gallery">${cards.join("")}</div>`;
    return;
  }

  if (!job.has_pdf && !job.tailored_pdf_path) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-title">NO TAILORED PDF GENERATED YET</div>
        <p>Click <strong>🎯 START TAILOR &amp; INSPECT</strong> to synthesize a verified PDF tailored to this role.</p>
      </div>
    `;
    return;
  }

  container.innerHTML = `<div class="muted-copy">Fetching tailored PDF artifact from backend...</div>`;
  try {
    const blob = await apiFetch(`/api/jobs/${job.id}/pdf`, { responseType: "blob" });
    const objUrl = URL.createObjectURL(blob);
    state.blobUrls.push(objUrl);
    container.innerHTML = `<iframe class="pdf-frame" src="${objUrl}" title="Tailored Resume PDF"></iframe>`;
  } catch (err) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-title">PDF PREVIEW UNAVAILABLE</div>
        <p>${esc(err.message)}</p>
      </div>
    `;
  }
}

async function triggerJobInterest(jobId) {
  try {
    const res = await apiFetch(`/api/jobs/${jobId}/interest`, {
      method: "POST",
      body: {},
    });
    toast(`Tailoring & inspection started for Job #${res.job_id}`);
    await fetchSystemStatus();
    if (state.view === "studio" && state.studioJobId === Number(jobId)) {
      await loadStudioJob(jobId, false);
    } else if (state.view === "pipeline") {
      await loadPipelineJobs({ silent: true });
    }
  } catch (err) {
    toast(`Could not start Job #${jobId}: ${err.message}`, "error");
  }
}

async function triggerJobAction(jobId, action) {
  try {
    await apiFetch(`/api/jobs/${jobId}/action`, {
      method: "POST",
      body: { action },
    });
    toast(`Job #${jobId} marked as ${action}`);
    await refreshTelemetryAndActiveView();
  } catch (err) {
    toast(`Action failed: ${err.message}`, "error");
  }
}

async function sendStudioEdit() {
  if (!state.studioJobId) return;
  const note = $("studio-edit-note").value.trim();
  if (!note) {
    toast("Enter an edit instruction before sending.", "warn");
    return;
  }
  try {
    await apiFetch(`/api/jobs/${state.studioJobId}/edit`, {
      method: "POST",
      body: { custom_note: note },
    });
    $("studio-edit-note").value = "";
    toast(`Resume edit queued for Job #${state.studioJobId}`);
    await loadStudioJob(state.studioJobId, false);
    await fetchSystemStatus();
  } catch (err) {
    toast(`Edit request failed: ${err.message}`, "error");
  }
}

function openSubmitModal(jobId) {
  state.pendingSubmitJobId = Number(jobId);
  const co = state.studioJob && state.studioJob.id === Number(jobId) ? state.studioJob.company : "Target";
  const title = state.studioJob && state.studioJob.id === Number(jobId) ? state.studioJob.title : `Job #${jobId}`;
  $("submit-modal-summary").innerHTML = `
    You are about to trigger autonomous Playwright form filling and submission for
    <strong>#${jobId} — ${esc(title)} @ ${esc(co)}</strong>.<br><br>
    Respects your <code>automation.apply_mode</code> setting in <code>config.yaml</code>.
  `;
  $("submit-modal").classList.remove("hidden");
}

async function confirmExecuteSubmit() {
  const jobId = state.pendingSubmitJobId;
  $("submit-modal").classList.add("hidden");
  if (!jobId) return;

  toast(`Submitting Job #${jobId} via Playwright runner...`, "warn");
  try {
    const res = await apiFetch(`/api/jobs/${jobId}/submit`, {
      method: "POST",
      body: {},
    });
    const statusLabel = res.status || (res.ok ? "SUBMITTED" : "CHECK_LOGS");
    toast(`Submit result for #${jobId}: ${statusLabel}`, res.ok ? "info" : "warn");
    await loadStudioJob(jobId, true);
    await fetchSystemStatus();
  } catch (err) {
    toast(`Submit failed: ${err.message}`, "error");
  }
}

// ==============================================================================
// OKF Knowledge Brain & Interactive Obsidian-Style Force Graph (View 4)
// ==============================================================================
class OkfGraphController {
  constructor() {
    this.canvas = null;
    this.ctx = null;
    this.nodes = [];
    this.edges = [];
    this.adj = new Map();
    this.nodeMap = new Map();

    this.panX = 0;
    this.panY = 0;
    this.zoom = 1;
    this.dragNode = null;
    this.isPanning = false;
    this.dragMoved = false;
    this.lastPointer = { x: 0, y: 0 };
    this.hoveredId = null;
    this.selectedId = null;

    this.rafId = null;
    this.energy = 1;
    this.bound = false;
  }

  initDom() {
    if (this.bound) return;
    this.canvas = $("okf-graph-canvas");
    if (!this.canvas) return;
    this.ctx = this.canvas.getContext("2d");

    const resize = () => this.syncCanvasSize();

    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(resize).observe(this.canvas.parentElement);
    }
    window.addEventListener("resize", resize);

    this.canvas.addEventListener("pointerdown", (ev) => this.onPointerDown(ev));
    window.addEventListener("pointermove", (ev) => this.onPointerMove(ev));
    window.addEventListener("pointerup", (ev) => this.onPointerUp(ev));
    this.canvas.addEventListener("wheel", (ev) => this.onWheel(ev), { passive: false });

    this.bound = true;
    this.syncCanvasSize();
  }

  syncCanvasSize() {
    if (!this.canvas || !this.canvas.parentElement) return;
    const rect = this.canvas.parentElement.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dpr = window.devicePixelRatio || 1;
    const nextW = Math.round(rect.width * dpr);
    const nextH = Math.round(rect.height * dpr);
    if (this.canvas.width !== nextW || this.canvas.height !== nextH) {
      this.canvas.width = nextW;
      this.canvas.height = nextH;
      this.renderFrame();
    }
  }

  setConcepts(concepts) {
    this.initDom();
    const prevPositions = new Map(this.nodes.map((n) => [n.id, { x: n.x, y: n.y }]));
    this.nodes = [];
    this.edges = [];
    this.adj = new Map();
    this.nodeMap = new Map();

    const hubSet = new Map();
    for (const c of concepts) {
      const ctype = (c.concept_type || "General").trim();
      const hubId = `hub:${ctype}`;
      if (!hubSet.has(hubId)) {
        hubSet.set(hubId, ctype);
      }
    }

    const hubList = [...hubSet.entries()];
    hubList.forEach(([hubId, label], idx) => {
      const angle = (idx / Math.max(1, hubList.length)) * Math.PI * 2;
      const prev = prevPositions.get(hubId);
      const node = {
        id: hubId,
        kind: "hub",
        label,
        radius: 11,
        x: prev ? prev.x : Math.cos(angle) * 145,
        y: prev ? prev.y : Math.sin(angle) * 145,
        vx: 0,
        vy: 0,
        concept: null,
      };
      this.nodes.push(node);
      this.nodeMap.set(hubId, node);
      this.adj.set(hubId, new Set());
    });

    concepts.forEach((c, idx) => {
      const cId = `concept:${c.path}`;
      const ctype = (c.concept_type || "General").trim();
      const hubId = `hub:${ctype}`;
      const hubNode = this.nodeMap.get(hubId);
      const angle = (idx * 2.39996) % (Math.PI * 2);
      const prev = prevPositions.get(cId);
      const x0 = prev ? prev.x : (hubNode ? hubNode.x : 0) + Math.cos(angle) * 70;
      const y0 = prev ? prev.y : (hubNode ? hubNode.y : 0) + Math.sin(angle) * 70;

      const node = {
        id: cId,
        kind: "concept",
        label: c.name || c.path.split("/").pop() || c.path,
        radius: 6.5,
        x: x0,
        y: y0,
        vx: 0,
        vy: 0,
        concept: c,
      };
      this.nodes.push(node);
      this.nodeMap.set(cId, node);
      this.adj.set(cId, new Set());

      if (hubNode) {
        this.addEdge(cId, hubId, "type");
      }
    });

    // Connect concepts sharing tags or explicit path references
    const tagIndex = new Map();
    for (const c of concepts) {
      for (const t of c.tags || []) {
        const key = String(t).trim().toLowerCase();
        if (!key) continue;
        if (!tagIndex.has(key)) tagIndex.set(key, []);
        tagIndex.get(key).push(`concept:${c.path}`);
      }
    }

    const edgeKeys = new Set();
    const connectUnique = (a, b, kind) => {
      if (a === b) return;
      const key = a < b ? `${a}|${b}` : `${b}|${a}`;
      if (edgeKeys.has(key)) return;
      edgeKeys.add(key);
      this.addEdge(a, b, kind);
    };

    for (const group of tagIndex.values()) {
      for (let i = 0; i < group.length; i += 1) {
        for (let j = i + 1; j < group.length; j += 1) {
          connectUnique(group[i], group[j], "tag");
        }
      }
    }

    // Cross-mention edges when a concept body references another concept path
    for (const a of concepts) {
      const bodyLow = String(a.body || "").toLowerCase();
      if (!bodyLow) continue;
      for (const b of concepts) {
        if (a.path === b.path) continue;
        if (bodyLow.includes(b.path.toLowerCase())) {
          connectUnique(`concept:${a.path}`, `concept:${b.path}`, "mention");
        }
      }
    }

    $("graph-node-count").textContent = `${concepts.length} CONCEPTS // ${this.edges.length} LINKS`;
    this.energy = 1.0;
    this.ensureStarted();
  }

  addEdge(a, b, kind) {
    this.edges.push({ a, b, kind });
    if (this.adj.has(a)) this.adj.get(a).add(b);
    if (this.adj.has(b)) this.adj.get(b).add(a);
  }

  screenToWorld(clientX, clientY) {
    const rect = this.canvas.getBoundingClientRect();
    const cx = rect.width / 2;
    const cy = rect.height / 2;
    const x = (clientX - rect.left - cx - this.panX) / this.zoom;
    const y = (clientY - rect.top - cy - this.panY) / this.zoom;
    return { x, y };
  }

  hitTest(worldX, worldY) {
    for (let i = this.nodes.length - 1; i >= 0; i -= 1) {
      const n = this.nodes[i];
      const dx = worldX - n.x;
      const dy = worldY - n.y;
      const hitR = n.radius + 7 / this.zoom;
      if (dx * dx + dy * dy <= hitR * hitR) {
        return n;
      }
    }
    return null;
  }

  onPointerDown(ev) {
    if (!this.canvas) return;
    const w = this.screenToWorld(ev.clientX, ev.clientY);
    const hit = this.hitTest(w.x, w.y);
    this.lastPointer = { x: ev.clientX, y: ev.clientY };
    this.dragMoved = false;
    if (hit) {
      this.dragNode = hit;
    } else {
      this.isPanning = true;
    }
    this.ensureStarted();
  }

  onPointerMove(ev) {
    if (!this.canvas || state.view !== "okf" || state.subTabs.okf !== "graph") return;

    if (this.dragNode) {
      const w = this.screenToWorld(ev.clientX, ev.clientY);
      this.dragNode.x = w.x;
      this.dragNode.y = w.y;
      this.dragNode.vx = 0;
      this.dragNode.vy = 0;
      this.dragMoved = true;
      this.energy = Math.max(this.energy, 0.35);
      this.ensureStarted();
      return;
    }

    if (this.isPanning) {
      this.panX += ev.clientX - this.lastPointer.x;
      this.panY += ev.clientY - this.lastPointer.y;
      this.lastPointer = { x: ev.clientX, y: ev.clientY };
      this.dragMoved = true;
      this.renderFrame();
      return;
    }

    const w = this.screenToWorld(ev.clientX, ev.clientY);
    const hit = this.hitTest(w.x, w.y);
    const nextHover = hit ? hit.id : null;
    if (nextHover !== this.hoveredId) {
      this.hoveredId = nextHover;
      this.canvas.style.cursor = hit ? "pointer" : "grab";
      this.renderFrame();
    }
  }

  onPointerUp() {
    if (this.dragNode && !this.dragMoved) {
      this.selectNode(this.dragNode.id);
    }
    this.dragNode = null;
    this.isPanning = false;
  }

  onWheel(ev) {
    ev.preventDefault();
    const factor = ev.deltaY < 0 ? 1.12 : 0.89;
    this.zoom = Math.max(0.3, Math.min(3.0, this.zoom * factor));
    this.renderFrame();
  }

  resetCamera() {
    this.panX = 0;
    this.panY = 0;
    this.zoom = 1;
    this.renderFrame();
  }

  reheat() {
    this.energy = 1.0;
    this.ensureStarted();
  }

  selectNode(nodeId) {
    this.selectedId = nodeId;
    const node = this.nodeMap.get(nodeId);
    renderGraphInspector(node, this.adj.get(nodeId) || new Set(), this.nodeMap);
    this.renderFrame();
  }

  stepPhysics() {
    const nLen = this.nodes.length;
    if (nLen === 0) return false;

    // Gentle centering & repulsion
    for (let i = 0; i < nLen; i += 1) {
      const a = this.nodes[i];
      a.vx += -a.x * 0.0025;
      a.vy += -a.y * 0.0025;

      for (let j = i + 1; j < nLen; j += 1) {
        const b = this.nodes[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let distSq = dx * dx + dy * dy;
        if (distSq < 1) {
          dx = 0.7;
          dy = 0.7;
          distSq = 1;
        }
        const force = (a.kind === "hub" && b.kind === "hub" ? 3600 : 1400) / distSq;
        const dist = Math.sqrt(distSq);
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        a.vx -= fx;
        a.vy -= fy;
        b.vx += fx;
        b.vy += fy;
      }
    }

    // Spring edges
    for (const edge of this.edges) {
      const a = this.nodeMap.get(edge.a);
      const b = this.nodeMap.get(edge.b);
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const ideal = edge.kind === "type" ? 72 : 105;
      const spring = (dist - ideal) * (edge.kind === "type" ? 0.032 : 0.015);
      const fx = (dx / dist) * spring;
      const fy = (dy / dist) * spring;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }

    let maxSpeed = 0;
    for (const n of this.nodes) {
      if (this.dragNode === n) continue;
      n.vx *= 0.82;
      n.vy *= 0.82;
      n.vx = Math.max(-14, Math.min(14, n.vx));
      n.vy = Math.max(-14, Math.min(14, n.vy));
      n.x += n.vx;
      n.y += n.vy;
      maxSpeed = Math.max(maxSpeed, Math.abs(n.vx) + Math.abs(n.vy));
    }

    this.energy = Math.max(this.energy * 0.96, maxSpeed * 0.15);
    return maxSpeed > 0.08 || this.dragNode !== null;
  }

  renderFrame() {
    if (!this.canvas || !this.ctx) return;
    const ctx = this.ctx;
    const dpr = window.devicePixelRatio || 1;
    const w = this.canvas.width / dpr;
    const h = this.canvas.height / dpr;
    if (!w || !h) return;

    ctx.save();
    ctx.scale(dpr, dpr);
    ctx.fillStyle = "#0a0d13";
    ctx.fillRect(0, 0, w, h);

    ctx.translate(w / 2 + this.panX, h / 2 + this.panY);
    ctx.scale(this.zoom, this.zoom);

    const focusId = this.hoveredId || this.selectedId;
    const focusNeighbors = focusId ? this.adj.get(focusId) || new Set() : null;

    // Draw edges (flat vectors, no glow)
    for (const edge of this.edges) {
      const a = this.nodeMap.get(edge.a);
      const b = this.nodeMap.get(edge.b);
      if (!a || !b) continue;

      const isFocusEdge = focusId && (edge.a === focusId || edge.b === focusId);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);

      if (isFocusEdge) {
        ctx.strokeStyle = edge.kind === "mention" ? "#a3e635" : "#c084fc";
        ctx.lineWidth = 1.6 / Math.max(0.6, this.zoom);
        ctx.globalAlpha = 0.95;
      } else {
        ctx.strokeStyle = "#263144";
        ctx.lineWidth = 1 / Math.max(0.6, this.zoom);
        ctx.globalAlpha = focusId ? 0.2 : 0.7;
      }
      ctx.stroke();
    }

    // Draw nodes
    ctx.globalAlpha = 1;
    for (const n of this.nodes) {
      const isFocused = !focusId || n.id === focusId || (focusNeighbors && focusNeighbors.has(n.id));
      const isSelected = n.id === this.selectedId;
      const isHovered = n.id === this.hoveredId;
      ctx.globalAlpha = isFocused ? 1.0 : 0.25;

      let fill = "#94a3b8";
      let stroke = "#1e2636";
      if (n.kind === "hub") {
        fill = "#1c1329";
        stroke = "#c084fc";
      } else if (n.concept) {
        if (n.concept.trust_tier === "human-reviewed") {
          fill = "#a3e635";
          stroke = "#15210b";
        } else if (n.concept.trust_tier === "machine-confirmed") {
          fill = "#c084fc";
          stroke = "#1c1329";
        } else {
          fill = "#f59e0b";
          stroke = "#261b07";
        }
      }

      ctx.beginPath();
      ctx.arc(n.x, n.y, n.radius, 0, Math.PI * 2);
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.lineWidth = n.kind === "hub" ? 1.5 : 1;
      ctx.strokeStyle = isSelected || isHovered ? "#bef264" : stroke;
      ctx.stroke();

      if (isSelected) {
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.radius + 3.5, 0, Math.PI * 2);
        ctx.lineWidth = 1.2;
        ctx.strokeStyle = "#a3e635";
        ctx.stroke();
      }

      if (n.kind === "hub" || isFocused || this.zoom >= 1.15) {
        ctx.font = `${n.kind === "hub" ? "600 11px" : "500 10px"} Inter, sans-serif`;
        ctx.fillStyle = n.kind === "hub" ? "#d8b4fe" : "#e6edf3";
        ctx.textAlign = "center";
        ctx.fillText(n.label.slice(0, 26), n.x, n.y + n.radius + 12);
      }
    }

    ctx.restore();
  }

  ensureStarted() {
    this.initDom();
    this.syncCanvasSize();
    if (this.rafId !== null) return;
    const tick = () => {
      if (state.view !== "okf" || state.subTabs.okf !== "graph") {
        this.rafId = null;
        return;
      }
      const moving = this.stepPhysics();
      this.renderFrame();
      if (moving) {
        this.rafId = window.requestAnimationFrame(tick);
      } else {
        this.rafId = null;
      }
    };
    this.rafId = window.requestAnimationFrame(tick);
  }
}

const okfGraph = new OkfGraphController();

function renderGraphInspector(node, neighborSet, nodeMap) {
  const box = $("okf-graph-detail");
  if (!box) return;
  if (!node) {
    box.innerHTML = `<div class="empty-state"><div class="empty-title">SELECT A NODE IN THE GRAPH</div><p>Click any concept or hub node to inspect its Markdown knowledge, tags, and connections.</p></div>`;
    return;
  }

  const neighborButtons = [...neighborSet]
    .map((nid) => nodeMap.get(nid))
    .filter(Boolean)
    .map(
      (nb) =>
        `<button class="btn btn-sm btn-outline" data-graph-focus="${esc(nb.id)}">${esc(nb.label)}</button>`,
    )
    .join("");

  if (node.kind === "hub") {
    box.innerHTML = `
      <div>
        <span class="badge badge-purple">CONCEPT TYPE HUB</span>
        <h3 style="margin:8px 0 4px;">${esc(node.label)}</h3>
        <p class="muted-copy">${neighborSet.size} linked concepts in this category cluster.</p>
      </div>
      <div class="kw-group">
        <div class="kw-group-title">CONNECTED NODES</div>
        <div class="kw-list">${neighborButtons || `<span class="muted-copy">None</span>`}</div>
      </div>
    `;
    return;
  }

  const c = node.concept;
  const tierCls =
    c.trust_tier === "human-reviewed"
      ? "badge-lime"
      : c.trust_tier === "machine-confirmed"
        ? "badge-purple"
        : "badge-amber";

  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
      <div>
        <div class="mono-tag" style="display:inline-block;margin-bottom:6px;">${esc(c.path)}</div>
        <h3 style="margin:0 0 4px;">${esc(c.name || c.path)}</h3>
      </div>
      <button class="btn btn-sm btn-lime" id="btn-graph-edit-node" data-edit-concept="${esc(c.path)}">✎ EDIT CONCEPT</button>
    </div>
    <div class="kw-list">
      <span class="badge">${esc(c.concept_type)}</span>
      <span class="badge ${tierCls}">${esc(c.trust_tier || "unverified")}</span>
      ${(c.tags || []).map((t) => `<span class="badge badge-purple">#${esc(t)}</span>`).join("")}
    </div>
    ${c.description ? `<p class="muted-copy">${esc(c.description)}</p>` : ""}
    <div>
      <div class="kw-group-title">KNOWLEDGE BODY</div>
      <pre class="terminal-pre">${esc(c.body || "")}</pre>
    </div>
    <div class="kw-group">
      <div class="kw-group-title">GRAPH CONNECTIONS (${neighborSet.size})</div>
      <div class="kw-list">${neighborButtons || `<span class="muted-copy">Isolated node</span>`}</div>
    </div>
  `;
}

async function loadOkfBrain() {
  const q = $("okf-search").value.trim();
  const ctype = $("okf-type-filter").value;
  const qs = new URLSearchParams();
  if (q) qs.set("query", q);
  if (ctype) qs.set("type", ctype);

  try {
    const data = await apiFetch(`/api/okf?${qs.toString()}`);
    state.okfData = data;
    state.okfLoadedOnce = true;
    renderOkfBrain();
  } catch (err) {
    toast(`Failed to load OKF Brain: ${err.message}`, "error");
  }
}

function renderOkfBrain() {
  const concepts = state.okfData.concepts || [];
  const sum = state.okfData.summary || {};

  $("nav-badge-okf").textContent = String(sum.total_concepts || concepts.length);
  $("okf-list-count").textContent = `${concepts.length} CONCEPTS`;

  okfGraph.setConcepts(concepts);

  $("okf-summary-cards").innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">TOTAL CONCEPTS</div>
      <div class="kpi-value">${sum.total_concepts || 0}</div>
      <div class="kpi-sub">Synced with Neon okf_concepts</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">TRACKED SCAM PATTERNS</div>
      <div class="kpi-value text-amber">${sum.scam_count || 0}</div>
      <div class="kpi-sub">Active guardrails in triage</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">HOT SKILLS SIGNAL</div>
      <div class="kpi-sub" style="margin-top:4px;">${esc((sum.hot_skills || "Pending distillation...").slice(0, 220))}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">HIRING VELOCITY</div>
      <div class="kpi-sub" style="margin-top:4px;">${esc((sum.hiring_velocity || "Pending distillation...").slice(0, 220))}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">REMOTE TRENDS</div>
      <div class="kpi-sub" style="margin-top:4px;">${esc((sum.remote_trends || "Pending distillation...").slice(0, 220))}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">RESUME EDIT HABITS</div>
      <div class="kpi-sub" style="margin-top:4px;">${esc((sum.edit_habits || "Pending human edit signals...").slice(0, 220))}</div>
    </div>
  `;

  const listEl = $("okf-concept-list");
  if (!concepts.length) {
    listEl.innerHTML = `<div class="muted-copy">No concepts found matching query.</div>`;
    return;
  }

  listEl.innerHTML = concepts
    .map((c) => {
      const active = c.path === state.selectedOkfPath ? "active" : "";
      const tierCls =
        c.trust_tier === "human-reviewed"
          ? "badge-lime"
          : c.trust_tier === "machine-confirmed"
            ? "badge-purple"
            : "badge-amber";
      return `
        <button class="concept-row ${active}" data-select-concept="${esc(c.path)}">
          <div class="concept-path">${esc(c.path)}</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
            <span class="badge">${esc(c.concept_type)}</span>
            <span class="badge ${tierCls}">${esc(c.trust_tier || "unverified")}</span>
            ${c.is_stale ? `<span class="badge badge-red">STALE</span>` : ""}
          </div>
          <div class="muted-copy" style="font-size:11px;">${esc(c.description || c.name || "")}</div>
        </button>
      `;
    })
    .join("");
}

function populateOkfEditor(concept) {
  if (!concept) {
    state.selectedOkfPath = null;
    $("okf-editor-title").textContent = "NEW OKF CONCEPT";
    $("okf-input-path").value = "";
    $("okf-input-path").readOnly = false;
    $("okf-input-type").value = "Preference";
    $("okf-input-name").value = "";
    setTagBox("okf-tags-box", []);
    $("okf-input-desc").value = "";
    $("okf-input-body").value = "";
    $("btn-okf-delete").classList.add("hidden");
    return;
  }

  state.selectedOkfPath = concept.path;
  $("okf-editor-title").textContent = `EDITING // ${concept.path}`;
  $("okf-input-path").value = concept.path;
  $("okf-input-type").value = concept.concept_type || "";
  $("okf-input-name").value = concept.name || "";
  setTagBox("okf-tags-box", concept.tags || []);
  $("okf-input-desc").value = concept.description || "";
  $("okf-input-body").value = concept.body || "";
  $("btn-okf-delete").classList.remove("hidden");
  renderOkfBrain();
}

async function saveOkfConcept(ev) {
  ev.preventDefault();
  const rawPath = $("okf-input-path").value.trim().replace(/^\/+/, "");
  const conceptType = $("okf-input-type").value.trim();
  const body = $("okf-input-body").value;
  const name = $("okf-input-name").value.trim();
  const description = $("okf-input-desc").value.trim();
  const tags = getTagBox("okf-tags-box");

  if (!rawPath || !conceptType) {
    toast("Concept Path and Concept Type are required.", "warn");
    return;
  }

  try {
    const encodedPath = rawPath.split("/").map(encodeURIComponent).join("/");
    const res = await apiFetch(`/api/okf/${encodedPath}`, {
      method: "PUT",
      body: { concept_type: conceptType, body, name, description, tags },
    });
    toast(`Saved OKF concept: ${res.concept.path}`);
    state.selectedOkfPath = res.concept.path;
    await loadOkfBrain();
  } catch (err) {
    toast(`Failed to save concept: ${err.message}`, "error");
  }
}

async function deleteSelectedOkfConcept() {
  const p = state.selectedOkfPath || $("okf-input-path").value.trim();
  if (!p) return;
  try {
    const encodedPath = p.split("/").map(encodeURIComponent).join("/");
    await apiFetch(`/api/okf/${encodedPath}`, { method: "DELETE" });
    toast(`Deleted concept ${p}`);
    populateOkfEditor(null);
    await loadOkfBrain();
  } catch (err) {
    toast(`Delete failed: ${err.message}`, "error");
  }
}

async function triggerDistill() {
  try {
    const res = await apiFetch("/api/okf/distill", { method: "POST", body: {} });
    toast(`OKF distillation complete (${res.distilled} signals processed)`);
    if (state.view === "okf") await loadOkfBrain();
  } catch (err) {
    toast(`Distillation failed: ${err.message}`, "error");
  }
}

// ==============================================================================
// ML Calibration & Eval Lab (View 5)
// ==============================================================================
async function loadEvalData() {
  try {
    const ev = await apiFetch("/api/eval");
    state.evalData = ev;
    renderEvalDashboard(ev);
  } catch (err) {
    toast(`Failed to load ML Eval: ${err.message}`, "error");
  }
}

function renderEvalDashboard(ev) {
  const n = ev.n || 0;
  $("nav-badge-eval").textContent = `n=${n}`;
  $("eval-k-badge").textContent = `K=${ev.k || 10}`;

  const ovCi = ev.overall_ci || [0, 0];
  $("eval-kpis").innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">GROUND-TRUTH LABELED (n)</div>
      <div class="kpi-value">${n}</div>
      <div class="kpi-sub">Applied: ${ev.total_applied || 0} | Skipped: ${ev.total_skipped || 0}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">OVERALL APPROVAL PRECISION</div>
      <div class="kpi-value text-purple">${pct(ev.overall_precision, 1)}</div>
      <div class="kpi-sub">95% Wilson CI: [${pct(ovCi[0], 1)}, ${pct(ovCi[1], 1)}]</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">JEV PRECISION@${ev.k || 10}</div>
      <div class="kpi-value text-lime">${pct(ev.jev_precision_at_k, 1)}</div>
      <div class="kpi-sub">${ev.jev_hits || 0} / ${ev.k || 0} top hits approved</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">FUNNEL CALLBACKS</div>
      <div class="kpi-value">${ev.callbacks || 0}</div>
      <div class="kpi-sub">Tracked outcomes across applied jobs</div>
    </div>
  `;

  const jevCi = ev.jev_ci || [0, 0];
  const baseCi = ev.baseline_ci || [0, 0];
  $("eval-precision-bars").innerHTML = `
    <div class="bar-row">
      <div class="bar-meta">
        <span>SYSTEM-1 JEV JUDGE (95% CI [${pct(jevCi[0], 1)}, ${pct(jevCi[1], 1)}])</span>
        <strong>${pct(ev.jev_precision_at_k, 1)}</strong>
      </div>
      <div class="bar-track">
        <div class="bar-fill lime" style="width:${Math.round((ev.jev_precision_at_k || 0) * 100)}%"></div>
      </div>
    </div>
    <div class="bar-row">
      <div class="bar-meta">
        <span>COSINE SIMILARITY BASELINE (95% CI [${pct(baseCi[0], 1)}, ${pct(baseCi[1], 1)}])</span>
        <strong>${pct(ev.baseline_precision_at_k, 1)}</strong>
      </div>
      <div class="bar-track">
        <div class="bar-fill" style="width:${Math.round((ev.baseline_precision_at_k || 0) * 100)}%"></div>
      </div>
    </div>
  `;

  const bss = ev.brier_skill_score;
  const bssText = bss === null || bss === undefined ? "N/A (constant baseline)" : bss.toFixed(4);
  const bssBadge = bss !== null && bss !== undefined && bss > 0 ? "badge-lime" : "badge-amber";
  $("eval-brier-box").innerHTML = `
    <div class="scorecard-grid">
      <div class="mini-stat">
        <div class="mini-stat-label">JEV BRIER SCORE</div>
        <div class="mini-stat-val text-lime">${Number(ev.brier_jev || 0).toFixed(4)}</div>
      </div>
      <div class="mini-stat">
        <div class="mini-stat-label">BASELINE BRIER SCORE</div>
        <div class="mini-stat-val">${Number(ev.brier_base || 0).toFixed(4)}</div>
      </div>
      <div class="mini-stat">
        <div class="mini-stat-label">BRIER SKILL SCORE (BSS)</div>
        <div class="mini-stat-val"><span class="badge ${bssBadge}">${bssText}</span></div>
      </div>
    </div>
    <p class="muted-copy">BSS &gt; 0 confirms System-1 Jev probability estimates add calibrated skill above naive base-rate prediction.</p>
  `;

  const srcEntries = Object.entries(ev.source_breakdown || {});
  $("eval-source-table").innerHTML = srcEntries.length
    ? `
      <div class="hud-table-wrap">
        <table class="hud-table">
          <thead>
            <tr>
              <th>SOURCE ATS</th>
              <th>LABELED</th>
              <th>APPROVED</th>
              <th>SKIPPED</th>
              <th>APPROVAL YIELD</th>
            </tr>
          </thead>
          <tbody>
            ${srcEntries
              .sort((a, b) => b[1].total - a[1].total)
              .map(
                ([src, st]) => `
                <tr>
                  <td><span class="badge">${esc(src)}</span></td>
                  <td>${st.total}</td>
                  <td class="text-lime">${st.applied}</td>
                  <td>${st.skipped}</td>
                  <td><strong>${pct(st.approval_rate, 1)}</strong></td>
                </tr>
              `,
              )
              .join("")}
          </tbody>
        </table>
      </div>
    `
    : `<div class="muted-copy">No labeled jobs across ATS sources yet.</div>`;

  const hist = ev.history || [];
  $("eval-history-table").innerHTML = hist.length
    ? `
      <div class="hud-table-wrap">
        <table class="hud-table">
          <thead>
            <tr><th>RAN AT</th><th>N</th><th>CONFIG</th><th>METRICS</th></tr>
          </thead>
          <tbody>
            ${hist
              .map(
                (h) => `
                <tr>
                  <td>${esc(fmtTime(h.ran_at))}</td>
                  <td><span class="mono-tag">${h.n}</span></td>
                  <td>${esc(h.config_version || "—")}</td>
                  <td class="mono" style="font-size:11px;">${esc(JSON.stringify(h.metrics || {}))}</td>
                </tr>
              `,
              )
              .join("")}
          </tbody>
        </table>
      </div>
    `
    : `<div class="muted-copy">No stored eval_runs rows yet.</div>`;

  $("eval-markdown-pre").textContent = ev.report_markdown || "No evaluation report generated.";
}

// ==============================================================================
// Config, Candidate Profile & Security Vault (View 6 — Structured Forms!)
// ==============================================================================
async function loadConfigVault() {
  try {
    const data = await apiFetch("/api/settings");
    state.settingsData = data;

    const prof = data.master_profile || {};
    const cfg = data.config || {};

    $("cfg-profile-editor").value = JSON.stringify(prof, null, 2);
    $("cfg-yaml-editor").value = JSON.stringify(cfg, null, 2);

    renderProfileForm(prof);
    renderEngineConfigForm(cfg);
    renderPauseCardState(Boolean(data.is_paused), data.paused_until);
  } catch (err) {
    toast(`Failed to load settings: ${err.message}`, "error");
  }
}

function appendBulletRow(listEl, text = "") {
  const row = document.createElement("div");
  row.className = "bullet-row";
  row.innerHTML = `
    <input type="text" class="hud-input pf-bullet-input" value="${esc(text)}" placeholder="Measurable engineering impact, scale, or metric..." />
    <button type="button" class="btn btn-sm btn-outline btn-bullet-up" title="Move Up">↑</button>
    <button type="button" class="btn btn-sm btn-outline btn-bullet-down" title="Move Down">↓</button>
    <button type="button" class="btn btn-sm btn-danger btn-bullet-remove" title="Remove Bullet">✕</button>
  `;
  listEl.appendChild(row);
}

function updateDateGroupPreview(groupEl) {
  if (!groupEl) return;
  groupEl.dataset.dateDirty = "1";
  const startInput = groupEl.querySelector(".pf-date-start");
  const endInput = groupEl.querySelector(".pf-date-end");
  const presentInput = groupEl.querySelector(".pf-date-present");
  const previewVal = groupEl.querySelector(".pf-date-preview-val");
  if (endInput && presentInput) {
    endInput.disabled = presentInput.checked;
  }
  const formatted = formatMonthInputsToDateRange(
    startInput ? startInput.value : "",
    endInput ? endInput.value : "",
    presentInput ? presentInput.checked : false
  );
  if (previewVal) previewVal.textContent = formatted || "—";
}

function readDateGroupValue(rowEl) {
  const group = rowEl.querySelector(".pf-date-group");
  if (!group) return "";
  if (group.dataset.dateDirty !== "1" && group.dataset.rawDate) {
    return group.dataset.rawDate;
  }
  const startYm = group.querySelector(".pf-date-start").value;
  const endYm = group.querySelector(".pf-date-end").value;
  const isPresent = group.querySelector(".pf-date-present").checked;
  return formatMonthInputsToDateRange(startYm, endYm, isPresent);
}

function appendExperienceRow(exp = {}) {
  const container = $("pf-experience-list");
  const idx = container.children.length + 1;
  const card = document.createElement("div");
  card.className = "repeatable-card pf-exp-row";
  const loc = exp.Location ?? exp.location ?? "";
  const rawDates = (exp.dates || "").trim();
  const parsedDate = parseDateRangeToMonthInputs(rawDates);
  const bullets = Array.isArray(exp.bullets) && exp.bullets.length ? exp.bullets : [""];

  card.innerHTML = `
    <div class="repeatable-head">
      <span class="mono-tag">ROLE #${idx}</span>
      <button type="button" class="btn btn-sm btn-danger btn-remove-row">REMOVE ROLE</button>
    </div>
    <div class="form-grid-3">
      <label class="field"><span>COMPANY</span><input type="text" class="hud-input pf-exp-company" value="${esc(exp.company || "")}" required /></label>
      <label class="field"><span>ROLE TITLE</span><input type="text" class="hud-input pf-exp-role" value="${esc(exp.role || "")}" required /></label>
      <label class="field"><span>LOCATION</span><input type="text" class="hud-input pf-exp-loc" value="${esc(loc)}" /></label>
    </div>
    <div class="pf-date-group" data-raw-date="${esc(rawDates)}" data-date-dirty="0" style="margin-top:10px;">
      <div class="date-range-grid">
        <label class="field">
          <span>START MONTH (CALENDAR)</span>
          <input type="month" class="hud-input pf-date-start" value="${esc(parsedDate.startYm)}" />
        </label>
        <label class="field">
          <span>END MONTH (CALENDAR)</span>
          <input type="month" class="hud-input pf-date-end" value="${esc(parsedDate.endYm)}" ${parsedDate.isPresent ? "disabled" : ""} />
        </label>
        <label class="toggle-item" style="align-self:end;min-height:42px;">
          <input type="checkbox" class="pf-date-present" ${parsedDate.isPresent ? "checked" : ""} />
          <span>CURRENT / PRESENT</span>
        </label>
      </div>
      <div class="muted-copy pf-date-preview" style="font-size:12px;margin-top:6px;">
        JSON Output: <span class="mono text-lime pf-date-preview-val">${esc(rawDates || formatMonthInputsToDateRange(parsedDate.startYm, parsedDate.endYm, parsedDate.isPresent) || "—")}</span>
      </div>
    </div>
    <div style="margin-top:14px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <span class="mono" style="font-size:11px;font-weight:700;letter-spacing:0.1em;color:var(--text-secondary);">ACHIEVEMENT BULLETS (STRUCTURED LIST)</span>
        <button type="button" class="btn btn-sm btn-lime btn-add-bullet">+ ADD BULLET</button>
      </div>
      <div class="bullet-list pf-exp-bullet-list"></div>
    </div>
  `;

  const bulletList = card.querySelector(".pf-exp-bullet-list");
  bullets.forEach((b) => appendBulletRow(bulletList, b));
  container.appendChild(card);
}

function appendEducationRow(edu = {}) {
  const container = $("pf-education-list");
  const idx = container.children.length + 1;
  const card = document.createElement("div");
  card.className = "repeatable-card pf-edu-row";
  const rawDates = (edu.dates || "").trim();
  const parsedDate = parseDateRangeToMonthInputs(rawDates);

  card.innerHTML = `
    <div class="repeatable-head">
      <span class="mono-tag">EDUCATION #${idx}</span>
      <button type="button" class="btn btn-sm btn-danger btn-remove-row">REMOVE</button>
    </div>
    <div class="form-grid-2">
      <label class="field"><span>DEGREE</span><input type="text" class="hud-input pf-edu-degree" value="${esc(edu.degree || "")}" required /></label>
      <label class="field"><span>INSTITUTION</span><input type="text" class="hud-input pf-edu-inst" value="${esc(edu.institution || "")}" required /></label>
    </div>
    <div class="pf-date-group" data-raw-date="${esc(rawDates)}" data-date-dirty="0" style="margin-top:10px;">
      <div class="date-range-grid">
        <label class="field">
          <span>START MONTH (CALENDAR)</span>
          <input type="month" class="hud-input pf-date-start" value="${esc(parsedDate.startYm)}" />
        </label>
        <label class="field">
          <span>END / GRADUATION MONTH</span>
          <input type="month" class="hud-input pf-date-end" value="${esc(parsedDate.endYm)}" ${parsedDate.isPresent ? "disabled" : ""} />
        </label>
        <label class="toggle-item" style="align-self:end;min-height:42px;">
          <input type="checkbox" class="pf-date-present" ${parsedDate.isPresent ? "checked" : ""} />
          <span>IN PROGRESS / PRESENT</span>
        </label>
      </div>
      <div class="muted-copy pf-date-preview" style="font-size:12px;margin-top:6px;">
        JSON Output: <span class="mono text-lime pf-date-preview-val">${esc(rawDates || formatMonthInputsToDateRange(parsedDate.startYm, parsedDate.endYm, parsedDate.isPresent) || "—")}</span>
      </div>
    </div>
  `;
  container.appendChild(card);
}

function renderProfileForm(p) {
  $("pf-name").value = p.name || "";
  $("pf-email").value = p.email || "";
  $("pf-phone").value = p.phone || "";
  $("pf-location").value = p.location || "";
  $("pf-linkedin").value = p.linkedin || "";
  $("pf-github").value = p.github || "";
  setTagBox("pf-skills-box", p.core_skills || []);
  $("pf-summary").value = p.experience_summary || "";

  $("pf-experience-list").innerHTML = "";
  (p.experience || []).forEach((exp) => appendExperienceRow(exp));

  $("pf-education-list").innerHTML = "";
  (p.education || []).forEach((edu) => appendEducationRow(edu));

  const d = p.application_defaults || {};
  $("pf-def-first-name").value = d.first_name ?? "";
  $("pf-def-last-name").value = d.last_name ?? "";
  $("pf-def-full-name").value = d.full_name ?? "";
  $("pf-def-current-company").value = d.current_company ?? "";
  $("pf-def-current-title").value = d.current_title ?? "";
  setSelectOrCustom("pf-def-highest-edu", d.highest_education ?? "");
  $("pf-def-total-exp").value = d.total_experience_years ?? "";
  $("pf-def-rel-exp").value = d.relevant_experience_years ?? "";
  $("pf-def-city").value = d.current_city ?? "";
  $("pf-def-country").value = d.country ?? "";
  setSelectOrCustom("pf-def-relocate", d.willing_to_relocate ?? "");
  setSelectOrCustom("pf-def-auth-india", d.authorized_to_work_in_india ?? "");
  setSelectOrCustom("pf-def-visa", d.requires_visa_sponsorship ?? "");
  setSelectOrCustom("pf-def-notice", d.notice_period ?? "");
  $("pf-def-notice-days").value = d.notice_period_days ?? "";
  $("pf-def-curr-ctc").value = d.current_ctc_lpa ?? "";
  $("pf-def-exp-ctc").value = d.expected_ctc_lpa ?? "";
  setSelectOrCustom("pf-def-gender", d.gender ?? "");
  setSelectOrCustom("pf-def-veteran", d.veteran_status ?? "");
  setSelectOrCustom("pf-def-disability", d.disability_status ?? "");
}

function syncDefaultsFromProfile() {
  const fullName = $("pf-name").value.trim();
  if (fullName) {
    const parts = fullName.split(/\s+/).filter(Boolean);
    $("pf-def-full-name").value = fullName;
    $("pf-def-first-name").value = parts[0] || "";
    $("pf-def-last-name").value = parts.length > 1 ? parts.slice(1).join(" ") : "";
  }

  const firstExp = document.querySelector(".pf-exp-row");
  if (firstExp) {
    const company = firstExp.querySelector(".pf-exp-company").value.trim();
    const role = firstExp.querySelector(".pf-exp-role").value.trim();
    if (company) $("pf-def-current-company").value = company;
    if (role) $("pf-def-current-title").value = role;
  }

  const firstEdu = document.querySelector(".pf-edu-row");
  if (firstEdu) {
    const degree = firstEdu.querySelector(".pf-edu-degree").value.trim();
    if (degree) setSelectOrCustom("pf-def-highest-edu", degree);
  }

  const loc = $("pf-location").value.trim();
  if (loc) {
    const locParts = loc.split(",").map((s) => s.trim()).filter(Boolean);
    if (locParts[0]) $("pf-def-city").value = locParts[0];
    if (locParts.length > 1) $("pf-def-country").value = locParts[locParts.length - 1];
  }

  toast("Auto-filled Application Defaults from profile identity & latest role!");
}

function collectProfileForm() {
  const base = JSON.parse(JSON.stringify((state.settingsData && state.settingsData.master_profile) || {}));
  base.name = $("pf-name").value.trim();
  base.email = $("pf-email").value.trim();
  base.phone = $("pf-phone").value.trim();
  base.location = $("pf-location").value.trim();
  base.linkedin = $("pf-linkedin").value.trim();
  base.github = $("pf-github").value.trim();
  base.core_skills = getTagBox("pf-skills-box");
  base.experience_summary = $("pf-summary").value.trim();

  base.experience = [...document.querySelectorAll(".pf-exp-row")].map((row) => ({
    company: row.querySelector(".pf-exp-company").value.trim(),
    role: row.querySelector(".pf-exp-role").value.trim(),
    Location: row.querySelector(".pf-exp-loc").value.trim(),
    dates: readDateGroupValue(row),
    bullets: [...row.querySelectorAll(".pf-bullet-input")]
      .map((inp) => inp.value.trim())
      .filter(Boolean),
  }));

  base.education = [...document.querySelectorAll(".pf-edu-row")].map((row) => ({
    degree: row.querySelector(".pf-edu-degree").value.trim(),
    institution: row.querySelector(".pf-edu-inst").value.trim(),
    dates: readDateGroupValue(row),
  }));

  const prevDefaults = base.application_defaults || {};
  base.application_defaults = {
    ...prevDefaults,
    first_name: $("pf-def-first-name").value.trim(),
    last_name: $("pf-def-last-name").value.trim(),
    full_name: $("pf-def-full-name").value.trim(),
    current_company: $("pf-def-current-company").value.trim(),
    current_title: $("pf-def-current-title").value.trim(),
    highest_education: $("pf-def-highest-edu").value.trim(),
    total_experience_years: $("pf-def-total-exp").value.trim(),
    relevant_experience_years: $("pf-def-rel-exp").value.trim(),
    current_city: $("pf-def-city").value.trim(),
    country: $("pf-def-country").value.trim(),
    willing_to_relocate: $("pf-def-relocate").value.trim(),
    authorized_to_work_in_india: $("pf-def-auth-india").value.trim(),
    requires_visa_sponsorship: $("pf-def-visa").value.trim(),
    notice_period: $("pf-def-notice").value.trim(),
    notice_period_days: $("pf-def-notice-days").value.trim(),
    current_ctc_lpa: $("pf-def-curr-ctc").value.trim(),
    expected_ctc_lpa: $("pf-def-exp-ctc").value.trim(),
    gender: $("pf-def-gender").value.trim(),
    veteran_status: $("pf-def-veteran").value.trim(),
    disability_status: $("pf-def-disability").value.trim(),
  };

  return base;
}

function appendWorkdayRow(wd = {}) {
  const container = $("ec-workday-list");
  const idx = container.children.length + 1;
  const card = document.createElement("div");
  card.className = "repeatable-card ec-wd-row";
  card.innerHTML = `
    <div class="repeatable-head">
      <span class="mono-tag">WORKDAY TARGET #${idx}</span>
      <button type="button" class="btn btn-sm btn-danger btn-remove-row">REMOVE</button>
    </div>
    <div class="form-grid-2">
      <label class="field"><span>COMPANY</span><input type="text" class="hud-input ec-wd-company" value="${esc(wd.company || "")}" required /></label>
      <label class="field"><span>TENANT</span><input type="text" class="hud-input ec-wd-tenant" value="${esc(wd.tenant || "")}" required /></label>
      <label class="field"><span>WD INSTANCE</span><input type="text" class="hud-input ec-wd-inst" value="${esc(wd.wd_instance || "")}" placeholder="wd1" required /></label>
      <label class="field"><span>SITE</span><input type="text" class="hud-input ec-wd-site" value="${esc(wd.site || "")}" required /></label>
    </div>
  `;
  container.appendChild(card);
}

function renderEngineConfigForm(cfg) {
  const auto = cfg.automation || {};
  $("ec-auto-apply-mode").value = auto.apply_mode || "DRY_RUN";
  $("ec-auto-daily-cap").value = auto.daily_cap ?? 5;
  $("ec-auto-cooldown").value = auto.per_company_cooldown_hours ?? 72;
  $("ec-auto-min-fit").value = auto.min_fit_prob ?? 0.7;
  $("ec-auto-max-free").value = auto.max_free_text_questions ?? 0;
  setTagBox("ec-auto-allowed-domains-box", auto.allowed_apply_domains || []);
  $("ec-auto-first-confirm").checked = auto.first_company_confirm !== false;

  const tiers = new Set(auto.allowed_tiers || ["strong_fit", "dream_role"]);
  $("ec-tier-dream").checked = tiers.has("dream_role");
  $("ec-tier-strong").checked = tiers.has("strong_fit");
  $("ec-tier-moderate").checked = tiers.has("moderate_fit");
  $("ec-tier-weak").checked = tiers.has("weak_fit");

  const prefs = cfg.candidate_preferences || {};
  setTagBox("ec-pref-roles-box", prefs.target_role_keywords || []);
  setTagBox("ec-pref-excludes-box", prefs.exclude_title_keywords || []);
  setTagBox("ec-pref-locations-box", prefs.allowed_locations || []);

  const ing = cfg.ingestion || {};
  $("ec-ing-concurrency").value = ing.triage_concurrency ?? 3;
  $("ec-ing-max-age").value = ing.max_job_age_days ?? 7;
  $("ec-ing-prioritize").checked = ing.prioritize_scraping !== false;
  $("ec-ing-scrape-only").checked = Boolean(ing.scrape_only);
  $("ec-ing-enable-ats").checked = ing.enable_ats !== false;
  $("ec-ing-enable-jobboards").checked = ing.enable_jobboards !== false;
  $("ec-ing-enable-gmail").checked = ing.enable_gmail !== false;

  const jb = cfg.jobboards || {};
  setTagBox("ec-jb-sites-box", jb.sites || ["naukri", "linkedin", "indeed"]);
  setTagBox("ec-jb-terms-box", jb.search_terms || []);
  setTagBox("ec-jb-locations-box", jb.locations || []);
  $("ec-jb-wanted").value = jb.results_wanted ?? 20;
  $("ec-jb-hours-old").value = jb.hours_old ?? 72;

  const gm = cfg.gmail_scanner || {};
  $("ec-gm-label").value = gm.label || "professional-job_alerts";
  $("ec-gm-lookback").value = gm.lookback_days ?? 3;
  $("ec-gm-max-age").value = gm.max_job_age_hours ?? 96;
  $("ec-gm-poll").value = gm.poll_interval_minutes ?? 20;
  setTagBox("ec-gm-senders-box", gm.sender_keywords || []);

  const ats = cfg.ats_targets || {};
  setTagBox("ec-ats-greenhouse-box", ats.greenhouse || []);
  setTagBox("ec-ats-lever-box", ats.lever || []);
  setTagBox("ec-ats-ashby-box", ats.ashby || []);
  setTagBox("ec-ats-smartrecruiters-box", ats.smartrecruiters || []);
  setTagBox("ec-ats-recruitee-box", ats.recruitee || []);

  $("ec-workday-list").innerHTML = "";
  (ats.workday_cxs || []).forEach((wd) => appendWorkdayRow(wd));
}

function collectEngineConfigForm() {
  const base = JSON.parse(JSON.stringify((state.settingsData && state.settingsData.config) || {}));

  const tiers = [];
  if ($("ec-tier-dream").checked) tiers.push("dream_role");
  if ($("ec-tier-strong").checked) tiers.push("strong_fit");
  if ($("ec-tier-moderate").checked) tiers.push("moderate_fit");
  if ($("ec-tier-weak").checked) tiers.push("weak_fit");

  base.automation = {
    ...(base.automation || {}),
    apply_mode: $("ec-auto-apply-mode").value,
    daily_cap: Number($("ec-auto-daily-cap").value || 0),
    per_company_cooldown_hours: Number($("ec-auto-cooldown").value || 0),
    min_fit_prob: Number($("ec-auto-min-fit").value || 0.7),
    max_free_text_questions: Number($("ec-auto-max-free").value || 0),
    first_company_confirm: $("ec-auto-first-confirm").checked,
    allowed_tiers: tiers,
    allowed_apply_domains: getTagBox("ec-auto-allowed-domains-box"),
  };

  base.candidate_preferences = {
    ...(base.candidate_preferences || {}),
    target_role_keywords: getTagBox("ec-pref-roles-box"),
    exclude_title_keywords: getTagBox("ec-pref-excludes-box"),
    allowed_locations: getTagBox("ec-pref-locations-box"),
  };

  base.ingestion = {
    ...(base.ingestion || {}),
    triage_concurrency: Number($("ec-ing-concurrency").value || 3),
    max_job_age_days: Number($("ec-ing-max-age").value || 7),
    prioritize_scraping: $("ec-ing-prioritize").checked,
    scrape_only: $("ec-ing-scrape-only").checked,
    enable_ats: $("ec-ing-enable-ats").checked,
    enable_jobboards: $("ec-ing-enable-jobboards").checked,
    enable_gmail: $("ec-ing-enable-gmail").checked,
  };

  base.jobboards = {
    ...(base.jobboards || {}),
    sites: getTagBox("ec-jb-sites-box"),
    search_terms: getTagBox("ec-jb-terms-box"),
    locations: getTagBox("ec-jb-locations-box"),
    results_wanted: Number($("ec-jb-wanted").value || 20),
    hours_old: Number($("ec-jb-hours-old").value || 72),
  };

  base.gmail_scanner = {
    ...(base.gmail_scanner || {}),
    label: $("ec-gm-label").value.trim(),
    lookback_days: Number($("ec-gm-lookback").value || 3),
    max_job_age_hours: Number($("ec-gm-max-age").value || 96),
    poll_interval_minutes: Number($("ec-gm-poll").value || 20),
    sender_keywords: getTagBox("ec-gm-senders-box"),
  };

  base.ats_targets = {
    ...(base.ats_targets || {}),
    greenhouse: getTagBox("ec-ats-greenhouse-box"),
    lever: getTagBox("ec-ats-lever-box"),
    ashby: getTagBox("ec-ats-ashby-box"),
    smartrecruiters: getTagBox("ec-ats-smartrecruiters-box"),
    recruitee: getTagBox("ec-ats-recruitee-box"),
    workday_cxs: [...document.querySelectorAll(".ec-wd-row")].map((row) => ({
      company: row.querySelector(".ec-wd-company").value.trim(),
      tenant: row.querySelector(".ec-wd-tenant").value.trim(),
      wd_instance: row.querySelector(".ec-wd-inst").value.trim(),
      site: row.querySelector(".ec-wd-site").value.trim(),
    })),
  };

  return base;
}

function renderPauseCardState(isPaused, pausedUntil) {
  $("cfg-pause-badge").textContent = isPaused ? `PAUSED UNTIL ${fmtTime(pausedUntil)}` : "ACTIVE";
  $("cfg-pause-desc").textContent = isPaused
    ? `Autonomous submissions are currently paused until ${fmtTime(pausedUntil)}. You can resume early below.`
    : "Autonomous Playwright submission is currently active according to your daily cap rules.";
}

async function saveProfilePayload(profileObj) {
  try {
    const res = await apiFetch("/api/settings/profile", {
      method: "PUT",
      body: profileObj,
    });
    if (state.settingsData) state.settingsData.master_profile = res.master_profile;
    $("cfg-profile-editor").value = JSON.stringify(res.master_profile, null, 2);
    renderProfileForm(res.master_profile);
    toast("Master Profile saved to Neon PostgreSQL!");
  } catch (err) {
    toast(`Save Profile failed: ${err.message}`, "error");
  }
}

async function saveEngineConfigPayload(cfgObj) {
  try {
    const res = await apiFetch("/api/settings/config", {
      method: "PUT",
      body: cfgObj,
    });
    if (state.settingsData) state.settingsData.config = res.config;
    $("cfg-yaml-editor").value = JSON.stringify(res.config, null, 2);
    renderEngineConfigForm(res.config);
    toast("Engine Config validated (Pydantic AppConfig) and saved to Neon!");
  } catch (err) {
    toast(`Config validation/save failed: ${err.message}`, "error");
  }
}

async function setAutomationPause(hoursVal) {
  try {
    const res = await apiFetch("/api/actions/pause", {
      method: "POST",
      body: { hours: hoursVal },
    });
    toast(res.is_paused ? `Automation paused until ${fmtTime(res.paused_until)}` : "Automation resumed!");
    renderPauseCardState(Boolean(res.is_paused), res.paused_until);
    await fetchSystemStatus();
  } catch (err) {
    toast(`Pause toggle failed: ${err.message}`, "error");
  }
}

async function executeDbReset() {
  const phrase = $("cfg-reset-confirm").value.trim().toUpperCase();
  const mode = $("cfg-reset-mode").value;
  if (phrase !== "CONFIRM RESET") {
    toast("Type 'CONFIRM RESET' in the confirmation box first.", "warn");
    return;
  }
  try {
    const res = await apiFetch("/api/actions/reset", {
      method: "POST",
      body: { mode, confirm: true },
    });
    $("cfg-reset-confirm").value = "";
    toast(res.message || `Database reset (${mode}) complete.`);
    await refreshTelemetryAndActiveView();
  } catch (err) {
    toast(`Reset failed: ${err.message}`, "error");
  }
}

// ==============================================================================
// Global Event Bindings
// ==============================================================================
function bindEvents() {
  // Left Nav & Brand links with native Ctrl+Click new-tab passthrough
  document.body.addEventListener("click", (ev) => {
    const viewLink = ev.target.closest("[data-view]");
    if (viewLink) {
      if (isModifiedClick(ev)) return; // Let browser open new tab natively
      ev.preventDefault();
      switchView(viewLink.dataset.view);
      return;
    }

    const studioLink = ev.target.closest("[data-open-studio]");
    if (studioLink) {
      if (isModifiedClick(ev)) return; // Let Ctrl+Click open /?view=studio&job=ID in new tab
      ev.preventDefault();
      openStudioForJob(studioLink.dataset.openStudio);
      return;
    }

    const subBtn = ev.target.closest(".subtab-btn");
    if (subBtn) {
      const groupEl = subBtn.closest("[data-subtab-group]");
      if (groupEl) {
        switchSubTab(groupEl.dataset.subtabGroup, subBtn.dataset.subtab);
      }
      return;
    }

    const intBtn = ev.target.closest("[data-job-interest]");
    if (intBtn) {
      triggerJobInterest(intBtn.dataset.jobInterest);
      return;
    }

    const actBtn = ev.target.closest("[data-job-action]");
    if (actBtn) {
      triggerJobAction(actBtn.dataset.jobId, actBtn.dataset.jobAction);
      return;
    }

    const subModalBtn = ev.target.closest("[data-open-submit]");
    if (subModalBtn) {
      openSubmitModal(subModalBtn.dataset.openSubmit);
      return;
    }

    const tagAddBtn = ev.target.closest("[data-tag-add]");
    if (tagAddBtn) {
      openTagModal(tagAddBtn.dataset.tagAdd, null);
      return;
    }

    const tagPillBtn = ev.target.closest("[data-tag-box-id][data-tag-index]");
    if (tagPillBtn) {
      openTagModal(tagPillBtn.dataset.tagBoxId, Number(tagPillBtn.dataset.tagIndex));
      return;
    }

    const addBulletBtn = ev.target.closest(".btn-add-bullet");
    if (addBulletBtn) {
      const expRow = addBulletBtn.closest(".pf-exp-row");
      const list = expRow && expRow.querySelector(".pf-exp-bullet-list");
      if (list) {
        appendBulletRow(list, "");
        const lastInput = list.querySelector(".bullet-row:last-child .pf-bullet-input");
        if (lastInput) lastInput.focus();
      }
      return;
    }

    const bulletUpBtn = ev.target.closest(".btn-bullet-up");
    if (bulletUpBtn) {
      const row = bulletUpBtn.closest(".bullet-row");
      if (row && row.previousElementSibling) {
        row.parentElement.insertBefore(row, row.previousElementSibling);
      }
      return;
    }

    const bulletDownBtn = ev.target.closest(".btn-bullet-down");
    if (bulletDownBtn) {
      const row = bulletDownBtn.closest(".bullet-row");
      if (row && row.nextElementSibling) {
        row.parentElement.insertBefore(row.nextElementSibling, row);
      }
      return;
    }

    const bulletRemoveBtn = ev.target.closest(".btn-bullet-remove");
    if (bulletRemoveBtn) {
      const row = bulletRemoveBtn.closest(".bullet-row");
      const list = row && row.parentElement;
      if (row) row.remove();
      if (list && list.children.length === 0) appendBulletRow(list, "");
      return;
    }

    const removeRowBtn = ev.target.closest(".btn-remove-row");
    if (removeRowBtn) {
      const card = removeRowBtn.closest(".repeatable-card");
      if (card) card.remove();
      return;
    }

    const shotCard = ev.target.closest("[data-lightbox-src]");
    if (shotCard) {
      $("lightbox-title").textContent = shotCard.dataset.lightboxTitle || "PLAYWRIGHT SCREENSHOT";
      $("lightbox-img").src = shotCard.dataset.lightboxSrc;
      $("lightbox-modal").classList.remove("hidden");
      return;
    }

    const focusGraphBtn = ev.target.closest("[data-graph-focus]");
    if (focusGraphBtn) {
      okfGraph.selectNode(focusGraphBtn.dataset.graphFocus);
      return;
    }

    const editFromGraphBtn = ev.target.closest("[data-edit-concept]");
    if (editFromGraphBtn) {
      const cPath = editFromGraphBtn.dataset.editConcept;
      const found = (state.okfData.concepts || []).find((c) => c.path === cPath);
      if (found) populateOkfEditor(found);
      switchSubTab("okf", "list");
    }
  });

  // Browser Back / Forward navigation
  window.addEventListener("popstate", () => {
    parseRouteFromUrl();
    switchView(state.view, { pushHistory: false });
  });

  // Topbar actions
  $("btn-top-digest").addEventListener("click", async () => {
    try {
      await apiFetch("/api/actions/digest", { method: "POST", body: { limit: 5 } });
      toast("Pending digest triggered!");
    } catch (err) {
      toast(`Digest failed: ${err.message}`, "error");
    }
  });

  $("btn-top-distill").addEventListener("click", triggerDistill);

  $("btn-top-pause").addEventListener("click", () => {
    const isPaused = Boolean(state.systemStatus && state.systemStatus.is_paused);
    setAutomationPause(isPaused ? null : 24);
  });

  $("btn-top-auth").addEventListener("click", openAuthModal);
  $("btn-manual-sync").addEventListener("click", () => {
    if (state.authenticated) refreshTelemetryAndActiveView();
    else checkAuthAndBoot();
  });

  // Dashboard buttons
  $("btn-dash-refresh-priority").addEventListener("click", () => loadDashboardPriorityJobs());

  // Pipeline controls
  $("mode-kanban").addEventListener("click", () => {
    state.pipelineMode = "kanban";
    $("mode-kanban").classList.add("active");
    $("mode-table").classList.remove("active");
    renderPipeline();
  });
  $("mode-table").addEventListener("click", () => {
    state.pipelineMode = "table";
    $("mode-table").classList.add("active");
    $("mode-kanban").classList.remove("active");
    renderPipeline();
  });

  $("pipeline-stage-pills").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-stage]");
    if (!btn) return;
    document.querySelectorAll("#pipeline-stage-pills .pill-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.pipelineFilters.stage = btn.dataset.stage || "";
    state.pipelineFilters.offset = 0;
    loadPipelineJobs();
  });

  let searchDebounce = null;
  $("filter-search").addEventListener("input", () => {
    window.clearTimeout(searchDebounce);
    searchDebounce = window.setTimeout(() => {
      state.pipelineFilters.search = $("filter-search").value;
      state.pipelineFilters.offset = 0;
      loadPipelineJobs();
    }, 300);
  });

  $("filter-tier").addEventListener("change", () => {
    state.pipelineFilters.fit_tier = $("filter-tier").value;
    state.pipelineFilters.offset = 0;
    loadPipelineJobs();
  });
  $("filter-ats").addEventListener("change", () => {
    state.pipelineFilters.source_ats = $("filter-ats").value;
    state.pipelineFilters.offset = 0;
    loadPipelineJobs();
  });
  $("filter-limit").addEventListener("change", () => {
    state.pipelineFilters.limit = Number($("filter-limit").value) || 100;
    state.pipelineFilters.offset = 0;
    loadPipelineJobs();
  });
  $("btn-pipeline-refresh").addEventListener("click", () => loadPipelineJobs());

  $("btn-page-prev").addEventListener("click", () => {
    state.pipelineFilters.offset = Math.max(0, state.pipelineFilters.offset - state.pipelineFilters.limit);
    loadPipelineJobs();
  });
  $("btn-page-next").addEventListener("click", () => {
    state.pipelineFilters.offset += state.pipelineFilters.limit;
    loadPipelineJobs();
  });

  // Studio controls
  $("btn-studio-load-id").addEventListener("click", () => {
    const jid = Number($("studio-jump-id").value);
    if (jid > 0) openStudioForJob(jid);
  });
  $("studio-jump-id").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      const jid = Number($("studio-jump-id").value);
      if (jid > 0) openStudioForJob(jid);
    }
  });
  $("btn-studio-refresh").addEventListener("click", () => {
    if (state.studioJobId) loadStudioJob(state.studioJobId, true);
  });
  $("btn-studio-send-edit").addEventListener("click", sendStudioEdit);

  document.querySelectorAll("[data-studio-left]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.studioLeftTab = btn.dataset.studioLeft;
      document.querySelectorAll("[data-studio-left]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      if (state.studioJob) renderStudioLeftPane(state.studioJob);
    });
  });

  document.querySelectorAll("[data-studio-right]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.studioRightTab = btn.dataset.studioRight;
      document.querySelectorAll("[data-studio-right]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      if (state.studioJob) renderStudioRightPane(state.studioJob);
    });
  });

  $("btn-studio-reload-media").addEventListener("click", () => {
    if (state.studioJob) renderStudioRightPane(state.studioJob);
  });

  $("btn-studio-download-pdf").addEventListener("click", async () => {
    if (!state.studioJobId) return;
    try {
      const blob = await apiFetch(`/api/jobs/${state.studioJobId}/pdf`, { responseType: "blob" });
      const dlUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = dlUrl;
      a.download = `resume_${state.studioJobId}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.setTimeout(() => URL.revokeObjectURL(dlUrl), 5000);
    } catch (err) {
      toast(`Download failed: ${err.message}`, "error");
    }
  });

  // OKF Brain & Graph events
  $("btn-okf-distill").addEventListener("click", triggerDistill);
  $("btn-okf-new").addEventListener("click", () => {
    populateOkfEditor(null);
    switchSubTab("okf", "list");
  });
  $("btn-okf-search").addEventListener("click", loadOkfBrain);
  $("okf-search").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") loadOkfBrain();
  });
  $("okf-type-filter").addEventListener("change", loadOkfBrain);
  $("okf-form").addEventListener("submit", saveOkfConcept);
  $("btn-okf-delete").addEventListener("click", deleteSelectedOkfConcept);

  $("btn-graph-zoom-in").addEventListener("click", () => {
    okfGraph.zoom = Math.min(3.0, okfGraph.zoom * 1.2);
    okfGraph.renderFrame();
  });
  $("btn-graph-zoom-out").addEventListener("click", () => {
    okfGraph.zoom = Math.max(0.3, okfGraph.zoom / 1.2);
    okfGraph.renderFrame();
  });
  $("btn-graph-relayout").addEventListener("click", () => okfGraph.reheat());
  $("btn-graph-reset").addEventListener("click", () => okfGraph.resetCamera());

  $("okf-concept-list").addEventListener("click", (ev) => {
    const row = ev.target.closest("[data-select-concept]");
    if (!row) return;
    const p = row.dataset.selectConcept;
    const found = (state.okfData.concepts || []).find((c) => c.path === p);
    if (found) populateOkfEditor(found);
  });

  // Eval events
  $("btn-eval-refresh").addEventListener("click", loadEvalData);

  // Config & Vault events (Forms + Raw JSON)
  initAllTagBoxes();
  document.body.addEventListener("change", (ev) => {
    if (ev.target.matches(".pf-date-start, .pf-date-end, .pf-date-present")) {
      updateDateGroupPreview(ev.target.closest(".pf-date-group"));
    }
  });
  document.body.addEventListener("input", (ev) => {
    if (ev.target.matches(".pf-date-start, .pf-date-end")) {
      updateDateGroupPreview(ev.target.closest(".pf-date-group"));
    }
  });

  $("btn-cfg-reload").addEventListener("click", loadConfigVault);
  $("btn-pf-sync-defaults").addEventListener("click", syncDefaultsFromProfile);
  $("btn-pf-add-exp").addEventListener("click", () => appendExperienceRow({}));
  $("btn-pf-add-edu").addEventListener("click", () => appendEducationRow({}));
  $("btn-ec-add-workday").addEventListener("click", () => appendWorkdayRow({}));

  // Tag Popup Modal events
  $("tag-modal-form").addEventListener("submit", submitTagModal);
  $("btn-tag-delete").addEventListener("click", deleteActiveTag);
  $("btn-tag-cancel").addEventListener("click", closeTagModal);
  $("btn-close-tag-modal").addEventListener("click", closeTagModal);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && $("tag-modal") && !$("tag-modal").classList.contains("hidden")) {
      closeTagModal();
    }
  });

  $("cfg-profile-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    saveProfilePayload(collectProfileForm());
  });
  $("cfg-engine-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    saveEngineConfigPayload(collectEngineConfigForm());
  });

  $("btn-save-profile").addEventListener("click", () => {
    try {
      const parsed = JSON.parse($("cfg-profile-editor").value);
      saveProfilePayload(parsed);
    } catch (err) {
      toast(`Invalid Profile JSON: ${err.message}`, "error");
    }
  });
  $("btn-save-config").addEventListener("click", () => {
    try {
      const parsed = JSON.parse($("cfg-yaml-editor").value);
      saveEngineConfigPayload(parsed);
    } catch (err) {
      toast(`Invalid Config JSON: ${err.message}`, "error");
    }
  });

  $("btn-cfg-pause").addEventListener("click", () => {
    setAutomationPause(Number($("cfg-pause-hours").value) || 24);
  });
  $("btn-cfg-resume").addEventListener("click", () => setAutomationPause(null));
  $("btn-cfg-reset").addEventListener("click", executeDbReset);

  // Modals
  $("btn-close-auth").addEventListener("click", closeAuthModal);
  $("btn-close-submit").addEventListener("click", () => $("submit-modal").classList.add("hidden"));
  $("btn-cancel-submit").addEventListener("click", () => $("submit-modal").classList.add("hidden"));
  $("btn-confirm-submit").addEventListener("click", confirmExecuteSubmit);
  $("btn-close-lightbox").addEventListener("click", () => $("lightbox-modal").classList.add("hidden"));

  $("auth-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const rawUrl = $("auth-backend-url").value.trim().replace(/\/+$/, "");
    const secret = $("auth-secret-input").value.trim();
    const useHdr = $("auth-use-header").checked;

    state.backendUrl = rawUrl;
    state.apiKey = secret;
    state.useApiHeader = useHdr;
    localStorage.setItem(STORAGE_BACKEND_URL, rawUrl);
    localStorage.setItem(STORAGE_API_KEY, useHdr ? secret : "");
    localStorage.setItem(STORAGE_USE_HEADER, useHdr ? "true" : "false");

    try {
      if (secret) {
        const loginRes = await apiFetch("/api/auth/login", {
          method: "POST",
          body: { password: secret },
          suppressAuthModal: true,
        });
        if (loginRes.token) {
          state.bearerToken = loginRes.token;
          localStorage.setItem(STORAGE_BEARER, loginRes.token);
        }
      }
      const ok = await checkAuthAndBoot();
      if (ok) {
        closeAuthModal();
        toast("Authenticated and synced with Relentless backend!");
      }
    } catch (err) {
      toast(`Authentication failed: ${err.message}`, "error");
    }
  });

  $("btn-auth-logout").addEventListener("click", async () => {
    try {
      await apiFetch("/api/auth/logout", { method: "POST", body: {}, suppressAuthModal: true });
    } catch {
      // Ignore logout failure when clearing local credentials
    }
    state.bearerToken = "";
    state.apiKey = "";
    state.authenticated = false;
    localStorage.removeItem(STORAGE_BEARER);
    localStorage.removeItem(STORAGE_API_KEY);
    updateConnectionBadge(false, "LOGGED OUT");
    toast("Session cleared.");
  });
}

// ==============================================================================
// Boot
// ==============================================================================
document.addEventListener("DOMContentLoaded", () => {
  parseRouteFromUrl();
  applyViewDom();
  bindEvents();
  checkAuthAndBoot();
  window.setInterval(checkNetlifyProtectionExpiry, 5 * 60 * 1000);
});
