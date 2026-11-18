// Admin form: one field schema drives rendering, collection, and error display.
// Inputs carry data-f (config path) and data-t (declared type); values are
// parsed by the declared type, never by guessing whether a string looks
// numeric. All rendered values are escaped.
import { esc } from "./dom.js";
import { GRID_COLS, streamOptions } from "./streams.js";

const num = (min, max, step = 1, extra = {}) => ({ min, max, step, ...extra });

// A select's options are values, or [value, what it means].
export const FIELDS = {
  airport: [
    { k: "icao", label: "Identifier", type: "text", attrs: { maxlength: 4, pattern: "[A-Za-z0-9]{3,4}", required: true, autocapitalize: "characters", spellcheck: "false", placeholder: "KSEA" } },
    { k: "name", label: "Name", type: "text", attrs: { maxlength: 80 } },
    { k: "lat", label: "Latitude", type: "number", attrs: num(-90, 90, "any", { required: true }) },
    { k: "lon", label: "Longitude", type: "number", attrs: num(-180, 180, "any", { required: true }) },
    { k: "local_radius_mi", label: "Radius", unit: "mi", type: "number", attrs: num(0.5, 100, 0.5, { required: true }) },
    { k: "enabled", label: "Shown", type: "bool" },
  ],
  region: [
    { k: "name", label: "Name", type: "text", attrs: { maxlength: 60, required: true } },
    { k: "center_lat", label: "Center latitude", type: "number", attrs: num(-90, 90, "any", { required: true }) },
    { k: "center_lon", label: "Center longitude", type: "number", attrs: num(-180, 180, "any", { required: true }) },
    { k: "radius_mi", label: "Radius", unit: "mi", type: "number", attrs: num(1, 500, 1, { required: true }) },
    { k: "enabled", label: "Shown", type: "bool" },
  ],
  cycle: [
    { k: "local_dwell_s", label: "Each local view stays up", unit: "s", type: "number", attrs: num(5, 3600, 1, { required: true }) },
    { k: "regional_dwell_s", label: "Each regional view stays up", unit: "s", type: "number", attrs: num(5, 3600, 1, { required: true }) },
    { k: "max_local_views", label: "Most local views per cycle", type: "number", attrs: num(0, 100, 1, { required: true }), help: "0 shows every airport." },
    { k: "interleave_regional", label: "Put a regional view between local views", type: "bool" },
  ],
  data_source: [
    { k: "mode", label: "Aircraft positions come from", type: "select", options: [["auto", "The local receiver, else a public feed"], ["local", "The local receiver only"], ["aggregator", "A public feed only"]] },
    { k: "aggregator", label: "Public feed", type: "select", options: [["adsbfi", "adsb.fi"], ["adsblol", "adsb.lol"], ["airplaneslive", "airplanes.live"]] },
    {
      k: "local_url", label: "Local receiver (its aircraft.json)", type: "text", span: 2,
      attrs: { maxlength: 500, pattern: "https?://.+", spellcheck: "false", placeholder: "http://192.168.1.20/tar1090/data/aircraft.json" },
      help: "The backend runs in Docker, so \"localhost\" is the container, not the Pi. Use the receiver's LAN address.",
    },
    { k: "drop_timeout_s", label: "Remove an aircraft not heard for", unit: "s", type: "number", attrs: num(1, 300, 1, { required: true }) },
  ],
  display: [
    { k: "timezone", label: "Timezone (IANA name)", type: "text", attrs: { list: "tz-list", required: true, maxlength: 64, placeholder: "America/Los_Angeles" } },
    { k: "basemap", label: "Map", type: "select", options: [["raster_osm", "OpenStreetMap tiles"], ["vector", "A vector style (needs its URL)"]] },
    { k: "tile_url", label: "Own tile server or vector style (optional)", type: "text", span: 2, attrs: { maxlength: 500, pattern: "https?://.+", spellcheck: "false" } },
  ],
  weather: [
    { k: "refresh_s", label: "Refresh METARs every", unit: "s", type: "number", attrs: num(60, 3600, 1, { required: true }) },
    { k: "stale_after_s", label: "Mark a report stale after", unit: "s", type: "number", attrs: num(60, 86400, 1, { required: true }) },
  ],
  satellite: [
    { k: "enabled", label: "Show the satellite loop", type: "bool" },
    { k: "label", label: "Heading on the TV", type: "text", attrs: { maxlength: 80 } },
    { k: "sat", label: "Satellite", type: "select", options: ["G16", "G18", "G19"] },
    { k: "sector", label: "Sector code", type: "text", attrs: { maxlength: 32, pattern: "[A-Za-z0-9_-]+", required: true } },
    { k: "band", label: "Band", type: "text", attrs: { maxlength: 32, pattern: "[A-Za-z0-9_-]+", required: true } },
    { k: "size", label: "Frame size", unit: "px", type: "select", options: ["300x300", "600x600", "1200x1200", "2400x2400"], help: "1200x1200 suits a 1080p panel; larger loops use much more browser memory." },
    { k: "frames", label: "Frames in the loop", type: "number", attrs: num(1, 60, 1, { required: true }) },
    { k: "dwell_s", label: "Stays up", unit: "s", type: "number", attrs: num(5, 3600, 1, { required: true }) },
  ],
  radar: [
    { k: "enabled", label: "Show radar on the regional view", type: "bool" },
    { k: "label", label: "Heading on the TV", type: "text", attrs: { maxlength: 80 } },
    { k: "product", label: "Product", type: "select", options: [["n0q", "N0Q (0.5 dBZ steps)"], ["n0r", "N0R (5 dBZ steps)"]] },
    { k: "frames", label: "Frames in the loop", type: "number", attrs: num(1, 12, 1, { required: true }) },
    { k: "interval_min", label: "Time between frames", unit: "min", type: "number", attrs: num(5, 55, 5, { required: true }), help: "The loop can reach back 55 minutes: (frames - 1) x interval must be 55 or less." },
    { k: "opacity", label: "Opacity", type: "range", fmt: "percent", attrs: num(0, 1, 0.05), help: "How strongly the radar shows over the map." },
    { k: "min_dbz", label: "Hide returns below", unit: "dBZ", type: "range", attrs: num(0, 40, 5), help: "Weaker returns are not drawn. Below about 10 dBZ they are mostly not precipitation." },
    { k: "crossfade", label: "Cross-fade frames", type: "bool", span: "full", help: "Dissolves between frames instead of stepping. Off by default: the fade re-renders the whole map and is expensive on a Pi." },
  ],
  cameras: [
    { k: "enabled", label: "Show the camera view", type: "bool" },
    { k: "label", label: "Heading on the TV", type: "text", attrs: { maxlength: 80 } },
    { k: "dwell_s", label: "Stays up", unit: "s", type: "number", attrs: num(5, 3600, 1, { required: true }) },
  ],
  notams: [
    { k: "enabled", label: "Show NOTAM views", type: "bool" },
    { k: "source", label: "NOTAMs come from", type: "select", options: [["faa", "The FAA, live"], ["sample", "Sample data, for a preview"]], help: "Sample NOTAMs are invented and marked as such on the TV." },
    { k: "label", label: "Heading on the TV", type: "text", attrs: { maxlength: 80 } },
    { k: "lookahead_h", label: "Include NOTAMs starting within", unit: "h", type: "number", attrs: num(0, 720, 1, { required: true }), help: "NOTAMs in effect are always shown." },
    { k: "dwell_s", label: "Each page stays up", unit: "s", type: "number", attrs: num(5, 3600, 1, { required: true }) },
    { k: "refresh_s", label: "Look for changes every", unit: "s", type: "number", attrs: num(180, 3600, 1, { required: true }), help: "Production allows one data pull every 180 seconds; the display keeps to that in every environment." },
  ],
  notam_environment: [
    { k: "environment", label: "Environment the display uses", type: "select", options: [["production", "Production"], ["staging", "Staging (test)"], ["fit", "FIT (test)"]], help: "Production allows one data pull every three minutes; staging and FIT allow one request a second. The display keeps to the production pace in all three. The TV marks NOTAMs from a test environment as not for operational use." },
  ],
  notam_translate: [
    { k: "enabled", label: "Rewrite NOTAMs with the model", type: "bool", span: "full", help: "A rewrite is shown only when every number, designator, identifier and word in it checks against the NOTAM. Live NOTAM views need this on: the TV names the model as part of the source." },
    { k: "description", label: "Model, as named on the TV", type: "text", attrs: { maxlength: 60, placeholder: "Qwen 3.8 27B" } },
    { k: "base_url", label: "Endpoint (OpenAI-compatible)", type: "text", attrs: { maxlength: 300, pattern: "https?://.+", spellcheck: "false", placeholder: "https://llm.example.net/v1" } },
    { k: "model", label: "Model name at the endpoint", type: "text", attrs: { maxlength: 120, list: "llm-models", spellcheck: "false" } },
    { k: "timeout_s", label: "Wait for an answer at most", unit: "s", type: "number", attrs: num(5, 600, 1, { required: true }) },
  ],
  notam_location: [
    { k: "ident", label: "Identifier", type: "text", attrs: { maxlength: 4, pattern: "[A-Za-z0-9]{3,4}", required: true, autocapitalize: "characters", spellcheck: "false", placeholder: "KS50" } },
    { k: "name", label: "Name, in the view's heading", type: "text", span: 2, attrs: { maxlength: 80 } },
    { k: "enabled", label: "Shown", type: "bool" },
  ],
  camera_tile: [
    { k: "name", label: "Caption (optional)", type: "text", attrs: { maxlength: 40 } },
  ],
};

