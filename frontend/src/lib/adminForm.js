// Admin form: one field schema drives rendering, collection, and error display.
// Inputs carry data-f (config path) and data-t (declared type); values are
// parsed by the declared type, never by guessing whether a string looks
// numeric. All rendered values are escaped.
import { esc } from "./dom.js";

const num = (min, max, step = 1, extra = {}) => ({ min, max, step, ...extra });

export const FIELDS = {
  airport: [
    { k: "icao", label: "ICAO", type: "text", span: 2, attrs: { maxlength: 4, pattern: "[A-Za-z0-9]{3,4}", required: true, autocapitalize: "characters", spellcheck: "false", placeholder: "KSEA" } },
    { k: "name", label: "Name", type: "text", span: 3, attrs: { maxlength: 80 } },
    { k: "lat", label: "Lat", type: "number", span: 2, attrs: num(-90, 90, "any", { required: true }) },
    { k: "lon", label: "Lon", type: "number", span: 2, attrs: num(-180, 180, "any", { required: true }) },
    { k: "local_radius_mi", label: "Radius mi", type: "number", span: 2, attrs: num(0.5, 100, 0.5, { required: true }) },
    { k: "enabled", label: "On", type: "bool", span: 1 },
  ],
  region: [
    { k: "name", label: "Name", type: "text", span: 4, attrs: { maxlength: 60, required: true } },
    { k: "center_lat", label: "Center lat", type: "number", span: 3, attrs: num(-90, 90, "any", { required: true }) },
    { k: "center_lon", label: "Center lon", type: "number", span: 3, attrs: num(-180, 180, "any", { required: true }) },
    { k: "radius_mi", label: "Radius mi", type: "number", span: 2, attrs: num(1, 500, 1, { required: true }) },
  ],
  cycle: [
    { k: "local_dwell_s", label: "Local dwell (s)", type: "number", attrs: num(5, 3600, 1, { required: true }) },
    { k: "regional_dwell_s", label: "Regional dwell (s)", type: "number", attrs: num(5, 3600, 1, { required: true }) },
    { k: "max_local_views", label: "Max local views (0 = all)", type: "number", attrs: num(0, 100, 1, { required: true }) },
    { k: "interleave_regional", label: "Interleave regional", type: "bool" },
  ],
  data_source: [
    { k: "mode", label: "Mode", type: "select", options: ["local", "aggregator", "auto"] },
    { k: "aggregator", label: "Aggregator", type: "select", options: ["adsbfi", "adsblol", "airplaneslive"] },
    {
      k: "local_url", label: "Local URL (tar1090 aircraft.json)", type: "text", span: 2,
      attrs: { maxlength: 500, pattern: "https?://.+", placeholder: "http://192.168.1.20/tar1090/data/aircraft.json" },
      help: "The backend runs in Docker, so \"localhost\" is the container, not the Pi. Use the receiver's LAN address.",
    },
    { k: "api_key", label: "API key (airplanes.live, optional)", type: "secret", attrs: { maxlength: 200 }, help: "Stored keys are never shown. Leave the mask to keep the current key; clear the field to remove it." },
    { k: "drop_timeout_s", label: "Drop timeout (s)", type: "number", attrs: num(1, 300, 1, { required: true }), help: "Aircraft whose position is older than this are removed." },
  ],
  display: [
    { k: "timezone", label: "Timezone (IANA)", type: "text", attrs: { list: "tz-list", required: true, maxlength: 64, placeholder: "America/Los_Angeles" } },
    { k: "basemap", label: "Basemap", type: "select", options: ["raster_osm", "vector"] },
    { k: "tile_url", label: "Tile URL (vector style JSON, or raster tile template)", type: "text", span: 2, attrs: { maxlength: 500, pattern: "https?://.+" } },
  ],
  weather: [
    { k: "refresh_s", label: "Weather refresh (s)", type: "number", attrs: num(60, 3600, 1, { required: true }) },
    { k: "stale_after_s", label: "Stale after (s)", type: "number", attrs: num(60, 86400, 1, { required: true }) },
  ],
  satellite: [
    { k: "enabled", label: "Enabled", type: "bool" },
    { k: "label", label: "Label", type: "text", attrs: { maxlength: 80 } },
    { k: "sat", label: "Satellite", type: "select", options: ["G16", "G18", "G19"] },
    { k: "sector", label: "Sector code", type: "text", attrs: { maxlength: 32, pattern: "[A-Za-z0-9_-]+", required: true } },
    { k: "band", label: "Band", type: "text", attrs: { maxlength: 32, pattern: "[A-Za-z0-9_-]+", required: true } },
    { k: "size", label: "Size", type: "select", options: ["300x300", "600x600", "1200x1200", "2400x2400"], help: "1200x1200 suits a 1080p panel; larger loops use much more browser memory." },
    { k: "frames", label: "Frames", type: "number", attrs: num(1, 60, 1, { required: true }) },
    { k: "dwell_s", label: "Dwell (s)", type: "number", attrs: num(5, 3600, 1, { required: true }) },
  ],
  radar: [
    { k: "enabled", label: "Enabled", type: "bool" },
    { k: "label", label: "Label", type: "text", span: 2, attrs: { maxlength: 80 } },
    { k: "product", label: "Product", type: "select", options: ["n0q", "n0r"] },
    { k: "frames", label: "Frames", type: "number", attrs: num(1, 12, 1, { required: true }) },
    { k: "interval_min", label: "Interval (min)", type: "number", attrs: num(5, 55, 5, { required: true }), help: "IEM only serves lag layers up to 55 min: (frames - 1) x interval must be 55 or less." },
    { k: "opacity", label: "Opacity", type: "range", fmt: "percent", attrs: num(0, 1, 0.05), help: "How strongly the radar shows over the map." },
    { k: "min_dbz", label: "Hide returns below (dBZ)", type: "range", attrs: num(0, 40, 5), help: "Weaker returns are not drawn. Below about 10 dBZ they are mostly not precipitation." },
    { k: "crossfade", label: "Cross-fade frames", type: "bool", help: "Dissolves between frames instead of stepping. Off by default: the fade re-renders the whole map and is expensive on a Pi." },
  ],
  cameras: [
    { k: "enabled", label: "Enabled", type: "bool" },
    { k: "label", label: "Label", type: "text", attrs: { maxlength: 80 } },
    { k: "dwell_s", label: "Dwell (s)", type: "number", attrs: num(5, 3600, 1, { required: true }) },
  ],
  notams: [
    { k: "enabled", label: "Enabled", type: "bool" },
    { k: "source", label: "Source", type: "select", options: ["faa", "sample"], help: "faa: live NOTAMs from the FAA (needs the credentials in .env). sample: invented NOTAMs, marked as such on screen, for previewing the view." },
    { k: "label", label: "Label", type: "text", attrs: { maxlength: 80 } },
    { k: "lookahead_h", label: "Show NOTAMs starting within (h)", type: "number", attrs: num(0, 720, 1, { required: true }), help: "NOTAMs in effect are always shown; this adds the ones that take effect within this many hours." },
    { k: "dwell_s", label: "Seconds per page", type: "number", attrs: num(5, 3600, 1, { required: true }), help: "A location with more NOTAMs than fit on one page stays up this long for each page." },
    { k: "refresh_s", label: "Refresh (s)", type: "number", attrs: num(120, 3600, 1, { required: true }) },
  ],
  notam_translate: [
    { k: "enabled", label: "Rewrite NOTAMs with a local model", type: "bool", help: "The model's sentence is shown only when every number, designator, identifier and word in it checks against the NOTAM and the FAA's own wording. Any other answer is discarded and the FAA terms spelled out stay on screen." },
    { k: "base_url", label: "Model endpoint (OpenAI-compatible URL)", type: "text", attrs: { maxlength: 300, pattern: "https?://.+", spellcheck: "false", placeholder: "https://llm.example.net/v1" }, help: "A LiteLLM or vLLM server. Its API key goes in .env as HANGAR_LLM_API_KEY." },
    { k: "model", label: "Model name", type: "text", attrs: { maxlength: 120, list: "llm-models", spellcheck: "false" } },
    { k: "timeout_s", label: "Model timeout (s)", type: "number", attrs: num(5, 600, 1, { required: true }) },
  ],
  notam_location: [
    { k: "ident", label: "Identifier", type: "text", span: 2, attrs: { maxlength: 4, pattern: "[A-Za-z0-9]{3,4}", required: true, autocapitalize: "characters", spellcheck: "false", placeholder: "KS50" } },
    { k: "name", label: "Name (shown in the view's heading)", type: "text", span: 4, attrs: { maxlength: 80 } },
    { k: "enabled", label: "On", type: "bool", span: 1 },
  ],
  camera_tile: [
    { k: "name", label: "Caption", type: "text", span: 4, attrs: { maxlength: 40 } },
    { k: "stream", label: "Stream (its name in go2rtc.yaml)", type: "text", span: 4, attrs: { maxlength: 64, pattern: "[A-Za-z0-9_.-]+", required: true, spellcheck: "false", placeholder: "cam1" } },
    { k: "cols", label: "Columns (of 6)", type: "number", span: 2, attrs: num(1, 6, 1, { required: true }) },
  ],
};

