// NOTAM view: time formatting, status, grouping, paging arithmetic and card
// markup. Times are rendered in the display's time zone, each with the name the
// zone has on that date, from the epoch seconds the backend passes through from
// the FAA. Clock times in a NOTAM's wording are UTC: the plain-language and
// schedule lines give them in the zone only where that is exact, and mark them
// UTC otherwise. The NOTAM as issued is shown as written. All text is escaped.
import { esc } from "./dom.js";

const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
const two = (n) => String(n).padStart(2, "0");

export const MAX_PAGES = 8;        // pages shown in one visit of a NOTAM view
export const LONG_TEXT = 700;      // a NOTAM longer than this may continue in the next column

const formats = new Map();
function zoneParts(ts, tz) {
  let f = formats.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short" });
    formats.set(tz, f);
  }
  const p = {};
  for (const { type, value } of f.formatToParts(new Date(ts * 1000))) p[type] = value;
  return p;
}

// 1794160800 in America/Los_Angeles -> "08 NOV 2026 0700 PST". null when there is no time.
export function fmtTime(ts, tz = "UTC") {
  if (typeof ts !== "number" || !isFinite(ts)) return null;
  const p = zoneParts(ts, tz);
  return `${p.day} ${MONTHS[Number(p.month) - 1]} ${p.year} ${p.hour}${p.minute} ${p.timeZoneName}`;
}

// The zone's offset from UTC at ts, in minutes (-480 for PST).
export function offsetMin(ts, tz) {
  const p = zoneParts(ts, tz);
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute) / 60000 - Math.floor(ts / 60);
}

const WEEK_S = 7 * 86400;
const YEAR_S = 366 * 86400;

// The zone's offset through what is left of a NOTAM's period, in minutes; null
// where it changes (daylight saving) or the period has no stated end. A
// permanent NOTAM is judged over the coming year.
export function steadyOffset(n, tz, nowS) {
  const from = typeof n.start === "number" ? Math.max(n.start, nowS) : nowS;
  const to = n.permanent ? from + YEAR_S : n.end;
  if (typeof to !== "number" || to < from) return null;
  const first = offsetMin(from, tz);
  for (let t = from + WEEK_S; t < to; t += WEEK_S) if (offsetMin(t, tz) !== first) return null;
  return offsetMin(to, tz) === first ? first : null;
}

// Clock times in a NOTAM's wording. HHMM, a range of two joined by "-" or "/",
// each optionally with Z; never a figure followed by a unit or after FL.
const CLOCK_RE = /(?<![\w/.])(\d{4})Z?(?:\s?[-/]\s?(\d{4})Z?)?(?![\w/]|\.\d)/g;
const UNIT_RE = /^\s?(FT|FEET|FOOT|M|MSL|AGL|NM|KM|SM|MHZ|KHZ|LBS?|KG|HP)\b/i;
const DAY_NAMES = "MON|TUE|WED|THU|FRI|SAT|SUN|MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY";
const MONTH_NAMES = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC";
const SCHEDULE_WORD_RE = new RegExp(`\\b(DLY|DAILY|SR|SS|${DAY_NAMES})\\b`);
// a named day or month, or a date (14, 14-16) before a time: what a time's day is tied to
const DAY_BOUND_RE = new RegExp(`\\b(${DAY_NAMES}|${MONTH_NAMES})\\b|(?<![\\d./])\\d{1,2}(?:\\s?-\\s?\\d{1,2})?,?\\s+\\d{4}\\b`);
// a single time in a NOTAM's text: with Z, beside sunrise or sunset, or after a word of time
const LONE_BEFORE_RE = /(\b(SR|SS)\s?[-/]\s?|\b(AFT|BFR|FM|TIL|UNTIL)\s)$/;
const LONE_AFTER_RE = /^\s?[-/]\s?(SR|SS)\b/;
// wording that gives local times itself says which of its times are which
const LOCAL_WORD_RE = /\b(LCL|LOCAL)\b/i;
const minutesOf = (hhmm) => {
  const h = Number(hhmm.slice(0, 2)), m = Number(hhmm.slice(2));
  return m < 60 && (h < 24 || (h === 24 && m === 0)) ? h * 60 + m : null;
};
const hhmmOf = (min) => {
  const m = ((min % 1440) + 1440) % 1440;
  return `${two(Math.floor(m / 60))}${two(m % 60)}`;
};

function clockMatches(text) {
  const out = [];
  for (const m of text.matchAll(CLOCK_RE)) {
    const after = text.slice(m.index + m[0].length);
    const before = text.slice(Math.max(0, m.index - 3), m.index);
    if (UNIT_RE.test(after) || /FL\s?$/i.test(before)) continue;
    const from = minutesOf(m[1]);
    const to = m[2] === undefined ? undefined : minutesOf(m[2]);
    if (from === null || from === 1440 || to === null) continue;
    out.push({ index: m.index, length: m[0].length, first: m[1], second: m[2], from, to,
      lone: m[2] === undefined && (/Z$/.test(m[0]) || LONE_BEFORE_RE.test(text.slice(0, m.index)) || LONE_AFTER_RE.test(after)) });
  }
  return out;
}

