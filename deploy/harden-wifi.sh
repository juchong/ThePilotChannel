#!/usr/bin/env bash
# The Pilot Channel: keep the Pi's Wi-Fi settings intact through power cuts.
#
# On Raspberry Pi OS (Debian 13) the Wi-Fi details from Raspberry Pi Imager reach
# NetworkManager through cloud-init as netplan files, which Raspberry Pi's NetworkManager
# rewrites at every start without flushing; a power cut during boot can leave them empty
# (https://github.com/raspberrypi/trixie-feedback/issues/99). Native keyfiles are not
# rewritten. This script, run by deploy/bootstrap.sh:
#
#   - copies each netplan-generated profile to a keyfile in
#     /etc/NetworkManager/system-connections under the same UUID, so the connection
#     stays up, and moves the netplan files to /var/backups/wifi-hardening;
#   - recreates Wi-Fi from the Imager settings in /boot/firmware/network-config when no
#     Wi-Fi profile is left at all;
#   - makes each Wi-Fi profile retry forever and keep the hardware MAC address, which
#     DHCP reservations depend on;
#   - keeps a read-only copy of each Wi-Fi profile, same UUID, in
#     /usr/lib/NetworkManager/system-connections. NetworkManager's service sandbox
#     (ProtectSystem=yes) cannot write there, and NetworkManager serves the profile from
#     that copy whenever the /etc copy is missing or unreadable;
#   - stops cloud-init from writing network settings again.
#
# Run it again after adding a Wi-Fi network or changing a password so the read-only
# copies match. To remove a network for good, delete it with nmcli or nmtui and also
# delete its /usr/lib/NetworkManager/system-connections/hardened-<uuid>.nmconnection.
#
# Usage:
#   sudo deploy/harden-wifi.sh                  apply; every step checks first, so re-running is safe
#   deploy/harden-wifi.sh --check               report, change nothing (exit 1 if anything is missing)
#   sudo deploy/harden-wifi.sh --dry-run        print what would change
#   sudo deploy/harden-wifi.sh --test-fallback  hide the /etc copies, reconnect Wi-Fi from the
#                                               read-only copies, then put everything back
set -euo pipefail

ETC_DIR=/etc/NetworkManager/system-connections
LIB_DIR=/usr/lib/NetworkManager/system-connections
RUN_DIR=/run/NetworkManager/system-connections
NETPLAN_DIR=/etc/netplan
SEED=/boot/firmware/network-config
CLOUD_CFG=/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg
BACKUP_DIR="/var/backups/wifi-hardening/$(date +%Y%m%d-%H%M%S)"
MISSING=0

case "${1:-}" in
  "") MODE=apply ;;
  --check) MODE=check ;;
  --dry-run) MODE=dry-run ;;
  --test-fallback) MODE=test-fallback ;;
  -h | --help)
    sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d; s/^# \{0,1\}//'
    exit 0
    ;;
  *)
    echo "unknown option: $1 (see --help)" >&2
    exit 2
    ;;
esac

info() { printf '    %s\n' "$*"; }
ok() { printf '    [ok]      %s\n' "$*"; }
miss() {
  printf '    [missing] %s\n' "$*"
  MISSING=$((MISSING + 1))
}
die() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}
dry() { [ "$MODE" = dry-run ]; }

# Prints "REMOVE<TAB>path" or "KEEP<TAB>path<TAB>reason" for each netplan file given
# after the first argument (a file listing the netplan-generated keyfiles already
# copied to /etc). A file can go once every network it defines has been copied.
read -r -d '' CLASSIFY_PY <<'PY' || true
import sys

try:
    import yaml
except ImportError:
    sys.exit("python3-yaml is required: sudo apt install python3-yaml")

with open(sys.argv[1], encoding="utf-8") as f:
    copied = [line.rstrip("\n") for line in f if line.strip()]


def was_copied(netdef):
    return any(name == f"netplan-{netdef}.nmconnection" or name.startswith(f"netplan-{netdef}-")
               for name in copied)


