import { api } from "./lib/api.js";
import {
  applyAction,
  applyErrors,
  clearErrors,
  collect,
  renderAll,
  syncRange,
  validateNative,
} from "./lib/adminForm.js";
import { el, esc } from "./lib/dom.js";

let cfg = null;
let version = null; // config version from GET, sent back as If-Match on save
let dirty = false;
let root = null;
let roots = null;

async function boot() {
  root = el("admin-root");
  roots = {
    airports: el("airports"),
    regions: el("regions"),
    cycle: el("cycle"),
    datasource: el("datasource"),
    display: el("display"),
    satellite: el("satellite"),
    radar: el("radar"),
    cameras: el("cameras"),
    notams: el("notam-settings"),
  };
  fillTimezones();
  await load();

  el("save").addEventListener("click", save);
  el("test-source").addEventListener("click", testSource);
  el("blackout-10").addEventListener("click", () => screenAction(() => api.blackout({ seconds: 10, reason: "admin page" })));
  el("blackout-hold").addEventListener("click", () => screenAction(() => api.blackout({ reason: "admin page" })));
  el("screen-restore").addEventListener("click", () => screenAction(() => api.restoreDisplay()));
  el("screen-previous").addEventListener("click", () => screenAction(() => api.previousView()));
  el("screen-next").addEventListener("click", () => screenAction(() => api.nextView()));
  el("screen-view").addEventListener("change", (ev) => {
    const id = ev.target.value;
    ev.target.value = "";
    if (id) screenAction(() => api.showView(id));
  });
  refreshScreenState();
  setInterval(refreshScreenState, 5000);

  // One delegated listener for every add / delete / move button, so re-rendering
  // rows can never double-bind handlers.
  root.addEventListener("click", (ev) => {
    const check = ev.target.closest("[data-check-notam]");
    if (check) return checkNotam(+check.dataset.checkNotam);
    if (ev.target.closest("[data-test-model]")) return testModel();
    const btn = ev.target.closest("[data-add],[data-del],[data-move]");
    if (!btn || btn.disabled) return;
    ev.preventDefault();
    collect(root, cfg); // keep edits made in other rows across the re-render
    if (applyAction(cfg, btn)) {
      renderAll(cfg, roots);
      markDirty();
    }
  });
  root.addEventListener("input", (ev) => {
    if (ev.target.id === "notam-new") return; // typing identifiers to add changes nothing yet
    syncRange(ev.target);
    markDirty();
  });
  root.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" || ev.target.id !== "notam-new") return;
    ev.preventDefault();
    el("notam-add").click();
  });
  root.addEventListener("change", markDirty);
  window.addEventListener("beforeunload", (e) => {
    if (!dirty) return;
    e.preventDefault();
    e.returnValue = "";
  });
  showStatusPanel();
}

async function load() {
  cfg = await api.getConfig();
  version = cfg.version || null;
  delete cfg.version;
  renderAll(cfg, roots);
  dirty = false;
  fillViews();
}

// The cycle can show a view more than once; the jump list names each view once.
async function fillViews() {
  try {
    const { views } = await api.getViews();
    const seen = new Set();
    const once = (views || []).filter((v) => !seen.has(v.id) && seen.add(v.id));
    el("screen-view").innerHTML =
      `<option value="">Jump to view…</option>` +
      once.map((v) => `<option value="${esc(v.id)}">${esc(v.label)}</option>`).join("");
  } catch (_) {}
}

function markDirty() {
  if (dirty) return;
  dirty = true;
  setStatus("status", "unsaved changes");
}

function fillTimezones() {
  const list = el("tz-list");
  if (!list || typeof Intl.supportedValuesOf !== "function") return;
  try {
    list.innerHTML = Intl.supportedValuesOf("timeZone")
      .map((z) => `<option value="${esc(z)}"></option>`)
      .join("");
  } catch (_) {}
}

