"""DataManager: owns sources, caching, rate limiting, weather refresh, and
config hot-reload. One upstream poll is shared across all clients/views.

Traffic freshness: views that a client polled recently are refreshed by a
background loop at a fixed cadence, independent of client polling, so the
snapshot age never aliases with the 1 Hz browser poll. Snapshots carry their
age and a stale flag so the display can show when data has stopped flowing.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Set

import httpx

from . import cameras
from .access import CONFIG_API_KEY, import_legacy
from .config import (
    CONFIG_PATH,
    Config,
    DataSource,
    config_version,
    live_notams_problem,
    load_config,
    notam_source_label,
    read_raw,
    save_config,
)
from .llm import ChatModel, ModelError, api_key
from .notam_translate import PROMPT_VERSION, RESPONSE_SCHEMA, checked_rewrite
from .notams import (
    BASELINE_S,
    DELTA_OVERLAP_S,
    DELTA_REACH_S,
    PULL_INTERVAL_S,
    CredentialsRejected,
    NmsSource,
    RateLimited,
    SampleSource,
    TokenRefused,
    affects,
    UNSETTLED,
    compare_sets,
    is_foreign,
    repeats,
    settle,
    unsettled_ids,
    normalize,
    query_forms,
    select,
)
from .nws_gateway import SOURCE as GATEWAY_SOURCE, GatewaySource
from .satellite import SatelliteSource
from .sources.aggregator import AggregatorSource
from .radar import RadarTiles
from .tiles import TileCache
from .sources.local import LocalReadsbSource
from .store import NMS, STORE
from .views import build_views
from .weather import MetarSource

log = logging.getLogger("hangar")

TRAFFIC_REFRESH_S = 1.0        # target cadence for an actively viewed view
ACTIVE_VIEW_WINDOW_S = 5.0     # keep refreshing a view for this long after its last poll
TRAFFIC_STALE_S = 10.0         # snapshot older than this is flagged stale
LEFTOVER_S = 3.0               # a snapshot older than this, for a view that just became active, is not data
FIRST_POLL_WAIT_S = 1.5        # the first poll of a view may wait this long for its refresh
LOCAL_RETRY_S = 60.0           # circuit breaker: after a local-source failure, skip it this long
AGGREGATOR_MIN_INTERVAL = 1.0  # published public rate limit: 1 req/sec
SAT_CACHE_TTL_S = 240.0
AREA_CACHE_MAX = 64
SAT_CACHE_MAX = 16
MAX_SUBSCRIBERS = 32
WEATHER_RETRY_S = 60.0         # after a failed METAR refresh, try again this soon
WEATHER_COLD_WAIT_S = 6.0      # a station query with nothing cached may wait this long for upstream
WEATHER_STORE = os.path.join(os.path.dirname(CONFIG_PATH) or ".", "weather-cache.json")
NOTAM_RETRY_S = 60.0           # after a failed NOTAM loop pass, look again this soon
NOTAM_IDLE_S = 30.0            # how often the NOTAM loop looks for a pull that became due
NOTAM_RETRY_MAX_S = 1800.0     # a location that keeps failing is asked again at most this far apart
NOTAM_SAVE_S = 900.0           # the NOTAM cache reaches the disk at least this often while pulls succeed
NOTAM_STORE = os.path.join(os.path.dirname(CONFIG_PATH) or ".", "notam-cache.json")


class TooManySubscribers(RuntimeError):
    pass


class UnknownView(ValueError):
    pass


def _age(ts: Optional[float]) -> Optional[float]:
    return round(time.time() - ts, 1) if ts else None


class DataManager:
    def __init__(self):
        loaded = load_config()
        self.cfg: Config = loaded.config
        self.config_source = loaded.source
        self.config_error = loaded.error
        self.config_version = config_version()
        self.boot_id = uuid.uuid4().hex[:12]
        self.started_at = time.time()
        # Values an earlier version read from the environment or kept in
        # config.yaml move into the secrets store once.
        self.imported = import_legacy(STORE, os.environ, read_raw())
        if CONFIG_API_KEY in self.imported and loaded.source == "file":
            self.config_version = save_config(self.cfg)   # the file no longer carries the key

        self.client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"User-Agent": "the-pilot-channel/0.2 (+https://github.com/juchong/ThePilotChannel)"},
        )
        self.metar = MetarSource(self.client)
        self.gateway = GatewaySource(self.client)
        self.satellite = SatelliteSource(self.client)
        self.tiles = TileCache(self.client)
        self.radar = RadarTiles(self.client)
        self.nms = NmsSource(self.client, environment=self.cfg.notams.environment)
        self.notam_sample = SampleSource()
        self.llm = ChatModel(self.client)
        self._warm_task: Optional[asyncio.Task] = None

        self._traffic_cache: Dict[str, Dict] = {}   # view_id -> {ts, aircraft, source}
        self._traffic_health: Dict[str, Dict] = {}  # view_id -> {healthy, error}
        self._polled: Dict[str, float] = {}         # view_id -> monotonic time of last client poll
        self._refresh_tasks: Dict[str, asyncio.Task] = {}
        self._weather_cache: Dict[str, Dict] = {}
        self._weather_ts: Optional[float] = None
        self._weather_error: Optional[str] = None
        self._gateway_error: Optional[str] = None
        self._area_cache: Dict[str, Dict] = {}        # key -> {ts (wall), data, error}
        self._area_refreshing: Set[str] = set()
        self._sat_cache: Dict[str, Dict] = {}
        self._store_dirty = False                       # a save of the weather store is pending
        self._store_task: Optional[asyncio.Task] = None
        self._store_failing = False
        self._load_weather_store()
        # ident -> {ts (wall time of the last whole fetch), synced (wall time the set
        # is complete through), source, environment, records, error, unknown,
        # unknown_forms, requested, retry_at, failures, diff}
        self._notam_cache: Dict[str, Dict] = {}
        self._notam_ts: Optional[float] = None
        self._notam_error: Optional[str] = None
        self._notam_job: Optional[Dict] = None        # a whole fetch in progress: one pull per spelling
        self._delta_tried = 0.0                       # wall time of the last pull of changes
        self._last_kind: Optional[str] = None
        self._notam_saved = 0.0
        self._nms_hold = 0.0                          # wall time until which the FAA is not asked: its keys were refused
        self._now = time.time                         # the NOTAM scheduler's clock
        self._rewrites: Dict[str, Dict] = {}          # content key -> {"text"} or {"refused": [...]}
        self._rewrite_task: Optional[asyncio.Task] = None
        self._rewrite_error: Optional[str] = None
        self._notam_store_dirty = False
        self._notam_store_task: Optional[asyncio.Task] = None
        self._notam_store_failing = False
        self._load_notam_store()

        self._agg_lock = asyncio.Lock()
        self._agg_last = 0.0
        self._local_state: Optional[str] = None      # None | "ok" | "down"
        self._local_failed_at = 0.0
        self._source_status: Dict[str, Any] = {"active": None, "healthy": True, "last_error": None}

        self._subscribers: List[asyncio.Queue] = []
        self._tasks: Set[asyncio.Task] = set()
        # Remote display control: a blackout hides the picture (screen goes black)
        # until restored or until a deadline. Driven by automations such as
        # Home Assistant through /api/display/*.
        self._blackout: Dict[str, Any] = {"active": False, "until": None, "reason": None}
        self._blackout_task: Optional[asyncio.Task] = None
        # Remote navigation: the latest request to move the cycle, numbered so the
        # display applies each one once, and the view the display last reported.
        self._nav: Dict[str, Any] = {"seq": 0, "action": None, "view": None, "ts": None}
        self._showing: Dict[str, Any] = {"view": None, "index": None, "since": None}
        self._weather_wake = asyncio.Event()
        self._notam_wake = asyncio.Event()
        self._loops: List[asyncio.Task] = []

    # ---- lifecycle -------------------------------------------------------
    async def start(self):
        # Loop-bound primitives are created on the running loop so start()
        # works on any event loop, not only the one active at import.
        self._weather_wake = asyncio.Event()
        self._notam_wake = asyncio.Event()
        self._agg_lock = asyncio.Lock()
        self._loop = asyncio.get_running_loop()
        self._loops = [
            asyncio.create_task(self._weather_loop(), name="weather-loop"),
            asyncio.create_task(self._traffic_loop(), name="traffic-loop"),
            asyncio.create_task(self._notam_loop(), name="notam-loop"),
            asyncio.create_task(cameras.sync_loop(), name="camera-sync-loop"),
        ]
        self.warm_tiles()

    def warm_tiles(self, delay_s: float = 0.0) -> None:
        """(Re)start the basemap tile warm-up for the configured views."""
        if self._warm_task and not self._warm_task.done():
            self._warm_task.cancel()

        async def run():
            if delay_s:
                await asyncio.sleep(delay_s)
            try:
                await self.tiles.warm(self.views())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("tile warm-up failed: %s", exc)

        self._warm_task = self._spawn(run(), name="tile-warm")

    async def stop(self):
        for t in self._loops + list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._loops, *self._tasks, return_exceptions=True)
        await self.client.aclose()
        await asyncio.to_thread(STORE.flush)

    def secret_changed(self, name: str) -> None:
        """A stored credential was set or cleared (called from a worker thread)."""
        if name == NMS:
            self.nms.reset_token()
            self._nms_hold = 0.0
            loop = getattr(self, "_loop", None)
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._notam_wake.set)

    def _spawn(self, coro, name: str) -> asyncio.Task:
        """create_task with a strong reference kept until the task finishes."""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---- config ----------------------------------------------------------
    def get_config(self) -> Config:
        return self.cfg

    async def update_config(self, cfg: Config) -> str:
        self.config_version = await asyncio.to_thread(save_config, cfg)
        self.cfg = cfg
        self.config_source = "file"
        self.config_error = None
        self._traffic_cache.clear()
        self._traffic_health.clear()
        self._area_cache.clear()
        self._local_state = None  # let a changed local_url be tried right away
        self._showing = {"view": None, "index": None, "since": None}  # the display reloads and reports again
        self.nms.use(cfg.notams.environment)
        # A saved change has the NOTAM loop look again at its next turn.
        self._delta_tried = 0.0
        for entry in self._notam_cache.values():
            entry["retry_at"] = 0.0
        self._weather_wake.set()  # refresh METARs for any new airports now
        self._notam_wake.set()    # fetch NOTAMs for any new locations or a changed source at the next turn
        self._kick_rewrites()     # a changed model is asked about the NOTAMs already held
        self.warm_tiles(delay_s=5.0)  # cache basemap tiles for any new or moved views
        self._broadcast({"event": "config_changed", "version": self.config_version})
        return self.config_version

    def views(self) -> List[Dict]:
        return build_views(self.cfg)

    # ---- sources ---------------------------------------------------------
    def _local_source(self, url: Optional[str] = None) -> LocalReadsbSource:
        return LocalReadsbSource(self.client, url or self.cfg.data_source.local_url)

    def _aggregator_source(self, ds: Optional[DataSource] = None) -> AggregatorSource:
        ds = ds or self.cfg.data_source
        return AggregatorSource(self.client, ds.aggregator, STORE.credential("aggregator_api_key"))

    async def _rate_limited_aggregator_fetch(self, lat, lon, nm, ds: Optional[DataSource] = None) -> List[Dict]:
        async with self._agg_lock:
            wait = AGGREGATOR_MIN_INTERVAL - (time.monotonic() - self._agg_last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._agg_last = time.monotonic()
        return await self._aggregator_source(ds).fetch(lat, lon, nm)

    # Circuit breaker for the local receiver: a failure is logged once per
    # state change and the receiver is skipped for LOCAL_RETRY_S.
    def _local_available(self) -> bool:
        if self._local_state != "down":
            return True
        return (time.monotonic() - self._local_failed_at) >= LOCAL_RETRY_S

    def _local_ok(self):
        if self._local_state == "down":
            log.info("local source recovered: %s", self.cfg.data_source.local_url)
        self._local_state = "ok"

    def _local_down(self, exc: Exception, fallback: bool):
        self._local_failed_at = time.monotonic()
        if self._local_state != "down":
            what = "falling back to the aggregator" if fallback else "no traffic"
            log.warning(
                "local source unavailable (%s), %s; retrying every %.0fs: %s",
                self.cfg.data_source.local_url, what, LOCAL_RETRY_S, exc,
            )
        self._local_state = "down"

    async def _fetch_traffic(self, lat, lon, nm) -> List[Dict]:
        ds = self.cfg.data_source
        if ds.mode == "local":
            try:
                ac = await self._local_source().fetch(lat, lon, nm)
            except Exception as exc:  # noqa: BLE001
                self._local_down(exc, fallback=False)
                raise
            self._local_ok()
            self._source_status.update(active="local", healthy=True, last_error=None)
            return ac
        if ds.mode == "auto" and self._local_available():
            try:
                ac = await self._local_source().fetch(lat, lon, nm)
                self._local_ok()
                self._source_status.update(active="local", healthy=True, last_error=None)
                return ac
            except Exception as exc:  # noqa: BLE001
                self._local_down(exc, fallback=True)
        ac = await self._rate_limited_aggregator_fetch(lat, lon, nm)
        self._source_status.update(active=f"aggregator:{ds.aggregator}", healthy=True, last_error=None)
        return ac

    def _drop_stale_positions(self, aircraft: List[Dict]) -> List[Dict]:
        """Aggregators keep reporting an aircraft's last position for a while
        after it stops transmitting; drop anything whose position is older
        than the configured drop timeout so it does not freeze on screen."""
        limit = self.cfg.data_source.drop_timeout_s
        out = []
        for ac in aircraft:
            seen = ac.get("seen_pos")
            if isinstance(seen, (int, float)) and seen > limit:
                continue
            out.append(ac)
        return out

    # ---- traffic ---------------------------------------------------------
    def _fresh(self, cached: Optional[Dict]) -> bool:
        return cached is not None and (time.monotonic() - cached["ts"]) <= LEFTOVER_S

    async def get_traffic(self, view_id: str) -> Optional[Dict]:
        """Records that the view is being watched (so the background loop keeps
        it fresh) and returns the current snapshot with its age. Polls of an
        active view never wait. When a view has just become active there is no
        fresh snapshot, only a leftover from its previous visit (up to a whole
        cycle old); handing that out would place every aircraft where it was
        minutes ago. So the refresh is kicked immediately and the poll waits
        for it, bounded by FIRST_POLL_WAIT_S; if it still is not fresh the
        response is `pending` rather than old data, unless refreshes are
        failing, in which case the last known aircraft are shown as stale."""
        view = next((v for v in self.views() if v["id"] == view_id), None)
        if view is None:
            return None
        self._polled[view_id] = time.monotonic()
        cached = self._traffic_cache.get(view_id)
        if not self._fresh(cached):
            task, created = self._kick_refresh(view_id, view)
            # Only the poll that started the refresh waits for it. Later polls
            # that find the same refresh still in flight (a hung upstream) must
            # not queue up behind it: they answer at once with what there is.
            if created:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=FIRST_POLL_WAIT_S)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except Exception:  # noqa: BLE001  (the refresh records its own failure)
                    pass
            cached = self._traffic_cache.get(view_id)
        if not self._fresh(cached) and cached is not None:
            health = self._traffic_health.get(view_id)
            if health is None or health["healthy"]:
                return self._snapshot(view_id, None, leftover_age=time.monotonic() - cached["ts"])
        return self._snapshot(view_id, cached)

    def _kick_refresh(self, view_id: str, view: Dict):
        """-> (task, created): the in-flight refresh for the view, or a new one."""
        task = self._refresh_tasks.get(view_id)
        if task is not None and not task.done():
            return task, False
        task = self._spawn(self._refresh_traffic(view_id, view), name=f"traffic:{view_id}")
        self._refresh_tasks[view_id] = task
        return task, True

    async def _traffic_loop(self):
        """Keep every recently polled traffic view refreshed at TRAFFIC_REFRESH_S."""
        while True:
            try:
                now = time.monotonic()
                views = {v["id"]: v for v in self.views() if v["type"] == "local"}
                for view_id, last in list(self._polled.items()):
                    if now - last > ACTIVE_VIEW_WINDOW_S or view_id not in views:
                        if now - last > 3600:
                            self._polled.pop(view_id, None)
                        continue
                    cached = self._traffic_cache.get(view_id)
                    if cached and now - cached["ts"] < TRAFFIC_REFRESH_S * 0.85:
                        continue
                    self._kick_refresh(view_id, views[view_id])
            except Exception as exc:  # noqa: BLE001
                log.error("traffic loop error: %s", exc)
            # A fine tick keeps the refresh period just under the display's 1 s
            # poll, so the display never sees the same snapshot twice in a row.
            await asyncio.sleep(TRAFFIC_REFRESH_S / 10)

    def _snapshot(self, view_id: str, cached: Optional[Dict], leftover_age: Optional[float] = None) -> Dict:
        health = self._traffic_health.get(view_id, {"healthy": True, "error": None})
        if cached is None:
            return {
                "view": view_id, "aircraft": [], "count": 0,
                "source": self._source_status["active"],
                "ts": None,
                "age_s": round(leftover_age, 1) if leftover_age is not None else None,
                "stale": leftover_age is not None,
                "pending": True,
                "healthy": health["healthy"], "error": health["error"],
            }
        age = time.monotonic() - cached["ts"]
        return {
            "view": view_id,
            "aircraft": cached["aircraft"],
            "count": len(cached["aircraft"]),
            "source": cached["source"],
            "ts": cached["wall_ts"],
            "age_s": round(age, 1),
            "stale": age > TRAFFIC_STALE_S,
            "healthy": health["healthy"],
            "error": health["error"],
        }

    async def _refresh_traffic(self, view_id: str, view: Dict) -> None:
        try:
            aircraft = await self._fetch_traffic(
                view["center_lat"], view["center_lon"], view.get("fetch_radius_nm", view["radius_nm"])
            )
            aircraft = self._drop_stale_positions(aircraft)
            self._traffic_cache[view_id] = {
                "ts": time.monotonic(),
                "wall_ts": time.time(),
                "aircraft": aircraft,
                "source": self._source_status["active"],
            }
            prev = self._traffic_health.get(view_id)
            if prev and not prev["healthy"]:
                log.info("traffic for %s recovered", view_id)
            self._traffic_health[view_id] = {"healthy": True, "error": None}
        except Exception as exc:  # noqa: BLE001
            err = str(exc) or exc.__class__.__name__
            self._source_status.update(healthy=False, last_error=err)
            prev = self._traffic_health.get(view_id)
            if not prev or prev["healthy"] or prev["error"] != err:
                log.error("traffic fetch failed for %s: %s", view_id, err)
            self._traffic_health[view_id] = {"healthy": False, "error": err}

    # ---- weather ---------------------------------------------------------
    async def _weather_loop(self):
        while True:
            try:
                await self._refresh_weather()
                if self._weather_error:
                    log.info("weather refresh recovered")
                self._weather_error = None
                wait = max(60, self.cfg.weather.refresh_s)
            except Exception as exc:  # noqa: BLE001
                err = str(exc) or exc.__class__.__name__
                if err != self._weather_error:
                    log.error("weather refresh failed: %s", err)
                self._weather_error = err
                wait = WEATHER_RETRY_S  # keep the cache fresh-ish through a flaky upstream or DNS
            self._weather_wake.clear()
            try:
                await asyncio.wait_for(self._weather_wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001  (never let the loop die)
                log.error("weather loop wait failed: %s", exc)
                await asyncio.sleep(wait)

    async def _refresh_weather(self):
        airports = {ap.icao.upper(): ap for ap in self.cfg.airports if ap.enabled}
        if not airports:
            self._weather_cache = {}
            return
        stale = self.cfg.weather.stale_after_s
        # merge so a station missing from one response keeps its last report
        # (its age keeps growing and it is flagged stale by get_weather)
        merged = {k: v for k, v in self._weather_cache.items() if k in airports}
        failure = None
        try:
            served = await self.metar.fetch(list(airports), stale)
            merged.update(served)
            lacking = [k for k in airports if k not in served]
        except Exception as exc:  # noqa: BLE001
            # the stations the gateway serves do not depend on aviationweather.gov
            failure = exc
            lacking = [k for k, m in merged.items() if m.get("source") == GATEWAY_SOURCE]
        taken = await self._refresh_from_gateway([airports[k] for k in lacking], merged, stale)
        self._weather_cache = merged
        if failure is None:
            self._weather_ts = time.time()
        if failure is None or taken:
            self._save_weather_store()
            self._broadcast({"event": "weather_updated"})
        if failure is not None:
            raise failure

    async def _refresh_from_gateway(self, airports: List, merged: Dict[str, Dict], stale_after_s: int) -> bool:
        """Read the airports aviationweather.gov did not answer for from the NWS
        gateway into `merged`; True when a report was taken. A gateway report never
        replaces a newer cached one. A failing gateway is logged once and leaves
        the cached reports in place."""
        if not airports:
            self._gateway_error = None
            return False
        found, error = await self.gateway.fetch(airports, stale_after_s)
        if error != self._gateway_error:
            if error:
                log.error("gateway weather fetch failed: %s", error)
            else:
                log.info("gateway weather fetch recovered")
            self._gateway_error = error
        taken = False
        for icao, m in found.items():
            cached = merged.get(icao)
            if cached is not None and (cached.get("obs_time") or 0) > (m.get("obs_time") or 0):
                continue
            taken = taken or cached is None or cached.get("raw") != m.get("raw")
            merged[icao] = m
        return taken

    # The last good weather is kept on disk so a restart during an upstream or
    # DNS outage (the two happen together when a router's resolver fails) still
    # has something to show, flagged stale by age.
    def _load_weather_store(self) -> None:
        try:
            with open(WEATHER_STORE) as fh:
                store = json.load(fh)
        except (OSError, ValueError):
            return
        self._weather_cache = store.get("metars") or {}
        self._weather_ts = store.get("metars_ts")
        for key, entry in (store.get("areas") or {}).items():
            if isinstance(entry, dict) and "data" in entry and "ts" in entry:
                self._area_cache[key] = {"ts": entry["ts"], "data": entry["data"], "error": None}
        if self._weather_cache or self._area_cache:
            log.info("weather cache restored from %s (%d METARs, %d areas)", WEATHER_STORE, len(self._weather_cache), len(self._area_cache))

    def _save_weather_store(self) -> None:
        """Queue a save of the weather caches. A single writer task runs until no
        save is pending, so overlapping saves never share the temp file and the
        file always ends with the latest state."""
        self._store_dirty = True
        if self._store_task is None or self._store_task.done():
            self._store_task = self._spawn(self._write_weather_store(), name="weather-store")

    async def _write_weather_store(self) -> None:
        def write(store):
            tmp = WEATHER_STORE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(store, fh, separators=(",", ":"))
            os.replace(tmp, WEATHER_STORE)

        while self._store_dirty:
            self._store_dirty = False
            store = {
                "metars": dict(self._weather_cache),
                "metars_ts": self._weather_ts,
                "areas": {k: {"ts": v["ts"], "data": v["data"]} for k, v in self._area_cache.items()},
            }
            try:
                await asyncio.to_thread(write, store)
            except OSError as exc:
                if not self._store_failing:
                    log.error("weather cache not saved to %s: %s", WEATHER_STORE, exc)
                self._store_failing = True
            else:
                if self._store_failing:
                    log.info("weather cache saved again")
                self._store_failing = False

    def _with_age(self, m: Dict) -> Dict:
        obs = m.get("obs_time")
        if isinstance(obs, (int, float)):
            age = int(time.time() - obs)
            m = dict(m, age_s=age, stale=age > self.cfg.weather.stale_after_s)
        return m

    def get_weather(self, icaos: Optional[List[str]] = None) -> Dict[str, Dict]:
        if not icaos:
            return {k: self._with_age(v) for k, v in self._weather_cache.items()}
        out = {}
        for i in icaos:
            k = i.upper()
            if k in self._weather_cache:
                out[k] = self._with_age(self._weather_cache[k])
        return out

    @staticmethod
    def _cache_put(cache: Dict[str, Dict], key: str, data, max_entries: int) -> None:
        cache[key] = {"ts": time.time(), "data": data, "error": None}
        while len(cache) > max_entries:
            oldest = min(cache, key=lambda k: cache[k]["ts"])
            cache.pop(oldest, None)

    def _with_gateway_stations(self, key: str, result: Dict, inside) -> Dict:
        """A station-set response plus the configured airports inside its area whose
        report came from the NWS gateway: the set is aviationweather.gov's answer,
        which has no such station. A set that was never fetched is left as it is."""
        if self._area_cache.get(key, {}).get("ts", 0) <= 0:
            return result
        listed = {s.get("icao") for s in result["stations"]}
        extra = [
            self._with_age(m)
            for icao, m in self._weather_cache.items()
            if m.get("source") == GATEWAY_SOURCE and icao not in listed and m.get("lat") is not None and inside(m["lat"], m["lon"])
        ]
        return dict(result, stations=result["stations"] + extra) if extra else result

    def _area_result(self, key: str) -> Dict:
        """Response for a cached station set: the stations plus how old the set
        is, whether it counts as stale (two refresh intervals), and the last
        fetch error if the refresh is failing."""
        cached = self._area_cache[key]
        age = max(0.0, time.time() - cached["ts"])
        return {
            "stations": [self._with_age(s) for s in cached["data"]],
            "age_s": round(age),
            "stale": age > 2 * max(60, self.cfg.weather.refresh_s),
            "error": cached.get("error"),
        }

    async def _area_fetch(self, key: str, fetch, what: str) -> bool:
        """Run one upstream fetch for a station set and record the outcome."""
        if key in self._area_refreshing:
            return False
        self._area_refreshing.add(key)
        try:
            stations = await fetch()
            out = [s for s in stations if s.get("lat") is not None]
            self._cache_put(self._area_cache, key, out, AREA_CACHE_MAX)
            self._save_weather_store()
            return True
        except Exception as exc:  # noqa: BLE001
            err = str(exc) or exc.__class__.__name__
            cached = self._area_cache.get(key)
            if cached is None or cached.get("error") != err:
                log.error("%s weather fetch failed: %s", what, err)
            if cached is not None:
                cached["error"] = err
            else:
                self._area_cache[key] = {"ts": 0.0, "data": [], "error": err}
            return False
        finally:
            self._area_refreshing.discard(key)

    async def _area_get(self, key: str, fetch, what: str) -> Dict:
        """Stale-while-revalidate: a cached station set is returned at once and
        refreshed in the background when it has expired; only a set that was
        never fetched waits (bounded) for upstream. The regional view therefore
        never blocks on aviationweather.gov or on DNS."""
        cached = self._area_cache.get(key)
        ttl = max(60, self.cfg.weather.refresh_s)
        if cached is not None and cached["ts"] > 0:
            if time.time() - cached["ts"] > ttl and key not in self._area_refreshing:
                self._spawn(self._area_fetch(key, fetch, what), name=f"weather:{key}")
            return self._area_result(key)
        if key not in self._area_refreshing:
            task = self._spawn(self._area_fetch(key, fetch, what), name=f"weather:{key}")
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=WEATHER_COLD_WAIT_S)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if key in self._area_cache:
            return self._area_result(key)
        return {"stations": [], "age_s": None, "stale": False, "error": "no data yet"}

    async def get_area_weather(self, lat: float, lon: float, radius_nm: float) -> Dict:
        """All METAR stations within radius_nm of (lat, lon), cached by area."""
        import math

        from .geo import haversine_nm

        key = f"{round(lat, 2)}:{round(lon, 2)}:{round(radius_nm)}"
        dlat = radius_nm / 60.0
        dlon = radius_nm / (60.0 * max(0.1, math.cos(math.radians(lat))))

        async def fetch():
            stations = await self.metar.fetch_bbox(
                lat - dlat, lon - dlon, lat + dlat, lon + dlon, self.cfg.weather.stale_after_s
            )
            return [s for s in stations if s.get("lat") is not None and haversine_nm(s["lat"], s["lon"], lat, lon) <= radius_nm]

        return self._with_gateway_stations(
            key, await self._area_get(key, fetch, "area"), lambda la, lo: haversine_nm(la, lo, lat, lon) <= radius_nm
        )

    async def get_bbox_weather(self, min_lat, min_lon, max_lat, max_lon) -> Dict:
        """All METAR stations within an explicit bounding box (the map's visible
        rectangle), cached by box and refreshed in the background."""
        key = f"bbox:{round(min_lat, 2)}:{round(min_lon, 2)}:{round(max_lat, 2)}:{round(max_lon, 2)}"

        async def fetch():
            return await self.metar.fetch_bbox(min_lat, min_lon, max_lat, max_lon, self.cfg.weather.stale_after_s)

        return self._with_gateway_stations(
            key, await self._area_get(key, fetch, "bbox"), lambda la, lo: min_lat <= la <= max_lat and min_lon <= lo <= max_lon
        )

    # ---- NOTAMs ----------------------------------------------------------
    # Kept per monitored location by a background loop and on disk, so the NOTAM
    # views never wait on the FAA and survive an outage or a restart with the
    # data's age shown. The loop is the only caller of the FAA's data endpoint:
    # one pull at a time, at the pace notams.PullGate allows. A location's whole
    # set is fetched by location; between those fetches one pull of everything
    # changed since the last one keeps every location current. Which NOTAMs are
    # in effect or upcoming is decided when a view asks, from the cached times.
    def _notam_source(self):
        return self.notam_sample if self.cfg.notams.source == "sample" else self.nms

    def notams_blocked(self) -> Optional[str]:
        """Why the live NOTAM views are not shown although they are enabled."""
        problem = live_notams_problem(self.cfg)
        return problem["msg"] if problem else None

    def _notam_idents(self) -> List[str]:
        cfg = self.cfg.notams
        if not cfg.enabled or live_notams_problem(self.cfg):
            return []
        return [loc.ident for loc in cfg.locations if loc.enabled]

    def _notam_key(self) -> tuple:
        """The source and environment a cache entry must come from to be used."""
        cfg = self.cfg.notams
        return (cfg.source, cfg.environment if cfg.source == "faa" else None)

    @staticmethod
    def _new_notam_entry(source: str, environment: Optional[str]) -> Dict:
        return {
            "ts": 0.0, "synced": 0.0, "source": source, "environment": environment, "records": [],
            "error": None, "unknown": False, "unknown_forms": [], "requested": False,
            "retry_at": 0.0, "failures": 0, "diff": None, "unseen": None,
        }

    def _notam_entry(self, ident: str, create: bool = True) -> Optional[Dict]:
        """The location's cache entry, if it comes from the configured source and
        environment (a fresh one otherwise, when `create`)."""
        entry = self._notam_cache.get(ident)
        if entry is not None and (entry["source"], entry["environment"]) == self._notam_key():
            return entry
        if not create:
            return None
        entry = self._notam_cache[ident] = self._new_notam_entry(*self._notam_key())
        return entry

    def _prune_notam_cache(self) -> None:
        idents, key = set(self._notam_idents()), self._notam_key()
        self._notam_cache = {
            k: v for k, v in self._notam_cache.items() if k in idents and (v["source"], v["environment"]) == key
        }
        if self._notam_job and self._notam_job["ident"] not in idents:
            self._notam_job = None

    def _notam_state(self, error: Optional[str]) -> None:
        if error != self._notam_error:
            if error:
                log.error("NOTAM refresh failed: %s", error)
            else:
                log.info("NOTAM refresh recovered")
        self._notam_error = error

    def _notams_changed(self, save: bool = True) -> None:
        self._notam_ts = self._now()
        if save or self._now() - self._notam_saved >= NOTAM_SAVE_S:
            self._save_notam_store()
        if save:
            self._broadcast({"event": "notams_updated"})
        self._kick_rewrites()

    async def _notam_loop(self):
        while True:
            self._notam_wake.clear()
            wait = NOTAM_IDLE_S
            try:
                self._prune_notam_cache()
                if not self._notam_idents():
                    self._notam_state(None)
                elif self.cfg.notams.source == "sample":
                    await self._refresh_sample_notams()
                    wait = max(PULL_INTERVAL_S, self.cfg.notams.refresh_s)
                else:
                    wait = await self._nms_step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001  (never let the loop die)
                log.error("NOTAM loop error: %s", exc)
                wait = NOTAM_RETRY_S
            if wait > 0:
                try:
                    await asyncio.wait_for(self._notam_wake.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass

    async def _fetch_notams(self, source, ident: str) -> List[Dict]:
        """The location's NOTAMs as display records: what the source returns for
        it, kept only where the affected location is this one."""
        records = [normalize(f) for f in await source.fetch(ident)]
        return [r for r in records if r and affects(r, ident)]

    async def _refresh_sample_notams(self) -> None:
        now = self._now()
        for ident in self._notam_idents():
            records = await self._fetch_notams(self.notam_sample, ident)
            self._notam_entry(ident).update(ts=now, synced=now, records=records, error=None)
        self._notam_state(None)
        self._notam_ts = now
        self._broadcast({"event": "notams_updated"})
        self._kick_rewrites()

    # -- the FAA: which pull is due, and making it
    @staticmethod
    def _holding(until: float, now: float) -> bool:
        """Whether a wait that was set to end at `until` still runs. None is set
        further ahead than NOTAM_RETRY_MAX_S: one that is ended when the clock
        was put back."""
        return 0 < until - now <= NOTAM_RETRY_MAX_S

    def _needs_whole(self, entry: Dict, now: float) -> bool:
        """Whether the location's whole set is to be fetched: never fetched, asked
        for, due again, or not to be brought up to date by a pull of changes (too
        old, or dated after `now` because the clock was set back)."""
        if self._holding(entry["retry_at"], now):
            return False
        return (entry["requested"] or entry["ts"] <= 0 or now - entry["ts"] >= BASELINE_S
                or not 0 <= now - entry["synced"] <= DELTA_REACH_S)

    def _delta_targets(self, now: float) -> List[str]:
        """The locations a pull of changes keeps current: those with a whole set
        recent enough to patch."""
        out = []
        for ident in self._notam_idents():
            entry = self._notam_entry(ident, create=False)
            if entry and entry["ts"] > 0 and 0 <= now - entry["synced"] <= DELTA_REACH_S:
                out.append(ident)
        return out

    def _delta_wait(self, now: float) -> float:
        since = now - self._delta_tried
        return max(PULL_INTERVAL_S, self.cfg.notams.refresh_s) - since if since >= 0 else 0.0

    def _nms_pick(self, now: float) -> Optional[tuple]:
        """The pull to make now: ("whole", ident), ("changes",) or None. A whole
        fetch in progress is finished first; otherwise whole fetches and pulls of
        changes take turns while both are due."""
        if self._notam_job:
            return ("whole", self._notam_job["ident"])
        entries = {i: self._notam_entry(i) for i in self._notam_idents()}
        need = [i for i, e in entries.items() if self._needs_whole(e, now)]
        need.sort(key=lambda i: (not entries[i]["requested"], entries[i]["ts"] > 0, entries[i]["synced"]))
        changes_due = bool(self._delta_targets(now)) and self._delta_wait(now) <= 0
        if need and changes_due:
            return ("changes",) if self._last_kind == "whole" else ("whole", need[0])
        if need:
            return ("whole", need[0])
        return ("changes",) if changes_due else None

    async def _nms_step(self) -> float:
        """Make the next pull that is due, at its turn. -> seconds until the loop
        should look again (0: at once)."""
        self.nms.use(self.cfg.notams.environment)
        if not self.nms.configured:
            self._notam_state(f"no FAA credentials are stored for the {self.cfg.notams.environment} environment")
            return NOTAM_IDLE_S
        now = self._now()
        if self._holding(self._nms_hold, now):
            return NOTAM_IDLE_S
        if self._nms_pick(now) is None:
            wait = self._delta_wait(now) if self._delta_targets(now) else NOTAM_IDLE_S
            return min(NOTAM_IDLE_S, max(1.0, wait))
        await self.nms.gate.wait_turn()
        self._prune_notam_cache()     # the configuration may have changed during the wait
        job = self._nms_pick(self._now())
        if job is None or not self.nms.configured:
            return 1.0
        try:
            if job[0] == "changes":
                await self._nms_changes()
            else:
                await self._nms_whole(job[1])
        except (RateLimited, TokenRefused) as exc:
            self._notam_state(str(exc))   # the same pull is made again at the next turn
        except CredentialsRejected as exc:
            self._notam_job = None
            self._nms_hold = self._now() + NOTAM_RETRY_MAX_S   # until then, or until the keys are replaced
            self._notam_state(str(exc))
        return 0.0

    async def _nms_whole(self, ident: str) -> None:
        """One pull of a location's whole set: the next spelling still to ask,
        until the API answers one. It answers every spelling of a location it
        knows with the same set; one it does not know is answered 400."""
        entry = self._notam_entry(ident)
        job = self._notam_job
        if job is None or job["ident"] != ident:
            forms = query_forms(ident)
            known = [f for f in forms if f not in entry["unknown_forms"]]
            job = self._notam_job = {
                "ident": ident, "forms": forms if entry["requested"] or not known else known,
                "features": {}, "unknown": [], "started": None,
            }
        form = job["forms"][0]
        started = self._now()
        self._last_kind = "whole"
        try:
            features = await self.nms.baseline(form, ident)
        except (RateLimited, TokenRefused, CredentialsRejected, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001
            self._notam_job = None
            entry["failures"] += 1
            entry["retry_at"] = self._now() + min(NOTAM_RETRY_MAX_S, PULL_INTERVAL_S * 2 ** entry["failures"])
            entry["error"] = str(exc) or exc.__class__.__name__
            self._notam_state(entry["error"])
            return
        job["started"] = job["started"] or started
        job["forms"].pop(0)
        if features is None:
            job["unknown"].append(form)
        else:
            job.setdefault("answered", []).append(form)
            for n, feature in enumerate(features):
                notam = (((feature or {}).get("properties") or {}).get("coreNOTAMData") or {}).get("notam") or {}
                job["features"].setdefault(str(notam.get("id") or f"{form}:{n}"), feature)
            job["forms"].clear()
        if job["forms"]:
            return                     # another spelling is asked at the next turn
        self._notam_job = None
        done = {"ts": job["started"], "synced": job["started"], "error": None, "requested": False,
                "failures": 0, "retry_at": 0.0}
        if not job.get("answered"):
            entry.update(done, records=[], unknown=True, unknown_forms=[], diff=None, unseen=None)
        else:
            records = [r for r in (normalize(f) for f in job["features"].values()) if r and affects(r, ident)]
            diff = unseen = None
            if entry["ts"] > 0 and entry["synced"] <= job["started"]:   # a set dated after now is not compared
                now = self._now()
                diff = compare_sets(entry["records"], records, now, entry["synced"])
                pending = unsettled_ids(entry["records"], records, now, entry["synced"])
                if any(pending.values()) and job["started"] - entry["synced"] <= DELTA_REACH_S:
                    # the next pull of changes reaches back to here and settles them
                    unseen = {"since": entry["synced"], "ids": pending}
                elif any(pending.values()):
                    # too long ago for a pull of changes to tell
                    diff["unverified"] = sum(diff.pop(k) for k in UNSETTLED)
                self._note_compare(ident, diff)
            asked = set(job["unknown"]) | set(job["answered"])
            # a spelling skipped this time because the API did not know it stays skipped
            unknown_forms = [f for f in entry["unknown_forms"] if f not in asked] + job["unknown"]
            entry.update(done, records=records, unknown=False, unknown_forms=unknown_forms, diff=diff, unseen=unseen)
        self._notam_state(None)
        self._notams_changed()

    def _note_compare(self, ident: str, diff: Dict) -> None:
        """Record how a merged set compared with a whole one; say so when the
        pulls of changes had missed something."""
        self.nms.usage.add({"t": round(self._now(), 1), "kind": "compare", "env": self.nms.environment,
                            "what": ident, "diff": diff})
        missed = {k: v for k, v in diff.items() if k.startswith("missed_") and v}
        if missed:
            log.warning("NOTAMs for %s: the pulls of changes had not brought %s", ident, missed)

    async def _nms_changes(self) -> None:
        """One pull of every NOTAM changed since the oldest point a location is
        complete through, merged by NOTAM id into the locations it affects."""
        now = self._now()
        targets = self._delta_targets(now)
        self._delta_tried = now
        self._last_kind = "changes"
        if not targets:
            return
        entries = {i: self._notam_entry(i) for i in targets}
        # back to the oldest point a location is complete through, and to the
        # point before a whole fetch whose withdrawals are still to be settled
        since = min(min(e["synced"], e["unseen"]["since"] if e["unseen"] else e["synced"]) for e in entries.values())
        since -= DELTA_OVERLAP_S
        try:
            features = await self.nms.changes(since)
        except (RateLimited, TokenRefused, CredentialsRejected, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001
            error = str(exc) or exc.__class__.__name__
            for entry in entries.values():
                entry["error"] = error
            self._notam_state(error)
            return
        records = [r for r in (normalize(f) for f in features) if r]
        changed = False
        for ident, entry in entries.items():
            at = {r["id"]: n for n, r in enumerate(entry["records"])}
            for record in records:
                if not affects(record, ident, strict=True):
                    continue
                n = at.get(record["id"])
                if n is None:
                    at[record["id"]] = len(entry["records"])
                    entry["records"].append(record)
                    changed = True
                elif entry["records"][n] != record:
                    entry["records"][n] = record
                    changed = True
            if entry["unseen"]:
                # a difference this pull reports was made after the last pull; one it
                # does not report was made earlier, and no pull brought it
                settled = settle(entry["unseen"]["ids"], {r["id"] for r in records})
                self._note_compare(ident, settled)
                if entry["diff"]:
                    entry["diff"] = {**entry["diff"], **{k: 0 for k in UNSETTLED},
                                     **settled, "newer": entry["diff"]["newer"] + settled["newer"]}
                entry["unseen"] = None
            entry["synced"], entry["error"] = now, None
        self._notam_state(None)
        self._notams_changed(save=changed)

    def get_notams(self, ident: str) -> Optional[Dict]:
        """The NOTAMs to show for a monitored location now, with the age of the
        data. Answers from the cache; None for a location that is not monitored."""
        cfg = self.cfg.notams
        loc = next((l for l in cfg.locations if l.enabled and l.ident == ident), None) if self._notam_idents() else None
        if loc is None:
            return None
        entry = self._notam_entry(ident, create=False)
        faa = cfg.source == "faa"
        now = self._now()
        fetched = entry["synced"] if entry and entry["ts"] > 0 else None
        age = max(0.0, now - fetched) if fetched else None
        error = None
        if entry:
            error = entry["error"] or (
                f"FAA NMS does not know location {' or '.join(query_forms(ident))}" if entry["unknown"] else None)
        shown = select(entry["records"], now, cfg.lookahead_h) if entry else []
        hidden = repeats(shown)   # the international series repeating a domestic NOTAM shown beside it
        notams = [self._with_rewrite(n) for n in shown if n["id"] not in hidden]
        return {
            "location": ident,
            "name": loc.name,
            "source": cfg.source,
            "source_label": notam_source_label(self.cfg),
            "environment": cfg.environment if faa else None,
            "test_environment": faa and cfg.environment != "production",
            "non_us": any(is_foreign(n) for n in notams),
            "notams": notams,
            "count": len(notams),
            "repeats_hidden": len(hidden),
            "now": int(now),
            "lookahead_h": cfg.lookahead_h,
            "fetched": int(fetched) if fetched else None,
            "age_s": round(age) if age is not None else None,
            "stale": age is not None and age > 2 * max(PULL_INTERVAL_S, cfg.refresh_s) + PULL_INTERVAL_S,
            "pending": fetched is None and error is None,
            "error": error,
        }

    def _notam_detail(self) -> List[Dict]:
        """The state of each monitored location, for the admin page."""
        out = []
        for ident in self._notam_idents():
            entry = self._notam_entry(ident, create=False) or self._new_notam_entry(*self._notam_key())
            out.append({
                "ident": ident,
                "whole_ts": entry["ts"] or None,
                "synced_ts": entry["synced"] or None,
                "count": len(entry["records"]),
                "error": entry["error"],
                "unknown": entry["unknown"],
                "unknown_forms": entry["unknown_forms"],
                "requested": entry["requested"],
                "in_progress": bool(self._notam_job and self._notam_job["ident"] == ident),
                "diff": entry["diff"],
            })
        return out

    async def check_notam_location(self, ident: str, source_name: str) -> Dict:
        """Sample data: what the sample holds for a location, saving nothing. The
        FAA: ask for the location's whole set at the next turn; the answer shows
        in the status once the pull was made."""
        if source_name == "sample":
            try:
                records = await self._fetch_notams(self.notam_sample, ident)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "ident": ident, "source": "sample", "error": str(exc) or exc.__class__.__name__}
            shown = select(records, self._now(), self.cfg.notams.lookahead_h)
            return {"ok": True, "ident": ident, "source": "sample", "asked_as": query_forms(ident),
                    "total": len(records), "shown": len(shown)}
        if self.cfg.notams.source != "faa" or ident not in self._notam_idents():
            return {"ok": False, "ident": ident, "source": "faa",
                    "error": "save this location with the NOTAM views on first; it is then fetched at the FAA's next turn"}
        entry = self._notam_entry(ident)
        entry["requested"], entry["retry_at"] = True, 0.0
        self._notam_wake.set()
        return {"ok": True, "queued": True, "ident": ident, "source": "faa", "asked_as": query_forms(ident),
                "wait_s": round(self.nms.gate.wait_s())}

    async def check_nms_credentials(self) -> Dict:
        """Whether the stored FAA credentials get a token for the configured environment."""
        self.nms.use(self.cfg.notams.environment)
        result = await self.nms.check_credentials()
        if result["ok"]:
            self._nms_hold = 0.0
            self._notam_wake.set()
        return result

    def notam_usage(self, limit: Optional[int] = None) -> Dict:
        """The record of the requests sent to the NMS-API."""
        rows = list(self.nms.usage.rows)
        return {
            "summary": self.nms.usage.summary(self._now()),
            "all_time": self.nms.usage.summary(self._now(), window_s=None),
            "rows": rows[-limit:] if limit else rows,
        }

    # ---- NOTAM rewrites by a model ---------------------------------------------
    # A background task asks the configured model to rewrite each NOTAM once and
    # keeps the answer only if notam_translate's check finds nothing wrong with
    # it. A view shows the checked answer when there is one and the literal
    # decode otherwise; it never waits for the model.
    @staticmethod
    def _rewrite_source(record: Dict) -> tuple:
        return record["text"] or record["raw"], (record["location"], record["icao_location"], record["accountability"])

    def _rewrite_key(self, record: Dict, model: str) -> str:
        text, protect = self._rewrite_source(record)
        digest = hashlib.sha256("|".join((text, *protect)).encode()).hexdigest()[:20]
        return f"{PROMPT_VERSION}|{model}|{digest}"

    def _with_rewrite(self, record: Dict) -> Dict:
        cfg = self.cfg.notams.translate
        if not cfg.enabled:
            return record
        kept = self._rewrites.get(self._rewrite_key(record, cfg.model))
        if not kept or "text" not in kept:
            return record
        return {**record, "plain": {"text": kept["text"], "source": "model", "model": cfg.model}}

    def _kick_rewrites(self) -> None:
        if self.cfg.notams.enabled and self.cfg.notams.translate.enabled and (
            self._rewrite_task is None or self._rewrite_task.done()
        ):
            self._rewrite_task = self._spawn(self._rewrite_notams(), name="notam-rewrite")

    def _rewrite_state(self, error: Optional[str]) -> None:
        if error != self._rewrite_error:
            if error:
                log.error("NOTAM rewrite model unavailable: %s", error)
            else:
                log.info("NOTAM rewrite model available")
        self._rewrite_error = error

    async def _rewrite_one(self, cfg, text: str, type_code: str, protect: tuple) -> Dict:
        """Ask the model for one rewrite and check it. -> {"text"} or {"refused": [...]}."""
        result = await checked_rewrite(
            lambda messages: self.llm.complete(cfg.base_url, cfg.model, messages, cfg.timeout_s, RESPONSE_SCHEMA), text, type_code, protect)
        return {"text": result["text"]} if "text" in result else {"refused": result["refused"]}

    def _rewrite_records(self) -> List[Dict]:
        """The cached NOTAMs a rewrite is wanted for: all but the repeats a view
        hides (repeats())."""
        now, out = self._now(), []
        for entry in list(self._notam_cache.values()):
            skip = repeats(entry["records"], now)
            out += [r for r in entry["records"] if r["id"] not in skip]
        return out

    async def _rewrite_notams(self) -> None:
        """One pass over the cached NOTAMs: rewrite those not yet attempted with
        the current model and prompt. An unusable endpoint ends the pass; the
        next refresh starts another."""
        cfg = self.cfg.notams.translate
        wanted = {}
        for record in self._rewrite_records():
            wanted[self._rewrite_key(record, cfg.model)] = record
        self._rewrites = {k: v for k, v in self._rewrites.items() if k in wanted}
        changed = False
        for key, record in wanted.items():
            if key in self._rewrites:
                continue
            text, protect = self._rewrite_source(record)
            try:
                self._rewrites[key] = await self._rewrite_one(cfg, text, record["type"], protect)
            except ModelError as exc:
                self._rewrite_state(str(exc))
                break
            changed = True
        else:
            self._rewrite_state(None)
        if changed:
            self._save_notam_store()
            self._broadcast({"event": "notams_updated"})

    def _rewrite_counts(self) -> Dict:
        cfg = self.cfg.notams.translate
        shown = refused = pending = 0
        for record in self._rewrite_records():
            kept = self._rewrites.get(self._rewrite_key(record, cfg.model))
            if kept is None:
                pending += 1
            elif "text" in kept:
                shown += 1
            else:
                refused += 1
        return {"shown": shown, "refused": refused, "pending": pending}

    async def test_model(self, base_url: str, model: str, timeout_s: int) -> Dict:
        """Try a model endpoint on one FAA example NOTAM, without saving anything (admin)."""
        text, type_code = "RWY 20 RWY END ID LGT U/S", "RWY"
        out: Dict[str, Any] = {"ok": False, "notam": text, "key": bool(api_key())}
        t0 = time.monotonic()
        try:
            out["models"] = await self.llm.models(base_url, min(timeout_s, 15))
        except ModelError as exc:
            out["models_error"] = str(exc)
        try:
            cfg = self.cfg.notams.translate.model_copy(update={"base_url": base_url, "model": model, "timeout_s": timeout_s})
            result = await self._rewrite_one(cfg, text, type_code, ())
        except ModelError as exc:
            out["error"] = str(exc)
            return out
        out["ms"] = int((time.monotonic() - t0) * 1000)
        out["answer"] = result.get("text")
        out["refused"] = result.get("refused", [])
        out["ok"] = "text" in result
        return out

    # NOTAMs fetched from the FAA are kept on disk like the weather; sample
    # NOTAMs are not.
    def _load_notam_store(self) -> None:
        try:
            with open(NOTAM_STORE) as fh:
                store = json.load(fh)
        except (OSError, ValueError):
            return
        for ident, entry in (store.get("locations") or {}).items():
            if isinstance(entry, dict) and isinstance(entry.get("records"), list) and entry.get("ts"):
                self._notam_cache[ident] = {
                    **self._new_notam_entry("faa", entry.get("environment") or "production"),
                    "ts": entry["ts"],
                    "synced": entry.get("synced") or entry["ts"],
                    "records": entry["records"],
                    "unknown": bool(entry.get("unknown")),
                    "unknown_forms": list(entry.get("unknown_forms") or []),
                }
        self._notam_ts = store.get("ts")
        self._rewrites = {k: v for k, v in (store.get("rewrites") or {}).items() if isinstance(v, dict)}
        if self._notam_cache:
            log.info("NOTAM cache restored from %s (%d locations)", NOTAM_STORE, len(self._notam_cache))

    def _save_notam_store(self) -> None:
        """Queue a save; a single writer task runs until none is pending."""
        self._notam_store_dirty = True
        if self._notam_store_task is None or self._notam_store_task.done():
            self._notam_store_task = self._spawn(self._write_notam_store(), name="notam-store")

    async def _write_notam_store(self) -> None:
        def write(store):
            tmp = NOTAM_STORE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(store, fh, separators=(",", ":"))
            os.replace(tmp, NOTAM_STORE)

        while self._notam_store_dirty:
            self._notam_store_dirty = False
            self._notam_saved = self._now()
            store = {
                "ts": self._notam_ts,
                "rewrites": dict(self._rewrites),
                "locations": {
                    k: {
                        "ts": v["ts"], "synced": v["synced"], "environment": v["environment"],
                        "records": list(v["records"]), "unknown": v["unknown"], "unknown_forms": v["unknown_forms"],
                    }
                    for k, v in self._notam_cache.items()
                    if v["source"] == "faa" and v["ts"] > 0
                },
            }
            try:
                await asyncio.to_thread(write, store)
            except OSError as exc:
                if not self._notam_store_failing:
                    log.error("NOTAM cache not saved to %s: %s", NOTAM_STORE, exc)
                self._notam_store_failing = True
            else:
                if self._notam_store_failing:
                    log.info("NOTAM cache saved again")
                self._notam_store_failing = False

    async def get_satellite(self, sat: str, sector: str, band: str, size: str, frames: int) -> Dict:
        key = f"{sat}:{sector}:{band}:{size}:{frames}"
        cached = self._sat_cache.get(key)
        if cached and (time.time() - cached["ts"]) < SAT_CACHE_TTL_S:
            return cached["data"]
        try:
            data = await self.satellite.frames(sat, sector, band, size, frames)
        except Exception as exc:  # noqa: BLE001
            log.error("satellite fetch failed: %s", exc)
            return cached["data"] if cached else {"frames": [], "count": 0, "error": str(exc)}
        self._cache_put(self._sat_cache, key, data, SAT_CACHE_MAX)
        return data

    # ---- source test (admin) ------------------------------------------------
    async def test_source(self, payload: Dict) -> Dict:
        """Try a candidate data source without saving it. Uses the first enabled
        airport as the probe point (or lat/lon from the payload)."""
        lat = payload.get("lat")
        lon = payload.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            ap = next((a for a in self.cfg.airports if a.enabled), None)
            if ap is None:
                return {"ok": False, "error": "add an enabled airport first (it is used as the test point)"}
            lat, lon = ap.lat, ap.lon
        ds = DataSource.model_validate({k: v for k, v in payload.items() if k in DataSource.model_fields})
        results: Dict[str, Dict] = {}
        if ds.mode in ("local", "auto"):
            t0 = time.monotonic()
            try:
                ac = await self._local_source(ds.local_url).fetch(lat, lon, 50)
                results["local"] = {"ok": True, "count": len(ac), "ms": int((time.monotonic() - t0) * 1000)}
            except Exception as exc:  # noqa: BLE001
                results["local"] = {"ok": False, "error": str(exc) or exc.__class__.__name__}
        if ds.mode in ("aggregator", "auto"):
            t0 = time.monotonic()
            try:
                ac = await self._rate_limited_aggregator_fetch(lat, lon, 50, ds)
                results[ds.aggregator] = {"ok": True, "count": len(ac), "ms": int((time.monotonic() - t0) * 1000)}
            except Exception as exc:  # noqa: BLE001
                results[ds.aggregator] = {"ok": False, "error": str(exc) or exc.__class__.__name__}
        ok = any(r["ok"] for r in results.values())
        return {"ok": ok, "point": {"lat": lat, "lon": lon}, "results": results}

    # ---- display control (remote blackout and navigation) -------------------
    def display_state(self) -> Dict:
        b = self._blackout
        if b["active"] and b["until"] is not None and time.time() >= b["until"]:
            self._blackout = {"active": False, "until": None, "reason": None}
        out = dict(self._blackout)
        out["remaining_s"] = (
            max(0, round(out["until"] - time.time(), 1)) if out["active"] and out["until"] else None
        )
        out["nav"] = {**self._nav, "age_s": _age(self._nav["ts"])}
        views = self.views()
        labels = {v["id"]: v["label"] for v in views}
        out["showing"] = {
            **self._showing,
            "label": labels.get(self._showing["view"]),
            "count": len(views),
            "age_s": _age(self._showing["since"]),
        }
        return out

    def _display_event(self) -> Dict:
        return {"event": "display", **self.display_state()}

    def _cancel_blackout_timer(self) -> None:
        if self._blackout_task and not self._blackout_task.done():
            self._blackout_task.cancel()
        self._blackout_task = None

    def set_blackout(self, seconds: Optional[float] = None, reason: Optional[str] = None) -> Dict:
        """Black out the display, for `seconds` or until restore_display()."""
        self._cancel_blackout_timer()
        until = time.time() + seconds if seconds else None
        self._blackout = {"active": True, "until": until, "reason": reason or None}
        if seconds:
            self._blackout_task = self._spawn(self._auto_restore(seconds), name="blackout-timer")
        log.info("display blackout on (%s)%s", f"{seconds:g}s" if seconds else "until restored", f": {reason}" if reason else "")
        self._broadcast(self._display_event())
        return self.display_state()

    def restore_display(self) -> Dict:
        self._cancel_blackout_timer()
        was_on = self._blackout["active"]
        self._blackout = {"active": False, "until": None, "reason": None}
        if was_on:
            log.info("display blackout off")
        self._broadcast(self._display_event())
        return self.display_state()

    async def _auto_restore(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self._blackout = {"active": False, "until": None, "reason": None}
        self._blackout_task = None
        log.info("display blackout off (timer)")
        self._broadcast(self._display_event())

    def navigate(self, action: str, view_id: Optional[str] = None) -> Dict:
        """Ask the display to move its cycle: "next", "previous", or "view" (to view_id).
        The display owns the cycle; this records a numbered request for it to apply."""
        if action == "view" and view_id not in {v["id"] for v in self.views()}:
            raise UnknownView(view_id)
        self._nav = {
            "seq": self._nav["seq"] + 1,
            "action": action,
            "view": view_id if action == "view" else None,
            "ts": time.time(),
        }
        log.info("display navigate: %s%s", action, f" {view_id}" if action == "view" else "")
        self._broadcast(self._display_event())
        return self.display_state()

    def report_showing(self, view_id: str, index: Optional[int] = None) -> Dict:
        """Record the view the display reports it has switched to."""
        views = self.views()
        if view_id not in {v["id"] for v in views}:
            raise UnknownView(view_id)
        if index is not None and not 0 <= index < len(views):
            index = None
        self._showing = {"view": view_id, "index": index, "since": time.time()}
        return self.display_state()

    # ---- status / SSE ----------------------------------------------------
    def status(self) -> Dict:
        return {
            **self._source_status,
            "boot_id": self.boot_id,
            "uptime_s": int(time.time() - self.started_at),
            "local_source": self._local_state,
            "config": {"version": self.config_version, "source": self.config_source, "error": self.config_error},
            "weather": {
                "last_refresh_ts": self._weather_ts,
                "stations": len(self._weather_cache),
                "error": self._weather_error,
                "gateway": {
                    "stations": sorted(k for k, m in self._weather_cache.items() if m.get("source") == GATEWAY_SOURCE),
                    "error": self._gateway_error,
                },
            },
            "notams": {
                "enabled": self.cfg.notams.enabled,
                "source": self.cfg.notams.source,
                "environment": self.cfg.notams.environment,
                "test_environment": self.cfg.notams.source == "faa" and self.cfg.notams.environment != "production",
                "source_label": notam_source_label(self.cfg),
                "blocked": self.notams_blocked(),
                "credentials": self.nms.configured,
                "locations": len(self._notam_idents()),
                "detail": self._notam_detail(),
                "last_refresh_ts": self._notam_ts,
                "error": self._notam_error,
                "next_pull_in_s": round(self.nms.gate.wait_s()),
                "rate_limited": self.nms.gate.limited,
                "usage": self.nms.usage.summary(time.time()),
                "translate": {
                    "enabled": self.cfg.notams.translate.enabled,
                    "model": self.cfg.notams.translate.model,
                    "description": self.cfg.notams.translate.description,
                    "key": bool(api_key()),
                    "error": self._rewrite_error,
                    **self._rewrite_counts(),
                },
            },
            "subscribers": len(self._subscribers),
            "display": self.display_state(),
            "tiles": {"dir": self.tiles.root, **self.tiles.warm_state},
        }

    def subscribe(self) -> asyncio.Queue:
        if len(self._subscribers) >= MAX_SUBSCRIBERS:
            raise TooManySubscribers()
        q: asyncio.Queue = asyncio.Queue(maxsize=10)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _broadcast(self, msg: Dict):
        for q in list(self._subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass
