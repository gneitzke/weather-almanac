#!/usr/bin/env bash
# Almanac kiosk launcher — shows the HTML weather overlay fullscreen on the Pi,
# fed live by the console. Runs as the console user inside their X session.
#
# Architecture:
#   1. DATA ENGINE — the console runs HEADLESS on a virtual X display (Xvfb :1)
#      with [Display] LayoutStyle=almanac, so lib/almanac_emit.py writes
#      /tmp/wfp_data/wx.json (~every 2s). It never touches the real screen.
#   2. SERVER — a tiny HTTP server serves the overlay page + wx.json (same origin).
#   3. DISPLAY — chromium --kiosk shows the page fullscreen on the real display :0,
#      with touch enabled and the cursor hidden.
#
#   HEADLESS MODE: set WFP_MODE=headless to run only steps 1-2 (engine + server)
#   with NO local chromium — for a box with no screen, viewed from another device
#   at http://<host>:PORT. Same self-healing watchdog, minus the display half.
#   See almanac-headless.service and README.md.
#
# Prereq (one time):  sudo apt-get install -y xvfb   (chromium-browser is already present)
# This launcher REPLACES the on-screen Kivy console: stop/disable wfpiconsole.service
# and autostart this instead (see README.md). Revert = re-enable wfpiconsole.service.
set -u

APP="${WFP_APP:-$HOME/wfpiconsole}"                 # console install dir
PY="$APP/venv/bin/python3"
WEB="${WFP_WEB:-$HOME/almanac_web}"                 # served dir (index.html + wx.json)
DATA_DIR="/tmp/wfp_data"; DATA="$DATA_DIR/wx.json"
PORT="${WFP_PORT:-8137}"; VDISP="${WFP_VDISP:-:1}"
THEME="${WFP_THEME:-night}"                          # night (dark) | paper (light)
MODE="${WFP_MODE:-kiosk}"                             # kiosk = on-screen chromium; headless = serve only, view remotely
UDD="/tmp/almanac_chrome"                            # chromium profile (wiped each launch)
HTTP_LOG="/tmp/almanac_http.log"

# real session env — chromium needs the exact session bus to map a window + touch.
USER_ID=$(id -u)
export XDG_RUNTIME_DIR="/run/user/$USER_ID"
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$USER_ID/bus"

# The on-screen half below (backend detection, cold-boot display gate, chromium)
# is skipped entirely when MODE=headless — there is no local screen to drive.
if [ "$MODE" != headless ]; then
# ── DISPLAY BACKEND — X11 (proven, the default) or Wayland ────────────────────
# Older Pi OS installs (and any raspi-config "X11" choice) run LXDE-pi/openbox on
# X11: the real screen is :0, gated by openbox + `xset q`. A fresh Raspberry Pi OS
# Bookworm install on a Pi 4/5 boots Wayland (labwc or wayfire) instead, where
# there is no :0, no openbox, and xset does nothing. Auto-detect so the same
# launcher works on both without anyone toggling raspi-config. Override with
# WFP_BACKEND=x11|wayland. The headless data engine always uses its own Xvfb
# (below), so only the on-screen chromium half depends on this.
find_wayland_display(){                                # sets WAYLAND_DISPLAY if a live socket exists
  [ -n "${WAYLAND_DISPLAY:-}" ] && [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] && return 0
  for s in "$XDG_RUNTIME_DIR"/wayland-[0-9]*; do
    [ -S "$s" ] && { WAYLAND_DISPLAY="$(basename "$s")"; return 0; }
  done
  return 1
}
BACKEND="${WFP_BACKEND:-auto}"
wait_for_backend(){
  local requested="$1" candidate
  for _ in $(seq 1 60); do
    case "$requested" in
      wayland)
        if { pgrep -x labwc >/dev/null 2>&1 || pgrep -x wayfire >/dev/null 2>&1; } && find_wayland_display; then
          BACKEND=wayland; return 0
        fi
        ;;
      x11)
        if pgrep -x openbox >/dev/null 2>&1 && DISPLAY=:0 xset q >/dev/null 2>&1; then
          BACKEND=x11; return 0
        fi
        ;;
      auto)
        # A declared session type is intent, even while its compositor is still
        # creating a socket. Never fall back to X11 during that interval.
        candidate="${XDG_SESSION_TYPE:-}"
        if [ "$candidate" = wayland ] || pgrep -x labwc >/dev/null 2>&1 || pgrep -x wayfire >/dev/null 2>&1; then
          if find_wayland_display; then BACKEND=wayland; return 0; fi
        elif [ "$candidate" = x11 ] || { pgrep -x openbox >/dev/null 2>&1 && DISPLAY=:0 xset q >/dev/null 2>&1; }; then
          if pgrep -x openbox >/dev/null 2>&1 && DISPLAY=:0 xset q >/dev/null 2>&1; then BACKEND=x11; return 0; fi
        fi
        ;;
      *) echo "invalid WFP_BACKEND: $requested" >&2; return 1 ;;
    esac
    sleep 1
  done
  echo "timed out waiting for $requested display backend" >&2
  return 1
}

