// Aircraft icon assignment and rendering, following tar1090.
//
// The shapes and the lookup tables live in the vendored tar1090 module
// (lib/vendor/tar1090/markers.js, GPL-2.0). Assignment order, as in tar1090:
// exact ICAO type designator (B738, C172, ...), then the ICAO 8643 type
// description plus wake turbulence category (L2J-M is an airliner, L2J-L a
// light jet, L1P a single piston, H a helicopter, ...), then the broadcast
// ADS-B emitter category (A1..A7, B1.., C0..), else the "unknown" shape.
// The backend supplies type_desc and wtc from the vendored type database.
import { getBaseMarker, shapes, svgShapeToSVG } from "./vendor/tar1090/markers.js";

const OUTLINE = "#0b0f14";
const OUTLINE_WIDTH = 1;

// -> { name, scale, noRotate } for an aircraft record from /api/traffic
export function classify(ac) {
  const [name, scale] = getBaseMarker(
    ac.category || null,
    ac.type ? String(ac.type).toUpperCase() : null,
    ac.type_desc || null,
    ac.wtc || null,
    null,
    ac.on_ground ? "ground" : null
  );
  const shape = shapes[name] || shapes.unknown;
  return { name: shapes[name] ? name : "unknown", scale: scale || 1, noRotate: !!shape.noRotate };
}

// SVG markup for an icon: tar1090's shape drawn at its native size times
// (shape scale x display scale), filled with the altitude color and outlined.
export function iconSvg(icon, fillColor, displayScale = 1) {
  const shape = shapes[icon.name] || shapes.unknown;
  return svgShapeToSVG(shape, fillColor, OUTLINE, OUTLINE_WIDTH, (icon.scale || 1) * displayScale);
}

// Importance tier for the side panel: true = general aviation / light / local
// traffic, false = airline, heavy, or military. Decided from the type
// description and wake category (piston and light aircraft, helicopters, and
// gliders are GA; medium and heavy jets and turboprops are not), the
// tar1090-db military flag, and finally the emitter category. Never the callsign.
export function isGA(ac) {
  if (typeof ac.db_flags === "number" && ac.db_flags & 1) return false; // military
  const d = ac.type_desc || "";
  if (d.length === 3) {
    const cls = d[0];
    const eng = d[2];
    if (cls === "H" || cls === "G") return true;
    if (eng === "P" || eng === "E") return true;
    if (eng === "J" || eng === "T") return ac.wtc === "L";
    return true;
  }
  const cat = ac.category || "";
  if (cat === "A3" || cat === "A4" || cat === "A5" || cat === "A6") return false;
  return true;
}