// Collections the add / delete / move buttons act on, by dotted config path,
// each with the factory for a new item.
const COLLECTIONS = { airports: newAirport, regions: newRegion, "cameras.tiles": newTile, "notams.locations": newNotamLocation };
const IDENT_RE = /^[A-Z0-9]{3,4}$/;

export function inputHtml(path, f, value) {
  const id = "f-" + path.replace(/[^A-Za-z0-9_-]/g, "-");
  const attrs = Object.entries(f.attrs || {})
    .map(([k, v]) => (v === true ? k : `${k}="${esc(v)}"`))
    .join(" ");
  const help = f.help ? `<small class="help">${esc(f.help)}</small>` : "";
  const wrap = (inner) => `<div class="${f.span ? "span" + f.span : ""}"><label for="${id}">${esc(f.label)}</label>${inner}${help}</div>`;
  const common = `id="${id}" data-f="${esc(path)}"`;
  switch (f.type) {
    case "bool":
      return wrap(`<input ${common} type="checkbox" data-t="bool" ${value ? "checked" : ""}>`);
    case "select":
      return wrap(
        `<select ${common} data-t="select">${(f.options || [])
          .map((o) => `<option value="${esc(o)}" ${o === value ? "selected" : ""}>${esc(o)}</option>`)
          .join("")}</select>`
      );
    case "number":
      return wrap(`<input ${common} type="number" data-t="number" value="${esc(value ?? "")}" ${attrs}>`);
    case "range":
      return wrap(
        `<div class="range"><input ${common} type="range" data-t="number" data-fmt="${esc(f.fmt || "")}" value="${esc(value ?? "")}" ${attrs}>` +
          `<output for="${id}">${esc(rangeText(value, f.fmt))}</output></div>`
      );
    case "secret":
      return wrap(
        `<input ${common} type="password" autocomplete="new-password" data-t="text" value="${esc(value ?? "")}" placeholder="${value ? "(set)" : "(none)"}" ${attrs}>`
      );
    default:
      return wrap(`<input ${common} type="text" data-t="text" value="${esc(value ?? "")}" ${attrs}>`);
  }
}

