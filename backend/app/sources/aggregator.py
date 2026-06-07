"""Free public aggregator sources (all ADSBExchange-v2 compatible).

adsb.fi uses /api/v3/lat/{lat}/lon/{lon}/dist/{nm}; adsb.lol and airplanes.live
use /v2/point/{lat}/{lon}/{radius}. All return {"ac": [...]}.
"""
from __future__ import annotations

from typing import Dict, List

from .base import TrafficSource, normalize_aircraft

# radius is capped at 250 nm by every provider.
MAX_NM = 250


class AggregatorSource(TrafficSource):
    name = "aggregator"

    def __init__(self, client, provider: str = "adsbfi", api_key: str = ""):
        super().__init__(client)
        self.provider = provider
        self.api_key = api_key

    def _url(self, lat: float, lon: float, nm: float) -> str:
        nm = min(round(nm), MAX_NM)
        if self.provider == "adsbfi":
            return f"https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{nm}"
        if self.provider == "adsblol":
            return f"https://api.adsb.lol/v2/point/{lat}/{lon}/{nm}"
        if self.provider == "airplaneslive":
            host = "rest.api.airplanes.live" if self.api_key else "api.airplanes.live"
            return f"https://{host}/v2/point/{lat}/{lon}/{nm}"
        raise ValueError(f"unknown aggregator provider: {self.provider}")

    async def fetch(self, lat: float, lon: float, radius_nm: float) -> List[Dict]:
        headers = {"User-Agent": "hangar-display/0.1"}
        if self.api_key and self.provider == "airplaneslive":
            headers["auth"] = self.api_key
        resp = await self.client.get(self._url(lat, lon, radius_nm), headers=headers, timeout=6.0)
        resp.raise_for_status()
        data = resp.json()
        out: List[Dict] = []
        for raw in data.get("ac", []):
            ac = normalize_aircraft(raw)
            if ac is not None:
                out.append(ac)
        return out