// Collections the add / delete / move buttons act on, by dotted config path,
// each with the factory for a new item.
const COLLECTIONS = { airports: newAirport, regions: newRegion, "cameras.tiles": newTile, "notams.locations": newNotamLocation };
const IDENT_RE = /^[A-Z0-9]{3,4}$/;

const fieldId = (path) => "f-" + path.replace(/[^A-Za-z0-9_-]/g, "-");

export function inputHtml(path, f, value) {
  const id = fieldId(path);
  const attrs = Object.entries(f.attrs || {})
    .map(([k, v]) => (v === true ? k : `${k}="${esc(v)}"`))
    .join(" ");
  const help = f.help ? `<small class="help">${esc(f.help)}</small>` : "";
  const unit = (inner) => (f.unit ? `<div class="with-unit">${inner}<span>${esc(f.unit)}</span></div>` : inner);
  const wrap = (inner) => `<div class="${f.span ? "span" + f.span : ""}"><label for="${id}">${esc(f.label)}</label>${unit(inner)}${help}</div>`;
  const common = `id="${id}" data-f="${esc(path)}"`;
  switch (f.type) {
    case "bool":
      return `<div class="${f.span ? "span" + f.span : ""}"><label class="check" for="${id}">` +
        `<input ${common} type="checkbox" data-t="bool" ${value ? "checked" : ""}> ${esc(f.label)}</label>${help}</div>`;
    case "select":
      return wrap(
        `<select ${common} data-t="select">${(f.options || [])
          .map((o) => (Array.isArray(o) ? o : [o, o]))
          .map(([v, text]) => `<option value="${esc(v)}" ${v === value ? "selected" : ""}>${esc(text)}</option>`)
          .join("")}</select>`
      );
    case "number":
      return wrap(`<input ${common} type="number" data-t="number" value="${esc(value ?? "")}" ${attrs}>`);
    case "range":
      return `<div class="${f.span ? "span" + f.span : ""}"><label for="${id}">${esc(f.label)}</label>` +
        `<div class="range"><input ${common} type="range" data-t="number" data-fmt="${esc(f.fmt || "")}" data-unit="${esc(f.unit || "")}" value="${esc(value ?? "")}" ${attrs}>` +
        `<output for="${id}">${esc(rangeText(value, f.fmt, f.unit))}</output></div>${help}</div>`;
    default:
      return wrap(`<input ${common} type="text" data-t="text" value="${esc(value ?? "")}" ${attrs}>`);
  }
}

