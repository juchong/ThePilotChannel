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
