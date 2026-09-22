import { api } from "./lib/api.js";
import {
  applyAction,
  applyErrors,
  clearErrors,
  collect,
  renderAll,
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
  };
  fillTimezones();
  await load();

  el("save").addEventListener("click", save);
  el("test-source").addEventListener("click", testSource);
  el("blackout-10").addEventListener("click", () => screenAction(() => api.blackout({ seconds: 10, reason: "admin page" })));
  el("blackout-hold").addEventListener("click", () => screenAction(() => api.blackout({ reason: "admin page" })));
  el("screen-restore").addEventListener("click", () => screenAction(() => api.restoreDisplay()));
  refreshScreenState();
  setInterval(refreshScreenState, 5000);

  // One delegated listener for every add / delete / move button, so re-rendering
  // rows can never double-bind handlers.
  root.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-add],[data-del],[data-move]");
    if (!btn || btn.disabled) return;
    ev.preventDefault();
    collect(root, cfg); // keep edits made in other rows across the re-render
    if (applyAction(cfg, btn)) {
      renderAll(cfg, roots);
      markDirty();
    }
  });
  root.addEventListener("input", markDirty);
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
      (s.weather && s.weather.error ? ` · weather: <code>${esc(s.weather.error)}</code>` : "");
  } catch (_) {}
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
  if (!s.active) return setStatus("screen-state", "screen is showing", "ok");
  const left = s.remaining_s != null ? `${Math.round(s.remaining_s)} s left` : "until restored";
  setStatus("screen-state", `blacked out, ${left}${s.reason ? ` (${s.reason})` : ""}`, "err");
}

function setStatus(id, text, cls = "") {
  const s = el(id);
  s.textContent = text;
  s.className = "status " + cls;
}

boot().catch((e) => setStatus("status", "load error: " + e.message, "err"));
