// The admin page. Settings pages edit one config in memory and are saved
// together; streams, keys, secrets and the account apply at once through
// their own calls and never reload the display.
import { api } from "./lib/api.js";
import { envName, keysHtml, leftoversText, newKeyHtml, nmsKeysHtml, nmsStatePill, secretsHtml, storedPill, waitText } from "./lib/access.js";
import {
  applyAction,
  applyErrors,
  applyStep,
  clearErrors,
  collect,
  locationState,
  renderAll,
  syncRange,
  validateNative,
} from "./lib/adminForm.js";
import { changedPages, changedText, errorPages, navHtml, pageFromHash } from "./lib/adminNav.js";
import { cardsHtml, healthCards } from "./lib/adminStatus.js";
import { el, esc } from "./lib/dom.js";
import { markdownHtml } from "./lib/markdown.js";
import { layoutHtml, streamsHtml, testText } from "./lib/streams.js";

let cfg = null; // the config being edited
let saved = null; // the config as last loaded or saved
let version = null; // its version, sent back as If-Match on save
let streams = [];
let status = null;
let invalidPages = [];
let leaving = false;
let root = null;
let roots = null;

const nowS = () => Date.now() / 1000;
const currentPage = () => pageFromHash(location.hash);
const dirtyPages = () => (cfg ? changedPages(saved, cfg) : []);

function toLogin() {
  leaving = true;
  const page = /^#\/[a-z]+$/.test(location.hash) ? location.hash : "";
  location.replace("/login?next=" + encodeURIComponent("/admin" + page));
}

// What a failed call says. A 401 means the session ended: back to sign-in.
function said(e) {
  if (e && e.status === 401) {
    toLogin();
    return "signed out";
  }
  if (e && e.status === 429) return `Wait ${waitText(e.retryAfter)}, then try again.`;
  if (e && typeof e.detail === "string") return e.detail;
  if (e && Array.isArray(e.detail)) return e.detail.map((d) => d.msg).join("; ");
  return e && e.status ? `The display answered ${e.status}.` : "The display's backend is not answering.";
}

function setStatus(id, text, cls = "") {
  const s = el(id);
  s.textContent = text;
  s.className = "status " + cls;
}

async function boot() {
  const session = await api.getSession().catch(() => null);
  if (!session || !session.authenticated) return toLogin();
  root = el("admin-root");
  roots = {
    airports: el("airports"), regions: el("regions"), cycle: el("cycle"), datasource: el("datasource"),
    display: el("display"), satellite: el("satellite"), radar: el("radar"), cameras: el("cameras"), tiles: el("tiles"),
    notams: el("notam-settings"), notamEnvironment: el("notam-environment"), notamModel: el("notam-model"),
    notamLocations: el("notam-locations"),
  };
  el("who").textContent = `Signed in as ${session.username}`;
  el("who-top").textContent = session.username;
  el("acct-user").value = session.username;
  fillTimezones();
  wire();
  showPage();
  await load();
  refreshStatus();
  refreshScreen();
  loadAccess();
  setInterval(refreshStatus, 10000);
  setInterval(refreshScreen, 5000);
}

async function load() {
  cfg = await api.getConfig();
  version = cfg.version || null;
  delete cfg.version;
  saved = structuredClone(cfg);
  if (secretsListing) renderNmsKeys(); // the environment in use may have changed
  await loadStreams(false);
  invalidPages = [];
  renderForm();
  updateDirty();
  fillViews();
}

function locationDetail() {
  const out = {};
  for (const d of (status && status.notams && status.notams.detail) || []) out[d.ident] = d;
  return out;
}

function renderForm() {
  renderAll(cfg, roots, { streams, locations: locationDetail() });
  renderDerived();
}

// What is drawn from the config being edited: the camera grid and the source label.
function renderDerived() {
  el("cam-layout").innerHTML = layoutHtml((cfg.cameras && cfg.cameras.tiles) || [], streams);
  const n = cfg.notams || {};
  const described = n.translate && n.translate.description;
  el("source-label").textContent =
    n.source === "sample"
      ? "source: sample data"
      : described
        ? `source: FAA NMS Processed with ${described} LLM`
        : "Name the model above: live NOTAM views need it.";
}

