# Weather Almanac

A content-first weather console for a WeatherFlow Tempest station on a Raspberry Pi:
one dominant temperature, a plain-language forecast, a seven-day band, and a live
radar map you drag and pinch like any other. It runs on a wall panel as a kiosk
and is viewable from any browser on the LAN.

> Weather Almanac is built on [Peter Davis's WeatherFlow PiConsole](https://github.com/peted-davis/WeatherFlow_PiConsole),
> the original and still-maintained console for WeatherFlow Tempest stations. Everything that
> reads the station, talks to WeatherFlow, and draws the classic six-panel console is his work
> and is kept intact here: the classic console remains a supported mode, and his fixes are
> merged as they land. If you find the console useful, the person to thank is him.
>
> **Installing the Almanac on a Pi:** see [`design/almanac/kiosk/PI4-SETUP.md`](design/almanac/kiosk/PI4-SETUP.md).
> The installation sections further down are the upstream instructions for the classic console.

The WeatherFlow PiConsole underneath is a Python console that displays the data collected
by a WeatherFlow Tempest or Smart Home Weather Station. The console uses either
the WeatherFlow REST API and websocket service or the local UDP connection to
stream data from your station in real time, including the 3-second rapid wind
updates. In UDP only mode, the console requires no connection to the internet
once installation is complete.

The console is fully supported for Raspberry Pi 3 Model B/B+, Raspberry Pi 4 
and Raspberry Pi 5 running the 64 bit version of Raspberry Pi OS Trixie
or the 32/64 bit version of Raspberry Pi OS Bookworm. It can be run on 
earlier models, but no direct support is provided for these systems. It is 
not compatible with Raspberry Pi Zero or Zero W. For full compatibility 
details, see below.

For a list of supported features and screenshots of the console in action,
please checkout the WeatherFlow community forums: https://community.weatherflow.com/t/weatherflow-piconsole/20083

https://weatherflow.com/tempest-weather-system/<br/>
https://community.weatherflow.com/

## The Almanac UI

This fork adds an optional **Almanac** interface: a glance-first redesign of the
same station data, built to be read from across a room on a wall-mounted 7-inch
screen. It runs on the same Raspberry Pi off the same feed and leaves the classic
six-panel console untouched.

The Almanac (this fork) reads at a glance and finds room for things the classic
layout can't. Here, heavy rain is falling through the intensity gauge, a 7-day
outlook runs along the foot of the page on one shared temperature scale, the
falling barometer has already switched its outlook to rain — and a quiet "Rain
tomorrow" sits under the headline (that line appears only when tomorrow is a
story: thunderstorms, snow, rain, wind, or fog; ordinary days say nothing).
Thursday in the band wears the wind glyph — a dry day whose story is its gusts:

![The Almanac interface during heavy rain, with the 7-day outlook band](design/almanac/screenshots/almanac.png)

While it rains, etched rain falls into the rate gauge — hatching at two depths,
its speed and density following the measured rate, into a sine-wave water
surface whose swell and drift also track the intensity:

![The rainfall panel during heavy rain: etched rain falls into the intensity gauge](design/almanac/screenshots/rainfall.gif)

Everything live moves the way an instrument should: the vane swings the short
way around the dial, and readings count to their new values instead of
snapping. Forecast furniture holds still — on this console, motion means "now":

![The wind panel: the vane swings and readings count up as the wind shifts](design/almanac/screenshots/wind.gif)

Winter is a first-class citizen. A pre-dawn January morning at −3.8 °F: the
Winter Storm Warning shares the page with a compressed 7-day outlook (snow
glyphs, sub-zero lows on the shared axis), and the rainfall panel reads
"Snow Likely" — the Tempest's haptic sensor cannot register snowfall, so in
freezing weather with snow forecast, the console says so instead of
pretending it is dry:

![The console on a winter morning: storm warning, snow outlook, Snow Likely status](design/almanac/screenshots/winter.png)

The classic six-panel console (the upstream default) for comparison:

![The classic six-panel console](design/almanac/screenshots/classic.png)

