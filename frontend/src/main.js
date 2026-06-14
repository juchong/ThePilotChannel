import { api, subscribeEvents } from "./lib/api.js";
import { AircraftStore } from "./lib/aircraft.js";
import { ALT_STOPS, HangarMap } from "./lib/map.js";
import { windBarbSVG } from "./lib/windbarb.js";

const el = (id) => document.getElementById(id);
const CAT_FALLBACK = "#ffffff";

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
  views: [],
  idx: 0,
  view: null,
  store: new AircraftStore(15),
  weather: {},
  trafficTimer: null,
  dwellTimer: null,
  satTimer: null,
  map: null,
};

async function boot() {
  showOverlay("Loading…");
  state.cfg = await api.getConfig();
  state.store.setDropTimeout(state.cfg?.data_source?.drop_timeout_s);
  state.map = new HangarMap(el("map"), {
    basemap: state.cfg?.display?.basemap,
    tileUrl: state.cfg?.display?.tile_url,
  });
  await state.map.ready;

  renderLegend();
  startClocks();
  subscribeEvents(onServerEvent);

  await loadWeather();
  await loadViews();
  await state.map.warmViews(state.views);
  hideOverlay();
  nextView(0);
}

async function loadViews() {
  const r = await api.getViews();
  state.views = r.views || [];
}

async function loadWeather() {
  try {
    const ids = (state.cfg.airports || []).map((a) => a.icao);
    const r = await api.getWeather(ids);
    state.weather = r.metars || {};
  } catch (_) {}
}

function onServerEvent(msg) {
  if (msg.event === "config_changed") {
    location.reload();
  } else if (msg.event === "weather_updated") {
    loadWeather().then(() => {
      if (!state.view) return;
      if (state.view.type === "local") {
        renderSideHeader();
        renderAirportBarbs();
      } else {
        showRegionalWeather(state.view);
      }
    });
  }
}

// ---- cycle ------------------------------------------------------------
function nextView(idx) {
  if (!state.views.length) {
    showOverlay("No views configured. Open /admin to add airports.");
    return;
  }
  state.idx = idx % state.views.length;
  state.view = state.views[state.idx];
  state.store.clear();
  state.map.clearAircraft();
  state.map.hideRadar();
  showRadarOverlays(false);
  if (state.trafficTimer) clearInterval(state.trafficTimer);
  if (state.satTimer) clearTimeout(state.satTimer);

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
    pollTraffic();
    state.trafficTimer = setInterval(pollTraffic, 1000);
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

  animateCountdown(state.view.dwell_s);
  if (state.dwellTimer) clearTimeout(state.dwellTimer);
  state.dwellTimer = setTimeout(() => nextView(state.idx + 1), state.view.dwell_s * 1000);
}

// Toggle the stage between layouts: "map" (map + side panel) and "sat"
// (fullscreen satellite loop).
function showStage(mode) {
  el("sat").classList.toggle("active", mode === "sat");
  el("map").style.display = mode === "sat" ? "none" : "";
  el("side").style.display = mode === "map" ? "flex" : "none";
}

function showRadarOverlays(on) {
  el("radar-caption").style.display = on ? "block" : "none";
  el("radar-legend").style.display = on ? "block" : "none";
}