function updateDirty() {
  if (!cfg) return;
  collect(root, cfg);
  const pages = dirtyPages();
  el("savebar").hidden = pages.length === 0;
  el("save-what").textContent = changedText(pages);
  el("nav").innerHTML = navHtml(currentPage(), pages, invalidPages);
  renderDerived();
}

function showPage() {
  const page = currentPage();
  for (const s of document.querySelectorAll(".page")) s.hidden = s.dataset.page !== page;
  el("nav").innerHTML = navHtml(page, dirtyPages(), invalidPages);
  el("side").classList.remove("open");
  el("menu-toggle").setAttribute("aria-expanded", "false");
  window.scrollTo(0, 0);
  if (page === "access") loadAccess();
  if (page === "cameras" && cfg) loadStreams(true);
  if (page === "api") loadApiDoc();
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

// ---- saving ---------------------------------------------------------------------
function markInvalid(extra = []) {
  const marked = [...root.querySelectorAll(".invalid, .field-error")];
  const pages = marked.map((m) => m.closest(".page")).filter(Boolean).map((p) => p.dataset.page);
  invalidPages = [...new Set([...pages, ...extra])];
  const first = marked[0];
  const page = first && first.closest(".page") ? first.closest(".page").dataset.page : invalidPages[0];
  if (page && page !== currentPage()) location.hash = `#/${page}`;
  if (first) {
    setTimeout(() => {
      first.scrollIntoView({ block: "center" });
      if (first.focus) first.focus();
    }, 60);
  }
  updateDirty();
}

async function save() {
  clearErrors(root);
  collect(root, cfg);
  const bad = validateNative(root);
  if (bad) {
    setStatus("status", `${bad} field${bad > 1 ? "s" : ""} need${bad > 1 ? "" : "s"} correcting before saving`, "err");
    return markInvalid();
  }
  setStatus("status", "Saving…");
  try {
    const r = await api.putConfig(cfg, version);
    version = r.version || version;
    await load();
    setStatus("status", "");
    note("Saved. The display is reloading with the new settings.");
    refreshStatus();
  } catch (e) {
    if (e.status === 409) {
      setStatus("status", "The settings were changed elsewhere. Discard, then redo your edits.", "err");
    } else if (e.status === 422 && Array.isArray(e.detail)) {
      const rest = applyErrors(root, e.detail);
      setStatus("status", rest.length ? rest.join("; ") : "Correct the marked fields, then save again", "err");
      markInvalid(errorPages(e.detail));
    } else {
      setStatus("status", said(e), "err");
    }
  }
}

function discard() {
  cfg = structuredClone(saved);
  clearErrors(root);
  invalidPages = [];
  setStatus("status", "");
  renderForm();
  updateDirty();
}

let noteTimer = null;
function note(text) {
  const n = el("saved-note");
  n.textContent = text;
  n.hidden = false;
  clearTimeout(noteTimer);
  noteTimer = setTimeout(() => (n.hidden = true), 8000);
}

// ---- events ---------------------------------------------------------------------
function wire() {
  window.addEventListener("hashchange", showPage);
  el("menu-toggle").addEventListener("click", () => {
    const open = el("side").classList.toggle("open");
    el("menu-toggle").setAttribute("aria-expanded", String(open));
  });
  el("sign-out").addEventListener("click", async () => {
    if (dirtyPages().length && !confirm("Sign out without saving the changes?")) return;
    try {
      await api.logout();
    } catch (_) {}
    leaving = true;
    location.replace("/login");
  });
  el("save").addEventListener("click", save);
  el("discard").addEventListener("click", discard);
  el("test-source").addEventListener("click", testSource);
  el("screen-previous").addEventListener("click", () => screenAction(() => api.previousView()));
  el("screen-next").addEventListener("click", () => screenAction(() => api.nextView()));
  el("blackout-10").addEventListener("click", () => screenAction(() => api.blackout({ seconds: 10, reason: "admin page" })));
  el("blackout-hold").addEventListener("click", () => screenAction(() => api.blackout({ reason: "admin page" })));
  el("screen-restore").addEventListener("click", () => screenAction(() => api.restoreDisplay()));
  el("stream-new-add").addEventListener("click", addStream);
  el("stream-new-test").addEventListener("click", testNewStream);
  el("nms-store").addEventListener("click", storeNmsKeys);
  el("key-create").addEventListener("click", createKey);
  el("acct-save").addEventListener("click", saveAccount);
  el("api-doc-copy").addEventListener("click", copyApiDoc);
  el("logout-all").addEventListener("click", async () => {
    if (!confirm("Sign out every browser, this one included?")) return;
    try {
      await api.logoutAll();
      toLogin();
    } catch (e) {
      setStatus("acct-status", said(e), "err");
    }
  });

  // One delegated listener for every button a re-render can replace.
  root.addEventListener("click", onClick);
  const edited = (ev) => {
    if (!ev.target.dataset || !ev.target.dataset.f) return;
    syncRange(ev.target);
    updateDirty();
  };
  root.addEventListener("input", edited);
  root.addEventListener("change", edited);
  root.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter") return;
    const press = { "notam-new": "notam-add", "stream-new-url": "stream-new-add", "key-name": "key-create" }[ev.target.id];
    if (!press) return;
    ev.preventDefault();
    el(press).click();
  });
  window.addEventListener("beforeunload", (e) => {
    if (leaving || !dirtyPages().length) return;
    e.preventDefault();
    e.returnValue = "";
  });
}

