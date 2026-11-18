"""FastAPI app: REST API, SSE event stream, and static frontend serving."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import access_api, apidoc, cameras
from .access import ACCESS, SESSION_COOKIE, require
from .access_api import NmsEnvironment
from .config import (
    ICAO_RE,
    STREAM_URL_MAX,
    Config,
    live_notams_problem,
    stream_host,
    unknown_keys,
    validate_source_url,
    validate_stream_url,
)
from .manager import DataManager, TooManySubscribers, UnknownView
from .radar import LAYER_RE
from .store import MAX_STREAMS, STORE, StoreLocked
from .tiles import valid_tile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every upstream request at INFO; keep warnings only.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("hangar.api")

manager = DataManager()

STATIC_DIR = os.environ.get("HANGAR_STATIC", "/app/static")

CODE_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"
CREDENTIAL_CHECK_S = 10.0   # the FAA is asked for a token on request at most this often

# Every route is in one access class (see access.py). The kiosk has no
# credentials, so what the display reads stays open.
control = require("control")
settings = require("settings")
session_only = require("session")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if manager.config_error:
        log.error("config problem (running with %s): %s", manager.config_source, manager.config_error)
    state = ACCESS.state()
    if state["locked"]:
        log.error("the secrets store cannot be read: the admin page and every protected route refuse until it is repaired")
    elif not state["account"]:
        log.warning("no admin account yet: the first visitor to /admin creates it")
    await manager.start()
    yield
    await manager.stop()


app = FastAPI(title="The Pilot Channel", version="0.3.0", lifespan=lifespan)
app.include_router(access_api.router)
access_api.changed.append(manager.secret_changed)


# ---- security headers (pure ASGI so it also wraps the SSE stream) -----------
class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                have = {k.lower() for k, _ in headers}
                # No Referrer-Policy header on purpose: the OpenStreetMap tile
                # servers require a Referer (their usage policy) and serve an
                # "Access denied" tile without one. The browser default
                # (strict-origin-when-cross-origin) sends the page origin.
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"SAMEORIGIN"),
                ]
                if path.startswith("/api/") or path == "/healthz":
                    extra.append((b"cache-control", b"no-store"))
                elif path == "/" or path.endswith(".html") or path in ("/admin", "/login"):
                    # The kiosk must always revalidate the page so a rebuilt
                    # bundle is picked up on reload; Chromium otherwise serves
                    # a heuristically cached index.html and keeps the old JS.
                    extra.append((b"cache-control", b"no-cache"))
                elif path.startswith("/assets/"):
                    extra.append((b"cache-control", b"public, max-age=31536000, immutable"))
                for k, v in extra:
                    if k not in have:
                        headers.append((k, v))
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(SecurityHeaders)


def _errors(exc: ValidationError) -> List[dict]:
    return [{"loc": [str(x) for x in e["loc"]], "msg": e["msg"]} for e in exc.errors()]


# ---- routes ---------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True, "boot_id": manager.boot_id, "status": manager.status()}


def _page(name: str):
    path = os.path.join(STATIC_DIR, name)
    if os.path.isfile(path):
        return FileResponse(path, headers={"Cache-Control": "no-cache"})
    raise HTTPException(status_code=404, detail="page not built")


@app.get("/admin")
def admin_page(request: Request):
    """The admin page, for a signed-in browser; anyone else is sent to sign in."""
    try:
        signed_in = ACCESS.session(request.cookies.get(SESSION_COOKIE)) is not None
    except StoreLocked:
        signed_in = False
    if not signed_in:
        return RedirectResponse("/login", status_code=303)
    return _page("admin.html")


@app.get("/login")
def login_page():
    return _page("login.html")


@app.get("/api/config")
async def get_config(response: Response):
    data = manager.get_config().model_dump(mode="json")
    data["version"] = manager.config_version
    response.headers["ETag"] = f'"{manager.config_version}"'
    return data


@app.put("/api/config", dependencies=[Depends(settings)])
async def put_config(request: Request, payload: dict):
    if_match = request.headers.get("if-match")
    if if_match and if_match.strip().strip('"') != manager.config_version:
        raise HTTPException(status_code=409, detail="config changed since it was loaded; reload the page and retry")
    payload = dict(payload)
    payload.pop("version", None)
    unknown = unknown_keys(payload, Config)
    if unknown:
        return JSONResponse(
            status_code=422,
            content={"detail": [{"loc": u.split("."), "msg": "unknown field"} for u in unknown]},
        )
    try:
        cfg = Config.model_validate(payload)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": _errors(exc)})
    problem = live_notams_problem(cfg)
    if problem:
        return JSONResponse(status_code=422, content={"detail": [problem]})
    version = await manager.update_config(cfg)
    return {"ok": True, "version": version}


@app.get("/api/views")
async def get_views():
    return {"views": manager.views()}


@app.get("/api/traffic")
async def get_traffic(view: str = Query(..., max_length=100)):
    snap = await manager.get_traffic(view)
    if snap is None:
        raise HTTPException(status_code=404, detail="unknown view")
    return snap


@app.get("/api/weather")
async def get_weather(ids: str = Query("", max_length=400)):
    icaos = [s.strip().upper() for s in ids.split(",") if s.strip()]
    icaos = [i for i in icaos if ICAO_RE.match(i)][:50]
    return {"metars": manager.get_weather(icaos or None)}


@app.get("/api/conditions", dependencies=[Depends(control)])
async def get_conditions():
    """The weather at every monitored airport and on every regional map, for automations."""
    return await manager.get_conditions()


@app.get("/api/weather/area")
async def get_weather_area(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius_nm: float = Query(..., ge=1, le=500),
):
    return await manager.get_area_weather(lat, lon, radius_nm)


@app.get("/api/weather/bbox")
async def get_weather_bbox(
    min_lat: float = Query(..., ge=-90, le=90),
    min_lon: float = Query(..., ge=-180, le=180),
    max_lat: float = Query(..., ge=-90, le=90),
    max_lon: float = Query(..., ge=-180, le=180),
    view: Optional[str] = Query(None, max_length=100),
):
    if max_lat <= min_lat or max_lon <= min_lon:
        raise HTTPException(status_code=422, detail="max_lat/max_lon must exceed min_lat/min_lon")
    if (max_lat - min_lat) > 20 or (max_lon - min_lon) > 30:
        raise HTTPException(status_code=422, detail="bounding box too large (max 20 x 30 degrees)")
    # the regional view names itself: its map is what the readout lists for that region
    manager.note_region_box(view, (min_lat, min_lon, max_lat, max_lon))
    return await manager.get_bbox_weather(min_lat, min_lon, max_lat, max_lon)


@app.get("/api/satellite")
async def get_satellite(
    sat: str = Query("G18", pattern=r"^(G16|G18|G19)$"),
    sector: str = Query("pnw", pattern=CODE_PATTERN),
    band: str = Query("GEOCOLOR", pattern=CODE_PATTERN),
    size: str = Query("1200x1200", pattern=r"^\d{3,4}x\d{3,4}$"),
    frames: int = Query(24, ge=1, le=60),
):
    return await manager.get_satellite(sat, sector, band, size, frames)


@app.get("/api/notams")
async def get_notams(location: str = Query(..., min_length=3, max_length=4)):
    """The NOTAMs in effect or upcoming for a monitored location, from the cache."""
    data = manager.get_notams(location.strip().upper())
    if data is None:
        raise HTTPException(status_code=404, detail="location is not monitored")
    return data


class NotamCheck(BaseModel):
    ident: str = Field(..., min_length=3, max_length=4)
    source: Literal["faa", "sample"] = "faa"


@app.post("/api/notams/check", dependencies=[Depends(settings)])
async def notams_check(req: NotamCheck):
    """Sample data: what it holds for a location. The FAA: fetch a monitored
    location's whole set at the next turn the pull limit allows."""
    ident = req.ident.strip().upper()
    if not ICAO_RE.match(ident):
        raise HTTPException(status_code=422, detail="identifier must be 3 or 4 letters or digits")
    return await manager.check_notam_location(ident, req.source)


