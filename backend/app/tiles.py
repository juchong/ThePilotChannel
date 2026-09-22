"""Basemap tile cache and proxy.

The display's map fetches its OpenStreetMap tiles from this backend at
/tiles/{z}/{x}/{y}.png instead of from the public tile servers. Tiles are
stored on disk (HANGAR_TILE_CACHE, default /data/tiles) and served from there,
so a view whose tiles are cached renders in milliseconds from localhost, and
each tile is requested from OpenStreetMap once (then again after TTL_DAYS).

At startup and after a config change the cache is warmed with every tile the
configured views can show: for each map view, the tiles covering the map area
at the zoom the display will use, for both the 1080p and 4K layouts, plus the
parent zoom that MapLibre falls back to. Requests to OpenStreetMap are paced
and carry an identifying User-Agent, per their tile usage policy.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple

import httpx

log = logging.getLogger("hangar.tiles")

OSM_TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_SUBDOMAINS = ("a", "b", "c")
USER_AGENT = "ThePilotChannel/0.2 (+https://github.com/juchong/ThePilotChannel)"
TTL_DAYS = 14                 # re-fetch a tile after this long
MAX_ZOOM = 17
CONCURRENCY = 2               # parallel upstream fetches during warm-up
WARM_SPACING_S = 0.1          # pause between warm-up fetches
MAX_TILES_PER_VIEW = 600
MAX_TILES_TOTAL = 3000

# Map area in CSS pixels for the two layouts in styles.css (stage minus the side
# panel; height minus countdown, header, and footer). fitBounds pads by 40.
LAYOUTS = ((1920 - 380, 1080 - 8 - 64 - 36), (3840 - 720, 2160 - 16 - 120 - 64))
FIT_PADDING = 40
WORLD_PX = 512                # MapLibre world size at zoom 0
RASTER_TILE_PX = 256

Tile = Tuple[int, int, int]


# ---- tile math ---------------------------------------------------------------------

def bbox_for_radius(lat: float, lon: float, radius_nm: float) -> Tuple[float, float, float, float]:
    """Same framing as frontend/src/lib/geo.js: (west, south, east, north)."""
    d_lat = radius_nm / 60.0
    d_lon = radius_nm / (60.0 * max(math.cos(math.radians(lat)), 1e-6))
    return lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat


def mercator(lat: float, lon: float) -> Tuple[float, float]:
    """Web Mercator position as fractions of the world in [0, 1]."""
    lat = max(-85.0511, min(85.0511, lat))
    x = (lon + 180.0) / 360.0
    s = math.sin(math.radians(lat))
    y = 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)
    return x, y


def fit_zoom(bbox: Tuple[float, float, float, float], map_w: int, map_h: int) -> float:
    """Fractional zoom MapLibre's fitBounds picks for bbox in a map_w x map_h area."""
    w, s, e, n = bbox
    x0, y1 = mercator(s, w)
    x1, y0 = mercator(n, e)
    dx = max(x1 - x0, 1e-9)
    dy = max(y1 - y0, 1e-9)
    zw = math.log2((map_w - 2 * FIT_PADDING) / (WORLD_PX * dx))
    zh = math.log2((map_h - 2 * FIT_PADDING) / (WORLD_PX * dy))
    return min(zw, zh)


def tiles_for_view(lat: float, lon: float, radius_nm: float, map_w: int, map_h: int) -> Set[Tile]:
    """Every raster tile the map shows for this view at this layout, at the
    zoom it will use plus the parent zoom it falls back to while loading."""
    zoom = fit_zoom(bbox_for_radius(lat, lon, radius_nm), map_w, map_h)
    # 256 px raster tiles on a 512 px world: MapLibre uses round(zoom + 1).
    z_ideal = int(round(zoom + 1))
    cx, cy = mercator(lat, lon)
    half_w = map_w / 2 / (WORLD_PX * 2 ** zoom)  # visible half-extent as a world fraction
    half_h = map_h / 2 / (WORLD_PX * 2 ** zoom)
    out: Set[Tile] = set()
    for z in (z_ideal - 1, z_ideal):
        if z < 0 or z > MAX_ZOOM:
            continue
        n = 2 ** z
        x_min = max(0, math.floor((cx - half_w) * n))
        x_max = min(n - 1, math.floor((cx + half_w) * n))
        y_min = max(0, math.floor((cy - half_h) * n))
        y_max = min(n - 1, math.floor((cy + half_h) * n))
        for x in range(x_min, x_max + 1):
            for y in range(y_min, y_max + 1):
                out.add((z, x, y))
    return out


