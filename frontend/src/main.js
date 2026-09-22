import { api, subscribeEvents } from "./lib/api.js";
import { AircraftStore } from "./lib/aircraft.js";
import { el, esc, sleep } from "./lib/dom.js";
import { ALT_STOPS, HangarMap } from "./lib/map.js";
import { windBarbSVG } from "./lib/windbarb.js";

const CAT_FALLBACK = "#ffffff";
const POLL_MS = 1000;                 // traffic poll cadence on local views
const MOTION_FRAME_MS = 50;           // aircraft marker update interval (20 fps)
const FALLBACK_DWELL_S = 15;          // used if a view switch throws
const WATCHDOG_MS = 30 * 1000;        // how often the watchdog checks
const WATCHDOG_STALL_MS = 5 * 60 * 1000; // no successful API call for this long -> reload
const DAILY_RELOAD_MS = 24 * 60 * 60 * 1000;
const SAT_FRAME_MS = 140;
const SAT_HOLD_MS = 1600;             // pause on the newest satellite frame each loop

// altitude -> color, mirroring the map's ALT_STOPS, for list dots.
function altColor(altFt, onGround) {
  if (onGround) return "#9ca3af";
  if (altFt == null || altFt < 0) return "#cbd5e1";
  let c = ALT_STOPS[0][1];
  for (const [stop, col] of ALT_STOPS) if (altFt >= stop) c = col;
  return c;
}

const state = {
  cfg: null,
  cfgVersion: null,
  bootId: null,
  views: [],
  idx: 0,
  view: null,
  store: new AircraftStore(15),
  weather: {},
  pollTimer: null,
  pollSeq: 0,
  motionRaf: null,
  dwellTimer: null,
  satTimer: null,
  blackoutTimer: null, // local safety timer for a timed remote blackout
  map: null,
  es: null,
  clocksStarted: false,
  lastOk: Date.now(), // last successful API response (watchdog)
  lastListHtml: null,
  tz: "UTC",
  timeFmt: null,
};

// ---- boot -------------------------------------------------------------------
async function boot() {
  showOverlay("Loading…");
  state.cfg = await api.getConfig();
  state.cfgVersion = state.cfg.version || null;
  state.lastOk = Date.now();
  state.store.setDropTimeout(state.cfg?.data_source?.drop_timeout_s);
  // Subscribe before the (slow) map setup so remote blackout / config events
  // are honored even while tiles are still loading.
  if (!state.es) state.es = subscribeEvents(onServerEvent);
  if (!state.map) {
    state.map = new HangarMap(el("map"), {
      basemap: state.cfg?.display?.basemap,
      tileUrl: state.cfg?.display?.tile_url,
    });
    await state.map.ready;
  }
  renderLegend();
  startClocks();

  await loadWeather();
  await loadViews();
  await state.map.warmViews(state.views);
  hideOverlay();
  nextView(0);
}

// Keep retrying boot until the backend answers. A page reload would be worse
// here: if the backend is down, Chromium's error page has no script to retry.
async function bootWithRetry() {
  let delay = 2000;
  for (;;) {
    try {
      await boot();
      return;
    } catch (e) {
      console.error("boot failed", e);
      const why = String(e.message || e).replace(/\s+/g, " ").slice(0, 100);
      showOverlay(`Waiting for backend… retrying in ${Math.round(delay / 1000)}s (${why})`);
      await sleep(delay);
      delay = Math.min(delay * 2, 30000);
    }
  }
}

async function loadViews() {
  const r = await api.getViews();
  state.lastOk = Date.now();
  state.views = r.views || [];
}

async function loadWeather() {
  try {
    const ids = (state.cfg.airports || []).map((a) => a.icao);
    const r = await api.getWeather(ids);
    state.lastOk = Date.now();
    state.weather = r.metars || {};
  } catch (_) {}
}