async function setSatelliteView(view) {
  const img = el("sat-img");
  const cap = el("sat-caption");
  img.removeAttribute("src");
  cap.textContent = view.label + " — loading…";
  try {
    const data = await api.getSatellite(view);
    if (state.view !== view) return;
    const frames = data.frames || [];
    if (!frames.length) {
      cap.textContent = view.label + " — imagery unavailable";
      updateFooter({ source: "noaa goes", healthy: false }, 0, "frames");
      return;
    }
    // Show the newest frame right away, then animate once all frames are cached.
    const latest = frames[frames.length - 1];
    img.src = latest.url;
    cap.innerHTML = `${view.label}<span class="frame-time">${latest.time}</span>`;
    updateFooter({ source: "noaa goes", healthy: true }, frames.length, "frames");
    preloadImages(frames.map((f) => f.url)).then(() => {
      if (state.view === view) animateSatellite(view, frames);
    });
  } catch (e) {
    cap.textContent = view.label + " — imagery unavailable";
    updateFooter({ healthy: false }, 0, "frames");
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
  const FRAME_MS = 140;
  const HOLD_MS = 1600; // pause on the newest frame each loop
  const step = () => {
    if (state.view !== view) return;
    const f = frames[i];
    img.src = f.url;
    cap.innerHTML = `${view.label}<span class="frame-time">${f.time}</span>`;
    const last = i === frames.length - 1;
    i = (i + 1) % frames.length;
    state.satTimer = setTimeout(step, last ? HOLD_MS : FRAME_MS);
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
    const hhmm = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const age = f.age_min ? `-${f.age_min} min` : "now";
    el("radar-caption").innerHTML = `${label}<span class="frame-time">${hhmm} (${age})</span>`;
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
    const stations = (r.stations || []).filter((s) => s.lat != null);
    if (state.view !== view) return; // view changed while awaiting
    state.map.setAirportBarbs(stations.map((s) => ({ icao: s.icao, lat: s.lat, lon: s.lon, metar: s })));
    renderStationList(stations);
    updateFooter({ source: "metar", healthy: true }, stations.length, "stations");
  } catch (e) {
    renderStationList([]);
    updateFooter({ healthy: false }, 0, "stations");
  }
}

function renderStationList(stations) {
  // sort worst conditions first: LIFR, IFR, MVFR, VFR, then unknown
  const rank = { LIFR: 0, IFR: 1, MVFR: 2, VFR: 3 };
  const sorted = [...stations].sort((a, b) => (rank[a.category] ?? 9) - (rank[b.category] ?? 9));
  el("ac-count").textContent = sorted.length;
  el("aircraft-list").innerHTML = sorted
    .map((m) => {
      const color = m.category_color || "#fff";
      const barb = windBarbSVG({
        speedKt: m.wind.speed_kt,
        dirDeg: m.wind.dir,
        variable: m.wind.variable,
        calm: m.wind.calm,
        color: m.category_color,
        fallback: "#ffffff",
        size: 40,
      });
      const wind = m.wind.calm
        ? "Calm"
        : `${m.wind.variable ? "VRB" : pad3(m.wind.dir)}° ${m.wind.speed_kt}${m.wind.gust_kt ? "G" + m.wind.gust_kt : ""}kt`;
      const cat = m.category ? `<span class="cat" style="background:${color}">${m.category}</span>` : "";
      return `<div class="st-row">${barb}<span class="icao">${m.icao}</span>${cat}<span class="wind">${wind}</span></div>`;
    })
    .join("");
}

async function pollTraffic() {
  const view = state.view;
  try {
    const r = await api.getTraffic(view.id);
    if (state.view !== view) return; // view changed while awaiting; drop result
    state.store.update(r.aircraft || []);
    // Ground aircraft are shown only in local views, not the regional overview.
    const includeGround = state.view.type === "local";
    state.map.setAircraft(state.store.toGeoJSON({ includeGround })); // render immediately on new data
    const shown = renderAircraftList(includeGround);
    updateFooter(r, shown);
  } catch (e) {
    updateFooter({ healthy: false });
  }
}

function renderAircraftList(includeGround = true) {
  const list = state.store.list({ includeGround });
  el("ac-count").textContent = list.length;
  renderAircraftListRows(list);
  return list.length;
}

function renderAircraftListRows(list) {
  el("aircraft-list").innerHTML = list
    .map((a) => {
      const alt = a.onGround ? "GND" : a.altFt != null ? Math.round(a.altFt).toLocaleString() : "—";
      const gs = a.gs ? Math.round(a.gs) + "kt" : "";
      const ga = a.ga ? `<span class="ga-tag">GA</span>` : "";
      return `<div class="ac-row${a.ga ? " ga" : ""}">
        <span class="dot" style="background:${altColor(a.altFt, a.onGround)}"></span>
        <span><span class="cs">${a.label}</span> ${ga}<span class="ty">${a.type || ""}</span></span>
        <span class="alt">${alt}</span>
        <span class="gs">${gs}</span>
      </div>`;
    })
    .join("");
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
  if (!m) return `<div class="metar-block"><div class="wind-text">${icao}: no weather</div></div>`;
  const color = m.category_color;
  const barb = windBarbSVG({
    speedKt: m.wind.speed_kt,
    dirDeg: m.wind.dir,
    variable: m.wind.variable,
    calm: m.wind.calm,
    color,
    fallback: CAT_FALLBACK,
    size: 120,
  });
  const windStr = m.wind.calm
    ? "Calm"
    : `${m.wind.variable ? "VRB" : pad3(m.wind.dir)}° ${m.wind.speed_kt}${m.wind.gust_kt ? "G" + m.wind.gust_kt : ""} kt`;
  const cat = m.category
    ? `<span class="cat-chip" style="background:${color || "#fff"}">${m.category}</span>`
    : `<span class="cat-chip" style="background:#fff">N/A</span>`;
  const stale = m.stale ? `<span class="stale-badge">STALE WX</span>` : "";
  return `
    <div class="metar-block">
      <div style="display:flex;align-items:center;gap:10px;">
        <div style="font-size:22px;font-weight:700">${m.icao}</div>${cat}${stale}
      </div>
      <div class="barb-wrap">
        ${barb}
        <div class="wind-text"><div class="big">${windStr}</div></div>
      </div>
      <div class="metar-grid">
        <div class="k">Visibility</div><div>${fmt(m.visibility_sm, "sm")}</div>
        <div class="k">Ceiling</div><div>${m.ceiling_ft != null ? m.ceiling_ft + " ft" : "—"}</div>
        <div class="k">Temp / Dew</div><div>${fmt(m.temp_c, "°C")} / ${fmt(m.dewpoint_c, "°C")}</div>
        <div class="k">Altimeter</div><div>${m.altimeter_hpa != null ? hpaToInHg(m.altimeter_hpa) + " inHg" : "—"}</div>
      </div>
      <div class="raw">${m.raw || ""}</div>
    </div>`;
}

function renderRegional(icaos) {
  const rows = (icaos || [])
    .map((icao) => {
      const m = state.weather[icao.toUpperCase()];
      if (!m) return `<div class="ap-row"><span class="icao">${icao}</span><span class="wind">no wx</span></div>`;
      const color = m.category_color;
      const barb = windBarbSVG({
        speedKt: m.wind.speed_kt,
        dirDeg: m.wind.dir,
        variable: m.wind.variable,
        calm: m.wind.calm,
        color,
        fallback: CAT_FALLBACK,
        size: 38,
      });
      const windStr = m.wind.calm ? "Calm" : `${m.wind.variable ? "VRB" : pad3(m.wind.dir)}° ${m.wind.speed_kt}kt`;
      const cat = m.category ? `<span class="cat" style="background:${color || "#fff"}">${m.category}</span>` : "";
      return `<div class="ap-row">${barb}<span class="icao">${m.icao}</span>${cat}<span class="wind">${windStr}</span></div>`;
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

function updateFooter(r, shown, unit = "aircraft") {
  el("f-source").textContent = "source: " + (r.source || state.cfg?.data_source?.mode || "—");
  el("f-count").textContent = (shown ?? r.count ?? state.store.map.size) + " " + unit;
  el("f-updated").textContent = "updated " + new Date().toLocaleTimeString();
  const h = el("f-health");
  if (r.healthy === false) h.classList.add("bad");
  else h.classList.remove("bad");
}

function startClocks() {
  const tz = state.cfg?.display?.timezone || "UTC";
  const localFmt = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: tz });
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

boot().catch((e) => showOverlay("Error: " + e.message));
