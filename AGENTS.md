# AGENTS.md

Orientation for AI agents and contributors working on this code. It describes how the
repository is organized, the invariants that must hold, and how to build, test, and
verify. It is not a changelog: `git log` holds the history. README.md is written for
people deploying the display in their hangar, not for working on the code.

## What this is

A wall-mounted hangar display: a Raspberry Pi 4 drives an HDMI TV through Chromium in
kiosk mode and cycles through live ADS-B traffic, METAR weather with wind barbs, a NEXRAD
radar loop, and a NOAA GOES satellite loop. It runs 24/7 from an SD card with no one
watching it, so robustness and low resource use matter more than features.

## Architecture

- `backend/` FastAPI on Python 3.13 with pydantic v2 and httpx. One process serves the
  REST API, a server-sent event (SSE) stream, and the built frontend as static files on
  port 8000. It runs in Docker via `docker compose` (rootless Docker on the reference Pi).
- `frontend/` plain JavaScript ES modules built with Vite. No framework. `src/main.js` is
  the display, `src/admin.js` the configuration page, `src/lib/*` shared modules. The
  Docker build compiles it into the image at `/app/static`.
- `deploy/` the native kiosk layer. `bootstrap.sh` provisions a fresh Pi (packages, rootless
  Docker, GPU overlay, seatd, tty1 autologin, Wi-Fi power saving, `.env`, first build) and
  is the reference for what the OS must look like; `kiosk-launch.sh` is exec'd from the
  tty1 autologin shell and starts cage (Wayland compositor) plus Chromium.
  The kiosk is not in Docker because it needs the GPU and HDMI output directly.
- `data/config.yaml` is the single source of truth for configuration, validated by the
  pydantic model in `backend/app/config.py`. It is bind-mounted as a directory
  (`./data:/data`) so atomic renames and backup rotation work.
- Config flow: the admin page `PUT`s the config, the backend validates and writes it
  (temp file, fsync, rename, `.bak.1..3` rotation), then broadcasts SSE `config_changed`
  and the display reloads.
- Traffic flow: the display polls `/api/traffic?view=<id>` once a second. The backend
  answers from a per-view snapshot that a background loop keeps fresh at 1 s for every
  recently polled view; the request itself never waits on an upstream fetch.

## Repository map

```
backend/app/
  main.py          routes, auth, security headers, SSE, static serving (AppStatic)
  config.py        pydantic models + constraints, secret masking, durable load/save
  manager.py       DataManager: sources, caches, rate limit, traffic/weather loops,
                   circuit breaker, blackout state, SSE subscribers
  views.py         config -> ordered list of views (local, regional, satellite)
  weather.py       aviationweather.gov METAR fetch, decode, flight category
  satellite.py     NOAA STAR CDN directory listing -> recent GOES frame URLs
  tiles.py         basemap tile cache/proxy and per-view warm-up (tile math mirrors
                   the display's fitBounds framing)
  geo.py           haversine, unit conversion
  sources/         traffic adapters: local.py (tar1090/readsb), aggregator.py
backend/tests/test_api.py   pytest suite (TestClient, upstreams monkeypatched)
frontend/src/
  index.html, main.js, styles.css    the display and its cycle
  admin.html, admin.js, admin.css    the configuration page
  lib/api.js        REST client with timeouts, ApiError, SSE subscription
  lib/adminForm.js  admin field schema, rendering, typed collection, error mapping
  lib/dom.js        esc(), el(), sleep()
  lib/map.js        MapLibre wrapper: markers, barbs, radar layers, view framing
  lib/aircraft.js   aircraft store, drop timeout, importance sort
  lib/shapes.js     silhouettes and type/category classification
  lib/windbarb.js   wind barb SVG
  lib/geo.js        client geo helpers
deploy/bootstrap.sh   idempotent OS provisioning for a fresh Pi (also --check / --dry-run)
deploy/kiosk-launch.sh, getty-autologin.conf
Dockerfile, docker-compose.yml, data/config.yaml
```

## Invariants: do not regress these

### Rendering

- Aircraft are HTML `maplibregl.Marker`s, not a GeoJSON symbol layer. Rapid `setData` on a
  GeoJSON source wedges it. Motion is client-side dead reckoning: `AircraftStore` keeps each
  aircraft's last fix (position, ground speed, track, and the fix time taken relative to
  the snapshot, so clock skew does not matter) and `moveAircraft()` positions markers at
  20 fps from `displayPosition()`, which blends a new fix in over about half a second
  instead of jumping. Do not reintroduce a CSS transition keyed to poll timing: receivers
  produce a new position per aircraft only about once a second with jitter, so a
  poll-driven tween stalls and jumps.
