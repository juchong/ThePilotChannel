// Tiny harness for the headless module tests. Each test page imports the code
// under test from ../src and calls check(); results go into <pre id="out"> as
// PASS/FAIL lines for run.sh, and window.__done flags completion.
const lines = [];
export function check(name, ok, detail = "") {
  lines.push(`${ok ? "PASS" : "FAIL"} ${name}${detail ? "  [" + detail + "]" : ""}`);
}
export function approx(a, b, tol) {
  return Math.abs(a - b) <= tol;
}
export function done() {
  document.getElementById("out").textContent = lines.join("\n");
  window.__done = true;
}
export function pre() {
  if (!document.getElementById("out")) {
    const p = document.createElement("pre");
    p.id = "out";
    document.body.appendChild(p);
  }
}
