# Almanac kiosk: fresh Pi 4 / Pi 5 setup

This is the from-scratch path for a new Raspberry Pi. The kiosk launcher
(`almanac-kiosk.sh`) runs on **both** display servers and picks the right one at
startup, so you do not have to force the desktop to X11 the way older builds
required.

## Recommended OS

**Raspberry Pi OS (64-bit), Bookworm, Desktop.**

- 64-bit matches the build target and CI (Python 3.11, `aarch64`).
- Desktop (not Lite) ships chromium and a login session, which the kiosk needs.
- Bookworm's Python 3.11 is exactly what `wfpiconsole.sh` builds its venv against.

A fresh Bookworm image on a Pi 4/5 boots a **Wayland** session (labwc or
wayfire). Older installs, and anything set to X11 in `raspi-config`, run
**X11** (LXDE-pi/openbox). The launcher detects which one is live and adapts:

| | X11 session | Wayland session |
|---|---|---|
| Real display | `:0`, gated on openbox + `xset q` | Wayland socket, gated on labwc/wayfire + socket |
| chromium flag | `--ozone-platform=x11` | `--ozone-platform=wayland` |
| Screen blanking | `xset s off -dpms` | compositor config (see below) |
| Headless data engine | `Xvfb :1` (identical on both) | `Xvfb :1` (identical on both) |

Force a backend with `WFP_BACKEND=x11` or `WFP_BACKEND=wayland` if detection ever
guesses wrong. Point at a specific browser with `WFP_CHROMIUM=/usr/bin/chromium`.

## Setup

1. **Flash the image** with Raspberry Pi Imager. In its settings, preset the
   hostname, your user, WiFi, locale, and enable SSH, for a headless first boot.
2. **Update now**, while the card is fresh:
   ```
   sudo apt update && sudo apt full-upgrade -y
   sudo apt install -y xvfb      # the headless data engine needs it; chromium is already present
   ```
3. **Autologin to the desktop** so a session exists at boot:
   `sudo raspi-config` → System Options → Boot / Auto Login → **Desktop Autologin**.
   You do **not** need to switch Wayland→X11; the launcher handles either.
4. **Clone the fork and install:**
   ```
   git clone -b main https://github.com/gneitzke/weather-almanac.git ~/wfpiconsole
   cd ~/wfpiconsole && ./wfpiconsole.sh install
   ```
5. **Configure** (`wfpiconsole start`, then the wizard). It asks for a WeatherFlow
   Personal Access Token; generate one at
   [tempestwx.com/settings/tokens](https://tempestwx.com/settings/tokens) (free
   account). No hardware at this Pi? The station picker will offer to search for a
   nearby public station, so you do not need your own Tempest.
6. **Enable the kiosk service.** The systemd user service, autostart, and revert
   steps are in [`README.md`](README.md). Set `WFP_BIND=0.0.0.0` in the service to
   view the page from other devices at `http://<hostname>.local:8137`.
7. **Pin the browser to the console.** A kiosk has no back button, so a stray tap
   on any link would strand the panel on another site. Install the Chromium policy
   that allows only the console's own origin (Pi OS reads `chromium-browser`,
   plain Debian reads `chromium`; installing both is harmless):
   ```
   for d in /etc/chromium/policies/managed /etc/chromium-browser/policies/managed; do
     sudo mkdir -p "$d" && sudo cp ~/wfpiconsole/design/almanac/kiosk/chromium-policy.json "$d/almanac-kiosk.json"
   done
   ```
   Chromium reads it at launch (restart the kiosk service after installing).
8. **Reboot** (`sudo reboot`). The Pi autologins to the desktop and the kiosk comes
   up fullscreen on its own. Autologin and the boot-time service both need this
   first reboot to take effect.

## Screen blanking on Wayland

X11 blanking is handled by the launcher (`xset`). Wayland has no `xset`:

- **labwc** ships no idle daemon by default, so the screen does not blank on its
  own; there is nothing to do. chromium `--kiosk` also holds an idle-inhibit.
- **wayfire** enables an idle/DPMS plugin. To stop the wall display from blanking,
  set a zero timeout in `~/.config/wayfire.ini`:
  ```ini
  [idle]
  dpms_timeout = 0
  screensaver_timeout = 0
  ```
  then log out/in (or reboot).

## WiFi: keep the box on the LAN

The Pi 4's onboard BCM43455 has twice dropped off the LAN while the router
still listed it as online: the association held, but ARP and ping were dead
until a power-cycle. Two things guard against it:

1. **Power save off.** The kiosk's NetworkManager profile sets
   `802-11-wireless.powersave disable`, and `/etc/NetworkManager/conf.d/wifi-powersave.conf`
   pins `wifi.powersave = 2` for every profile. `dmesg | grep power_mgmt` should end
   with `power save disabled`.
