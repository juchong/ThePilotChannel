// What the regional view shows after a station-set response. The backend
// answers from its cache and reports a failing background refresh through
// `error` and `stale`; the map keeps the last known stations and only the
// footer changes.

// The station list to display: the fresh set if it has any stations, else the
// last set shown, else the (empty) fresh set.
export function nextStations(last, r) {
  const fresh = ((r && r.stations) || []).filter((s) => s.lat != null);
  if (fresh.length) return fresh;
  return last && last.length ? last : fresh;
}

// Footer summary for a station response, or for a failed request (failure is
// the error message). A failed request keeps whatever is shown and reports it
// as stale and unhealthy.
export function stationsFooter(r, failure) {
  if (failure) return { source: "metar", healthy: false, stale: true, error: failure };
  return { source: "metar", healthy: !r.error, stale: !!r.stale, age_s: r.age_s, error: r.error || null };
}
