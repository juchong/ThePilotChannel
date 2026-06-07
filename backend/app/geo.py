"""Geographic helpers shared by traffic sources and view building."""
from __future__ import annotations

import math

NM_PER_MILE = 0.868976
KM_PER_NM = 1.852
EARTH_RADIUS_NM = 3440.065


def miles_to_nm(miles: float) -> float:
    return miles * NM_PER_MILE


def nm_to_km(nm: float) -> float:
    return nm * KM_PER_NM


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in nautical miles."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(math.sqrt(a))


def within_radius(lat: float, lon: float, center_lat: float, center_lon: float, radius_nm: float) -> bool:
    return haversine_nm(lat, lon, center_lat, center_lon) <= radius_nm