2. **A keepalive timer** (`wifi-keepalive.sh` / `.service` / `.timer` in this directory)
   pings an always-on LAN host every minute and, after three consecutive
   failures, re-associates through NetworkManager. The peer defaults to the
   DHCP default gateway (read from the routing table at run time, never baked
   in); set `WIFI_PEER=<ip>` in `/etc/default/wifi-keepalive` only to probe a
   different always-on host.

   It also sends a *bridge nudge* every check: a couple of unicast pings to the
   most recent LAN device that viewed the console (the server records it in
   `/tmp/wfp_data/last_viewer`). On a multi-node mesh the node bridging the Pi
   can stop forwarding wired-side traffic to it (wired hosts see ARP
   `(incomplete)` while the Pi's own outbound still works, and the gateway still
   answers, so a gateway probe cannot detect it). The Pi's own frames toward a
   wired host are what re-teach the node's bridge table. The nudge's reply is
   ignored; the frames are the point.

   Every check also *announces* the Pi (`wifi-announce.py`): two rounds of
   broadcast ARP for its own address. Measured 2026-10-26, the mesh delivers no
   broadcast or multicast frames to wireless clients at all (none in 30 s on
   either Pi), so another host can find a Pi only through the mesh's record of
   it. That record goes stale after a roam or ages out: the Pi 3 roamed between
   nodes and nothing on the LAN, not even the Pi 4 on the same node, reached it
   for nine hours. The Pi's own broadcast re-teaches every node, the mesh's ARP
   proxy and every wired host, so a stale record lasts at most a minute. The
   result rides on the per-minute log line (`announced <ip> at <mac>`).
   Install:

   ```
   sudo install -m 0755 wifi-keepalive.sh /usr/local/sbin/
   sudo install -m 0755 wifi-announce.py /usr/local/sbin/wifi-announce
   sudo install -m 0644 wifi-keepalive.service wifi-keepalive.timer /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now wifi-keepalive.timer
   ```

   The kernel can announce on its own when the link comes up; turn that on too:

   ```
   echo 'net.ipv4.conf.all.arp_notify = 1' | sudo tee /etc/sysctl.d/99-arp-notify.conf
   sudo sysctl -p /etc/sysctl.d/99-arp-notify.conf
   ```

   The same applies to any Pi on this network, the Pi 3 included: it runs the
   timer with the same files.

   Every recovery is logged: `journalctl -t wifi-keepalive`. Make the journal
   persistent (`Storage=persistent` in `/etc/systemd/journald.conf.d/`) so the
   NetworkManager log from a drop survives the reboot that follows it.

## Display and touch

The console is a fixed **1024×600 artboard**. On a native-1024×600 panel it
renders 1:1 — the page's `--fit` computes `min(vw/1024, vh/600) = 1.0`, and it
uses CSS `zoom` (which re-rasterizes) rather than `transform: scale`, so text
stays crisp. A smaller panel (e.g. an 800×480 DSI) fits at ~0.78; a larger one
scales up. Nothing to configure — it adapts to whatever the compositor reports.

- **Driver:** `dtoverlay=vc4-kms-v3d` in `config.txt` (the real KMS GPU, with
  `disable_fw_kms_setup=1` so the firmware doesn't pre-set a mode). Confirm with
  `lsmod | grep -E 'vc4|v3d'` and `ls /dev/dri` (expect `card*` + `renderD128`).
- **Mode:** let the panel's EDID pick its native mode; check with `wlr-randr`
  (Wayland) — the "(preferred, current)" line is native. **Do not force a higher
  mode.** A 1024×600 panel that also advertises 1920×1080 will *downscale* it
  internally — blurrier than native for no gain. Keep the compositor output
  `Scale: 1.000000` (fractional scaling blurs a pixel-art console).
- **Touch** (e.g. an Elecrow 7" 1024×600 IPS, a USB-HID capacitive panel): udev
  tags it `ID_INPUT_TOUCHSCREEN=1`, and with a single connected output labwc maps
  it to that output **by default** — no rule required. The console's tab bar
  responds to taps directly (Chromium runs `--touch-events=enabled` and the tabs
  listen for `click`/`pointerdown`, both of which a tap fires natively), so
  compositor mouse-emulation is not needed for them.

  The one thing that will break touch is a **`<touch>` rule in
  `~/.config/labwc/rc.xml` that doesn't match reality** — labwc matches
  `deviceName` *exactly* and resolves `mapToOutput` against the live connector.
  This box once carried a rule for a now-absent `DSI-1` + I2C `ft5x06`; when that
  panel was replaced, a rule naming the new device as `"QDtech MPI5001"` (from
  `/proc/bus/input/devices`) still failed, because libinput's real name carries a
  port suffix — `"QDtech MPI5001 (USB 1-1.1)"`. Get the exact name from
  `sudo libinput list-devices`. The robust form for a single-output kiosk names
  the device only and lets the default output mapping stand:

  ```xml
  <touch deviceName="QDtech MPI5001 (USB 1-1.1)" mouseEmulation="yes"/>
  ```

  Note the `(USB 1-1.1)` is the physical port — moving the panel to another USB
  socket changes it, so if touch dies after a re-plug, re-check the name. After
  editing rc.xml, reload without dropping the session:
  `kill -HUP "$(pgrep -x labwc)"` (`labwc --reconfigure` fails over SSH — it needs
  `LABWC_PID` from inside the session).

## Why a Pi 4/5 over the old Pi 3

- Wired Ethernet. Avoids the dead-onboard-radio / flaky-dongle trouble the Pi 3
  had, which matters for a headless wall appliance you manage over SSH.
- The cores and RAM the headless engine + chromium actually want. 2 GB is enough,
  4 GB comfortable.
- The cold-boot white-screen guard and self-healing watchdog in the launcher are
  GPU-agnostic, so they carry over unchanged.

## Verifying the backend it chose

After the service is up:
```
grep -E "healthy|attempt|backend" /tmp/almanac_chrome.log
echo "$XDG_SESSION_TYPE"          # x11 or wayland (what the desktop is running)
pgrep -a -x 'labwc|wayfire|openbox'
```
`/health` should report growing `polls` (the render heartbeat):
```
curl -s http://127.0.0.1:8137/health
```
