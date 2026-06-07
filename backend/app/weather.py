"""METAR adapter: aviationweather.gov Data API.

Fetches batched METARs, trusts the API's fltCat when present, derives a
fallback per FAA AIM 7-1-7 otherwise, and produces a decoded summary plus the
data the wind barb needs.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

import httpx

API_URL = "https://aviationweather.gov/api/data/metar"

# Flight category -> display color (hex). Black/white fallback chosen by frontend theme.
CATEGORY_COLORS = {
    "VFR": "#22c55e",   # green
    "MVFR": "#3b82f6",  # blue
    "IFR": "#ef4444",   # red
    "LIFR": "#d946ef",  # magenta
}

CEILING_COVERS = {"BKN", "OVC", "OVX"}


def _ceiling_ft(clouds: List[Dict]) -> Optional[int]:
    """Lowest BKN/OVC/vertical-vis base = ceiling. FEW/SCT are not ceilings."""
    bases = [c.get("base") for c in clouds or [] if c.get("cover") in CEILING_COVERS and c.get("base") is not None]
    return min(bases) if bases else None


def _visibility_sm(visib) -> Optional[float]:
    if visib is None:
        return None
    if isinstance(visib, (int, float)):
        return float(visib)
    s = str(visib).strip()
    if s.endswith("+"):
        s = s[:-1]
    try:
        if " " in s:  # e.g. "1 1/2"
            whole, frac = s.split(" ", 1)
            num, den = frac.split("/")
            return float(whole) + float(num) / float(den)
        if "/" in s:
            num, den = s.split("/")
            return float(num) / float(den)
        return float(s)
    except (ValueError, ZeroDivisionError):
        return None


def derive_category(ceiling_ft: Optional[int], vis_sm: Optional[float]) -> Optional[str]:
    """FAA AIM 7-1-7 categorical ceiling/visibility (worst of the two)."""
    if ceiling_ft is None and vis_sm is None:
        return None
    cig = ceiling_ft if ceiling_ft is not None else 99999
    vis = vis_sm if vis_sm is not None else 99.0
    if cig < 500 or vis < 1:
        return "LIFR"
    if cig < 1000 or vis < 3:
        return "IFR"
    if cig <= 3000 or vis <= 5:
        return "MVFR"
    return "VFR"


def normalize_metar(raw: Dict, stale_after_s: int) -> Dict:
    clouds = raw.get("clouds") or []
    ceiling = _ceiling_ft(clouds)
    vis = _visibility_sm(raw.get("visib"))

    category = raw.get("fltCat") or derive_category(ceiling, vis)

    wdir = raw.get("wdir")  # int degrees true, or "VRB", or None
    variable = isinstance(wdir, str) and wdir.upper() == "VRB"
    wspd = raw.get("wspd")
    wgst = raw.get("wgst")

    obs = raw.get("obsTime")
    age_s = int(time.time() - obs) if isinstance(obs, (int, float)) else None
    stale = age_s is not None and age_s > stale_after_s

    return {
        "icao": raw.get("icaoId"),
        "name": raw.get("name"),
        "lat": raw.get("lat"),
        "lon": raw.get("lon"),
        "category": category,
        "category_color": CATEGORY_COLORS.get(category) if category else None,
        "wind": {
            "dir": None if variable else wdir,
            "variable": variable,
            "speed_kt": wspd,
            "gust_kt": wgst,
            "calm": wspd == 0,
        },
        "ceiling_ft": ceiling,
        "visibility_sm": vis,
        "temp_c": raw.get("temp"),
        "dewpoint_c": raw.get("dewp"),
        "altimeter_hpa": raw.get("altim"),
        "clouds": clouds,
        "raw": raw.get("rawOb"),
        "obs_time": obs,
        "age_s": age_s,
        "stale": stale,
    }


class MetarSource:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch(self, icaos: List[str], stale_after_s: int) -> Dict[str, Dict]:
        if not icaos:
            return {}
        ids = ",".join(sorted({i.upper() for i in icaos}))
        resp = await self.client.get(
            API_URL, params={"ids": ids, "format": "json"}, timeout=8.0
        )
        resp.raise_for_status()
        records = resp.json() or []
        out: Dict[str, Dict] = {}
        for raw in records:
            m = normalize_metar(raw, stale_after_s)
            if m["icao"]:
                out[m["icao"].upper()] = m
        return out

    async def fetch_bbox(self, min_lat, min_lon, max_lat, max_lon, stale_after_s: int) -> List[Dict]:
        """All reporting stations within a bounding box (every airport with a
        METAR, not just configured ones)."""
        bbox = f"{min_lat},{min_lon},{max_lat},{max_lon}"
        resp = await self.client.get(
            API_URL, params={"bbox": bbox, "format": "json"}, timeout=10.0
        )
        resp.raise_for_status()
        records = resp.json() or []
        return [normalize_metar(r, stale_after_s) for r in records if r.get("icaoId")]
