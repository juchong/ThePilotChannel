// Camera streams on the admin page: the list of stored streams, the result of
// a test, the choices of a tile, and the TV's six-column grid as a picture.
// A stream's address never reaches the browser again once stored: only its host.
import { esc } from "./dom.js";

export const GRID_COLS = 6;
export const HARDWARE_VIDEO = "H265"; // what the Pi 5 decodes in hardware

// What a test of a stream found, as status text and its class.
export function testText(result) {
  if (!result) return { text: "", cls: "" };
  if (!result.ok) return { text: result.error || "The camera did not answer", cls: "err" };
  if (String(result.video).toUpperCase() !== HARDWARE_VIDEO) {
    return { text: `Answering with ${result.video} video; the Pi decodes only H.265 in hardware`, cls: "err" };
  }
  return { text: "Answering with H.265 video", cls: "ok" };
}

function statePill(stream) {
  if (stream.pending) return `<span class="pill warn">Waiting for the relay</span>`;
  const n = (stream.tiles || []).length;
  return n ? `<span class="pill good">On ${n} tile${n === 1 ? "" : "s"}</span>` : `<span class="pill">On no tile</span>`;
}

export function streamsHtml(streams) {
  if (!(streams || []).length) return `<div class="empty">No streams yet. Add the first one below.</div>`;
  return streams
    .map((s) => {
      const id = esc(s.id);
      return `<div class="item" data-stream="${id}">
        <div class="row-actions">
          <span class="name grow">${esc(s.name)}</span>
          <span class="mono sub">${esc(s.host)}</span>
          ${statePill(s)}
        </div>
        <div class="row-actions">
          <button type="button" class="btn" data-stream-test="${id}">Test</button>
          <button type="button" class="btn" data-stream-edit="${id}">Rename or replace address</button>
          <button type="button" class="btn danger" data-stream-remove="${id}" aria-label="Remove the stream ${esc(s.name)}">Remove</button>
          <span class="status" id="stream-status-${id}" role="status"></span>
        </div>
        <div class="row end" id="stream-edit-${id}" hidden>
          <div style="flex: 0 1 200px"><label for="stream-name-${id}">Name</label>
            <input id="stream-name-${id}" value="${esc(s.name)}" maxlength="40"></div>
          <div style="flex: 1 1 280px"><label for="stream-url-${id}">New address (leave empty to keep the stored one)</label>
            <input id="stream-url-${id}" maxlength="500" spellcheck="false" autocapitalize="none" inputmode="url"
              placeholder="rtsps://192.168.1.1:7441/AbCdEfGhIjKl"></div>
          <button type="button" class="btn primary" data-stream-save="${id}">Store</button>
        </div>
      </div>`;
    })
    .join("");
}

// The <option>s of a tile's stream choice. A tile naming a stream that is not
// stored keeps its value, marked, so saving never silently changes it.
export function streamOptions(streams, current) {
  const known = (streams || []).some((s) => s.id === current);
  const extra = current && !known ? `<option value="${esc(current)}" selected>${esc(current)} (no such stream)</option>` : "";
  const blank = current ? "" : `<option value="" selected>Choose a stream</option>`;
  return (
    blank +
    extra +
    (streams || [])
      .map((s) => `<option value="${esc(s.id)}" ${s.id === current ? "selected" : ""}>${esc(s.name)}</option>`)
      .join("")
  );
}

// The tiles as the TV lays them out: six columns, each tile as wide as its span.
export function layoutHtml(tiles, streams) {
  const names = Object.fromEntries((streams || []).map((s) => [s.id, s.name]));
  if (!(tiles || []).length) return `<div class="empty">No tiles yet.</div>`;
  const said = tiles
    .map((t, i) => `tile ${i + 1}, ${t.name || names[t.stream] || t.stream || "no stream"}, ${spanOf(t)} of ${GRID_COLS} columns`)
    .join("; ");
  return (
    `<div class="tilegrid" role="img" aria-label="${esc("The camera grid on the TV: " + said)}">` +
    tiles
      .map((t) => {
        const stream = names[t.stream] || (t.stream ? `${t.stream} (no such stream)` : "no stream");
        const caption = t.name || names[t.stream] || t.stream || "";
        const under = stream === caption ? "" : `<small>${esc(stream)}</small>`;   // the stream, where the caption is not its name
        return `<div class="tile" style="grid-column: span ${spanOf(t)}"><span>${esc(caption)}</span>${under}</div>`;
      })
      .join("") +
    `</div>`
  );
}

// The columns a tile spans: its own number within the grid, two when it has none.
function spanOf(tile) {
  const n = tile.cols == null || tile.cols === "" ? NaN : Math.round(Number(tile.cols));
  return Math.min(GRID_COLS, Math.max(1, Number.isFinite(n) ? n : 2));
}