async function showStatusPanel() {
  const box = el("config-alert");
  try {
    const s = await api.getStatus();
    const c = s.config || {};
    if (c.error) {
      box.innerHTML =
        `<strong>config.yaml could not be read.</strong> The display is running with ${esc(c.source)}. ` +
        `Saving from this page writes a fresh file (the unreadable one is kept as config.yaml.bak.1).<br><code>${esc(c.error)}</code>`;
      box.style.display = "block";
    } else {
      box.style.display = "none";
    }
    const src = s.active ? `${esc(s.active)}${s.healthy ? "" : " (failing)"}` : "no traffic yet";
    el("backend-status").innerHTML =
      `backend up ${esc(fmtUptime(s.uptime_s))} · traffic source: ${src}` +
      (s.last_error ? ` · last error: <code>${esc(s.last_error)}</code>` : "") +
      weatherStatus(s.weather) +
      cameraStatus(s.cameras) +
      notamStatus(s.notams);
  } catch (_) {}
}

// The NOTAM feed's state for the status line: the source, whether the FAA
// credentials are set, and the last refresh error.
function notamStatus(n) {
  if (!n || !n.enabled) return "";
  if (n.source === "sample") return ` · NOTAMs: sample data${rewriteStatus(n.translate)}`;
  const creds = n.credentials ? "" : " (credentials missing in .env)";
  const err = n.error && n.credentials ? ` <code>${esc(n.error)}</code>` : "";
  return ` · NOTAMs: FAA${creds}${err}${rewriteStatus(n.translate)}`;
}

// The model rewrite's state: how many NOTAMs show a checked rewrite, how many
// answers the check refused, how many are still to be asked.
function rewriteStatus(t) {
  if (!t || !t.enabled) return "";
  const key = t.key ? "" : ", no API key in .env";
  const err = t.error ? ` <code>${esc(t.error)}</code>` : "";
  return ` · model ${esc(t.model)}: ${t.shown} shown, ${t.refused} refused, ${t.pending} waiting${key}${err}`;
}

// Ask the model endpoint typed into the form to rewrite one FAA example NOTAM
// and show the answer with the check's verdict.
async function testModel() {
  collect(root, cfg);
  const t = (cfg.notams && cfg.notams.translate) || {};
  const id = "model-test-result";
  if (!t.base_url || !t.model) return setStatus(id, "enter the endpoint URL and the model name first", "err");
  setStatus(id, "asking the model…");
  try {
    const r = await api.testNotamModel({ base_url: t.base_url, model: t.model, timeout_s: t.timeout_s || 60 });
    if (Array.isArray(r.models)) el("llm-models").innerHTML = r.models.map((m) => `<option value="${esc(m)}"></option>`).join("");
    const key = r.key ? "" : " (no HANGAR_LLM_API_KEY in .env)";
    if (r.error) return setStatus(id, r.error + key, "err");
    if (r.ok) return setStatus(id, `${r.notam} → “${r.answer}” passed the check (${r.ms} ms)`, "ok");
    setStatus(id, `the model answered, but its answer failed the check: ${(r.refused || []).join("; ")}`, "err");
  } catch (e) {
    setStatus(id, e.status === 401 ? "not authorized: reload and enter the admin password" : "error: " + (typeof e.detail === "string" ? e.detail : e.message), "err");
  }
}

// Ask the configured source for one location now and show what it returns.
async function checkNotam(i) {
  collect(root, cfg);
  const loc = ((cfg.notams && cfg.notams.locations) || [])[i];
  const id = `notam-check-${i}`;
  if (!loc || !el(id)) return;
  setStatus(id, "checking…");
  try {
    const r = await api.checkNotamLocation(loc.ident, cfg.notams.source);
    if (r.ok) {
      const from = r.source === "sample" ? "sample data" : "FAA";
      setStatus(id, `${from}: ${r.total} NOTAMs for ${r.asked_as.join(" / ")}, ${r.shown} shown with the saved look-ahead`, "ok");
    } else setStatus(id, r.error, "err");
  } catch (e) {
    setStatus(id, e.status === 401 ? "not authorized: reload and enter the admin password" : "error: " + (typeof e.detail === "string" ? e.detail : e.message), "err");
  }
}

// Weather for the status line: a failing refresh, and the airports whose report
// comes from the NWS gateway because aviationweather.gov has none for them.
function weatherStatus(w) {
  if (!w) return "";
  const g = w.gateway || {};
  const err = w.error || g.error;
  const via = (g.stations || []).length ? ` · weather via NWS gateway: ${esc(g.stations.join(", "))}` : "";
  return (err ? ` · weather: <code>${esc(err)}</code>` : "") + via;
}