A **Radar** tab puts a live reflectivity view under the same discipline, and
it behaves like a map you already know: drag, flick, pinch, and the picture just
moves. Any browser on your home network can steer the radar; the panel follows.
Geography — coastline and water, state and county
lines, major roads, from bundled public-domain data — is rendered once by the
engine into small tiles for every zoom and both themes, cached, and drawn by the
page at any zoom in a few milliseconds; radar arrives as tiles the page places
itself, so a pan reveals more and a zoom step never waits on a server and never
replaces what you are looking at. It is centered on the station and works
anywhere on Earth: in the US it leads with NOAA's MRMS composite at two-minute
frames, and everywhere else — or whenever that feed can't be reached — it falls
back to a global ten-minute mosaic, always saying which one is on screen. Every
source's echoes are drawn on one reflectivity scale — nine bands from 10 to
75 dBZ in the RadarScope tradition, sage through green for light rain before
the yellows and reds, the same pixels on paper and night, nothing below 15 dBZ
so insects and clear-air clutter never film the map, and no guessing at rain versus snow.
Recent frames play as a smooth loop, five a second with a short crossfade and
a hold on the newest, whose scan time is printed beside the clock and is never
dressed up as "live"; opening the tab brings the loop up from cache in
milliseconds, and play means play even while frames are still arriving. Three
buttons choose the picture. **Auto**, the default, follows the zoom: close in
(zoom 8 and up) it shows the nearest radar's own scans, wide (6 and out) the
Region mosaic, and at 7 it keeps whatever is showing, so pinching back and forth
never flips it. It only moves to the site when reporting radars cover the view,
and the old picture stays up until the new one has frames. Tap Region or the
site to hold that choice; it returns to Auto after 45 minutes untouched. **Region** blends
many radars with a new image every two minutes. The nearest **NEXRAD** site
keeps its callsign and nearby-site count on the button. It shows its own
scans at their real times, every neighbouring radar whose range reaches the
view composited underneath, and a plain caption under the buttons that says which radar,
how far, and how often, with the radar's operating mode read from its own
cadence: a scan every four minutes or so is precipitation mode, every ten is
clear-air. The site button tells you before you tap when that radar is off the
air or its last scan is stale, and a tap on a dark site says so in the same
breath instead of trying for ten seconds. A tap acknowledges the choice at once.
The old source stays visible while the selected source loads. A quiet +/− sets
the zoom and remembers it across
reboots — a zoom step keeps the loop you were watching playing until the new
frames are in — and a **SMOOTH** toggle, off by default, softens the gate edges
of every echo without inventing detail, interpolated on the reflectivity field
so every pixel is still a legend colour. The site radar always uses **v2**:
NOAA Level III, the radar's own half-degree by 250-metre cells, on every board
including the Pi 3. It works across the US, including Alaska, Hawaii,
Puerto Rico and Guam. IEM site tiles fill in automatically only when Level III
is unreachable or its daily allowance is paused; the source caption plainly
labels the fallback and recovery. Smooth applies to Region and IEM fallback
tiles and is disabled while native frames draw.
While unattended, watch keeps only the primary radar's newest N0B/N0H scan
warm; opening Radar backfills the full mosaic loop. Rest and dormant fetch no
Site tiles or Level III products. Rest keeps its four-tile MRMS sentinel at home
every hour by day and every two hours at night. The daily cap keeps only the
newest frame past 150 MB and uses labelled IEM fallback past 250 MB, resetting
at midnight UTC; `/health` shows the count.
v2 merges nearby radars pixel by pixel: each pixel shows the echo from the
lowest beam within 230 km that sees one, so a radar blocked by mountains never
hides its neighbour's rain, and there are no seams where one radar's coverage
ends. NOAA's own classification removes ground clutter, and birds and insects
outside rain, for about 25 KB more per scan; if it is late, rain still draws
and the caption says "unfiltered". Region keeps its MRMS picture. The station glyph shows where home is
while you're away and the view drifts back to it after a minute and a half
untouched.

![The Radar tab in Region mode: the NOAA MRMS mosaic over Puget Sound on a showery morning, with the Auto, Region and KATX buttons](design/almanac/screenshots/radar.png)

