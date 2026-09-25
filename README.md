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

The Almanac reads at a glance and finds room for things the classic layout
can't. In the screenshot below, heavy rain is falling through the intensity
gauge. A 7-day outlook runs along the foot of the page on one shared temperature
scale. The barometer is falling and has already switched its outlook to rain.
Under the headline sits a quiet "Rain tomorrow". That line appears only when
tomorrow has a story (thunderstorms, snow, rain, wind or fog); ordinary days say
nothing. Thursday in the band wears the wind glyph: a dry day whose story is its
gusts.

![The Almanac interface during heavy rain, with the 7-day outlook band](design/almanac/screenshots/almanac.png)

While it rains, etched rain falls into the rate gauge. The hatching falls at two
depths, its speed and density follow the measured rate, and it lands in a
wave-topped water surface whose swell tracks the intensity too.

![The rainfall panel during heavy rain: etched rain falls into the intensity gauge](design/almanac/screenshots/rainfall.gif)

Live readings move the way an instrument should. The vane swings the short way
around the dial, and numbers count to their new values instead of snapping.
Forecast elements hold still, so on this console motion means "now".

![The wind panel: the vane swings and readings count up as the wind shifts](design/almanac/screenshots/wind.gif)

Winter gets the same care. Here it is −3.8 °F before dawn in January. The Winter
Storm Warning shares the page with a compressed 7-day outlook (snow glyphs,
sub-zero lows on the shared axis). The rainfall panel reads "Snow Likely": the
Tempest's haptic sensor cannot register snowfall, so in freezing weather with
snow in the forecast the console says so instead of pretending it is dry.

![The console on a winter morning: storm warning, snow outlook, Snow Likely status](design/almanac/screenshots/winter.png)

The classic six-panel console (the upstream default), for comparison:

![The classic six-panel console](design/almanac/screenshots/classic.png)

### The Radar tab

The Radar tab behaves like a map you already know: drag, flick and pinch, and
the picture moves. Geography (coastline, water, state and county lines, major
roads) comes from bundled public-domain data. The engine renders it once into
small tiles for every zoom and both themes, and the page draws them in a few
milliseconds. Radar arrives as tiles too, so a pan reveals more of the map and
a zoom step never waits on a server or blanks what you were looking at.

The map is centred on the station and works anywhere on Earth. Three buttons
choose the picture:

- **Region** shows NOAA's MRMS composite, a new image every two minutes, across
  the US. Everywhere else, or when that feed is down, it falls back to a global
  ten-minute mosaic, and the caption says which one is on screen.
- **The nearest radar** (its callsign on the button, e.g. "KATX +3") shows that
  radar's own scans at their real times, merged with every neighbour whose range
  reaches the view. The caption names the radar, its distance and how often it
  scans, and reads its operating mode from that cadence: about every four
  minutes is precipitation mode, about every ten is clear-air. The button warns
  you before you tap when the radar is off the air or its last scan is stale.
- **Auto**, the default, follows the zoom. At zoom 8 and closer it shows the
  nearest radar; at 6 and wider, Region. At 7 it keeps whatever is showing, so
  pinching back and forth never flips it. It moves to the radar only when
  reporting radars cover the view. Tapping Region or the radar holds that choice
  until you tap Auto or leave the screen untouched for 45 minutes.

A switch never blanks the map: the old picture stays until the new one has
frames. The station glyph marks home while you are away, and the view drifts
back to it after a minute and a half untouched. Any browser on your home network
can steer the radar, and the panel follows whoever touched it last.

**The radar's own cells.** The nearest-radar view is drawn from NOAA's Level III
product: half-degree by 250-metre cells, the radar's native resolution, on every
board including the Pi 3. Nearby radars are merged pixel by pixel. Each pixel
takes the echo from the lowest beam within 230 km that sees one, so a radar
blocked by mountains never hides its neighbour's rain and there are no seams
where one radar's coverage ends. NOAA's classification product (about 25 KB more
per scan) removes ground clutter, and birds and insects outside rain. If that
classification is late, rain still draws and the caption says "unfiltered". This
works across the US, including Alaska, Hawaii, Puerto Rico and Guam.

**One colour scale.** Every source is drawn on the same reflectivity scale, nine
bands from 10 to 75 dBZ in the RadarScope tradition: greens for light rain,
darkening steadily to 35 dBZ, then yellows and reds. Paper and night themes use
the same pixels. Nothing below 15 dBZ is drawn, so insects and clear-air clutter
never film the map. There is no guessing at rain versus snow.

**The loop.** Recent frames play five a second with a short crossfade and a hold
on the newest. The newest scan time is printed beside the clock and is never
dressed up as "live". Opening the tab brings the loop up from cache in
milliseconds. A **+/−** control sets the zoom and remembers it across reboots,
and a zoom step keeps the loop playing until the new frames are in. **SMOOTH**
(off by default) softens echo edges for Region; it is disabled while the radar's
own cells are drawn.

