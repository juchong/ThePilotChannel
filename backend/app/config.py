"""Config model: load/validate/save the single YAML source of truth."""
from __future__ import annotations

import os
import threading
from typing import List, Literal

import yaml
from pydantic import BaseModel, Field

CONFIG_PATH = os.environ.get("HANGAR_CONFIG", "/data/config.yaml")
_LOCK = threading.Lock()


class Airport(BaseModel):
    icao: str
    name: str = ""
    lat: float
    lon: float
    local_radius_mi: float = 8
    enabled: bool = True


class Region(BaseModel):
    name: str
    center_lat: float
    center_lon: float
    radius_mi: float = 50
    enabled: bool = True


class Cycle(BaseModel):
    local_dwell_s: int = 20
    regional_dwell_s: int = 25
    order: List[str] = Field(default_factory=list)
    max_local_views: int = 0
    interleave_regional: bool = True


class DataSource(BaseModel):
    mode: Literal["local", "aggregator", "auto"] = "auto"
    local_url: str = "http://localhost/tar1090/data/aircraft.json"
    aggregator: Literal["adsbfi", "adsblol", "airplaneslive"] = "adsbfi"
    api_key: str = ""
    drop_timeout_s: int = 15


class Display(BaseModel):
    units: Literal["imperial", "metric"] = "imperial"
    timezone: str = "America/Los_Angeles"
    resolution: str = "1920x1080"
    basemap: Literal["raster_osm", "vector"] = "raster_osm"
    tile_url: str = ""
    tile_api_key: str = ""


class Weather(BaseModel):
    refresh_s: int = 300
    stale_after_s: int = 4500


class Satellite(BaseModel):
    enabled: bool = True
    sat: str = "G18"            # G16 (East), G18 (West), G19
    sector: str = "pnw"         # NOAA STAR sector code
    band: str = "GEOCOLOR"
    frames: int = 24            # number of frames in the loop
    size: str = "1200x1200"     # 300x300 | 600x600 | 1200x1200 | 2400x2400
    dwell_s: int = 25
    label: str = "GOES-West PNW GeoColor"


class Config(BaseModel):
    airports: List[Airport] = Field(default_factory=list)
    regions: List[Region] = Field(default_factory=list)
    cycle: Cycle = Field(default_factory=Cycle)
    data_source: DataSource = Field(default_factory=DataSource)
    display: Display = Field(default_factory=Display)
    weather: Weather = Field(default_factory=Weather)
    satellite: Satellite = Field(default_factory=Satellite)


def load_config(path: str | None = None) -> Config:
    path = path or CONFIG_PATH
    with _LOCK:
        if not os.path.exists(path):
            cfg = Config()
            _write(path, cfg)
            return cfg
        with open(path, "r") as fh:
            raw = yaml.safe_load(fh) or {}
    return Config.model_validate(raw)


def save_config(cfg: Config, path: str | None = None) -> None:
    path = path or CONFIG_PATH
    with _LOCK:
        _write(path, cfg)


def _write(path: str, cfg: Config) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        yaml.safe_dump(cfg.model_dump(mode="json"), fh, sort_keys=False, default_flow_style=False)
    os.replace(tmp, path)
