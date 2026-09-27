# AGENTS.md

Orientation for agents and contributors working on this code: how the repository is
organized, the invariants that must hold, and how to build, test, and verify. It is not a
changelog; `git log` holds the history. README.md is for people deploying the display.

## What this is

A wall-mounted hangar display: a Raspberry Pi 4 drives an HDMI TV through Chromium in
kiosk mode and cycles through live ADS-B traffic, METAR weather with wind barbs, a NEXRAD
radar loop, and a NOAA GOES satellite loop. It runs unattended from an SD card, so
robustness and low resource use come before features.

## Architecture

- `backend/` FastAPI on Python 3.13 with pydantic v2 and httpx. One process serves the
  REST API, a server-sent event (SSE) stream, and the built frontend as static files on
  port 8000. It runs in Docker via `docker compose` (rootless Docker on the reference Pi).
- `frontend/` plain JavaScript ES modules built with Vite, no framework. `src/main.js` is
  the display, `src/admin.js` the configuration page, `src/lib/*` shared modules. The
  Docker build compiles it into the image at `/app/static`.
- `deploy/` the native kiosk layer. `bootstrap.sh` provisions a fresh Pi and is the
  reference for what the OS must look like; `kiosk-launch.sh` is exec'd from the tty1
  autologin shell and starts cage (Wayland compositor) plus Chromium; `harden-wifi.sh`,
  run by the bootstrap, keeps Wi-Fi in NetworkManager keyfiles. The kiosk is not in Docker
  because it needs the GPU and HDMI output directly.
- `data/config.yaml` is the single source of truth for configuration, validated by the
  pydantic model in `backend/app/config.py`. It is bind-mounted as a directory
  (`./data:/data`) so atomic renames and backup rotation work.
- Config flow: the admin page `PUT`s the config, the backend validates and writes it
  (temp file, fsync, rename, `.bak.1..3` rotation), then broadcasts SSE `config_changed`
  and the display reloads.
- Traffic flow: the display polls `/api/traffic?view=<id>` once a second. The backend
  answers from a per-view snapshot that a background loop keeps fresh at 1 s for every
  recently polled view; the request never waits on an upstream fetch.

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
  sources/         traffic adapters: local.py (tar1090/readsb), aggregator.py; base.py
                   normalizes records and adds type_desc/wtc from data/icao_aircraft_types.json
backend/tests/    support.py (env), conftest.py (client fixture), test_api.py, test_behaviour.py
frontend/tests/   headless module tests (*.test.html), harness.js, run.sh
frontend/src/
  index.html, main.js, styles.css    the display and its cycle
  admin.html, admin.js, admin.css    the configuration page
  lib/api.js        REST client with timeouts, ApiError, SSE subscription
  lib/adminForm.js  admin field schema, rendering, typed collection, error mapping
  lib/dom.js        esc(), el(), sleep()
  lib/map.js        MapLibre wrapper: markers, barbs, radar layers, view framing
  lib/aircraft.js   aircraft store, dead reckoning, drop timeout, importance sort
  lib/shapes.js     icon assignment (tar1090 methodology) and rendering
  lib/vendor/tar1090/markers.js, LICENSE   vendored tar1090 shapes and tables (GPL-2.0)
  lib/stations.js   which station set the regional view shows, footer state
  lib/windbarb.js   wind barb SVG
  lib/geo.js        client geo helpers
deploy/bootstrap.sh   idempotent OS provisioning for a fresh Pi (also --check / --dry-run)
deploy/harden-wifi.sh   Wi-Fi as NetworkManager keyfiles with read-only copies (also --check)
deploy/grim-web-wrapper.sh   installed as /usr/local/bin/grim: web image formats via ImageMagick
deploy/kiosk-launch.sh, getty-autologin.conf
Dockerfile, docker-compose.yml, data/config.yaml
```

## Invariants

### Rendering

- Aircraft are HTML `maplibregl.Marker`s, not a GeoJSON symbol layer (rapid `setData` on a
  GeoJSON source wedges it). Motion is client-side dead reckoning: `AircraftStore` keeps
  each aircraft's last fix (position, ground speed, track, and the fix time relative to
  the snapshot, so clock skew does not matter) and `moveAircraft()` positions markers at
  20 fps from `displayPosition()`, which blends a new fix in over about half a second and
  snaps when the correction exceeds half a mile. Do not key a CSS transition to poll
  timing: receivers report a position per aircraft about once a second, with jitter.
- Radar frames are MapLibre raster layers built once per page session and animated by
  toggling `visibility` and `raster-opacity`. Never add and remove raster sources per
  view: the Pi does not reclaim removed raster textures. The cross-fade
  (`radar.crossfade`, default off) sets a `raster-opacity-transition`, which re-renders
  the whole map for the duration of every fade. Tile URLs carry a 5-minute bucket (`?v=`)
  and `refreshRadar()` re-points them with `setTiles` when the regional view starts.
- The style declares `transition: {duration: 0}` and `_initLayers` zeroes it for vector
  styles. MapLibre's default global transition applies to every style change, including
  the style's light, and re-renders the whole map for its duration on each radar step.
- The map container is never hidden with `display: none`; the satellite view is an overlay
  (`#sat` over the stage, map and side panel `visibility: hidden`). A 0x0 map shrinks
  MapLibre's tile cache to a few tiles.
