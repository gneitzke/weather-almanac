# Changelog

Changes in Weather Almanac, newest first. The upstream WeatherFlow
PiConsole keeps its own release notes; entries under **Core** below are fixes
to shared upstream code that the classic console benefits from too.

## 2026-09-26

### Console
- **Wind figures line up.** The AVERAGE row was wider than the column beside
  the dial, so its value ran about 15 px past GUST and MAX. The wind ledger now
  takes its widest row's width: all three values end on one right edge and the
  shorter rows' dot leaders stretch to meet them. The labels do not move.

## 2026-09-25

### Radar
- **v2-only review fixes.** Watch publication lag retains the last frame and
  retries at negative-cache expiry without provider strikes or warnings before
  ten minutes. Primary selection respects the saved viewport, and watch cannot
  change Auto's mode using incomplete neighbour evidence. Attended loops rebuild
  watch frames with full coverage; warm keeps its four-frame mosaic loop even
  off-tab. Shadow no longer applies watch's primary-only restriction.
- **Cooldown fallback and honest recovery.** N0B cooldowns select IEM tiles;
  Level III Retry-After is capped at five minutes. An expired timer alone does
  not claim recovery: a validated N0B fetch must succeed. Level III budget notices are Site
  only, without duplicate fallback-caption text or replacing Region's scale note.
- **Second v2-only review fixes.** A Level III 429 inside a pass now switches
  Site to labelled IEM tiles within seconds instead of waiting out the
  cooldown. A stalled NOAA feed in watch is measured from the first unpublished
  scan: after ten minutes it warns (rate limited) and draws IEM tiles captioned
  "NOAA Level III delayed", returning to v2 when a newer product publishes.
  Watch skips a dark nearest radar for the next reporting one, as live does.
  Watch holds only a mode Auto chose; a Region forced by Site failures is
  re-evaluated. The fallback reason appears once, in the caption; renderer
  changes say "updating newest frame".
- **Review regression coverage.** Native-on Region evidence checks are restored,
  including a cooldown beginning during a pass. General hybrid tests no longer
  force native off; the IEM topology fixture owns that override.
- **One site renderer on every board.** Site always draws the NOAA Level III
  per-pixel mosaic with N0H clutter control, including on Pi 3. The v1 | v2
  switch and saved renderer preference are gone. IEM site tiles fill in only
  while Level III is unreachable or the daily ceiling is paused; the caption
  labels the fallback and recovery, keeping the old map until new tiles decode.
  Smooth remains available for Region and IEM fallback.
- **A small native scan stays warm unattended.** Watch keeps only the primary
  radar's newest N0B and optional N0H, about 2.75–4.26 MB/hour in rainy weather.
  Opening Radar backfills the mosaic loop. Rest and dormant fetch no Site tiles
  or Level III products. Rest keeps its four-tile MRMS sentinel at home every
  hour by day and every two hours at night;
  the 150 MB newest-only and 250 MB pause ceilings still count every body byte.
- **Quiet publication lag, visible outages.** Per-site failures remain counted
  in health, but warnings now cover transport failures and scans still
  unpublished after ten minutes, with the existing rate limit. Whole-network
  retry behaviour is documented and tested across the 120-second fallback window.
- **Remote control from your home network.** A browser on a Mac or phone can
  zoom, pan, choose Auto, Region or a site, and change Smooth; the
  panel follows the same engine view. The last user action takes control.
  Other pages and reloads follow without stealing it or getting stuck on
  “Updating view”. Remote touches count as presence; only the panel reports
  live viewing and painted frames. Per-client write limits keep polling readable.
- **v2 merges nearby radars pixel by pixel and filters clutter.** The seams
  where one radar's layer ended are gone. Each pixel now shows the echo from the
  lowest radar beam that sees one, so a beam blocked by the Cascades no longer
  hides a neighbouring radar's rain. NOAA's own classification product (N0H,
  about 25 KB a scan) removes ground clutter, and clears birds and insects unless
  they sit inside rain, where the classifier is least reliable. A radar whose
  classification is missing is marked "unfiltered" in the caption and cannot
  paint over clear sky measured by a filtered one. Measured on the Pi 4: a cold
  eight-frame loop at zoom 9 builds in 25 s (was 50-60 s), a restart reuses the
  tiles on disk with no new downloads, and a mosaic tile renders in about 8 ms
  for one radar and 36 ms for four overlapping ones.
- **Auto picks the radar by zoom.** A new **Auto** button, the default, sits
  beside Region and the nearest site. Zoom in to 8 or closer and it switches to
  the site radar; zoom out to 6 or wider and it returns to Region; at 7 it keeps
  whatever is showing, so a pinch back and forth never flips the source. It only
  goes to the site when reporting radars cover at least 85 % of the view (70 % to
  stay), waits 10 s before reversing unless the zoom moved two levels, and keeps
  the old loop on screen until the new one has frames to show. A failed listing
  or a busy moment no longer drops it to Region; a radar that is really off the
  air, or whose picture has gone stale, still does.
- **Manual choices time out.** Tapping Region or the site holds that choice until
  you tap Auto or nobody touches the screen for 45 minutes.
- **Every US radar.** Swept all 160 sites from the Pi 4: 157 decode cleanly
  (KGGW was off the air; the Azores and Okinawa radars are not in NOAA's
  bucket). Site mode no longer requires the continental-US mask, which only
  bounds MRMS, so stations in Alaska, Hawaii, Puerto Rico and Guam use their own
  radar.
- **More contrast in the greens.** Light, moderate and heavier rain now separate
  at a glance: the greens get darker steadily from 15 to 35 dBZ instead of
  jumping back and forth, with a clear step at 25 dBZ. Every colour still clears
  2:1 contrast on the paper theme and 3:1 at night; yellows and above are
  unchanged. The grey clear-air band, unused since the 15 dBZ floor, is gone.
- **Zooming v2 stops re-downloading.** The decoded-scan cache held 24 scans; a
  loop across four radars needs 36, so every zoom step fetched products again.

## 2026-09-24

### Kiosk
- **An engine restart keeps the evidence.** The launcher overwrote the engine log
  every time it restarted the engine, so its watchdog destroyed the record of why
  it fired: on 2026-09-24 the live Tempest feed stalled twice while WeatherFlow's
  own records show the station reporting every minute, and nothing was left to
  explain it. Each start now rotates the log (the two previous runs are kept as
  `.1` and `.2`) and writes its reason on the first line: `boot`, `died`,
  `data-stale`, `data-error` or `sensor-silent`.
- **No dead Menu button.** The tab bar carried a dimmed "Menu" button with no
  function; on a touchscreen it was a target that did nothing. Removed.

### Radar
- **v2 draws the radar's own cells.** A new **v1 | v2** switch beside SMOOTH. v1
  is the site radar as before, from IEM's tiles, which are gridded to about 1 km
  before we see them. v2 reads NOAA's Level III base reflectivity from its public
  AWS bucket and draws each tile from the radar's own gates, half a degree by
  250 m, so zoom 9 and 10 show squall lines and cell edges the way a phone radar
  app does. Same scan, same colours, same loop; the caption credits NOAA Level
  III. It applies to the single-site view (Region stays v1) and remembers the
  choice across reboots. This is phase 0: nearby radars are layered, not yet
  merged cell by cell, and a cold switch downloads about 9 MB and takes about a
  minute to fill all eight frames on the Pi 4.
- **The header no longer calls a current loop old.** While the loop played, the
  "As of" line took its age from whichever frame was on screen, so a fresh loop
  read "48 min old" on its oldest frame. It now warns only when the data itself
  is old; each frame's offset stays on the loop caption.
- **The radar draws rain, not clutter.** Nothing below 15 dBZ is drawn now, from
  any source, and the grey clear-air band is gone from site mode. On a dry
  afternoon with a 0 % forecast, 49 % of what the panel drew was that grey band
  and another 32 % was 10 to 15 dBZ: insects, birds and ground clutter fanning out
  around each radar. Light rain starts above the floor, so every shower still
  shows. The colour ramp itself is unchanged; the legend now starts at 15, and the
  render revision moved, so every cached tile re-renders once.

## 2026-09-23

