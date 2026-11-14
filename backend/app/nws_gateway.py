"""METARs from the NWS telecommunication gateway, for the configured airports
aviationweather.gov does not serve.

The gateway keeps one text file per station with the station's latest report: a
`YYYY/MM/DD HH:MM` line (UTC) and the report. It answers 404 for a station it does
not know, and keeps the file of a station that stopped reporting, so a report older
than MAX_AGE_S is not taken. Reports are decoded by `metar_decode` and shaped like
aviationweather.gov's by `weather.normalize_metar`; the flight category and the
ceiling are given only when the report settles them.
"""
from __future__ import annotations

import asyncio
import calendar
import re
import time
from typing import Dict, List, Optional, Tuple

import httpx

from .metar_decode import UNLIMITED_FT, decode, observed_at
from .weather import CATEGORY_COLORS, RETRY_DELAY_S, TRANSIENT, category_in_range, normalize_metar

STATION_URL = "https://tgftp.nws.noaa.gov/data/observations/metar/stations/{icao}.TXT"
SOURCE = "nws"
MAX_AGE_S = 30 * 86400  # how long aviationweather.gov keeps returning a station's last report
MAX_FILE_CHARS = 2000  # a station file is one report
PARALLEL = 4

_STAMP = re.compile(r"^(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2})$")


def parse_station_file(text: str, icao: str) -> Optional[Tuple[Dict, float]]:
    """(decoded record, observation time as epoch seconds) from a station file, or
    None when the text is not a dated report of that station."""
    lines = [ln.strip() for ln in text[:MAX_FILE_CHARS].splitlines() if ln.strip()]
    stamp = _STAMP.match(lines[0]) if len(lines) >= 2 else None
    if not stamp:
        return None
    year, month, day, hour, minute = (int(g) for g in stamp.groups())
    if not (1 <= month <= 12 and 1 <= day <= calendar.monthrange(year, month)[1] and hour < 24 and minute < 60):
        return None
    rec = decode(" ".join(lines[1:]))
    if rec is None or rec["icaoId"] != icao.upper():
        return None
    obs = observed_at(rec["time"], calendar.timegm((year, month, day, hour, minute, 0)))
    return (rec, obs) if obs is not None else None


def to_metar(rec: Dict, obs_time: float, name: str, lat: float, lon: float, stale_after_s: int) -> Dict:
    """The display's METAR record for a decoded gateway report of a configured airport."""
    m = normalize_metar(dict(rec, obsTime=obs_time, name=name, lat=lat, lon=lon), stale_after_s)
    low, high = rec["ceiling_range"]
    category = category_in_range(rec["ceiling_range"], rec["visibility_range"])
    m.update(
        ceiling_ft=low if low == high and low < UNLIMITED_FT else None,
        category=category,
        category_color=CATEGORY_COLORS.get(category),
        source=SOURCE,
    )
    return m


class GatewaySource:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _get(self, url: str) -> httpx.Response:
        try:
            return await self.client.get(url, timeout=8.0)
        except TRANSIENT:
            await asyncio.sleep(RETRY_DELAY_S)
            return await self.client.get(url, timeout=8.0)

    async def fetch_one(self, airport, stale_after_s: int) -> Optional[Dict]:
        """The airport's latest report as a METAR record; None when the gateway has
        no file for it or the file holds no report newer than MAX_AGE_S. Raises on
        a transport error or any other HTTP failure."""
        resp = await self._get(STATION_URL.format(icao=airport.icao.upper()))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        parsed = parse_station_file(resp.text, airport.icao)
        if parsed is None:
            return None
        rec, obs = parsed
        if time.time() - obs > MAX_AGE_S:
            return None
        return to_metar(rec, obs, airport.name or airport.icao, airport.lat, airport.lon, stale_after_s)

    async def fetch(self, airports: List, stale_after_s: int) -> Tuple[Dict[str, Dict], Optional[str]]:
        """Latest reports for the given configured airports, keyed by identifier,
        and the first failure (None when every request was answered). One station
        failing does not keep the others from being read."""
        gate = asyncio.Semaphore(PARALLEL)

        async def one(airport):
            async with gate:
                return await self.fetch_one(airport, stale_after_s)

        results = await asyncio.gather(*(one(a) for a in airports), return_exceptions=True)
        found: Dict[str, Dict] = {}
        error = None
        for airport, result in zip(airports, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                error = error or f"{airport.icao}: {str(result) or result.__class__.__name__}"
            elif result is not None:
                found[airport.icao.upper()] = result
        return found, error
