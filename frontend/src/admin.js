import { api } from "./lib/api.js";

const el = (id) => document.getElementById(id);
let cfg = null;

async function boot() {
  cfg = await api.getConfig();
  renderAll();
  el("save").addEventListener("click", save);
  el("test-source").addEventListener("click", testSource);
  document.querySelectorAll("[data-add]").forEach((b) =>
    b.addEventListener("click", () => addRow(b.dataset.add))
  );
}

function renderAll() {
  renderAirports();
  renderRegions();
  renderCycle();
  renderDataSource();
  renderDisplay();
  renderSatellite();
  renderRadar();
}

function renderAirports() {
  el("airports").innerHTML = (cfg.airports || [])
    .map(
      (a, i) => `
    <div class="row" data-i="${i}">
      <div class="span2"><label>ICAO</label><input data-f="airports.${i}.icao" value="${a.icao || ""}"></div>
      <div class="span3"><label>Name</label><input data-f="airports.${i}.name" value="${a.name || ""}"></div>
      <div class="span2"><label>Lat</label><input data-f="airports.${i}.lat" value="${a.lat ?? ""}"></div>
      <div class="span2"><label>Lon</label><input data-f="airports.${i}.lon" value="${a.lon ?? ""}"></div>
      <div class="span2"><label>Radius mi</label><input data-f="airports.${i}.local_radius_mi" value="${a.local_radius_mi ?? 8}"></div>
      <div><label>On</label><input type="checkbox" data-f="airports.${i}.enabled" ${a.enabled ? "checked" : ""}></div>
      <div style="grid-column:span 12;display:flex;gap:6px;justify-content:flex-end">
        <button class="btn sm" data-move="airports.${i}.-1">↑</button>
        <button class="btn sm" data-move="airports.${i}.1">↓</button>
        <button class="btn sm danger" data-del="airports.${i}">delete</button>
      </div>
    </div>`
    )
    .join("");
  bindRowControls();
}

function renderRegions() {
  el("regions").innerHTML = (cfg.regions || [])
    .map(
      (r, i) => `
    <div class="row" data-i="${i}">
      <div class="span4"><label>Name</label><input data-f="regions.${i}.name" value="${r.name || ""}"></div>
      <div class="span3"><label>Center lat</label><input data-f="regions.${i}.center_lat" value="${r.center_lat ?? ""}"></div>
      <div class="span3"><label>Center lon</label><input data-f="regions.${i}.center_lon" value="${r.center_lon ?? ""}"></div>
      <div class="span2"><label>Radius mi</label><input data-f="regions.${i}.radius_mi" value="${r.radius_mi ?? 50}"></div>
      <div style="grid-column:span 12;display:flex;gap:6px;justify-content:flex-end">
        <label style="display:flex;gap:5px;align-items:center;margin:0"><input type="checkbox" data-f="regions.${i}.enabled" ${r.enabled ? "checked" : ""}> enabled</label>
        <button class="btn sm danger" data-del="regions.${i}">delete</button>
      </div>
    </div>`
    )
    .join("");
  bindRowControls();
}

function renderCycle() {
  const c = cfg.cycle || {};
  el("cycle").innerHTML = `
    <div class="fields">
      <div><label>Local dwell (s)</label><input data-f="cycle.local_dwell_s" value="${c.local_dwell_s ?? 20}"></div>
      <div><label>Regional dwell (s)</label><input data-f="cycle.regional_dwell_s" value="${c.regional_dwell_s ?? 25}"></div>
      <div><label>Max local views (0=all)</label><input data-f="cycle.max_local_views" value="${c.max_local_views ?? 0}"></div>
      <div><label>Interleave regional</label><input type="checkbox" data-f="cycle.interleave_regional" ${c.interleave_regional ? "checked" : ""}></div>
    </div>`;
}

function renderDataSource() {
  const d = cfg.data_source || {};
  el("datasource").innerHTML = `
    <div class="fields">
      <div><label>Mode</label><select data-f="data_source.mode">
        ${opt(["local", "aggregator", "auto"], d.mode)}</select></div>
      <div><label>Aggregator</label><select data-f="data_source.aggregator">
        ${opt(["adsbfi", "adsblol", "airplaneslive"], d.aggregator)}</select></div>
      <div style="grid-column:span 2"><label>Local URL (tar1090 aircraft.json)</label><input data-f="data_source.local_url" value="${d.local_url || ""}"></div>
      <div><label>API key (optional)</label><input data-f="data_source.api_key" value="${d.api_key || ""}"></div>
      <div><label>Drop timeout (s)</label><input data-f="data_source.drop_timeout_s" value="${d.drop_timeout_s ?? 15}"></div>
    </div>`;
}

function renderDisplay() {
  const d = cfg.display || {};
  const w = cfg.weather || {};
  el("display").innerHTML = `
    <div class="fields">
      <div><label>Units</label><select data-f="display.units">${opt(["imperial", "metric"], d.units)}</select></div>
      <div><label>Timezone</label><input data-f="display.timezone" value="${d.timezone || "UTC"}"></div>
      <div><label>Resolution</label><input data-f="display.resolution" value="${d.resolution || "1920x1080"}"></div>
      <div><label>Basemap</label><select data-f="display.basemap">${opt(["raster_osm", "vector"], d.basemap)}</select></div>
      <div style="grid-column:span 2"><label>Tile URL (vector style / raster override)</label><input data-f="display.tile_url" value="${d.tile_url || ""}"></div>
      <div><label>Weather refresh (s)</label><input data-f="weather.refresh_s" value="${w.refresh_s ?? 300}"></div>
      <div><label>Stale after (s)</label><input data-f="weather.stale_after_s" value="${w.stale_after_s ?? 4500}"></div>
    </div>`;
}