for path in sys.argv[2:]:
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.load(f, Loader=yaml.BaseLoader)
    except OSError as e:
        print(f"KEEP\t{path}\tunreadable ({e.strerror})")
        continue
    except yaml.YAMLError:
        print(f"KEEP\t{path}\tnot valid YAML")
        continue
    if doc is None:
        net = {}  # empty, for example truncated by a power cut
    elif isinstance(doc, dict) and isinstance(doc.get("network") or {}, dict):
        net = doc.get("network") or {}
    else:
        print(f"KEEP\t{path}\tunexpected structure")
        continue
    left = [f"{kind}/{name}" for kind, defs in net.items() if isinstance(defs, dict)
            for name in defs if not was_copied(name)]
    print(f"KEEP\t{path}\tnot copied: {', '.join(left)}" if left else f"REMOVE\t{path}")
PY

# Prints each Wi-Fi network in a cloud-init network-config file as base64 fields
# "ssid<TAB>password", or only the SSID with --ssids.
read -r -d '' SEED_PY <<'PY' || true
import base64
import sys

try:
    import yaml
except ImportError:
    sys.exit("python3-yaml is required: sudo apt install python3-yaml")

ssids_only = sys.argv[1] == "--ssids"
with open(sys.argv[2], encoding="utf-8") as f:
    doc = yaml.load(f, Loader=yaml.BaseLoader)
net = doc.get("network", doc) if isinstance(doc, dict) else {}
wifis = net.get("wifis") if isinstance(net, dict) else None
for nd in (wifis.values() if isinstance(wifis, dict) else []):
    aps = nd.get("access-points") if isinstance(nd, dict) else None
    for ssid, ap in (aps.items() if isinstance(aps, dict) else []):
        ap = ap if isinstance(ap, dict) else {}
        auth = ap.get("auth") if isinstance(ap.get("auth"), dict) else {}
        psk = ap.get("password", auth.get("password"))
        if psk:
            fields = [ssid] if ssids_only else [ssid, psk]
            print("\t".join(base64.b64encode(x.encode()).decode() for x in fields))
PY

# "uuid<TAB>type<TAB>file" for every profile NetworkManager knows.
profiles() {
  local uuid kind file
  nmcli -t -f UUID,TYPE,FILENAME connection show </dev/null |
    while IFS=: read -r uuid kind file; do printf '%s\t%s\t%s\n' "$uuid" "$kind" "$file"; done
}

# "uuid<TAB>file" for every Wi-Fi profile.
wifi_profiles() {
  local uuid kind file
  while IFS=$'\t' read -r uuid kind file; do
    if [ "$kind" = 802-11-wireless ]; then printf '%s\t%s\n' "$uuid" "$file"; fi
  done < <(profiles)
}

# The file NetworkManager serves a profile from (empty if the profile is hidden).
served_from() {
  local uuid kind file
  while IFS=$'\t' read -r uuid kind file; do
    if [ "$uuid" = "$1" ]; then printf '%s\n' "$file"; fi
  done < <(profiles)
}

prop() { nmcli -g "$2" connection show "$1" </dev/null; }
safe_name() { printf '%s' "$1" | tr -c 'A-Za-z0-9._ -' '_'; }

# Replace $1 with stdin atomically: a temp file in the same directory, flushed to
# disk, then renamed over the target.
write_file() {
  local dst=$1 mode=${2:-0600} dir tmp
  dir=$(dirname "$dst")
  tmp=$(mktemp "$dir/.harden-wifi-XXXXXX")
  cat >"$tmp"
  chmod "$mode" "$tmp"
  sync "$tmp"
  mv -f -- "$tmp" "$dst"
  sync "$dir"
}