// Reload only when the backend is actually reachable; otherwise stay on this
// page (which keeps retrying) rather than landing on a dead browser error page.
async function safeReload(reason) {
  try {
    await api.health();
  } catch (_) {
    return false;
  }
  console.log("reloading:", reason);
  location.reload();
  return true;
}

function onServerEvent(msg) {
  if (msg.event === "connected") {
    // A different boot id means the backend was restarted or rebuilt since we
    // loaded; a different config version means it was edited while we were
    // disconnected. Either way, pick up the current bundle and config.
    if (state.bootId && msg.boot_id && msg.boot_id !== state.bootId) return safeReload("backend restarted");
    if (state.cfgVersion && msg.version && msg.version !== state.cfgVersion) return safeReload("config changed");
    state.bootId = msg.boot_id || state.bootId;
    if (msg.display) applyDisplayState(msg.display); // stay in sync with a blackout that started while disconnected
  } else if (msg.event === "display") {
    applyDisplayState(msg);
  } else if (msg.event === "config_changed") {
    safeReload("config changed");
  } else if (msg.event === "weather_updated") {
    loadWeather().then(() => {
      if (!state.view) return;
      if (state.view.type === "local") {
        renderSideHeader();
        renderAirportBarbs();
      } else if (state.view.type === "regional") {
        showRegionalWeather(state.view);
      }
    });
  }
}

// Remote blackout (e.g. a Home Assistant automation calling /api/display/blackout):
// cover the whole screen with black; everything keeps running underneath so the
// picture comes back instantly on restore. A timed blackout also arms a local
// timer so the screen returns even if the restore event were missed.
function applyDisplayState(s) {
  const cover = el("blackout");
  if (state.blackoutTimer) clearTimeout(state.blackoutTimer);
  state.blackoutTimer = null;
  const on = !!s.active;
  cover.classList.toggle("active", on);
  if (on && s.until) {
    const ms = s.until * 1000 - Date.now();
    if (ms <= 0) cover.classList.remove("active");
    else state.blackoutTimer = setTimeout(() => cover.classList.remove("active"), ms + 1000);
  }
}

function startWatchdog() {
  setInterval(() => {
    if (Date.now() - state.lastOk > WATCHDOG_STALL_MS) safeReload("no successful API call for 5 minutes");
  }, WATCHDOG_MS);
  // A daily reload bounds any slow browser-side memory growth on a 24/7 kiosk.
  const jitter = Math.random() * 60 * 60 * 1000;
  setTimeout(async function daily() {
    if (!(await safeReload("daily refresh"))) setTimeout(daily, 10 * 60 * 1000);
  }, DAILY_RELOAD_MS + jitter);
}

// ---- cycle ------------------------------------------------------------
// Always schedules the next view, even if switching this one throws, so a
// single bad view can never stop the cycle.
function nextView(idx) {
  if (state.dwellTimer) clearTimeout(state.dwellTimer);
  let dwell = FALLBACK_DWELL_S;
  try {
    dwell = switchView(idx) || dwell;
  } catch (e) {
    console.error("view switch failed", e);
    el("view-label").textContent = "View error";
  }
  animateCountdown(dwell);
  state.dwellTimer = setTimeout(() => nextView(state.idx + 1), dwell * 1000);
}

