// Aircraft silhouette icon set (top-down, nose pointing up / north) and a
// type/category classifier, in the spirit of tar1090 / adsbexchange markers.
// Each shape is a white-filled SVG so it can be rasterized as an SDF icon and
// tinted per-aircraft (by altitude) via MapLibre icon-color.

const VB = 64;

function svg(inner) {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${VB} ${VB}" width="${VB}" height="${VB}"><g fill="#ffffff">${inner}</g></svg>`;
}

export const SHAPES = {
  // Swept twin jet airliner
  airliner: svg(
    `<path d="M32 3 L34.5 25 L61 41 L61 46 L34.5 38 L34 53 L43 60 L43 63 L32 59 L21 63 L21 60 L30 53 L29.5 38 L3 46 L3 41 L29.5 25 Z"/>`
  ),
  // Rear-engine business jet (slimmer, less wing sweep)
  bizjet: svg(
    `<path d="M32 4 L34 26 L54 40 L54 44 L34 38 L34 52 L41 58 L41 61 L32 57 L23 61 L23 58 L30 52 L30 38 L10 44 L10 40 L30 26 Z"/>`
  ),
  // Light single GA (high straight wing)
  ga_single: svg(
    `<path d="M32 6 L33.5 24 L60 29 L60 34 L33.5 32 L33.5 51 L41 57 L41 60 L32 56 L23 60 L23 57 L30.5 51 L30.5 32 L4 34 L4 29 L30.5 24 Z"/>`
  ),
  // Twin turboprop (straight wing + nacelles)
  turboprop: svg(
    `<path d="M32 5 L34 24 L62 30 L62 35 L34 33 L34 52 L42 58 L42 61 L32 57 L22 61 L22 58 L30 52 L30 33 L2 35 L2 30 L30 24 Z"/>
     <rect x="16" y="29" width="5" height="9" rx="1.5"/><rect x="43" y="29" width="5" height="9" rx="1.5"/>`
  ),
  // Helicopter: rotor disc + body + tail boom + tail rotor
  helicopter: svg(
    `<circle cx="32" cy="28" r="21" fill="none" stroke="#ffffff" stroke-width="2.5"/>
     <ellipse cx="32" cy="28" rx="7" ry="11"/>
     <rect x="30.5" y="38" width="3" height="20" rx="1.5"/>
     <rect x="26" y="56" width="12" height="3" rx="1.5"/>`
  ),
  // Glider (long thin wings)
  glider: svg(
    `<path d="M32 6 L33.5 28 L62 30.5 L62 33 L33.5 32 L33 55 L39 60 L39 62 L32 58 L25 62 L25 60 L31 55 L30.5 32 L2 33 L2 30.5 L30.5 28 Z"/>`
  ),
  // Balloon
  balloon: svg(`<circle cx="32" cy="26" r="20"/><path d="M26 44 L38 44 L35 58 L29 58 Z"/>`),
  // Military / fast jet (delta)
  military: svg(
    `<path d="M32 3 L34 30 L51 55 L51 59 L34 50 L34 60 L39 63 L25 63 L30 60 L30 50 L13 59 L13 55 L30 30 Z"/>`
  ),
  // Generic fallback
  default: svg(
    `<path d="M32 4 L34.5 25 L61 41 L61 46 L34.5 38 L34 53 L43 60 L43 63 L32 59 L21 63 L21 60 L30 53 L29.5 38 L3 46 L3 41 L29.5 25 Z"/>`
  ),
};

// ICAO type-designator patterns. Classification is driven primarily by TYPE
// (not callsign), so a flight-school C172 flying as "RFS716" is still GA and an
// N-registered private B737 is still an airliner.
const HELI_RE = /^(EC|AS3|AS5|AS6|R22|R44|R66|B06|B47|H60|UH|AH|S76|S92|A109|A119|A139|A169|MD5|H125|H130|H135|H145|H155|H175|EH10|B412|B429|B505|GAZL|R900|B407|B430|S55|S58|S61|S64|S70|EXPL|EN28|EN48)/i;
const GLIDER_RE = /^(GLID|AS2|DG[0-9]|LS[0-9]|ASK|ASW|ASH|DISC|VENT|JANU|NIMB|ARCU|TWIN|STD|PIK|SZD)/i;
const MIL_RE = /^(F15|F16|F18|FA18|F22|F35|F5|A10|B1B|B2|B52|C17|C130|C135|KC|E3[CTB]|E3CF|E2|E6|E8|RC13|U2|T38|T6|P3|P8|H53|CH47|CH53|V22|AV8|EUFI|TOR|RAFL|GLF5M)/i;

// Commercial airliner / regional types (jets and airline turboprops) -> not GA.
const AIRLINER_RE = /^(A30|A31|A318|A319|A32|A19N|A20N|A21N|A33|A34|A35|A38|B46|B71|B72|B73|B38M|B39M|B3XM|B74|B75|B76|B77|B78|BCS|CRJ|CL44|E70|E75|E90|E17|E19|E29|E45X|F70|F100|MD8|MD9|MD11|DC10|AT4|AT5|AT7|DH8|SF34|SB20|J328|F28|RJ85|RJ1H|SU95|SSJ|L101|IL76|IL96|T204)/i;

// Turboprop SHAPE (both GA turboprops and airline turboprops look the same).
const TURBOPROP_SHAPE_RE = /^(DH8|AT4|AT5|AT7|SF34|SB20|C208|PC12|PC6|TBM|BE20|BE9|B350|B300|B190|DHC2|DHC3|DHC6|P46T|M600|SW4|JS31|JS41|D228|E110|E120|C441|C425|PA31|PAY|EPIC)/i;

// Business jet SHAPE.
const BIZJET_RE = /^(LJ[0-9]|C25|C50|C51|C52|C55|C56|C68|C700|GLF|GLEX|GL5T|GL7T|G280|CL30|CL35|CL60|F900|F2TH|FA[5-8]|H25|PRM1|BE40|E55P|E50P|HDJT|EA50|MU30|WW24|ASTR)/i;

// emitter category (A1..A7, B1..B7) + ICAO type designator -> shape key
export function classify(category, type) {
  const cat = (category || "").toUpperCase();
  const t = (type || "").toUpperCase();

  if (cat === "A7" || HELI_RE.test(t)) return "helicopter";
  if (cat === "B2") return "balloon";
  if (cat === "B1" || GLIDER_RE.test(t)) return "glider";
  if (MIL_RE.test(t) || cat === "A6") return "military";
  if (TURBOPROP_SHAPE_RE.test(t)) return "turboprop"; // shape; tier decided by isGA()
  if (AIRLINER_RE.test(t)) return "airliner";
  if (BIZJET_RE.test(t)) return "bizjet";
  if (cat === "A3" || cat === "A4" || cat === "A5") return "airliner";
  return "ga_single"; // light single / unknown-small -> small plane silhouette
}

// Importance tier: true = GA / light / local traffic (prioritized), false =
// commercial airliner or military. Driven by aircraft type, not callsign.
export function isGA(category, type) {
  const cat = (category || "").toUpperCase();
  const t = (type || "").toUpperCase();
  if (AIRLINER_RE.test(t)) return false;
  if (MIL_RE.test(t)) return false;
  if (cat === "A6") return false;
  if (cat === "A3" || cat === "A4" || cat === "A5") return false;
  return true;
}

export const SHAPE_KEYS = Object.keys(SHAPES);
