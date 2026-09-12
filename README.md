# spatialdata

Turns geographic data into cinematic documentary video: 3D terrain flyovers
with animated data overlays, AI voiceover, and a timeline that syncs the
overlays to the narration script.

Everything it uses is free. No paid APIs, no usage-billed services, no API
keys, no credit card anywhere in the stack.

---

## What it does

Give it a region, a date, some overlay layers and a narration script. It
returns a finished MP4:

1. Synthesises the narration locally (Kokoro) and force-aligns it word by word
   (Whisper), so the video's length is set by the script rather than a guess.
2. Builds a camera flyover over the region and expands it to per-frame
   keyframes.
3. Works out exactly which map tiles those frames will need and downloads them
   **before** rendering starts.
4. Renders frame by frame in headless Chromium with MapLibre GL JS, on the GPU.
5. Fades data overlays in and out at the moments the script mentions them.
6. Mixes narration over music, ducking the music under the voice.
7. Burns in attribution, appends a credit card, and encodes on the AMD hardware
   encoder.

---

## Quick start

```bash
pip install -r requirements.txt
python -m playwright install chromium

# One-time datasets (all free; ~700 MB with the 3km population build)
python data/download_data.py --natural-earth --music --kontur 3km

# Check the machine has what it needs
python -m worker doctor

# Render the bundled example
python -m worker render --job examples/alps.json

# Or use the dashboard
python -m worker serve          # http://127.0.0.1:8000
```

`ffmpeg` and `ffprobe` must be on `PATH`. On Windows:
`winget install Gyan.FFmpeg`.

---

## The $0 rule, and how it is enforced

| Need | Service | Cost | Key? |
|---|---|---|---|
| Basemap tiles | OpenFreeMap | free, unlimited | no |
| 3D terrain (DEM) | Mapterhorn | free | no |
| Satellite + weather | NASA GIBS (WMTS) | free | no |
| Political borders | Natural Earth *(bundled)* | public domain | no |
| Population density | Kontur Population *(bundled)* | CC BY | no |
| Place search | OSM Nominatim | free | no |
| Map renderer | MapLibre GL JS | open source | never Mapbox |
| Voiceover | Kokoro (local) | free, unlimited | no |
| Word timings | Whisper (local) | free | no |
| Music | Kevin MacLeod, CC BY | free | no |
| Encode | `h264_amf` (your GPU) | free | — |

Google Cloud TTS is available as an optional second provider in the same
dropdown, but it is never the default and is only reachable if you have already
configured credentials.

### Fair use is structural, not a promise

These are public-good services, so the pipeline is built so that overusing them
is difficult:

- **Every network read goes through one registry** (`worker/sources/upstreams.py`)
  which carries a per-service requests/second cap and concurrency limit. There
  is no other code path to the internet.
- **The GIBS capabilities document (5.6 MB) is fetched at most once**, ever, and
  read from disk thereafter. A normal render never needs it at all, because the
  layers this project offers are a curated table in `worker/sources/gibs.py`.
- **All tiles are pre-fetched before rendering starts.** The render loop reads
  from local disk through a localhost server; the map page cannot reach the
  internet because every URL in the style — including URLs *inside* fetched
  TileJSON documents — is rewritten to point at `127.0.0.1`.
- **Natural Earth and Kontur are downloaded once** by `data/download_data.py`
  and read from local disk. They are never fetched per render.
- **404s are cached**, so a prefetch pass never re-asks for tiles that do not
  exist.

You can verify the no-live-requests guarantee yourself:

```bash
python -m worker render --job examples/alps.json --offline
```

`--offline` makes the local tile server refuse to fetch anything mid-render. It
reports the miss count at the end; a correctly warmed cache reports **0**.

```
plan   : 379 objects (206 OpenFreeMap + 173 Mapterhorn)
render : 300 frames, 32.3s, 9.3 fps, 0 timeouts
MISSES : 0
```

### Attribution

Required by the data licences, so it is automatic and not optional: a compact
credit sits in the corner of every frame. That alone satisfies the licences, so
the closing credit card is **off by default** (`encode.end_card_seconds: 0`) —
a full-screen card cutting in after the picture reads as the video breaking
rather than ending. Set it to 4 for a card; it lists only the sources that
render actually used. If you publish a video that uses the bundled music,
keep the Kevin MacLeod credit in your description as well
(`data/music/CREDITS.txt`).

---

## Architecture