### Before the rain
- **Radar no longer flips between live and warm while you watch it.** About
  twice an hour an open Radar tab dropped to warm and back, cutting the loop
  target from 8 frames to 4 and cancelling any pass in flight. The server only
  cleared the "tab open" marker on polls that were accepted for camera
  ownership, so once a radar session owned the camera the tab-closed signal never
  fired and the engine guessed from a 10-second silence. Every kiosk poll now
  writes or clears the marker; silence alone must last a minute.
- **An open Radar tab nobody touches stays live but stops prefetching.** A panel
  left on Radar counted as someone studying it all day (21.6 MB by 3 PM). After
  30 minutes without a touch it keeps the full 8-frame loop current and drops the
  zoom and mode prefetch; the next touch restores it.
- **Rain onset is instant.** The Tempest sends a rain-start event the moment its
  sensor feels rain; both transports discarded it, so the panel waited up to a
  minute for the next observation. The Rainfall tile now reads "Rain Starting"
  until that observation confirms or contradicts it (never longer than five
  minutes), and the radar warms at once.
- **Reviewed before the rain.** Astra's review hardened all of the above: the
  page reports Radar visibility as an ordered message, so a stray or delayed
  request can never clear or resurrect it; going unattended no longer cancels the
  visible loop, only optional warming; the silent budget note requires the
  current view's frames actually on screen; the rain-start event is validated and
  timed by the Pi's clock, so a station clock that drifts cannot hide it.
- **Quieter radar notes.** The history note counts the frames of the loop on
  screen ("frame 6 of 8", never "frame 25 of 31"), and the engine pacing itself
  ("Retrying · work budget") is not announced while the whole loop is on screen
  and current.

## 2026-09-17

### Radar
- **Attention tiers.** The engine used to keep the newest radar frame warm forever
  (22 to 35 MB a day with the tab closed). It now spends where a person is likely
  to look and weather is worth looking at: `live` (tab open), `warm` (a touch on
  any screen or a view within 45 min: newest plus four frames, so a tap opens on a
  moving loop), `watch` (rain, lightning, echo or a wet forecast: the loop stays
  warm by day), `rest` (listings only plus a four-tile sentinel at home each hour
  to see rain approaching), `dormant` (quiet nights and absences: one listing an
  hour). Promotion is immediate, demotion waits; unknown observations are never
  "dry"; a LAN browser polling is weak evidence. The Radar tab shows a small
  mark when weather is nearby, the page tells the engine about touches, and a
  wake says "Waking radar · showing HH:MM while the newest scan loads". Bytes
  per tier, holds, transitions and the sentinel are in `/health.radar.attention`.
  Glance-hour priors are collected and stay inert until two weeks of data exist.
  `WFP_RADAR_ATTENTION=shadow` publishes without applying; a `radar_attention_force`
  marker overrides for testing. Modelled: about 1 MB on a dry day, 15 to 20 MB on
  a rainy day with use, 6 to 7 MB a day over a Pacific Northwest month.
- **Attention tiers, reviewed and hardened.** Astra's adversarial review (10
  findings, 76 tests) is merged: presence promotes the tier before a pass can
  start, so a changed preference file cannot bypass a quiet tier; quiet floors
  are measured from the last quiet check; a demand change supersedes a running
  pass; "waking" ends only on a fresh complete frame; echo counts pixels at or
  above 10 dBZ, never opacity, and a frame needs 200 of them; the sentinel
  validates its tiles, reports coverage and looks at zoom 7 (about 425 km
  across) instead of zoom 5 (1,700 km); "Chance of rain 10%" is judged by its
  number. Three panel-found fixes share one lesson: the discovery schedule owns
  its due time, so tier floors and prompt wakes live on the wakeup, never in the
  schedule. All verified on the panel over CDP with a forced tier marker.
- **No tabs, no radar.** A panel without the tab bar has no way to reach the
  Radar tab, yet the engine still scanned the tile cache at boot, acquired a
  loop, pre-warmed geography and ran listings. The launcher now derives
  `WFP_RADAR` from `WFP_TABS`, and with it off the engine runs none of that and
  publishes `radar.reason: "radar off"`.
- **"Weather nearby" means rain, not insects.** The Radar tab's mark stayed on
  through a dry night: the KATX frame carried 315,000 pixels at 10 dBZ and
  6,900 at 20, none at 30, the nocturnal biology and clutter signature, while
  the previous evening's real showers in the mosaic had 10,000 at 30. Echo now
  counts pixels at 25 dBZ or more and needs 0.1 % of the footprint (frames and
  the sentinel alike); the dry night measured 0.04 %, the showers 2 %. The
  newest frame's count is in `/health.radar.attention.frameEcho` for tuning.

## 2026-09-16

### Zoom
- **A zoom-out no longer hangs the radar.** Zooming from 8 to 5 asks for tiles
  at a level the engine has not rendered and for history frames it has not
  reached. The page reported every 404 as a bad tile and re-fetched it 2 s
  later (1,241 reports in one minute on the panel), and when the request gate
  refused a history frame the engine yielded with a budget of zero, so its
  retry fired 2 s later into the same full window, one probe per pass, with
  the caption stuck on "Retrying view · work budget · next attempt now". A 404
  is now "not yet": the page backs off per tile (2 s doubling to 30 s) and
  reports only a tile that arrived and failed to decode; the engine asks for
  one frame's headroom on that yield and names every yield in the pass log
  (`error=deferred: <reason> needed=N at=<function:line>`). Below zoom 7 the
  site mode still hands over to Region by design; the note says "KATX resumes
  at zoom 7".

### Restart
- **Nobody is stranded by a restart.** For about a minute after every engine
  restart the Radar tab vanished (the boot scan had no result yet, so the engine
  said "unavailable"), a user on the radar screen was bounced to Observations,
  and every observation blinked to a dash for six seconds before the first live
  reading. The engine now publishes `radar.starting` with its phase and tile
  count, the page keeps the tab and shows "Starting · checking N saved tiles",
  and it infers the same state when an older engine restarts underneath it. The
  engine reads its own last `wx.json` at start and republishes the last known
  observations, forecast and air quality with their real age until live data
  lands, so a restart never blinks the panel to dashes. (The kiosk wipes the
  browser profile on every start, so this cannot live in the page.)

### Tools
- **A CDP probe for the live kiosk** lives at `design/almanac/kiosk/tools/cdp_probe.py`:
  run on the Pi, it evaluates a JavaScript expression in the panel's Chromium over
  the loopback debug port and prints the value. Every session used to rewrite it.
- **The Radar tab's picker**: a tap on the mode already showing records the
  preference and posts the intent, but never enters the switching state.

### Clock
- **A clock string never breaks.** "7:13 PM" wrapped the masthead onto two
  lines and "Clear until 1 AM" orphaned its "AM". Every meridiem now follows a
  no-break space, in the emitter's own strings and in the upstream ones it
  republishes (sunrise, moon, extremes, forecast hour, conditions, Sager), the
  masthead sets the meridiem small beside the hour, and the lightning flag no
  longer reserves its width while hidden.
- **One clock format everywhere, and it is 12-hour unless you say otherwise.**
  The masthead clock, alert timestamps and radar check times were hardcoded
  24-hour while the AQI peak hour and an alert's "until Wed 5 PM" were hardcoded
  12-hour, so every console showed a mix whatever `Display/TimeFormat` said.
  Everything the emitter formats now follows that setting, the same one the
  sunrise, observation-extreme, forecast and Sager modules already follow, and
  the page derives a missing frame label in the payload's own style, and the
  Sager issue time says "PM" like everything else instead of upstream's "pm".
  The setting itself stays upstream's (`lib/config.py` is guarded for
  mergeability, default `24 hr`); the almanac install notes say to set
  `[Display] TimeFormat = 12 hr`, and the panel is set so.

### Radar
- **Final adversarial review, 11 fixes.** Boot reconciles the tile directory
  to the inventory (unindexed and temporary files go, a partially scanned
  stamp does not keep unindexed leftovers), admits both render revisions
  newest-first under one age order, and never follows a symlinked cache root.
  A tile write prunes before creating its directory, since eviction could
  delete the destination. The acquisition deadline starts after the boot
  scan wait. Discovery wakeups honour the local-failure floor. (The review
  also reclassified permanent DNS answers as provider failures; on Linux that
  turned a plain offline hour into 3,250 log lines and flapped the fallback
  chain, so every DNS failure stays local and backs off.) The page no longer treats the
  engine's coverage camera as plate identity, keeps the target manifest
  through a pan during a switch, and finishes a blend before starting the
  next under a slow animation clock.
