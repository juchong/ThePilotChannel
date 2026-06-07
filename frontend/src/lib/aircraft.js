// Aircraft store keyed by hex. Holds the latest authoritative position only.
// Smooth motion is handled by a CSS transform transition on the marker element
// (see map.js) - the browser animates between successive 1 Hz positions, which
// is monotonic and never overshoots or reverses.
import { classify, isGA } from "./shapes.js";

export class AircraftStore {
  constructor(dropTimeoutS = 15) {
    this.map = new Map();
    this.dropTimeoutS = dropTimeoutS;
  }

  setDropTimeout(s) {
    if (s) this.dropTimeoutS = s;
  }

  update(list) {
    const now = performance.now();
    const seen = new Set();
    for (const ac of list) {
      if (ac.lat == null || ac.lon == null) continue;
      const altFt = ac.on_ground ? 0 : ac.alt_ft;
      // Negative reported altitude is treated as on-the-ground/invalid and dropped.
      if (typeof altFt === "number" && altFt < 0) continue;
      seen.add(ac.hex);
      this.map.set(ac.hex, {
        hex: ac.hex,
        label: (ac.callsign || ac.registration || ac.hex || "").trim(),
        type: ac.type,
        iconType: classify(ac.category, ac.type),
        track: ac.track ?? 0,
        gs: ac.gs ?? 0,
        altFt,
        onGround: !!ac.on_ground,
        ga: isGA(ac.category, ac.type),
        lat: ac.lat,
        lon: ac.lon,
        lastSeen: now,
      });
    }
    for (const [hex, a] of this.map) {
      if (!seen.has(hex) && now - a.lastSeen > this.dropTimeoutS * 1000) {
        this.map.delete(hex);
      }
    }
  }

  toGeoJSON({ includeGround = true } = {}) {
    const features = [];
    for (const a of this.map.values()) {
      if (!includeGround && a.onGround) continue;
      features.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: [a.lon, a.lat] },
        properties: {
          hex: a.hex,
          icon: a.iconType,
          track: a.track || 0,
          altFt: a.altFt == null ? -1 : a.altFt,
          onGround: a.onGround ? 1 : 0,
          label: a.label,
        },
      });
    }
    return { type: "FeatureCollection", features };
  }

  // Importance (lower = more important): GA before commercial, then lower
  // altitude before higher; ground/parked aircraft last within a group.
  _score(a) {
    return (a.ga ? 0 : 100000) + (a.onGround ? 60000 : a.altFt || 0);
  }

  list({ includeGround = true } = {}) {
    let items = [...this.map.values()];
    if (!includeGround) items = items.filter((a) => !a.onGround);
    return items.sort((a, b) => this._score(a) - this._score(b));
  }

  clear() {
    this.map.clear();
  }
}