- Radar frames are MapLibre raster layers built once per page session and animated by
  toggling `visibility` and `raster-opacity` (cross-fade). Never add and remove raster
  sources per view: removed textures are not reclaimed on the Pi and GPU memory grows.
  Their tile URLs carry a 5-minute bucket (`?v=`) and `refreshRadar()` re-points them
  with `setTiles` when the regional view starts; without that the retained tile cache
  would show the same radar frames for the life of the page.
- The map container is never hidden with `display: none`. The satellite view is an
  overlay (`#sat` absolute over the stage, map and side panel `visibility: hidden`).
  A 0x0 map makes MapLibre shrink its per-source tile cache to a few tiles and evict
  everything, so every later view refetches its basemap and renders blurry first.
- The satellite loop is parallel `<img>` preloading followed by `src` swapping. Do not
  pre-decode frames into ImageBitmaps or draw them on a canvas: 24 frames at 1200x1200 is
  138 MB of GPU memory, the Pi's CMA pool is 512 MB and is shared with map tile textures,
  and the view goes black. Frames are about 750 KB each, so never load them one at a time
  either.
- Two color systems that never mix: aircraft are colored by altitude band; wind barbs and
  METAR text are colored by flight category (VFR green, MVFR blue, IFR red, LIFR magenta,
  white when unknown).
- Aircraft type and the GA-versus-airliner tier come from the ICAO type designator and the
  ADS-B emitter category (`shapes.js`), never from the callsign.
- Negative reported altitude means invalid; drop the aircraft. Local views include ground
  traffic. The regional view is weather only and asks `/api/weather/bbox` for every station
  inside the map's visible bounds.
- The UI is designed at 1920 wide. `@media (min-width: 2560px)` in `styles.css` and the
  `UI` factor in `map.js` scale it for 4K panels. The kiosk runs at the panel's native
  resolution; never use Chromium `--force-device-scale-factor` (cage renders into a quarter
  of the screen).

### Network and upstream services

- Basemap tiles are served by the backend from an on-disk cache (`GET /tiles/{z}/{x}/{y}.png`,
  `tiles.py`, directory `HANGAR_TILE_CACHE`, default `/data/tiles`), fetched from
  OpenStreetMap once per tile with an identifying User-Agent and a 14-day TTL. The cache is
  warmed at startup and after a config change with the tiles every configured view can
  show (both layouts, ideal zoom plus parent), capped so a bad config cannot bulk download.
  Keep OSM usage light and attributed: the footer credits OpenStreetMap, and warm-up must
  stay paced (two concurrent fetches). A custom `display.tile_url` bypasses the proxy.
- Never set a `Referrer-Policy` header or otherwise strip the Referer on pages that talk
  to OpenStreetMap directly (a custom raster `tile_url` may): their tile servers serve an
  "Access blocked" tile to requests without one.
- `/api/traffic` never blocks on an upstream fetch, and freshness comes from the background
  loop in `manager.py`, not from client polls. Refreshing only when a client polls aliases
  with the 1 Hz poll and halves the effective update rate.
- All aggregator calls, including test endpoints, go through the shared rate limiter
  (`_rate_limited_aggregator_fetch`, 1 request per second).
- The local receiver has a circuit breaker: log a failure once per state change and skip
  the receiver for 60 s. Never log per poll; per-second log lines fill the SD card. Uvicorn
  access logs are off and the httpx logger is at WARNING for the same reason.
- Inside the container `localhost` is the container. Under rootless Docker
  `host.docker.internal` does not reach the host either; a receiver on the Pi is addressed
  by its LAN IP.
- IEM serves NEXRAD time-lagged layers only up to `-m55m`; `config.py` enforces
  `(frames - 1) * interval_min <= 55`.
- Satellite frame URLs come from parsing the NOAA STAR CDN directory listing
  (`satellite.py`), cached for 240 s per parameter set. Query parameters are validated
  before they reach a URL.

### Safety and robustness

- Everything interpolated into `innerHTML` goes through `esc()` from `lib/dom.js`. Aircraft
  strings, METAR text, station names, and config labels are untrusted input.