### Radar
- **A full tile cache is trimmed at boot, never frozen.** The Pi 4 rebooted
  with a full cache: 8,000 tiles, exactly where the running prune keeps it.
  The boot scan stopped at the cap with directories still unvisited, counted
  that as a bounded scan, and marked the cache read-only for the life of the
  process. Every tile after that was fetched, rendered and discarded once its
  directory existed: no radar all evening, and the pass log said
  `outcome=deferred error=None`. Any full cache plus a restart did this. The
  scan now indexes newest-first within its entry bound, removes the stamp
  directories a bounded scan never reached, evicts oldest-first down to the
  caps, and stays writable.
- **Cache caps follow free space.** 2 % of the free space on the cache's disk,
  between the old floor (8,000 files / 64 MB) and a ceiling of 12,000 files /
  256 MB. The ceiling is the boot validation cost, not disk: the panel opens
  every cached PNG once on the inventory thread at 5 ms each (8,000 tiles
  took 40 s), and a pass waits 30 s for that scan before it retries. Tiles
  average under 2 KB, so files bind long before bytes. `/health.radar.cache`
  reports the caps, the live count and bytes, readiness and the startup scan
  summary, and a pass that yields internally logs `error=deferred: <reason>`.
- **Local failures back off.** A dead route or resolver is never the provider's
  fault, so it never opens a host breaker, and each failed pass retried after a
  flat two seconds for as long as the network stayed down. Consecutive local
  failures now double the retry from 2 s to a 60 s ceiling; a completed pass, an
  unchanged discovery or any other kind of failure resets it. The backoff is a
  floor under every scheduled radar retry, so a partial-frame pass cannot re-arm
  a two-second retry over it. Ambiguous stalls on a reused socket are classified
  apart from local failures: they still never advance the fallback chain, but
  they keep the two-second retry. The fallback chain itself is unchanged.
- **A tap on the mode already showing is silent.** After the engine refused a
  site tap, tapping Region said "Switching to Region · showing Region" and put
  the segment into its pending state, although Region never left the screen.
  The tap now records the preference and posts the intent without entering a
  switch, so the caption and the picker stay settled.

## 2026-09-15

### Radar
- **Radar v6.3 gives retries a timer-owned lifetime.** `refresh.nextRetry` and
  `refresh.retryReason` exist only for a successfully scheduled future retry.
  Dispatch, scheduled pass start, fulfilled completion, unchanged discovery,
  supersession and stop clear the retry; intent work preserves only a still
  pending future validation. Replacement callbacks are fenced and serialization
  omits expired timestamps even when dispatch is late.
- Both themes use the payload's `ts` for retry copy, name the budget/deadline/
  provider/local/site cause, and say “next attempt now” below one second. A UI
  switch timeout alone keeps “Updating”/“Switching” copy. Automatic buffering
  settles on advancing playback without waiting for an unrequested intent ACK.
  Expired retries leave settled captions and off-air refusal notes intact. The masthead's existing
  data-age freshness rules are unchanged and now covered against retry state.
- Deterministic engine lifecycle tests and both-theme headless regressions cover
  expired/future retries, skewed browser clocks, subsecond copy, off-air refusal,
  and independent STALE behavior. Verification is local only.
- **Radar v6.2 bounds routine outage logging.** Each pass reports outcome,
  attempted source/site, elapsed time, request totals and failure classes, hedges,
  breaker state, next retry seconds and one escaped, bounded error. Independent
  pass counters include inner transport retries and survive history eviction;
  the two 128-entry histories remain unchanged in `/health.radar`.
- Identical `(source, exception class, message)` warnings repeat at most every
  ten minutes (five 120-second discovery backoff intervals). Changed failures
  report immediately; reminders, changes and verified recovery account for
  suppressed repeats. Concurrent site-listing failures share the same policy.
  Retry timing, transport admission and fallback decisions are unchanged.
- The reproducible offline 1,800-pass/hour benchmark reduces Region logger
  messages from 69,866,789 to 427,300 bytes/hour and Site from 71,198,989 to
  399,043, including newlines, excluding logger-specific prefixes. Actual pass
  lines are 237/221 bytes; regression tests enforce <768 bytes even with both
  histories saturated and a long escaped error. No panel measurement is claimed.
- **Radar v6.1 keeps Smooth within the native page memory model.** The engine
  interpolates weighted reflectivity/coverage at 2×, then area-reduces to 256px
  before applying the legend LUT. Tiles, merge scratch, 128 KiB response limits
  and 786,432-byte job reservations now match Smooth off; the 40 MiB cap stays.
  A new Smooth revision prevents old 512px caches from aliasing. At zoom 7 the
  effect is softened gate edges at 1:1, as the button and contract explain.
- **Reservations have one cleanup owner.** Radar and geography jobs transfer
  retained-tile ownership from their existing slot and return the remainder in
  `finally`; busy/pending cleanup precedes diagnostics, including cap throws.
  Preference publication cancels superseded work and fences late completions
  while preserving the v5.9 playing cycle until four replacements and wrap.
  The note retains v6.0’s “Playing previous view · sharpening N of M” wording.
- **Refused admission waits for changed memory or a poll.** Queue rebuilds and
  animation frames cannot restart an unchanged refusal. Deterministic four-job
  lifecycle tests and both-theme headless handoff/memory/scheduler checks cover
  the regression. Local Mac Smooth decode/remap/encode medians are 6.82–8.62 ms
  per tile, +3.16–3.71 ms over off; these are not panel performance measurements.
- **Radar v6.0 carries closest-site evidence into the picker.** `nexrad` adds
  `reporting`, `newestTs`, `ageSec`, `reason`, `checkedTs`, `checkedAt`,
  `nextCheckTs` and `nextCheckAt` from the last listing and existing discovery
  schedule. Region checks the closest eligible site on that same wakeup, within
  the shared deadline/budget and footprint-aware reserve (at least 34 requests).
  Listing results are shared across the pass; tile failure cannot erase the last check.
  Empty-listing site taps are refused immediately, with `refresh.reason:
  "not reporting"` and `sourceFallback:"site-not-reporting"`, while Region
  continues updating. The site remains tappable and names its last check or
  measured scan age. The note uses the scheduled check time; Region caption
  copy stays unchanged. Unknown/failed listings never claim an outage start.
- **Zoom notes separate retained playback from incoming acquisition.** While the
  previous view loops, the note says “Playing previous view · sharpening N of M,”
  counting only incoming decoded composites. A coincident dirty-camera paint and
  playback deadline now paint the successor once. Local Chromium counters found
  a maximum of two echo paints per RAF before, one after; native decode admission
  remains at most one per RAF. With eight scans actively looping, renderer task time was 92–122 ms per
  0.6-second step before and 99–116 ms after, without a material CPU reduction.
  Engine, both-theme state-machine and v5.9 retained-window checks cover the
  change; `tools/benchmark_radar_v60.py` records reproducible local counters.
- **Radar v5.6 adds durable Smooth.** A quiet 44px-target toggle beside zoom reset
  defaults off and persists through the loopback-only `radar_smooth` marker.
  Smooth upsamples native reflectivity 2× with valid-coverage bilinear weights,
  then applies the legend LUT; missing/below-floor gates never supply intensity.
  Echo scaling follows the selected variant. Both rendered revisions share the
  existing disk cap. v6.1 supersedes its larger browser tiles with native-sized
  output under the same 40 MiB cap. Native byte reuse and warming tiers remain
  unchanged. Offline field/persistence tests and both-theme browser checks cover
  the toggle; an offline benchmark reports per-tile CPU cost.
- **Radar v5.9 keeps zoom/pan playback alive.** Camera settle and manifest geometry
  changes retain and reproject the playing cycle until four replacement composites
  decode, then adopt at the existing wrap. The frame read keeps its timestamp;
  the corner note reports acquisition. Decode admission is serialized to one per
  animation frame after paints. Incoming history pauses at four until adoption,
  with oldest-first outgoing release and the existing 40 MiB admission cap.
