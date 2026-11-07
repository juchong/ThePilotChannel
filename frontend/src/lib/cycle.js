// Cycle position helpers for the display: wrap an index into the view list and
// resolve a remote navigation request (/api/display/next, /previous, /view) to
// the index it selects. The display owns its cycle; the backend only numbers
// the requests.

// A request older than this when it reaches the display (after a reconnect)
// was meant for a screen that has since moved on, and is dropped.
export const NAV_MAX_AGE_S = 15;

export function wrapIndex(idx, count) {
  if (!count) return 0;
  return ((idx % count) + count) % count;
}

// The index `nav` selects from the current position, or null when the request
// is not to be applied: not newer than the last one applied, too old, naming a
// view that is not in the cycle, or no cycle yet. A view id that appears more
// than once resolves to its next occurrence from the current position, so the
// cycle keeps moving forward.
export function navTarget(nav, { idx, views, lastSeq }) {
  if (!nav || typeof nav.seq !== "number" || !views || !views.length) return null;
  if (lastSeq != null && nav.seq <= lastSeq) return null;
  if (nav.age_s != null && nav.age_s > NAV_MAX_AGE_S) return null;
  const n = views.length;
  if (nav.action === "next") return wrapIndex(idx + 1, n);
  if (nav.action === "previous") return wrapIndex(idx - 1, n);
  if (nav.action === "view") {
    for (let k = 0; k < n; k++) {
      const i = wrapIndex(idx + k, n);
      if (views[i].id === nav.view) return i;
    }
  }
  return null;
}
