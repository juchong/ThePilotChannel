// MapLibre wrapper. Aircraft are rendered as HTML markers (type silhouettes,
// altitude-colored, rotated by track, with tail/callsign labels) rather than a
// GeoJSON symbol layer - DOM markers render reliably and avoid the async
// GeoJSON-source pitfalls. Airport wind barbs are also HTML markers.
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { bboxForRadius } from "./geo.js";
import { SHAPES } from "./shapes.js";
import { windBarbSVG } from "./windbarb.js";

export const ALT_STOPS = [
  [0, "#e0245e"],
  [2000, "#f59e0b"],
  [5000, "#eab308"],
  [10000, "#22c55e"],
  [20000, "#3b82f6"],
  [30000, "#a855f7"],
  [40000, "#e5e7eb"],
];
const GROUND_COLOR = "#9ca3af";
const UNKNOWN_COLOR = "#cbd5e1";

export function altColorFor(altFt, onGround) {
  if (onGround) return GROUND_COLOR;
  if (altFt == null || altFt < 0) return UNKNOWN_COLOR;
  let c = ALT_STOPS[0][1];
  for (const [stop, col] of ALT_STOPS) if (altFt >= stop) c = col;
  return c;
}

function shapeSvg(name, color, size) {
  const raw = SHAPES[name] || SHAPES.default;
  return raw.split("#ffffff").join(color).replace('width="64" height="64"', `width="${size}" height="${size}"`);
}

function rasterStyle(tileUrl) {
  const tiles = tileUrl
    ? [tileUrl]
    : ["https://a.tile.openstreetmap.org/{z}/{x}/{y}.png", "https://b.tile.openstreetmap.org/{z}/{x}/{y}.png"];
  return {
    version: 8,
    sources: { osm: { type: "raster", tiles, tileSize: 256, attribution: "© OpenStreetMap" } },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#0d1117" } },
      { id: "osm", type: "raster", source: "osm", paint: { "raster-brightness-max": 0.85, "raster-saturation": -0.2 } },
    ],
  };
}

export class HangarMap {
  constructor(container, { basemap, tileUrl } = {}) {
    this.map = new maplibregl.Map({
      container,
      style: rasterStyle(basemap === "vector" ? tileUrl : tileUrl || null),
      center: [-122.27, 47.39],
      zoom: 9,
      attributionControl: false,
      interactive: false,
    });
    this._apMarkers = [];
    this._ac = new Map(); // hex -> { marker, iconEl, labelEl, name, color, track }
    this._showLabels = true;
    window.__hmap = this.map;
    this.ready = this.map.once("load").then(() => this._initLayers());
  }

  _initLayers() {
    this.map.addSource("ring", { type: "geojson", data: empty() });
    this.map.addLayer({
      id: "ring",
      type: "line",
      source: "ring",
      paint: { "line-color": "#38bdf8", "line-width": 1.5, "line-dasharray": [3, 3], "line-opacity": 0.6 },
    });
  }

  // features: GeoJSON-like array from AircraftStore.toGeoJSON().features
  setAircraft(fc) {
    const features = fc.features || [];
    const seen = new Set();
    for (const f of features) {
      const p = f.properties;
      const [lon, lat] = f.geometry.coordinates;
      const color = altColorFor(p.altFt, p.onGround === 1);
      seen.add(p.hex);
      let rec = this._ac.get(p.hex);
      if (!rec) {
        const el = document.createElement("div");
        el.className = "ac-marker";
        const iconEl = document.createElement("div");
        iconEl.className = "ac-icon";
        const labelEl = document.createElement("div");
        labelEl.className = "ac-label";
        el.append(iconEl, labelEl);
        const marker = new maplibregl.Marker({ element: el, anchor: "center" }).setLngLat([lon, lat]).addTo(this.map);
        // Defer enabling the transform transition so the marker doesn't fly in
        // from the corner on its first placement. Subsequent position updates
        // (once per poll) then glide smoothly; the browser handles the tween,
        // which is monotonic between successive fixes (no overshoot/reverse).
        requestAnimationFrame(() => {
          el.style.transition = "transform 1.1s linear";
        });
        rec = { marker, iconEl, labelEl, name: null, color: null, track: null };
        this._ac.set(p.hex, rec);
      }
      rec.marker.setLngLat([lon, lat]);
      if (rec.name !== p.icon || rec.color !== color) {
        rec.iconEl.innerHTML = shapeSvg(p.icon, color, 26);
        rec.name = p.icon;
        rec.color = color;
      }
      const trk = Math.round(p.track || 0);
      if (rec.track !== trk) {
        rec.iconEl.style.transform = `translate(-50%,-50%) rotate(${trk}deg)`;
        rec.track = trk;
      }
      if (rec.labelEl.textContent !== p.label) rec.labelEl.textContent = p.label;
    }
    for (const [hex, rec] of this._ac) {
      if (!seen.has(hex)) {
        rec.marker.remove();
        this._ac.delete(hex);
      }
    }
  }

