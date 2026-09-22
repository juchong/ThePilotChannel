"""TrafficSource interface + the normalized aircraft schema.

All adapters return a list of dicts in the unified shape so the rest of the
app (and the frontend) never needs to know which provider produced the data.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

import httpx

# ICAO type designator -> [ICAO 8643 description ("L2J"), wake turbulence
# category ("L"/"M"/"H")], from tar1090-db (db/icao_aircraft_types2.js). The
# display's icon assignment follows tar1090, which needs these two fields that
# receivers and aggregators do not send.
_TYPES_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "icao_aircraft_types.json")
try:
    with open(_TYPES_PATH) as _fh:
        AIRCRAFT_TYPES: Dict[str, List[Optional[str]]] = json.load(_fh)
except OSError:
    AIRCRAFT_TYPES = {}


def type_info(designator: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(type description, wake turbulence category) for an ICAO type designator."""
    if not designator:
        return None, None
    entry = AIRCRAFT_TYPES.get(str(designator).strip().upper())
    if not entry:
        return None, None
    return entry[0] or None, entry[1] or None


def normalize_aircraft(raw: Dict, now_s: Optional[float] = None) -> Optional[Dict]:
    """Map an ADSBExchange-v2 / readsb aircraft record to the unified schema.

    now_s is the feed's own timestamp (seconds) for the record set; with the
    record's seen_pos it gives fix_ts, when the position was actually measured,
    which the display uses to dead-reckon the aircraft between updates.
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
    type_desc, wtc = type_info(raw.get("t"))
    seen_pos = raw.get("seen_pos")
    fix_ts = None
    if isinstance(now_s, (int, float)) and isinstance(seen_pos, (int, float)):
        fix_ts = round(now_s - seen_pos, 3)

    return {
        "hex": raw.get("hex"),
        "callsign": callsign,
        "registration": raw.get("r"),
        "type": raw.get("t"),
        "type_desc": type_desc,      # ICAO 8643 description, e.g. L2J
        "wtc": wtc,                  # wake turbulence category L/M/H
        "db_flags": raw.get("dbFlags"),  # tar1090-db flags (bit 0: military)
        "lat": lat,
        "lon": lon,
        "track": raw.get("track"),
        "gs": raw.get("gs"),
        "alt_ft": alt_ft,
        "on_ground": on_ground,
        "baro_rate": raw.get("baro_rate"),
        "category": raw.get("category"),
        "seen": raw.get("seen"),
        "seen_pos": seen_pos,
        "fix_ts": fix_ts,
        "squawk": raw.get("squawk"),
    }


def feed_now(data: Dict) -> Optional[float]:
    """The feed's timestamp in seconds: readsb writes seconds, the v2 aggregator
    APIs write milliseconds."""
    now = data.get("now")
    if not isinstance(now, (int, float)):
        return None
    return now / 1000.0 if now > 1e11 else float(now)


class TrafficSource:
    """Abstract pull-based traffic source."""

    name = "base"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch(self, lat: float, lon: float, radius_nm: float) -> List[Dict]:
        raise NotImplementedError