wait_for_backend "$BACKEND" || exit 1
if [ "$BACKEND" = wayland ]; then
  export WAYLAND_DISPLAY; unset DISPLAY                # chromium maps onto the Wayland compositor, not :0
  CR_OZONE=wayland
else
  export DISPLAY=":0"                                  # force standard paths (autostart may carry empty values)
  export XAUTHORITY="$HOME/.Xauthority"
  CR_OZONE=x11
fi

# chromium binary name differs by image (Pi OS: chromium-browser; plain Debian: chromium)
CR_BIN="${WFP_CHROMIUM:-}"
if [ -z "$CR_BIN" ]; then
  for c in chromium-browser chromium; do command -v "$c" >/dev/null 2>&1 && { CR_BIN="$c"; break; }; done
fi
CR_BIN="${CR_BIN:-chromium-browser}"

# ── COLD-BOOT RACE (the root cause of "fragile on reboot") ────────────────────
# If chromium launches before the GPU/compositor is ready, its GPU process
# initialises into a broken state and composites a BLANK WHITE window that never
# recovers. Gate the launch on real readiness signals, not a fixed sleep:
#   1) the compositor / window manager is up,
#   2) the display is actually answering (X: `xset q`; Wayland: the socket exists),
#   3) a short settle for the GPU stack.
# The watchdog below is the belt-and-braces guarantee if the race still slips through.
sleep 5
fi   # end on-screen display setup (skipped when MODE=headless)

# RESTART-SAFE: if a previous instance died uncleanly (SIGKILL, OOM), its Xvfb /
# engine / server / chromium children are orphaned onto init and would collide
# with a fresh launch (two Xvfb on the same display, duelling chromiums). Clear
# any leftovers so a relaunch — by systemd, cron, or by hand — starts clean. At a
# normal boot this matches nothing.
pkill -9 chromium 2>/dev/null || true
pkill -f "Xvfb $VDISP" 2>/dev/null || true
pkill -f "venv/bin/python3 main.py" 2>/dev/null || true
pkill -f "kiosk/serve.py" 2>/dev/null || true
sleep 1

mkdir -p "$DATA_DIR" "$WEB"
# /tmp is cleared at reboot. Keep the channel beside wx.json, backed by durable
# station-local storage; serve.py atomically replaces the link target.
RADAR_STATE="${XDG_STATE_HOME:-$HOME/.local/state}/wfpiconsole"
mkdir -p "$RADAR_STATE"
if [ ! -e "$RADAR_STATE/radar_zoom" ] && [ -f "$DATA_DIR/radar_zoom" ]; then
  cp "$DATA_DIR/radar_zoom" "$RADAR_STATE/radar_zoom"
fi
ln -sfn "$RADAR_STATE/radar_zoom" "$DATA_DIR/radar_zoom"
if [ ! -e "$RADAR_STATE/radar_source" ] && [ -f "$DATA_DIR/radar_source" ]; then
  cp "$DATA_DIR/radar_source" "$RADAR_STATE/radar_source"