- The satellite loop is parallel `<img>` preloading followed by `src` swapping. Do not
  pre-decode frames into ImageBitmaps or draw them on a canvas (the frames exceed the Pi's
  GPU memory, a CMA pool shared with map textures), and do not load them one at a time.
- Two color systems that never mix: aircraft by altitude band; wind barbs and METAR text
  by flight category (VFR green, MVFR blue, IFR red, LIFR magenta, white when unknown).
- Aircraft icons follow tar1090: `lib/shapes.js` calls the vendored `getBaseMarker()` in
  `lib/vendor/tar1090/markers.js` (tar1090's shapes and tables, GPL-2.0, license in that
  directory; regenerate from upstream rather than editing), which picks a shape by exact
  ICAO type designator, then ICAO 8643 type description plus wake turbulence category, then
  ADS-B emitter category. The backend supplies `type_desc` and `wtc` per aircraft from the
  vendored tar1090-db type table (`backend/app/data/icao_aircraft_types.json`) and passes
  `db_flags`. The GA-versus-airline tier (`isGA()`) comes from those fields and the
  military flag, never from the callsign. Shapes flagged `noRotate` are drawn upright.
- Negative reported altitude means invalid; drop the aircraft. Local views include ground
  traffic. The regional view is weather only and asks `/api/weather/bbox` for every station
  inside the map's visible bounds.
- A local view's traffic is fetched for `fetch_radius_nm` (`FETCH_RADIUS_FACTOR` times the
  framing radius, covering the corners of the 16:9 map). The store trims to the map's
  visible bounds and drops an unreported aircraft as soon as its reckoned position is off
  screen.
- The basemap keeps every configured view's tiles in MapLibre's memory cache
  (`maxTileCacheZoomLevels: 12`, `maxTileCacheSize: 480`, `refreshExpiredTiles: false`)
  and the `osm` layer has `raster-fade-duration: 0`. A settled cycle shows no `/tiles/`
  responses on a view switch (DevTools `Network` domain).
- The UI is designed at 1920 wide. `@media (min-width: 2560px)` in `styles.css` and the
  `UI` factor in `map.js` scale it for 4K panels. The kiosk runs at the panel's native
  resolution; never use Chromium `--force-device-scale-factor` (cage renders into a quarter
  of the screen).

### Network and upstream services

- Basemap tiles are served by the backend from an on-disk cache (`GET /tiles/{z}/{x}/{y}.png`,
  `tiles.py`, directory `HANGAR_TILE_CACHE`, default `/data/tiles`), fetched from
  OpenStreetMap once per tile with an identifying User-Agent and a 14-day TTL. The cache is
  warmed at startup and after a config change with the tiles every configured view can
  show (both layouts, ideal zoom plus parent), capped per view and in total so a bad
  config cannot bulk download. Warm-up runs two fetches at a time. The footer credits
  OpenStreetMap. A custom `display.tile_url` bypasses the proxy.
- Never set a `Referrer-Policy` header or otherwise strip the Referer on pages that talk
  to OpenStreetMap directly (a custom raster `tile_url` may): their tile servers serve an
  "Access blocked" tile to requests without one.
- `/api/traffic` polls of an active view never wait on an upstream fetch; freshness comes
  from the background loop in `manager.py`, not from client polls (refreshing only when a
  client polls aliases with the 1 Hz poll). The first poll of a view that just became
  active waits up to `FIRST_POLL_WAIT_S` for the refresh it started and otherwise answers
  `pending` rather than the leftover snapshot from the previous visit.
- All aggregator calls, including test endpoints, go through the shared rate limiter
  (`_rate_limited_aggregator_fetch`, 1 request per second).