class ModelTest(BaseModel):
    base_url: str = Field(..., min_length=8, max_length=300)
    model: str = Field(..., min_length=1, max_length=120)
    timeout_s: int = Field(60, ge=5, le=600)


@app.post("/api/notams/model-test", dependencies=[Depends(settings)])
async def notams_model_test(req: ModelTest):
    """Ask a model endpoint to rewrite one FAA example NOTAM and check the answer; saves nothing."""
    try:
        url = validate_source_url(req.base_url.strip().rstrip("/"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return await manager.test_model(url, req.model.strip(), req.timeout_s)


@app.get("/api/notams/usage", dependencies=[Depends(settings)])
async def notams_usage(limit: Optional[int] = Query(None, ge=1, le=10000), download: bool = False):
    """Every request sent to the NMS-API (time, kind, status, size, seconds since
    the previous data pull) with totals for the last 24 hours and for the record."""
    data = manager.notam_usage(limit)
    if download:
        name = time.strftime("nms-usage-%Y%m%dT%H%MZ.json", time.gmtime())
        return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return data


_credential_check = {"at": 0.0}


@app.post("/api/secrets/nms/{environment}/check", dependencies=[Depends(session_only)])
async def secrets_check_nms(environment: NmsEnvironment):
    """Ask an environment for a token with the pair stored for it. Costs no data pull."""
    wait = CREDENTIAL_CHECK_S - (time.monotonic() - _credential_check["at"])
    if _credential_check["at"] and wait > 0:
        raise HTTPException(status_code=429, detail=f"wait {round(wait)} s before checking again",
                            headers={"Retry-After": str(round(wait) or 1)})
    _credential_check["at"] = time.monotonic()
    return await manager.check_nms_credentials(environment)


@app.get("/api/status")
async def get_status():
    data = manager.status()
    cams = manager.get_config().cameras
    data["cameras"] = (
        {"enabled": True, **(await cameras.status([t.stream for t in cams.tiles]))}
        if cams.enabled
        else {"enabled": False}
    )
    return data


@app.websocket("/api/cameras/ws")
async def cameras_ws(ws: WebSocket, stream: str = Query(..., max_length=64)):
    """Relay one tile's MSE stream from go2rtc. Open like the read endpoints (the
    kiosk has no credentials); only streams configured as tiles are relayed."""
    if stream not in {t.stream for t in manager.get_config().cameras.tiles}:
        await ws.close(code=1008)
        return
    await ws.accept()
    await cameras.proxy(ws, stream)


@app.get("/tiles/{z}/{x}/{y}.png")
async def get_tile(z: int, x: int, y: int):
    """Basemap tile from the on-disk cache (fetched from OpenStreetMap once)."""
    if not valid_tile(z, x, y):
        raise HTTPException(status_code=404, detail="no such tile")
    path = await manager.tiles.ensure(z, x, y)
    if path is None:
        raise HTTPException(status_code=502, detail="tile unavailable")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/radar/{layer}/{z}/{x}/{y}.png")
async def get_radar_tile(layer: str, z: int, x: int, y: int, v: int = 0):
    """NEXRAD tile from IEM with returns below radar.min_dbz removed, cached per bucket."""
    if not LAYER_RE.match(layer) or not valid_tile(z, x, y):
        raise HTTPException(status_code=404, detail="no such tile")
    data = await manager.radar.get(layer, z, x, y, v, manager.get_config().radar.min_dbz)
    if data is None:
        raise HTTPException(status_code=502, detail="radar tile unavailable")
    return Response(content=data, media_type="image/png", headers={"Cache-Control": "public, max-age=300"})


# ---- camera stream addresses ---------------------------------------------------------
class NewStream(BaseModel):
    name: str = Field(max_length=40)
    url: str = Field(max_length=STREAM_URL_MAX)


class StreamChange(BaseModel):
    name: Optional[str] = Field(None, max_length=40)
    url: Optional[str] = Field(None, max_length=STREAM_URL_MAX)


class StreamTest(BaseModel):
    id: Optional[str] = Field(None, max_length=64)
    url: Optional[str] = Field(None, max_length=STREAM_URL_MAX)


def _stream_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise HTTPException(status_code=422, detail=[{"loc": ["name"], "msg": "give the stream a name"}])
    return name


def _stream_url(url: str) -> str:
    try:
        return validate_stream_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=[{"loc": ["url"], "msg": str(exc)}])


def _stream_view(entry: dict) -> dict:
    """A stored stream as the API shows it: the address is reduced to its host."""
    tiles = [t.name or t.stream for t in manager.get_config().cameras.tiles if t.stream == entry["id"]]
    return {"id": entry["id"], "name": entry["name"], "host": stream_host(entry["url"]),
            "changed": entry.get("changed"), "tiles": tiles}


@app.get("/api/cameras/streams", dependencies=[Depends(session_only)])
async def streams_list():
    state = await cameras.status([])
    pending = set(state.get("pending_streams") or [])
    return {
        "streams": [{**_stream_view(s), "pending": s["id"] in pending} for s in STORE.streams()],
        "relay": state["relay"],
        "error": state["error"],
        "max": MAX_STREAMS,
    }


@app.post("/api/cameras/streams", dependencies=[Depends(session_only)])
async def streams_add(req: NewStream):
    name, url = _stream_name(req.name), _stream_url(req.url)
    try:
        entry = await asyncio.to_thread(STORE.add_stream, name, url)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    cameras.request_sync()
    return _stream_view(entry)


@app.put("/api/cameras/streams/{stream_id}", dependencies=[Depends(session_only)])
async def streams_change(stream_id: str, req: StreamChange):
    name = _stream_name(req.name) if req.name is not None else None
    url = _stream_url(req.url) if req.url is not None else None
    entry = await asyncio.to_thread(STORE.update_stream, stream_id, name, url)
    if entry is None:
        raise HTTPException(status_code=404, detail="no such stream")
    if url is not None:
        cameras.request_sync()
    return _stream_view(entry)


@app.delete("/api/cameras/streams/{stream_id}", dependencies=[Depends(session_only)])
async def streams_remove(stream_id: str):
    entry = STORE.stream(stream_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="no such stream")
    used = _stream_view(entry)["tiles"]
    if used:
        raise HTTPException(status_code=409, detail=f"used by the tile {', '.join(used)}: remove it and save first")
    await asyncio.to_thread(STORE.remove_stream, stream_id)
    cameras.request_sync()
    return {"ok": True}


_stream_test = asyncio.Lock()


@app.post("/api/cameras/streams/test", dependencies=[Depends(session_only)])
async def streams_test(req: StreamTest):
    """Have the relay open a stored stream (`id`) or an address not stored yet
    (`url`) and say what video it sends."""
    if (req.id is None) == (req.url is None):
        raise HTTPException(status_code=422, detail="send either id or url")
    if req.id is not None and STORE.stream(req.id) is None:
        raise HTTPException(status_code=404, detail="no such stream")
    url = _stream_url(req.url) if req.url is not None else None
    async with _stream_test:
        return await cameras.test(url=url, stream=req.id)


@app.post("/api/test-source", dependencies=[Depends(settings)])
async def test_source(payload: dict):
    """Validate connectivity for a candidate data source without saving it."""
    payload = dict(payload)
    try:
        return await manager.test_source(payload)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": _errors(exc)})


# ---- remote display control (e.g. Home Assistant automations) ---------------------
class BlackoutRequest(BaseModel):
    seconds: Optional[float] = Field(None, gt=0, le=86400)  # omit to hold until restored
    reason: Optional[str] = Field(None, max_length=120)


@app.get("/api/display")
async def get_display():
    return manager.display_state()


@app.post("/api/display/blackout", dependencies=[Depends(control)])
async def display_blackout(req: Optional[BlackoutRequest] = None):
    """Black out the kiosk picture for `seconds`, or until /api/display/restore."""
    req = req or BlackoutRequest()
    return manager.set_blackout(req.seconds, req.reason)


@app.post("/api/display/restore", dependencies=[Depends(control)])
async def display_restore():
    return manager.restore_display()


class NavigateRequest(BaseModel):
    view: str = Field(..., max_length=100)  # a view id from /api/views


class ShowingReport(BaseModel):
    view: str = Field(..., max_length=100)
    index: Optional[int] = Field(None, ge=0)


@app.post("/api/display/next", dependencies=[Depends(control)])
async def display_next():
    """Move the display on to the next view in its cycle."""
    return manager.navigate("next")


@app.post("/api/display/previous", dependencies=[Depends(control)])
async def display_previous():
    """Move the display back to the previous view in its cycle."""
    return manager.navigate("previous")


@app.post("/api/display/view", dependencies=[Depends(control)])
async def display_view(req: NavigateRequest):
    """Jump the display to a view by id (see /api/views)."""
    try:
        return manager.navigate("view", req.view)
    except UnknownView:
        raise HTTPException(status_code=404, detail="unknown view")


@app.post("/api/display/showing")
async def display_showing(req: ShowingReport):
    """The display reports the view it has just switched to. Open like the read
    endpoints: the kiosk has no credentials, and the report only names a view."""
    try:
        return manager.report_showing(req.view, req.index)
    except UnknownView:
        raise HTTPException(status_code=404, detail="unknown view")


@app.get("/api/docs/display", dependencies=[Depends(session_only)])
async def display_api_doc(request: Request, download: bool = False):
    """The display API reference, filled in with this display's address and views."""
    text = apidoc.render(str(request.base_url), manager.views())
    headers = {"Content-Disposition": f'attachment; filename="{apidoc.FILENAME}"'} if download else {}
    return Response(text, media_type="text/markdown; charset=utf-8", headers=headers)


SSE_KEEPALIVE_S = 20.0  # a comment line after this long without an event


def _sse(msg: dict) -> str:
    return f"data: {json.dumps(msg)}\n\n"


@app.get("/api/stream")
async def stream(request: Request):
    """SSE: config_changed / weather_updated / notams_updated / display so the kiosk hot-reloads
    and obeys remote blackouts and navigation. The `connected` event carries the backend boot
    id (so a client can tell when the backend was rebuilt and reload itself)
    and the current display state (so a reconnecting kiosk stays in sync)."""
    try:
        q = manager.subscribe()
    except TooManySubscribers:
        raise HTTPException(status_code=503, detail="too many event subscribers")

    async def gen():
        try:
            yield _sse({
                "event": "connected",
                "boot_id": manager.boot_id,
                "version": manager.config_version,
                "display": manager.display_state(),
            })
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_S)
                    yield _sse(msg)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            manager.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- static frontend (built Vite assets); mounted last so /api/* wins ------------
class AppStatic(StaticFiles):
    """Serve the built frontend but never admin.html directly: the admin page
    is only reachable through /admin so the password check cannot be bypassed."""

    async def get_response(self, path: str, scope):
        if path.replace("\\", "/").strip("/") == "admin.html":
            raise StarletteHTTPException(status_code=404)
        return await super().get_response(path, scope)


if os.path.isdir(STATIC_DIR):
    app.mount("/", AppStatic(directory=STATIC_DIR, html=True), name="static")
else:
    @app.get("/")
    async def no_frontend():
        return JSONResponse(
            {"message": "Frontend not built. Run the Vite build or use Docker.", "static_dir": STATIC_DIR}
        )