function rangeText(v, fmt) {
  if (v == null || v === "") return "";
  return fmt === "percent" ? `${Math.round(Number(v) * 100)}%` : String(v);
}

// Keep a slider's readout current; call from a delegated "input" listener.
export function syncRange(inp) {
  if (!inp || inp.type !== "range") return;
  const out = inp.parentElement && inp.parentElement.querySelector("output");
  if (out) out.textContent = rangeText(inp.value, inp.dataset.fmt);
}

export function fieldsHtml(prefix, spec, obj) {
  return spec.map((f) => inputHtml(`${prefix}.${f.k}`, f, (obj || {})[f.k])).join("");
}

export function airportRowHtml(a, i, count) {
  return `
    <div class="row" data-i="${i}">
      ${fieldsHtml(`airports.${i}`, FIELDS.airport, a)}
      <div class="row-actions">
        <button type="button" class="btn sm" data-move="airports.${i}.-1" ${i === 0 ? "disabled" : ""} title="move up">↑</button>
        <button type="button" class="btn sm" data-move="airports.${i}.1" ${i === count - 1 ? "disabled" : ""} title="move down">↓</button>
        <button type="button" class="btn sm danger" data-del="airports.${i}">delete</button>
      </div>
    </div>`;
}

export function regionRowHtml(r, i, count) {
  return `
    <div class="row" data-i="${i}">
      ${fieldsHtml(`regions.${i}`, FIELDS.region, r)}
      <div class="row-actions">
        <label class="inline"><input type="checkbox" data-f="regions.${i}.enabled" data-t="bool" ${r.enabled ? "checked" : ""}> enabled</label>
        <button type="button" class="btn sm" data-move="regions.${i}.-1" ${i === 0 ? "disabled" : ""} title="move up">↑</button>
        <button type="button" class="btn sm" data-move="regions.${i}.1" ${i === count - 1 ? "disabled" : ""} title="move down">↓</button>
        <button type="button" class="btn sm danger" data-del="regions.${i}">delete</button>
      </div>
    </div>`;
}

