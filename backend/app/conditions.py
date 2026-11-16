"""The weather at the monitored airports for automations (GET /api/conditions): one
flat record per airport from the cached METAR, with the ceiling read from the
report's text and the relative humidity worked out from the temperature and the dew
point."""
from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

from .metar_decode import UNLIMITED_FT, decode

UNKNOWN = "UNKNOWN"   # the category of a report that does not settle one, as the display labels it
INHG_PER_HPA = 0.02953   # the National Weather Service's pressure conversion, as the display shows it


def relative_humidity(temp_c: Optional[float], dewpoint_c: Optional[float]) -> Optional[float]:
    """Relative humidity in percent, at most 100: the Magnus formula with Alduchov and
    Eskridge's coefficients (17.625, 243.04 C), which gives the relative humidity the
    National Weather Service publishes with its observations (api.weather.gov)."""
    if temp_c is None or dewpoint_c is None:
        return None
    e = math.exp(17.625 * dewpoint_c / (243.04 + dewpoint_c))
    e_s = math.exp(17.625 * temp_c / (243.04 + temp_c))
    return min(100.0, 100 * e / e_s)


def ceiling(metar: Dict) -> Tuple[Optional[int], bool]:
    """The ceiling in feet (None when there is none) and whether the report settles
    it, from the report's text: aviationweather.gov's `clouds` leave out a group whose
    amount or height is slashed (VV002///, VV///, BKN///). Without the text only a
    stated height counts as known."""
    decoded = decode(metar["raw"]) if metar.get("raw") else None
    if decoded is None:
        return metar.get("ceiling_ft"), metar.get("ceiling_ft") is not None
    low, high = decoded["ceiling_range"]
    return (low if low == high and low < UNLIMITED_FT else None), low == high


def _whole(value: Optional[float]) -> Optional[int]:
    return None if value is None else int(value + 0.5)


def readout(metar: Optional[Dict], name: str, now: float, stale_after_s: int) -> Dict:
    """An airport's record: every field present, null where the report gives no value."""
    m = metar or {}
    wind = m.get("wind") or {}
    obs = m.get("obs_time")
    age = int(now - obs) if isinstance(obs, (int, float)) else None
    speed = wind.get("speed_kt")
    direction = wind.get("dir")
    if wind.get("variable") or speed == 0 or not isinstance(direction, (int, float)):
        direction = None
    ceiling_ft, ceiling_known = ceiling(m)
    hpa = m.get("altimeter_hpa")
    return {
        "name": name or m.get("name") or "",
        "reported": metar is not None,
        "category": m.get("category") or UNKNOWN,
        "observed": obs,
        "age_s": age,
        "stale": None if metar is None else age is not None and age > stale_after_s,
        "ceiling_ft": ceiling_ft,
        "ceiling_known": ceiling_known,
        "visibility_sm": m.get("visibility_sm"),
        "wind_dir_deg": direction,
        "wind_variable": bool(wind.get("variable")),
        "wind_speed_kt": speed,
        "wind_gust_kt": wind.get("gust_kt"),
        "temperature_c": m.get("temp_c"),
        "dewpoint_c": m.get("dewpoint_c"),
        "humidity_pct": _whole(relative_humidity(m.get("temp_c"), m.get("dewpoint_c"))),
        "altimeter_inhg": None if hpa is None else round(hpa * INHG_PER_HPA, 2),
        "raw": m.get("raw"),
    }


def conditions(airports, cache: Dict[str, Dict], now: float, stale_after_s: int) -> Dict:
    """The enabled airports in the settings' order, and each one's record."""
    monitored = [ap for ap in airports if ap.enabled]
    return {
        "monitored": [ap.icao.upper() for ap in monitored],
        "airports": {ap.icao.upper(): readout(cache.get(ap.icao.upper()), ap.name, now, stale_after_s)
                     for ap in monitored},
    }