fi
ln -sfn "$RADAR_STATE/radar_source" "$DATA_DIR/radar_source"
if [ ! -e "$RADAR_STATE/radar_smooth" ] && [ -f "$DATA_DIR/radar_smooth" ]; then
  cp "$DATA_DIR/radar_smooth" "$RADAR_STATE/radar_smooth"
fi
ln -sfn "$RADAR_STATE/radar_smooth" "$DATA_DIR/radar_smooth"
if [ ! -e "$RADAR_STATE/radar_native_bytes.json" ] && [ -f "$DATA_DIR/radar_native_bytes.json" ]; then
  cp "$DATA_DIR/radar_native_bytes.json" "$RADAR_STATE/radar_native_bytes.json"
fi
ln -sfn "$RADAR_STATE/radar_native_bytes.json" "$DATA_DIR/radar_native_bytes.json"
cp -f "$APP/design/almanac/console_live.html" "$WEB/index.html"
ln -sf "$DATA" "$WEB/wx.json"

# Bounded health probe. No -f: /health answers 503 with a {"status":"stale"}
# body when the engine wedges, and that body is exactly what the watchdog
# needs to restart the ENGINE rather than the server.
health_response(){
  curl -sS --connect-timeout 2 --max-time 4 "http://127.0.0.1:$PORT/health" 2>/dev/null
}

STOPPING=0
XVFB_PID=""; ENGINE_PID=""; SERVE_PID=""; CRPID=""; SLEEP_PID=""
stop_process(){
  local pid="$1" name="$2" _
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 0
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 2); do kill -0 "$pid" 2>/dev/null || { wait "$pid" 2>/dev/null || true; return 0; }; sleep 1; done
  echo "$name did not stop after TERM; killing" >> /tmp/almanac_chrome.log
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}
cleanup(){
  STOPPING=1
  [ -n "${SLEEP_PID:-}" ] && { kill "$SLEEP_PID" 2>/dev/null; wait "$SLEEP_PID" 2>/dev/null; }
  stop_process "${CRPID:-}" chromium
  stop_process "${SERVE_PID:-}" server
  stop_process "${ENGINE_PID:-}" engine
  stop_process "${XVFB_PID:-}" Xvfb
}
on_signal(){ STOPPING=1; exit 0; }
trap cleanup EXIT
trap on_signal INT TERM

# 1) data engine on a virtual display (invisible).
#    WFP_HEADLESS=1 runs the console's data pipeline with NO GUI panels, so the
#    software GL rasterizer (llvmpipe) has nothing to draw — cuts the engine from
#    ~70% of a core to near-idle. Empty window still needs a display (Xvfb); cap
#    its frame rate low since nothing is shown.
# Each critical process is launched via a function so the watchdog can relaunch it.
launch_xvfb(){
  Xvfb "$VDISP" -screen 0 1024x600x24 -nolisten tcp >/tmp/almanac_xvfb.log 2>&1 &
  XVFB_PID=$!
}
ENGINE_LOG=/tmp/almanac_data.log
rotate_engine_log(){
  # A watchdog restart used to truncate the log that explained why the watchdog
  # fired (2026-09-24: two "sensor silent" restarts, no evidence left). Keep the
  # two previous runs; each new run starts with why it was started.
  [ -s "$ENGINE_LOG.1" ] && mv -f "$ENGINE_LOG.1" "$ENGINE_LOG.2"
  [ -s "$ENGINE_LOG" ] && mv -f "$ENGINE_LOG" "$ENGINE_LOG.1"
  printf '=== engine start %s reason=%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "${1:-boot}" > "$ENGINE_LOG"
}
launch_engine(){
  rotate_engine_log "${1:-boot}"
  # A panel with no tab bar (WFP_TABS=0) has no way to show radar, so the engine
  # runs none of it: no tile cache scan, no acquisition, no geography, no
  # listings. WFP_RADAR overrides explicitly if ever needed.
  ( cd "$APP" && DISPLAY="$VDISP" WFP_HEADLESS=1 WFP_RADAR="${WFP_RADAR:-${WFP_TABS:-1}}" KCFG_GRAPHICS_MAXFPS=10 "$PY" main.py ) >>"$ENGINE_LOG" 2>&1 &
  ENGINE_PID=$!
  ENGINE_GRACE=6                                   # ~90s warmup before the freshness check judges it
}
# 2) local web server (page + live feed + /health endpoint). /health exposes a
#    "renders" counter (frames the local page confirmed it painted) — our render
#    heartbeat, replacing the access-log grep. Bind 127.0.0.1 by default;
#    WFP_BIND=0.0.0.0 exposes it.
launch_server(){
  ( cd "$WEB" && WFP_PORT="$PORT" WFP_WEB="$WEB" WFP_DATA="$DATA" WFP_BIND="${WFP_BIND:-127.0.0.1}" \
      "$PY" "$APP/design/almanac/kiosk/serve.py" ) >"$HTTP_LOG" 2>&1 &
  SERVE_PID=$!
}

