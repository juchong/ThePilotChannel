// Rows of the regional view's station list. Every row has the same parts in the same
// order (wind icon, identifier, category chip, wind), so a station of unknown category
// shows the UNKNOWN chip in the chip's place and its wind stays in the wind column.
import { esc } from "./dom.js";
import { categoryChip, windBarbSVG, windText } from "./windbarb.js";

// One station of the list.
export function stationRow(m) {
  const w = m.wind;
  const barb = windBarbSVG({ speedKt: w.speed_kt, dirDeg: w.dir, variable: w.variable, calm: w.calm, color: m.category_color, size: 40 });
  return `<div class="st-row">${barb}<span class="icao">${esc(m.icao)}</span>${categoryChip(m, "cat")}<span class="wind">${esc(windText(w))}</span></div>`;
}
