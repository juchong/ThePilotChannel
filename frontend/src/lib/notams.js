// NOTAM view: time formatting, status, grouping, paging arithmetic and card
// markup. Every date and time is rendered in UTC with the Z designator, from
// the epoch seconds the backend passes through from the FAA. All text is
// escaped.
import { esc } from "./dom.js";

const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
const two = (n) => String(n).padStart(2, "0");

export const MAX_PAGES = 8;        // pages shown in one visit of a NOTAM view
export const LONG_TEXT = 700;      // a NOTAM longer than this may continue in the next column

// 1794160800 -> "08 NOV 2026 1500Z". null when there is no time.
export function fmtZ(ts) {
  if (typeof ts !== "number" || !isFinite(ts)) return null;
  const d = new Date(ts * 1000);
  return `${two(d.getUTCDate())} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()} ${two(d.getUTCHours())}${two(d.getUTCMinutes())}Z`;
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
export function effective(n) {
  const from = fmtZ(n.start) || "not stated";
  let until = "not stated";
  if (n.permanent) until = "PERMANENT";
  else if (typeof n.end === "number") until = fmtZ(n.end) + (n.end_estimated ? " EST" : "");
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

export function cardHtml(n, nowS) {
  const st = statusOf(n, nowS);
  const eff = effective(n);
  const id = [n.accountability, n.number].filter(Boolean).join(" ");
  const plain = n.plain && n.plain.text
    ? `<div class="nt-plain">${esc(n.plain.text)}</div><div class="nt-origin">${esc(originText(n.plain))}</div>`
    : "";
  const differ = n.times_differ ? `<div class="nt-warn">TIMES ABOVE DIFFER FROM THE NOTAM TEXT: READ THE NOTAM</div>` : "";
  const schedule = n.schedule ? `<div class="nt-sched"><span class="k">SCHEDULE (Z)</span> ${esc(n.schedule)}</div>` : "";
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
      ${timeCell("ISSUED", fmtZ(n.issued) || "not stated", ago(n.issued, nowS))}
      ${timeCell("EFFECTIVE", `${eff.from} → ${eff.until}`, eff.span ? `for ${eff.span}` : "", "wide")}
      ${timeCell("UPDATED", fmtZ(n.updated) || "not stated", ago(n.updated, nowS))}
    </div>
    ${differ}
  </article>`;
}

export function listHtml(notams, nowS) {
  return groupByType(notams)
    .map(
      (g) =>
        `<h3 class="nt-group">${esc(g.label)} <span>${g.items.length}</span></h3>` +
        g.items.map((n) => cardHtml(n, nowS)).join("")
    )
    .join("");
}
