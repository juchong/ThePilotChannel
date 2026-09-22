// Small DOM helpers shared by the display and the admin page.
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

// Escape a value for interpolation into innerHTML. Everything that comes from
// the network (aircraft callsigns, METAR text, station names, config labels)
// goes through this before it is rendered.
export function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
}

export const el = (id) => document.getElementById(id);

export const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
