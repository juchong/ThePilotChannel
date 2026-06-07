#!/usr/bin/env bash
# Provision the native kiosk layer on the Pi (Debian Trixie / Raspberry Pi OS Lite).
# The app stack runs in Docker; this sets up auto-login on tty1 that launches
# cage + Chromium fullscreen. Auto-login is required so the session owns seat0
# and Chromium/cage can become DRM master on the HDMI output.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="${SUDO_USER:-$USER}"
UID_NUM="$(id -u "$USER_NAME")"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"

echo "==> Installing kiosk packages (cage, chromium, curl, seatd)"
sudo apt-get update
sudo apt-get install -y cage chromium curl seatd
if ! command -v chromium >/dev/null 2>&1 && command -v chromium-browser >/dev/null 2>&1; then
  sudo ln -sf "$(command -v chromium-browser)" /usr/local/bin/chromium
fi
sudo systemctl enable --now seatd

echo "==> Enabling vc4-kms-v3d GPU overlay"
BOOTCFG=/boot/firmware/config.txt
[ -f "$BOOTCFG" ] || BOOTCFG=/boot/config.txt
if ! grep -q "^dtoverlay=vc4-kms-v3d" "$BOOTCFG"; then
  echo "dtoverlay=vc4-kms-v3d" | sudo tee -a "$BOOTCFG" >/dev/null
fi

echo "==> Configuring auto-login on tty1 for $USER_NAME"
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
sudo cp "$REPO_DIR/deploy/getty-autologin.conf" /etc/systemd/system/getty@tty1.service.d/autologin.conf
sudo sed -i "s/--autologin pi/--autologin $USER_NAME/" /etc/systemd/system/getty@tty1.service.d/autologin.conf

echo "==> Installing kiosk launcher into the login shell"
chmod +x "$REPO_DIR/deploy/kiosk-launch.sh"
PROFILE="$HOME_DIR/.bash_profile"
MARK="# >>> hangar-kiosk >>>"
if ! grep -qF "$MARK" "$PROFILE" 2>/dev/null; then
  cat >>"$PROFILE" <<EOF

$MARK
# Launch the hangar kiosk only on the physical primary console.
if [ "\$(tty)" = "/dev/tty1" ] && [ -z "\${WAYLAND_DISPLAY:-}" ] && [ -z "\${SSH_CONNECTION:-}" ]; then
  exec $REPO_DIR/deploy/kiosk-launch.sh
fi
# <<< hangar-kiosk <<<
EOF
fi

echo "==> Disabling the legacy graphical-target unit if present"
sudo systemctl disable hangar-kiosk.service 2>/dev/null || true

echo "==> Bringing up the dockerized app stack"
( cd "$REPO_DIR" && docker compose up -d --build )

sudo systemctl daemon-reload
echo
echo "Done. Reboot to start the kiosk:  sudo reboot"
echo "Or start it now without reboot:   sudo systemctl restart getty@tty1"
echo "Admin UI: http://<pi-ip>:8000/admin   |   kiosk log: /dev/shm/hangar-kiosk/hangar-kiosk.log"