convert_netplan() {
  local list=() p uuid src name dst copied yamls=() lines=() line kind path reason remove=()
  mapfile -t list < <(profiles)
  copied=$(mktemp)
  for p in "${list[@]}"; do
    IFS=$'\t' read -r uuid _ src <<<"$p"
    [[ $src == "$RUN_DIR"/netplan-* ]] || continue
    name=$(prop "$uuid" connection.id)
    dst="$ETC_DIR/$(safe_name "$name").nmconnection"
    [ ! -e "$dst" ] || dst="$ETC_DIR/$(safe_name "$name")-${uuid:0:8}.nmconnection"
    if dry; then
      info "would copy netplan profile \"$name\" to $dst (same UUID, so it stays connected)"
    elif grep -q '^uuid=' "$src"; then
      write_file "$dst" <"$src"
      info "copied netplan profile \"$name\" to $dst"
    else
      sed "/^\[connection\]\$/a uuid=$uuid" "$src" | write_file "$dst"
      info "copied netplan profile \"$name\" to $dst"
    fi
    printf '%s\n' "${src##*/}" >>"$copied"
  done
  shopt -s nullglob
  yamls=("$NETPLAN_DIR"/*.yaml "$NETPLAN_DIR"/*.yml)
  shopt -u nullglob
  if [ ${#yamls[@]} -gt 0 ]; then
    mapfile -t lines < <(python3 -c "$CLASSIFY_PY" "$copied" "${yamls[@]}" && echo END)
    if [ "${lines[-1]:-}" != END ]; then
      rm -f "$copied"
      die "could not read $NETPLAN_DIR"
    fi
    unset 'lines[-1]'
    for line in "${lines[@]}"; do
      IFS=$'\t' read -r kind path reason <<<"$line"
      if [ "$kind" = REMOVE ]; then remove+=("$path"); else info "leaving $path in place: $reason"; fi
    done
  fi
  rm -f "$copied"
  if [ ${#remove[@]} -eq 0 ]; then return 0; fi
  if dry; then
    info "would move ${remove[*]##*/} to $BACKUP_DIR"
    return 0
  fi
  install -d -m 0700 "$BACKUP_DIR"
  mv -- "${remove[@]}" "$BACKUP_DIR"/
  if command -v netplan >/dev/null; then netplan generate; fi
  nmcli connection reload </dev/null
  info "moved ${#remove[@]} netplan file(s) to $BACKUP_DIR"
}