function onClick(ev) {
  const pick = (sel) => ev.target.closest(sel);
  let b;
  if ((b = pick("[data-check-notam]"))) return checkNotam(+b.dataset.checkNotam);
  if (pick("[data-test-model]")) return testModel();
  if ((b = pick("[data-step]"))) {
    if (applyStep(root, b)) updateDirty();
    return;
  }
  if ((b = pick("[data-stream-test]"))) return testStream(b.dataset.streamTest);
  if ((b = pick("[data-stream-edit]"))) {
    const box = el(`stream-edit-${b.dataset.streamEdit}`);
    box.hidden = !box.hidden;
    return;
  }
  if ((b = pick("[data-stream-save]"))) return saveStream(b.dataset.streamSave);
  if ((b = pick("[data-stream-remove]"))) return removeStream(b.dataset.streamRemove);
  if ((b = pick("[data-secret-store]"))) return storeSecret(b.dataset.secretStore, b.dataset.secretInput);
  if ((b = pick("[data-secret-clear]"))) return clearSecret(b.dataset.secretClear);
  if ((b = pick("[data-nms-check]"))) return checkNmsKeys(b.dataset.nmsCheck);
  if ((b = pick("[data-nms-remove]"))) return removeNmsKeys(b.dataset.nmsRemove);
  if ((b = pick("[data-revoke-key]"))) return revokeKey(b.dataset.revokeKey);
  if ((b = pick("[data-copy]"))) return copyText(b);
  if ((b = pick("[data-view]"))) return screenAction(() => api.showView(b.dataset.view));
  b = pick("[data-add],[data-del],[data-move]");
  if (!b || b.disabled) return;
  ev.preventDefault();
  collect(root, cfg); // keep edits made in other rows across the re-render
  if (applyAction(cfg, b)) {
    renderForm();
    updateDirty();
  }
}

// ---- overview -------------------------------------------------------------------
async function refreshStatus() {
  try {
    status = await api.getStatus();
  } catch (_) {
    return;
  }
  const c = status.config || {};
  const alert = el("config-alert");
  alert.hidden = !c.error;
  if (c.error) {
    alert.innerHTML =
      `<span><strong>config.yaml could not be read.</strong> The display is running with ${esc(c.source)}. ` +
      `Saving from this page writes a fresh file (the unreadable one is kept as config.yaml.bak.1).<br><code>${esc(c.error)}</code></span>`;
  }
  el("health").innerHTML = cardsHtml(healthCards(status, nowS()));
  renderNotamState();
}

function stat(label, value, sub) {
  return `<div class="stat"><span class="sub">${esc(label)}</span><b>${esc(value)}</b><span class="sub">${esc(sub)}</span></div>`;
}

