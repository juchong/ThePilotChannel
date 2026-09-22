"""Config model: load/validate/save the single YAML source of truth.

Loading is fault tolerant: a corrupt or invalid file falls back to the newest
readable backup, then to built-in defaults, and the problem is reported through
``LoadedConfig.error`` so /healthz and the admin page can show it instead of the
container restart-looping. Writes are atomic and durable (temp file, fsync,
rename, directory fsync) and rotate a few backups of the previous file.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger("hangar.config")

CONFIG_PATH = os.environ.get("HANGAR_CONFIG", "/data/config.yaml")
BACKUPS = 3
_LOCK = threading.Lock()

# Secrets are never returned by the API. GET replaces them with SECRET_MASK and a
# PUT that sends the mask back keeps the stored value (empty string clears it).
SECRET_MASK = "********"
SECRET_FIELDS = (("data_source", "api_key"),)

# IEM publishes NEXRAD time-lagged layers every 5 minutes up to -m55m.
RADAR_MAX_AGE_MIN = 55

ICAO_RE = re.compile(r"^[A-Z0-9]{3,4}$")
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
SIZE_RE = re.compile(r"^\d{3,4}x\d{3,4}$")


def validate_source_url(url: str) -> str:
    """A traffic-source URL the backend will fetch: http(s) only, must have a
    host, and must not point at link-local / metadata address ranges."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    if not parts.hostname:
        raise ValueError("URL must include a host name")
    try:
        ip = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return url  # a host name; LAN names are fine
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise ValueError("URL must not use a link-local or multicast address")
    return url


class Airport(BaseModel):
    icao: str = Field(min_length=3, max_length=4)
    name: str = Field("", max_length=80)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    local_radius_mi: float = Field(8, gt=0, le=100)
    enabled: bool = True

    @field_validator("icao")
    @classmethod
    def _icao(cls, v: str) -> str:
        v = v.strip().upper()
        if not ICAO_RE.match(v):
            raise ValueError("ICAO must be 3 or 4 letters or digits")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return v.strip()