function rangeText(v, fmt, unit) {
  if (v == null || v === "") return "";
  if (fmt === "percent") return `${Math.round(Number(v) * 100)}%`;
  return unit ? `${v} ${unit}` : String(v);
}

// Keep a slider's readout current; call from a delegated "input" listener.
export function syncRange(inp) {
  if (!inp || inp.type !== "range") return;
  const out = inp.parentElement && inp.parentElement.querySelector("output");
  if (out) out.textContent = rangeText(inp.value, inp.dataset.fmt, inp.dataset.unit);
}

export function fieldsHtml(prefix, spec, obj) {
  return spec.map((f) => inputHtml(`${prefix}.${f.k}`, f, (obj || {})[f.k])).join("");
}

// The move and remove buttons of row i of a collection.
function actionsHtml(path, i, count, what) {
  return `<button type="button" class="btn" data-move="${path}.${i}.-1" ${i === 0 ? "disabled" : ""} aria-label="Move ${esc(what)} up">Up</button>
    <button type="button" class="btn" data-move="${path}.${i}.1" ${i === count - 1 ? "disabled" : ""} aria-label="Move ${esc(what)} down">Down</button>
    <button type="button" class="btn danger" data-del="${path}.${i}" aria-label="Remove ${esc(what)}">Remove</button>`;
}