# A profile served only from its read-only copy gets an /etc copy back.
restore_etc_copies() {
  local lib uuid file
  for lib in "$LIB_DIR"/hardened-*.nmconnection; do
    [ -e "$lib" ] || continue
    uuid=${lib##*/hardened-}
    uuid=${uuid%.nmconnection}
    file=$(served_from "$uuid")
    if [ "$file" = "$lib" ]; then
      if dry; then
        info "would restore the /etc copy of $uuid from $lib"
      else
        write_file "$ETC_DIR/hardened-$uuid.nmconnection" <"$lib"
        nmcli connection reload </dev/null
        info "restored the /etc copy of $uuid from $lib"
      fi
    elif [ -z "$file" ]; then
      info "$uuid is hidden (deleted with nmcli?); delete $lib to remove it for good"
    fi
  done
}

create_from_seed() {
  local lines=() line ssid psk
  if [ -n "$(wifi_profiles)" ] || [ ! -r "$SEED" ]; then return 0; fi
  mapfile -t lines < <(python3 -c "$SEED_PY" --all "$SEED" && echo END)
  [ "${lines[-1]:-}" = END ] || die "could not read $SEED"
  unset 'lines[-1]'
  for line in "${lines[@]}"; do
    IFS=$'\t' read -r ssid psk <<<"$line"
    ssid=$(base64 -d <<<"$ssid")
    psk=$(base64 -d <<<"$psk")
    if dry; then
      info "would recreate Wi-Fi \"$ssid\" from $SEED"
      continue
    fi
    nmcli connection add type wifi con-name "$ssid" ssid "$ssid" \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$psk" </dev/null >/dev/null ||
      die "NetworkManager rejected Wi-Fi \"$ssid\" from $SEED"
    info "recreated Wi-Fi \"$ssid\" from $SEED"
  done
}

install_copy() {
  local uuid=$1 src=$2 name=$3
  local dst="$LIB_DIR/hardened-$uuid.nmconnection"
  if [ -f "$dst" ] && cmp -s -- "$src" "$dst"; then
    if [ "$src" -nt "$dst" ] && ! dry; then touch -- "$dst"; fi
    return 0
  fi
  if dry && [ -f "$dst" ] && [ ! -r "$src" ]; then
    info "would refresh the read-only copy of \"$name\" if it differs (sudo is needed to compare)"
    return 0
  elif dry; then
    info "would save a read-only copy of \"$name\" to $dst"
    return 0
  fi
  install -d -m 0755 "$LIB_DIR"
  write_file "$dst" <"$src"
  info "saved a read-only copy of \"$name\" to $dst"
}

harden_profiles() {
  local list=() p uuid file name
  mapfile -t list < <(wifi_profiles)
  for p in "${list[@]}"; do
    IFS=$'\t' read -r uuid file <<<"$p"
    [[ $file == "$ETC_DIR"/* ]] || continue
    name=$(prop "$uuid" connection.id)
    if [ "$(prop "$uuid" connection.autoconnect)" != yes ] ||
      [ "$(prop "$uuid" connection.autoconnect-retries)" != 0 ] ||
      [ "$(prop "$uuid" 802-11-wireless.cloned-mac-address)" != permanent ]; then
      if dry; then
        info "would make \"$name\" retry forever and keep the hardware MAC address"
      else
        nmcli connection modify "$uuid" connection.autoconnect yes \
          connection.autoconnect-retries 0 802-11-wireless.cloned-mac-address permanent </dev/null
        file=$(served_from "$uuid")
        [[ $file == "$ETC_DIR"/* ]] || die "\"$name\" moved to '${file:-nowhere}' after the change; stopping"
        info "\"$name\" now retries forever and keeps the hardware MAC address"
      fi
    fi
    install_copy "$uuid" "$file" "$name"
  done
}

disable_cloud_init_network() {
  if [ ! -d /etc/cloud/cloud.cfg.d ] || grep -qs 'config: *disabled' "$CLOUD_CFG"; then return 0; fi
  if dry; then
    info "would write $CLOUD_CFG"
    return 0
  fi
  printf '%s\n' '# Network settings are kept out of cloud-init; see deploy/harden-wifi.sh.' \
    'network: {config: disabled}' | write_file "$CLOUD_CFG" 0644
  info "cloud-init no longer writes network settings ($CLOUD_CFG)"
}

report() {
  local yamls=() list=() p uuid file name lib n=0
  shopt -s nullglob
  yamls=("$NETPLAN_DIR"/*.yaml "$NETPLAN_DIR"/*.yml)
  shopt -u nullglob
  if [ ${#yamls[@]} -eq 0 ]; then
    ok "no netplan profiles for NetworkManager to rewrite at boot"
  else
    miss "no netplan profiles for NetworkManager to rewrite at boot (found ${yamls[*]##*/})"
  fi
  mapfile -t list < <(wifi_profiles)
  for p in "${list[@]}"; do
    IFS=$'\t' read -r uuid file <<<"$p"
    n=$((n + 1))
    name=$(prop "$uuid" connection.id)
    lib="$LIB_DIR/hardened-$uuid.nmconnection"
    if [[ $file == "$ETC_DIR"/* ]]; then
      ok "Wi-Fi \"$name\" is a keyfile in $ETC_DIR"
    else
      miss "Wi-Fi \"$name\" is a keyfile in $ETC_DIR (served from $file)"
    fi
    if [ ! -e "$lib" ]; then
      miss "Wi-Fi \"$name\" has a read-only copy in $LIB_DIR"
    elif [[ $file == "$ETC_DIR"/* && $file -nt $lib ]]; then
      miss "Wi-Fi \"$name\" has an up-to-date read-only copy (the profile changed after it)"
    else
      ok "Wi-Fi \"$name\" has a read-only copy in $LIB_DIR"
    fi
    if [ "$(prop "$uuid" connection.autoconnect)" = yes ] &&
      [ "$(prop "$uuid" connection.autoconnect-retries)" = 0 ] &&
      [ "$(prop "$uuid" 802-11-wireless.cloned-mac-address)" = permanent ]; then
      ok "Wi-Fi \"$name\" retries forever and keeps the hardware MAC address"
    else
      miss "Wi-Fi \"$name\" retries forever and keeps the hardware MAC address"
    fi
  done
  if [ "$n" -eq 0 ]; then
    if [ -r "$SEED" ] && [ -n "$(python3 -c "$SEED_PY" --ssids "$SEED" 2>/dev/null)" ]; then
      miss "a Wi-Fi profile (the Imager settings in $SEED include Wi-Fi)"
    else
      ok "no Wi-Fi configured (wired only)"
    fi
  fi
  if [ -d /etc/cloud/cloud.cfg.d ]; then
    if grep -qs 'config: *disabled' "$CLOUD_CFG"; then
      ok "cloud-init leaves network settings alone"
    else
      miss "cloud-init leaves network settings alone ($CLOUD_CFG)"
    fi
  fi
}

# "uuid<TAB>/etc path" of each profile hidden by --test-fallback.
TEST_FILES=()
put_back() {
  local p uuid file
  for p in "${TEST_FILES[@]}"; do
    IFS=$'\t' read -r uuid file <<<"$p"
    if [ -e "$BACKUP_DIR/${file##*/}" ]; then mv -f -- "$BACKUP_DIR/${file##*/}" "$file"; fi
  done
  nmcli connection reload </dev/null || true
  rmdir "$BACKUP_DIR" 2>/dev/null || true
}

test_fallback() {
  local list=() p uuid file name
  mapfile -t list < <(wifi_profiles)
  for p in "${list[@]}"; do
    IFS=$'\t' read -r uuid file <<<"$p"
    [[ $file == "$ETC_DIR"/* ]] || continue
    [ -e "$LIB_DIR/hardened-$uuid.nmconnection" ] ||
      die "Wi-Fi $uuid has no read-only copy yet; run this script without options first"
    TEST_FILES+=("$p")
  done
  [ ${#TEST_FILES[@]} -gt 0 ] || die "no Wi-Fi keyfiles in $ETC_DIR to test"
  install -d -m 0700 "$BACKUP_DIR"
  trap put_back EXIT
  trap 'put_back; exit 1' HUP INT TERM
  info "hiding the /etc copies; Wi-Fi drops for a few seconds"
  for p in "${TEST_FILES[@]}"; do
    IFS=$'\t' read -r uuid file <<<"$p"
    mv -- "$file" "$BACKUP_DIR"/
  done
  nmcli connection reload </dev/null
  for p in "${TEST_FILES[@]}"; do
    IFS=$'\t' read -r uuid file <<<"$p"
    name=$(prop "$uuid" connection.id)
    if [ "$(served_from "$uuid")" != "$LIB_DIR/hardened-$uuid.nmconnection" ]; then
      miss "Wi-Fi \"$name\" is served from its read-only copy"
    elif nmcli --wait 60 connection up "$uuid" </dev/null >/dev/null; then
      ok "Wi-Fi \"$name\" connected using only its read-only copy"
    else
      miss "Wi-Fi \"$name\" connected using only its read-only copy"
    fi
  done
  put_back
  trap - EXIT HUP INT TERM
  info "the /etc copies are back in place"
}

case $MODE in
  check) report ;;
  test-fallback)
    [ "$(id -u)" -eq 0 ] || die "--test-fallback needs sudo"
    test_fallback
    ;;
  apply | dry-run)
    if ! dry && [ "$(id -u)" -ne 0 ]; then die "run it with sudo (or use --check)"; fi
    if dry && [ "$(id -u)" -ne 0 ]; then info "not root: files only root can read show as unreadable"; fi
    systemctl is-active --quiet NetworkManager || die "NetworkManager is not running"
    convert_netplan
    restore_etc_copies
    create_from_seed
    harden_profiles
    disable_cloud_init_network
    if ! dry; then
      nmcli connection reload </dev/null
      sync
      report
    fi
    ;;
esac
if [ "$MISSING" -gt 0 ]; then exit 1; fi