launch_xvfb
sleep 2
launch_engine
launch_server

# wait for first data frame (up to 45s) so the page opens populated
for _ in $(seq 1 45); do [ -s "$DATA" ] && break; sleep 1; done

# stop the screen from blanking (kiosk has no working input). X11: xset. Wayland
# has no xset — labwc/wayfire idle-blank is disabled via compositor config (see
# design/almanac/kiosk/PI4-SETUP.md); chromium --kiosk also inhibits the idle.
if [ "$MODE" != headless ] && [ "${BACKEND:-}" = x11 ]; then
  xset s off -dpms s noblank 2>/dev/null || true
fi

if [ "$MODE" != headless ]; then
# ── chromium kiosk, with a self-healing watchdog ──────────────────────────────
# Flags stay MINIMAL and use the REAL GPU (default). Do NOT add --disable-gpu
# (software rendering can't composite on VC4 -> no window maps), nor
# --disable-dev-shm-usage / --single-process (they starve renderer IPC).
CR_FLAGS=(--kiosk --ozone-platform="$CR_OZONE" --touch-events=enabled
  --no-first-run --no-default-browser-check --disable-infobars
  --disable-session-crashed-bubble --noerrdialogs --password-store=basic)
URL="http://127.0.0.1:$PORT/index.html?theme=$THEME"
# The page hides navigation unless tabs is present. On-screen touch kiosks
# need visible targets; WFP_TABS=0 retains the observations-only presentation.
if [ "${WFP_TABS:-1}" = 1 ]; then URL="$URL&tabs=1"; fi

CRPID=""
launch_cr(){
  [ "$STOPPING" -eq 0 ] || return 0
  stop_process "${CRPID:-}" chromium
  rm -rf "$UDD"                                   # fresh profile: no stale SingletonLock
  "$CR_BIN" "${CR_FLAGS[@]}" --user-data-dir="$UDD" "$URL" \
    >/tmp/almanac_chrome.log 2>&1 &
  CRPID=$!
}

# A healthy render means the page's JS painted a frame (~every 2s). A blank/
# broken GPU init leaves the renderer unable to run JS -> ZERO new renders. That
# is our screenshot-free, root-free health check.
#
# "renders", NOT "polls": the page marks its NEXT wx.json request with r=1 only
# after the previous frame actually reached the screen, and the server counts
# that mark only from loopback. A request that 404s, throws in render(), or
# comes from a LAN browser therefore proves nothing and is not counted.
read_renders(){
  local response renders
  response=$(health_response) || return 1
  renders=$(printf '%s\n' "$response" | sed -n 's/.*"renders": *\([0-9][0-9]*\).*/\1/p')
  [ -n "$renders" ] || return 1
  printf '%s\n' "$renders"
}
renders_growing(){
  local before after
  before=$(read_renders) || return 1
  sleep 8
  after=$(read_renders) || return 1
  [ "$after" -gt "$before" ]
}

# launch, and if it came up blank (not polling), wipe and retry
for attempt in 1 2 3 4; do
  launch_cr
  sleep 12
  if renders_growing; then
    echo "kiosk healthy on attempt $attempt" >> /tmp/almanac_chrome.log
    break
  fi
  echo "blank render on attempt $attempt — restarting chromium" >> /tmp/almanac_chrome.log
done
fi   # end chromium kiosk (skipped when MODE=headless)