function renderNotamState() {
  const n = (status && status.notams) || {};
  el("notam-blocked").hidden = !n.blocked;
  el("notam-blocked-text").textContent = n.blocked ? `NOTAM views are held back: ${n.blocked}.` : "";
  const pulling = n.enabled && n.source === "faa" && !n.blocked;
  el("notam-gate").textContent = !pulling
    ? "No pulls: live NOTAM views are off"
    : n.rate_limited
      ? `The FAA is limiting requests; next pull in ${waitText(n.next_pull_in_s)}`
      : n.next_pull_in_s > 0
        ? `Next FAA pull in ${waitText(n.next_pull_in_s)}`
        : "The next pull can be made now";
  const u = n.usage || {};
  el("notam-usage-title").textContent = u.environment
    ? `Requests to the FAA (${envName(u.environment)}), last 24 hours` : "Requests to the FAA, last 24 hours";
  el("notam-usage").innerHTML =
    stat("Data pulls", u.pulls || 0, `${u.deltas || 0} of changes, ${u.baselines || 0} whole sets`) +
    stat("Shortest interval", u.shortest_gap_s == null ? "–" : `${u.shortest_gap_s} s`, `Production's limit is ${u.limit_gap_s || 180} s`) +
    stat("Refused by the FAA", (u.status_429 || 0) + (u.status_401 || 0), `${u.status_429 || 0} for pace (429), ${u.status_401 || 0} tokens (401)`) +
    stat("Sets compared", u.compared || 0, u.mismatches ? `${u.mismatches} missed by pulls of changes` : "Nothing missed");
  const detail = locationDetail();
  ((cfg && cfg.notams && cfg.notams.locations) || []).forEach((loc, i) => {
    const span = document.getElementById(`notam-check-${i}`);
    if (span && !span.dataset.said) span.textContent = locationState(detail[String(loc.ident || "").toUpperCase()]);
  });
  const t = n.translate || {};
  el("model-state").innerHTML = !t.enabled
    ? `<span class="pill">Rewrite off</span>`
    : t.error
      ? `<span class="pill bad">Model not answering</span>`
      : `<span class="pill good">${Number(t.shown) || 0} shown, ${Number(t.refused) || 0} refused, ${Number(t.pending) || 0} waiting</span>`;
}

// The cycle can show a view more than once; the lists name each view once.
let views = [];
async function fillViews() {
  try {
    const seen = new Set();
    views = ((await api.getViews()).views || []).filter((v) => !seen.has(v.id) && seen.add(v.id));
  } catch (_) {
    return;
  }
  el("rotation-list").innerHTML =
    views.map((v) => `<div class="row"><span class="name">${esc(v.label)}</span><span class="sub">${esc(v.dwell_s)} s</span></div>`).join("") ||
    `<div class="empty">No views yet.</div>`;
  refreshScreen();
}

async function screenAction(fn) {
  try {
    showScreen(await fn());
    setTimeout(refreshScreen, 1500); // the display reports its new view once it has switched
  } catch (e) {
    setStatus("screen-state", said(e), "err");
  }
}

async function refreshScreen() {
  try {
    showScreen(await api.getDisplay());
  } catch (_) {}
}

function showScreen(s) {
  const v = s.showing || {};
  const place = v.index != null && v.count ? ` (${v.index + 1} of ${v.count})` : "";
  el("screen-now").textContent = s.active ? "Blacked out" : v.label ? `${v.label}${place}` : "No display has reported a view yet";
  el("screen-restore").hidden = !s.active;
  el("blackout-10").hidden = el("blackout-hold").hidden = !!s.active;
  el("screen-views").innerHTML = views
    .map((x) => `<button type="button" class="btn${x.id === v.view ? " on" : ""}" data-view="${esc(x.id)}">${esc(x.label)}</button>`)
    .join("");
  if (!s.active) return setStatus("screen-state", "");
  const left = s.remaining_s != null ? `${Math.round(s.remaining_s)} s left` : "until restored";
  setStatus("screen-state", `Blacked out, ${left}${s.reason ? ` (${s.reason})` : ""}`, "err");
}

// ---- tests of what the form holds ---------------------------------------------------
async function testSource() {
  clearErrors(root);
  collect(root, cfg);
  setStatus("test-result", "Testing…");
  try {
    const r = await api.testSource(cfg.data_source);
    const parts = Object.entries(r.results || {}).map(([name, x]) =>
      x.ok ? `${name}: ${x.count} aircraft within 50 nm (${x.ms} ms)` : `${name}: ${x.error}`
    );
    const where = r.point ? `, around ${r.point.lat.toFixed(2)}, ${r.point.lon.toFixed(2)}` : "";
    setStatus("test-result", (r.error ? r.error + " " : "") + parts.join("; ") + where, r.ok ? "ok" : "err");
  } catch (e) {
    if (e.status === 422 && Array.isArray(e.detail)) {
      const rest = applyErrors(root, e.detail.map((d) => ({ ...d, loc: ["data_source", ...(d.loc || [])] })));
      setStatus("test-result", rest.join("; ") || "Correct the marked fields", "err");
    } else {
      setStatus("test-result", said(e), "err");
    }
  }
}