**Data use.** Level III is downloaded only while someone is looking or the panel
was touched in the last 45 minutes. Left alone on a rainy day, the panel keeps
just the nearest radar's newest scan warm, about 3 to 4 MB an hour. A daily cap
keeps it bounded: past 150 MB only the newest frame is fetched, and past 250 MB
the radar view switches to IEM's pre-gridded tiles until midnight UTC. IEM tiles
also fill in, automatically and labelled, whenever Level III can't be reached.
`/health` shows the day's count.

![The Radar tab in Region mode: the NOAA MRMS mosaic over Puget Sound on a showery morning, with the Auto, Region and KATX buttons](design/almanac/screenshots/radar.png)

![The Radar tab on Auto at zoom 9: v2 draws the radar's own 250-metre cells, KATX and three neighbours merged pixel by pixel](design/almanac/screenshots/radar-site.png)

![Forty minutes of radar looping: showers moving across the Cascade foothills](design/almanac/screenshots/radar.gif)

### What the Almanac changes

- One dominant temperature and a plain-language forecast line, in place of six
  equal-weight panels.
- A barometer zone bar (Stormy / Change / Fair / Dry) and a 24-hour pressure
  barograph labelled with the day's high and low.
- Active US National Weather Service alerts in one strip below the masthead,
  coloured by severity and collapsed to one line when several are active. The
  7-day outlook stays on screen during an alert, compressed to highs and bars,
  because a winter storm warning can run for days, exactly when the week ahead
  matters most.
- Air quality (AQI) for the station's own coordinates, with a short forecast, so
  a rising smoke event shows before the number climbs.
- A 7-day outlook band. Each day's low and high is drawn as a bar on one shared
  axis, so a cool-down is visibly a shorter, lower bar. Condition glyphs sit
  beside the highs: sun, cloud, fog, rain, snow, thunderstorm, wind, and the
  compound marks for wind-driven rain and blowing snow. Rain chance appears only
  when it matters, with the expected amount in the station's own unit. Today's
  bar carries a dot at the observed temperature.
- A one-line hint about tomorrow under the headline ("Rain tomorrow",
  "Freezing rain tomorrow", "Blowing snow tomorrow"…), led by tomorrow's glyph.
  It appears only when tomorrow has a story; no line means fair weather.
- The Radar tab described above.
- A rainfall gauge scaled by intensity rather than linearly. The five named
  bands (Very Light through Very Heavy) each take a fifth of the tube, so
  drizzle registers and a downpour doesn't pin the needle. Light rain never reads
  as dry: the Tempest logs drizzle as an occasional trace minute with zeros
  between, so the rate uses a 10-minute window that bridges those gaps. The
  falling-rain animation builds quickly and eases slowly, so the sensor's
  minute-to-minute wet/dry flips read as a swell and a settle.
- A wind panel that resolves to one current reading, with a bolder compass.
- Snow awareness: in freezing weather with snow forecast, a dry rain sensor reads
  "Snow Likely" rather than "Currently Dry".
- Day and night: after sunset the Sun & Sky panel becomes Moon & Sky (phase,
  illumination, moonrise and moonset).
- A storm-aware layout: while lightning is detected, the Lightning tile takes the
  Sun & Sky slot so rain and strikes stay on screen together.
- A forecast curve for today that starts at the current reading and blends onto
  the hourly forecast over the next few hours. The sensor is the better guide
  for the next hour or two; the model is better for the rest of the day. The
  curve arrives at the model's own peak, labelled at its hour, which always
  matches the printed HIGH. Without hourly data it draws nothing ahead rather
  than guess.
- Honesty about silence. The masthead reads STALE when nothing new is reaching
  the screen, and SILENT when the engine is fine but the station has stopped
  reporting. `/health` reports the same difference. Before the first frame ever
  arrives (a cold boot, or the engine down), every value reads "—" rather than a
  sample number.

Both extra data sources degrade quietly. Alerts come from the US National
Weather Service, so outside the US the strip stays hidden. Air quality is
worldwide (Open-Meteo), and its panel hides wherever no reading is available.
Neither can stall the display: both are fetched off the main thread, keep their
last good value through a network blip, and are marked stale if the connection
stays down.

### Running the Almanac

- **HTML kiosk (recommended).** The console runs headless as a data engine, and
  `chromium --kiosk` draws the interface, using about half a core on a Pi. A
  systemd-supervised watchdog relaunches any piece that dies, restarts the engine
  if the feed goes stale, and checks for a wedged screen. Setup, management and
  revert steps are in [`design/almanac/kiosk/README.md`](design/almanac/kiosk/README.md).
- **Native Kivy layout.** Set `[Display] LayoutStyle = almanac` in the config.
- **12-hour clock.** Set `[Display] TimeFormat = 12 hr` in `wfpiconsole.ini`. The
  upstream default is `24 hr`. Every clock on the page follows this one setting.

For the architecture, the `wx.json` data contract and upgrade notes, see
[`design/almanac/ARCHITECTURE.md`](design/almanac/ARCHITECTURE.md).

### Viewing the Almanac remotely

Once the kiosk is running, the page is also reachable from any device on your
local network (phone, laptop, tablet) without any extra software:

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

For live weather readings (temperature, wind, rain, pressure), **yes**. The
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
setup (Websocket + REST mode), if you say you don't own hardware, the wizard
offers to find nearby public stations. Enter an approximate latitude and
longitude, and it lists the closest ones by distance with their hardware type. Pick one and it fills in the station and device IDs for you, so you get
full live readings with no hardware of your own. All you need is a free
WeatherFlow account and a Personal Access Token (see below). The station belongs
to someone else, so the feed stops if its owner makes it private or takes it
offline; re-run the wizard to choose another.

**What works without a Tempest:** the Almanac's supplementary panels (air
quality, weather forecasts and astronomy) pull from public APIs keyed only on
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

