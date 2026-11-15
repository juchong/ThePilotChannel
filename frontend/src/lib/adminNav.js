// The admin's pages: which there are, which config sections each one edits,
// which page an address names, and which pages hold unsaved or refused values.
import { esc } from "./dom.js";

export const PAGES = [
  { id: "overview", title: "Overview", sections: [] },
  { id: "airports", title: "Airports", group: "Views", sections: ["airports"] },
  { id: "regions", title: "Regions and radar", group: "Views", sections: ["regions", "radar"] },
  { id: "satellite", title: "Satellite", group: "Views", sections: ["satellite"] },
  { id: "cameras", title: "Cameras", group: "Views", sections: ["cameras"] },
  { id: "notams", title: "NOTAMs", group: "Views", sections: ["notams"] },
  { id: "rotation", title: "Rotation", group: "Views", sections: ["cycle"] },
  { id: "traffic", title: "Traffic source", group: "System", sections: ["data_source"] },
  { id: "display", title: "Display", group: "System", sections: ["display", "weather"] },
  { id: "access", title: "Access", group: "System", sections: [] },
  { id: "api", title: "Display API", group: "System", sections: [] },
];

const BY_ID = Object.fromEntries(PAGES.map((p) => [p.id, p]));

// "#/cameras" -> "cameras"; anything else is the overview.
export function pageFromHash(hash) {
  const m = /^#\/([a-z]+)$/.exec(hash || "");
  return m && BY_ID[m[1]] ? m[1] : "overview";
}

// The page that edits a config section ("radar" -> "regions"), or null.
export function pageOfSection(section) {
  const page = PAGES.find((p) => p.sections.includes(section));
  return page ? page.id : null;
}

// A value as text that does not depend on the order its keys were set in.
function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${stable(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value === undefined ? null : value);
}

// The pages on which the config being edited differs from the saved one.
export function changedPages(saved, current) {
  return PAGES.filter((p) => p.sections.some((s) => stable((saved || {})[s]) !== stable((current || {})[s]))).map((p) => p.id);
}

// The pages the backend's refusal ({loc, msg} entries) points at.
export function errorPages(detail) {
  const out = [];
  for (const e of detail || []) {
    const page = pageOfSection((e.loc || []).filter((x) => x !== "body")[0]);
    if (page && !out.includes(page)) out.push(page);
  }
  return out;
}

function listed(ids) {
  const names = ids.map((id) => BY_ID[id].title);
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

// What the save bar says: "Unsaved changes on Airports and Rotation".
export function changedText(ids) {
  return ids.length ? `Unsaved changes on ${listed(ids)}` : "";
}

export function navHtml(current, unsaved = [], invalid = []) {
  let group = null;
  let out = "";
  for (const p of PAGES) {
    if (p.group && p.group !== group) out += `<div class="grp">${esc(p.group)}</div>`;
    group = p.group || group;
    const mark = invalid.includes(p.id)
      ? `<span class="dot invalid" role="img" aria-label="has a value to correct"></span>`
      : unsaved.includes(p.id)
        ? `<span class="dot unsaved" role="img" aria-label="has unsaved changes"></span>`
        : "";
    const here = p.id === current;
    out += `<a href="#/${p.id}" class="${here ? "on" : ""}" ${here ? 'aria-current="page"' : ""}>${esc(p.title)}${mark}</a>`;
  }
  return out;
}