class Region(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    center_lat: float = Field(ge=-90, le=90)
    center_lon: float = Field(ge=-180, le=180)
    radius_mi: float = Field(50, gt=0, le=500)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("region name is required")
        return v


class Cycle(BaseModel):
    local_dwell_s: int = Field(20, ge=5, le=3600)
    regional_dwell_s: int = Field(25, ge=5, le=3600)
    order: List[str] = Field(default_factory=list)
    max_local_views: int = Field(0, ge=0, le=100)
    interleave_regional: bool = True


class DataSource(BaseModel):
    mode: Literal["local", "aggregator", "auto"] = "auto"
    local_url: str = "http://localhost/tar1090/data/aircraft.json"
    aggregator: Literal["adsbfi", "adsblol", "airplaneslive"] = "adsbfi"
    api_key: str = Field("", max_length=200)
    drop_timeout_s: int = Field(15, ge=1, le=300)

    @field_validator("local_url")
    @classmethod
    def _local_url(cls, v: str) -> str:
        return validate_source_url(v)


class Display(BaseModel):
    timezone: str = "America/Los_Angeles"
    basemap: Literal["raster_osm", "vector"] = "raster_osm"
    tile_url: str = Field("", max_length=500)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        v = v.strip()
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown IANA timezone: {v!r}")
        return v

    @field_validator("tile_url")
    @classmethod
    def _tile_url(cls, v: str) -> str:
        v = v.strip()
        if v and urlsplit(v).scheme not in ("http", "https"):
            raise ValueError("tile URL must start with http:// or https://")
        return v


class Weather(BaseModel):
    refresh_s: int = Field(300, ge=60, le=3600)
    stale_after_s: int = Field(4500, ge=60, le=86400)


class Satellite(BaseModel):
    enabled: bool = True
    sat: Literal["G16", "G18", "G19"] = "G18"
    sector: str = "pnw"          # NOAA STAR sector code
    band: str = "GEOCOLOR"
    frames: int = Field(24, ge=1, le=60)
    size: str = "1200x1200"      # 300x300 | 600x600 | 1200x1200 | 2400x2400
    dwell_s: int = Field(25, ge=5, le=3600)
    label: str = Field("GOES-West PNW GeoColor", max_length=80)

    @field_validator("sector", "band")
    @classmethod
    def _code(cls, v: str) -> str:
        v = v.strip()
        if not CODE_RE.match(v):
            raise ValueError("use letters, digits, '-' or '_' only")
        return v

    @field_validator("size")
    @classmethod
    def _size(cls, v: str) -> str:
        v = v.strip()
        if not SIZE_RE.match(v):
            raise ValueError("size must look like 1200x1200")
        return v


class Radar(BaseModel):
    """NEXRAD base reflectivity precipitation overlay (IEM RIDGE II).

    Drawn beneath the wind barbs on the regional weather view. The current
    frame plus 5-minute time-lagged frames form an animated loop; IEM only
    serves lag layers up to 55 minutes, so (frames - 1) * interval_min is
    capped at RADAR_MAX_AGE_MIN.
    """
    enabled: bool = True
    label: str = Field("NEXRAD Base Reflectivity", max_length=80)
    frames: int = Field(10, ge=1, le=12)
    interval_min: int = Field(5, ge=5, le=RADAR_MAX_AGE_MIN)
    opacity: float = Field(0.75, ge=0, le=1)
    product: Literal["n0q", "n0r"] = "n0q"

    @field_validator("interval_min")
    @classmethod
    def _interval(cls, v: int) -> int:
        if v % 5:
            raise ValueError("interval must be a multiple of 5 minutes")
        return v

    @model_validator(mode="after")
    def _span(self):
        span = (self.frames - 1) * self.interval_min
        if span > RADAR_MAX_AGE_MIN:
            raise ValueError(
                f"(frames - 1) x interval is {span} min; IEM only serves lag layers up to {RADAR_MAX_AGE_MIN} min"
            )
        return self


class Config(BaseModel):
    airports: List[Airport] = Field(default_factory=list)
    regions: List[Region] = Field(default_factory=list)
    cycle: Cycle = Field(default_factory=Cycle)
    data_source: DataSource = Field(default_factory=DataSource)
    display: Display = Field(default_factory=Display)
    weather: Weather = Field(default_factory=Weather)
    satellite: Satellite = Field(default_factory=Satellite)
    radar: Radar = Field(default_factory=Radar)

    @model_validator(mode="after")
    def _unique(self):
        seen = set()
        for ap in self.airports:
            if ap.icao in seen:
                raise ValueError(f"duplicate airport {ap.icao}")
            seen.add(ap.icao)
        names = set()
        for rg in self.regions:
            key = rg.name.lower()
            if key in names:
                raise ValueError(f"duplicate region name {rg.name!r}")
            names.add(key)
        return self


# ---- unknown-key detection (typos in a PUT would otherwise be silently dropped) ---

def unknown_keys(payload: Any, model: type[BaseModel], prefix: str = "") -> List[str]:
    if not isinstance(payload, dict):
        return []
    out: List[str] = []
    fields = model.model_fields
    for key, val in payload.items():
        path = f"{prefix}{key}"
        if key not in fields:
            out.append(path)
            continue
        ann = fields[key].annotation
        sub = _model_of(ann)
        if sub is not None:
            if isinstance(val, list):
                for i, item in enumerate(val):
                    out.extend(unknown_keys(item, sub, f"{path}.{i}."))
            else:
                out.extend(unknown_keys(val, sub, f"{path}."))
    return out


def _model_of(ann) -> Optional[type[BaseModel]]:
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return ann
    for arg in getattr(ann, "__args__", ()) or ():
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


# ---- secrets ---------------------------------------------------------------

def mask_secrets(data: Dict[str, Any]) -> Dict[str, Any]:
    for section, key in SECRET_FIELDS:
        sec = data.get(section)
        if isinstance(sec, dict) and sec.get(key):
            sec[key] = SECRET_MASK
    return data


def unmask_secrets(payload: Dict[str, Any], current: Config) -> Dict[str, Any]:
    cur = current.model_dump(mode="json")
    for section, key in SECRET_FIELDS:
        sec = payload.get(section)
        if isinstance(sec, dict) and sec.get(key) == SECRET_MASK:
            sec[key] = cur.get(section, {}).get(key, "")
    return payload


# ---- load / save -----------------------------------------------------------

@dataclass
class LoadedConfig:
    config: Config
    source: str            # "file" | "backup:<path>" | "defaults"
    error: Optional[str]   # why the primary file was not used, if it was not


def _backup_path(path: str, n: int) -> str:
    return f"{path}.bak.{n}"


def config_version(path: str | None = None) -> str:
    """Short content hash of the file on disk, used as an ETag for PUT If-Match."""
    path = path or CONFIG_PATH
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except OSError:
        return "0"


def _read(path: str) -> Config:
    with open(path, "r") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError("top level of config.yaml must be a mapping")
    return Config.model_validate(raw)


def load_config(path: str | None = None) -> LoadedConfig:
    path = path or CONFIG_PATH
    with _LOCK:
        if not os.path.exists(path):
            cfg = Config()
            _write(path, cfg)
            return LoadedConfig(cfg, "file", None)
        try:
            return LoadedConfig(_read(path), "file", None)
        except Exception as exc:  # noqa: BLE001
            error = f"{path}: {exc}"
            log.error("config unreadable, trying backups: %s", error)
        for n in range(1, BACKUPS + 1):
            bak = _backup_path(path, n)
            if not os.path.exists(bak):
                continue
            try:
                cfg = _read(bak)
                log.warning("running with backup config %s", bak)
                return LoadedConfig(cfg, f"backup:{bak}", error)
            except Exception as exc:  # noqa: BLE001
                log.error("backup %s unreadable: %s", bak, exc)
        log.error("no readable config; running with defaults (file left untouched)")
        return LoadedConfig(Config(), "defaults", error)


def save_config(cfg: Config, path: str | None = None) -> str:
    """Atomically write cfg, rotating backups. Returns the new version."""
    path = path or CONFIG_PATH
    with _LOCK:
        _write(path, cfg)
        return config_version(path)


def _fsync_dir(dirname: str) -> None:
    try:
        fd = os.open(dirname or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path: str, cfg: Config) -> None:
    dirname = os.path.dirname(path) or "."
    os.makedirs(dirname, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        yaml.safe_dump(cfg.model_dump(mode="json"), fh, sort_keys=False, default_flow_style=False)
        fh.flush()
        os.fsync(fh.fileno())
    if os.path.exists(path):
        # rotate: .bak.2 -> .bak.3, .bak.1 -> .bak.2, current -> .bak.1
        for n in range(BACKUPS, 1, -1):
            older = _backup_path(path, n - 1)
            if os.path.exists(older):
                os.replace(older, _backup_path(path, n))
        os.replace(path, _backup_path(path, 1))
    os.replace(tmp, path)
    _fsync_dir(dirname)