- **Stalled bodies get warm hedges.** Two seconds without received-byte progress
  now races headers/body stalls as well as silent first bytes. Every received
  chunk rearms inactivity; `stallHedges` identifies the progressed-response subset.
  Connection, request, attempt and absolute deadline limits remain unchanged.
  Real TLS regressions barrier the body stall and drive hedge time explicitly.
- **Radar v5.4: greener light rain and measured site operating copy.** The first
  three reflectivity bands now use Fable v5.2 greens; the remaining ramp, clear-air
  slate, alpha mapping and legend geometry stay unchanged. Legend and remap
  revisions invalidate previous rendered tiles. Contrast is computed across all
  26 LUT entries for both themes. Site captions use the primary listing's median
  of the latest three gaps, infer precipitation/clear-air mode only outside the
  ambiguous range, and reserve “scanning slowly” for intervals over 15 minutes.
  Caption fitting drops the interval before the inferred mode.
- **Warm hedge admission recovers when a lease returns.** A denied hedge check
  no longer disables hedging for the rest of a tile race. Bounded 50ms checks
  reuse the original deadline, request cap and sole second attempt. A scripted
  clock regression reproduces the missing tile on the old race and delivers all
  ten tiles with healthy second attempts; real loopback TLS coverage remains.
- **Radar v5.8 removes cache inventory from the worker's filesystem path.** A
  bounded startup worker validates the disk cache once; writes, evictions and
  explicit page damage reports maintain its memory index. Warm hits skip PNG
  decode/read-back and executor work. Scan/level masks and site coverage are
  reused and pressure eviction uses indexed records. Geography raster palette
  composition now uses Pillow operations
  with identical output pixels. JSON publication remains on its two-second tick.
- **Ordinary tile retries retain their second attempt.** Each attempt may use
  six seconds within the unchanged batch/pass deadline; committed-camera tiles
  retain their two-second total deadline and warm-only hedges. No-warm-lease
  timeout/failure and synchronized warm-hedge regressions cover both policies.
- Local before/after worker profiles, cache-I/O counting tests, bounded startup
  measurements and the real TLS/page switch harness cover the v5.8 root fix.
  The three-site warm switch and cold 15-tile Region newest both reach eight
  decoded frames in both themes; no Pi or LAN access is needed by these checks.
- **Radar v5.7 makes intent ordered and switch completion bounded.** Session,
  generation and heartbeat fencing prevent delayed requests or old failure
  payloads from replacing a newer choice. Reload reconciles accepted intent;
  Auto survives durable restart. Pending captions paint in the input frame.
  Healthy switches/zoom with rate capacity must decode four frames and advance
  playback within 20 seconds; expiry visibly retries while keeping old imagery.
- **Mandatory visible work comes first:** newest → four → eight frames, then
  margin/opposite-source/adjacent-zoom warming, then deep history. Pending work
  survives discovery and busy-worker wakeups. Reserves count actual missing
  tiles and site layers; camera footprint controls capped-source coverage.
- **Both sides now bound acquisition.** Mandatory camera tiles get two seconds
  for at most two attempts; browser headers/body/decode get 2.5 seconds and
  cancellation, with a three-second compositor progress escape. DNS/local and
  ambiguous reused-path errors do not blame providers. Acquisition-objective
  failures qualify fallback, and failed fallback can escape recovery dwell.
- **Publication and long sessions retain honest state.** Panned regression
  guards preserve newer history; captions age the painted frame. Bounded caches,
  process-wide resolver ownership and exact visible-loop disk pins prevent
  accumulated work. Publication uses inventory membership rather than per-tile
  filesystem probes (v5.8 extends this ownership across passes). Phase/request telemetry and deterministic plus real TLS
  loopback acceptance cover both themes, budget pressure and 30% hangs.

### Radar v5.3 baseline (superseded where v5.7 differs)
- **Radar v5.3 uses one settled camera intent.** Activity reports supply runtime
  zoom and centre; durable zoom follows after a debounce and is used only for
  cold start. Stepper, pinch and restored cameras share this path. Provider zoom
  caps select native tile resolution without moving the camera.
- **Connection setup is bounded per host.** Hedges reserve warm pooled sockets
  and cannot open connections. At most two connections perform TCP/TLS setup at
  once; shared TLS contexts reuse session tickets. Client setup/pool timeouts and
  cancelled attempts do not lower host health. Losing more than half of issued
  hedges in 60 seconds suspends hedging for five minutes. A tile retains its
  two-attempt, six-second total budget, including sequential stale-socket recovery.
- **Source transitions retain the displayed loop.** Three consecutive provider
  failures are required for fallback; local failures cannot trigger it. A new
  source builds at least four complete frames before publication, and recovery
  waits for a five-minute dwell. Every source switch logs its reason.

### Console
- **The board runs on plain http again.** Since radar v5.7 the page called
  `crypto.randomUUID()` at top level to name its radar session. Browsers offer
  that only in a secure context (https or localhost), so the kiosk was fine but
  a phone or laptop opening `http://<pi-ip>:<port>/` threw
  `crypto.randomUUID is not a function`, the whole board script stopped, and the
  page kept the artboard's sample numbers with no visible error. The session id
  now prefers `crypto.randomUUID` and otherwise builds a version 4 UUID from
  `crypto.getRandomValues`, which works on plain http; the result still passes
  `serve.py`'s `radarSession` check. Offline tests pin the guard and run the
  fallback in node; verified in Chromium over the LAN address with
  `isSecureContext` false.

## 2026-09-14

### Radar
- **Radar v5.1 keeps the closest-site control stable.** The site segment and its
  accessible name stay on `nexrad.id` when that site is dark, with the existing
  drawn-contributor count. Captions retain the drawn site's name and outage
  suffix. “High resolution” appears in the accessible name and, when it fits,
  the caption; it yields before the existing distance/cadence/nearby drop order.
  Health counters now separate overlapping two-second hedges from second
  attempts after failure; a hedge no longer increments retries. Local unit and
  both-theme browser/picker regressions cover identity, copy, fitting and counters.
- **Radar v5.0 discovers scans at expected readiness.** A cancellable one-shot
  replaces the unaligned 180-second poll. MRMS predicts the next stamp plus a
  300-second provider lag, then re-polls every 20 seconds; site listings predict
  the next volume from recent spacing and re-poll every 30 seconds. Six fast
  attempts are bounded by 120-second backoff, host breakers and the shared
  240/minute budget. Unchanged discovery ends after metadata/listings. New scans
  retain the sliding manifest and wrap playback, reserve, partial repair and
  prefetch tiers. The lag predicts discovery without suppressing early scans;
  newest archive misses expire within a re-poll. Health/INFO expose expected
  readiness, next/last poll, bounded polling state and live age. Deterministic
  scheduler tests and a five-stamp, both-theme loopback TLS/browser harness cover
  publication jitter and the warm-path fetchability-to-screen/age targets.
- **Radar v4.9 isolates request failures from the displayed window.** Newest
  tiles hedge after two seconds without a response byte, using fresh connections
  and three reserved rescue leases. Each tile gets at most two six-second
  attempts; admitted hedges/retries share the unchanged 240/minute gate and a
  per-pass hedge cap. Failed tiles stay local, partial newest inventory publishes,
  and its repair precedes history/warming. A 60-second host health window opens
  a 30-second circuit below 50% success with six samples, then admits one recovery
  probe. Source acquisition gets 16 seconds inside the existing 25-second pass,
  with immediate eligible fallback. The artificial five-minute MRMS readiness
  delay is removed: acquisition starts at the advertised scan. `/health.radar`
  and per-pass INFO expose success, hedges, retries, breakers and last error.
  Existing failure copy appears only after a failed pass with newest older than
  two cadences. Fake-origin, real loopback TLS and both-theme browser scenarios
  cover recovery, deadlines, partial repair, rate gates and retained playback.
