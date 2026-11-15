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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger("hangar.config")

CONFIG_PATH = os.environ.get("HANGAR_CONFIG", "/data/config.yaml")
BACKUPS = 3
_LOCK = threading.Lock()

# IEM publishes NEXRAD time-lagged layers every 5 minutes up to -m55m.
RADAR_MAX_AGE_MIN = 55

ICAO_RE = re.compile(r"^[A-Z0-9]{3,4}$")
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
SIZE_RE = re.compile(r"^\d{3,4}x\d{3,4}$")
STREAM_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# The camera view lays its tiles out on a grid this many columns wide.
CAMERA_GRID_COLS = 6
CAMERA_MAX_TILES = 12

NOTAM_MAX_LOCATIONS = 12
# The FAA allows one data pull every three minutes.
NOTAM_MIN_REFRESH_S = 180

# The NMS-API environments, by the host the FAA names for each.
NMS_HOSTS = {
    "production": "https://api-nms.aim.faa.gov",
    "staging": "https://api-staging.cgifederal-aim.com",
    "fit": "https://api-fit.cgifederal-aim.com",
}

# A camera address may only use these schemes: the relay also accepts sources
# that run a program (exec, echo, expr, ffmpeg), which a web form must never reach.
STREAM_SCHEMES = ("rtsp", "rtsps")
STREAM_URL_MAX = 500


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


def validate_stream_url(url: str) -> str:
    """A camera stream address the relay will open: rtsp or rtsps only, with a
    host that is not link-local, and no fragment (the relay reads options from
    it). UniFi Protect's `enableSrtp` parameter is removed: the relay does not
    decrypt SRTP, and rtsps is carried inside TLS without it."""
    url = (url or "").strip()
    if not url or len(url) > STREAM_URL_MAX:
        raise ValueError(f"address must be 1 to {STREAM_URL_MAX} characters")
    if any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("address must not contain spaces or control characters")
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme not in STREAM_SCHEMES:
        raise ValueError("address must start with rtsp:// or rtsps://")
    parts = urlsplit(url)
    if not parts.netloc or not parts.hostname:
        raise ValueError("address must include a host")
    if parts.fragment or "#" in url:
        raise ValueError("address must not contain '#'")
    try:
        ip = ipaddress.ip_address(parts.hostname)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_link_local or ip.is_multicast or ip.is_unspecified):
        raise ValueError("address must not use a link-local or multicast address")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "enablesrtp"]
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), ""))