# keep the session alive; relaunch ANY critical process that dies (not just
# chromium — a dead data engine or server used to leave the screen stale forever),
# and every ~5 min re-check for a wedged alive-but-blank render.
CLOG=/tmp/almanac_chrome.log
loops=0; stale_hits=0; health_failures=0; degraded_hits=0; degraded_acted=0
while [ "$STOPPING" -eq 0 ]; do
  kill -0 "$XVFB_PID" 2>/dev/null || { echo "Xvfb died — relaunching" >> "$CLOG"; launch_xvfb; sleep 2; }
  kill -0 "$ENGINE_PID" 2>/dev/null || { echo "data engine died — relaunching" >> "$CLOG"; launch_engine died; }
  kill -0 "$SERVE_PID"  2>/dev/null || { echo "web server died — relaunching"  >> "$CLOG"; launch_server; }
  if [ "$MODE" != headless ] && ! kill -0 "$CRPID" 2>/dev/null; then
    echo "chromium exited — relaunching" >> "$CLOG"
    launch_cr; sleep 12
  fi

  # DATA FRESHNESS — the engine can be alive-but-wedged (a hung websocket or a
  # stalled emit loop): the PID guard above won't catch that, but the screen goes
  # silently stale. /health reports "stale" once wx.json stops updating (age > 20s).
  # After a fresh engine's warmup grace, restart it if data stays stale two checks
  # running (~30s) — recovering a hang the classic UI would just sit in.
  if [ "${ENGINE_GRACE:-0}" -gt 0 ]; then
    ENGINE_GRACE=$((ENGINE_GRACE - 1)); stale_hits=0
  else
    health=$(health_response) || health=""
    st=$(printf '%s\n' "$health" | sed -n 's/.*"status": *"\([a-z][a-z]*\)".*/\1/p')
    case "$st" in ok|stale|degraded|error) ;; *) st="" ;; esac
    if [ -z "$st" ]; then
      stale_hits=0
      health_failures=$((health_failures + 1))
      if [ "$health_failures" -ge 2 ]; then
        echo "web server health check failed — restarting server" >> "$CLOG"
        stop_process "$SERVE_PID" server; launch_server
        health_failures=0
      fi
    elif [ "$st" = "stale" ] || [ "$st" = "error" ]; then
      health_failures=0
      stale_hits=$((stale_hits + 1))
      if [ "$stale_hits" -ge 2 ]; then
        echo "data $st — restarting data engine" >> "$CLOG"
        stop_process "$ENGINE_PID" engine; launch_engine "data-$st"
        stale_hits=0; degraded_hits=0     # degraded_acted survives: only a fresh observation ends the episode
      fi
    elif [ "$st" = "degraded" ]; then
      # SENSOR SILENT: the engine is emitting fresh files, but the station
      # behind them stopped reporting. Two possible causes and only one is ours
      # — a wedged websocket (fixable by a restart) or a dead/offline station
      # (not). So restart the engine ONCE per episode, then leave it alone
      # rather than thrash a box whose Tempest battery is simply flat.
      health_failures=0; stale_hits=0
      degraded_hits=$((degraded_hits + 1))
      if [ "$degraded_hits" -ge 4 ] && [ "$degraded_acted" -eq 0 ]; then
        echo "sensor silent — restarting data engine once" >> "$CLOG"
        stop_process "$ENGINE_PID" engine; launch_engine sensor-silent
        degraded_acted=1
      fi
    else
      health_failures=0; stale_hits=0; degraded_hits=0; degraded_acted=0
    fi
  fi

  loops=$((loops + 1))
  if [ "$MODE" != headless ] && [ $((loops % 8)) -eq 0 ]; then   # ~every 2 min (8 × 15s): alive-but-blank render
    renders_growing || { [ "$STOPPING" -ne 0 ] || { echo "render wedged — relaunching chromium" >> "$CLOG"; launch_cr; sleep 12; }; }
  fi
  # background sleep + wait: a TERM during the pause reaches the trap at once
  # instead of after the sleep, keeping shutdown inside TimeoutStopSec
  sleep 15 & SLEEP_PID=$!; wait "$SLEEP_PID"; SLEEP_PID=""
done