- The local receiver has a circuit breaker: a failure is logged once per state change and
  the receiver is skipped for `LOCAL_RETRY_S`. Never log per poll. Uvicorn access logs are
  off and the httpx logger is at WARNING.
- Inside the container `localhost` is the container. Under rootless Docker
  `host.docker.internal` does not reach the host either; a receiver on the Pi is addressed
  by its LAN IP.
- IEM serves NEXRAD time-lagged layers only up to `-m55m`; `config.py` enforces
  `(frames - 1) * interval_min <= 55`. Radar sources are capped at `maxzoom: 9`: the data
  is about 1 km, and closer views overzoom z9 tiles instead of fetching finer ones per
  frame.
- Weather never blocks the display. `/api/weather/bbox` and `/area` are
  stale-while-revalidate (`manager.py`): a cached set is returned at once with `age_s`,
  `stale`, and the last `error` and refreshed in the background; only a never-fetched set
  waits, bounded by `WEATHER_COLD_WAIT_S`. The METAR loop retries a failed refresh after
  `WEATHER_RETRY_S`; `weather.py` retries transient errors (DNS `EAI_AGAIN`, connect and
  read timeouts) once; the last good weather is persisted to `weather-cache.json` next to
  the config and restored at start. `lib/stations.js` keeps the last non-empty station set
  through a failing refresh and changes only the footer. Do not add public fallback
  resolvers: the LAN receiver's name is known only to the router.
- Satellite frame URLs come from parsing the NOAA STAR CDN directory listing
  (`satellite.py`), cached for 240 s per parameter set. Query parameters are validated
  before they reach a URL.

### Safety and robustness

- Everything interpolated into `innerHTML` goes through `esc()` from `lib/dom.js`. Aircraft
  strings, METAR text, station names, and config labels are untrusted input.
- The admin page renders from the field schema in `lib/adminForm.js`: typed inputs carry
  `data-f` (config path) and `data-t` (type), values are parsed by declared type, and all
  add/delete/move buttons are handled by one delegated click listener on the root. Do not
  add per-render `addEventListener` calls and do not infer a field's type from its text.
- Config loading never raises at import time. `load_config()` falls back to the newest
  readable `.bak.N`, then to defaults, and reports the problem through `/healthz` and the
  admin banner. Saves are atomic with fsync and rotate three backups.
- Secrets (`SECRET_FIELDS` in `config.py`) are masked in every API response; a PUT that
  sends the mask keeps the stored value and an empty string clears it. `PUT /api/config`
  rejects unknown keys and honors `If-Match` against the config version (409 on mismatch).
- `HANGAR_ADMIN_PASSWORD` gates `/admin`, `PUT /api/config`, `POST /api/test-source`, and
  `POST /api/display/*` with HTTP Basic auth. Read endpoints the display uses stay open.
  `admin.html` is only reachable through `/admin` (`AppStatic` blocks the direct path).
- The display never calls `location.reload()` while the backend is unreachable (Chromium's
  error page has no script to recover); use `safeReload()`. Boot retries with backoff,
  `nextView()` always schedules the next view even if a view throws, an invalid timezone
  falls back to UTC, and a watchdog reloads only when the backend answers.
- SSE `connected` carries `boot_id`, config `version`, and the current `display` state. The
  display reloads on a changed boot id or version and applies blackout state from the
  event. Subscribe to events before map setup so this works even while tiles load.
- HTML responses are `Cache-Control: no-cache`, hashed `/assets/` are immutable
  (`SecurityHeaders` in `main.py`), and the kiosk holding page navigates to the display
  with a unique `?launch=` query, so a rebuilt bundle replaces Chromium's cached page. When
  checking a deploy, compare `document.scripts` in the live page with the script the
  server serves at `/`.
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
- The project is licensed GPL-2.0 (see LICENSE). Anything vendored must be GPL-2.0
  compatible and keep its own copyright and license notices next to it, as
  `lib/vendor/tar1090/` does.
- Brand name is "The Pilot Channel". Avoid em dashes in prose.
- Commit only when asked. Do not put history, measurements, attribution, or change notes
  into source files or this document; commit messages carry them.

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

  `tests/support.py` prepares the environment (throwaway config, stub static dir, temp tile
  cache, admin password) before `app.main` is imported; `tests/conftest.py` provides the
  `client` fixture with every upstream stubbed, including tile warm-up. `test_api.py`
  covers the API surface; `test_behaviour.py` covers speed, reliability, and decoding
  properties. Name each test for the failure it prevents. The manager is a module
  singleton whose loops keep refreshing views polled by earlier tests, so apply config
  before installing a failing stub. The SSE endpoint is an endless stream and hangs
  Starlette's TestClient; exercise it with `curl -N`.
