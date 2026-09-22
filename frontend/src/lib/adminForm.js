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
    { k: "opacity", label: "Opacity (0-1)", type: "number", attrs: num(0, 1, 0.05, { required: true }) },
  ],
};

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
    case "secret":
      return wrap(
        `<input ${common} type="password" autocomplete="new-password" data-t="text" value="${esc(value ?? "")}" placeholder="${value ? "(set)" : "(none)"}" ${attrs}>`
      );
    default:
      return wrap(`<input ${common} type="text" data-t="text" value="${esc(value ?? "")}" ${attrs}>`);
  }
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

// Apply a button action (data-add / data-del / data-move) to cfg. Returns true
// if cfg changed.
export function applyAction(cfg, btn) {
  if (btn.dataset.add) {
    const coll = btn.dataset.add === "airport" ? "airports" : "regions";
    cfg[coll] = cfg[coll] || [];
    cfg[coll].push(btn.dataset.add === "airport" ? newAirport() : newRegion());
    return true;
  }
  if (btn.dataset.del) {
    const [coll, i] = btn.dataset.del.split(".");
    if (!cfg[coll] || +i >= cfg[coll].length) return false;
    cfg[coll].splice(+i, 1);
    return true;
  }
  if (btn.dataset.move) {
    const [coll, i, dir] = btn.dataset.move.split(".");
    const arr = cfg[coll] || [];
    const from = +i;
    const to = from + +dir;
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