- The admin page renders from the field schema in `lib/adminForm.js`: typed inputs carry
  `data-f` (config path) and `data-t` (type), values are parsed by declared type, and all
  add/delete/move buttons are handled by one delegated click listener on the root. Do not
  add per-render `addEventListener` calls (double-bound handlers) and do not decide a
  field's type by whether its text looks numeric.
- Config loading never raises at import time. `load_config()` falls back to the newest
  readable `.bak.N`, then to defaults, and reports the problem through `/healthz` and the
  admin banner. Saves are atomic with fsync and rotate three backups.
- Secrets (`SECRET_FIELDS` in `config.py`) are masked in every API response; a PUT that
  sends the mask keeps the stored value and an empty string clears it. `PUT /api/config`
  rejects unknown keys and honors `If-Match` against the config version (409 on mismatch).
- `HANGAR_ADMIN_PASSWORD` gates `/admin`, `PUT /api/config`, `POST /api/test-source`, and
  `POST /api/display/*` with HTTP Basic auth. Read endpoints the display uses stay open.
  `admin.html` is only reachable through `/admin` (`AppStatic` blocks the direct path).
- The display never calls `location.reload()` while the backend is unreachable, because
  Chromium's error page has no script to recover; use `safeReload()`. Boot retries with
  backoff, `nextView()` always schedules the next view even if a view throws, an invalid
  timezone falls back to UTC, and a watchdog reloads only when the backend answers.
- SSE `connected` carries `boot_id`, config `version`, and the current `display` state. The
  display reloads on a changed boot id or version and applies blackout state from the
  event. Subscribe to events before map setup so this works even while tiles load.
- HTML responses are `Cache-Control: no-cache` and hashed `/assets/` are immutable
  (`SecurityHeaders` in `main.py`), and the kiosk holding page navigates to the display
  with a unique `?launch=` query. Without both, Chromium's cache on the RAM disk serves a
  heuristically "fresh" old `index.html` for days (its `Last-Modified` is the build time)
  and the kiosk keeps running an old bundle after a rebuild. When checking a deploy, compare
  `document.scripts` in the live page with the script the server serves at `/`.
- The remote blackout (`POST /api/display/blackout` and `/restore`) is a full-screen cover
  (`#blackout`), not a stop: the cycle and polling keep running underneath so restore is
  instant. A timed blackout auto-restores server-side and the display arms its own
  fallback timer.

### Style

- Backend: pydantic models with explicit constraints; validators normalize input (ICAO is
  upper-cased). Structured 422 errors as `[{loc, msg}]`.
- Frontend: ES modules, no framework, no build tooling beyond Vite. Keep modules single
  purpose and keep the display's per-second work minimal (skip DOM rebuilds when the
  rendered markup is unchanged).
- Brand name is "The Pilot Channel". Avoid em dashes in prose.
- Commit only when asked. Do not put history, attribution, or change notes into source
  files or this document.

## Build, test, verify

- Rebuild after any change: `docker compose up -d --build`. Nothing hot-reloads; the
  frontend is compiled into the image. The display reloads itself when it sees the new
  backend boot id. To restart the kiosk browser by hand: `pkill -x cage` (autologin
  respawns it through `deploy/kiosk-launch.sh`).
- Backend tests (no network needed):

  ```bash
  cd backend && python -m pytest -q tests
  ```

  or inside the image, without touching the running container:

  ```bash
  docker run --rm -v "$PWD/backend/tests:/app/tests:ro" -w /app hangar-display:latest \
    sh -c "pip install -q pytest && python -m pytest -q tests"
  ```

  Tests set `HANGAR_CONFIG` and `HANGAR_STATIC` before importing `app.main` and monkeypatch
  every upstream fetch. The SSE endpoint is an endless stream and hangs Starlette's
  TestClient; exercise it with `curl -N`.
- Try a build beside production: `docker build -t hangar-display:test .` then
  `docker run -d -p 8001:8000 -v "$PWD/data:/data" hangar-display:test`.
- Frontend checks: `node --check` on each module. Headless Chromium
  (`chromium --headless=new --disable-gpu --dump-dom URL`) works for the admin page and for
  module tests loaded with `--allow-file-access-from-files`. Headless Chromium has no WebGL,
  so the display's map never finishes loading there; the display page holds its event
  stream open, so `--dump-dom` will not return either. Drive it over the DevTools protocol
  (`--remote-debugging-port`) when you need page state.
