"""DataManager: owns sources, caching, rate limiting, weather refresh, and
config hot-reload. One upstream poll is shared across all clients/views.

Traffic freshness: views that a client polled recently are refreshed by a
background loop at a fixed cadence, independent of client polling, so the
snapshot age never aliases with the 1 Hz browser poll. Snapshots carry their
age and a stale flag so the display can show when data has stopped flowing.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Set

import httpx

from .config import (
    Config,
    DataSource,
    config_version,
    load_config,
    save_config,
)
from .satellite import SatelliteSource
from .sources.aggregator import AggregatorSource
from .tiles import TileCache
from .sources.local import LocalReadsbSource
from .views import build_views
from .weather import MetarSource

log = logging.getLogger("hangar")

TRAFFIC_REFRESH_S = 1.0        # target cadence for an actively viewed view
ACTIVE_VIEW_WINDOW_S = 5.0     # keep refreshing a view for this long after its last poll
TRAFFIC_STALE_S = 10.0         # snapshot older than this is flagged stale
LOCAL_RETRY_S = 60.0           # circuit breaker: after a local-source failure, skip it this long
AGGREGATOR_MIN_INTERVAL = 1.0  # published public rate limit: 1 req/sec
SAT_CACHE_TTL_S = 240.0
AREA_CACHE_MAX = 64
SAT_CACHE_MAX = 16
MAX_SUBSCRIBERS = 32


class TooManySubscribers(RuntimeError):
    pass


class DataManager:
    def __init__(self):
        loaded = load_config()
        self.cfg: Config = loaded.config
        self.config_source = loaded.source
        self.config_error = loaded.error
        self.config_version = config_version()
        self.boot_id = uuid.uuid4().hex[:12]
        self.started_at = time.time()

        self.client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"User-Agent": "the-pilot-channel/0.2 (+https://github.com/juchong/ThePilotChannel)"},
        )
        self.metar = MetarSource(self.client)
        self.satellite = SatelliteSource(self.client)
        self.tiles = TileCache(self.client)
        self._warm_task: Optional[asyncio.Task] = None

        self._traffic_cache: Dict[str, Dict] = {}   # view_id -> {ts, aircraft, source}
        self._traffic_health: Dict[str, Dict] = {}  # view_id -> {healthy, error}
        self._polled: Dict[str, float] = {}         # view_id -> monotonic time of last client poll
        self._refreshing: Set[str] = set()
        self._weather_cache: Dict[str, Dict] = {}
        self._weather_ts: Optional[float] = None
        self._weather_error: Optional[str] = None
        self._area_cache: Dict[str, Dict] = {}
        self._sat_cache: Dict[str, Dict] = {}

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
        self._weather_wake = asyncio.Event()
        self._loops: List[asyncio.Task] = []

    # ---- lifecycle -------------------------------------------------------
    async def start(self):
        self._loops = [
            asyncio.create_task(self._weather_loop(), name="weather-loop"),
            asyncio.create_task(self._traffic_loop(), name="traffic-loop"),
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
        self._weather_wake.set()  # refresh METARs for any new airports now
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
        return AggregatorSource(self.client, ds.aggregator, ds.api_key)

    async def _rate_limited_aggregator_fetch(self, lat, lon, nm, ds: Optional[DataSource] = None) -> List[Dict]:
        async with self._agg_lock:
            wait = AGGREGATOR_MIN_INTERVAL - (time.monotonic() - self._agg_last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._agg_last = time.monotonic()
        return await self._aggregator_source(ds).fetch(lat, lon, nm)

    # Circuit breaker for the local receiver. Failures are logged once per state
    # change (not once per second) and the local source is skipped for
    # LOCAL_RETRY_S before being tried again.
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
    async def get_traffic(self, view_id: str) -> Optional[Dict]:
        """Non-blocking: records that the view is being watched (so the
        background loop keeps it fresh), kicks an immediate refresh if there is
        no snapshot yet, and returns the current snapshot with its age."""
        view = next((v for v in self.views() if v["id"] == view_id), None)
        if view is None:
            return None
        self._polled[view_id] = time.monotonic()
        cached = self._traffic_cache.get(view_id)
        if cached is None:
            self._kick_refresh(view_id, view)
        return self._snapshot(view_id, cached)

    def _kick_refresh(self, view_id: str, view: Dict) -> None:
        if view_id in self._refreshing:
            return
        self._refreshing.add(view_id)
        self._spawn(self._refresh_traffic(view_id, view), name=f"traffic:{view_id}")

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
                    if cached and now - cached["ts"] < TRAFFIC_REFRESH_S * 0.9:
                        continue
                    self._kick_refresh(view_id, views[view_id])
            except Exception as exc:  # noqa: BLE001
                log.error("traffic loop error: %s", exc)
            await asyncio.sleep(TRAFFIC_REFRESH_S / 4)

    def _snapshot(self, view_id: str, cached: Optional[Dict]) -> Dict:
        health = self._traffic_health.get(view_id, {"healthy": True, "error": None})
        if cached is None:
            return {
                "view": view_id, "aircraft": [], "count": 0,
                "source": self._source_status["active"],
                "ts": None, "age_s": None, "stale": False, "pending": True,
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
            aircraft = await self._fetch_traffic(view["center_lat"], view["center_lon"], view["radius_nm"])
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
        finally:
            self._refreshing.discard(view_id)

    # ---- weather ---------------------------------------------------------
    def _all_icaos(self) -> List[str]:
        return [ap.icao for ap in self.cfg.airports if ap.enabled]

    async def _weather_loop(self):
        while True:
            try:
                await self._refresh_weather()
                if self._weather_error:
                    log.info("weather refresh recovered")
                self._weather_error = None
            except Exception as exc:  # noqa: BLE001
                err = str(exc) or exc.__class__.__name__
                if err != self._weather_error:
                    log.error("weather refresh failed: %s", err)
                self._weather_error = err
            self._weather_wake.clear()
            try:
                await asyncio.wait_for(self._weather_wake.wait(), timeout=max(60, self.cfg.weather.refresh_s))
            except asyncio.TimeoutError:
                pass

    async def _refresh_weather(self):
        icaos = self._all_icaos()
        if not icaos:
            self._weather_cache = {}
            return
        data = await self.metar.fetch(icaos, self.cfg.weather.stale_after_s)
        # merge so a station missing from one response keeps its last report
        # (its age keeps growing and it is flagged stale by get_weather)
        merged = {k: v for k, v in self._weather_cache.items() if k in icaos}
        merged.update(data)
        self._weather_cache = merged
        self._weather_ts = time.time()
        self._broadcast({"event": "weather_updated"})

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
        cache[key] = {"ts": time.monotonic(), "data": data}
        while len(cache) > max_entries:
            oldest = min(cache, key=lambda k: cache[k]["ts"])
            cache.pop(oldest, None)

    async def get_area_weather(self, lat: float, lon: float, radius_nm: float) -> List[Dict]:
        """All METAR stations within radius_nm of (lat, lon), cached by area."""
        import math

        from .geo import haversine_nm

        key = f"{round(lat, 2)}:{round(lon, 2)}:{round(radius_nm)}"
        cached = self._area_cache.get(key)
        now = time.monotonic()
        if cached and (now - cached["ts"]) < max(60, self.cfg.weather.refresh_s):
            return [self._with_age(s) for s in cached["data"]]
        dlat = radius_nm / 60.0
        dlon = radius_nm / (60.0 * max(0.1, math.cos(math.radians(lat))))
        try:
            stations = await self.metar.fetch_bbox(
                lat - dlat, lon - dlon, lat + dlat, lon + dlon, self.cfg.weather.stale_after_s
            )
        except Exception as exc:  # noqa: BLE001
            log.error("area weather fetch failed: %s", exc)
            return cached["data"] if cached else []
        out = [
            s for s in stations
            if s.get("lat") is not None and haversine_nm(s["lat"], s["lon"], lat, lon) <= radius_nm
        ]
        self._cache_put(self._area_cache, key, out, AREA_CACHE_MAX)
        return out

    async def get_bbox_weather(self, min_lat, min_lon, max_lat, max_lon) -> List[Dict]:
        """All METAR stations within an explicit bounding box (the map's visible
        rectangle), cached by box."""
        key = f"bbox:{round(min_lat, 2)}:{round(min_lon, 2)}:{round(max_lat, 2)}:{round(max_lon, 2)}"
        cached = self._area_cache.get(key)
        now = time.monotonic()
        if cached and (now - cached["ts"]) < max(60, self.cfg.weather.refresh_s):
            return [self._with_age(s) for s in cached["data"]]
        try:
            stations = await self.metar.fetch_bbox(min_lat, min_lon, max_lat, max_lon, self.cfg.weather.stale_after_s)
        except Exception as exc:  # noqa: BLE001
            log.error("bbox weather fetch failed: %s", exc)
            return cached["data"] if cached else []
        out = [s for s in stations if s.get("lat") is not None]
        self._cache_put(self._area_cache, key, out, AREA_CACHE_MAX)
        return out

    async def get_satellite(self, sat: str, sector: str, band: str, size: str, frames: int) -> Dict:
        key = f"{sat}:{sector}:{band}:{size}:{frames}"
        cached = self._sat_cache.get(key)
        now = time.monotonic()
        if cached and (now - cached["ts"]) < SAT_CACHE_TTL_S:
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

    # ---- display control (remote blackout) ---------------------------------
    def display_state(self) -> Dict:
        b = self._blackout
        if b["active"] and b["until"] is not None and time.time() >= b["until"]:
            self._blackout = {"active": False, "until": None, "reason": None}
        out = dict(self._blackout)
        out["remaining_s"] = (
            max(0, round(out["until"] - time.time(), 1)) if out["active"] and out["until"] else None
        )
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