export function tileRowHtml(t, i, count) {
  return `
    <div class="row" data-i="${i}">
      ${fieldsHtml(`cameras.tiles.${i}`, FIELDS.camera_tile, t)}
      <div class="row-actions">
        <button type="button" class="btn sm" data-move="cameras.tiles.${i}.-1" ${i === 0 ? "disabled" : ""} title="move up">↑</button>
        <button type="button" class="btn sm" data-move="cameras.tiles.${i}.1" ${i === count - 1 ? "disabled" : ""} title="move down">↓</button>
        <button type="button" class="btn sm danger" data-del="cameras.tiles.${i}">delete</button>
      </div>
    </div>`;
}

export function notamLocationRowHtml(l, i, count) {
  return `
    <div class="row" data-i="${i}">
      ${fieldsHtml(`notams.locations.${i}`, FIELDS.notam_location, l)}
      <div class="row-actions">
        <span class="status" id="notam-check-${i}" style="margin-right:auto"></span>
        <button type="button" class="btn sm" data-check-notam="${i}" title="ask the source for this location now">Check</button>
        <button type="button" class="btn sm" data-move="notams.locations.${i}.-1" ${i === 0 ? "disabled" : ""} title="move up">↑</button>
        <button type="button" class="btn sm" data-move="notams.locations.${i}.1" ${i === count - 1 ? "disabled" : ""} title="move down">↓</button>
        <button type="button" class="btn sm danger" data-del="notams.locations.${i}">delete</button>
      </div>
    </div>`;
}

// One button per display airport that is not a NOTAM location yet; a click adds it.
export function notamSuggestHtml(cfg) {
  const have = new Set(((cfg.notams && cfg.notams.locations) || []).map((l) => String(l.ident || "").toUpperCase()));
  return (cfg.airports || [])
    .filter((a) => IDENT_RE.test(String(a.icao || "").toUpperCase()) && !have.has(String(a.icao).toUpperCase()))
    .map((a) => {
      const preset = JSON.stringify({ ident: String(a.icao).toUpperCase(), name: a.name || "" });
      return `<button type="button" class="btn sm" data-add="notams.locations" data-preset="${esc(preset)}">+ ${esc(a.icao)}</button>`;
    })
    .join(" ");
}

