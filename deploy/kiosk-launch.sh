#!/usr/bin/env bash
# Launch the Chromium kiosk under the cage Wayland compositor.
# Invoked from the autologin session on tty1 (which owns seat0 / DRM master).
set -u

# If cage exits immediately and repeatedly, getty@tty1 would trip systemd's
# start-rate limit (5 starts in 10 s) and stop respawning the kiosk. A short
# pause keeps a crash loop under that limit.
sleep 2

# The OS runs from an SD card, so keep all of Chromium's churning writes (profile,
# disk cache, GPU/shader cache) and the kiosk log on a RAM disk (/dev/shm, tmpfs)
# to minimize SD wear. These are all disposable across reboots.
RAMDIR=/dev/shm/hangar-kiosk
PROFILE_DIR="$RAMDIR/profile"
CACHE_DIR="$RAMDIR/cache"
mkdir -p "$PROFILE_DIR" "$CACHE_DIR"

LOG="$RAMDIR/hangar-kiosk.log"
exec >>"$LOG" 2>&1
echo "=== kiosk launch $(date) ==="

APP_URL="http://localhost:8000/"

# Ensure a runtime dir exists for Wayland.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# Park the pointer in the bottom-right corner so it stays out of view. cage warps
# the cursor to screen center on startup; once cage is up, a large relative move
# clamps the pointer to the far corner. Runs in the background because we exec cage.
# WAYLAND_DISPLAY is scoped to this subshell only: setting it in cage's own env
# would make cage try to run nested as a Wayland client instead of using DRM.
(
  export WAYLAND_DISPLAY=wayland-0
  for _ in $(seq 1 150); do
    [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] && break
    sleep 0.1
  done
  sleep 1
  wlrctl pointer move 100000 100000
) &

# A branded holding page shown while Docker and the backend come up, instead of
# a black TV. It probes /healthz and navigates to the display as soon as the
# backend answers; if the backend is unreachable it just keeps waiting.
WAIT_PAGE="$RAMDIR/waiting.html"
cat >"$WAIT_PAGE" <<HTML
<!doctype html><html lang="en"><head><meta charset="utf-8"><title>The Pilot Channel</title>
<style>
html,body{margin:0;height:100%;background:#0b0f14;color:#8b98a5;font-family:ui-sans-serif,system-ui,sans-serif;cursor:none}
.c{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px}
.b{font-weight:800;letter-spacing:4px;font-size:34px;background:linear-gradient(90deg,#38bdf8,#818cf8);-webkit-background-clip:text;background-clip:text;color:transparent}
.s{font-size:20px}
</style></head><body><div class="c"><div class="b">THE PILOT CHANNEL</div><div class="s" id="s">Starting…</div></div>
<script>
const APP = "$APP_URL";
let n = 0;
async function probe() {
  n++;
  try {
    await fetch(APP + "healthz", { mode: "no-cors", cache: "no-store" });
    location.replace(APP);
    return;
  } catch (e) {}
  document.getElementById("s").textContent = "Waiting for the backend to start… (" + n + ")";
  setTimeout(probe, 2000);
}
probe();
</script></body></html>
HTML

CHROME_BIN="$(command -v chromium || command -v chromium-browser)"
echo "starting cage + chromium ($CHROME_BIN)"

# Flags after --kiosk turn off Chromium background services (update checks,
# sync, Google push connections, component updates) that a kiosk never needs and
# that otherwise cost CPU and network on the Pi.
exec cage -- "$CHROME_BIN" \
  --kiosk \
  --ozone-platform=wayland \
  --user-data-dir="$PROFILE_DIR" \
  --disk-cache-dir="$CACHE_DIR" \
  --disk-cache-size=104857600 \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --no-default-browser-check \
  --disable-translate \
  --disable-features=TranslateUI,MediaRouter,OptimizationHints \
  --disable-background-networking \
  --disable-component-update \
  --disable-sync \
  --disable-default-apps \
  --disable-breakpad \
  --check-for-update-interval=31536000 \
  --password-store=basic \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  "file://$WAIT_PAGE"
