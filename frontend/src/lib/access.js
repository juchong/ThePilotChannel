// Signing in, API keys and stored secrets: the texts and markup of the sign-in
// page and the admin's Access page. All rendered values are escaped.
import { esc } from "./dom.js";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export const SCOPE_TEXT = {
  control: "Control the display",
  settings: "Control the display and change settings",
};

// Where to go after signing in: the admin page, optionally one of its pages,
// and nothing else (the value comes from the address bar).
export function nextPage(search) {
  const next = new URLSearchParams(search || "").get("next") || "";
  return /^\/admin(#\/[a-z]{1,20})?$/.test(next) ? next : "/admin";
}

export function waitText(seconds) {
  const s = Math.max(1, Math.ceil(Number(seconds) || 0));
  if (s < 90) return `${s} second${s === 1 ? "" : "s"}`;
  return `${Math.ceil(s / 60)} minutes`;
}

// What to tell someone whose sign-in or account creation was refused.
export function signInError(e) {
  const said = typeof e.detail === "string" ? e.detail : "";
  if (e.status === 401) return "The username or password is not right.";
  if (e.status === 429) return `Too many failed sign-ins. Try again in ${waitText(e.retryAfter)}.`;
  if (e.status === 409) return "An account exists already. Reload this page to sign in.";
  if (e.status === 422) return said || "Check the username and the password.";
  if (e.status === 503) return said || "The secrets store cannot be read.";
  return "The display's backend is not answering. Try again in a moment.";
}

// A time in the past, for a list: "never", "just now", "5 min ago", "3 h ago", "13 Nov".
export function whenText(ts, nowS) {
  if (typeof ts !== "number") return "never";
  const age = nowS - ts;
  if (age < 60) return "just now";
  if (age < 3600) return `${Math.floor(age / 60)} min ago`;
  if (age < 48 * 3600) return `${Math.floor(age / 3600)} h ago`;
  const d = new Date(ts * 1000);
  return `${d.getDate()} ${MONTHS[d.getMonth()]}`;
}

export function keysHtml(keys, nowS) {
  if (!(keys || []).length) return `<div class="empty">No keys yet.</div>`;
  return keys
    .map(
      (k) => `<div class="row">
      <span class="name">${esc(k.name)}</span>
      <span class="mono sub">${esc(k.prefix)}…</span>
      <span class="pill">${esc(SCOPE_TEXT[k.scope] || k.scope)}</span>
      <span class="sub">${k.last_used ? "used " + esc(whenText(k.last_used, nowS)) : "never used"}</span>
      <button type="button" class="btn danger" data-revoke-key="${esc(k.id)}" aria-label="Revoke the key ${esc(k.name)}">Revoke</button>
    </div>`
    )
    .join("");
}

// The key that was just created, shown once.
export function newKeyHtml(key) {
  return `<div class="reveal" role="status">
    <div class="grow"><strong>Copy this key now. It is not shown again.</strong>
      <div class="mono" id="new-key-value">${esc(key)}</div></div>
    <button type="button" class="btn" data-copy="new-key-value">Copy</button>
  </div>`;
}

// The retired variables the environment still sets, HANGAR_CAM runs folded.
export function leftoversText(names) {
  const cams = [];
  const rest = [];
  for (const n of names || []) {
    const m = /^HANGAR_CAM(\d+)$/.exec(n);
    if (m) cams.push(Number(m[1]));
    else rest.push(n);
  }
  cams.sort((a, b) => a - b);
  const runs = [];
  for (const n of cams) {
    const last = runs[runs.length - 1];
    if (last && last[1] === n - 1) last[1] = n;
    else runs.push([n, n]);
  }
  const folded = runs.map(([a, b]) => (a === b ? `HANGAR_CAM${a}` : `HANGAR_CAM${a} to HANGAR_CAM${b}`));
  return [...rest, ...folded].join(", ");
}

// Whether a credential is stored, as a pill.
export function storedPill(entry, nowS, extra = "") {
  if (!entry || !entry.set) return `<span class="pill">Not stored</span>`;
  const when = typeof entry.changed === "number" && entry.changed > 0 ? `, ${whenText(entry.changed, nowS)}` : "";
  return `<span class="pill good">${esc(extra)}Stored${esc(when)}</span>`;
}

// The NMS-API environments, each with its own keys, by the host the FAA names for it.
export const NMS_ENVIRONMENTS = [
  ["production", "Production", "api-nms.aim.faa.gov"],
  ["staging", "Staging", "api-staging.cgifederal-aim.com"],
  ["fit", "FIT", "api-fit.cgifederal-aim.com"],
];
export const envName = (env) => (NMS_ENVIRONMENTS.find(([e]) => e === env) || [env, String(env || "")])[1];

// One row per environment: its host, whether keys are stored, and their actions.
export function nmsKeysHtml(nms, inUse, nowS) {
  return NMS_ENVIRONMENTS.map(([env, name, host]) => {
    const entry = (nms || {})[env];
    const used = env === inUse ? ` <span class="pill">In use</span>` : "";
    const actions = entry && entry.set
      ? `<button type="button" class="btn" data-nms-check="${env}">Check keys</button>` +
        `<button type="button" class="btn danger" data-nms-remove="${env}">Remove keys</button>`
      : "";
    return `<div class="row env" data-nms-env="${env}"><span class="name">${esc(name)}${used}</span>` +
      `<span class="addr mono">${esc(host)}</span>${storedPill(entry, nowS)}${actions}</div>`;
  }).join("");
}

// Whether the environment in use has keys, as a pill.
export function nmsStatePill(nms, inUse) {
  const entry = (nms || {})[inUse];
  return entry && entry.set
    ? `<span class="pill good">${esc(envName(inUse))}: keys stored</span>`
    : `<span class="pill bad">No keys for ${esc(envName(inUse))}</span>`;
}

// One line per kind of stored secret, with where to change it.
export function secretsHtml(secrets, streamCount, nowS) {
  const s = secrets || {};
  const nms = s.nms || {};
  const withKeys = NMS_ENVIRONMENTS.filter(([env]) => nms[env] && nms[env].set).map(([, name]) => name);
  const nmsPill = withKeys.length
    ? `<span class="pill good">Stored for ${esc(withKeys.join(", "))}</span>`
    : `<span class="pill">Not stored</span>`;
  const row = (label, pill, page, text) =>
    `<div class="row"><span class="name">${esc(label)}</span>${pill}<a class="btn" href="#/${page}">${esc(text)}</a></div>`;
  return (
    row("Camera stream addresses", `<span class="pill${streamCount ? " good" : ""}">${Number(streamCount) || 0} stored</span>`, "cameras", "Open Cameras") +
    row("FAA NOTAM keys", nmsPill, "notams", "Open NOTAMs") +
    row("Model endpoint API key", storedPill(s.llm_api_key, nowS), "notams", "Open NOTAMs") +
    row("airplanes.live API key", storedPill(s.aggregator_api_key, nowS), "traffic", "Open Traffic source")
  );
}