// The camera relay's state for the status line: whether go2rtc answers, which tile
// streams it does not know, and which have no camera URL in .env.
function cameraStatus(c) {
  if (!c || !c.enabled) return "";
  const unknown = (c.unknown_streams || []).length ? ` (unknown streams: ${esc(c.unknown_streams.join(", "))})` : "";
  const empty = (c.empty_streams || []).length ? ` (no URL in .env: ${esc(c.empty_streams.join(", "))})` : "";
  const err = c.error ? ` <code>${esc(c.error)}</code>` : "";
  return ` · cameras: relay ${esc(c.relay)}${unknown}${empty}${err}`;
}

function fmtUptime(s) {
  if (s == null) return "";
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} days`;
}

async function save() {
  clearErrors(root);
  collect(root, cfg);
  const bad = validateNative(root);
  if (bad) {
    setStatus("status", `${bad} field${bad > 1 ? "s" : ""} need${bad > 1 ? "" : "s"} attention`, "err");
    return;
  }
  setStatus("status", "saving…");
  try {
    const r = await api.putConfig(cfg, version);
    version = r.version || version;
    dirty = false;
    setStatus("status", "saved ✓ (kiosk is reloading)", "ok");
    await load(); // re-read so normalized values (upper-cased ICAO, etc.) show
    showStatusPanel();
  } catch (e) {
    if (e.status === 409) {
      setStatus("status", "config was changed elsewhere; reload this page, then redo your edits", "err");
    } else if (e.status === 422 && Array.isArray(e.detail)) {
      const rest = applyErrors(root, e.detail);
      setStatus("status", rest.length ? rest.join("; ") : "fix the highlighted fields", "err");
    } else if (e.status === 401) {
      setStatus("status", "not authorized: reload the page and enter the admin password", "err");
    } else {
      setStatus("status", "error: " + (typeof e.detail === "string" ? e.detail : e.message), "err");
    }
  }
}

async function testSource() {
  clearErrors(root);
  collect(root, cfg);
  setStatus("test-result", "testing…");
  try {
    const r = await api.testSource(cfg.data_source);
    const parts = Object.entries(r.results || {}).map(([name, x]) =>
      x.ok ? `${name}: ok, ${x.count} aircraft within 50 nm (${x.ms} ms)` : `${name}: ${x.error}`
    );
    const where = r.point ? ` · test point ${r.point.lat.toFixed(2)}, ${r.point.lon.toFixed(2)}` : "";
    setStatus("test-result", (r.error ? r.error + " " : "") + parts.join(" · ") + where, r.ok ? "ok" : "err");
  } catch (e) {
    if (e.status === 422 && Array.isArray(e.detail)) {
      const rest = applyErrors(root, e.detail.map((d) => ({ ...d, loc: ["data_source", ...(d.loc || [])] })));
      setStatus("test-result", rest.join("; ") || "fix the highlighted fields", "err");
    } else {
      setStatus("test-result", "error: " + (typeof e.detail === "string" ? e.detail : e.message), "err");
    }
  }
}

// ---- remote screen control ---------------------------------------------------
async function screenAction(fn) {
  try {
    showScreenState(await fn());
    setTimeout(refreshScreenState, 1500); // the display reports its new view once it has switched
  } catch (e) {
    setStatus("screen-state", e.status === 401 ? "not authorized: reload and enter the admin password" : "error: " + e.message, "err");
  }
}

async function refreshScreenState() {
  try {
    showScreenState(await api.getDisplay());
  } catch (_) {}
}

function showScreenState(s) {
  const v = s.showing || {};
  const place = v.index != null && v.count ? ` (${v.index + 1} of ${v.count})` : "";
  const where = v.label ? ` ${v.label}${place}` : "";
  if (!s.active) return setStatus("screen-state", `screen is showing${where}`, "ok");
  const left = s.remaining_s != null ? `${Math.round(s.remaining_s)} s left` : "until restored";
  setStatus("screen-state", `blacked out, ${left}${s.reason ? ` (${s.reason})` : ""}`, "err");
}

function setStatus(id, text, cls = "") {
  const s = el(id);
  s.textContent = text;
  s.className = "status " + cls;
}

boot().catch((e) => setStatus("status", "load error: " + e.message, "err"));
