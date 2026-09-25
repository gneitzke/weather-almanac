# Native-resolution radar: three designs compared (2026-09-25)

The owner, comparing the Radar tab with a phone radar app: "Ours feels super low res but this app is so
much sharper", then "Can we still mosaic if we fetch the raw?" and "When I zoom into essentially a single
radar can we auto upgrade, and auto downgrade on zoom out?". Three designers (Astra / gpt-6-astra, Opus
5.5, Fable) answered one brief independently; the orchestrator verified the facts they disagreed on.

## Verified facts (orchestrator, 2026-09-25 ~03:45 UTC, rain at the coast)
- IEM's `ridge::{site}-N0B` tiles carry ~1 km squares at z8, z9 and z10 alike: the resolution ceiling today.
- NOAA's `unidata-nexrad-level3` S3 bucket carries N0B for ATX, LGX, RTX and OTX every ~6 min.
- N0B size depends on the weather: 106-128 KB in clear air (Fable, Opus), 251-333 KB with rain
  (orchestrator: ATX 258, LGX 327, RTX 276, OTX 139 KB median).
- Delivery 40-83 s after the scan time; bzip2 payload decompresses to exactly 1,329,150 B (720 x 1840 gates).
- N0H (hydrometeor classification) is in the same bucket, 20-30 KB, ~70 s latency (Opus).
- Level II volumes are 5-11 MB: rejected by all three.

## Where all three agree
Level III N0B from the Unidata S3 bucket; a small numpy decoder, no MetPy/Py-ART; render in the engine
into the existing 256-px palette-PNG tiles, cache and manifest (no page-side polar drawing); nearest gate
with no smoothing at z9-10, max-supersampling at z8; a per-pixel mosaic by lowest beam; MRMS for wide
zooms and native for close zooms, switched on settle with the old image kept until the new one is ready;
Pi 3 stays on IEM tiles; a prototype that proves sharpness on the panel before the full build.

## Where they differ

| | Astra | Opus 5.5 | Fable |
|---|---|---|---|
| Clutter control | flag, don't erase; N0H later; DEM blockage tables | **N0H class mask** (clutter/AP fall through, biological clears) + speckle + archive-built static masks | speckle + clutter map learned on the panel + VCP floor; N0C later |
| Below-threshold gate from the preferred radar | a measurement: blocks far radars | a measurement: blocks far radars | falls through to a beam under 3 km |
| Switch rule | z>=8 native, z<=7 Region | up at z>=8, down at z<=6, **z7 keeps what is showing**; 85 % coverage guard; 10 s guard | up at 8.0, down at 6.7 |
| When native is fetched | live (attended), budgeted | **live and warm only**, contributing sites only | live/warm/watch by day, beam-gated |
| Bandwidth estimate | 28-884 MB/day (sizes unverified) | ~5-8 MB/h while open, ~30 MB on a rainy day, 0 when not viewing | 53-100 MB/day naive, gated lower |
| Code shape | adapter in the engine | three fork-only modules (`radar_level3`, `radar_native`, `radar_auto`) | new module + source entry |
| Safety valves | byte admission + 500 MB/day ceiling | slow-render kill switch, Pi 4 calibration gate | daily byte ceiling |
| Evidence | docs only (listing failed in its sandbox) | decoded real N0B + N0H, timed decode/render | decoded a real N0B, timed decode/render |

## Decision: Opus 5.5's plan, with three grafts
Opus's plan is the most complete and the most frugal, and it is the only one whose clutter control is
principled on day one: the radar's own dual-pol classification (N0H) says which gates are ground clutter
and which are birds and insects, at 20-30 KB a scan. It fetches native data only while someone is looking
at a zoomed-in radar, keeps new logic out of the 4,900-line engine in three small modules, and has the
cleanest hysteresis (z7 keeps whatever is showing). Fable's fall-through on below-threshold gates would
reintroduce the far-radar bleed the mosaic exists to fix, and a clutter map learned on the panel takes
days to converge; Astra's plan is sound but carried unverified sizes and defers clutter control.

Grafted in:
1. From Astra and Fable: a **daily native byte ceiling** that degrades to newest-only, then to IEM tiles,
   on top of Opus's tier gating (a stuck-open Radar tab on a rainy day must stay bounded).
2. From Astra: **immutable frame identity** discipline (a published mosaic key never changes meaning) and
   the rule never to mask native echoes with MRMS.
3. From Fable: read the **VCP from the product** to replace today's inferred scan cadence, and its real
   decode fixture and timing as the first golden test.

Verify before building (phase 0): the N0H class codes (Opus quoted them from memory) and the meaning of
N0B code 1 (Astra: missing; Opus and Fable: range-folded), both against the NOAA ICD.

## Phases
0. Prototype: KATX only, newest frame, floor + speckle, manual "KATX native" entry, z8-11. Gate: the
   owner judges it sharp next to the phone app, and cached tiles take <= 40 ms on the Pi 4.
1. Hardened decoder, N0H QC, 8-frame history, one site.
2. Per-pixel mosaic across contributing radars.
3. Automatic switching, picker (Auto / Region / Radar), captions, attention tiers, byte ceiling.
4. Archive-built blockage masks, Pi 3 gating, contract and changelog, one-week bandwidth check.

---

# Design A: Astra (gpt-6-astra)

## 1. Summary

Use native polar reflectivity data to generate one per-pixel radar mosaic in the engine, served through the existing 256-pixel tile system. Prefer NOAA’s binary Level III N0B product, which preserves super-resolution sampling without downloading complete Level II volumes. Select each pixel’s radar using coverage, blockage, freshness and beam height before applying the 15 dBZ display floor. Auto mode uses MRMS at settled zooms ≤7 and native data at ≥8, retaining the displayed source until its replacement is ready. Prove sharpness and resource consumption with a KATX prototype before adding terrain-aware mosaicking and automatic switching.

