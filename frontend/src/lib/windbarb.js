// Standard meteorological wind barb.
// The staff points FROM the direction the wind originates (degrees true).
// A filled STATION DOT marks the observation point (the airport) at the base;
// the barbs/pennants sit at the far (upwind) tip -> clear head/tail distinction.
// 5 kt = half barb, 10 kt = full barb, 50 kt = pennant. Calm = open ring.
// Colored by flight category, with a dark halo so it reads on a map.

const CX = 50;
const CY = 50;
const TIP = 8; // top of staff (upwind end, where barbs live)

export function windBarbSVG(opts) {
  const { speedKt, dirDeg, variable, calm, color, fallback = "#ffffff", size = 100, halo = "#0b0f14" } = opts;
  const stroke = color || fallback;

  if (calm || speedKt === 0) {
    return wrap(
      size,
      `<circle cx="${CX}" cy="${CY}" r="11" fill="none" stroke="${halo}" stroke-width="9"/>
       <circle cx="${CX}" cy="${CY}" r="11" fill="none" stroke="${stroke}" stroke-width="3"/>`
    );
  }
  if (speedKt == null) {
    return wrap(size, `<text x="50" y="58" text-anchor="middle" fill="${stroke}" font-size="24">--</text>`);
  }

  const prims = barbPrimitives(speedKt);
  const rotate = variable ? 0 : dirDeg ?? 0;

  // Render halo (wide, dark) then colored, so it pops on any background. A thick
  // halo keeps overlapping barbs distinct, especially over the radar overlay.
  const haloLayer = prims.map((p) => renderPrim(p, halo, 9)).join("");
  const colorLayer = prims.map((p) => renderPrim(p, stroke, 3)).join("");
  const group = `<g transform="rotate(${rotate} ${CX} ${CY})">${haloLayer}${colorLayer}</g>`;

  // Station dot at the base (airport / observation point) - rotation independent.
  const station = `<circle cx="${CX}" cy="${CY}" r="4.5" fill="${stroke}" stroke="${halo}" stroke-width="2.5"/>`;
  const vrb = variable
    ? `<text x="${CX}" y="${CY + 20}" text-anchor="middle" fill="${stroke}" stroke="${halo}" stroke-width="0.6" font-size="14" font-weight="700">VRB</text>`
    : "";

  return wrap(size, group + station + vrb);
}

// Build geometry as primitives so we can draw halo + color passes.
function barbPrimitives(speedKt) {
  const prims = [{ t: "line", x1: CX, y1: CY, x2: CX, y2: TIP }]; // staff
  let spd = Math.round(speedKt / 5) * 5;
  const step = 7;
  const len = 18;
  const ang = (-65 * Math.PI) / 180; // barbs lean toward the tip, on the left
  let y = TIP;

  const pennants = Math.floor(spd / 50);
  spd -= pennants * 50;
  const fulls = Math.floor(spd / 10);
  spd -= fulls * 10;
  const halves = Math.floor(spd / 5);

  for (let i = 0; i < pennants; i++) {
    const xEnd = CX + len * Math.cos(ang);
    const yEnd = y + len * Math.sin(ang);
    prims.push({ t: "poly", pts: [[CX, y], [xEnd, yEnd], [CX, y + step]] });
    y += step + 2;
  }
  for (let i = 0; i < fulls; i++) {
    prims.push({ t: "line", x1: CX, y1: y, x2: CX + len * Math.cos(ang), y2: y + len * Math.sin(ang) });
    y += step;
  }
  for (let i = 0; i < halves; i++) {
    if (pennants === 0 && fulls === 0) y += step; // keep a lone half-barb off the very tip
    prims.push({ t: "line", x1: CX, y1: y, x2: CX + (len / 2) * Math.cos(ang), y2: y + (len / 2) * Math.sin(ang) });
    y += step;
  }
  return prims;
}

function renderPrim(p, color, width) {
  if (p.t === "line") {
    return `<line x1="${p.x1}" y1="${p.y1}" x2="${p.x2}" y2="${p.y2}" stroke="${color}" stroke-width="${width}" stroke-linecap="round"/>`;
  }
  if (p.t === "poly") {
    const pts = p.pts.map((a) => a.join(",")).join(" ");
    return `<polygon points="${pts}" fill="${color}" stroke="${color}" stroke-width="${Math.max(1, width - 2)}" stroke-linejoin="round"/>`;
  }
  return "";
}

function wrap(size, inner) {
  return `<svg viewBox="0 0 100 100" width="${size}" height="${size}" xmlns="http://www.w3.org/2000/svg">${inner}</svg>`;
}