// Render every section of the form into the given roots.
export function renderAll(cfg, roots) {
  const airports = cfg.airports || [];
  roots.airports.innerHTML = airports.map((a, i) => airportRowHtml(a, i, airports.length)).join("") ||
    `<div class="empty">No airports yet.</div>`;
  const regions = cfg.regions || [];
  roots.regions.innerHTML = regions.map((r, i) => regionRowHtml(r, i, regions.length)).join("") ||
    `<div class="empty">No regions yet.</div>`;
  roots.cycle.innerHTML = `<div class="fields">${fieldsHtml("cycle", FIELDS.cycle, cfg.cycle)}</div>`;
  roots.datasource.innerHTML = `<div class="fields">${fieldsHtml("data_source", FIELDS.data_source, cfg.data_source)}</div>`;
  roots.display.innerHTML =
    `<div class="fields">${fieldsHtml("display", FIELDS.display, cfg.display)}${fieldsHtml("weather", FIELDS.weather, cfg.weather)}</div>`;
  roots.satellite.innerHTML = `<div class="fields">${fieldsHtml("satellite", FIELDS.satellite, cfg.satellite)}</div>`;
  roots.radar.innerHTML = `<div class="fields">${fieldsHtml("radar", FIELDS.radar, cfg.radar)}</div>`;
  const tiles = (cfg.cameras && cfg.cameras.tiles) || [];
  roots.cameras.innerHTML =
    `<div class="fields">${fieldsHtml("cameras", FIELDS.cameras, cfg.cameras)}</div>` +
    `<div class="tiles">${tiles.map((t, i) => tileRowHtml(t, i, tiles.length)).join("") || `<div class="empty">No tiles yet.</div>`}</div>`;
  if (roots.notams) {
    const locations = (cfg.notams && cfg.notams.locations) || [];
    const suggest = notamSuggestHtml(cfg);
    roots.notams.innerHTML =
      `<div class="fields">${fieldsHtml("notams", FIELDS.notams, cfg.notams)}` +
      `${fieldsHtml("notams.translate", FIELDS.notam_translate, (cfg.notams && cfg.notams.translate) || {})}</div>` +
      `<div class="row-actions" style="justify-content:flex-start;margin:8px 0 12px">` +
      `<button type="button" class="btn sm" data-test-model="1">Test model</button>` +
      `<span class="status" id="model-test-result"></span></div>` +
      `<div class="tiles">${locations.map((l, i) => notamLocationRowHtml(l, i, locations.length)).join("") || `<div class="empty">No locations yet.</div>`}</div>` +
      (suggest ? `<div class="row-actions" style="justify-content:flex-start">Your airports: ${suggest}</div>` : "");
  }
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

// ---- validation feedback ----------------------------------------------------
export function clearErrors(root) {
  root.querySelectorAll(".field-error").forEach((e) => e.remove());
  root.querySelectorAll(".invalid").forEach((e) => e.classList.remove("invalid"));
}

function showFieldError(inp, msg) {
  inp.classList.add("invalid");
  inp.insertAdjacentHTML("afterend", `<div class="field-error">${esc(msg)}</div>`);
}

// Native constraint validation from the input attributes. Returns the number
// of invalid fields (and marks them).
export function validateNative(root) {
  const bad = [...root.querySelectorAll("input,select")].filter((i) => !i.checkValidity());
  for (const i of bad) showFieldError(i, i.validationMessage);
  if (bad.length) bad[0].focus();
  return bad.length;
}

// Map backend 422 details ({loc, msg}) onto fields. Returns messages that
// could not be attached to a field (shown in the global status).
export function applyErrors(root, detail) {
  const unmatched = [];
  let first = null;
  for (const e of detail || []) {
    const loc = (e.loc || []).filter((x) => x !== "body");
    const path = loc.join(".");
    const inp = path ? root.querySelector(`[data-f="${path.replace(/"/g, "")}"]`) : null;
    if (inp) {
      showFieldError(inp, e.msg);
      first = first || inp;
      continue;
    }
    const section = loc.length ? root.querySelector(`[data-section="${loc[0].replace(/"/g, "")}"]`) : null;
    if (section) {
      section.insertAdjacentHTML("afterbegin", `<div class="field-error">${esc(loc.join(" > "))}: ${esc(e.msg)}</div>`);
      first = first || section;
      continue;
    }
    unmatched.push(e.msg);
  }
  if (first && first.scrollIntoView) first.scrollIntoView({ block: "center", behavior: "smooth" });
  return unmatched;
}
