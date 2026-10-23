"""NEXRAD radar tiles from the Iowa Environmental Mesonet, proxied with a
reflectivity floor: every pixel weaker than `radar.min_dbz` is made
transparent by looking its color up in the product's palette. Tiles are
cached in memory per 5-minute bucket."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sys
from array import array
from collections import OrderedDict
from typing import Dict, FrozenSet, List, Optional, Tuple

import httpx
from PIL import Image, ImageChops

from .tiles import USER_AGENT

log = logging.getLogger("hangar.radar")

IEM_TILE_URL = "https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-{product}-900913{lag}/{z}/{x}/{y}.png"
LAYER_RE = re.compile(r"^(?P<product>n0q|n0r)(?P<lag>-m\d{2}m)?$")
CONCURRENCY = 8
TIMEOUT_S = 15.0
MAX_CACHED = 1200              # filtered tiles kept in memory
LEGEND_STEPS = (5, 15, 25, 35, 45, 55, 65, 75)

# Palettes of IEM's composite images, index -> [r, g, b].
with open(os.path.join(os.path.dirname(__file__), "data", "radar_palettes.json")) as _fh:
    PALETTES: Dict[str, List[List[int]]] = json.load(_fh)


def dbz_of_index(product: str, i: int) -> Optional[float]:
    """Reflectivity of palette index i, or None where the index is not a reflectivity."""
    if product == "n0q":
        return (i - 65) / 2 if 1 <= i <= 245 else None  # -32 to 90 dBZ in 0.5 dBZ steps
    if product == "n0r":
        return (i - 6) * 5 if 7 <= i <= 21 else None  # 5 to 75 dBZ in 5 dBZ steps
    return None


def legend(product: str, min_dbz: float) -> List[Tuple[int, str]]:
    """[(dbz, '#rrggbb')] for the standard legend steps at or above the floor,
    in the product's own colors."""
    pal = PALETTES.get(product) or []
    out: List[Tuple[int, str]] = []
    for step in LEGEND_STEPS:
        if step < min_dbz:
            continue
        idx = next((i for i in range(len(pal)) if dbz_of_index(product, i) == step), None)
        if idx is None:
            continue
        r, g, b = pal[idx]
        out.append((step, f"#{r:02x}{g:02x}{b:02x}"))
    return out


_drop_sets: Dict[Tuple[str, float], FrozenSet[int]] = {}


def _drop_set(product: str, min_dbz: float) -> FrozenSet[int]:
    """Packed RGB values (r | g << 8 | b << 16) of the palette entries below the floor."""
    key = (product, min_dbz)
    if key not in _drop_sets:
        pal = PALETTES.get(product) or []
        _drop_sets[key] = frozenset(
            r | (g << 8) | (b << 16)
            for i, (r, g, b) in enumerate(pal)
            if (d := dbz_of_index(product, i)) is not None and d < min_dbz
        )
    return _drop_sets[key]


def filter_tile(png: bytes, product: str, min_dbz: float) -> bytes:
    """The tile with every pixel below min_dbz made transparent. Pixels whose
    color is not a reflectivity in the palette are left as they are."""
    drop = _drop_set(product, min_dbz)
    if not drop:
        return png
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    px = array("I")
    px.frombytes(im.tobytes())
    if sys.byteorder != "little":
        px.byteswap()
    mask = bytes(0 if (v & 0xFFFFFF) in drop else 255 for v in px)
    im.putalpha(ImageChops.multiply(im.getchannel("A"), Image.frombytes("L", im.size, mask)))
    out = io.BytesIO()
    im.save(out, "PNG", compress_level=6)
    return out.getvalue()


class RadarTiles:
    """Memory cache of filtered radar tiles keyed by layer, tile, bucket and floor."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._cache: "OrderedDict[Tuple, bytes]" = OrderedDict()
        self._inflight: Dict[Tuple, asyncio.Future] = {}
        self._sem = asyncio.Semaphore(CONCURRENCY)
        self._failing = False

    def stats(self) -> Dict:
        return {"cached": len(self._cache)}

    async def _fetch_upstream(self, product: str, lag: str, z: int, x: int, y: int) -> bytes:
        url = IEM_TILE_URL.format(product=product, lag=lag, z=z, x=x, y=y)
        resp = await self.client.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S)
        resp.raise_for_status()
        if not resp.content or not resp.headers.get("content-type", "").startswith("image/"):
            raise ValueError(f"unexpected radar tile response ({resp.headers.get('content-type')})")
        return resp.content

    async def get(self, layer: str, z: int, x: int, y: int, bucket: int, min_dbz: float) -> Optional[bytes]:
        """The filtered tile, or None when it could not be fetched."""
        m = LAYER_RE.match(layer)
        if not m:
            return None
        key = (layer, z, x, y, bucket, min_dbz)
        data = self._cache.get(key)
        if data is not None:
            self._cache.move_to_end(key)
            return data
        fut = self._inflight.get(key)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self._inflight[key] = fut
            try:
                async with self._sem:
                    raw = await self._fetch_upstream(m["product"], m["lag"] or "", z, x, y)
                data = await asyncio.to_thread(filter_tile, raw, m["product"], min_dbz)
                self._cache[key] = data
                while len(self._cache) > MAX_CACHED:
                    self._cache.popitem(last=False)
                if self._failing:
                    log.info("radar tiles recovered")
                    self._failing = False
                fut.set_result(data)
            except Exception as exc:  # noqa: BLE001
                if not self._failing:
                    log.warning("radar tiles unavailable (%s %d/%d/%d): %s", layer, z, x, y, exc)
                    self._failing = True
                fut.set_result(None)
            finally:
                self._inflight.pop(key, None)
        return await fut