// The UTC clock times a NOTAM's wording gives: {times: Set of HHMM, ranges,
// dayBound}. `schedule` is the schedule field, where every figure is a time.
// In the NOTAM text a range counts beside a schedule word, a single figure only
// as LONE_*_RE describe it. Wording that names local time has none.
export function clockTimes(text, schedule = false) {
  const times = new Set();
  const ranges = [];
  const t = text || "";
  if (!LOCAL_WORD_RE.test(t) && (schedule || SCHEDULE_WORD_RE.test(t))) {
    for (const m of clockMatches(t)) {
      if (m.second !== undefined) {
        times.add(m.first).add(m.second);
        ranges.push(m);
      } else if (schedule || m.lone) {
        times.add(m.first);
      }
    }
  }
  return { times, ranges, dayBound: DAY_BOUND_RE.test(t) };
}

// How a line of a NOTAM's wording reads in the zone: each clock time of
// `clock` converted where the zone's offset holds through the NOTAM and no
// named day or date would move, else each marked UTC. -> {text, local}
export function localizeLine(line, clock, n, tz, nowS) {
  const text = line || "";
  if (!clock.times.size) return { text, local: false };
  const offset = steadyOffset(n, tz, nowS);
  const found = clockMatches(text);
  const moves = (m) => clock.dayBound && (m.from + offset < 0 || m.from + offset >= 1440);
  const known = found.filter((m) => clock.times.has(m.first) || (m.second !== undefined && clock.times.has(m.second)));
  // a time of the NOTAM's that is not one of its ranges here (a lone figure) is only ever marked
  const lone = known.some((m) => m.second === undefined || !clock.ranges.some((r) => r.first === m.first && r.second === m.second));
  const local = offset !== null && !lone && known.length > 0 && !known.some(moves);
  const zone = local ? zoneParts(Math.max(typeof n.start === "number" ? n.start : nowS, nowS), tz).timeZoneName : "UTC";
  let out = "", at = 0;
  for (const m of known) {
    const orig = text.slice(m.index, m.index + m.length);
    const sep = /\//.test(orig) ? "/" : "-";
    let shown = `${orig} UTC`;
    if (local) shown = `${hhmmOf(m.from + offset)}${sep}${hhmmOf(m.to + offset)} ${zone}`;
    else if (/Z$/.test(orig)) shown = orig;   // marked Zulu already
    out += text.slice(at, m.index) + shown;
    at = m.index + m.length;
  }
  return { text: out + text.slice(at), local };
}

// A length of time in at most two units: "45 min", "3 h 20 min", "2 d 4 h", "6 wk 2 d".
export function fmtSpan(seconds) {
  const min = Math.max(0, Math.round(seconds / 60));
  if (min < 1) return "under 1 min";
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  if (h < 48) return min % 60 ? `${h} h ${min % 60} min` : `${h} h`;
  const d = Math.floor(h / 24);
  if (d < 28) return h % 24 ? `${d} d ${h % 24} h` : `${d} d`;
  const wk = Math.floor(d / 7);
  return d % 7 ? `${wk} wk ${d % 7} d` : `${wk} wk`;
}

// NOTAMs still to be shown at nowS: one that ended since it was fetched is dropped.
export function current(notams, nowS) {
  return (notams || []).filter((n) => !(typeof n.end === "number" && n.end <= nowS));
}

// -> { kind: "active" | "upcoming", label, detail }
export function statusOf(n, nowS) {
  if (typeof n.start === "number" && n.start > nowS) {
    return { kind: "upcoming", label: `STARTS IN ${fmtSpan(n.start - nowS)}`, detail: "" };
  }
  let detail = "end not stated";
  if (n.permanent) detail = "permanent";
  else if (typeof n.end === "number") {
    detail = `ends in ${fmtSpan(n.end - nowS)}` + (n.end_estimated ? " (estimated)" : "");
  }
  return { kind: "active", label: "IN EFFECT", detail };
}

// The effective period: { from, until, span }, each already text.
export function effective(n, tz = "UTC") {
  const from = fmtTime(n.start, tz) || "not stated";
  let until = "not stated";
  if (n.permanent) until = "PERMANENT";
  else if (typeof n.end === "number") until = fmtTime(n.end, tz) + (n.end_estimated ? " EST" : "");
  const span = typeof n.start === "number" && typeof n.end === "number" ? fmtSpan(n.end - n.start) : null;
  return { from, until, span };
}

// Consecutive NOTAMs of one type as a group, in the order given.
export function groupByType(notams) {
  const groups = [];
  for (const n of notams) {
    const last = groups[groups.length - 1];
    if (last && last.type === n.type) last.items.push(n);
    else groups.push({ type: n.type, label: n.type_label || n.type, items: [n] });
  }
  return groups;
}

export function counts(notams, nowS) {
  const upcoming = notams.filter((n) => statusOf(n, nowS).kind === "upcoming").length;
  return { total: notams.length, active: notams.length - upcoming, upcoming };
}