- Verify the display on the real kiosk: screenshots with
  `WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 grim out.png`, per-process CPU
  from `top`, and GPU memory from `CmaFree` in `/proc/meminfo`. The kiosk Chromium exposes
  the DevTools protocol on `127.0.0.1:9222` (localhost only), so page state can be read or
  sampled live: `curl -s 127.0.0.1:9222/json` lists the page target. Any performance change must
  be measured there before it is called an improvement. The regional radar view is the
  heaviest view by design (renderer 30 to 60 percent of a core, GPU about 20 percent); local
  views sit near 10 percent.
- Dev servers: backend `uvicorn app.main:app --reload --port 8000` with `HANGAR_CONFIG` and
  `HANGAR_STATIC` set; frontend `npm run dev` proxies `/api` to port 8000 and serves the
  admin page at `/admin.html`.
- Dependencies are pinned: `backend/requirements.txt` exactly, `frontend/package-lock.json`
  committed and installed with `npm ci`. Run `pip-audit` inside the image when bumping.

## Runtime environment

- Environment variables: `HANGAR_CONFIG` (default `/data/config.yaml`), `HANGAR_STATIC`
  (default `/app/static`), `HANGAR_TILE_CACHE` (default `/data/tiles`),
  `HANGAR_ADMIN_PASSWORD` (optional; compose reads it from `.env`).
- Docker runs rootless as the kiosk user (Docker CE from Docker's apt repo, user
  `docker.service`, linger enabled, CLI on the `rootless` context; the rootful daemon is
  disabled by the bootstrap). Keep OS-level requirements in `deploy/bootstrap.sh` and verify
  a Pi with `bootstrap.sh --check` rather than by hand.
- Container: the process is root inside the container on purpose. Under rootless Docker
  that is the unprivileged host user, and a non-root container user could not write the
  bind-mounted `data/` directory. Compensating controls in `docker-compose.yml`: all
  capabilities dropped, `no-new-privileges`, read-only root filesystem with a `/tmp` tmpfs,
  json-file logging capped at three 10 MB files. The memory cgroup is not delegated to
  rootless Docker on Raspberry Pi OS, so `mem_limit` is unavailable.
- Kiosk: Chromium's profile, disk cache, and the kiosk log live on the RAM disk
  `/dev/shm/hangar-kiosk` and are recreated at boot. The launcher opens a holding page that
  probes `/healthz` and navigates to the display when the backend is up. The Chromium flags
  in `kiosk-launch.sh` disable background services (sync, component updates, push
  connections) that a kiosk never needs.

## HTTP API

- `GET /healthz` liveness, `boot_id`, source status, config load state.
- `GET /api/config` config with secrets masked plus `version` (also the `ETag`).
  `PUT /api/config` validates and writes; optional `If-Match`; 422 with `[{loc, msg}]`.
- `GET /api/views` ordered view descriptors the display cycles through.
- `GET /api/traffic?view=<id>` snapshot with `aircraft`, `ts`, `age_s`, `stale`,
  `healthy`, `error`; 404 for an unknown view.
- `GET /api/weather?ids=<csv>`, `GET /api/weather/bbox?min_lat=&min_lon=&max_lat=&max_lon=`
  (at most 20 by 30 degrees), `GET /api/weather/area?lat=&lon=&radius_nm=`.
- `GET /api/satellite?sat=&sector=&band=&size=&frames=` recent frame URLs.
- `GET /api/status` source, health, uptime, weather, config, display, and tile cache state.
- `GET /tiles/{z}/{x}/{y}.png` cached basemap tile (fetched upstream on a miss; a stale
  tile is served if upstream fails; 404 for invalid coordinates).
- `POST /api/test-source` tries a `data_source` block from the first enabled airport.
- `GET /api/display`, `POST /api/display/blackout` (`{"seconds", "reason"}`, both
  optional), `POST /api/display/restore`.
- `GET /api/stream` SSE: `connected`, `config_changed`, `weather_updated`, `display`.

## Configuration model

`Config` in `config.py` has sections `airports`, `regions`, `cycle`, `data_source`,
`display`, `weather`, `satellite`, and `radar`. Every numeric field has bounds, ICAO
identifiers match `^[A-Z0-9]{3,4}$`, airport ICAOs and region names are unique, the
timezone must be a valid IANA name, `local_url` and `tile_url` must be http(s) and not
link-local, satellite `sector`/`band`/`size` are constrained to safe characters, and the
radar loop span is capped at 55 minutes. Add new settings to the model with constraints,
to the admin field schema in `lib/adminForm.js`, and to the config reference in README.md.