- Frontend module tests: `frontend/tests/*.test.html` run headlessly with
  `frontend/tests/run.sh` (needs a chromium binary, no npm packages) and cover the aircraft
  store, icon assignment, station selection, wind barbs, the admin form, and per-frame
  cost bounds. `harness.js` prints the PASS/FAIL lines the runner parses. Run it after any
  change under `frontend/src/lib`.
- Try a build beside production: `docker build -t hangar-display:test .` then
  `docker run -d -p 8001:8000 -v "$PWD/data:/data" hangar-display:test`.
- Frontend checks: `node --check` on each module. Headless Chromium
  (`chromium --headless=new --disable-gpu --dump-dom URL`) works for the admin page and for
  module tests loaded with `--allow-file-access-from-files`. Headless Chromium has no WebGL,
  so the display's map never finishes loading there, and the display page holds its event
  stream open, so `--dump-dom` does not return. Drive it over the DevTools protocol
  (`--remote-debugging-port`) when you need page state.
- Verify the display on the real kiosk: screenshots with
  `WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 grim -t jpeg -q 88 -s 0.47 out.jpg`
  (`-s` scales the 4K panel), per-process CPU from `top`, and GPU memory from `CmaFree` in
  `/proc/meminfo`. Debian's grim writes only png/ppm; the bootstrap installs
  `deploy/grim-web-wrapper.sh` as `/usr/local/bin/grim`, which adds jpeg, webp, avif, gif,
  tiff, bmp, and heic through ImageMagick, either with `-t webp` or inferred from the file
  name; png/ppm and unknown options pass through to the real grim unchanged.
  `sudo rm /usr/local/bin/grim` reverts. The kiosk Chromium exposes the DevTools protocol
  on `127.0.0.1:9222` (localhost only): `curl -s 127.0.0.1:9222/json` lists the page target,
  and `window.__tpcMap` is the MapLibre instance. Measure any performance change there
  before calling it an improvement; the regional radar view is the heaviest view.
- Dev servers: backend `uvicorn app.main:app --reload --port 8000` with `HANGAR_CONFIG` and
  `HANGAR_STATIC` set; frontend `npm run dev` proxies `/api` to port 8000 and serves the
  admin page at `/admin.html`.
- Dependencies are pinned: `backend/requirements.txt` exactly, `frontend/package-lock.json`
  committed and installed with `npm ci`. Run `pip-audit` inside the image when bumping.

## Documentation screenshots

`docs/display-*.jpg` are captured from the live kiosk with grim at 1920 wide (JPEG, quality
88) once each view is fully rendered: the KSEA local view a few seconds in, the regional view
with the radar loop and barbs loaded, and the satellite view once its loop is animating.
`docs/admin.png` is headless Chromium against the running backend at 1120 px wide. Time the
captures to a view start through the DevTools port, and check that the frame shows real
data (aircraft listed, stations listed, frames loaded) before keeping it.

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
- Network: Wi-Fi profiles are NetworkManager keyfiles in
  `/etc/NetworkManager/system-connections` with read-only copies of the same UUID in
  `/usr/lib/NetworkManager/system-connections`, never netplan: Raspberry Pi's
  NetworkManager rewrites `/etc/netplan` at every start without flushing, so a power cut
  during boot can empty it (raspberrypi/trixie-feedback#99). `deploy/harden-wifi.sh`
  converts the cloud-init netplan profiles from Imager, disables cloud-init's network
  config, and verifies with `--check`; `--test-fallback` connects from the read-only
  copies. Keep `/etc/netplan` empty. Ethernet needs no saved profile.

## HTTP API

- `GET /healthz` liveness, `boot_id`, source status, config load state.
- `GET /api/config` config with secrets masked plus `version` (also the `ETag`).
  `PUT /api/config` validates and writes; optional `If-Match`; 422 with `[{loc, msg}]`.
- `GET /api/views` ordered view descriptors the display cycles through.
- `GET /api/traffic?view=<id>` snapshot with `aircraft`, `ts`, `age_s`, `stale`,
  `healthy`, `error`, and `pending` while a newly active view has no fresh data; 404 for
  an unknown view.
- `GET /api/weather?ids=<csv>`, `GET /api/weather/bbox?min_lat=&min_lon=&max_lat=&max_lon=`
  (at most 20 by 30 degrees), `GET /api/weather/area?lat=&lon=&radius_nm=`; the last two
  return `{stations, age_s, stale, error}`.
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
