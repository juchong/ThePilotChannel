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

// Scale map glyphs up on high-resolution panels (e.g. 4K) so they are the same
// physical size as on a 1080p screen. CSS handles the rest of the UI.
const UI = typeof window !== "undefined" && window.innerWidth >= 2560 ? 2 : 1;
const AC_ICON_PX = 32 * UI;
const BARB_PX = 78 * UI;
// Radar frame cross-fade duration (ms). Should be a bit under the frame step so
// each frame dissolves into the next, making the 5-minute steps look fluid.
const RADAR_FADE_MS = 450;

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
      // The cycle revisits a small fixed set of views, so cache tiles in memory
      // and do not re-fetch expired tiles. OSM raster tiles change rarely, so
      // reusing them makes view switches near-instant. Kept modest (not unbounded)
      // because each cached tile is a GPU texture and this runs 24/7 on a Pi.
      maxTileCacheSize: 400,
      refreshExpiredTiles: false,
      fadeDuration: 0,
    });
    this._apMarkers = [];
    this._ac = new Map(); // hex -> { marker, iconEl, labelEl, name, color, track }
    this._radarLayers = []; // one raster layer id per radar frame (built once, reused)
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
        rec.iconEl.innerHTML = shapeSvg(p.icon, color, AC_ICON_PX);
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

  // airports: [{icao, lat, lon, metar}]. With { barbs: false } only a station dot
  // and the identifier are drawn (used by the radar view to label airports without
  // cluttering the precipitation map with wind barbs).
  setAirportBarbs(airports, { barbs = true } = {}) {
    for (const m of this._apMarkers) m.remove();
    this._apMarkers = [];
    for (const ap of airports || []) {
      if (ap.lat == null || ap.lon == null) continue;
      const el = document.createElement("div");
      el.className = "map-airport";
      if (barbs) {
        const barb = ap.metar
          ? windBarbSVG({
              speedKt: ap.metar.wind.speed_kt,
              dirDeg: ap.metar.wind.dir,
              variable: ap.metar.wind.variable,
              calm: ap.metar.wind.calm,
              color: ap.metar.category_color,
              fallback: "#ffffff",
              size: BARB_PX,
            })
          : windBarbSVG({ speedKt: null, color: "#ffffff", size: BARB_PX });
        el.innerHTML = `${barb}<div class="map-airport-label">${ap.icao}</div>`;
      } else {
        el.innerHTML = `<div class="map-airport-dot"></div><div class="map-airport-label">${ap.icao}</div>`;
      }
      const marker = new maplibregl.Marker({ element: el, anchor: "center" }).setLngLat([ap.lon, ap.lat]).addTo(this.map);
      this._apMarkers.push(marker);
    }
  }

  // Current visible map rectangle as a METAR bbox.
  visibleBbox() {
    const b = this.map.getBounds();
    return { minLat: b.getSouth(), minLon: b.getWest(), maxLat: b.getNorth(), maxLon: b.getEast() };
  }

  frameView(view, { ring = true } = {}) {
    const [w, s, e, n] = bboxForRadius(view.center_lat, view.center_lon, view.radius_nm);
    this.map.fitBounds([[w, s], [e, n]], { padding: 40, animate: false });
    const src = this.map.getSource("ring");
    if (src) src.setData(ring ? ringPolygon(view.center_lat, view.center_lon, view.radius_nm) : empty());
  }

  // NEXRAD radar loop: one raster layer per time-lagged frame, BUILT ONCE per page
  // session and reused. Animated by toggling the layout `visibility` property;
  // hidden (not removed) when off the regional view. Repeatedly adding/removing
  // these raster sources on every regional view leaked GPU/dma-buf memory on the
  // Pi (removed raster textures were never reclaimed). Building them once keeps a
  // fixed, bounded set of textures, so memory no longer grows over time. Layers
  // sit below the ring/markers so aircraft and barbs draw on top.
  // frames: [{ suffix, age_min }]; tileBase: IEM tile template prefix.
  ensureRadar(frames, { tileBase, opacity = 0.75 } = {}) {
    this._radarOpacity = opacity;
    if (this._radarLayers.length || !(frames || []).length) return;
    const beforeId = this.map.getLayer("ring") ? "ring" : undefined;
    frames.forEach((f, i) => {
      const id = `radar-${i}`;
      this.map.addSource(id, {
        type: "raster",
        tiles: [`${tileBase}${f.suffix}/{z}/{x}/{y}.png`],
        tileSize: 256,
        attribution: "NEXRAD via Iowa Environmental Mesonet",
      });
      this.map.addLayer(
        {
          id,
          type: "raster",
          source: id,
          layout: { visibility: "none" },
          // Frames cross-fade via raster-opacity transitions for smooth motion.
          paint: {
            "raster-opacity": 0,
            "raster-opacity-transition": { duration: RADAR_FADE_MS, delay: 0 },
            "raster-fade-duration": 0,
          },
        },
        beforeId
      );
      this._radarLayers.push(id);
    });
    this._radarCur = -1;
  }

  // Cross-fade to frame i: fade it in while fading the previous frame out, and
  // stop rendering the frame two steps back (its fade has finished). At most two
  // radar layers render at once, so memory stays bounded (no source churn).
  showRadarFrame(i) {
    const L = this._radarLayers;
    const n = L.length;
    if (!n) return;
    this.map.setLayoutProperty(L[i], "visibility", "visible");
    this.map.setPaintProperty(L[i], "raster-opacity", this._radarOpacity);
    const prev = (i - 1 + n) % n;
    if (prev !== i) this.map.setPaintProperty(L[prev], "raster-opacity", 0);
    const old = (i - 2 + n) % n; // two frames back: fade complete, stop rendering it
    if (old !== i && old !== prev) this.map.setLayoutProperty(L[old], "visibility", "none");
    this._radarCur = i;
  }

  // Hide the radar loop without tearing down the sources (called when leaving the
  // regional view). Keeping the sources avoids the add/remove churn that leaked.
  hideRadar() {
    for (const id of this._radarLayers) {
      if (this.map.getLayer(id)) {
        this.map.setPaintProperty(id, "raster-opacity", 0);
        this.map.setLayoutProperty(id, "visibility", "none");
      }
    }
    this._radarCur = -1;
  }

  clearRadar() {
    for (const id of this._radarLayers) {
      if (this.map.getLayer(id)) this.map.removeLayer(id);
      if (this.map.getSource(id)) this.map.removeSource(id);
    }
    this._radarLayers = [];
  }

  clearAircraft() {
    for (const [, rec] of this._ac) rec.marker.remove();
    this._ac.clear();
  }

  // Warm the tile cache for every map-based view. On first display, MapLibre shows
  // upscaled parent tiles (blurry) until the real tiles load; pre-fetching each
  // view's tiles up front means they are already cached when the view appears, so
  // it renders crisp immediately. The in-memory tile cache (maxTileCacheSize) keeps
  // them for the whole session since the cycle revisits a small fixed set of views.
  async warmViews(views) {
    const seen = new Set();
    for (const v of views || []) {
      if (!v || v.center_lat == null || v.radius_nm == null || seen.has(v.id)) continue;
      seen.add(v.id);
      const [w, s, e, n] = bboxForRadius(v.center_lat, v.center_lon, v.radius_nm);
      this.map.fitBounds([[w, s], [e, n]], { padding: 40, animate: false });
      await this._waitForTiles(1500);
    }
  }

  // Resolve when the map has finished loading/rendering tiles for the current view,
  // or after maxMs as a safety cap so a slow tile never blocks startup.
  _waitForTiles(maxMs) {
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        this.map.off("idle", finish);
        resolve();
      };
      this.map.once("idle", finish);
      setTimeout(finish, maxMs);
    });
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
