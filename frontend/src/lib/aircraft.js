// Aircraft store keyed by hex. Holds each aircraft's last fix (position, ground
// speed, track, and when the fix was measured) and computes where to draw it at
// any instant by dead reckoning from that fix. Drawing is therefore continuous
// and independent of when snapshots happen to arrive: a snapshot that repeats
// the previous fix changes nothing, and a late one is blended in rather than
// jumped to.
import { movePoint } from "./geo.js";
import { classify, isGA } from "./shapes.js";

const MAX_EXTRAPOLATION_MS = 30 * 1000; // beyond this, hold the last predicted position
const CORRECTION_TAU_MS = 600;          // a new fix's correction decays with this time constant
const MAX_FIX_AGE_MS = 15 * 1000;       // clamp implausible fix ages (clock skew, bad feeds)

export class AircraftStore {
  constructor(dropTimeoutS = 15) {
    this.map = new Map();
    this.dropTimeoutS = dropTimeoutS;
  }

  setDropTimeout(s) {
    if (s) this.dropTimeoutS = s;
  }

  // list: aircraft records from a /api/traffic snapshot; snapshot: the snapshot
  // itself ({ ts, age_s }) so fix ages can be taken relative to it, which makes
  // backend and browser clock skew irrelevant.
  update(list, snapshot = {}) {
    const now = Date.now();
    const perf = performance.now();
    const snapAgeMs = Math.max(0, (snapshot.age_s || 0) * 1000);
    const seen = new Set();
    for (const ac of list) {
      if (ac.lat == null || ac.lon == null) continue;
      const altFt = ac.on_ground ? 0 : ac.alt_ft;
      // Negative reported altitude is treated as on-the-ground/invalid and dropped.
      if (typeof altFt === "number" && altFt < 0) continue;
      seen.add(ac.hex);
      let fixAgeMs = 0;
      if (typeof ac.fix_ts === "number" && typeof snapshot.ts === "number") {
        fixAgeMs = (snapshot.ts - ac.fix_ts) * 1000;
      } else if (typeof ac.seen_pos === "number") {
        fixAgeMs = ac.seen_pos * 1000;
      }
      fixAgeMs = Math.min(Math.max(0, fixAgeMs), MAX_FIX_AGE_MS);
      const fixTs = now - snapAgeMs - fixAgeMs;
      const prev = this.map.get(ac.hex);
      const rec = {
        hex: ac.hex,
        label: (ac.callsign || ac.registration || ac.hex || "").trim(),
        type: ac.type,
        iconType: classify(ac.category, ac.type),
        track: ac.track ?? prev?.track ?? 0,
        gs: ac.gs ?? 0,
        altFt,
        onGround: !!ac.on_ground,
        ga: isGA(ac.category, ac.type),
        lat: ac.lat,
        lon: ac.lon,
        fixTs,
        lastSeen: perf,
        // display state: correction offset (degrees) and where it was last drawn
        offLat: 0,
        offLon: 0,
        offTs: now,
        dispLat: ac.lat,
        dispLon: ac.lon,
      };
      if (prev) {
        const newFix = prev.lat !== rec.lat || prev.lon !== rec.lon || Math.abs(prev.fixTs - rec.fixTs) > 250;
        if (newFix) {
          // Keep the marker where it is drawn right now and let the difference
          // to the new prediction decay away instead of jumping.
          const [pLat, pLon] = this._predict(rec, now);
          rec.offLat = prev.dispLat - pLat;
          rec.offLon = prev.dispLon - pLon;
          rec.offTs = now;
        } else {
          // Same fix as last time (the snapshot repeated): nothing changes.
          rec.fixTs = prev.fixTs;
          rec.offLat = prev.offLat;
          rec.offLon = prev.offLon;
          rec.offTs = prev.offTs;
        }
        rec.dispLat = prev.dispLat;
        rec.dispLon = prev.dispLon;
      } else {
        // First sighting: start the marker where the aircraft is now, not at
        // the (possibly seconds-old) fix, so it does not hop on the first frame.
        [rec.dispLat, rec.dispLon] = this._predict(rec, now);
      }
      this.map.set(ac.hex, rec);
    }
    for (const [hex, a] of this.map) {
      if (!seen.has(hex) && perf - a.lastSeen > this.dropTimeoutS * 1000) {
        this.map.delete(hex);
      }
    }
  }

  // Dead-reckoned position of a at nowMs: the last fix moved along its track at
  // its ground speed for the time since the fix. Ground and stationary aircraft
  // stay put.
  _predict(a, nowMs) {
    if (a.onGround || !(a.gs > 1) || a.track == null) return [a.lat, a.lon];
    const dt = Math.min(Math.max(0, nowMs - a.fixTs), MAX_EXTRAPOLATION_MS) / 1000;
    if (dt <= 0) return [a.lat, a.lon];
    return movePoint(a.lat, a.lon, a.track, (a.gs / 3600) * dt);
  }

  // Where to draw a at nowMs: the prediction plus the decaying correction offset.
  displayPosition(a, nowMs) {
    const [pLat, pLon] = this._predict(a, nowMs);
    const k = a.offLat || a.offLon ? Math.exp(-(nowMs - a.offTs) / CORRECTION_TAU_MS) : 0;
    a.dispLat = pLat + a.offLat * k;
    a.dispLon = pLon + a.offLon * k;
    return [a.dispLat, a.dispLon];
  }

  toGeoJSON({ includeGround = true } = {}) {
    const features = [];
    for (const a of this.map.values()) {
      if (!includeGround && a.onGround) continue;
      features.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: [a.dispLon, a.dispLat] },
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