Two pieces, split so the hosted half can stay free:

```
  ┌────────────────────────┐         ┌──────────────────────────────┐
  │  Dashboard             │  poll   │  Local render worker         │
  │  (localhost, or        │◄────────│  (this machine, RX 6600 XT)  │
  │   Railway free tier)   │ ──────► │                              │
  │                        │ upload  │  data fetch + cache          │
  │  job queue (JSON)      │         │  Playwright + MapLibre       │
  │  static UI             │         │  Kokoro TTS + Whisper        │
  │  no rendering, ever    │         │  ffmpeg / h264_amf           │
  └────────────────────────┘         └──────────────────────────────┘
```

Run it either way:

- **All local** (simplest): `python -m worker serve` runs the UI *and* a render
  thread on the same machine. Nothing is hosted.
- **Split**: deploy the dashboard to Railway, then on this machine run
  `python -m worker poll --url https://your-app.up.railway.app --token SECRET`.

The hosted side installs only `requirements-dashboard.txt` — FastAPI, uvicorn
and aiohttp. No Chromium, no ffmpeg, no torch. It stores job JSON, serves the
UI, and receives one MP4 per job, which is what keeps it inside a free tier.

### How the two halves talk

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/meta` | voices, layers, styles, music the worker actually has |
| `POST` | `/api/jobs` | queue a job |
| `POST` | `/api/jobs/claim` | worker takes the oldest queued job |
| `POST` | `/api/jobs/{id}/progress` | stage, %, message, stats |
| `POST` | `/api/jobs/{id}/upload` | finished MP4 |
| `GET` | `/api/jobs/{id}/video` | stream / download |

Claiming marks a job started, so two workers cannot pick up the same one.
Progress posts are throttled to one every two seconds, so a 500-frame render
does not spend the hosted side's request budget.

Set `SPATIALDATA_TOKEN` on both sides to require a shared secret.

---

## Pipeline stages

| Stage | Module | Notes |
|---|---|---|
| Narration | `worker/tts/` | Kokoro (default) or Google Cloud; per-sentence |
| Word timings | `worker/tts/align.py` | Whisper, mapped back onto the script |
| Camera path | `worker/camera.py` | hold / flyto / orbit / path, great-circle |
| Timeline | `worker/beats.py` | timestamp → camera → active overlays |
| Prefetch | `worker/prefetch.py` | exact tile set, derived from the built style |
| Render | `worker/render/` | Playwright + MapLibre, deterministic per frame |
| Overlays | `worker/render/style.py` | borders, population, weather, satellite |
| Audio | `worker/audio/mix.py` | sidechain ducking under the voice |
| Encode | `worker/encode.py` | `h264_amf`, attribution, end card |

### The beats file

Each job writes `output/<job>/beats.json` — one document mapping time to camera
state and overlay visibility:

```json
{
  "fps": 30, "frames": 494, "duration_s": 16.467,
  "beats": [
    {"t": 0.0,   "duration": 4.72, "overlays": {"borders": {"opacity": 1.0}},
     "text": "The Alps rise along the border between Switzerland and Italy."},
    {"t": 5.0,   "duration": 4.98, "overlays": {},
     "text": "Shaped over thirty million years, they form a wall of rock and ice."},
    {"t": 10.26, "duration": 4.72, "overlays": {"population": {"opacity": 1.0}},
     "text": "Today, nearly fourteen million people live in the valleys below."}
  ]
}
```

Those overlay assignments are generated from the script: "border between
Switzerland and Italy" raises the borders layer, "people live in" raises
population, and the geology sentence gets neither. Overlays cross-fade rather
than popping, because a hard cut on a translucent raster reads as a glitch.

An overlay is *introduced* on its sentence and then **stays on** at a lower
opacity for the rest of the piece (`0.78` by default). Switching it off again
when its sentence ends makes the layer flash on and vanish with nothing on
screen to explain it; documentary layers accumulate rather than blink. Pass
`persist=False` to `auto_beats`, or write the `beats` array yourself, for
strict per-sentence visibility.

Pass your own `beats` array in the job spec to take manual control.

---

## Rendering notes

**It is not a screen recording.** Each frame sets the camera, waits for MapLibre
to report itself genuinely settled, then screenshots. Two runs of the same
camera path produce the same pixels: `fadeDuration` is 0, the camera moves with
`jumpTo` (never `flyTo`), and locale, timezone and colour profile are pinned.

**Prefetching is exact, not guessed.** `worker/frustum.py` reproduces MapLibre's
camera projection — perspective FOV, pitch, bearing, rays intersected with the
ground plane — so the pipeline knows each frame's true ground footprint. It is
verified against the browser:

```bash
python tests/test_frustum_matches_maplibre.py
# PASS: python frustum matches MapLibre unproject on all cases
```

With 3D terrain the flat-ground footprint is not enough (elevated ground rises
into view beyond the flat horizon), so the plan widens by an amount that grows
with pitch.

**The camera does not bob over terrain.** With 3D terrain on, MapLibre clamps
the camera's centre to the ground by default, so the shot lurches upward every
time a ridge passes beneath it — moving the camera a short distance across the
Alps swings its height from 3375 m to 4464 m. The pipeline instead samples the
cached DEM along the whole path, low-pass filters it, and feeds that back as an
explicit elevation with `centerClampedToGround` turned off. Peak frame-to-frame
height change drops from ~350 m to ~18 m, so the camera rises gently over a
mountain range the way a helicopter would instead of tracking each bump.

Tune it with `render.elevation_follow` (1.0 hugs the smoothed ground, 0.0 flies
dead level) and `render.elevation_smoothing_s`.

**Speed.** Roughly 9 fps at 1280×720 on an RX 6600 XT via ANGLE/D3D11 — a
12-second clip takes about a minute of rendering. Confirm you are on the GPU
and not software rendering with `python -m worker doctor`; it prints the WebGL
renderer string.

---

## Overlays

| Overlay | Source | Notes |
|---|---|---|
| `borders` | Natural Earth, or OpenMapTiles `boundary` | falls back to the basemap's own boundary layer, which costs **no extra download** |
| `population` | Kontur H3 hexagons | choropleth draped on terrain; opacity ramps with density so empty ground stays clear |
| `weather` | NASA GIBS | clouds, aerosols, snow, thermal anomalies |
| `satellite` | NASA GIBS | daily true colour, time-enabled |

The population overlay picks the Kontur build that suits the view: 22 km cells
for a continent, 3 km for a country, 400 m for a city. If the finest installed
build is too coarse for the shot it says so and tells you which one to fetch.

Kontur ships in **EPSG:3857 (metres)**, not degrees — the reader reprojects both
the query box and the output geometry. Its GeoPackage is read with `sqlite3` and
a small WKB parser, so there is no GDAL or GeoPandas dependency:

```bash
python tests/test_population_reader.py
# PASS: Kontur GeoPackage reader works without GDAL/GeoPandas
```

---

## Rendering in the cloud, with your machine off

`.github/workflows/render.yml` renders a film on a GitHub Actions runner.
Public repositories get unlimited free Actions minutes, so this costs nothing
and needs no account anywhere else.

Actions → **Render a film** → Run workflow. Give it an example name
(`alps`, `indus`, …) or paste a whole job document, and collect the MP4 from
the run's artifacts.

The runner has no GPU, so Chromium falls back to SwiftShader software
rendering. **3D terrain still works** — it is simply slow, so
`config/cloud.json` trades resolution and terrain detail for a render that
finishes:

| | this desktop, GPU | this desktop, CPU only | 4-core runner (est.) |
|---|---|---|---|
| 3D terrain, 854×480 | — | **1.6 fps** | ~0.5 fps |
| 3D terrain, 1920×1080 | 5.0 fps | impractical | impractical |

At roughly 0.5 fps a 20-second film is about 90 minutes of runner time, well
inside the 6-hour limit on a single job. Dependencies, the Chromium build, the
bundled datasets and the tile cache are all cached between runs, so only the
first run pays for setup.

What the cloud profile changes, and why:

| Key | Cloud value | Reason |
|---|---|---|
| `render.gpu` | `false` | go straight to SwiftShader instead of failing to get a GL context |
| `render.max_width` / `max_height` | `854` / `480` | the job file is scaled to fit rather than rejected |
| `render.max_fps` | `24` | a fifth fewer frames to draw than 30 |
| `render.terrain_maxzoom` | `8` | DEM detail is where software rendering spends its time |
| `render.idle_timeout_s` | `90` | a software frame legitimately takes longer to settle |
| `encode.encoder` | `libx264` | there is no AMD card on a runner |
| `tts.whisper_model` | `tiny.en` | alignment on 4 cores |

Select the profile locally the same way the workflow does:

```bash
SPATIALDATA_PROFILE=cloud python -m worker render --job examples/alps.json
```

---

## Configuration

`config/default.json` → `config/<profile>.json` → `config/local.json` →
per-job settings, last wins. The profile layer is chosen with the
`SPATIALDATA_PROFILE` environment variable and is how the cloud renderer
reduces quality without editing any tracked job. The dashboard's Settings tab
writes `config/local.json`.

| Key | Default | Meaning |
|---|---|---|
| `render.miss_policy` | `warn` | `offline` refuses live fetches mid-render |
| `render.terrain_exaggeration` | `1.3` | vertical scale of the 3D terrain |
| `encode.end_card_seconds` | `0` | closing credit card; 0 = off |
| `render.terrain_maxzoom` | `12` | DEM tiles are large; capping saves bandwidth |
| `render.elevation_follow` | `0.85` | how much of the terrain's rise the camera adopts |
| `render.elevation_smoothing_s` | `2.5` | low-pass window on the camera height |
| `prefetch.tile_padding` | `1` | extra ring of tiles around each frame |
| `encode.encoder` | `h264_amf` | falls back to libx264 only if AMF is absent |
| `render.gpu` | `true` | `false` forces SwiftShader software rendering |
| `render.max_width` / `max_height` / `max_fps` | `null` | ceilings a job is scaled down to fit |
| `overlays.population.opacity` | `0.68` | peak choropleth opacity |
| `audio.music_gain_db` / `duck_gain_db` | `-22` / `-14` | music bed, and how far it ducks |

---

## CLI

```
python -m worker render   --job examples/alps.json [--offline] [--overlays borders,population]
python -m worker serve    [--port 8000] [--no-worker]
python -m worker poll     --url https://... [--token SECRET]
python -m worker doctor
python -m worker cache    [--clear] [--upstream ofm]
python -m worker jobs
```

`render` accepts `--bbox w,s,e,n`, `--place`, `--date`, `--narration` (text or a
`.txt` path), `--music`, `--voice`, `--duration`, `--fps`, `--width/--height`.

---

## Layout

```
worker/
  cli.py  pipeline.py  job.py  config.py  doctor.py
  camera.py  frustum.py  tiles.py  beats.py  prefetch.py
  compose.py  encode.py  remote.py  dashboard_app.py
  cache/     store.py  fetch.py  server.py
  sources/   upstreams.py  gibs.py  borders.py  population.py
  render/    style.py  capture.py  map.html  vendor/
  tts/       catalog.py  kokoro_tts.py  gcloud_tts.py  align.py
  audio/     mix.py