function switchView(idx) {
  stopPolling();
  stopMotion();
  if (state.satTimer) clearTimeout(state.satTimer);
  if (!state.views.length) {
    showOverlay("No views configured. Open /admin to add airports.");
    return FALLBACK_DWELL_S;
  }
  state.idx = idx % state.views.length;
  state.view = state.views[state.idx];
  state.store.clear();
  state.lastListHtml = null;
  state.map.clearAircraft();
  state.map.hideRadar();
  showRadarOverlays(false);

  el("view-label").textContent = state.view.label;

  if (state.view.type === "satellite") {
    showStage("sat");
    setSatelliteView(state.view);
  } else if (state.view.type === "local") {
    showStage("map");
    state.map.resize();
    state.map.frameView(state.view);
    state.map.showLabels(true);
    el("panel-title").textContent = "AIRCRAFT IN VIEW";
    renderLegend("alt");
    renderSideHeader();
    renderAirportBarbs();
    startPolling();
    startMotion();
  } else {
    // Regional = weather: wind barbs for ALL stations in view, drawn on top of an
    // animated NEXRAD precipitation overlay. No aircraft.
    showStage("map");
    state.map.resize();
    state.map.frameView(state.view);
    el("side-header").innerHTML = "";
    el("panel-title").textContent = "WEATHER STATIONS";
    renderLegend("cat");
    startRadarOverlay(state.view); // precipitation underneath; barbs (DOM markers) draw above
    showRegionalWeather(state.view);
  }
  return state.view.dwell_s;
}

// Toggle the stage between "map" (map + side panel) and "sat" (fullscreen
// satellite loop). The satellite view is an overlay: the map container keeps
// its size underneath. Hiding the map with display:none shrinks it to 0x0 and
// MapLibre then trims its tile cache to a handful of tiles, so every later
// view would refetch its basemap and render blurry first.
function showStage(mode) {
  el("stage").classList.toggle("stage-sat", mode === "sat");
  el("sat").classList.toggle("active", mode === "sat");
}

function showRadarOverlays(on) {
  el("radar-caption").style.display = on ? "block" : "none";
  el("radar-legend").style.display = on ? "block" : "none";
}

// ---- traffic polling ----------------------------------------------------
// Self-scheduling: the next poll is queued only after the current one finishes,
// so a slow backend never causes overlapping in-flight requests.
function startPolling() {
  const seq = ++state.pollSeq;
  const loop = async () => {
    if (seq !== state.pollSeq) return;
    await pollTraffic();
    if (seq !== state.pollSeq) return;
    state.pollTimer = setTimeout(loop, POLL_MS);
  };
  loop();
}