def stream_host(url: str) -> str:
    """The host and port of a stream address: the only part of it shown again."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{parts.port}" if parts.port else host


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
    # Dissolve between frames instead of stepping. Off by default: the fade
    # forces a full map re-render for its whole duration.
    crossfade: bool = False
    # Returns weaker than this are not drawn (the composite's weak returns
    # are mostly not precipitation).
    min_dbz: float = Field(10, ge=-30, le=60)

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


class CameraTile(BaseModel):
    """One tile of the camera grid: a go2rtc stream name and the number of grid
    columns it spans. The tile shows the stream at its own aspect ratio inside a
    cell whose height is that of a 16:9 two-column cell."""
    name: str = Field("", max_length=40)
    stream: str = Field(min_length=1, max_length=64)
    cols: int = Field(2, ge=1, le=CAMERA_GRID_COLS)

    @field_validator("stream")
    @classmethod
    def _stream(cls, v: str) -> str:
        v = v.strip()
        if not STREAM_RE.match(v):
            raise ValueError("stream name may use letters, digits, '.', '_' and '-' only")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return v.strip()


class Cameras(BaseModel):
    """Live camera view: a grid of tiles playing relayed streams. A tile names a
    stream; the stream addresses (with their access tokens) live in the secrets
    store, never here."""
    enabled: bool = False
    label: str = Field("Cameras", max_length=80)
    dwell_s: int = Field(30, ge=5, le=3600)
    tiles: List[CameraTile] = Field(default_factory=list, max_length=CAMERA_MAX_TILES)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        return v.strip() or "Cameras"


class NotamLocation(BaseModel):
    """A location whose NOTAMs are shown: the identifier of the affected facility
    (an airport, a NAVAID or an ARTCC), in its domestic or ICAO form."""
    ident: str = Field(min_length=3, max_length=4)
    name: str = Field("", max_length=80)
    enabled: bool = True

    @field_validator("ident")
    @classmethod
    def _ident(cls, v: str) -> str:
        v = v.strip().upper()
        if not ICAO_RE.match(v):
            raise ValueError("identifier must be 3 or 4 letters or digits")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return v.strip()


class NotamTranslate(BaseModel):
    """Rewriting NOTAMs with a locally hosted model through an OpenAI-compatible
    endpoint. An answer is shown only if it passes the check in notam_translate;
    the API key lives in the secrets store, never here. `description` names the
    model on the display ("FAA NMS Processed with <description> LLM")."""
    enabled: bool = False
    base_url: str = Field("", max_length=300)     # e.g. https://llm.example.net/v1
    model: str = Field("", max_length=120)
    description: str = Field("", max_length=60)   # e.g. Qwen 3.8 27B
    timeout_s: int = Field(60, ge=5, le=600)

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        return validate_source_url(v) if v else v

    @field_validator("model", "description")
    @classmethod
    def _model(cls, v: str) -> str:
        return " ".join(v.split())

    @model_validator(mode="after")
    def _complete(self):
        if self.enabled and not (self.base_url and self.model):
            raise ValueError("an endpoint URL and a model name are needed to enable the model rewrite")
        return self


class Notams(BaseModel):
    """NOTAM views, one per monitored location. A NOTAM is shown while it is in
    effect or when it takes effect within `lookahead_h` hours. `source` "sample"
    shows invented NOTAMs, marked as such, for previewing the view; the FAA
    credentials live in the secrets store, never here. `environment` picks the
    NMS-API host; anything but production is a test environment."""
    enabled: bool = False
    source: Literal["faa", "sample"] = "faa"
    environment: Literal["production", "staging", "fit"] = "production"
    label: str = Field("NOTAMs", max_length=80)
    dwell_s: int = Field(30, ge=5, le=3600)
    lookahead_h: int = Field(24, ge=0, le=720)
    refresh_s: int = Field(NOTAM_MIN_REFRESH_S, le=3600)
    locations: List[NotamLocation] = Field(default_factory=list, max_length=NOTAM_MAX_LOCATIONS)
    translate: NotamTranslate = Field(default_factory=NotamTranslate)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        return v.strip() or "NOTAMs"

    @field_validator("refresh_s", mode="before")
    @classmethod
    def _refresh(cls, v):
        # A file saved with a shorter interval still loads: the value is raised.
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v < NOTAM_MIN_REFRESH_S:
            return NOTAM_MIN_REFRESH_S
        return v

    @model_validator(mode="after")
    def _unique(self):
        seen = set()
        for loc in self.locations:
            if loc.ident in seen:
                raise ValueError(f"duplicate NOTAM location {loc.ident}")
            seen.add(loc.ident)
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
    cameras: Cameras = Field(default_factory=Cameras)
    notams: Notams = Field(default_factory=Notams)

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


# ---- live NOTAM views ----------------------------------------------------------

def live_notams_problem(cfg: "Config") -> Optional[Dict[str, Any]]:
    """Why live NOTAM views cannot be shown with this config, as {loc, msg}, or
    None. The display names its NOTAM source "FAA NMS Processed with <description>
    LLM", so live views need the model rewrite and its description. Checked when a
    config is saved and when the views are built, never when a file is loaded."""
    n = cfg.notams
    if not n.enabled or n.source != "faa":
        return None
    if not n.translate.enabled:
        return {"loc": ["notams", "translate", "enabled"],
                "msg": "live NOTAM views need the model rewrite: enable it, or turn the NOTAM views off"}
    if not n.translate.description:
        return {"loc": ["notams", "translate", "description"],
                "msg": "describe the model (for example \"Qwen 3.8 27B\"): the display names it as the source"}
    return None


def notam_source_label(cfg: "Config") -> str:
    """How the display names the source of its NOTAMs."""
    if cfg.notams.source == "sample":
        return "sample data"
    return f"FAA NMS Processed with {cfg.notams.translate.description} LLM"


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


def read_raw(path: str | None = None) -> Dict[str, Any]:
    """The config file as written, unvalidated ({} when unreadable): what a file
    from an earlier version still carries."""
    try:
        with open(path or CONFIG_PATH, "r") as fh:
            raw = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


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
