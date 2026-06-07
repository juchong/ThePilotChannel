"""DataManager: owns sources, caching, rate limiting, weather refresh, and
config hot-reload. One upstream poll is shared across all clients/views.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

import httpx

from .config import Config, load_config, save_config
from .sources.aggregator import AggregatorSource
from .sources.local import LocalReadsbSource
from .views import build_views
from .weather import MetarSource

log = logging.getLogger("hangar")

TRAFFIC_CACHE_TTL = 1.0      # seconds; one upstream poll per view per second max
AGGREGATOR_MIN_INTERVAL = 1.0  # published public rate limit: 1 req/sec


class DataManager:
    def __init__(self):
        self.cfg: Config = load_config()
        self.client = httpx.AsyncClient(follow_redirects=True)
        self.metar = MetarSource(self.client)

        self._traffic_cache: Dict[str, Dict] = {}
        self._refreshing: set = set()
        self._weather_cache: Dict[str, Dict] = {}
        self._area_cache: Dict[str, Dict] = {}
        self._agg_lock = asyncio.Lock()
        self._agg_last = 0.0
        self._source_status = {"active": None, "healthy": True, "last_error": None}

        self._subscribers: List[asyncio.Queue] = []
        self._weather_task: Optional[asyncio.Task] = None

    # ---- lifecycle -------------------------------------------------------
    async def start(self):
        self._weather_task = asyncio.create_task(self._weather_loop())

    async def stop(self):
        if self._weather_task:
            self._weather_task.cancel()
        await self.client.aclose()

    # ---- config ----------------------------------------------------------
    def get_config(self) -> Config:
        return self.cfg

    def update_config(self, cfg: Config):
        save_config(cfg)
        self.cfg = cfg
        self._traffic_cache.clear()
        self._broadcast({"event": "config_changed"})

    def views(self) -> List[Dict]:
        return build_views(self.cfg)

    # ---- sources ---------------------------------------------------------
    def _local_source(self) -> LocalReadsbSource:
        return LocalReadsbSource(self.client, self.cfg.data_source.local_url)

    def _aggregator_source(self) -> AggregatorSource:
        ds = self.cfg.data_source
        return AggregatorSource(self.client, ds.aggregator, ds.api_key)

    async def _rate_limited_aggregator_fetch(self, lat, lon, nm) -> List[Dict]:
        async with self._agg_lock:
            wait = AGGREGATOR_MIN_INTERVAL - (time.monotonic() - self._agg_last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._agg_last = time.monotonic()
        return await self._aggregator_source().fetch(lat, lon, nm)

    async def _fetch_traffic(self, lat, lon, nm) -> List[Dict]:
        mode = self.cfg.data_source.mode
        if mode == "local":
            ac = await self._local_source().fetch(lat, lon, nm)
            self._source_status.update(active="local", healthy=True, last_error=None)
            return ac
        if mode == "aggregator":
            ac = await self._rate_limited_aggregator_fetch(lat, lon, nm)
            self._source_status.update(
                active=f"aggregator:{self.cfg.data_source.aggregator}", healthy=True, last_error=None
            )
            return ac
        # auto: local-first, aggregator fallback
        try:
            ac = await self._local_source().fetch(lat, lon, nm)
            self._source_status.update(active="local", healthy=True, last_error=None)
            return ac
        except Exception as exc:  # noqa: BLE001
            log.warning("local source failed, falling back to aggregator: %s", exc)
            ac = await self._rate_limited_aggregator_fetch(lat, lon, nm)
            self._source_status.update(
                active=f"aggregator:{self.cfg.data_source.aggregator}", healthy=True, last_error=None
            )
            return ac

    async def get_traffic(self, view_id: str) -> Dict:
        """Non-blocking: returns cached data immediately and refreshes upstream in
        the background, so the client poll never waits on the source fetch."""
        view = next((v for v in self.views() if v["id"] == view_id), None)
        if view is None:
            return {"error": "unknown view", "aircraft": [], "count": 0}

        cached = self._traffic_cache.get(view_id)
        now = time.monotonic()
        fresh = cached and (now - cached["ts"]) < TRAFFIC_CACHE_TTL
        if not fresh and view_id not in self._refreshing:
            self._refreshing.add(view_id)
            asyncio.create_task(self._refresh_traffic(view_id, view))

        if cached:
            return cached["data"]
        return {
            "view": view_id,
            "aircraft": [],
            "count": 0,
            "source": self._source_status["active"],
            "healthy": True,
            "pending": True,
        }

    async def _refresh_traffic(self, view_id: str, view: Dict) -> None:
        try:
            aircraft = await self._fetch_traffic(
                view["center_lat"], view["center_lon"], view["radius_nm"]
            )
            self._traffic_cache[view_id] = {
                "ts": time.monotonic(),
                "data": {
                    "view": view_id,
                    "aircraft": aircraft,
                    "count": len(aircraft),
                    "source": self._source_status["active"],
                    "ts": time.time(),
                    "healthy": True,
                },
            }
        except Exception as exc:  # noqa: BLE001
            self._source_status.update(healthy=False, last_error=str(exc))
            log.error("traffic fetch failed for %s: %s", view_id, exc)
        finally:
            self._refreshing.discard(view_id)

    # ---- weather ---------------------------------------------------------
    def _all_icaos(self) -> List[str]:
        return [ap.icao for ap in self.cfg.airports if ap.enabled]

    async def _weather_loop(self):
        while True:
            try:
                await self._refresh_weather()
            except Exception as exc:  # noqa: BLE001
                log.error("weather refresh failed: %s", exc)
            await asyncio.sleep(max(30, self.cfg.weather.refresh_s))

    async def _refresh_weather(self):
        icaos = self._all_icaos()
        if not icaos:
            return
        data = await self.metar.fetch(icaos, self.cfg.weather.stale_after_s)
        self._weather_cache = data
        self._broadcast({"event": "weather_updated"})

    def get_weather(self, icaos: Optional[List[str]] = None) -> Dict[str, Dict]:
        if not icaos:
            return self._weather_cache
        return {i.upper(): self._weather_cache[i.upper()] for i in icaos if i.upper() in self._weather_cache}

    async def get_area_weather(self, lat: float, lon: float, radius_nm: float) -> List[Dict]:
        """All METAR stations within radius_nm of (lat, lon), cached by area."""
        from .geo import haversine_nm

        key = f"{round(lat, 2)}:{round(lon, 2)}:{round(radius_nm)}"
        cached = self._area_cache.get(key)
        now = time.monotonic()
        if cached and (now - cached["ts"]) < max(60, self.cfg.weather.refresh_s):
            return cached["data"]
        # bounding box (degrees) around the center; filter to the circle after.
        dlat = radius_nm / 60.0
        import math

        dlon = radius_nm / (60.0 * max(0.1, math.cos(math.radians(lat))))
        try:
            stations = await self.metar.fetch_bbox(
                lat - dlat, lon - dlon, lat + dlat, lon + dlon, self.cfg.weather.stale_after_s
            )
        except Exception as exc:  # noqa: BLE001
            log.error("area weather fetch failed: %s", exc)
            return cached["data"] if cached else []
        out = [
            s
            for s in stations
            if s.get("lat") is not None
            and haversine_nm(s["lat"], s["lon"], lat, lon) <= radius_nm
        ]
        self._area_cache[key] = {"ts": now, "data": out}
        return out

    async def get_bbox_weather(self, min_lat, min_lon, max_lat, max_lon) -> List[Dict]:
        """All METAR stations within an explicit bounding box (the map's visible
        rectangle), cached by box."""
        key = f"bbox:{round(min_lat, 2)}:{round(min_lon, 2)}:{round(max_lat, 2)}:{round(max_lon, 2)}"
        cached = self._area_cache.get(key)
        now = time.monotonic()
        if cached and (now - cached["ts"]) < max(60, self.cfg.weather.refresh_s):
            return cached["data"]
        try:
            stations = await self.metar.fetch_bbox(
                min_lat, min_lon, max_lat, max_lon, self.cfg.weather.stale_after_s
            )
        except Exception as exc:  # noqa: BLE001
            log.error("bbox weather fetch failed: %s", exc)
            return cached["data"] if cached else []
        out = [s for s in stations if s.get("lat") is not None]
        self._area_cache[key] = {"ts": now, "data": out}
        return out

    # ---- status / SSE ----------------------------------------------------
    def status(self) -> Dict:
        return dict(self._source_status)

    def subscribe(self) -> asyncio.Queue:
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