def tiles_for_views(views: Iterable[Dict]) -> List[Tile]:
    """Union of tiles for every map-based view across both layouts, bounded so
    a mistaken config cannot turn warm-up into bulk downloading."""
    out: Set[Tile] = set()
    for v in views:
        if v.get("center_lat") is None or v.get("radius_nm") is None:
            continue
        per_view: Set[Tile] = set()
        for map_w, map_h in LAYOUTS:
            per_view |= tiles_for_view(v["center_lat"], v["center_lon"], v["radius_nm"], map_w, map_h)
        if len(per_view) > MAX_TILES_PER_VIEW:
            log.warning("view %s needs %d tiles; warming only the first %d", v.get("id"), len(per_view), MAX_TILES_PER_VIEW)
            per_view = set(sorted(per_view)[:MAX_TILES_PER_VIEW])
        out |= per_view
    tiles = sorted(out)
    if len(tiles) > MAX_TILES_TOTAL:
        log.warning("%d tiles across views; warming only %d", len(tiles), MAX_TILES_TOTAL)
        tiles = tiles[:MAX_TILES_TOTAL]
    return tiles


def valid_tile(z: int, x: int, y: int) -> bool:
    return 0 <= z <= MAX_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z


# ---- cache ----------------------------------------------------------------------------

class TileCache:
    def __init__(self, client: httpx.AsyncClient, root: Optional[str] = None):
        self.client = client
        self.root = root or os.environ.get("HANGAR_TILE_CACHE", "/data/tiles")
        self._inflight: Dict[Tile, asyncio.Future] = {}
        self._sem = asyncio.Semaphore(4)
        self._rr = 0
        self.warm_state: Dict = {"state": "idle", "total": 0, "fetched": 0, "cached": 0, "failed": 0}
        try:
            os.makedirs(self.root, exist_ok=True)
            probe = os.path.join(self.root, ".write-test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
        except OSError as exc:
            fallback = os.path.join("/tmp", "hangar-tiles")
            log.warning("tile cache dir %s not writable (%s); using %s", self.root, exc, fallback)
            self.root = fallback
            os.makedirs(self.root, exist_ok=True)

    def path(self, z: int, x: int, y: int) -> str:
        return os.path.join(self.root, str(z), str(x), f"{y}.png")

    def _fresh(self, path: str) -> bool:
        try:
            st = os.stat(path)
        except OSError:
            return False
        return st.st_size > 0 and (time.time() - st.st_mtime) < TTL_DAYS * 86400

    def _url(self, z: int, x: int, y: int) -> str:
        self._rr = (self._rr + 1) % len(OSM_SUBDOMAINS)
        return OSM_TILE_URL.format(s=OSM_SUBDOMAINS[self._rr], z=z, x=x, y=y)

    async def _fetch_upstream(self, z: int, x: int, y: int) -> bytes:
        resp = await self.client.get(self._url(z, x, y), headers={"User-Agent": USER_AGENT}, timeout=15.0)
        resp.raise_for_status()
        if not resp.content or not resp.headers.get("content-type", "").startswith("image/"):
            raise ValueError(f"unexpected tile response ({resp.headers.get('content-type')})")
        return resp.content

    def _write(self, path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)

    async def ensure(self, z: int, x: int, y: int) -> Optional[str]:
        """Path of a fresh cached tile, fetching it if needed. Returns a stale
        cached tile if upstream fails, or None if there is nothing to serve."""
        path = self.path(z, x, y)
        if self._fresh(path):
            return path
        key = (z, x, y)
        fut = self._inflight.get(key)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self._inflight[key] = fut
            try:
                async with self._sem:
                    data = await self._fetch_upstream(z, x, y)
                await asyncio.to_thread(self._write, path, data)
                fut.set_result(path)
            except Exception as exc:  # noqa: BLE001
                fut.set_result(None)
                log.warning("tile %d/%d/%d fetch failed: %s", z, x, y, exc)
            finally:
                self._inflight.pop(key, None)
        result = await fut
        if result:
            return result
        return path if os.path.exists(path) else None

    def count(self) -> int:
        n = 0
        for _root, _dirs, files in os.walk(self.root):
            n += sum(1 for f in files if f.endswith(".png"))
        return n

    async def warm(self, views: Iterable[Dict]) -> Dict:
        """Fetch every tile the views need that is not already cached."""
        tiles = tiles_for_views(views)
        st = self.warm_state = {"state": "running", "total": len(tiles), "fetched": 0, "cached": 0, "failed": 0}
        todo = [t for t in tiles if not self._fresh(self.path(*t))]
        st["cached"] = len(tiles) - len(todo)
        if todo:
            log.info("warming tile cache: %d tiles to fetch (%d already cached)", len(todo), st["cached"])
        sem = asyncio.Semaphore(CONCURRENCY)

        async def one(t: Tile):
            async with sem:
                ok = await self.ensure(*t)
                st["fetched" if ok else "failed"] += 1
                await asyncio.sleep(WARM_SPACING_S)

        await asyncio.gather(*(one(t) for t in todo))
        st["state"] = "done"
        if todo:
            log.info("tile cache warm: %d fetched, %d failed, %d total cached", st["fetched"], st["failed"], self.count())
        return dict(st)