export function airportRowHtml(a, i, count) {
  return `<div class="item" data-i="${i}">
      <div class="fields">${fieldsHtml(`airports.${i}`, FIELDS.airport, a)}</div>
      <div class="row-actions">${actionsHtml("airports", i, count, a.icao || "this airport")}</div>
    </div>`;
}

export function regionRowHtml(r, i, count) {
  return `<div class="item" data-i="${i}">
      <div class="fields">${fieldsHtml(`regions.${i}`, FIELDS.region, r)}</div>
      <div class="row-actions">${actionsHtml("regions", i, count, r.name || "this region")}</div>
    </div>`;
}

// A tile: the stream it plays (chosen from the stored streams when they are
// known), its caption, and how many of the grid's columns it spans.
export function tileRowHtml(t, i, count, streams) {
  const path = `cameras.tiles.${i}`;
  const id = fieldId(`${path}.stream`);
  const stream = Array.isArray(streams)
    ? `<select id="${id}" data-f="${path}.stream" data-t="select" required>${streamOptions(streams, t.stream || "")}</select>`
    : `<input id="${id}" data-f="${path}.stream" data-t="text" type="text" value="${esc(t.stream ?? "")}" maxlength="64" pattern="[A-Za-z0-9_.-]+" required spellcheck="false" placeholder="cam1">`;
  const cols = fieldId(`${path}.cols`);
  return `<div class="item" data-i="${i}">
      <div class="fields">
        <div><label for="${id}">Stream</label>${stream}</div>
        ${fieldsHtml(path, FIELDS.camera_tile, t)}
        <div><label for="${cols}">Columns, of ${GRID_COLS}</label>
          <div class="with-unit">
            <button type="button" class="btn square" data-step="${path}.cols" data-by="-1" aria-label="Narrower">−</button>
            <input id="${cols}" data-f="${path}.cols" data-t="number" type="number" value="${esc(t.cols ?? "")}" min="1" max="${GRID_COLS}" step="1" required>
            <button type="button" class="btn square" data-step="${path}.cols" data-by="1" aria-label="Wider">+</button>
          </div></div>
      </div>
      <div class="row-actions">${actionsHtml("cameras.tiles", i, count, `tile ${i + 1}`)}</div>
    </div>`;
}

// A monitored location. `detail` is its state from /api/status, when known.
export function notamLocationRowHtml(l, i, count, detail) {
  return `<div class="item" data-i="${i}">
      <div class="fields">${fieldsHtml(`notams.locations.${i}`, FIELDS.notam_location, l)}</div>
      <div class="row-actions">
        <span class="status grow" id="notam-check-${i}" role="status">${esc(locationState(detail))}</span>
        <button type="button" class="btn" data-check-notam="${i}">Refresh now</button>
        ${actionsHtml("notams.locations", i, count, l.ident || "this location")}
      </div>
    </div>`;
}

// What is held for a location: how many NOTAMs, complete through when.
export function locationState(d) {
  if (!d) return "";
  if (d.error) return d.error;
  if (d.unknown) return "The FAA does not know this identifier";
  if (d.in_progress || d.requested) return "Waiting for its turn at the FAA";
  if (!d.synced_ts) return "Not fetched yet";
  const at = new Date(d.synced_ts * 1000).toISOString().slice(11, 16);
  return `${d.count} NOTAM${d.count === 1 ? "" : "s"} held, complete through ${at}Z`;
}

// One button per display airport that is not a NOTAM location yet; a click adds it.
export function notamSuggestHtml(cfg) {
  const have = new Set(((cfg.notams && cfg.notams.locations) || []).map((l) => String(l.ident || "").toUpperCase()));
  return (cfg.airports || [])
    .filter((a) => IDENT_RE.test(String(a.icao || "").toUpperCase()) && !have.has(String(a.icao).toUpperCase()))
    .map((a) => {
      const preset = JSON.stringify({ ident: String(a.icao).toUpperCase(), name: a.name || "" });
      return `<button type="button" class="btn" data-add="notams.locations" data-preset="${esc(preset)}">+ ${esc(a.icao)}</button>`;
    })
    .join(" ");
}

