#!/usr/bin/env bash
# The Pilot Channel: turn a fresh Raspberry Pi OS Lite (64-bit) install into the
# hangar kiosk. Everything the OS needs, in one idempotent script.
#
# Run it on the Pi as the user that will run the kiosk (NOT as root; it uses
# sudo where needed), from an SSH session or a keyboard:
#
#   curl -fsSL https://raw.githubusercontent.com/juchong/ThePilotChannel/main/deploy/bootstrap.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/juchong/ThePilotChannel/main/deploy/bootstrap.sh \
#       | bash -s -- --admin-password 'choose-a-password' --reboot
#
# or from a clone:  ./deploy/bootstrap.sh
#
# Every step checks its end state before acting, so re-running is safe.
# Options:
#   --check               report the state of every step, change nothing (exit 1 if any is missing)
#   --dry-run             print the commands that would run, change nothing
#   --no-upgrade          skip "apt-get full-upgrade"
#   --no-start            do not build and start the Docker stack
#   --reboot              reboot when finished
#   --admin-password PW   write HANGAR_ADMIN_PASSWORD into .env (also read from the environment)
#   --repo-dir DIR        where the repository is or should be cloned (default: ~/ThePilotChannel,
#                         or the clone this script is run from)
#
# What it sets up (see README.md "Install on the Raspberry Pi"):
#   system update; kiosk packages (cage, chromium, wlrctl, seatd, grim, imagemagick); the
#   grim wrapper that adds jpeg/webp/avif/gif screenshots; Docker CE in rootless mode as
#   this user (rootful daemon disabled); the GPU/KMS overlay; seatd and the
#   video/render/input groups; automatic login on tty1 that launches the kiosk; Wi-Fi
#   power saving off; the optional admin password; and the app stack itself.
set -euo pipefail

REPO_URL="https://github.com/juchong/ThePilotChannel.git"
APT_PACKAGES=(git curl ca-certificates cage chromium wlrctl seatd grim imagemagick uidmap dbus-user-session)
GRIM_WRAPPER=/usr/local/bin/grim
MARK_BEGIN="# >>> hangar-kiosk >>>"
MARK_END="# <<< hangar-kiosk <<<"

MODE=apply
DO_UPGRADE=1
DO_START=1
DO_REBOOT=0
ADMIN_PASSWORD="${HANGAR_ADMIN_PASSWORD:-}"
REPO_DIR=""
MISSING=0

usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE=check ;;
    --dry-run) MODE=dry-run ;;
    --no-upgrade) DO_UPGRADE=0 ;;
    --no-start) DO_START=0 ;;
    --reboot) DO_REBOOT=1 ;;
    --admin-password) ADMIN_PASSWORD="${2:-}"; shift ;;
    --repo-dir) REPO_DIR="${2:-}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# ---- helpers --------------------------------------------------------------------
log()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
ok()   { printf '    [ok]      %s\n' "$*"; }
miss() { printf '    [missing] %s\n' "$*"; MISSING=$((MISSING + 1)); }

# run a command, or print it in dry-run mode
run() {
  if [ "$MODE" = dry-run ]; then printf '    + %s\n' "$*"; else "$@"; fi
}
sudo_run() { run sudo "$@"; }
applying() { [ "$MODE" != check ]; }

pkg_installed() { [ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" = "install ok installed" ]; }
line_present() { grep -qxF "$2" "$1" 2>/dev/null; }  # exact, uncommented line
have() { command -v "$1" >/dev/null 2>&1; }

USER_NAME="$(id -un)"
USER_UID="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$USER_UID}"

# ---- 0. preflight ----------------------------------------------------------------
log "Preflight"
[ "$USER_UID" -ne 0 ] || die "run this as the user that will run the kiosk, not as root (it uses sudo where needed)"
[ "$(uname -m)" = aarch64 ] || die "this needs the 64-bit OS (Raspberry Pi OS Lite 64-bit); found $(uname -m)"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
  debian:12|debian:13|raspbian:12|raspbian:13) info "OS: ${PRETTY_NAME:-unknown} on $(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo 'unknown board')" ;;
  *) info "warning: untested OS ${PRETTY_NAME:-unknown}; continuing" ;;
esac
have sudo || die "sudo is required"
if applying && [ "$MODE" != dry-run ]; then sudo -v || die "sudo access is required"; fi

# Where is (or will be) the repository?
if [ -z "$REPO_DIR" ]; then
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd || true)"
  if [ -n "$script_dir" ] && [ -f "$script_dir/../docker-compose.yml" ]; then
    REPO_DIR="$(cd "$script_dir/.." && pwd)"
  else
    REPO_DIR="$HOME/ThePilotChannel"
  fi
fi
info "repository: $REPO_DIR"

