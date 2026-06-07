// Thin REST client for the backend API.
async function j(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}

export const api = {
  getConfig: () => j("/api/config"),
  putConfig: (cfg) =>
    j("/api/config", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(cfg),
    }),
  getViews: () => j("/api/views"),
  getTraffic: (viewId) => j(`/api/traffic?view=${encodeURIComponent(viewId)}`),
  getWeather: (ids) => j(`/api/weather?ids=${encodeURIComponent((ids || []).join(","))}`),
  getAreaWeather: (lat, lon, radiusNm) =>
    j(`/api/weather/area?lat=${lat}&lon=${lon}&radius_nm=${radiusNm}`),
  getBboxWeather: ({ minLat, minLon, maxLat, maxLon }) =>
    j(`/api/weather/bbox?min_lat=${minLat}&min_lon=${minLon}&max_lat=${maxLat}&max_lon=${maxLon}`),
  getSatellite: (v) =>
    j(`/api/satellite?sat=${encodeURIComponent(v.sat)}&sector=${encodeURIComponent(v.sector)}&band=${encodeURIComponent(v.band)}&size=${encodeURIComponent(v.size)}&frames=${v.frames}`),
  getStatus: () => j("/api/status"),
  testSource: (payload) =>
    j("/api/test-source", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    }),
};

// SSE for config_changed / weather_updated events.
export function subscribeEvents(onEvent) {
  const es = new EventSource("/api/stream");
  es.onmessage = (e) => {
    try {
      onEvent(JSON.parse(e.data));
    } catch (_) {}
  };
  es.onerror = () => {}; // EventSource auto-reconnects
  return es;
}