function stopPolling() {
  state.pollSeq++;
  if (state.pollTimer) clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

// ---- aircraft motion ------------------------------------------------------
// Between snapshots each aircraft is dead-reckoned from its last fix (see
// AircraftStore), and the markers are moved at MOTION_FRAME_MS while a local
// view is on screen. Motion is therefore continuous regardless of when the
// receiver, the backend, or the poll happen to deliver the next fix.
function startMotion() {
  stopMotion();
  let last = 0;
  const frame = (t) => {
    if (!state.view || state.view.type !== "local") {
      state.motionRaf = null;
      return;
    }
    if (t - last >= MOTION_FRAME_MS) {
      last = t;
      try {
        state.map.moveAircraft(state.store, Date.now());
      } catch (e) {
        console.error("motion frame failed", e);
      }
    }
    state.motionRaf = requestAnimationFrame(frame);
  };
  state.motionRaf = requestAnimationFrame(frame);
}

function stopMotion() {
  if (state.motionRaf) cancelAnimationFrame(state.motionRaf);
  state.motionRaf = null;
}

async function pollTraffic() {
  const view = state.view;
  try {
    const r = await api.getTraffic(view.id);
    state.lastOk = Date.now();
    if (state.view !== view) return; // view changed while awaiting; drop result
    state.store.update(r.aircraft || [], r);
    // Ground aircraft are shown only in local views, not the regional overview.
    const includeGround = view.type === "local";
    state.map.setAircraft(state.store.toGeoJSON({ includeGround })); // render immediately on new data
    const shown = renderAircraftList(includeGround);
    updateFooter(r, shown);
  } catch (e) {
    if (state.view === view) updateFooter({ healthy: false, error: e.message });
  }
}

// ---- satellite loop ---------------------------------------------------------
function setSatCaption(cap, label, time) {
  cap.innerHTML = `${esc(label)}<span class="frame-time">${esc(time)}</span>`;
}

async function setSatelliteView(view) {
  const img = el("sat-img");
  const cap = el("sat-caption");
  img.removeAttribute("src");
  cap.textContent = view.label + " — loading…";
  try {
    const data = await api.getSatellite(view);
    state.lastOk = Date.now();
    if (state.view !== view) return;
    const frames = data.frames || [];
    if (!frames.length) {
      cap.textContent = view.label + " — imagery unavailable";
      updateFooter({ source: "noaa goes", healthy: false, error: data.error }, 0, "frames");
      return;
    }
    // Show the newest frame right away, then animate once all frames are cached.
    // Frames are fetched in parallel (each is several hundred KB); the browser's
    // decoded-image cache makes the src swaps cheap on a fixed set of URLs.
    const latest = frames[frames.length - 1];
    img.src = latest.url;
    setSatCaption(cap, view.label, latest.time);
    updateFooter({ source: "noaa goes", healthy: true }, frames.length, "frames");
    await preloadImages(frames.map((f) => f.url));
    if (state.view === view) animateSatellite(view, frames);
  } catch (e) {
    cap.textContent = view.label + " — imagery unavailable";
    updateFooter({ healthy: false, error: e.message }, 0, "frames");
  }
}

function preloadImages(urls) {
  return Promise.all(
    urls.map(
      (u) =>
        new Promise((res) => {
          const im = new Image();
          im.onload = res;
          im.onerror = res;
          im.src = u;
        })
    )
  );
}

function animateSatellite(view, frames) {
  let i = 0;
  const img = el("sat-img");
  const cap = el("sat-caption");
  const step = () => {
    if (state.view !== view) return;
    const f = frames[i];
    img.src = f.url;
    setSatCaption(cap, view.label, f.time);
    const last = i === frames.length - 1;
    i = (i + 1) % frames.length;
    state.satTimer = setTimeout(step, last ? SAT_HOLD_MS : SAT_FRAME_MS);
  };
  step();
}

// ---- NEXRAD radar loop --------------------------------------------------
// Standard NWS reflectivity color scale (dBZ -> color) for the legend.
const RADAR_DBZ = [
  [5, "#04e9e7"], [15, "#0300f4"], [25, "#02fd02"], [35, "#fdf802"],
  [45, "#fd9500"], [55, "#fd0000"], [65, "#f800fd"], [75, "#fdfdfd"],
];

// NEXRAD precipitation overlay for the regional view. The radar raster sits below
// the wind-barb markers (DOM markers always render above map layers), so barbs
// stay legible on top of the precipitation.
function startRadarOverlay(view) {
  const radar = view.radar;
  if (!radar || !(radar.frames || []).length) return;
  state.map.ensureRadar(radar.frames, { tileBase: radar.tile_base, opacity: radar.opacity });
  state.map.refreshRadar(); // new 5-minute bucket -> reload the frames
  renderRadarLegend();
  showRadarOverlays(true);
  animateRadar(view, radar.frames, radar.label);
}

function animateRadar(view, frames, label) {
  let i = 0;
  const FRAME_MS = 550; // dwell per frame; ~matches the cross-fade so steps dissolve smoothly
  const HOLD_MS = 1800; // pause on the newest frame each loop
  const step = () => {
    if (state.view !== view) return;
    state.map.showRadarFrame(i);
    const f = frames[i];
    const t = new Date(Date.now() - (f.age_min || 0) * 60000);
    const hhmm = state.timeFmt.format(t); // configured display timezone, same as the header clock
    const age = f.age_min ? `-${f.age_min} min` : "now";
    el("radar-caption").innerHTML = `${esc(label)}<span class="frame-time">${esc(hhmm)} (${esc(age)})</span>`;
    const last = i === frames.length - 1;
    i = (i + 1) % frames.length;
    state.satTimer = setTimeout(step, last ? HOLD_MS : FRAME_MS);
  };
  step();
}

function renderRadarLegend() {
  const swatches = RADAR_DBZ.map(([, c]) => `<i style="background:${c}"></i>`).join("");
  const labels = RADAR_DBZ.map(([d]) => `<span>${d}</span>`).join("");
  el("radar-legend").innerHTML =
    `<h4>REFLECTIVITY (dBZ)</h4><div class="scale">${swatches}</div><div class="labels">${labels}</div>`;
}

async function showRegionalWeather(view) {
  try {
    // Use the actual visible map rectangle so every airport on screen gets a barb.
    const r = await api.getBboxWeather(state.map.visibleBbox());
    state.lastOk = Date.now();
    const stations = (r.stations || []).filter((s) => s.lat != null);
    if (state.view !== view) return; // view changed while awaiting
    state.map.setAirportBarbs(stations.map((s) => ({ icao: s.icao, lat: s.lat, lon: s.lon, metar: s })));
    renderStationList(stations);
    updateFooter({ source: "metar", healthy: true }, stations.length, "stations");
  } catch (e) {
    if (state.view !== view) return;
    renderStationList([]);
    updateFooter({ healthy: false, error: e.message }, 0, "stations");
  }
}

function windText(w, spaced) {
  if (w.calm) return "Calm";
  const gust = w.gust_kt ? "G" + w.gust_kt : "";
  return `${w.variable ? "VRB" : pad3(w.dir)}° ${w.speed_kt ?? "--"}${gust}${spaced ? " " : ""}kt`;
}

function renderStationList(stations) {
  // sort worst conditions first: LIFR, IFR, MVFR, VFR, then unknown
  const rank = { LIFR: 0, IFR: 1, MVFR: 2, VFR: 3 };
  const sorted = [...stations].sort(
    (a, b) => (rank[a.category] ?? 9) - (rank[b.category] ?? 9) || String(a.icao).localeCompare(String(b.icao))
  );
  el("ac-count").textContent = sorted.length;
  state.lastListHtml = null;
  el("aircraft-list").innerHTML = sorted
    .map((m) => {
      const color = esc(m.category_color || "#fff");
      const barb = windBarbSVG({
        speedKt: m.wind.speed_kt,
        dirDeg: m.wind.dir,
        variable: m.wind.variable,
        calm: m.wind.calm,
        color: m.category_color,
        fallback: "#ffffff",
        size: 40,
      });
      const cat = m.category ? `<span class="cat" style="background:${color}">${esc(m.category)}</span>` : "";
      return `<div class="st-row">${barb}<span class="icao">${esc(m.icao)}</span>${cat}<span class="wind">${esc(windText(m.wind))}</span></div>`;
    })
    .join("");
}

function renderAircraftList(includeGround = true) {
  const list = state.store.list({ includeGround });
  el("ac-count").textContent = list.length;
  renderAircraftListRows(list);
  return list.length;
}

function renderAircraftListRows(list) {
  const html = list
    .map((a) => {
      const alt = a.onGround ? "GND" : a.altFt != null ? Math.round(a.altFt).toLocaleString() : "—";
      const gs = a.gs ? Math.round(a.gs) + "kt" : "";
      const ga = a.ga ? `<span class="ga-tag">GA</span>` : "";
      return `<div class="ac-row${a.ga ? " ga" : ""}">
        <span class="dot" style="background:${altColor(a.altFt, a.onGround)}"></span>
        <span><span class="cs">${esc(a.label)}</span> ${ga}<span class="ty">${esc(a.type || "")}</span></span>
        <span class="alt">${alt}</span>
        <span class="gs">${gs}</span>
      </div>`;
    })
    .join("");
  if (html === state.lastListHtml) return; // nothing changed since last second; skip the DOM rebuild
  state.lastListHtml = html;
  el("aircraft-list").innerHTML = html;
}

function renderAirportBarbs() {
  const coords = {};
  for (const ap of state.cfg.airports || []) coords[ap.icao.toUpperCase()] = ap;
  const airports = (state.view.airports || [])
    .map((icao) => {
      const ap = coords[icao.toUpperCase()];
      if (!ap) return null;
      return { icao, lat: ap.lat, lon: ap.lon, metar: state.weather[icao.toUpperCase()] };
    })
    .filter(Boolean);
  state.map.setAirportBarbs(airports);
}

// ---- side panel header (weather) --------------------------------------
function renderSideHeader() {
  const c = el("side-header");
  if (!state.view) {
    c.innerHTML = "";
    return;
  }
  if (state.view.type === "local") {
    c.innerHTML = renderLocal(state.view.airports[0]);
  } else {
    c.innerHTML = renderRegional(state.view.airports);
  }
}

function renderLocal(icao) {
  const m = state.weather[(icao || "").toUpperCase()];
  if (!m) return `<div class="metar-block"><div class="wind-text">${esc(icao)}: no weather</div></div>`;
  const color = esc(m.category_color || "#fff");
  const barb = windBarbSVG({
    speedKt: m.wind.speed_kt,
    dirDeg: m.wind.dir,
    variable: m.wind.variable,
    calm: m.wind.calm,
    color: m.category_color,
    fallback: CAT_FALLBACK,
    size: 120,
  });
  const cat = m.category
    ? `<span class="cat-chip" style="background:${color}">${esc(m.category)}</span>`
    : `<span class="cat-chip" style="background:#fff">N/A</span>`;
  const stale = m.stale ? `<span class="stale-badge">STALE WX</span>` : "";
  return `
    <div class="metar-block">
      <div style="display:flex;align-items:center;gap:10px;">
        <div style="font-size:22px;font-weight:700">${esc(m.icao)}</div>${cat}${stale}
      </div>
      <div class="barb-wrap">
        ${barb}
        <div class="wind-text"><div class="big">${esc(windText(m.wind, true))}</div></div>
      </div>
      <div class="metar-grid">
        <div class="k">Visibility</div><div>${esc(fmt(m.visibility_sm, "sm"))}</div>
        <div class="k">Ceiling</div><div>${m.ceiling_ft != null ? esc(m.ceiling_ft) + " ft" : "—"}</div>
        <div class="k">Temp / Dew</div><div>${esc(fmt(m.temp_c, "°C"))} / ${esc(fmt(m.dewpoint_c, "°C"))}</div>
        <div class="k">Altimeter</div><div>${m.altimeter_hpa != null ? hpaToInHg(m.altimeter_hpa) + " inHg" : "—"}</div>
      </div>
      <div class="raw">${esc(m.raw || "")}</div>
    </div>`;
}

function renderRegional(icaos) {
  const rows = (icaos || [])
    .map((icao) => {
      const m = state.weather[icao.toUpperCase()];
      if (!m) return `<div class="ap-row"><span class="icao">${esc(icao)}</span><span class="wind">no wx</span></div>`;
      const color = esc(m.category_color || "#fff");
      const barb = windBarbSVG({
        speedKt: m.wind.speed_kt,
        dirDeg: m.wind.dir,
        variable: m.wind.variable,
        calm: m.wind.calm,
        color: m.category_color,
        fallback: CAT_FALLBACK,
        size: 38,
      });
      const cat = m.category ? `<span class="cat" style="background:${color}">${esc(m.category)}</span>` : "";
      return `<div class="ap-row">${barb}<span class="icao">${esc(m.icao)}</span>${cat}<span class="wind">${esc(windText(m.wind))}</span></div>`;
    })
    .join("");
  return `<div class="ap-list"><h3>AIRPORTS IN REGION</h3>${rows || "<div class='wind'>none</div>"}</div>`;
}

// ---- chrome -----------------------------------------------------------
function animateCountdown(seconds) {
  const fill = el("countdown-fill");
  fill.style.transition = "none";
  fill.style.transform = "scaleX(1)";
  // force reflow then animate to 0
  void fill.offsetWidth;
  fill.style.transition = `transform ${seconds}s linear`;
  fill.style.transform = "scaleX(0)";
}

function fmtAge(s) {
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

// r: a traffic snapshot or a {source, healthy, stale, ts, age_s, error} summary.
// The dot is green when data flows, amber when the last snapshot is stale, red
// when the backend or upstream reported a failure. "updated" shows the data's
// own timestamp, not the time of the poll, so frozen data is visible.
function updateFooter(r, shown, unit = "aircraft") {
  el("f-source").textContent = "source: " + (r.source || state.cfg?.data_source?.mode || "—");
  el("f-count").textContent = (shown ?? r.count ?? state.store.map.size) + " " + unit;
  const h = el("f-health");
  h.classList.toggle("bad", r.healthy === false);
  h.classList.toggle("warn", r.healthy !== false && r.stale === true);
  h.title = r.error || "";
  const upd = el("f-updated");
  if (r.pending) upd.textContent = "waiting for data";
  else if (r.stale && r.age_s != null) upd.textContent = `data ${fmtAge(r.age_s)} old`;
  else if (r.ts) upd.textContent = "updated " + state.timeFmt.format(new Date(r.ts * 1000));
  else upd.textContent = "updated " + state.timeFmt.format(new Date());
}

function startClocks() {
  if (state.clocksStarted) return;
  state.clocksStarted = true;
  let tz = state.cfg?.display?.timezone || "UTC";
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: tz });
  } catch (_) {
    console.warn("invalid timezone in config, using UTC:", tz);
    tz = "UTC";
  }
  state.tz = tz;
  state.timeFmt = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: tz });
  const localFmt = state.timeFmt;
  const tzAbbr = new Intl.DateTimeFormat("en-US", { timeZoneName: "short", timeZone: tz })
    .formatToParts(new Date())
    .find((p) => p.type === "timeZoneName")?.value || "LOCAL";
  const utcFmt = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
  const dateFmt = new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: tz });

  el("clock-local").querySelector(".tz").textContent = tzAbbr;
  const tick = () => {
    const now = new Date();
    el("clock-local").querySelector(".time").textContent = localFmt.format(now);
    el("clock-utc").querySelector(".time").textContent = utcFmt.format(now);
    el("date").textContent = dateFmt.format(now);
  };
  tick();
  setInterval(tick, 1000);
}