Repository inspected on `feature/almanac-ux`; no files changed and no LAN hosts contacted.

## 2. Data source

**Recommended product:** NEXRAD Level III **153, N0B**, nominally 0.5° elevation, 250 m range × 0.5° azimuth. This is a processed radial reflectivity product, not raw Level II, but it retains the requested spatial sampling before IEM’s Cartesian re-gridding. NOAA’s product catalogue verifies that resolution. [NOAA product definitions](https://www.weather.gov/source/datamgmt/xr05_X_ref_by_TT.html)

**Primary endpoint:**

`https://unidata-nexrad-level3.s3.amazonaws.com/`

Discover through S3 ListObjectsV2; proposed site/product prefix is `ATX_N0B_`, with equivalent RTX/LGX/OTX prefixes. Fetch the exact returned object key, never manufacture scan filenames. Use bounded incremental listings, pagination and UTC rollover handling.

**Verification boundary:** AWS verifies the public bucket and describes its contents as *select* real-time Level III data. Object listings failed through the available tools; those prefixes, four-site availability, retention and actual compressed sizes remain prototype acceptance checks. [AWS registry](https://registry.opendata.aws/noaa-nexrad/)

**Format and size:** NIDS binary, big-endian headers, radial data and optional bzip2 compression. NOAA specifies a maximum uncompressed product-data size of **1,329,150 bytes**. Budget **0.10–0.80 MB compressed per sweep**, nominally **0.25 MB**; these compressed figures are estimates, not measurements. [NOAA ICD, product 153](https://www.roc.noaa.gov/public-documents/icds/2620001AD.pdf)

**Timing:** assume ordinary 5–7-minute sweeps and 30–120 seconds delivery delay after sweep completion; measure both. Supplemental scans can arrive faster. Use embedded observation time, not object modification time.

**Level II alternative:** if N0B fails the sharpness comparison, investigate lowest-sweep extraction from:

`https://unidata-nexrad-level2-chunks.s3.amazonaws.com/{site}/{volume}/{YYYYMMDD-HHMMSS-chunk-type}`

Full-volume reference data use:

`https://unidata-nexrad-level2.s3.amazonaws.com/{YYYY}/{MM}/{DD}/{site}/{object}`

The current buckets and chunk structure are documented; Level II is a separately budgeted option, not an automatic expensive fallback. [AWS registry](https://registry.opendata.aws/noaa-nexrad/), [chunk documentation](https://github.com/awslabs/open-data-docs/tree/main/docs/noaa/noaa-nexrad)

Keep existing MRMS metadata and tiles:

`https://mesonet.agron.iastate.edu/data/gis/images/4326/mrms/lcref.json`

`https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/mrms::lcref-{stamp}/{z}/{x}/{y}.png`

Their use is verified in the repository; cadence and measured resolution come from the brief. Existing IEM site tiles remain another explicitly labelled fallback.

## 3. Decoding

Implement a narrowly scoped product-153 decoder using `struct`, `bz2`, optional transport `zlib`, and NumPy. Validate against pinned MetPy-generated golden outputs prepared outside CI; do not install MetPy/Py-ART on the panels. MetPy confirms support for product 153 and digital radial packet 16. [MetPy decoder](https://raw.githubusercontent.com/Unidata/MetPy/main/src/metpy/io/nexrad.py)

Extract site coordinates/altitude, elevation, VCP, sweep timing including supplemental-scan offsets, radial boundaries, first gate, gate spacing and reflectivity scaling.

Validate product/site identity, lengths, offsets, compression completion, dimensions, timestamps, angular coverage and physical bounds. Reject truncation and unsupported variants; cap input and decompressed bytes before allocation.

Keep numeric values and validity separate. Crucially, product 153 defines **code 0 as below threshold and code 1 as missing**; do not collapse both into NaN or transparency. [NOAA ICD, Figure 3-6 note 1](https://www.roc.noaa.gov/public-documents/icds/2620001AD.pdf)

## 4. Rendering

Run decoding and rendering in one bounded background worker within the engine’s radar acquisition lane.

For each output pixel:

1. Invert Web Mercator to latitude/longitude.
2. Compute radar-relative geodesic azimuth and ground distance; convert to slant range consistently with the beam model.
3. Find the containing radial and gate using actual boundaries.
4. Select the radar, then apply the existing display palette and floor.

Use vectorized NumPy operations and cached geometry tables. Start with **nearest-gate sampling, no interpolation or smoothing**; this preserves sharp boundaries, accepting some aliasing at z8. Each zoom renders directly from polar data.

**Estimated Pi 4 cost:** 20–80 ms per 256² tile with four candidates and cached geometry; 0.3–1.2 seconds for fifteen tiles, excluding decoding. These are targets to benchmark with clear-air and wet fixtures, cold/warm geometry, and Chromium running—not measured results.

Add a `native-n0b` adapter to `_RADAR_SOURCES` and `_do_radar`. Reuse `_radar_history` scheduling, cancellation, deadlines and inventory publication, while factoring `_radar_fill_frame`’s validated tile commit from its upstream-PNG fetch/remap path.

Native tiles need numeric-render metadata rather than pretending RGB inversion occurred. Preserve `weather_pixels` semantics at ≥25 dBZ.

Publish **one tile per mosaic frame/location**. Add immutable `frameId` derived from exact input scan identities, QC configuration and selection policy; retain `ts` separately. Extend path validation, inventory keys and page URL construction accordingly. Store contributors separately from `siteScans`, whose current meaning triggers page stacking.

Include decoder, geometry, palette, blockage and QC versions in `_radar_render_revision`. Acquisition completeness and meteorological coverage must remain separate.

## 5. Mosaic

First identify radars capable of contributing within the viewport; fetch those plus useful fallback candidates, maximum four. At Duvall, do not fetch Portland or Spokane merely to preserve “+3.”

Use the conventional effective-Earth approximation:

\[
h \approx h_{\rm radar}+\sqrt{r^2+(kR)^2+2rkR\sin\theta}-kR,\quad k=4/3
\]

Here \(r\) is slant range and \(\theta\) is the product’s actual elevation. Subtract terrain elevation for height above ground. This is an atmospheric approximation, especially during AP. [Beam geometry reference](https://docs.wradlib.org/en/latest/generated/wradlib.georef.misc.bin_altitude.html)

**Proposed selection policy:**

- Require coverage within both the product range and a conservative **230 km operational limit**.
- Exclude missing gates and severe blockage.
- Prefer scans no more than **6 minutes** behind the frame time; use 6–10-minute candidates only where fresher coverage is unavailable.
- Rank acceptable candidates by blockage class, then lowest beam height, shortest range and stable site ID.
- Apply display suppression only after selection.

Generate versioned cumulative blockage tables off-device from a documented DEM and antenna geometry. Initial thresholds: <20% preferred, 20–50% degraded, >50% excluded; validate regionally. Blockage must accumulate along the beam, not merely test terrain at the target pixel. [Blockage reference](https://docs.wradlib.org/en/latest/generated/wradlib.qual.cum_beam_block_frac.html)

A valid below-floor winner stays transparent **without exposing another radar**. Missing, stale, blocked or out-of-range winners permit fallback. Uncovered pixels remain unknown, never “clear.”

Anchor loop frames to the dominant radar’s actual sweep times; choose other scans at or before each anchor. Freeze the input tuple once published, expose contributor age ranges and never interpolate weather through time. This replaces viewport-wide painter order with deterministic pixel ownership.

## 6. Quality control

Native sharpness does not provide MRMS quality control.

Keep the 15 dBZ floor, but describe it as display suppression: it can hide weak precipitation and cannot reliably remove strong clutter. Preserve missing-data semantics, apply validated blockage/static clutter masks, and record suspect coverage separately.

The initial release should **flag uncertain echoes rather than aggressively erase them**. Small isolated-gate filters are cheap but require precipitation-retention tests before enabling.

A later optional filter can consume aligned hydrometeor classification—product 165/N0H—to identify biological and AP/ground-clutter classes. Its availability, additional bandwidth and coarser sampling require separate validation. NOAA documents those classes. [NOAA classification definitions](https://www.roc.noaa.gov/public-documents/icds/2620001AD.pdf)

Do not hard-mask native echoes using MRMS: its different resolution and timing can erase developing showers. Do not claim reliable second-trip removal from reflectivity alone.

## 7. Automatic zoom switching

Picker: **Auto / Region / Native**. Keep requested preference separate from effective source.

- Auto enters native at settled **z≥8**, returns to Region at **z≤7**, and retains its source between thresholds.
- Evaluate after gesture/inertia settlement plus 500 ms stability, never mid-pinch.
- Persist manual preference; Native retains the existing z7 floor and temporarily uses Region below it.
- Give native a z10 source maximum; preserve Region’s existing limit and camera position.

In attended `live`, use spare budget near the boundary to prepare the target’s newest frame and up to four history frames. Prefetch never outranks the current source’s newest frame. Unattended live disables it.

Reuse the existing staged-source mechanism: keep outgoing pixels, legend and caption until four target frames decode—or all genuinely available frames for a shorter loop. Switch at a loop boundary. The 20-second switch deadline reports delay without blanking the map.

Captions describe **displayed** data, for example:

- “Auto · Region · quality controlled”
- “Auto · Native · KATX +1 · limited filtering”
- “Region · native temporarily unavailable”

Show actual contributing sites, observation age and mixed scan-time range in details. Explicitly revise the existing caption contract.

Keep eight real frames: MRMS normally spans ~14 minutes; native typically ~35–49 minutes. Preserve the 350 ms playback interval but show actual span/cadence; never pad native frames to imitate two-minute updates.

## 8. Budgets

All figures below are planning estimates.

**CPU:** one renderer, two concurrent raw downloads, yielding between tiles. Target <5 percentage points additional CPU averaged across all four cores while viewing, <2 seconds warm newest-frame rendering, and unchanged page paint targets.

**Engine memory:** ≤64 MiB incremental: approximately 6 MiB current gate arrays, 24 MiB geometry LRU, bounded decode/tile scratch and small metadata. Retain history compressed on disk; decode older scans on demand.

**Page:** unchanged **40 MiB** graphics admission cap. Eight 956×490 plates occupy 14.3 MiB; four incoming plates add 7.1 MiB. Existing canvases, tiles, basemaps and reservations share the remainder through eviction. Use indexed PNG output to stay below the existing 128 KiB response limit.

**Bandwidth:** decimal MB/day, assuming 0.25 MB/sweep, six-minute cadence and 15% retry overhead:

| Tier | Policy | Estimated MB/day |
|---|---|---:|
| Live, native | 1–4 useful sites; rolling eight frames | 70–280, plus ≤10 metadata/prefetch |
| Live, Region | Existing MRMS acquisition | 20–100 |
| Warm | Four-frame MRMS preparation; retain native cache | 20–100 |
| Watch | MRMS eight frames/day, newest/night | 20–100 |
| Rest | Existing four-tile sentinel and sparse discovery | 0.5–2 |
| Dormant | Hourly metadata only | <0.1 |

These are full-day tier equivalents, not summed daily totals. Native compressed-size uncertainty expands its range to roughly **28–884 MB/day**. Eight-frame, four-site cold backfill costs approximately **9.2 MB** nominally. The supplied 22–35 MB/day idle measurement remains the baseline to beat.

Keep `RADAR_REQUESTS_PER_MIN=240`, adding byte admission: proposed 2 MB/min replenishment, 12 MB burst and 500 MB/day native ceiling. Charge failed/hedged bytes; exceeding the ceiling selects labelled Region fallback.

Keep rendered tiles inside the existing **64–256 MB, 8,000–12,000-file** cache limits. Add a separately bounded 64 MB raw cache, subject to shared disk-pressure checks. Validate raw objects lazily; do not extend the already costly PNG boot scan.

## 9. Failure modes and fallbacks

- **Wi-Fi failure:** reuse local-failure backoff; do not mistake it for multiple provider outages. Avoid speculative duplicate raw downloads unless progress stalls.
- **Decode error:** quarantine that object, retain previous valid scans, retry another object; never publish partial decoding as clear coverage.
- **One radar down:** reselect affected pixels from acceptable candidates; publish actual contributors and gaps.
- **Native unavailable:** fresh MRMS, then existing IEM site/RainViewer fallback as appropriate; visibly disclose source and reduced detail.
- **All feeds stale:** retain the last image with its true age, without advancing observation time.
- **Pi 3:** native disabled by default; remain on IEM. Memory pressure or throttling on Pi 4 first cancels history/prefetch, then falls back.

## 10. Test and verification plan

**Phase 1—sharpness prototype:** obtain a wet KATX N0B sweep and matching Level II reference off-device. Verify endpoint keys, actual gate spacing, compressed sizes and delivery latency. Render z8–10 Seattle/Duvall tiles using the existing palette. Subsequently test on the panel during an explicitly authorized verification session; no panel access is part of this design work.

**Phase 2—single-site integration:** add bounded decoding, tile publication, cache identities and manual Native mode. Measure CPU, RSS, cold-start latency and bandwidth.

**Phase 3—mosaic:** add terrain tables and pixel selection; verify seams, high-beam fallback and asynchronous scans.

**Phase 4—Auto:** add switching, prefetch and tier budgets behind a feature flag.

Offline CI fixtures cover real clear/wet sweeps, supplemental scans, malformed compression, missing radials, midnight rollover and numeric scaling. Synthetic tests must prove that below-floor winners block distant echoes, missing winners permit fallback, tile boundaries agree and published identities never mutate.

Extend `test_radar_buffer_page.py` for hysteresis, stale generations, staged-source cancellation, truthful captions and graphics ownership. Replay deterministic 10% failures plus correlated outages. Add NumPy to development dependencies; CI remains network-free.

Later CDP checks measure z9/z10 sharpness, 30 fps interaction, paint timing, ≤40 MiB ownership, switch continuity and a 24-hour resource soak.

## 11. Three biggest risks and what I would not do

1. **Unverified delivery assumptions:** N0B availability, latency and size must pass the prototype before committing to this feed.
2. **Sharp but misleading echoes:** terrain, AP and asynchronous sampling demand explicit uncertainty; this will not equal MRMS QC.
3. **Acquisition complexity:** immutable frame identities, source transitions and byte budgets must preserve existing recovery behavior.

I would not download four complete Level II volumes continuously, implement full meteorological QC, blend reflectivity across radars, introduce browser polar rendering, or change `service/*.py`, `observation_parser.py`, `config.py`, or `main.py`’s `CurrentConditions`.

---

# Native-resolution radar: design (designer 3)

## 1. Summary

The engine fetches NWS Level III super-resolution reflectivity (N0B, 0.5° × 250 m, lowest tilt) and hydrometeor classification (N0H) per radar from the public `unidata-nexrad-level3` S3 bucket, about 260 KB per radar per scan. A small numpy decoder in a new fork-only module turns them into polar arrays. The engine renders these straight into the page's existing 256-px palette PNG tiles, cache paths and manifest. Each pixel takes the lowest-beam radar with a valid, fresh, non-clutter gate, so one source (`native-l3`) replaces the painter's stack. The source is chosen when the view settles: MRMS at z≤6, native at z≥8, and z7 keeps whatever is showing. An Auto/Region/Radar picker and an honest caption sit on top, and fetching runs only in the live and warm tiers.

## 2. Data source

**Primary: `unidata-nexrad-level3`** (AWS Open Data, anonymous, us-east-1).
- Keys look like `ATX_N0B_2026_09_25_03_36_14`: site without the K, product, volume start in UTC.
- Discovery: ListObjectsV2 by hour prefix (`?list-type=2&prefix=ATX_N0B_2026_09_25_03`), then GET.

**Verified on the live bucket (2026-09-25 UTC):**
- Anonymous list and GET work. One hour's listing is about 2.1 KB.
- N0B sizes: KATX 106 KB (clear air), 236-311 KB (echo); KRTX 187 KB; KOTX 157 KB; KLGX 333 KB (coastal clutter).
- N0H sizes: 20-30 KB.
- Latency from volume start to LastModified: N0B 40-80 s (19:47:11 → 19:47:51; 03:36:14 → 03:36:54). N0H about 70 s. HHC about 6 min, so I don't use it.
- Cadence: KATX 6:09, KRTX 4.5 min, KLGX/KOTX 6-6.5 min.
- Format, from my own decode: a 30-byte WMO header, an 18-byte message header (code 153) and a 102-byte description block (site 48.195/-122.496, tilt 0.5°, thresholds −32 dBZ / 0.5 / 254 levels, compression flag 1). Then a bzip2 symbology block with packet 16. N0B is 720 radials × 1840 bins (0.5° on half-degree starts, 250 m, 460 km); N0H is 360 × 1200 (about 1°, 250 m, 300 km).
- N0H codes observed: 0, 10, 20, 30, 40, 50, 60, 80, 90, 100, 140.
- Level II KATX volumes: 10.1-10.9 MB (rejected).
- tgftp: `DS.165h0/SI.katx/sn.last` is current (30 KB). `DS.00n0b` serves only KDGX, KEPZ, KGWX, KICX and KMTX, so **it has no KATX N0B**. `DS.p94r0` (N0Q) is 1 km, no sharper than IEM.

**Assumed:** the bucket and key scheme stay stable; HCA code meanings follow the ICD (10 BI, 20 GC/AP, 150 RF), from memory, not re-read.

**Fallbacks:** native-l3 → today's IEM ridge N0B tiles → MRMS lcref → RainViewer. tgftp N0H is the secondary source for QC only.

## 3. Decoding

**Our own decoder:** a new `lib/radar_level3.py` of about 150 lines, using `bz2`, `struct` and numpy. No MetPy or Py-ART.

**Speed.** 11 ms per real N0B on a Mac, about 90 ms on a Pi 4. The ×8 factor comes from the panel's measured 5 ms tile validation, which takes 0.65 ms here.

**Validation** (any failure rejects the scan): WMO header; product code 153/165 matching the key; site within 0.05° of `_NEXRAD_SITES`; volume time within 2 min of the key; N0B thresholds exactly (−320, 5, 254); a clean bzip2 end and body length equal to the listed Size; packet 16 with 1840/1200 bins, 700-740 radials and at least 350° of azimuth.

**Output.** `Scan(site, product, ts, elev, site_height, rows[720,1840] uint8)`:
- Radials go into fixed half-degree rows by `floor(az·2)`. If starts sit off the grid, a `searchsorted` fallback places them.
- Codes: 0 is below threshold (a *measurement* of no echo), 1 is range-folded, and n≥2 is (n−2)/2 − 32 dBZ.
- Raw files are cached on disk under `RADAR_DIR/raw/`; decoded scans in a 6-entry LRU.

## 4. Rendering

**Runs in the engine**, on the existing tile threads. It emits the same palette PNGs with the same `radarRemap` metadata, so `_radar_tile_metadata`, `weather_pixels`, pruning and boot validation work unchanged. No page-side polar rendering: the Pi GPU canvas already failed the vector map.

**Per tile (z/x/y) and frame:**
1. **Static geometry**, cached per (site, z, x, y, sub):
   - Pixel-centre lat/lon, then haversine ground distance *s* and bearing.
   - Slant range from a per-site 1-D *r(s)* table (4/3-earth at the product's tilt, `np.interp`).
   - Store row, bin (uint16) and beam height (uint8, 50 m steps). KATX's beam over Duvall is about 1.1 km; KLGX's is 3.3 km at 170 km.
2. **Mosaic selection** (§5) gives one code per sample.
3. **Sampling.**
   - At z≥9: nearest gate, **no anti-aliasing and no interpolation**. Gates render as honest 2-6 px trapezoids at z10.
   - At z7-8, where a pixel is bigger than a gate: supersample 2-4× and keep the **max** code, so small cores survive.
4. **Colour.** A 256-entry LUT built from `_RADAR_DISPLAY_LUT` (15 dBZ floor, same ramp as every other source), written as a palette PNG with tRNS.

**Per-tile cost on a Pi 4** (Mac prototype × 8):

| Step | Mac | Pi 4 |
|---|---|---|
| Geometry, z9-11 | 1.4 ms | ~11 ms (cached after first use) |
| Geometry, z8 at 2× | 6.4 ms | ~50 ms |
| Gather/select | 0.3-2 ms | 3-16 ms |
| PNG encode | 0.8 ms | ~6 ms |

A tile is 15-25 ms warm, 40-70 ms cold; a 24-tile frame is about 0.5 s CPU (0.3 s wall on two threads).

**Fit with the existing model:**
- New source `_RADAR_SOURCES['native-l3']` (max_zoom 11, cadence = primary site's scan cadence).
- Each frame gets a `mosaicKey = 'm'+sha1(pairs, NATIVE_REVISION, QC state)[:10]`, stored in the tile path's `site` slot. Every scan combination is then its own immutable tile set.
- Frames keep `siteScans` (caption, aria) and gain `layers:[{id:mosaicKey, ts}]`. `radarFetchTile` iterates `layers` instead of `siteScans`: one fetch per tile, not four. That is the only page change here.
- `_radar_render_revision` folds in `NATIVE_REVISION`.
- `_radar_tile_batch`'s per-tile `fetch` is generalised into a `produce` callable. Deadlines, supersession, cache admission, partial publication and manifest masks are all reused.
- A new adapter `_radar_native_frames` mirrors `_radar_site_frames` (discover → newest → `_radar_history`).

## 5. Mosaic

**Candidates.** Radars whose 230 km disc (`RADAR_SITE_RANGE_METERS`) covers the pixel, ordered by beam-centre height. The order is static per tile and packed into a uint8 per pixel (up to 3 candidates).

**Selection.** Take the first candidate for which all of these hold:
- Its scan exists and passes the time rule.
- The gate isn't in the site's static blockage mask (phase 4).
- The gate isn't range-folded.
- Its N0H class isn't GC/AP or RF.

Then:
- That gate is the answer, **including "below threshold"**. So a distant high beam no longer shows through where the lowest beam says "nothing".
- A biological (BI) gate becomes clear. It doesn't fall through, because a higher beam sees the same bugs.
- If no candidate qualifies, the pixel is transparent.

**Time.** Frames are clocked by the *primary* radar, the one with the lowest beam over the station (KATX). Each other site contributes its newest scan with ts ≤ frame + 60 s and age ≤ 8 min. Stale sites drop out and their pixels fall through. There is no advection, so a 50 km/h line can show a seam offset of up to about 5 km where selection changes radar; the caption lists every contributing scan.

**Versus today.** Today ranks per view and treats transparent as "look through". Here ranking is per pixel by beam height, "no echo" is a measurement that wins, and only missing or non-meteorological data falls through. The page draws one layer.

## 6. Quality control

Raw Level III carries the RDA's clutter filter, but none of MRMS's QC. These cheap steps, in order of value:

1. **N0H mask** (28 KB/scan, same volume key). GC/AP falls through to the next radar, BI becomes clear, RF becomes no data. Dual-pol HCA already folds in CC, ZDR and texture, so this is the biggest single win. The HCA row is the N0B row // 2; there is no class beyond 300 km.
2. **The 15 dBZ display floor**, as today.
3. **Polar speckle filter.** An echo gate with fewer than 3 of its 8 neighbours at or above the floor becomes clear. It runs once per scan: 8 ms on the Mac, about 65 ms on the Pi.
4. **Static clutter/blockage masks** (phase 4). A dev script builds them offline from about two weeks of public N0H/N0B archive: GC in more than 50 % of volumes, or persistent echo on dry days. They are committed as 1-bit 720×920 PNGs of a few KB per site.
5. **Missing N0H:** render with steps 2-3 only and caption "KATX unfiltered".

Not attempted: blockage power correction, bright-band correction, velocity-based AP detection. MRMS does those, so wide zooms stay on MRMS.

## 7. Automatic zoom switching

**Rule.** The engine keeps ownership of the source, via a pure `radar_auto.choose(settled_zoom, showing, coverage, health, pref)` in a fork-only module:
- **Up** at settled z≥8.
- **Down** at z≤6.
- **z7 is the hysteresis band**: keep what is showing. A cold start at z7 uses MRMS.
- **Coverage guard:** stay on MRMS if native coverage of the viewport is under 85 %.
- **Guard time:** no reverse auto-switch for 10 s unless the zoom moves 2 or more levels.

**Settle, not pinch.** The page already scales tiles mid-gesture and posts intent on settle. The old source stays on screen until the new source's newest frame is publishable, using the existing staging path.

**Prefetch.**
- In live at z7 on MRMS, the newest native frame for the z8 grid is rendered ahead of time. The primary's newest scan is already local.
- On native, the z−1 manifest level and MRMS z7 newest tiles are kept warm.

**Picker: Auto (default) | Region | Radar.** "KATX+3" becomes "Radar", now meaning the native mosaic.
- A manual choice holds until the user taps Auto or 45 min pass without touch.
- Manual Radar keeps today's z≥7 floor.

**Caption examples:** `Auto · KATX + LGX · 250 m native · filtered · scan every 6 min`, `Auto · Region · NOAA MRMS 1 km · every 2 min`, and exceptions such as `LGX 9 min old`.

**Loop.** MRMS frames come every 2 min (8 frames ≈ 14 min). Native follows the primary's volumes (8 frames ≈ 45-50 min). Dwell per frame is unchanged; the caption shows the span, and the loop restarts at the newest frame on a switch.

## 8. Budgets

**CPU (Pi 4, estimated).**
- About 170 ms per site-scan (decode, N0H and QC).
- Steady live state: about 1.5 s of CPU per 6-min volume (2-3 scans; 24 tiles at z and 24 at z±1), about 0.4 % of one core.
- Cold 8-frame loop: about 5 s of CPU on two threads, leaving two cores for Chromium.

**Memory.**
- Engine: 8 MB of decoded scans, a 32 MB geometry LRU and about 4 MB of raw files.
- Page: unchanged or lower (one bitmap per tile instead of merging up to four). The 40 MiB cap and admission logic stay untouched, and tile count doesn't depend on zoom.

**Bandwidth.** A site-scan (N0B + N0H) is about 260 KB typical and 360 KB peak. At about 10 volumes/h that is about 2.6 MB/h per site. Listings are polled at the predicted volume time + 45 s, then every 20 s.

| Tier | Native fetching | Extra |
|---|---|---|
| live | contributing sites (KATX + KLGX at Duvall, sometimes KRTX); 8-frame backfill once, then newest | ~5-8 MB/h open, plus ~4-6 MB per cold session |
| warm | primary newest only, if the last settled view was native | ~2 MB per 45-min hold |
| watch / rest / dormant | none (MRMS and sentinel as today) | 0 |

A rainy day with three 10-min sessions and five warm holds is about 30 MB/day; a day with no viewing adds 0. Dropping IEM ridge tiles at z≥8 offsets part of it.

**Cache.** Raw files are about 8 MB per hour for 3 sites, pruned by age. Tiles (3-25 KB palette PNGs): about 5-10 MB per 8-frame viewport at three levels. The existing free-space caps (64-256 MB, 12,000 files) are enough.

## 9. Failure modes and fallbacks

- **S3 down or 5xx.** The `radar_http` host breaker opens and the page shows IEM ridge tiles with the caption "Native radar unavailable · showing KATX tiles".
- **Wi-Fi failures (about 10 %).** Whole-object retry of about 250 KB with existing hedging and deadlines; a short body fails the length check before decode.
- **Decode or validation error.** The scan is rejected and negative-cached for 10 min. The site leaves that frame and its pixels fall through. Logged through `_radar_log_failure(scope=site)`.
- **One radar down.** Its area falls to the next beam, or to no data; caption "KLGX not reporting".
- **N0H late.** Render unfiltered with a caption. Re-render if N0H lands within 3 min; the new QC state gives a new `mosaicKey`.
- **Slow Pi.** If p90 tile time exceeds 150 ms or frame time exceeds 3 s, native is disabled for 6 h, the reason goes to `/health`, and auto uses IEM tiles at z≥8.
- **Pi 3 (proposal superseded 2026-09-25).** Original proposal: `WFP_RADAR_NATIVE=auto|on|off`. Auto requires `/proc/device-tree/model` to report a Pi 4 *and* a startup calibration under 60 ms/tile. The under-volted Pi 3 keeps today's behaviour unchanged. **Shipped policy:** Site uses Level III on every board, including Pi 3; IEM tiles are the automatic outage/daily-limit fallback. See DATA_CONTRACT.md, “One site renderer”.

## 10. Test and verification plan

**Offline CI (no network):**
- **Fixtures.** One real KATX N0B (the 106 KB clear-air scan) and one N0H under `tests/data/level3/`. A synthetic Level III writer (numpy → packet 16 → bzip2 → headers) covers everything else.
- **Decoder and geometry.** Bad headers, thresholds and truncation are rejected; off-grid radials map correctly. A synthetic 40 dBZ ring at 100 km, 90° lands within 1 px; beam height is 1.07 km at 68 km.
- **Mosaic tables:**
  - A near below-threshold gate beats a distant echo (the painter regression).
  - GC falls through; BI clears.
  - A stale site drops out; the clock switches when the primary is down.
  - `mosaicKey` stays stable, and changes when QC state changes.
- **Tiers and switching.** Mocked S3 listing XML per tier; a `radar_auto.choose` table (band, guard time, coverage, manual expiry).
- **Page and validators.** Node harness tests for the `layers` path, captions and no mid-gesture decision; existing tile validators over rendered tiles.

**Live (CDP, each phase):**
- z10 over Seattle, native vs IEM ridge: screenshots plus a sharpness metric, the median run length of constant colour along canvas rows. IEM should measure about 9-10 px, native 2-6 px.
- A scripted pinch z6→9→7→6, plus `/health` timings and CPU through a rain event.

**Rollout:**
- **Phase 0, prototype.** `WFP_RADAR_NATIVE=proto`: KATX only, newest frame only, floor and speckle only, a manual "KATX native" picker entry, z8-11. **Gate:** the owner judges it sharp beside the phone app, and warm tiles take ≤40 ms on the Pi.
- **Phases 1-4.** Hardened decoder, N0H QC and history (one site); per-pixel mosaic; auto switching, picker, captions and tiers; blockage masks, Pi 3 gating, DATA_CONTRACT/CHANGELOG and a one-week bandwidth check against §8.

## 11. Risks and what I would not do

**Risks:**
1. **Sharp but untrustworthy.** KLGX sea clutter, cold-morning AP, bright band and blockage sectors could make native look worse than MRMS. Mitigations: HCA masking, fall-through, blockage masks, MRMS below z8, and an owner review on a real rain day after phases 0 and 2.
2. **Feed dependency.** A key or hosting change breaks discovery. Mitigations: strict validation, automatic fallback to IEM tiles, and a visible `/health` alarm.
3. **Engine complexity and Pi load.** `almanac_emit.py` is already 4,900 lines. New logic goes in three fork-only modules (`radar_level3`, `radar_native`, `radar_auto`) behind a thin adapter, plus a slow-render kill switch.

**What I would not do:**
- Fetch Level II (about 10 MB per volume).
- Add MetPy or Py-ART.
- Render polar data or WebGL on the page.
- Interpolate or smooth gates.
- Blend radars.
- Make N0Q or tgftp the primary source.
- Fetch native data in the watch, rest or dormant tiers.
- Show native at z≤6.


---

# Native-resolution radar: design (Fable)

## 1. Summary

Fetch NEXRAD Level III N0B (super-resolution 0.5° base reflectivity, 720 radials × 1840 gates × 250 m) for KATX and its neighbours straight from the public `unidata-nexrad-level3` S3 bucket, decode it in the engine with a ~150-line pure-Python/numpy NIDS reader, and render 256-px web-mercator tiles by nearest-gate lookup through a cached per-tile gate-index map. The mosaic is decided per pixel in gate space before drawing: lowest beam height wins, and a below-floor measurement from the preferred radar is a measurement, not a hole, so distant high beams no longer bleed through. The output is an ordinary immutable tile under a new source id, so the page, manifest, cache, revision digest and attention tiers need extension, not replacement. Zoom decides the source automatically at settle: MRMS at z≤7, native mosaic at z≥8, with one level of hysteresis, prefetch of the other side, a manual override and a caption that names what is drawn. The Pi 3 stays on IEM tiles.

## 2. Data source

N0B, product code 153, 0.5° tilt: 460 km at 250 m gates, 0.5° azimuth, 8-bit values (v/2 − 33 dBZ; 0 = below threshold, 1 = range-folded). Endpoint (verified 2026-09-25 03:42 UTC, anonymous HTTPS): `https://unidata-nexrad-level3.s3.amazonaws.com/{SITE3}_N0B_{YYYY}_{MM}_{DD}_{HH}_{mm}_{ss}`; listing via `?list-type=2&prefix=ATX_N0B_2026_09_25_03&max-keys=20` (~2 KB XML). Verified sizes (clear air): KATX 106–109 KB, KLGX 105–106 KB, KRTX 117–120 KB, KOTX 127–128 KB. Latency 40–90 s. Cadence ~6 min (VCP 35 in the PDB). Format: WMO header, 18-byte message header (code 153), 102-byte PDB, bzip2 symbology block (108 KB → 1.33 MB, `BZh9`) holding one packet-16 digital radial array; parsed end to end: 720 radials, 1840 bins, max 57.5 dBZ. N0C (CC, product 161) exists in the same bucket (~132 KB). Level II rejected (4.76 MB clear-air volume, 40× the bytes). tgftp `DS.p153r0/SI.katx/` returned 404. Fallbacks: native S3 → IEM ridge N0B tiles → MRMS Region → RainViewer.

## 3. Decoding

Own decoder `lib/radar_nids.py` (stdlib + numpy). Strip WMO lines; check code 153 and length; parse PDB (lat/lon/height, VCP, volume time, elevation, thresholds, symbology offset); bz2 with a 4 MB output cap; packet 16 into a (720, 1840) uint8 array plus azimuth starts. Validation: magic, product code, nbins ≤ 1840, nradials 360–720, azimuths increasing mod 360, site within 1 km of `_NEXRAD_SITES`, volume time within (−20 min, +2 min) and matching the key within 60 s, elevation 0.3–0.7°. Measured Mac: bz2 5.2 ms + parse 0.5 ms; Pi 4 estimate ~50 ms/scan.

## 4. Rendering

Engine, on the radar-tile threads. Per tile compute a gate-index map (site, z, x, y) → flat int32 gate index (−1 outside 460 km), cached in a 64-entry LRU (~16 MB); per scan one gather per site, per-pixel selection, 256-entry LUT gather into `_RADAR_DISPLAY_LUT`, palette PNG encode. Measured Mac 0.8 ms/tile (121-tile z9 sweep 98 ms); Pi 4 estimate 5–8 ms/tile. No anti-aliasing at z9–10; 2×2 supersample with max at z8. Palette PNG with tRNS so `_radar_tile_metadata` checks pass; new source id `nexrad-native`, path `t/<rev>/nexrad-native/-/<stamp>/z/x/y.png`; revision digest adds decoder, mosaic and QC versions; serve.py regex and page allow-list add the source; frames keep `siteScans`. Raw bytes in the native LRU; decoded arrays for the newest frame only.

## 5. Mosaic

Beam height h(r) = r·sin(0.5°) + r²/(2·(4/3)R) + site height. From Duvall: KATX 68 km → 865 m; KLGX 178 km → 3.4 km; KRTX 237 km → out of 230 km coverage; KOTX out. Per pixel, sites by ascending beam height: first site with an existing gate and value 2–255 wins, even below the floor. If the preferred gate is 0 or 1, fall through only to a site whose beam is under 3 km. Blockage: fall-through handles it in phase 1; learned occultation masks in phase 3. Time: frame ts = primary's scan; others within ts − 450 s … ts + 60 s; 100 m of beam-height penalty per minute of age.

## 6. Quality control

15 dBZ display floor and 25 dBZ echo floor unchanged; range-folded transparent. Speckle: keep a gate only if ≥ 3 of 8 polar neighbours ≥ 15 dBZ (~100 ms/scan Pi est). Static clutter map learned on the panel over dry scans (mask gates hot in > 80 % of the last 128 dry scans). VCP hint: clear-air VCP nights with no weather hold raise this source's floor to 20 dBZ. Optional phase 3: N0C for the primary, drop CC < 0.90.

## 7. Automatic zoom switching

MRMS at ≤ 7, native at ≥ 8; up at settle ≥ 8.0, down at settle ≤ 6.7; decided only in `radarSettle()`; intent gains `auto`. Hand-off keeps the current plates until ≥ 4 decoded frames. Live-tier prefetch of the other side's newest frame. Picker: Auto (default) / Region / KATX. Captions name the drawn data and append "· auto". Loop cadence follows the source. Native `min_zoom` 8, MRMS `max_zoom` 9. Rest/dormant use the S3 listing.

## 8. Budgets

Pi 4 estimates: decode 50 ms/scan, QC 100 ms/scan, render 5–8 ms/tile; newest frame with 2 sites over 15 tiles ≈ 0.4 s of one core. Engine memory +~25 MB. Page unchanged. Bandwidth (2 sites, 6-min cadence): ~53 MB/day if every scan is fetched all day; newest-only at night; rest/dormant listings only; 4 sites ~100 MB/day, so live fetches only sites with beam < 3 km in the viewport. Precipitation VCPs +30–50 %.

## 9. Failure modes

S3 outage → host breaker → IEM N0B tiles, caption "· lower detail". Decode error → rejected, logged, negative-cached 120 s. One radar down → mosaic from the rest. Late/short object → retry. Pi 3: `WFP_RADAR_NATIVE=0` default when cpu_count < 4 or boot validation > 10 ms/tile.

## 10. Test and verification plan

Commit the real KATX N0B fixture plus a KLGX scan and hand-built bad files; golden decode checksums (2198 gates ≥ 15 dBZ, 232 ≥ 25, max 57.5), index-map round trips, mosaic truth tables, speckle and clutter-map tests, S3 listing parser, IEM fallback, Pi 3 gate; node page tests for auto intent, hysteresis, captions. Live: z9 over Duvall screenshots, distinct colour cells per 100 px. Rollout: Phase 0 one evening prototype; 1 decoder + single site; 2 mosaic + QC; 3 auto switching; 4 N0C / occultation masks.

## 11. Risks and what I would not do

Risks: live-tier bandwidth (53–100 MB/day naive; mitigate with beam-height gating, newest-only at night, daily byte ceiling); raw-data ugliness (speckle, learned clutter, VCP floor, MRMS at z ≤ 7); bucket dependence (keep IEM path alive). Would not: fetch Level II, add MetPy/pyart, render in the page, change source mid-gesture.


_(Fable's design condensed from its final message.)_
