// Thin REST client for the backend API. Every request carries a timeout so a
// stalled backend cannot leave requests piling up on the kiosk.
const DEFAULT_TIMEOUT_MS = 8000;

export class ApiError extends Error {
  constructor(url, status, detail, retryAfter = null) {
    super(`${url} -> ${status}`);
    this.status = status;
    this.detail = detail; // parsed JSON "detail" from the backend, if any
    this.retryAfter = retryAfter; // seconds, from a Retry-After header
  }
}

async function j(url, opts = {}, timeoutMs = DEFAULT_TIMEOUT_MS, read = (r) => r.json()) {
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
      throw new ApiError(url, r.status, detail, Number(r.headers.get("retry-after")) || null);
    }
    return await read(r);
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
  getNotams: (location) => j(`/api/notams?location=${encodeURIComponent(location)}`),
  checkNotamLocation: (ident, source) => j("/api/notams/check", json("POST", { ident, source }), 30000),
  testNotamModel: (body) => j("/api/notams/model-test", json("POST", body), 150000),
  getStatus: () => j("/api/status"),
  health: () => j("/healthz", {}, 4000),
  // remote display control
  getDisplay: () => j("/api/display"),
  blackout: (body) => j("/api/display/blackout", json("POST", body || {})),
  restoreDisplay: () => j("/api/display/restore", { method: "POST" }),
  nextView: () => j("/api/display/next", { method: "POST" }),
  previousView: () => j("/api/display/previous", { method: "POST" }),
  showView: (viewId) => j("/api/display/view", json("POST", { view: viewId })),
  // the display's own report of the view it has switched to
  reportShowing: (body) => j("/api/display/showing", json("POST", body), 4000),
  testSource: (payload) => j("/api/test-source", json("POST", payload), 30000),
  // signing in and the account
  getSession: () => j("/api/auth/session"),
  login: (username, password) => j("/api/auth/login", json("POST", { username, password }), 15000),
  setup: (username, password) => j("/api/auth/setup", json("POST", { username, password }), 15000),
  logout: () => j("/api/auth/logout", { method: "POST" }),
  logoutAll: () => j("/api/auth/logout-all", { method: "POST" }),
  updateAccount: (body) => j("/api/auth/account", json("PUT", body), 15000),
  // API keys
  getKeys: () => j("/api/keys"),
  createKey: (name, scope) => j("/api/keys", json("POST", { name, scope })),
  revokeKey: (id) => j(`/api/keys/${encodeURIComponent(id)}`, { method: "DELETE" }),
  // stored credentials: state only, never a value
  getSecrets: () => j("/api/secrets"),
  putNmsKeys: (body) => j("/api/secrets/nms", json("PUT", body)),
  putSecret: (name, value) => j(`/api/secrets/${encodeURIComponent(name)}`, json("PUT", { value })),
  deleteSecret: (name) => j(`/api/secrets/${encodeURIComponent(name)}`, { method: "DELETE" }),
  checkNmsKeys: () => j("/api/secrets/nms/check", { method: "POST" }, 45000),
  // camera stream addresses
  getStreams: () => j("/api/cameras/streams"),
  addStream: (body) => j("/api/cameras/streams", json("POST", body)),
  changeStream: (id, body) => j(`/api/cameras/streams/${encodeURIComponent(id)}`, json("PUT", body)),
  removeStream: (id) => j(`/api/cameras/streams/${encodeURIComponent(id)}`, { method: "DELETE" }),
  testStream: (body) => j("/api/cameras/streams/test", json("POST", body), 40000),
};

// SSE for connected / config_changed / weather_updated / notams_updated / display events. EventSource
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
