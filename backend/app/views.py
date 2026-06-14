"""Resolve config into the ordered list of display views the frontend cycles."""
from __future__ import annotations

from typing import Dict, List

from .config import Config
from .geo import miles_to_nm


def build_views(cfg: Config) -> List[Dict]:
    """Return ordered view descriptors. Each: id, type, label, center, radius, airports."""
    local_views: List[Dict] = []
    for ap in cfg.airports:
        if not ap.enabled:
            continue
        local_views.append(
            {
                "id": f"local:{ap.icao}",
                "type": "local",
                "label": f"{ap.icao} - Local {int(ap.local_radius_mi)}mi",
                "center_lat": ap.lat,
                "center_lon": ap.lon,
                "radius_mi": ap.local_radius_mi,
                "radius_nm": round(miles_to_nm(ap.local_radius_mi), 2),
                "dwell_s": cfg.cycle.local_dwell_s,
                "airports": [ap.icao],
            }
        )

    if cfg.cycle.max_local_views and cfg.cycle.max_local_views > 0:
        local_views = local_views[: cfg.cycle.max_local_views]

    regional_views: List[Dict] = []
    for rg in cfg.regions:
        if not rg.enabled:
            continue
        airports_in = [
            ap.icao
            for ap in cfg.airports
            if _within(ap.lat, ap.lon, rg.center_lat, rg.center_lon, rg.radius_mi)
        ]
        regional_views.append(
            {
                "id": f"region:{rg.name}",
                "type": "regional",
                "label": f"Region: {rg.name} - {int(rg.radius_mi)}mi",
                "center_lat": rg.center_lat,
                "center_lon": rg.center_lon,
                "radius_mi": rg.radius_mi,
                "radius_nm": round(miles_to_nm(rg.radius_mi), 2),
                "dwell_s": cfg.cycle.regional_dwell_s,
                "airports": airports_in,
                # NEXRAD precipitation overlay, drawn beneath the wind barbs.
                "radar": _radar_payload(cfg) if cfg.radar.enabled else None,
            }
        )

    views = _order(cfg, local_views, regional_views)

    if cfg.satellite.enabled:
        views.append(
            {
                "id": "satellite",
                "type": "satellite",
                "label": cfg.satellite.label,
                "dwell_s": cfg.satellite.dwell_s,
                "sat": cfg.satellite.sat,
                "sector": cfg.satellite.sector,
                "band": cfg.satellite.band,
                "size": cfg.satellite.size,
                "frames": cfg.satellite.frames,
            }
        )
    return views


def _radar_payload(cfg: Config) -> Dict:
    """NEXRAD radar overlay data for the regional view: an ordered frame list
    (oldest -> newest) of IEM time-lagged tile-layer suffixes, plus the tile
    template. Framing comes from the regional view it is attached to."""
    r = cfg.radar
    n = max(1, r.frames)
    step = max(1, r.interval_min)
    frames: List[Dict] = []
    for k in range(n - 1, -1, -1):  # oldest first so the loop animates forward in time
        age = k * step
        suffix = "" if age == 0 else f"-m{age:02d}m"
        frames.append({"suffix": suffix, "age_min": age})
    return {
        "label": r.label,
        "opacity": r.opacity,
        "product": r.product,
        # Iowa Environmental Mesonet RIDGE II composite, Web Mercator (EPSG:3857).
        "tile_base": f"https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-{r.product}-900913",
        "frames": frames,
    }


def _order(cfg: Config, local_views: List[Dict], regional_views: List[Dict]) -> List[Dict]:
    by_id = {v["id"]: v for v in (local_views + regional_views)}

    if cfg.cycle.order:
        ordered = [by_id[i] for i in cfg.cycle.order if i in by_id]
        if ordered:
            return ordered

    if cfg.cycle.interleave_regional and regional_views:
        ordered = []
        for i, lv in enumerate(local_views):
            ordered.append(lv)
            ordered.append(regional_views[i % len(regional_views)])
        if not local_views:
            ordered = regional_views
        return ordered

    return local_views + regional_views


def _within(lat, lon, clat, clon, radius_mi) -> bool:
    from .geo import haversine_nm

    return haversine_nm(lat, lon, clat, clon) <= miles_to_nm(radius_mi)