- **Radar v4.8 bounds silently dead pooled sockets.** Reused connections wait at
  most three seconds for the first response byte, then retry a zero-byte
  GET/HEAD failure once on a fresh connection. Partial responses and fresh hangs
  are never replayed. One absolute 25-second budget now spans the whole pass,
  including pooled workers, DNS waits, TLS, headers/body, retries and history;
  trickled reads cannot restart the timeout. Idle reuse defaults to two seconds
  (or the shorter advertised Keep-Alive), with guarded TCP keepalive settings
  of 2/2/2 seconds/seconds/probes on Linux. INFO telemetry separately counts
  stale-first-byte retries; successful recovery keeps an honest idle refresh
  state. Real loopback HTTPS tests cover six hanging reused sockets recovering
  on fresh connections in about three seconds and bounded failure when retries
  also hang. No panel or mesh timing claim is made.
- **Radar v4.7 keeps the manifest sliding during a new scan.** At unchanged
  source, geometry and legend, the first pending-newest publish retains the
  previous hour's frames and complete counts. Slow newest tiles cannot replace
  the window with one frame; completion updates that scan in place. Site history
  keeps its original per-site scan identities. Cold starts and new geometry keep
  their existing first-measurement behavior. The page independently retains
  omitted in-hour frames across truncated manifests, preserving composites,
  playback deadlines and scan-time reads; true identity changes release them.
  Mocked engine tests and both-theme loopback checks cover a 25-second newest
  fetch, retained bitmaps, zero old-scan fetches and adoption at the v4.6 wrap.
- **Radar v4.6 slides the window at the wrap.** Manifest updates keep the running
  scan, 120ms crossfade and deadline intact. After the old-newest 1100ms hold,
  playback wraps to the slid window's oldest decoded scan and reaches the new
  newest at the existing 350ms cadence. Decoded frames on both sides of a gap
  count; late frames join at the next wrap. Playback now enforces the four-scan
  start shown by the buffering read. Paused updates preserve the displayed scan;
  cold acquisition shows newest immediately and AS OF follows the manifest.
  Stable frame identities retain composites; aged-out bitmaps close after their
  last display/blend use. Native acquisition order and cache reuse are preserved.
  Deterministic both-theme checks cover wraps, zero resident-native refetches,
  late decode, paused/cold states, reads and reduced-motion single sweeps.
- **The loop breathes: 350 ms between scans, and the read tells the truth.** After 200 ms
  the user asked for a longer pause; scans now advance every 350 ms (newest hold and
  crossfade unchanged, hard cuts under reduced motion). The inventory read measured
  itself against a hard eight and said "Buffering · 6 of 8" forever on a ten-minute
  site with six scans an hour, while the loop was in fact running; it now counts the
  scans that exist and the loop's own start of four.
- **Radar v4.5 removes the hatch entirely (user decision, superseding P4/v4.4).**
  Missing tiles draw nothing; same-stamp parent tiles still fill in while finer
  tiles load. The drawing pattern, hatch token use, 400ms timer, coverage/40%
  gates and partial-coverage aria suffix are deleted. Incomplete inventory
  reads `Buffering · N of 8` (or `Paused · N of 8`); the loop read and refresh
  note carry acquisition state. Empty regions may mean not yet loaded.
  Both-theme regression checks assert no hatch elements or pixels.
- **Radar v4.4 keeps hatch to a few real holes.** The 400ms hatch requires the
  manifest's expected bit and drawn site-range/MRMS coverage, clips at coverage
  edges, and disappears when more than 40% of expected cells are missing.
  Acquiring views use the loop/refresh copy. The reporting nearest radar keeps
  its subject while tiles are late, with `KATX loading` instead of `timeline KATX`.
- **Smoother local playback.** Frames advance every 200ms with the existing
  1100ms newest hold and a 120ms linear blend of two real scans. RainViewer's
  interval formula scales by 200/110. Nearest-neighbour sampling stays; reduced
  motion hard-cuts through its opt-in single sweep. Eight cached composites fit
  the existing 40MiB cap; playback fetches and decodes nothing.
- **Radar v4.3b explains the source choice.** Region replaces Mosaic; the live
  callsign keeps its nearby count, with descriptive accessible names on both
  segments. Captions name the radar and new-image cadence, keep visible provider
  credit, and attach station distance only to the nearest drawn primary. The
  grey-band explanation now sits under the legend. Overflow drops optional
  detail in order and clips within the caption's box.
- **Switch copy follows the picture.** The displayed source stays named while
  pending; `switching` appears after the existing 600ms grace. A refused choice
  uses `Couldn't switch · showing …` in the existing note. Immediate intent,
  pending treatment, bounded fast polling and warm-cache behavior remain.
- **Radar v4.3 immediate source choice.** Touch contact, click and keyboard show
  a dotted pending choice immediately (caption copy refined in v4.3b above). The displayed
  source retains `aria-pressed`; the group exposes busy state. Intent uses the
  existing loopback GET immediately, coalesces bursts and supersedes a stalled
  poll. Follow-up polling is 300ms until source acknowledgement, capped at 20s.
- **Warm source switches use the cache.** Current-camera opposite-source tiles
  precede optional zoom neighbours. Interrupted rounds and missing disk tiles
  resume, and a complete mosaic tab return no longer skips missing site warming.
  Shared request limits remain in force. Already decoded tiles complete a source
  transition without waiting for a new fetch; repeated payloads preserve pending
  tile work. Older site volumes can publish their first tile across a source
  change without waiting for the whole viewport.
- **Radar v4.2 geography scheduling.** An independent `geo` worker starts home
  pre-warming with the engine, even on Observations and before radar fetches.
  It renders one missing tile per 250ms scheduling quantum, yields between
  background tiles, and idles after both themes/all home zooms are complete.
  Station/revision changes rebuild the queue. Fresh settled camera reports
  prioritize the current viewport; reported motion suppresses generation.
  Radar network passes cannot block it, and PNG/revision writes are atomic.
- **Kiosk benchmark summary first.** Cached paint, pan/pinch timing and draw
  counts, actual CDP network request counts, graphics memory, disk tile counts
  and fences precede details. `--verbose` includes raw frames/fetches/GPU data;
  `--radar-dir` selects the local tile cache for disk counts.
- **Radar v4.1 raster basemaps.** Paper and night now use opaque, versioned
  Natural Earth PNG-8 tiles with 4× antialiased strokes. The vector cell service
  and backing canvas are removed. Missing geography remains visible as a true
  latitude-adaptive graticule; local maps survive a cold provider outage.
- **Bounded graphics and immutable identities.** One 40 MiB admission budget
  covers both tile LRUs, history, canvases and decode reservations. Geography
  has a separate 32 MB/6,000-file disk cap with pinned home tiles. Radar tiles
  include the remap/ramp/basemap revision; the site table is independently
  versioned. Pruning removes empty directories and tile logs no longer grow.
- **Review fixes.** Dateline placement uses unwrapped rectangles; site range and
  acquisition completeness are separate. Historical echoes are composited once.
  AS OF and age both name the newest primary scan. Durable zoom/source caps,
  pointer cancellation, stationary release, abandoned source transitions,
  primary-site timestamp changes, viewport retries and first-view warming now
  preserve their state invariants. Partial histories no longer count as complete.
- **Verification.** Both-theme loopback checks include real captured input,
  cold acquisition, source recovery, fetch/decode instrumentation and independent
  graphics accounting. `tools/benchmark_radar_kiosk.py` prints live-page CDP
  pan/pinch frame times, draw counts, cached first paint and memory as JSON.
  Panel measurements remain the performance acceptance authority.
- **Tab-to-loop uses the warm hour immediately.** Entering Radar posts its view
  marker immediately and polls at 100ms until history arrives (20-second cap).
  The intent watcher schedules one view-start pass; an unchanged warm cache
  publishes history with zero provider HTTP. Off-tab passes retain the current
  geometry's hour of crops while fetching only newest, with the existing grace
  for abandoned generations and unchanged request budgets.
- **Play reflects intent while buffering.** The always-enabled button flips
  immediately between Play and Pause, with inventory feedback and a 120ms dip.
  Playback starts automatically at two decoded frames; pausing during buffering
  prevents a later automatic start. Reduced-motion single sweeps are preserved.
- **Site mode joins the lazy local radar cache.** Site listings run concurrently
  and retain each site's scan slots for 300 seconds on intent passes. Idle mosaic
  viewing warms selected site newest tiles after its loop and adjacent zooms;
  idle site viewing warms the mosaic and neighbouring zooms. Warm mode/zoom
  presses reuse discovery and native tiles with zero newest network requests.
  The eight-frame multi-site identity, 240/minute cap, 34-request reserve,
  60-slot admission headroom and 20-second deep-history view gate are unchanged.
