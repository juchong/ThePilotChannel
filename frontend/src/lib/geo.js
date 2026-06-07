// Client-side geo helpers for dead reckoning and view framing.
const EARTH_NM = 3440.065;

// Move a point by distance (nm) along a true bearing (deg). Returns [lat, lon].
export function movePoint(lat, lon, bearingDeg, distNm) {
  const d = distNm / EARTH_NM;
  const brg = (bearingDeg * Math.PI) / 180;
  const lat1 = (lat * Math.PI) / 180;
  const lon1 = (lon * Math.PI) / 180;
  const lat2 = Math.asin(Math.sin(lat1) * Math.cos(d) + Math.cos(lat1) * Math.sin(d) * Math.cos(brg));
  const lon2 =
    lon1 + Math.atan2(Math.sin(brg) * Math.sin(d) * Math.cos(lat1), Math.cos(d) - Math.sin(lat1) * Math.sin(lat2));
  return [(lat2 * 180) / Math.PI, (((lon2 * 180) / Math.PI + 540) % 360) - 180];
}

// Bounding box [west, south, east, north] for a center + radius (nm).
export function bboxForRadius(lat, lon, radiusNm) {
  const dLat = radiusNm / 60; // 1 deg lat ~ 60 nm
  const dLon = radiusNm / (60 * Math.cos((lat * Math.PI) / 180) || 1e-6);
  return [lon - dLon, lat - dLat, lon + dLon, lat + dLat];
}