function renderLegend(mode = "alt") {
  const sets = {
    alt: [
      "ALT",
      [["Ground", "#9ca3af"], ["<2k", "#e0245e"], ["5k", "#eab308"], ["10k", "#22c55e"], ["20k", "#3b82f6"], ["30k+", "#a855f7"]],
    ],
    cat: [
      "WX",
      [["VFR", "#22c55e"], ["MVFR", "#3b82f6"], ["IFR", "#ef4444"], ["LIFR", "#d946ef"]],
    ],
  };
  const [title, items] = sets[mode] || sets.alt;
  el("legend").innerHTML =
    `<span style="color:#cbd5e1">${title}</span>` +
    items.map(([l, c]) => `<span class="sw"><span class="dot" style="background:${c}"></span>${l}</span>`).join("");
}

// ---- helpers ----------------------------------------------------------
function showOverlay(t) {
  el("overlay-text").textContent = t;
  el("overlay").classList.remove("hidden");
}
function hideOverlay() {
  el("overlay").classList.add("hidden");
}
function pad3(n) {
  return String(n ?? 0).padStart(3, "0");
}
function fmt(v, unit) {
  return v == null ? "—" : `${v}${unit ? " " + unit : ""}`;
}
function hpaToInHg(hpa) {
  return (hpa * 0.02953).toFixed(2);
}

startWatchdog();
bootWithRetry();
