// The overview's health cards, from /api/status: one card per feed, each with
// what it is doing and one pill that says whether that is all right.
import { esc } from "./dom.js";

export function fmtUptime(s) {
  if (typeof s !== "number") return "";
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} days`;
}

export function fmtAgo(seconds) {
  if (typeof seconds !== "number" || seconds < 0) return "";
  if (seconds < 90) return "just now";
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 48 * 3600) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

const cap = (text) => (text ? text[0].toUpperCase() + text.slice(1) : "");
const short = (text) => (String(text).length > 90 ? String(text).slice(0, 88) + "…" : String(text));
const pill = (text, kind = "") => ({ text, kind });

function traffic(s) {
  if (!s.active) return { label: "Traffic", value: "No traffic yet", pill: pill("Waiting for the first view") };
  return {
    label: "Traffic",
    value: s.active === "local" ? "Local receiver" : `Aggregator ${s.active}`,
    pill: s.healthy ? pill("Healthy", "good") : pill(short(s.last_error || "Failing"), "bad"),
  };
}

function weather(s, nowS) {
  const w = s.weather || {};
  const g = w.gateway || {};
  const value = w.last_refresh_ts ? `Refreshed ${fmtAgo(nowS - w.last_refresh_ts)}` : "Not refreshed yet";
  const problem = w.error || g.error;
  if (problem) return { label: "Weather", value, pill: pill(short(problem), "bad") };
  if ((g.stations || []).length) {
    return { label: "Weather", value, pill: pill(`${g.stations.join(", ")} from the Weather Service files`) };
  }
  return { label: "Weather", value, pill: pill(`${w.stations || 0} stations`, w.last_refresh_ts ? "good" : "") };
}

function cameras(s) {
  const c = s.cameras || {};
  if (!c.enabled) return { label: "Cameras", value: "View off", pill: pill("Not in the rotation") };
  const value = `${c.streams || 0} stream${c.streams === 1 ? "" : "s"}, ${c.tiles || 0} tile${c.tiles === 1 ? "" : "s"}`;
  if (c.relay !== "ok") return { label: "Cameras", value, pill: pill("Relay not answering", "bad") };
  if ((c.unknown_streams || []).length) {
    return { label: "Cameras", value, pill: pill(`Tiles without a stream: ${c.unknown_streams.join(", ")}`, "warn") };
  }
  if ((c.pending_streams || []).length) return { label: "Cameras", value, pill: pill("Relay catching up", "warn") };
  return { label: "Cameras", value, pill: pill("Relay in step", "good") };
}

function notams(s) {
  const n = s.notams || {};
  if (!n.enabled) return { label: "NOTAMs", value: "Views off", pill: pill("Not in the rotation") };
  if (n.source === "sample") return { label: "NOTAMs", value: "Sample data", pill: pill("Not real NOTAMs", "warn") };
  if (n.blocked) return { label: "NOTAMs", value: "Views held back", pill: pill("Needs the model rewrite", "warn") };
  const value = `${n.locations || 0} location${n.locations === 1 ? "" : "s"}, ${cap(n.environment || "")}`;
  if (!n.credentials) return { label: "NOTAMs", value, pill: pill(`No FAA keys for ${n.environment}`, "bad") };
  if (n.rate_limited) return { label: "NOTAMs", value, pill: pill("The FAA is limiting requests", "warn") };
  if (n.error) return { label: "NOTAMs", value, pill: pill(short(n.error), "bad") };
  if (n.test_environment) return { label: "NOTAMs", value, pill: pill("Test environment", "warn") };
  return { label: "NOTAMs", value, pill: pill("Up to date", "good") };
}

// The requests sent to the FAA in the last 24 hours, against its limits.
export function usageCard(u) {
  const label = "FAA pulls, last 24 h";
  if (!u || !u.pulls) return { label, value: "No pulls", pill: pill("Nothing asked of the FAA") };
  const value = `${u.pulls} pull${u.pulls === 1 ? "" : "s"}`;
  if (u.status_429) return { label, value, pill: pill(`${u.status_429} refused for pace (429)`, "bad") };
  if (u.shortest_gap_s != null && u.shortest_gap_s < u.limit_gap_s) {
    return { label, value, pill: pill(`Shortest interval ${u.shortest_gap_s} s, under the ${u.limit_gap_s} s limit`, "bad") };
  }
  if (u.mismatches) return { label, value, pill: pill(`${u.mismatches} missed by pulls of changes`, "warn") };
  if (u.shortest_gap_s == null) return { label, value, pill: pill("None refused", "good") };
  return { label, value, pill: pill(`Shortest interval ${u.shortest_gap_s} s`, "good") };
}

function backend(s) {
  const c = s.config || {};
  const value = `Up ${fmtUptime(s.uptime_s)}`;
  if (c.error) return { label: "Backend", value, pill: pill("config.yaml could not be read", "bad") };
  return { label: "Backend", value, pill: pill("Settings read from config.yaml", "good") };
}

export function healthCards(status, nowS) {
  const s = status || {};
  const cards = [traffic(s), weather(s, nowS), cameras(s), notams(s)];
  if ((s.notams || {}).enabled && (s.notams || {}).source === "faa") cards.push(usageCard((s.notams || {}).usage));
  cards.push(backend(s));
  return cards;
}

export function cardsHtml(cards) {
  return cards
    .map(
      (c) => `<div class="card"><span class="sub">${esc(c.label)}</span><h2>${esc(c.value)}</h2>
      <div><span class="pill ${esc(c.pill.kind)}">${esc(c.pill.text)}</span></div></div>`
    )
    .join("");
}