  showLabels(show) {
    this._showLabels = show;
    this.map.getContainer().classList.toggle("hide-ac-labels", !show);
  }

  // airports: [{icao, lat, lon, metar}]
  setAirportBarbs(airports) {
    for (const m of this._apMarkers) m.remove();
    this._apMarkers = [];
    for (const ap of airports || []) {
      if (ap.lat == null || ap.lon == null) continue;
      const el = document.createElement("div");
      el.className = "map-airport";
      const barb = ap.metar
        ? windBarbSVG({
            speedKt: ap.metar.wind.speed_kt,
            dirDeg: ap.metar.wind.dir,
            variable: ap.metar.wind.variable,
            calm: ap.metar.wind.calm,
            color: ap.metar.category_color,
            fallback: "#ffffff",
            size: 70,
          })
        : windBarbSVG({ speedKt: null, color: "#ffffff", size: 70 });
      el.innerHTML = `${barb}<div class="map-airport-label">${ap.icao}</div>`;
      const marker = new maplibregl.Marker({ element: el, anchor: "center" }).setLngLat([ap.lon, ap.lat]).addTo(this.map);
      this._apMarkers.push(marker);
    }
  }

  // Current visible map rectangle as a METAR bbox.
  visibleBbox() {
    const b = this.map.getBounds();
    return { minLat: b.getSouth(), minLon: b.getWest(), maxLat: b.getNorth(), maxLon: b.getEast() };
  }

  frameView(view) {
    const [w, s, e, n] = bboxForRadius(view.center_lat, view.center_lon, view.radius_nm);
    this.map.fitBounds([[w, s], [e, n]], { padding: 40, animate: false });
    const ring = this.map.getSource("ring");
    if (ring) ring.setData(ringPolygon(view.center_lat, view.center_lon, view.radius_nm));
  }

  clearAircraft() {
    for (const [, rec] of this._ac) rec.marker.remove();
    this._ac.clear();
  }

  // Re-measure the canvas after the container was hidden (satellite view) and shown.
  resize() {
    this.map.resize();
  }

  destroy() {
    this.map.remove();
  }
}

function empty() {
  return { type: "FeatureCollection", features: [] };
}

function ringPolygon(lat, lon, radiusNm, steps = 64) {
  const coords = [];
  for (let i = 0; i <= steps; i++) {
    const brg = (i / steps) * 2 * Math.PI;
    const d = radiusNm / 3440.065;
    const lat1 = (lat * Math.PI) / 180;
    const lon1 = (lon * Math.PI) / 180;
    const lat2 = Math.asin(Math.sin(lat1) * Math.cos(d) + Math.cos(lat1) * Math.sin(d) * Math.cos(brg));
    const lon2 = lon1 + Math.atan2(Math.sin(brg) * Math.sin(d) * Math.cos(lat1), Math.cos(d) - Math.sin(lat1) * Math.sin(lat2));
    coords.push([(lon2 * 180) / Math.PI, (lat2 * 180) / Math.PI]);
  }
  return { type: "Feature", geometry: { type: "LineString", coordinates: coords }, properties: {} };
}