function renderSatellite() {
  const s = cfg.satellite || {};
  el("satellite").innerHTML = `
    <div class="fields">
      <div><label>Enabled</label><input type="checkbox" data-f="satellite.enabled" ${s.enabled ? "checked" : ""}></div>
      <div><label>Label</label><input data-f="satellite.label" value="${s.label || ""}"></div>
      <div><label>Satellite</label><select data-f="satellite.sat">${opt(["G16", "G18", "G19"], s.sat)}</select></div>
      <div><label>Sector code</label><input data-f="satellite.sector" value="${s.sector || ""}"></div>
      <div><label>Band</label><input data-f="satellite.band" value="${s.band || ""}"></div>
      <div><label>Size</label><select data-f="satellite.size">${opt(["300x300", "600x600", "1200x1200", "2400x2400"], s.size)}</select></div>
      <div><label>Frames</label><input data-f="satellite.frames" value="${s.frames ?? 24}"></div>
      <div><label>Dwell (s)</label><input data-f="satellite.dwell_s" value="${s.dwell_s ?? 25}"></div>
    </div>`;
}

function renderRadar() {
  const r = cfg.radar || {};
  el("radar").innerHTML = `
    <div class="fields">
      <div><label>Enabled</label><input type="checkbox" data-f="radar.enabled" ${r.enabled ? "checked" : ""}></div>
      <div style="grid-column:span 2"><label>Label</label><input data-f="radar.label" value="${r.label || ""}"></div>
      <div><label>Product</label><input data-f="radar.product" value="${r.product || "n0q"}"></div>
      <div><label>Frames</label><input data-f="radar.frames" value="${r.frames ?? 10}"></div>
      <div><label>Interval (min)</label><input data-f="radar.interval_min" value="${r.interval_min ?? 5}"></div>
      <div><label>Opacity (0-1)</label><input data-f="radar.opacity" value="${r.opacity ?? 0.75}"></div>
    </div>`;
}

function opt(values, sel) {
  return values.map((v) => `<option value="${v}" ${v === sel ? "selected" : ""}>${v}</option>`).join("");
}

function bindRowControls() {
  document.querySelectorAll("[data-del]").forEach((b) =>
    b.addEventListener("click", () => {
      const [coll, i] = b.dataset.del.split(".");
      cfg[coll].splice(+i, 1);
      renderAll();
    })
  );
  document.querySelectorAll("[data-move]").forEach((b) =>
    b.addEventListener("click", () => {
      const [coll, i, dir] = b.dataset.move.split(".");
      const arr = cfg[coll];
      const from = +i;
      const to = from + +dir;
      if (to < 0 || to >= arr.length) return;
      [arr[from], arr[to]] = [arr[to], arr[from]];
      renderAll();
    })
  );
}

function addRow(kind) {
  if (kind === "airport") {
    cfg.airports = cfg.airports || [];
    cfg.airports.push({ icao: "", name: "", lat: 0, lon: 0, local_radius_mi: 8, enabled: true });
    renderAirports();
  } else {
    cfg.regions = cfg.regions || [];
    cfg.regions.push({ name: "", center_lat: 0, center_lon: 0, radius_mi: 50, enabled: true });
    renderRegions();
  }
}

// Pull current form values back into cfg using data-f="path.to.field".
function collect() {
  document.querySelectorAll("[data-f]").forEach((inp) => {
    const path = inp.dataset.f.split(".");
    let obj = cfg;
    for (let i = 0; i < path.length - 1; i++) {
      const key = isNaN(path[i]) ? path[i] : +path[i];
      obj = obj[key];
    }
    const leaf = path[path.length - 1];
    let val;
    if (inp.type === "checkbox") val = inp.checked;
    else if (inp.tagName === "SELECT") val = inp.value;
    else {
      const raw = inp.value.trim();
      val = raw !== "" && !isNaN(raw) ? Number(raw) : raw;
    }
    obj[leaf] = val;
  });
}

async function save() {
  collect();
  setStatus("status", "saving…");
  try {
    await api.putConfig(cfg);
    setStatus("status", "saved ✓", "ok");
    cfg = await api.getConfig();
    renderAll();
  } catch (e) {
    setStatus("status", "error: " + e.message, "err");
  }
}

async function testSource() {
  collect();
  setStatus("test-result", "testing…");
  try {
    const r = await api.testSource(cfg.data_source);
    setStatus("test-result", r.ok ? `ok: ${r.source} (${r.count} ac)` : `fail: ${r.error}`, r.ok ? "ok" : "err");
  } catch (e) {
    setStatus("test-result", "error: " + e.message, "err");
  }
}

function setStatus(id, text, cls = "") {
  const s = el(id);
  s.textContent = text;
  s.className = "status " + cls;
}

boot().catch((e) => setStatus("status", "load error: " + e.message, "err"));
