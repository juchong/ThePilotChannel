// Thin REST client for the backend API. Every request carries a timeout so a
// stalled backend cannot leave requests piling up on the kiosk.
const DEFAULT_TIMEOUT_MS = 8000;

export class ApiError extends Error {
  constructor(url, status, detail) {
    super(`${url} -> ${status}`);
    this.status = status;
    this.detail = detail; // parsed JSON "detail" from the backend, if any
  }
}

async function j(url, opts = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    // Resolve against the origin (never the document URL): a document URL that
    // carries basic-auth credentials cannot be used to build a Request.
    const r = await fetch(new URL(url, location.origin).href, { cache: "no-store", ...opts, signal: ctl.signal });
    if (!r.ok) {
      let detail = null;
      try {
        detail = (await r.json()).detail;
      } catch (_) {}
      throw new ApiError(url, r.status, detail);
    }
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

const json = (method, body, extraHeaders = {}) => ({
  method,
  headers: { "content-type": "application/json", ...extraHeaders },
  body: JSON.stringify(body),
});

export const api = {
  getConfig: () => j("/api/config"),
  // version: the config version returned by getConfig; sent as If-Match so a
  // save made from a stale page is rejected (409) instead of clobbering.
  putConfig: (cfg, version) =>
    j("/api/config", json("PUT", cfg, version ? { "if-match": `"${version}"` } : {}), 15000),
  getViews: () => j("/api/views"),
  getTraffic: (viewId) => j(`/api/traffic?view=${encodeURIComponent(viewId)}`, {}, 5000),
  getWeather: (ids) => j(`/api/weather?ids=${encodeURIComponent((ids || []).join(","))}`),
  getAreaWeather: (lat, lon, radiusNm) =>
    j(`/api/weather/area?lat=${lat}&lon=${lon}&radius_nm=${radiusNm}`, {}, 15000),
  getBboxWeather: ({ minLat, minLon, maxLat, maxLon }) =>
    j(`/api/weather/bbox?min_lat=${minLat}&min_lon=${minLon}&max_lat=${maxLat}&max_lon=${maxLon}`, {}, 15000),
  getSatellite: (v) =>
    j(
      `/api/satellite?sat=${encodeURIComponent(v.sat)}&sector=${encodeURIComponent(v.sector)}&band=${encodeURIComponent(v.band)}&size=${encodeURIComponent(v.size)}&frames=${encodeURIComponent(v.frames)}`,
      {},
      20000
    ),
  getStatus: () => j("/api/status"),
  health: () => j("/healthz", {}, 4000),
  // remote display control
  getDisplay: () => j("/api/display"),
  blackout: (body) => j("/api/display/blackout", json("POST", body || {})),
  restoreDisplay: () => j("/api/display/restore", { method: "POST" }),
  testSource: (payload) => j("/api/test-source", json("POST", payload), 30000),
};

// SSE for connected / config_changed / weather_updated events. EventSource
// reconnects on its own; each (re)connection delivers a `connected` event that
// carries the backend boot id, so the caller can detect a rebuilt backend.
export function subscribeEvents(onEvent) {
  const es = new EventSource(new URL("/api/stream", location.origin).href);
  es.onmessage = (e) => {
    try {
      onEvent(JSON.parse(e.data));
    } catch (_) {}
  };
  es.onerror = () => {};
  return es;
}