// Render every part of the form into the given roots (a root that is not given
// is skipped). `extra.streams` are the stored camera streams; `extra.locations`
// the state of the monitored NOTAM locations by identifier.
export function renderAll(cfg, roots, extra = {}) {
  const fill = (root, html) => {
    if (root) root.innerHTML = html;
  };
  const airports = cfg.airports || [];
  fill(roots.airports, airports.map((a, i) => airportRowHtml(a, i, airports.length)).join("") ||
    `<div class="empty">No airports yet.</div>`);
  const regions = cfg.regions || [];
  fill(roots.regions, regions.map((r, i) => regionRowHtml(r, i, regions.length)).join("") ||
    `<div class="empty">No regions yet.</div>`);
  fill(roots.cycle, `<div class="fields">${fieldsHtml("cycle", FIELDS.cycle, cfg.cycle)}</div>`);
  fill(roots.datasource, `<div class="fields">${fieldsHtml("data_source", FIELDS.data_source, cfg.data_source)}</div>`);
  fill(roots.display,
    `<div class="fields">${fieldsHtml("display", FIELDS.display, cfg.display)}${fieldsHtml("weather", FIELDS.weather, cfg.weather)}</div>`);
  fill(roots.satellite, `<div class="fields">${fieldsHtml("satellite", FIELDS.satellite, cfg.satellite)}</div>`);
  fill(roots.radar, `<div class="fields">${fieldsHtml("radar", FIELDS.radar, cfg.radar)}</div>`);
  fill(roots.cameras, `<div class="fields">${fieldsHtml("cameras", FIELDS.cameras, cfg.cameras)}</div>`);
  const tiles = (cfg.cameras && cfg.cameras.tiles) || [];
  fill(roots.tiles, tiles.map((t, i) => tileRowHtml(t, i, tiles.length, extra.streams)).join("") ||
    `<div class="empty">No tiles yet.</div>`);
  const notams = cfg.notams || {};
  fill(roots.notams, `<div class="fields">${fieldsHtml("notams", FIELDS.notams, notams)}</div>`);
  fill(roots.notamEnvironment, `<div class="fields">${fieldsHtml("notams", FIELDS.notam_environment, notams)}</div>`);
  fill(roots.notamModel, `<div class="fields">${fieldsHtml("notams.translate", FIELDS.notam_translate, notams.translate || {})}</div>`);
  const locations = notams.locations || [];
  const state = extra.locations || {};
  const suggest = notamSuggestHtml(cfg);
  fill(roots.notamLocations,
    (locations.map((l, i) => notamLocationRowHtml(l, i, locations.length, state[String(l.ident || "").toUpperCase()])).join("") ||
      `<div class="empty">No locations yet.</div>`) +
    (suggest ? `<div class="row-actions" id="notam-suggest"><span class="sub">Your airports:</span> ${suggest}</div>` : ""));
}

// Pull current form values back into cfg using data-f="path.to.field" and the
// declared data-t type.
export function collect(root, cfg) {
  root.querySelectorAll("[data-f]").forEach((inp) => {
    const path = inp.dataset.f.split(".");
    let obj = cfg;
    for (let i = 0; i < path.length - 1; i++) {
      const key = path[i];
      if (obj[key] == null) obj[key] = /^\d+$/.test(path[i + 1]) ? [] : {};
      obj = obj[key];
    }
    const leaf = path[path.length - 1];
    const t = inp.dataset.t;
    let val;
    if (t === "bool") val = inp.checked;
    else if (t === "number") {
      const raw = inp.value.trim();
      val = raw === "" ? null : Number(raw);
    } else val = inp.value.trim();
    obj[leaf] = val;
  });
  return cfg;
}

export function newAirport() {
  return { icao: "", name: "", lat: null, lon: null, local_radius_mi: 5, enabled: true };
}

export function newRegion() {
  return { name: "", center_lat: null, center_lon: null, radius_mi: 30, enabled: true };
}

export function newTile() {
  return { name: "", stream: "", cols: 2 };
}

export function newNotamLocation() {
  return { ident: "", name: "", enabled: true };
}

// Add a NOTAM location for each identifier in `text` (separated by spaces or
// commas). One already monitored is skipped; anything that is not an identifier
// is returned in `rest` for the person to correct.
export function addNotamLocations(cfg, text) {
  const list = collectionAt(cfg, "notams.locations");
  const have = new Set(list.map((l) => String(l.ident || "").toUpperCase()));
  const rest = [];
  let added = 0;
  for (const token of String(text || "").toUpperCase().split(/[\s,;]+/).filter(Boolean)) {
    if (!IDENT_RE.test(token)) rest.push(token);
    else if (!have.has(token)) {
      list.push({ ...newNotamLocation(), ident: token });
      have.add(token);
      added++;
    }
  }
  return { added, rest: rest.join(" ") };
}

