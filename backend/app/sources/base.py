"""TrafficSource interface + the normalized aircraft schema.

All adapters return a list of dicts in the unified shape so the rest of the
app (and the frontend) never needs to know which provider produced the data.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import httpx


def normalize_aircraft(raw: Dict) -> Optional[Dict]:
    """Map an ADSBExchange-v2 / readsb aircraft record to the unified schema.

    Returns None if the record has no usable position.
    """
    lat = raw.get("lat")
    lon = raw.get("lon")
    # readsb/aggregators expose last-known position under lastPosition when stale.
    if lat is None or lon is None:
        last = raw.get("lastPosition") or {}
        lat = last.get("lat")
        lon = last.get("lon")
    if lat is None or lon is None:
        return None

    alt = raw.get("alt_baro")
    on_ground = alt == "ground"
    alt_ft = None if on_ground else (alt if isinstance(alt, (int, float)) else None)

    callsign = (raw.get("flight") or "").strip() or None

    return {
        "hex": raw.get("hex"),
        "callsign": callsign,
        "registration": raw.get("r"),
        "type": raw.get("t"),
        "lat": lat,
        "lon": lon,
        "track": raw.get("track"),
        "gs": raw.get("gs"),
        "alt_ft": alt_ft,
        "on_ground": on_ground,
        "baro_rate": raw.get("baro_rate"),
        "category": raw.get("category"),
        "seen": raw.get("seen"),
        "seen_pos": raw.get("seen_pos"),
        "squawk": raw.get("squawk"),
    }


class TrafficSource:
    """Abstract pull-based traffic source."""

    name = "base"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch(self, lat: float, lon: float, radius_nm: float) -> List[Dict]:
        raise NotImplementedError