// Ask the model endpoint typed into the form to rewrite one FAA example NOTAM
// and show the answer with the check's verdict.
async function testModel() {
  collect(root, cfg);
  const t = (cfg.notams && cfg.notams.translate) || {};
  const id = "model-test-result";
  if (!t.base_url || !t.model) return setStatus(id, "Enter the endpoint and the model name first", "err");
  setStatus(id, "Asking the model…");
  try {
    const r = await api.testNotamModel({ base_url: t.base_url, model: t.model, timeout_s: t.timeout_s || 60 });
    if (Array.isArray(r.models)) el("llm-models").innerHTML = r.models.map((m) => `<option value="${esc(m)}"></option>`).join("");
    const key = r.key ? "" : " (no endpoint API key is stored)";
    if (r.error) return setStatus(id, r.error + key, "err");
    if (r.ok) return setStatus(id, `${r.notam} → “${r.answer}” passed the check (${r.ms} ms)`, "ok");
    setStatus(id, `The model answered, but its answer failed the check: ${(r.refused || []).join("; ")}`, "err");
  } catch (e) {
    setStatus(id, said(e), "err");
  }
}

// Sample data: show what it holds. The FAA: fetch the location's whole set at the next turn.
async function checkNotam(i) {
  collect(root, cfg);
  const loc = ((cfg.notams && cfg.notams.locations) || [])[i];
  const id = `notam-check-${i}`;
  if (!loc || !document.getElementById(id)) return;
  el(id).dataset.said = "1";
  setStatus(id, "Asking…");
  try {
    const r = await api.checkNotamLocation(loc.ident, cfg.notams.source);
    if (!r.ok) return setStatus(id, r.error, "err");
    if (r.queued) {
      setStatus(id, r.wait_s > 0 ? `Queued: the FAA is asked in about ${waitText(r.wait_s)}` : "Queued: the FAA is asked now", "ok");
      setTimeout(() => {
        const span = document.getElementById(id);
        if (span) delete span.dataset.said;
      }, (r.wait_s + 20) * 1000);
      return;
    }
    setStatus(id, `Sample data: ${r.total} NOTAMs for ${r.asked_as.join(" / ")}, ${r.shown} shown with the saved look-ahead`, "ok");
  } catch (e) {
    setStatus(id, said(e), "err");
  }
}

// ---- camera streams -----------------------------------------------------------------
async function loadStreams(rerender) {
  try {
    const r = await api.getStreams();
    streams = r.streams || [];
    el("relay-state").innerHTML =
      r.relay === "ok" ? `<span class="pill good">Relay answering</span>` : `<span class="pill bad">Relay not answering</span>`;
  } catch (e) {
    if (e.status === 401) return toLogin();
  }
  el("streams").innerHTML = streamsHtml(streams);
  if (rerender && cfg) {
    collect(root, cfg);
    renderAll(cfg, { tiles: roots.tiles }, { streams }); // the tiles' choices follow the stored streams
    renderDerived();
  }
}

async function addStream() {
  const name = el("stream-new-name").value.trim();
  const url = el("stream-new-url").value.trim();
  if (!name) return setStatus("stream-new-status", "Give the stream a name", "err");
  if (!url) return setStatus("stream-new-status", "Paste the stream's address", "err");
  try {
    const made = await api.addStream({ name, url });
    el("stream-new-name").value = el("stream-new-url").value = "";
    setStatus("stream-new-status", `Stored as ${made.name}. The relay has it within moments.`, "ok");
    await loadStreams(true);
    loadAccess();
  } catch (e) {
    setStatus("stream-new-status", said(e), "err");
  }
}

async function testNewStream() {
  const url = el("stream-new-url").value.trim();
  if (!url) return setStatus("stream-new-status", "Paste the stream's address", "err");
  setStatus("stream-new-status", "Asking the camera…");
  try {
    const t = testText(await api.testStream({ url }));
    setStatus("stream-new-status", t.text, t.cls);
  } catch (e) {
    setStatus("stream-new-status", said(e), "err");
  }
}