dashboard/static/   index.html  style.css  app.js
data/      download_data.py  natural_earth/  kontur/  music/
examples/  alps.json  satellite_storm.json  city_population.json
tests/     test_frustum_matches_maplibre.py  test_population_reader.py
cache/     output/  jobs/          (all gitignored)
```

---

## Troubleshooting

**Cache misses during render.** The prefetch was under-warmed. Raise
`prefetch.tile_padding`, or check whether the camera path leaves the region.
Misses are reported per job and never break a render under the default `warn`
policy.

**Software rendering / very slow frames.** `doctor` prints the WebGL renderer.
If it says SwiftShader, Chromium did not get the GPU; the capture code falls
back automatically but a render will take several times longer.

**The camera jolts upward over hills.** Elevation stabilisation is only applied
when `terrain` is on for the job. Check the log for `camera elevation
stabilised: peak step ... -> ...`; if the DEM tiles were not cached it warns and
falls back. Lower `render.elevation_follow` for a flatter, calmer flight.

**Population overlay looks blocky.** The Kontur build is too coarse for the
view; the log names the one to download.

**A data source is unreachable.** `doctor` probes all three upstreams. Note that
GIBS, Kontur and Natural Earth are IPv4-only: on an IPv6-only network without
NAT64 they vanish while OpenFreeMap and Mapterhorn keep working. `curl -4`
versus `curl -6` will tell you quickly. Borders fall back to the basemap layer,
and a job skips the population overlay rather than failing.

---

## Licences

Code in this repository is yours. The data and media it pulls carry their own
terms, all of which are free and all of which are credited automatically:
OpenStreetMap contributors (ODbL), OpenFreeMap, Mapterhorn, NASA GIBS/EOSDIS,
Kontur Population (CC BY), Natural Earth (public domain), Kevin MacLeod
(CC BY 4.0), MapLibre GL JS (BSD-3-Clause).