By default the AQI panel pulls from Open-Meteo, a CAMS satellite model that
needs no account. For readings that match [airnow.gov](https://www.airnow.gov/)
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

### Operating the radar

The engine reports its radar health at `/health`, under `radar`:

- `cache`: the tile cache's file count and bytes, its caps, and a summary of the
  boot scan. The caps come from free space at boot: 2 % of the free bytes on the
  cache's disk, between 8,000 files / 64 MB and 12,000 files / 256 MB. The file
  ceiling reflects boot time, not disk: the Pi 4 checks about 200 tiles a
  second, and the first radar pass waits for that scan.
- `lastError`, `localFailures`, `ambiguousFailures`, `breaker`, `hosts`: the
  fetch layer. Repeated local failures (a dead route, an exhausted client) stretch
  the retry from 2 s to 60 s. Provider failures move on to the next source
  instead. A stalled reused socket does neither.
- `native`, `nativeFallback`, `classification`, `mosaic`: Level III. Today's byte
  count and cap state, whether IEM tiles are filling in and why, and per-radar
  failures.
- `attention`: which of five tiers the engine is in (`live`, `warm`, `watch`,
  `rest`, `dormant`), why and since when, the weather holds, recent transitions
  and bytes per tier. With the tab closed and quiet weather, the engine checks
  scan listings plus a four-tile MRMS sentinel at home every hour by day (every
  two hours at night). Rain, lightning, a wet forecast, a touch or an
  approaching echo wakes it.
- `discovery`, `pending`, `phases`, `requests`: the acquisition schedule and the
  last 128 requests.

A few behaviours worth knowing:

- A Radar tab left open with nobody touching the screen for 30 minutes keeps its
  loop current but stops prefetching other zooms and sources. A touch restores it.
- The Rainfall tile reads "Rain Starting" the moment the station senses rain,
  before the next minute's observation arrives.
- A panel with the tab bar off (`WFP_TABS=0`, for a screen without touch) runs no
  radar at all. Set `WFP_RADAR=1` on the service to override.
- After a restart the Radar tab reads "Starting · checking N saved tiles" until
  the first pass publishes, about a minute on the Pi 4. Other panels show the
  previous run's last readings, marked with their age, until live data arrives.

The engine logs one line per radar pass (`radar pass outcome=...`). A pass that
yields to an internal budget says `error=deferred: <reason>`. On the Pi 4 kiosk
the engine log is `/tmp/almanac_data.log`; the two previous runs are kept as
`.1` and `.2`, each starting with the reason the engine was started. The tile
cache lives under `~/almanac_web/radar`.

To inspect the running page, the kiosk's Chromium listens on a loopback debug
port. `design/almanac/kiosk/tools/cdp_probe.py` evaluates a JavaScript expression
there and prints the result:

```bash
python3 design/almanac/kiosk/tools/cdp_probe.py 'radarReady().length'
python3 design/almanac/kiosk/tools/cdp_probe.py '({mem: radarMemory(), cap: RAD_MEMORY_CAP})'
```

### Tests

The fork's data pipeline (the observation parser, the `wx.json` emitter and a
merge-safety guard) has an offline pytest suite. It needs no Kivy, display or
network, so paths like a lightning strike are tested without waiting for real
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

**[The Almanac UI (this fork)](#the-almanac-ui)**<br>
&nbsp;&nbsp;&nbsp;&nbsp;[The Radar tab](#the-radar-tab)<br>
&nbsp;&nbsp;&nbsp;&nbsp;[What the Almanac changes](#what-the-almanac-changes)<br>
&nbsp;&nbsp;&nbsp;&nbsp;[Running the Almanac](#running-the-almanac)<br>
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
