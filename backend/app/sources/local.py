"""Local receiver source: reads a tar1090/readsb/dump1090 aircraft.json."""
from __future__ import annotations

from typing import Dict, List

from ..geo import haversine_nm
from .base import TrafficSource, normalize_aircraft


class LocalReadsbSource(TrafficSource):
    name = "local"

    def __init__(self, client, url: str):
        super().__init__(client)
        self.url = url

    async def fetch(self, lat: float, lon: float, radius_nm: float) -> List[Dict]:
        resp = await self.client.get(self.url, timeout=1.5)
        resp.raise_for_status()
        data = resp.json()
        out: List[Dict] = []
        for raw in data.get("aircraft", []):
            ac = normalize_aircraft(raw)
            if ac is None:
                continue
            if haversine_nm(ac["lat"], ac["lon"], lat, lon) <= radius_nm:
                out.append(ac)
        return out