// The array at a dotted config path (created if missing).
function collectionAt(cfg, path) {
  const keys = path.split(".");
  let obj = cfg;
  for (let i = 0; i < keys.length - 1; i++) obj = obj[keys[i]] = obj[keys[i]] || {};
  const last = keys[keys.length - 1];
  return (obj[last] = obj[last] || []);
}

// Apply a button action to cfg: data-add="<collection>", data-del="<collection>.<i>",
// data-move="<collection>.<i>.<-1|1>", where <collection> is a dotted path in
// COLLECTIONS. An add button may carry the new item's values as JSON
// (data-preset), or name a text input holding NOTAM location identifiers
// (data-from). Returns true if cfg changed.
export function applyAction(cfg, btn) {
  if (btn.dataset.add) {
    const make = COLLECTIONS[btn.dataset.add];
    if (!make) return false;
    if (btn.dataset.from && btn.dataset.add === "notams.locations") {
      const input = document.getElementById(btn.dataset.from);
      const { added, rest } = addNotamLocations(cfg, input ? input.value : "");
      if (input) input.value = rest;
      return added > 0;
    }
    let preset = {};
    try {
      preset = btn.dataset.preset ? JSON.parse(btn.dataset.preset) : {};
    } catch (_) {}
    collectionAt(cfg, btn.dataset.add).push({ ...make(), ...preset });
    return true;
  }
  if (btn.dataset.del) {
    const parts = btn.dataset.del.split(".");
    const i = +parts.pop();
    const arr = collectionAt(cfg, parts.join("."));
    if (!(i < arr.length)) return false;
    arr.splice(i, 1);
    return true;
  }
  if (btn.dataset.move) {
    const parts = btn.dataset.move.split(".");
    const dir = +parts.pop();
    const from = +parts.pop();
    const arr = collectionAt(cfg, parts.join("."));
    const to = from + dir;
    if (to < 0 || to >= arr.length) return false;
    [arr[from], arr[to]] = [arr[to], arr[from]];
    return true;
  }
  return false;
}

// A step button (data-step="<path of a number field>", data-by="-1|1") moves
// that field by one step within its bounds. Returns the field it changed.
export function applyStep(root, btn) {
  const inp = root.querySelector(`[data-f="${String(btn.dataset.step).replace(/"/g, "")}"]`);
  if (!inp) return null;
  const now = Number(inp.value);
  const min = inp.min === "" ? -Infinity : Number(inp.min);
  const max = inp.max === "" ? Infinity : Number(inp.max);
  const next = Math.min(max, Math.max(min, (Number.isFinite(now) ? now : 0) + Number(btn.dataset.by || 0)));
  if (String(next) === inp.value) return null;
  inp.value = String(next);
  return inp;
}

// ---- validation feedback ----------------------------------------------------
export function clearErrors(root) {
  root.querySelectorAll(".field-error").forEach((e) => e.remove());
  root.querySelectorAll(".invalid").forEach((e) => e.classList.remove("invalid"));
}

function showFieldError(inp, msg) {
  inp.classList.add("invalid");
  const host = inp.closest(".with-unit") || inp.closest("label.check") || inp;
  host.insertAdjacentHTML("afterend", `<div class="field-error">${esc(msg)}</div>`);
}

// Native constraint validation from the input attributes. Returns the number
// of invalid fields (and marks them).
export function validateNative(root) {
  const bad = [...root.querySelectorAll("input[data-f],select[data-f]")].filter((i) => !i.checkValidity());
  for (const i of bad) showFieldError(i, i.validationMessage);
  return bad.length;
}

// Map backend 422 details ({loc, msg}) onto fields. Returns messages that
// could not be attached to a field (shown in the global status).
export function applyErrors(root, detail) {
  const unmatched = [];
  for (const e of detail || []) {
    const loc = (e.loc || []).filter((x) => x !== "body");
    const path = loc.join(".");
    const inp = path ? root.querySelector(`[data-f="${path.replace(/"/g, "")}"]`) : null;
    if (inp) {
      showFieldError(inp, e.msg);
      continue;
    }
    const section = loc.length ? root.querySelector(`[data-section="${loc[0].replace(/"/g, "")}"]`) : null;
    if (section) {
      section.insertAdjacentHTML("afterbegin", `<div class="field-error">${esc(loc.join(" > "))}: ${esc(e.msg)}</div>`);
      continue;
    }
    unmatched.push(e.msg);
  }
  return unmatched;
}
