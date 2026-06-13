#!/usr/bin/env bash
# Launch the Chromium kiosk under the cage Wayland compositor.
# Invoked from the autologin session on tty1 (which owns seat0 / DRM master).
set -u

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

# Wait for the dockerized backend to be serving before opening the browser.
echo "waiting for backend health..."
until curl -sf http://localhost:8000/healthz >/dev/null 2>&1; do sleep 2; done
echo "backend healthy, starting cage + chromium"

CHROME_BIN="$(command -v chromium || command -v chromium-browser)"

exec cage -- "$CHROME_BIN" \
  --kiosk \
  --ozone-platform=wayland \
  --user-data-dir="$PROFILE_DIR" \
  --disk-cache-dir="$CACHE_DIR" \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --disable-translate \
  --disable-features=TranslateUI \
  --check-for-update-interval=31536000 \
  --password-store=basic \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  http://localhost:8000