![The Radar tab on Auto at zoom 9: v2 draws the radar's own 250-metre cells, KATX and three neighbours merged pixel by pixel](design/almanac/screenshots/radar-site.png)

![Forty minutes of v2 radar looping: showers moving across the Cascade foothills](design/almanac/screenshots/radar.gif)

What it changes:

- One dominant temperature and a plain-language forecast line, in place of six equal-weight panels.
- A barometer zone bar (Stormy / Change / Fair / Dry) and a 24-hour pressure barograph labelled with the day's high and low.
- Active weather alerts (US National Weather Service) in a single strip below the masthead, coloured by severity and collapsed to one line when several are active. The 7-day outlook stays on screen through an alert (compressed to highs and bars) — a winter storm warning can run for days, exactly when the week ahead matters most.
- Air quality (AQI) by the station's own latitude and longitude, with a short forecast so a rising smoke event shows before the number climbs.
- A 7-day outlook band: each day's low–high drawn as a bar on ONE shared axis for the week (so a cool-down is visibly a shorter, lower bar), condition glyphs beside the highs — sun, cloud, fog, rain, snow, thunderstorm, wind, and the compound marks wind-driven rain and blowing snow (a gusty wet day keeps its water but the drops slant to the engraver's driving-rain angle behind a wind curl) — precipitation probability only when it matters, the day's expected amount beside it in the station's own unit (dry days print nothing), and today's bar carrying a dot at the observed temperature.
- A one-line hint about tomorrow under the conditions headline ("Rain tomorrow", "Wind-driven rain tomorrow", "Freezing rain tomorrow", "Blowing snow tomorrow"…), led by tomorrow's own band glyph, that appears only when tomorrow is a story — its absence is the fair-weather signal.
- A Radar tab (above): a map you drag and pinch like any other, geography and radar both as locally cached tiles so nothing waits on a server; two-minute NOAA MRMS frames in the US, a ten-minute global mosaic elsewhere, one dBZ scale for every source, a five-a-second loop with a crossfade, a remembered zoom, and a NEXRAD view that composites every radar in reach.
- A rainfall rate gauge scaled by intensity rather than linearly — the five named bands (Very Light through Very Heavy) each take an equal fifth of the tube, so drizzle registers and a downpour doesn't pin the needle. Light rain never reads as dry: the Tempest's haptic sensor logs drizzle as an occasional trace minute with zeros between, so the rate carries a 10-minute window that bridges those gaps. Rain arrives and leaves like weather, not like a switch — the volume of falling drops counts up quickly and down slowly, column by column, toward the measured rate (so the sensor's wet/dry flip between minutes reads as a swell and a settle), the drops shorten and slow as it eases, and the last column leaves through a fade.
- A wind panel that resolves to one current reading, with a bolder compass.
- Animated updates: values count up, the vane swings — and while rain falls, etched rain falls through the gauge into a waving water surface, with fall speed, density, swell, and drift all tracking the measured rate.
- Snow-aware: in freezing weather with snow in the forecast, a dry rain sensor reads "Snow Likely" rather than "Currently Dry" (the Tempest's haptic sensor cannot register snowfall).
- Day/night aware. After sunset the Sun & Sky panel becomes Moon & Sky (phase, illumination, moonrise/set).
- Storm-aware layout: while lightning is being detected, the Lightning tile takes the Sun & Sky slot so rain and strikes stay on screen together.
- The forecast-today curve begins at the current reading and blends onto the real hourly forecast over the next few hours — the sensor is the better guide for the next hour or two, the model for the rest of the day — arriving exactly at the model's own peak, which is labelled at the hour it occurs and always agrees with the printed HIGH. With no hourly data it draws nothing forward rather than guess.
- Honest about silence: the masthead reads STALE when nothing new is reaching the screen and SILENT when the engine is fine but the station itself has stopped reporting - a fresh file is never mistaken for a live sensor. `/health` reports the same distinction for monitoring. Before the first frame ever arrives — a cold boot, or the engine down — every value reads "—" rather than a sample number, so an empty console can never be mistaken for a forecast.

Both extra data sources degrade quietly. Weather alerts come from the US National
Weather Service, so outside the US the strip simply stays hidden. Air quality is
worldwide (Open-Meteo), and its panel hides itself wherever a reading isn't
available. Neither one can stall the display: they are fetched off the main
thread, keep their last good value through a network blip, and are marked stale
rather than shown as current if the connection stays down.

### Viewing the Almanac remotely

Once the kiosk is running, the page is also reachable from any device on your
local network — phone, laptop, tablet — without any extra software:

```
http://weather.local:8137
```

Replace `weather` with your Pi's hostname if it differs. The page polls
`/wx.json` every two seconds and renders the same live data the wall display
shows. A status endpoint is also available:

```
http://weather.local:8137/health
```

This is enabled by default in the systemd service via `Environment=WFP_BIND=0.0.0.0`.
If you want to restrict the server back to the Pi only (no LAN access), remove
that line from `~/.config/systemd/user/almanac-kiosk.service` and run
`systemctl --user daemon-reload && systemctl --user restart almanac-kiosk`.

### Do I need a WeatherFlow Tempest?

For live weather readings (temperature, wind, rain, pressure) **yes** — the
console is built entirely around WeatherFlow's data formats and has no support
for other hardware brands (Ecowitt, Davis, Ambient, etc.).

The three connection modes are all WeatherFlow-only:

| Mode | What you need |
|---|---|
| Websocket + REST API (default) | WeatherFlow Personal Access Token + any Tempest, AIR, or SKY device |
| UDP + REST API | WeatherFlow device broadcasting on your local network |
| UDP only | WeatherFlow device + serial number; no internet required after setup |

**No hardware? Use a nearby public station.** Many WeatherFlow owners share
their stations publicly, and the console can read one of them. During first-run
setup (Websocket + REST mode), when you say you don't own hardware, the wizard
offers to find nearby public stations: enter an approximate latitude and
longitude and it lists the closest ones by distance, each with its hardware
type. Pick one and it fills in the station and device IDs for you, so you get
full live readings with no hardware of your own. All you need is a free
WeatherFlow account and a Personal Access Token (see below). The station belongs
to someone else, so the feed stops if its owner makes it private or takes it
offline; re-run the wizard to choose another.

**What works without a Tempest:** the Almanac's supplementary panels — air
quality, weather forecasts, and astronomy — pull from public APIs keyed only on
latitude and longitude. If you set those manually in `wfpiconsole.ini`, those
panels display correctly even with no hardware attached.

**Trying it before you buy:** the first-run wizard offers a "blank config"
option that installs a minimal `wfpiconsole.ini` and starts the console
immediately. The weather readings will be empty, but you can see the layout and
verify the Pi setup is working. Run `wfpiconsole start`, choose the blank
config option at the prompt, then edit `~/wfpiconsole/wfpiconsole.ini` manually
to add your station details once your hardware arrives.

To get a WeatherFlow Personal Access Token, go to
[tempestwx.com/settings/tokens](https://tempestwx.com/settings/tokens). Your
station ID and device IDs are under the WeatherFlow app: gear icon → Stations
→ [your station] → Status.

### Air quality source

By default the AQI panel pulls from Open-Meteo (a CAMS satellite model — no
account needed). For readings that match [airnow.gov](https://www.airnow.gov/)
exactly, configure a free WAQI token: it switches the source to the nearest
EPA/AirNow monitoring station.

**During install or update** the script asks for the token automatically. If you
skipped it or want to change it later, run:

```
wfpiconsole configure-aqi
```

That command prompts for a token, validates it against the WAQI API, and saves
it to `wfpiconsole.ini`. Leave the field blank to clear an existing token and
revert to Open-Meteo. A free token takes under a minute to obtain at
[aqicn.org/data-platform/token](https://aqicn.org/data-platform/token/).

Two ways to run it:

- **HTML kiosk (recommended).** The console runs headless as a data engine and `chromium --kiosk` renders the interface, pixel-identical and low-power (~½ core total on a Pi). A systemd-supervised watchdog keeps the whole chain alive: it relaunches any component that dies, restarts the engine if the feed goes stale (a hung websocket the classic UI would sit in), and re-checks for a wedged render. It self-heals: I confirmed that by killing the watchdog and watching systemd bring the display back on its own. Setup, management, and revert steps are in [`design/almanac/kiosk/README.md`](design/almanac/kiosk/README.md).
- **12-hour clock.** Set `[Display] TimeFormat = 12 hr` in `wfpiconsole.ini`. The
  upstream default is `24 hr`; every clock string in the payload follows this one
  setting, so the panel never mixes styles.
- **Native Kivy layout.** Set `[Display] LayoutStyle = almanac` in the config.

For the architecture, the `wx.json` data contract, and upgrade notes, see
[`design/almanac/ARCHITECTURE.md`](design/almanac/ARCHITECTURE.md).

### Operating the radar

The radar engine reports its own health at `/health` under `radar`:

- `cache`: the tile cache's live file count and bytes, its caps, whether the
  boot scan has finished, and that scan's summary (files indexed, directories
  purged, tiles evicted, seconds taken). Caps are derived from free space at
  boot: 2 % of the free bytes on the cache's disk, between 8,000 files / 64 MB
  and 12,000 files / 256 MB. The file ceiling is the boot validation cost, not
  disk: the Pi 4 validates about 200 tiles a second, and the first radar pass
  waits for the scan.
- `lastError`, `localFailures`, `ambiguousFailures`, `breaker`, `hosts`: the
  fetch layer. Consecutive local failures (a dead route, an exhausted client)
  back the retry off from 2 s to 60 s; provider failures advance the fallback
  chain instead; a stalled reused socket does neither.
- `discovery`, `pending`, `phases`, `requests`: the acquisition schedule and the
  last 128 requests.

A Radar tab left open with nobody touching the screen for 30 minutes keeps its
8-frame loop current but stops the zoom and source prefetch; a touch restores it.
The Rainfall tile reads "Rain Starting" the moment the station senses rain, before
the next minute's observation arrives.

A panel with the tab bar off (`WFP_TABS=0`, for a screen without touch) runs no
radar at all; set `WFP_RADAR=1` on the service to override.

After a restart the Radar tab stays and reads "Starting · checking N saved
tiles" until the first pass publishes, about a minute on the Pi 4; the other
panels show the previous run's last observations, marked by their age, until the
first live reading arrives.

- `attention`: which of the five acquisition tiers the engine is in (`live`,
  `warm`, `watch`, `rest`, `dormant`), why, since when, the weather holds, the
  last transitions, and bytes fetched per tier. With the tab closed the engine
  rests on listings plus a four-tile MRMS sentinel at home every hour by day
  (every two hours at night) until rain, lightning, a wet forecast, a touch on
  the device or an approaching echo warms it. Rest and dormant fetch no Site
  tiles or Level III products; dormant has no sentinel.

The engine logs one summary line per radar pass (`radar pass outcome=...`); a
pass that yields on an internal budget says `error=deferred: <reason>`. On the
Pi 4 kiosk the engine log is `/tmp/almanac_data.log` (the two previous runs as
`.1` and `.2`, each starting with the reason the engine was started) and the tile cache lives
under `~/almanac_web/radar`.

To inspect the running page itself, the kiosk's Chromium listens on the loopback
debug port; `design/almanac/kiosk/tools/cdp_probe.py` evaluates a JavaScript
expression there and prints the result:

```bash
python3 design/almanac/kiosk/tools/cdp_probe.py 'radarReady().length'
python3 design/almanac/kiosk/tools/cdp_probe.py '({mem: radarMemory(), cap: RAD_MEMORY_CAP})'
```

### Tests

The fork's data pipeline (the observation parser, the `wx.json` emitter, and a
merge-safety guard) has an offline pytest suite that needs no Kivy, display, or
network, so paths like a lightning strike are verified without waiting for real
weather:

```bash
python3 -m venv venv-test
venv-test/bin/pip install -r requirements-dev.txt
venv-test/bin/pytest
```

CI runs the same suite on GitHub Actions (Python 3.11) on every push and pull
request. One guard test compares this fork's `CurrentConditions` class against
the upstream project and fails on any divergence, so upstream fixes keep merging
cleanly.

## Contents

**[The Almanac UI (this fork)](#the-almanac-ui-this-fork)**<br>
&nbsp;&nbsp;&nbsp;&nbsp;[Viewing the Almanac remotely](#viewing-the-almanac-remotely)<br>
&nbsp;&nbsp;&nbsp;&nbsp;[Do I need a WeatherFlow Tempest?](#do-i-need-a-weatherflow-tempest)<br>
&nbsp;&nbsp;&nbsp;&nbsp;[Air quality source](#air-quality-source)<br>
&nbsp;&nbsp;&nbsp;&nbsp;[Operating the radar](#operating-the-radar)<br>
&nbsp;&nbsp;&nbsp;&nbsp;[Tests](#tests)<br>
**[Compatibility](#compatibility)**<br>
**[Installation Instructions](#installation-instructions)**<br>
**[Update Instructions](#update-instructions)**<br>
**[Auto-Start Instructions](#auto-start-instructions)**<br>
**[Advanced: Custom Panels](#advanced-custom-panels)**<br>
**[Advanced: Device Replacement](#advanced-device-replacement)**<br>
**[Advanced: Windows Installation](#advanced-installation-windows)**<br>
**[Credits](#credits)**<br>

## Compatibility

### Raspberry Pi

The console is fully supported for Raspberry Pi 3 Model B/B+, Raspberry Pi 4 and 
Raspberry Pi 5 running the 64 bit version of Raspberry Pi OS Trixie or the 32/64 
bit legacy version of Raspberry Pi OS Bookworm. It can be run on earlier models, 
but no direct support is provided for these systems. It is not compatible with 
the 32 bit version of Raspberry Pi OS Trixie or Raspberry Pi Zero or Zero W. 
While the console is compatible with Raspberry Pi 3, the graphics hardware on 
this model is ageing and performance of the console can be sluggish. It is 
recommended to use a Pi 4 or above. The console is not compatible with 
Raspberry Pi OS Buster.

The console is compatible with the Raspberry Pi Official 7 inch Touchscreen or
other HDMI equivalents. Note, screens that attach solely to the GPIO pins (SPI)
are not compatible and the console will not start.

### PC / Laptop

The console is fully supported on laptops and PCs running Ubuntu 22.04 LTS or
later, or the desktop version of Raspberry Pi OS. It will run on other
debian-based operating systems with Python version 3.9 or above, but no direct
support is provided for these environments.

## Installation Instructions

The installation of the WeatherFlow PiConsole is fully automated, and can
be started from the terminal with a single command. The automated installation
should take no longer than 10 minutes.

The automated installer assumes you have already successfully setup your Raspberry
Pi and have installed Raspberry Pi OS with Desktop, or you ar running on a PC
with Ubuntu 20.04 or later or Raspberry Pi OS installed. For a Raspberry Pi you
should have also attached the touch screen, and have either a keyboard and mouse
attached directly to the Pi, or have accessesd the Pi remotely through SSH/VNC.
If you are starting from scratch with a Raspberry Pi, the documentation should
help get you started:

* https://www.raspberrypi.org/documentation/

### Install WeatherFlow PiConsole

The WeatherFlow PiConsole can be installed quickly and conveniently with the
following command:
```
curl -sSL https://peted-davis.github.io/wfpiconsole | bash
```
Piping a command directly to ```bash``` is controversial, as it prevents the
user from reading code that is about to run on their system. If you are worried
about the contents of the installer, please examine the [first](https://raw.githubusercontent.com/peted-davis/peted-davis.github.io/master/wfpiconsole)
and [second](https://raw.githubusercontent.com/peted-davis/WeatherFlow_PiConsole/master/wfpiconsole.sh)
installation files in the Git repository before running the install command.

### Configure and Run WeatherFlow PiConsole

When the console is run for the first time, you'll first be asked whether you 
want to install a blank configuration file for demonstration purposes or 
advanced setup. You can use this option if you wish to try out the console 
before your WeatherFlow hardware has arrived, or if you are a power user and 
wish to configure the console manually rather than using the configuration 
wizard. For most users, the advanced installation option is not appropriate and 
the default option of 'no' should be selected at this prompt. If you choose to 
install a blank configuration file, the console will start but no data will 
show unless you edit the configuration file manually. 

You will also be prompted to specify your preferred connection type: Websocket 
and REST API (default), UDP and REST API, or UDP only. For UDP only you will be
required to manually enter futher information about your station (location,
name, elevation etc.). For Websocket and REST API or UDP and REST API you will
be asked to enter a WeatherFlow Personal Access Token and a CheckWX Aviation
Weather API key. The Personal Access Token is required for the PiConsole to
access the data from your station, and the CheckWX API key is required to
download the closest METAR information to your station location.

A Personal Access Token can be generated, viewed, and deleted here: 
https://tempestwx.com/settings/tokens, and a CheckWX API key can be obtained by 
registering for a free account here: https://www.checkwxapi.com/auth/signup

Once you have a Personal Access Token and registered with CheckWX (if required),
go ahead and run the console for the first time using:
```
wfpiconsole start
```
Depending on the connection type you select, you'll be asked to enter the API
keys you have just generated above, as well as information about your station.
This includes your station ID and device IDs for your AIR, SKY, or TEMPEST
modules. To find this information either open the WeatherFlow app or view your
station page in a web browser. Click on the gear (settings) icon -> Stations ->
[Station name] -> Status.

If you don't own WeatherFlow hardware, the station-ID step (Websocket + REST
mode) instead offers to search for a nearby public station. Answer yes, give an
approximate latitude and longitude, and pick from the distance-sorted list; the
console fills in the station and device IDs for you. See "Do I need a WeatherFlow
Tempest?" above for the details and caveats.

If all goes smoothly the console should automatically add the extra information
it needs to your configuration file and then start running. You should not need
to enter this configuration information again.

Congratulations, you have installed the PiConsole for the Weather Flow Tempest
and Smart Home Weather Stations.

### Screen size

By default the PiConsole will run in full screen mode. Fullscreen mode can be
disabled in Menu -> Settings -> Display. In this case the console will use the
dimensions specified in the configuration file (```wfpiconsole.ini```), which
can be changed manually. Please note that extreme changes to the aspect ratio
will result in text fields running into one another. Under Settings -> Display
there are also settings to show/hide the cursor and show/hide the window border.

### Remote access

Please note that you cannot use SSH to start the console remotely.  Instead for
remote access it is recommended to setup VNC (https://www.raspberrypi.org/documentation/remote-access/vnc/).
Note there are currently issues using Real VNC (the default VNC provider on
Raspberry Pis) with the latest version of Raspberry  Pi OS (Bookworm): https://help.realvnc.com/hc/en-us/articles/14110635000221-Raspberry-Pi-5-Bookworm-and-RealVNC-Connect

## Update Instructions

The WeatherFlow PiConsole can be updated quickly and easily with the following
command:
```
wfpiconsole update
```
The update process will retain your existing user settings, but may prompt for
input from time to time in order to add new functionality. Once the update has
finished, restart the console using:
```
wfpiconsole start
```

## Auto-Start Instructions

The WeatherFlow PiConsole can be configured to run automatically when the
Raspberry Pi powers up. To enable the console to start automatically, run
```
wfpiconsole autostart-enable
```
To stop the WeatherFlow PiConsole from starting automatically, run
```
wfpiconsole autostart-disable
```
If you are going to use the auto-start method, it is highly recommended that you
can SSH into your Raspberry Pi, as the console can only be stopped using the
stop command or a hard shutdown:
```
wfpiconsole stop
```

## Advanced: Custom Panels

The console is distributed with 7 built-in panels to display weather, forecast
and astronomical information. For advanced users, custom panels can be created
allowing the data display to be customised, or additional data sources to be 
integrated into the console. Custom panels should not be overwritten when the 
console is updated.

The custom panel templates are contained within the `~\wfpiconsole\user` folder. 
To use the custom panel feature, you first need to rename `customPanels.kv.tmpl` 
to `customPanels.kv` and `customPanels.py.tmpl` to `customPanels.py`. An example 
panel called "BigTemperature" is included as an example, and will be loaded the 
next time you start the console.

In the `customPanels.py` file you must create two classes per custom panel called: 
`[panel_name]Panel` and `[panel_name]Button`. "panel_name" can be whatever you want, 
but you must add the two classes that end with Panel and Button per custom panel. 
The classes should be empty (just add pass under the class name), unless you want 
to add methods to your custom panel to control its behaviour. The classes required
for the "BigTemperature" panel can be used as examples. 

In the `customPanels.kv` file you can define the layout of the panel. You need to 
add the two class names that you defined in `customPanels.py` surrounded by left and 
right angled brackets: <>. Again, you can see the "BigTemperature" panel in 
`customPanels.kv.tmpl` as an example. For the Button class, you can change the text 
attribute under PanelButton: to set the name of the panel that will be displayed in 
the bottom bar of the PiConsole. Otherwise leave this class unchanged. For the Panel 
class, the panel title is defined by the _title attribute under PanelTitle:. This can 
be different to the name of the panel that is displayed in the bottom bar. Otherwise 
you are free to define the layout however you want using in-built or custom Kivy 
widgets (https://kivy.org/doc/stable/api-kivy.uix.html).

## Advanced: Device Replacement

Occasionally it may be necessary to replace your Tempest device due to hardware
failure. Depending on how the replacement Tempest is added to your existing station,
the Tempest device ID and serial number may change. If this is the case, the
`wfpiconsole.ini` file needs to be updated with the new device ID and serial number.
The `.ini` file can either be edited directly, or if  you are not comfortable editing 
the `.ini` file, you can delete it and then restart the console. You will be taken
through the steps to generate a new `.ini` file with the updated device ID and serial
number. 

When a device is replaced, the total monthly/yearly rain accumulation displayed in the
console will also reset to zero as these fields are calculated directly from the 
total rain accumuluation recorded by the new device (which is naturally zero as 
the device is brand new). To retain the correct values, it is necessary to switch
the console to use the Tempest Statistics API endpoint using `Menu` -> `Settings` -> 
`System` -> `Statistics API endpoint`. By default this option is disbaled as it
results in a small loss of accuracy through rounding errors. Therefore it is not 
recommended for use unless you have replaced a device within the last calendar 
year. At the end of a calendar year, the Statistics endoint can be switched off.  

## Advanced Installation: Windows

Although not officially supported, use the following step-by-step instructions
to install and run the WeatherFlow PiConsole on Windows.

1. Download and install the Python 3.11.5 version of Miniconda for Windows (a
lightweight Python interpreter): https://conda.io/miniconda.html

2. Once Miniconda is installed open the ‘Anaconda Prompt’ program.

3. In the Anaconda prompt, run:
```
python -m pip install --upgrade pip
```

4. Once that process has finished, run:
```
python -m pip install websockets numpy pytz tzlocal ephem packaging pyOpenSSL certifi
```

5. Once that has finished, install Kivy using
```
python -m pip install kivy[base]
```
This is the GUI library that drives the console.

6. Once Kivy is installed, run the following commands in order in the Anaconda
Prompt. This will install the WeatherFlow PiConsole.
```
cd && mkdir wfpiconsole && cd wfpiconsole
curl -sL https://api.github.com/repos/peted-davis/WeatherFlow_PiConsole/tarball -o PiConsole.tar.gz
tar -xvf PiConsole.tar.gz --strip 1
del /f PiConsole.tar.gz
```

7. You’re almost there now! You can start the console using ```python main.py```.
As this is the first time you have run the console, you’ll be asked for some API
keys. Details of what you need can be found under "Configure and Run WeatherFlow
PiConsole" in the **[Installation Instructions](#installation-instructions)**.

## Credits

Many of the graphical elements in the console are based on the Weather34 Home
Weather Station Template (https://www.weather34.com/homeweatherstation/)
copyright 2015-2021 Brian Underdown. The Weather34 Home Weather Station Template
is licensed under a Creative Commons Attribution-NonCommercial-NoDerivatives 4.0
International License.
