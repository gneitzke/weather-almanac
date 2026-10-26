#!/bin/bash
# Probe a stable LAN peer: the gateway can answer while the Pi is isolated
# from other LAN clients. The 2026-09-08 pre-reboot journal showed the old
# gateway checks completing in ~2s throughout the outage, without recovery.
# There is no evidence yet that nmcli recovery fails; do not reload drivers
# or restart NetworkManager speculatively. Log actual post-recovery probes.
# Configure via /etc/default/wifi-keepalive or a systemd Environment override.
set -eu
IFACE="${WIFI_IFACE:-wlan0}"
# The peer defaults to the DHCP default gateway on the wifi interface — the one
# host every site has always on — and is never baked in here. Override with
# WIFI_PEER in /etc/default/wifi-keepalive for a different always-on LAN host.
PEER="${WIFI_PEER:-}"
if [ -z "$PEER" ]; then
  PEER=$(ip route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}') || PEER=""
fi
# Bridge nudge: the console's server records the most recent LAN viewer in
# <data dir>/last_viewer. A few unicast frames from the Pi toward that host each
# check keep the mesh node's forwarding entry for the Pi fresh on the WIRED side;
# a reachable gateway alone did not (observed 2026-09-13/14: gateway REACHABLE,
# wired hosts could not ARP the Pi, and pinging the wired viewer healed it).
NUDGE_FILE="${WIFI_NUDGE_FILE:-/tmp/wfp_data/last_viewer}"
# Announce: the mesh delivers no broadcasts to its wireless clients (2026-10-26:
# none in 30 s on either Pi), so others find the Pi only through the mesh's own
# record of it, which goes stale after a roam or ages out. The Pi's broadcast
# ARP re-teaches every node and wired host (wifi-announce).
ANNOUNCE="${WIFI_ANNOUNCE:-/usr/local/sbin/wifi-announce}"
FAIL_LIMIT="${WIFI_FAIL_LIMIT:-3}"
# A peer outage must not cause repeated wifi bounces every three minutes.
COOLDOWN="${WIFI_RECOVERY_COOLDOWN:-900}"
STATE=/run/wifi-keepalive.fails
LAST_RECOVERY=/run/wifi-keepalive.last-recovery

log() { logger -t wifi-keepalive -- "$*"; }
if [[ ! "$FAIL_LIMIT" =~ ^[1-9][0-9]{0,5}$ ]] ||
   [[ ! "$COOLDOWN" =~ ^[1-9][0-9]{0,5}$ ]]; then
  log "invalid WIFI_FAIL_LIMIT or WIFI_RECOVERY_COOLDOWN; refusing recovery"
  exit 1
fi
if [ -z "$PEER" ]; then
  log "WIFI_PEER is not set (configure it in /etc/default/wifi-keepalive); nothing to probe"
  exit 0
fi
# Serialize timer and manual invocations, including the recovery wait.
exec 9>/run/wifi-keepalive.lock
if ! flock -n 9; then
  log "another check is running; skipping"
  exit 0
fi

read_count() {
  local value
  value=$(cat "$1" 2>/dev/null) || value=0
  [[ "$value" =~ ^(0|[1-9][0-9]{0,9})$ ]] || value=0
  printf '%s\n' "$value"
}
probe() { ping -n -c 3 -W 2 -w 8 -I "$IFACE" "$1" >/dev/null 2>&1; }

# Announce every check, first: a failure is logged on its own line, success
# rides on the peer line so the journal stays one line a minute.
announced="announce not installed"
if [ -x "$ANNOUNCE" ]; then
  if out=$("$ANNOUNCE" "$IFACE" 2>&1); then
    announced="${out%% on *}"
  else
    announced="announce failed"
    log "announce failed: ${out:-no output}"
  fi
fi

# Bridge nudge every check, before the probe and regardless of outcome: the
# result is deliberately ignored (the viewer may be asleep); the frames are the point.
if [ -r "$NUDGE_FILE" ]; then
  viewer=$(head -c 64 "$NUDGE_FILE" | tr -d '[:space:]')
  if [[ "$viewer" =~ ^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)[0-9]{1,3}\.[0-9]{1,3}(\.[0-9]{1,3})?$ ]] && [ "$viewer" != "$PEER" ]; then
    ping -n -c 2 -W 1 -w 3 -I "$IFACE" "$viewer" >/dev/null 2>&1 && nudge=answered || nudge=silent
    log "bridge nudge to last LAN viewer $viewer: $nudge"
  fi
fi

fails=$(read_count "$STATE")
if probe "$PEER"; then
  echo 0 > "$STATE"
  log "peer $PEER reachable on $IFACE; misses $fails -> 0; $announced; no recovery needed"
  exit 0
fi

fails=$((fails + 1))
echo "$fails" > "$STATE"

gw=$(ip route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}')
gateway_status=unreachable
if [ -n "$gw" ] && probe "$gw"; then
  gateway_status=reachable
fi
log "peer $PEER unreachable on $IFACE; consecutive misses=$fails/$FAIL_LIMIT; gateway ${gw:-unknown} $gateway_status"
# A reachable gateway is diagnostic, never a veto of peer-based recovery.
if [ "$fails" -lt "$FAIL_LIMIT" ]; then
  exit 0
fi

# Monotonic boot time and /run state avoid wall-clock adjustments and reboots.
read -r uptime_seconds _ < /proc/uptime
now=${uptime_seconds%%.*}
last=$(read_count "$LAST_RECOVERY")
if [ "$last" -gt 0 ] && [ "$((now - last))" -lt "$COOLDOWN" ]; then
  log "recovery deferred by ${COOLDOWN}s cooldown; peer may be offline; misses retained"
  exit 0
fi
echo "$now" > "$LAST_RECOVERY"

log "peer $PEER unreachable ${fails}x; re-associating $IFACE with nmcli"
if nmcli --wait 15 device disconnect "$IFACE" >/dev/null 2>&1; then
  log "nmcli disconnect $IFACE succeeded"
else
  status=$?
  log "nmcli disconnect $IFACE failed (exit $status); still attempting connect"
fi
sleep 3
if nmcli --wait 45 device connect "$IFACE" >/dev/null 2>&1; then
  log "nmcli connect $IFACE succeeded; verifying LAN peer"
else
  status=$?
  log "nmcli connect $IFACE failed (exit $status); checking actual LAN reachability"
fi

# Association success alone is insufficient. Reset only on a peer response.
for attempt in 1 2 3; do
  if probe "$PEER"; then
    echo 0 > "$STATE"
    log "peer $PEER reachable after nmcli (verification $attempt/3); misses reset"
    exit 0
  fi
  log "peer $PEER still unreachable after nmcli (verification $attempt/3)"
  if [ "$attempt" -lt 3 ]; then sleep 5; fi
done
log "recovery did not restore peer $PEER; misses retained; retry after cooldown; inspect peer and network journal before escalating"
exit 1