- **Faint returns in site mode, from 5 dBZ.** Single and neighbouring NEXRAD
  sites gain one flat slate band at 5–10 dBZ, with alpha 180/255 multiplied by
  native coverage. The nine rain bands keep their exact pixels; MRMS and the
  global mosaic still start at 10, including the wide-view site fallback. The
  legend re-ticks inside the same box, with a theme-composited swatch, a short
  caption tail and an accessible explanation. Reflectivity only, never a
  precipitation-type inference.
- **The loop controls stay while frames arrive.** Zoom staging no longer hides
  Play, the rail or the read. Play remains enabled during buffering and clear
  content. With no frames the read shows the play/pause inventory and the marker
  is absent; the single corner note owns refresh progress. One frame pins the marker at newest. Every phase keeps the same box
  on paper and night.
- **Loop first, next zoom second, deep history last.** After the newest scan,
  acquire eight loop frames and warm adjacent zooms before spending requests on
  the rest of the hour. Deep history starts after 20 seconds of continuous viewing
  at the current geometry and leaves 60 spare requests above the interaction
  reserve, including archive probes and transport retries. It resumes as capacity
  returns and keeps the warmed newest tiles recent in the bounded cache. Each
  geometry can warm its neighbours once per scan; scheduled new scans follow the
  same order. New intents cancel at tile boundaries; multi-site keeps eight frames.
- **Zoom without asking what is newest again.** Intent passes reuse validated
  source timestamps and per-site scan listings for one source cadence, then go
  straight to tiles. Scheduled refreshes, expiry and failures revalidate; purged
  newest tiles trigger validation in the same pass. Successful archive probes
  are remembered per stamp. Interaction never extends freshness or cadence.
- **Map first, echoes follow.** Zoom and pan publish the new geography before any
  radar request. The map, station, rings and scale settle immediately; the drawn
  scan keeps its own reprojection at stale opacity until matching echoes decode.
  A second press continues from that held scan. The plate stays painted throughout.
- **Stop paying twice for the same tiles.** A bounded native-tile cache keeps valid
  downloads from superseded passes. Concurrent tile fetches share persistent
  IPv4/SNI connections across passes; failures and idle sockets are discarded.
  The shared limiter keeps its history reserve for the next widest mosaic. Worker
  publications emit immediately, intent checks run at 100 ms, and the page polls at
  400 ms until acknowledged with a frame, capped at 20 seconds. The single refresh
  note keeps its 600 ms suppression and honest restarted/failure copy.
- **A dropped keep-alive socket is not an outage.** The provider closes idle
  connections after a few seconds; a reused one that fails before any response
  byte is retried once on a fresh connection, idle reuse is bounded to four
  seconds (or the advertised Keep-Alive window), and a transport hiccup on the
  primary keeps the drawn scan and retries in seconds instead of switching to
  the global mosaic. The request cap rises from 90 to 240 a minute so an hour of
  history fills in about a minute and a half rather than five; the tile cache
  means re-zooms do not spend it again. The attribution in the caption is text,
  never a link: a kiosk has no way back from another site.

- **DNS stays warm when sockets go cold.** Resolved IPv4 addresses now live for
  15 minutes independently of keep-alive expiry. One worker resolves outside the
  pool lock; expired addresses serve tiles immediately while a background refresh
  runs, and a resolver failure keeps the last good answer until the next pass.
- **Warm the next zoom when idle.** A viewed radar pass with 60 spare requests
  above the interaction reserve warms the newest tiles one zoom out and one zoom
  in, once per scan. A new intent stops submissions; valid downloads stay in the
  same bounded cache without becoming frames. Newest fetches use six connections
  for parallel cold renders; history and idle warming stay at four.

## 2026-09-13

### Radar
- **Radar refresh recovery and Pi remapping.** Local budget deferrals keep fresh
  scans idle and retry when capacity returns; history reserves the next newest
  frame. Whole intents publish atomically, survive failed delivery, and stay
  monotonic across reloads. Verified lossless palette swaps replace repeated
  full-tile masks. Crop pixel loss and intensity ambiguity are disclosed; damaged
  cache entries rebuild. Primary recovery, actual-contributor captions, held
  historical frames and live failure timestamps now follow their own identities.
- **Radar v3: one reflectivity scale everywhere.** MRMS, NEXRAD and the global
  mosaic now share nine measured bands and a true dBZ scale, in the same pixels
  on paper and night. Returns below 10 dBZ disappear; stale echoes recede to .66.
  Native colour tables are pinned and remapped once per distinct tile colour,
  preserving alpha. Unrecognised colours are transparent and counted; an incomplete
  remap says so on the caption. The separate precipitation-type ramp is retired;
  RainViewer explicitly reads “reflectivity only.” Old native-colour crops cannot
  enter the new cache namespace. Secondary radar ink now clears 4.5:1 over the scrim.
- **Neighbouring radars fill the plate.** Site mode selects up to four intersecting
  230 km circles nearest the view, fetches only each site's intersecting tiles,
  and stacks the nearest on top. The nearest reporting site to your station owns
  the real scan times; neighbours contribute their latest scan within 15 minutes.
  A dark or failed neighbour does not discard working layers. Multi-site history
  stops at eight frames, with newest still published first. The picker names
  “KATX +2”; hairline coverage arcs and site labels explain where returns end,
  and the caption names a site that is not reporting.
- **Zoom out without losing your site choice.** Below zoom 7 a saved site choice
  shows the mosaic, with a dotted underline on the chosen site and an honest
  “resumes at zoom 7” note. Zooming back restores it; tapping the site while wide
  sets source and zoom 7 together. The shared zoom-out floor is now 4.
- **Fetches follow your hands.** A new pan or pinch continues from the held preview,
  keeping the drawn scan until matching geometry decodes. Every intent carries a
  sequence; a newer intent abandons the old pass at the next tile boundary without
  poisoning its retry cache. Rapid controls share a 120 ms trailing debounce.
  One non-blocking corner line replaces “Updating”: newest/history progress,
  restarted work, or a failed refresh naming the scan still shown. It waits 600 ms
  before appearing, and shares its space with zoom-cap and site-resume notes.
- **Zoom +/− responds instantly.** A stepper press now previews the new level on
  screen at once (the same centre-scale, held-until-the-frame-lands path a pinch
  uses) and posts the intent within a fraction of a second instead of waiting for
  the next poll — a burst of presses still coalesces into a single re-composite at
  the final level. Before, a press showed nothing until the poll and the server
  round-trip (~5 s), which read as broken.
- **Touch: pinch to zoom, drag to pan.** On the Pi's touchscreen (and with a mouse
  or trackpad in a browser) a two-finger pinch scales the whole plate live about your
  fingers and, on release, snaps to the nearest zoom level — the same remembered zoom
  the +/− stepper sets, capped per source with a rubber-band at the limits. A
  one-finger drag moves the map 1:1 with no inertia (every pan is a real
  re-composite around the lifted point, so momentum would be dishonest); on release
  the offset converts to a lat/lon through the exact Mercator projection the emitter
  uses, and the emitter re-centers the crop there — basemap, rings, scale bar and
  echoes all follow, and the scale stays truthful as latitude changes. Your station's
  marker moves to its true position with an accent ring, the meaningless plate-center
  crosshair goes away while panned, and a quiet "Recenter on station" button appears
  (it also auto-recenters after 90 s idle). Pan is deliberately transient — a wall
  display wakes on its own station — while zoom persists. The control clusters never
  start a gesture, the page never scrolls or browser-zooms, the loop freezes on the
  drawn frame during a gesture and resumes when the new frame lands, and
  reduced-motion turns the spring-backs into snaps.