async function testStream(id) {
  const where = `stream-status-${id}`;
  setStatus(where, "Asking the camera…");
  try {
    const t = testText(await api.testStream({ id }));
    setStatus(where, t.text, t.cls);
  } catch (e) {
    setStatus(where, said(e), "err");
  }
}

async function saveStream(id) {
  const body = { name: el(`stream-name-${id}`).value.trim() };
  const url = el(`stream-url-${id}`).value.trim();
  if (url) body.url = url;
  try {
    await api.changeStream(id, body);
    await loadStreams(true);
    setStatus(`stream-status-${id}`, url ? "Stored. Its tiles reconnect onto the new address." : "Stored.", "ok");
  } catch (e) {
    setStatus(`stream-status-${id}`, said(e), "err");
  }
}

async function removeStream(id) {
  if (!confirm("Remove this stream and its stored address?")) return;
  try {
    await api.removeStream(id);
    await loadStreams(true);
    loadAccess();
  } catch (e) {
    setStatus(`stream-status-${id}`, said(e), "err");
  }
}

// ---- stored secrets, API keys, the account --------------------------------------------
const SECRET_STATUS = { llm_api_key: "model-test-result", aggregator_api_key: "test-result" };
let secretsListing = null; // the stored credentials' state as last read

// The FAA keys of each environment, against the environment the saved settings use.
function renderNmsKeys() {
  const nms = (secretsListing && secretsListing.secrets && secretsListing.secrets.nms) || {};
  const inUse = (saved && saved.notams && saved.notams.environment) || "production";
  el("nms-state").innerHTML = secretsListing ? nmsStatePill(nms, inUse) : "";
  el("nms-keys").innerHTML = nmsKeysHtml(nms, inUse, nowS());
}

function renderSecrets(listing) {
  secretsListing = listing;
  const s = listing.secrets || {};
  renderNmsKeys();
  el("llm-key-state").innerHTML = storedPill(s.llm_api_key, nowS());
  el("agg-key-state").innerHTML = storedPill(s.aggregator_api_key, nowS());
  el("secrets").innerHTML = secretsHtml(s, streams.length, nowS());
  if (listing.leftovers) {
    const text = listing.leftovers.length
      ? `Still set in .env and no longer read: ${leftoversText(listing.leftovers)}. Delete those lines, then run docker compose up -d.`
      : "";
    for (const id of ["leftovers", "leftovers-access"]) {
      el(id).hidden = !text;
      el(`${id}-text`).textContent = text;
    }
  }
}

async function loadAccess() {
  try {
    const [keys, listing] = await Promise.all([api.getKeys(), api.getSecrets()]);
    el("keys").innerHTML = keysHtml(keys.keys, nowS());
    renderSecrets(listing);
  } catch (e) {
    if (e.status === 401) toLogin();
  }
}

async function storeSecret(name, inputId) {
  const input = el(inputId);
  const value = input.value.trim();
  if (!value) return setStatus(SECRET_STATUS[name], "Type or paste the key first", "err");
  try {
    renderSecrets(await api.putSecret(name, value));
    input.value = "";
    setStatus(SECRET_STATUS[name], "Key stored.", "ok");
  } catch (e) {
    setStatus(SECRET_STATUS[name], said(e), "err");
  }
}

async function clearSecret(name) {
  if (!confirm("Remove the stored value?")) return;
  try {
    renderSecrets(await api.deleteSecret(name));
    setStatus(SECRET_STATUS[name], "Removed.", "ok");
  } catch (e) {
    setStatus(SECRET_STATUS[name], said(e), "err");
  }
}

async function storeNmsKeys() {
  const env = el("nms-for").value;
  const client_id = el("nms-id").value.trim();
  const client_secret = el("nms-secret").value.trim();
  if (!client_id || !client_secret) return setStatus("nms-status", "Enter both the client ID and the secret", "err");
  try {
    renderSecrets(await api.putNmsKeys(env, { client_id, client_secret }));
    el("nms-id").value = el("nms-secret").value = "";
    const unused = saved.notams.environment !== env
      ? ` The display uses ${envName(saved.notams.environment)}; choose ${envName(env)} above and save to use them.` : "";
    setStatus("nms-status", `Keys stored for ${envName(env)}.${unused}`, "ok");
  } catch (e) {
    setStatus("nms-status", said(e), "err");
  }
}