# ---- 1. system update ---------------------------------------------------------------
log "System packages up to date"
if [ "$MODE" = check ]; then
  info "(not checked)"
elif [ "$DO_UPGRADE" = 1 ]; then
  export DEBIAN_FRONTEND=noninteractive
  sudo_run apt-get update -q
  sudo_run apt-get full-upgrade -y -q
else
  info "skipped (--no-upgrade)"
fi

# ---- 2. kiosk and prerequisite packages ---------------------------------------------
log "Kiosk packages: ${APT_PACKAGES[*]}"
missing_pkgs=()
for p in "${APT_PACKAGES[@]}"; do pkg_installed "$p" && ok "$p" || { miss "$p"; missing_pkgs+=("$p"); }; done
if applying && [ ${#missing_pkgs[@]} -gt 0 ]; then
  export DEBIAN_FRONTEND=noninteractive
  [ "$DO_UPGRADE" = 1 ] || sudo_run apt-get update -q
  sudo_run apt-get install -y -q "${missing_pkgs[@]}"
fi
if ! have chromium && have chromium-browser; then
  applying && sudo_run ln -sf "$(command -v chromium-browser)" /usr/local/bin/chromium
fi

# ---- 3. the repository ----------------------------------------------------------------
log "Repository at $REPO_DIR"
if [ -f "$REPO_DIR/docker-compose.yml" ]; then
  ok "clone present"
else
  miss "clone present"
  applying && run git clone "$REPO_URL" "$REPO_DIR"
fi
if [ -f "$REPO_DIR/deploy/kiosk-launch.sh" ]; then
  [ -x "$REPO_DIR/deploy/kiosk-launch.sh" ] && ok "kiosk launcher executable" || { miss "kiosk launcher executable"; applying && run chmod +x "$REPO_DIR/deploy/kiosk-launch.sh"; }
fi

# ---- 3b. screenshots in web formats (grim wrapper + ImageMagick) ------------------------
# Debian's grim writes only png/ppm. deploy/grim-web-wrapper.sh, installed as
# /usr/local/bin/grim (which shadows /usr/bin/grim on PATH), adds jpeg, webp, avif,
# gif, tiff, bmp, and heic by encoding grim's lossless PPM with ImageMagick. png/ppm
# and unknown options pass straight through to the real grim. Remove
# /usr/local/bin/grim to revert. WebP and AVIF/HEIC come from ImageMagick's
# extra-codecs delegate, whose package name carries the library ABI version, so
# it is resolved from the package index rather than hardcoded.
log "Screenshots in web formats (grim wrapper + ImageMagick codecs)"
EXTRA_PKG="$(apt-cache search --names-only 'libmagickcore.*extra' 2>/dev/null | awk '{print $1}' | grep -v hdri | head -1)"
EXTRA_PKG="${EXTRA_PKG:-libmagickcore-7.q16-10-extra}"
if pkg_installed "$EXTRA_PKG"; then ok "$EXTRA_PKG (webp, avif, heic codecs)"; else
  miss "$EXTRA_PKG (webp, avif, heic codecs)"
  if applying; then
    export DEBIAN_FRONTEND=noninteractive
    sudo_run apt-get install -y -q "$EXTRA_PKG"
  fi
fi
WRAPPER_SRC="$REPO_DIR/deploy/grim-web-wrapper.sh"
if [ -f "$WRAPPER_SRC" ] && cmp -s "$WRAPPER_SRC" "$GRIM_WRAPPER"; then ok "grim wrapper installed at $GRIM_WRAPPER"; else
  miss "grim wrapper installed at $GRIM_WRAPPER"
  if applying && [ -f "$WRAPPER_SRC" ]; then
    sudo_run install -m 0755 "$WRAPPER_SRC" "$GRIM_WRAPPER"
  fi
fi

# ---- 4. Docker Engine -----------------------------------------------------------------
log "Docker Engine (Docker CE with compose plugin and rootless extras)"
if have docker; then ok "docker installed ($(docker --version 2>/dev/null | head -1))"; else
  miss "docker installed"
  if applying; then
    info "installing Docker CE with the official convenience script"
    if [ "$MODE" = dry-run ]; then info "+ curl -fsSL https://get.docker.com | sudo sh"; else curl -fsSL https://get.docker.com | sudo sh; fi
  fi
fi
for p in docker-compose-plugin docker-ce-rootless-extras; do
  pkg_installed "$p" && ok "$p" || { miss "$p"; applying && sudo_run apt-get install -y -q "$p"; }
done

# ---- 5. rootless Docker for this user --------------------------------------------------
log "Rootless Docker for $USER_NAME"
# The rootful daemon is not used; it would only cost memory.
if [ "$(systemctl is-enabled docker.service 2>/dev/null || true)" = enabled ] || systemctl is-active --quiet docker.service 2>/dev/null; then
  miss "rootful docker daemon disabled"
  applying && sudo_run systemctl disable --now docker.service docker.socket
else
  ok "rootful docker daemon disabled"
fi
if grep -q "^$USER_NAME:" /etc/subuid 2>/dev/null && grep -q "^$USER_NAME:" /etc/subgid 2>/dev/null; then
  ok "subordinate uid/gid ranges"
else
  miss "subordinate uid/gid ranges"
  applying && sudo_run usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$USER_NAME"
fi
if [ -f "$HOME/.config/systemd/user/docker.service" ]; then
  ok "rootless daemon installed (user service)"
else
  miss "rootless daemon installed (user service)"
  if applying && have dockerd-rootless-setuptool.sh; then
    run dockerd-rootless-setuptool.sh install --skip-iptables
  fi
fi
if [ "$(systemctl --user is-enabled docker 2>/dev/null || true)" = enabled ] && systemctl --user is-active --quiet docker 2>/dev/null; then
  ok "rootless daemon enabled and running"
else
  miss "rootless daemon enabled and running"
  applying && run systemctl --user enable --now docker
fi
if [ "$(loginctl show-user "$USER_NAME" -p Linger --value 2>/dev/null)" = yes ]; then
  ok "user services keep running without a login (linger)"
else
  miss "user services keep running without a login (linger)"
  applying && sudo_run loginctl enable-linger "$USER_NAME"
fi
if [ "$(docker context show 2>/dev/null || true)" = rootless ]; then
  ok "docker CLI uses the rootless context"
else
  miss "docker CLI uses the rootless context"
  applying && run docker context use rootless
fi

# ---- 6. GPU / KMS ---------------------------------------------------------------------
log "GPU overlay for the kiosk (KMS)"
BOOTCFG=/boot/firmware/config.txt
[ -f "$BOOTCFG" ] || BOOTCFG=/boot/config.txt
boot_missing=()
for line in "dtoverlay=vc4-kms-v3d" "max_framebuffers=2"; do
  line_present "$BOOTCFG" "$line" && ok "$line" || { miss "$line"; boot_missing+=("$line"); }
done
if applying && [ ${#boot_missing[@]} -gt 0 ]; then
  if [ "$MODE" = dry-run ]; then
    info "+ append [all] ${boot_missing[*]} to $BOOTCFG"
  else
    { printf '\n[all]\n'; printf '%s\n' "${boot_missing[@]}"; } | sudo tee -a "$BOOTCFG" >/dev/null
  fi
fi

# ---- 7. seatd and device groups -------------------------------------------------------
log "Seat management (seatd) and device groups"
if [ "$(systemctl is-enabled seatd 2>/dev/null || true)" = enabled ]; then ok "seatd enabled"; else
  miss "seatd enabled"; applying && sudo_run systemctl enable --now seatd
fi
group_missing=()
for g in video render input; do id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$g" && ok "group $g" || { miss "group $g"; group_missing+=("$g"); }; done
if applying && [ ${#group_missing[@]} -gt 0 ]; then
  sudo_run usermod -aG "$(IFS=,; echo "${group_missing[*]}")" "$USER_NAME"
fi

# ---- 8. automatic login on tty1 that launches the kiosk --------------------------------
log "Automatic login on tty1 and kiosk launch"
DROPIN_DIR=/etc/systemd/system/getty@tty1.service.d
DROPIN="$DROPIN_DIR/autologin.conf"
WANT_DROPIN="[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $USER_NAME --noclear %I \$TERM"
if [ -f "$DROPIN" ] && [ "$(cat "$DROPIN")" = "$WANT_DROPIN" ]; then ok "getty@tty1 autologin for $USER_NAME"; else
  miss "getty@tty1 autologin for $USER_NAME"
  if applying; then
    if [ "$MODE" = dry-run ]; then info "+ write $DROPIN"; else
      sudo mkdir -p "$DROPIN_DIR"
      printf '%s\n' "$WANT_DROPIN" | sudo tee "$DROPIN" >/dev/null
      sudo systemctl daemon-reload
    fi
  fi
fi
PROFILE="$HOME/.bash_profile"
LAUNCHER="$REPO_DIR/deploy/kiosk-launch.sh"
WANT_HOOK="$MARK_BEGIN
# Launch the hangar kiosk only on the physical primary console.
if [ \"\$(tty)\" = \"/dev/tty1\" ] && [ -z \"\${WAYLAND_DISPLAY:-}\" ] && [ -z \"\${SSH_CONNECTION:-}\" ]; then
  exec $LAUNCHER
fi
$MARK_END"
if grep -qF "exec $LAUNCHER" "$PROFILE" 2>/dev/null; then ok "login shell launches the kiosk on tty1"; else
  miss "login shell launches the kiosk on tty1"
  if applying; then
    if [ "$MODE" = dry-run ]; then info "+ add kiosk block to $PROFILE"; else
      if [ -f "$PROFILE" ] && grep -qF "$MARK_BEGIN" "$PROFILE"; then
        # replace a block that points somewhere else
        sed -i "/^$(printf '%s' "$MARK_BEGIN" | sed 's/[][\.*^$/]/\\&/g')$/,/^$(printf '%s' "$MARK_END" | sed 's/[][\.*^$/]/\\&/g')$/d" "$PROFILE"
      fi
      if [ ! -f "$PROFILE" ]; then
        printf '# ~/.bash_profile\n\n# Source .bashrc for interactive (e.g. SSH) logins.\nif [ -f ~/.bashrc ]; then\n  . ~/.bashrc\nfi\n' > "$PROFILE"
      fi
      printf '\n%s\n' "$WANT_HOOK" >> "$PROFILE"
    fi
  fi
fi

# ---- 9. Wi-Fi power saving off (dropped connections on an unattended Pi) ----------------
log "Wi-Fi power saving off"
NM_CONF=/etc/NetworkManager/conf.d/tpc-wifi-powersave.conf
if have nmcli; then
  if [ -f "$NM_CONF" ]; then ok "NetworkManager wifi.powersave disabled"; else
    miss "NetworkManager wifi.powersave disabled"
    if applying; then
      if [ "$MODE" = dry-run ]; then info "+ write $NM_CONF"; else
        printf '[connection]\nwifi.powersave = 2\n' | sudo tee "$NM_CONF" >/dev/null
        sudo systemctl reload NetworkManager 2>/dev/null || true
      fi
    fi
  fi
else
  info "NetworkManager not present; skipped"
fi

# ---- 10. admin password ---------------------------------------------------------------
log "Admin password (.env)"
ENV_FILE="$REPO_DIR/.env"
if [ -n "$ADMIN_PASSWORD" ]; then
  if grep -qF "HANGAR_ADMIN_PASSWORD=$ADMIN_PASSWORD" "$ENV_FILE" 2>/dev/null; then ok "password set in $ENV_FILE"; else
    miss "password set in $ENV_FILE"
    if applying; then
      if [ "$MODE" = dry-run ]; then info "+ write $ENV_FILE"; else
        { grep -v '^HANGAR_ADMIN_PASSWORD=' "$ENV_FILE" 2>/dev/null || true; printf 'HANGAR_ADMIN_PASSWORD=%s\n' "$ADMIN_PASSWORD"; } > "$ENV_FILE.tmp"
        mv "$ENV_FILE.tmp" "$ENV_FILE" && chmod 600 "$ENV_FILE"
      fi
    fi
  fi
elif grep -q '^HANGAR_ADMIN_PASSWORD=.' "$ENV_FILE" 2>/dev/null; then
  ok "password already set in $ENV_FILE"
else
  info "no password (pass --admin-password to protect the admin page; anyone on the LAN can change settings without it)"
fi

# ---- 11. the app stack ------------------------------------------------------------------
log "App stack (docker compose)"
if curl -fs --max-time 3 http://localhost:8000/healthz >/dev/null 2>&1; then
  ok "backend answering on port 8000"
elif [ "$MODE" = check ]; then
  miss "backend answering on port 8000"
elif [ "$DO_START" = 1 ]; then
  if [ "$MODE" = dry-run ]; then info "+ (cd $REPO_DIR && docker compose up -d --build)"; else
    info "building the image and starting the stack (several minutes on a Pi 4)"
    (cd "$REPO_DIR" && docker compose up -d --build)
    for _ in $(seq 1 120); do curl -fs --max-time 3 http://localhost:8000/healthz >/dev/null 2>&1 && break; sleep 5; done
    curl -fs --max-time 3 http://localhost:8000/healthz >/dev/null 2>&1 && ok "backend answering on port 8000" || miss "backend answering on port 8000 (see: docker compose logs)"
  fi
else
  info "not started (--no-start)"
fi

# ---- summary ------------------------------------------------------------------------------
echo
if [ "$MODE" = check ]; then
  if [ "$MISSING" -eq 0 ]; then echo "All steps satisfied."; exit 0; else echo "$MISSING step(s) missing; run this script without --check to apply them."; exit 1; fi
fi
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "Done. Reboot to start the kiosk on the TV:   sudo reboot"
echo "Admin page:  http://${IP:-<pi-ip>}:8000/admin     Display:  http://${IP:-<pi-ip>}:8000"
echo "Kiosk log:   /dev/shm/hangar-kiosk/hangar-kiosk.log   App log:  docker compose logs -f"
echo "Re-run with --check any time to verify the setup."
if [ "$DO_REBOOT" = 1 ] && [ "$MODE" = apply ]; then sleep 3; sudo reboot; fi
