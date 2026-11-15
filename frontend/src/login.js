// The sign-in page: signs in, or creates the account on a display that has none.
import { api } from "./lib/api.js";
import { nextPage, signInError } from "./lib/access.js";
import { el } from "./lib/dom.js";

const next = nextPage(location.search);

function show(id, on = true) {
  el(id).hidden = !on;
}

function fail(id, text) {
  el(id).textContent = text;
  show(id);
}

async function boot() {
  let s;
  try {
    s = await api.getSession();
  } catch (e) {
    show("signin");
    return fail("si-error", "The display's backend is not answering. Try again in a moment.");
  }
  if (s.authenticated) return location.replace(next);
  if (s.locked) {
    el("gate-locked-text").textContent = s.detail || "The secrets store cannot be read.";
    return show("gate-locked");
  }
  if (s.setup_required) {
    el("gate-title").textContent = "Create the admin account";
    show("gate-lead");
    show("setup");
    el("fv-user").focus();
  } else {
    show("signin");
    show("gate-foot");
    el("si-user").focus();
  }
}

el("signin").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  show("si-error", false);
  const username = el("si-user").value.trim();
  const password = el("si-pass").value;
  if (!username || !password) return fail("si-error", "Enter the username and the password.");
  try {
    await api.login(username, password);
    location.replace(next);
  } catch (e) {
    fail("si-error", signInError(e));
  }
});

el("setup").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  show("fv-error", false);
  const username = el("fv-user").value.trim();
  const password = el("fv-pass").value;
  if (!el("fv-user").checkValidity()) return fail("fv-error", "Username: 1 to 32 letters, digits, '.', '_' or '-'.");
  if (password.length < 8) return fail("fv-error", "The password needs at least 8 characters.");
  if (password !== el("fv-again").value) return fail("fv-error", "The two passwords differ.");
  try {
    await api.setup(username, password);
    location.replace(next);
  } catch (e) {
    fail("fv-error", signInError(e));
  }
});

boot();