// Pages of a column layout whose overflow continues to the right: the list is
// scrollWidth wide in all and shows pageWidth at a time, columns `gap` apart.
export function pageCount(scrollWidth, pageWidth, gap) {
  if (!(pageWidth > 0)) return 1;
  return Math.max(1, Math.round((scrollWidth + gap) / (pageWidth + gap)));
}

// How long a NOTAM view stays up: perPage seconds for each page shown.
export function visitSeconds(pages, perPage) {
  return Math.min(Math.max(1, pages), MAX_PAGES) * perPage;
}

// What the view says across its top when its NOTAMs are not operational data:
// invented samples, or what one of the FAA's test environments holds.
export function bannerText(r) {
  if (r.source === "sample") return "SAMPLE DATA · NOT REAL NOTAMS · FOR PREVIEW ONLY";
  if (r.test_environment) return "NMS TEST ENVIRONMENT · MAY HOLD TEST NOTAMS · NOT FOR OPERATIONAL USE";
  return "";
}

// How the footer names where the NOTAMs come from: the label the backend gives.
export function sourceText(r) {
  return r.source_label || (r.source === "sample" ? "sample data" : "NMS");
}

// The caution for a location whose NOTAMs include international ones.
export function cautionText(r) {
  return r.non_us ? "NMS IS NOT THE AUTHORITATIVE SOURCE FOR NON-US NOTAMS" : "";
}

// Where the plain-language line came from.
export function originText(plain) {
  return plain && plain.source === "model"
    ? "Model rewrite, checked against the NOTAM"
    : "FAA contractions spelled out";
}

function ago(ts, nowS) {
  return typeof ts === "number" ? `${fmtSpan(nowS - ts)} ago` : "";
}

function timeCell(label, value, sub, cls = "") {
  return `<div class="nt-time ${cls}"><span class="k">${esc(label)}</span><span class="s">${esc(sub || "")}</span>` +
    `<span class="v">${esc(value)}</span></div>`;
}

// The schedule line: in the zone where every figure in it is a range that
// converts exactly, otherwise as written under a UTC label.
export function scheduleText(n, tz, nowS) {
  const written = n.schedule || "";
  if (!/\d{4}/.test(written) || LOCAL_WORD_RE.test(written)) return { label: "SCHEDULE", text: written };
  const clock = clockTimes(written, true);
  let rest = written;
  for (const r of [...clock.ranges].reverse()) rest = rest.slice(0, r.index) + " " + rest.slice(r.index + r.length);
  const line = localizeLine(written, clock, n, tz, nowS);
  if (line.local && !/\d{4}/.test(rest)) return { label: "SCHEDULE", text: line.text };
  return { label: "SCHEDULE (UTC)", text: written };
}

export function cardHtml(n, nowS, tz = "UTC") {
  const st = statusOf(n, nowS);
  const eff = effective(n, tz);
  const id = [n.accountability, n.number].filter(Boolean).join(" ");
  const plainText = n.plain && n.plain.text ? localizeLine(n.plain.text, clockTimes(n.text), n, tz, nowS).text : "";
  const plain = plainText
    ? `<div class="nt-plain">${esc(plainText)}</div><div class="nt-origin">${esc(originText(n.plain))}</div>`
    : "";
  const differ = n.times_differ ? `<div class="nt-warn">TIMES ABOVE DIFFER FROM THE NOTAM TEXT: READ THE NOTAM</div>` : "";
  const sched = n.schedule ? scheduleText(n, tz, nowS) : null;
  const schedule = sched ? `<div class="nt-sched"><span class="k">${esc(sched.label)}</span> ${esc(sched.text)}</div>` : "";
  const long = (n.raw || "").length > LONG_TEXT ? " long" : "";
  return `<article class="nt-card ${st.kind}${long}">
    <div class="nt-top">
      <span class="nt-type">${esc(n.type_label || n.type)}</span>
      <span class="nt-id">${esc(id)}</span>
      <span class="nt-status"><b>${esc(st.label)}</b>${st.detail ? ` <span>${esc(st.detail)}</span>` : ""}</span>
    </div>
    ${plain}
    <div class="nt-raw">${esc(n.raw || n.text || "")}</div>
    ${schedule}
    <div class="nt-times">
      ${timeCell("ISSUED", fmtTime(n.issued, tz) || "not stated", ago(n.issued, nowS))}
      ${timeCell("EFFECTIVE", `${eff.from} → ${eff.until}`, eff.span ? `for ${eff.span}` : "", "wide")}
      ${timeCell("UPDATED", fmtTime(n.updated, tz) || "not stated", ago(n.updated, nowS))}
    </div>
    ${differ}
  </article>`;
}

export function listHtml(notams, nowS, tz = "UTC") {
  return groupByType(notams)
    .map(
      (g) =>
        `<h3 class="nt-group">${esc(g.label)} <span>${g.items.length}</span></h3>` +
        g.items.map((n) => cardHtml(n, nowS, tz)).join("")
    )
    .join("");
}