- **Radar top-band cleanup.** The masthead content was overflowing its own 41px
  reservation and landing on the header line below it (the station subtitle and a
  30px clock, whose hidden alert/lightning flags pinned an oversized line box). On
  the radar tab the subtitle is dropped (it's already in the footer and the source
  caption), the clock drops to 22px with a fixed line-height, and the double-rule
  tightens under an alert — so nothing overlaps in either alert state. The word
  "REFLECTIVITY" was printed twice in one band; the legend's duplicate title is
  gone and the unit now sits inline as "dBZ" (mixed case — it's a unit) before the
  ramp. The header and the plate chrome share two clean columns instead of a
  four-step staircase, and the age suffix only appears once a frame is genuinely
  late (80% of the source's stale window) rather than on every routine MRMS scan.
- **Stale no longer false-alarms on a healthy 2-minute feed.** The stale flag was a
  bare 3× cadence (6 min for the mosaic), but the emitter never shows an MRMS frame
  younger than ~5 min (IEM renders on demand and returns 503 for newer minutes), so
  the freshest-possible frame already tripped it — the console read STALE, in alarm
  red, almost constantly. Stale now uses a per-source threshold set above each
  source's inherent latency (MRMS 10 min, single-site 15, global 20); "stale" once
  again means the feed actually stopped, not that the source runs its normal few
  minutes behind. Exposed as `staleSec` so the console stays consistent.
- **Radar v2: the radar is the star of its tab.** The plate grows from a 480px
  square to the whole 956 × 490 body — twice the echo area, all of it horizontal,
  where weather comes from — and the rail is gone: its eight elements become
  four quiet overlays confined to the top and bottom bands (nothing within 150px
  of the station marker), the dBZ legend turns into a horizontal ramp with ticks,
  and RANGE/UPDATED/FRAMES rows are cut (the scale bar states distance; scan time
  and lateness live only in the "As of" line). Overlays sit on a flat, theme-aware
  scrim (no filters — the Pi's GPU can't afford them) that clears WCAG 4.5:1 over the
  worst-case echo colours in both themes; no accent on any chrome.
- **Smooth playback.** Frames are pre-decoded to bitmaps and drawn on a canvas by a
  clock-referenced animation frame — a tick does no fetch, no decode, no image-src
  write and never touches the basemap. About nine frames a second (110 ms) with a
  1.1 s hold on the newest, hard cuts between scans (a crossfade would paint a state
  the radar never observed), one 120 ms dip on the wrap. Buffering is a visible
  state ("Buffering · N of M"); partial history loops what exists and labels its true
  start; reduced-motion gets a static newest frame and a one-sweep play button. To fit
  the wider plate on a Pi the decoded loop holds the last 16 frames (~32 min on the
  2-minute feed), and the caption reports the span it actually shows.
- **Tighter, source-aware default zoom.** Auto now targets 200 km across the plate's
  height (zoom 8 at mid-latitudes — half the ground scale of before, with the same
  horizontal reach), and may go finer where the live source can render it: the US
  mosaic to 9, a single site to 10, the global fallback stays at 7 with the existing
  "closest view" note.
- **Single-site NEXRAD, switchable.** A `MOSAIC | KATX` picker on the plate swaps the
  composite for the nearest radar's own super-resolution base reflectivity (IEM's
  per-scan RIDGE tiles, animated over real scan times, ~5-minute volumes, native
  palette verified against IEM's colour curve). The picker is honest about its
  states — pending, no site in range, site not reporting, hidden entirely on the
  global fallback, a failed switch reverts with a note — the choice persists across
  restarts, and stale is judged per source (three scans, not a fixed clock).
- **The 2-minute feed now actually keeps up.** Root causes fixed, not a deadline
  raised: one TLS connection and one DNS lookup per host per pass instead of a fresh
  handshake per tile (a 9-tile frame in ~1.2 s, down from ~3 s on a good link and far
  worse on the Pi), the not-yet-rendered freshest minutes are skipped and a bad tile
  ends its candidate immediately, a failed pass keeps the last good frame instead of
  flapping to the 10-minute source, a newer scan can never be replaced by an older
  one, and every fallback and source switch is now logged with its cause. The "As
  of" line carries an age suffix only when a frame is late (≥ 2 × cadence).
- **Diagnosed why the 2-minute US feed loses to the fallback on IPv6-broken
  networks.** IEM's host advertises an IPv6 address; where IPv6 is a black hole
  (as on the test Pi — `curl -6` times out, `curl -4` answers in 0.23s), Python's
  urllib has no Happy-Eyeballs and stalls ~10s on the dead address before falling
  back to IPv4, so the IEM adapter starves and the console silently stays on the
  10-minute global source. The cure is to reach IEM over IPv4 (disable IPv6 on the
  appliance). The primary-attempt cap stays tight (25s) — a longer cap only delays
  the fallback and leaves the plate empty longer when a source is unreachable; it
  was never the real limit. (Reverts a 55s bump from earlier the same day that had
  mis-attributed the stall to wifi bandwidth.)

### Console
- **Temperature curve: the forecast now begins where the temperature is.** The
  hourly forecast line used to weld its start onto the sensor reading and then run
  to the model's first future hour — so whenever the sensor and the model disagreed
  (sensor 55°, model 53° in rain) it drew a drop-and-recover the forecast never
  predicted, and printed a phantom low label ("53° · 14:00"). A first fix anchored
  the forecast at the model's own now-value with a vertical seam; on the live panel
  that read as a cliff to the chart floor — the same false story. The forecast now
  starts at the current reading and blends onto the model over the next few hours
  (a standard nowcast bias correction: the sensor is the better guide near-term,
  the model for the rest of the day), with a smoothstep decay so there's no kink at
  the join, and a horizon that ends exactly at the model's first turning point so
  the drawn peak *is* the model's peak. Result on the day that exposed it: 55.0 →
  55.1 → 55.1 → 55.6 → 56.5, no dip, and "57° · 17:00" still agrees with HIGH 57°.
  Extreme labels print only where the drawn and model values round the same, so a
  label can never contradict the HIGH/LOW row. Observed-only rendering (no hourly
  data) is byte-identical to before. Guarded by a headless check that fails on the
  old rendering.
- **No more phantom forecast on a cold boot.** The page ships as the design
  artboard, and until the first data frame arrived it kept showing the artboard's
  sample values — 64.0°, "Rising 4.6° per hour", **Low 50° / High 82°**, "Clear &
  Sunny", a July date, sunrise 05:43 — as if they were real, behind only a small
  STALE mark. On a freshly rebooted Pi (or with the engine down) that read as a
  plausible, wildly wrong forecast. The console now paints its no-data pose
  (dashes everywhere, gauges neutral) before the first poll, using the same
  missing-value rules every panel already follows, so it never shows a number it
  hasn't received. The headless verifier now guards this (a cold load with no
  `wx.json` must show no sample values).

## 2026-09-12

### Console
- **New Radar tab.** A regional radar mosaic from RainViewer, centered on the
  station so it works anywhere on Earth (not just the US). The echoes keep
  RainViewer's true reflectivity colors with a real dBZ scale and snow shown
  distinctly from rain — the one deliberately bordered exception inside the
  four-pigment console — framed as an instrument with a console-drawn basemap
  (range rings, scale bar, station marker), light and dark both first-class.
  Honest states: fetching / no echoes shown / stale; the tab hides where there
  is no location. Zoom adapts to hold a consistent ~256 km view at any latitude
  (clamped to the free tier). The emitter keeps only the latest frame current
  when the tab is unwatched and builds the full history only when it's been
  viewed — an idle radar tab costs one frame's fetch, not thirteen. Radar runs
  off-thread and never affects engine health.
- **Radar animation loop.** While the Radar tab is open the past hour of frames
  plays as a loop (oldest → newest, hold on the latest), so motion reads as "now";
  a Play/Pause control and a relative "−N min → newest" counter sit under the plate.
  The loop touches only the echo layer — never the basemap — pauses off-tab and
  under reduced-motion, and never fetches per frame.
- **Finer cadence where it's available.** For US stations the radar now leads with
  IEM's MRMS reflectivity mosaic (~2-minute frames), falling back to RainViewer's
  10-minute global mosaic elsewhere or when the primary is unavailable — it always
  tries the finest source first. The plate shows both an "As of" scan time and a
  distinct "Updated" refresh time (RadarScope-style), plus the source's frame
  cadence, and the legend switches to match whichever source is live (IEM's native
  reflectivity table or RainViewer's Universal Blue). Source hand-offs stage
  atomically so a switch never shows a half-loaded or mislabeled frame.
- **Persistent radar zoom.** A quiet +/− stepper in the rail with a reset-to-auto,
  so you can pin the radar closer or wider than the latitude-auto default; the level
  is saved per station and survives refreshes and reboots (kept in durable state,
  not tmpfs). The control is honest about each source's real range — RainViewer's
  free tier caps at zoom 7, the US MRMS feed reaches 9 — so it never offers a step
  that does nothing, and a level set closer than the live source reaches shows that
  source's closest view while remembering your intent (a saved zoom 8 shows 7 on
  RainViewer and restores to 8 when the US feed returns). Keyboard-operable (+/−/0),
  both themes, no accent on the chrome. Zoom rides a loopback preference the emitter
  reads, so the plate re-composites server-side at the chosen level (no CSS scaling,
  no blur); scale bar, range rings, and the loop all follow, and a zoom change
  respects the same demand-gate, rate limiter, and cooldowns as every other refresh.
- **Geographic basemap under the echoes.** The radar plate now shows real geography
  — coastline and water, country and state/province borders, and major roads — as
  hairline themed lines beneath the reflectivity, RadarScope-style, so a storm reads
  against the land instead of an abstract grid. It's drawn from bundled Natural Earth
  1:10m vector data (public domain, ~3.6 MB), so it needs no API key and works
  offline, anywhere on Earth — a landlocked station shows borders and roads, a coastal
  one shows the shoreline, mid-ocean stays calm water. The emitter projects and clips
  the geography to the exact station-centered viewport (pixel-registered to the
  echoes) and writes a class-tagged SVG the console colors entirely through its own
  palette tokens, so both light and dark are correct with no accent on the map
  furniture. It's generated once per viewport (never per animation frame), only while
  the tab is watched, cached and pruned like the echo frames, and it falls back to the
  old graticule if anything is missing — radar never breaks. The abstract lat/lon grid
  gives way to the basemap when it's present. Build tooling and provenance live in
  `tools/RADAR_BASEMAP.md`.

### Core (shared with the classic console)
- Sager Weathercaster no longer fails on a clear sky: it keyed on CheckWX's
  parsed `clouds` array, which is omitted for CLR/SKC, so on a clear day the
  forecast errored with "Missing METAR cloud information." It now selects the
  nearest station whose raw METAR carries a sky group (clear codes included).

## 2026-09-08

The Pi 4's DSI panel was replaced with a 1024×600 HDMI **capacitive
touchscreen** (Elecrow 7"), which surfaced a set of touch/layout issues.

### Console
- Navigation tabs (Observations / Moon & Sky / Lightning / Sager) can be
  enabled for a touch kiosk. The tab bar is hidden by default; the launcher
  adds `tabs=1` (env `WFP_TABS=0` opts out).
- When the tab bar is shown, the Observations screen now fits the panel: the
  forecast chart yields its slack height, the barometer panel reflows so the
  value/trend and both charts keep room, and the AQI category/trend wraps to a
  full line instead of truncating ("Moderate to…"). All of it is scoped to the
  tabbed mode, so the default single-dashboard layout is unchanged.

### Kiosk
- WiFi keepalive now probes a stable LAN **peer** (the other Pi) instead of the
  default gateway. The gateway stays reachable while the box is isolated from
  the rest of the LAN, so the old probe never fired during an hour-long dropout.
  Adds a recovery cooldown, run serialization, and a post-recovery check that
  only clears the failure count once the peer actually answers.
- Display/touch setup documented for the new panel: 1024×600 is the console's
  native artboard (fit 1.0), the `vc4-kms-v3d` driver and native-mode rule
  (never force a higher mode a small panel only downscales), and the labwc
  touch mapping — labwc matches the libinput device name exactly, and a rule
  pinned to a connector or device that is later swapped out silently breaks
  touch.

## 2026-09-06

Two independent audits of the fork (an initial one, then an adversarial review
of the fixes) drove this release. The full findings and review are in
`design/almanac/AUDIT-2026-09-06.md`.

### Console
- Freshness is measured at the source. The masthead mark now reads **STALE** when
  nothing new is reaching the screen and **SILENT** when frames arrive but the
  station has stopped reporting (`obsAgeSec`, from the observation's own epoch).
  Polling is single-flight with a 4 s deadline; a hung request flags STALE
  within about 12 s instead of never.
- The hero forecast curve's forward segment follows real hourly forecast
  temperatures, with the high and low labelled at the hours they occur. It no
  longer draws a rise back to a high that already happened.
- Lightning takes the Sun & Sky slot while strikes are active; the rainfall
  panel stays visible in a storm.
- Rain volume slews toward the measured rate (columns count up fast, down slow)
  and rain fades in and out instead of switching.
- Light rain never reads as dry: a 10-minute rolling window bridges the haptic
  sensor's zero minutes between trace readings.
- AQI marker and colour bands share one scale; inHg keeps two decimals; the
  barograph plots samples by their epoch on the producer's 24 h window; long AQI
  text ellipsizes; the lightning tile follows the distance unit.
- Barometer outlook uses a compact vocabulary that fits its row; the band's
  TODAY hi/lo come from the same provider as the hero's LOW/HIGH.

### Data engine
- Non-finite numbers (NaN/inf, including formatted strings) can no longer stop
  the JSON feed.
- Every scheduled timer is registered and cancelled by `stop()`; each provider
  gets one in-flight fetch and one retry chain (a day of outage used to
  accumulate 25 retry chains). Provider results publish as one immutable
  snapshot.
- Cached NWS alerts are re-expired every emit; tomorrow's hint and the AQI
  daily trend are chosen by calendar date, never by array position.
- Payload carries `obsTs`/`obsAgeSec`, `lightningTs`, per-provider fetch ages,
  `fcHourly`, `dayStartTs`, and per-day forecast precipitation (`qpf`).
- `/health` reports `degraded` ("sensor silent") distinct from `stale` ("engine
  stalled"), and counts frames the kiosk confirmed painting.

### Core (shared with the classic console)
- WeatherFlow's websocket delivers every `obs_st` twice; the duplicate guard
  compared a list to a number and never fired, so per-minute integrators (peak
  sun, strike counts) double-counted. Fixed.
- Tempest daily-bucket rain columns were swapped: month and year were seeded
  from the rain-check corrected figure while today/yesterday used the raw
  sensor. With `nc_rain` off the year total now matches the device (34.5 in,
  not 42.5 in).
- REST seeds (daily wind average, gust max, yesterday's rain) retry every five
  minutes while missing instead of only once at boot; an echoed message no
  longer wipes the REST cache.
- Yearly rain rollover no longer doubles its baseline for one observation;
  lightning frequency averages over real coverage; sunrise/sunset use station
  midnight in explicit UTC, with polar days handled; statistics rows are
  selected by date and must carry a finite number; short websocket rows are
  shape-checked.

### Kiosk
- Launcher waits for the declared display backend (a Wayland session never
  falls back to X11), bounds every health probe, reads the 503 stale body so a
  wedged engine restarts the engine rather than the server, exits cleanly on
  TERM with children reaped, and restarts the engine once per silent-sensor
  episode.
- Pi 4: `wifi-keepalive` timer re-associates when the gateway stops answering;
  persistent journal so the next drop leaves a log. See `kiosk/PI4-SETUP.md`.

## 2026-09-01

- Wind-driven rain and blowing snow glyphs; tomorrow hint under the headline;
  MAX gust row on the wind panel; forecast-day rain amounts (from drench44).
- Rolling rain window; rain fade; rain volume slew.
- Barograph in the station's pressure unit; outlook vocabulary compacted.

## 2026-08-29 to 2026-08-31

- 7-day outlook band on one shared temperature scale, condition glyphs, snow-aware
  status, alerts and outlook coexisting, etched two-depth rain with a waving
  water surface, intensity-scaled rain gauge.
- Forecast fetch retries until first success; fit-and-finish audit fixes.

## 2026-08-03 to 2026-08-10

- NWS weather alerts and AQI (WAQI) with severity forecast; alert rotation.
- Systemd-supervised kiosk with stale-data recovery; headless mode; LAN view;
  X11/Wayland auto-detect; Pi 4 setup guide; public-station picker for
  hardware-less setup; offline test suite and CI.