async function checkNmsKeys(env) {
  setStatus("nms-status", `Asking ${envName(env)} for a token…`);
  try {
    const r = await api.checkNmsKeys(env);
    setStatus("nms-status", r.ok ? `${envName(r.environment)} issued a token: the keys work.` : r.error, r.ok ? "ok" : "err");
  } catch (e) {
    setStatus("nms-status", said(e), "err");
  }
}

async function removeNmsKeys(env) {
  const inUse = saved.notams.environment === env ? " The display uses this environment and stops getting NOTAMs." : "";
  if (!confirm(`Remove the keys stored for ${envName(env)}?${inUse}`)) return;
  try {
    renderSecrets(await api.deleteNmsKeys(env));
    setStatus("nms-status", `Keys for ${envName(env)} removed.`, "ok");
  } catch (e) {
    setStatus("nms-status", said(e), "err");
  }
}

async function createKey() {
  const name = el("key-name").value.trim();
  if (!name) return setStatus("key-status", "Name the key after what will use it", "err");
  try {
    const made = await api.createKey(name, el("key-scope").value);
    el("key-name").value = "";
    setStatus("key-status", "");
    el("key-new").innerHTML = newKeyHtml(made.key);
    loadAccess();
  } catch (e) {
    setStatus("key-status", said(e), "err");
  }
}

async function revokeKey(id) {
  if (!confirm("Revoke this key? Whatever uses it stops working at once.")) return;
  try {
    await api.revokeKey(id);
    el("key-new").innerHTML = "";
    loadAccess();
  } catch (e) {
    setStatus("key-status", said(e), "err");
  }
}

// Put text on the clipboard: through the Clipboard API where the page may use it
// (https or localhost), else with the copy command from a hidden text box.
async function clip(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {}
  }
  const box = document.createElement("textarea");
  box.value = text;
  box.readOnly = true;
  box.style.cssText = "position: fixed; top: 0; left: 0; opacity: 0; pointer-events: none";
  document.body.appendChild(box);
  box.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (_) {}
  box.remove();
  return ok;
}

async function copyText(button) {
  const source = el(button.dataset.copy);
  if (await clip(source.textContent)) {
    button.textContent = "Copied";
    return;
  }
  const range = document.createRange(); // left selected, to copy by hand
  range.selectNodeContents(source);
  const selection = getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  button.textContent = "Selected: copy it";
}

// ---- the display API reference ------------------------------------------------------
let apiDoc = "";

async function loadApiDoc() {
  try {
    apiDoc = await api.getDisplayApiDoc();
    el("api-doc").innerHTML = markdownHtml(apiDoc);
    setStatus("api-doc-status", "");
  } catch (e) {
    setStatus("api-doc-status", said(e), "err");
  }
}

async function copyApiDoc() {
  if (!apiDoc) return setStatus("api-doc-status", "The reference has not loaded yet.", "err");
  const ok = await clip(apiDoc);
  setStatus(
    "api-doc-status",
    ok ? "Copied as Markdown, ready to paste." : "This browser did not allow copying: use Download.",
    ok ? "ok" : "err"
  );
}

async function saveAccount() {
  const body = { current_password: el("acct-current").value };
  const username = el("acct-user").value.trim();
  const fresh = el("acct-new").value;
  if (!body.current_password) return setStatus("acct-status", "Enter the current password to confirm", "err");
  if (username) body.username = username;
  if (fresh) body.new_password = fresh;
  try {
    const r = await api.updateAccount(body);
    el("acct-current").value = el("acct-new").value = "";
    el("who").textContent = `Signed in as ${r.username}`;
    el("who-top").textContent = r.username;
    setStatus("acct-status", fresh ? "Updated. Every other browser was signed out." : "Updated.", "ok");
  } catch (e) {
    setStatus("acct-status", e.status === 403 && typeof e.detail === "string" ? e.detail : said(e), "err");
  }
}

boot().catch((e) => {
  document.body.insertAdjacentHTML("afterbegin", `<p class="notice bad">The admin page could not start: ${esc(e.message)}</p>`);
});
