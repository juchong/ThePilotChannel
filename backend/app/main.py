"""FastAPI app: REST API, SSE event stream, and static frontend serving."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import ICAO_RE, Config, mask_secrets, unknown_keys, unmask_secrets
from .manager import DataManager, TooManySubscribers
from .tiles import valid_tile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every upstream request at INFO (one line per second on a kiosk); keep warnings only.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("hangar.api")

manager = DataManager()

STATIC_DIR = os.environ.get("HANGAR_STATIC", "/app/static")
# Optional admin password. When set, the admin page and every mutating endpoint
# require HTTP Basic auth (any username). Read-only endpoints the kiosk uses stay open.
ADMIN_PASSWORD = os.environ.get("HANGAR_ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD") or ""

CODE_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if manager.config_error:
        log.error("config problem (running with %s): %s", manager.config_source, manager.config_error)
    if not ADMIN_PASSWORD:
        log.warning("HANGAR_ADMIN_PASSWORD is not set: /admin and config writes are open to the whole LAN")
    await manager.start()
    yield
    await manager.stop()


app = FastAPI(title="The Pilot Channel", version="0.2.0", lifespan=lifespan)


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
                elif path == "/" or path.endswith(".html") or path == "/admin":
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


# ---- auth ----------------------------------------------------------------------
_basic = HTTPBasic(auto_error=False)


async def require_admin(credentials: Optional[HTTPBasicCredentials] = Depends(_basic)):
    if not ADMIN_PASSWORD:
        return
    ok = credentials is not None and secrets.compare_digest(
        credentials.password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="admin password required",
            headers={"WWW-Authenticate": 'Basic realm="The Pilot Channel admin"'},
        )


def _errors(exc: ValidationError) -> List[dict]:
    return [{"loc": [str(x) for x in e["loc"]], "msg": e["msg"]} for e in exc.errors()]


# ---- routes ---------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True, "boot_id": manager.boot_id, "status": manager.status()}


@app.get("/admin", dependencies=[Depends(require_admin)])
async def admin_page():
    path = os.path.join(STATIC_DIR, "admin.html")
    if os.path.isfile(path):
        return FileResponse(path, headers={"Cache-Control": "no-cache"})
    raise HTTPException(status_code=404, detail="admin page not built")


@app.get("/api/config")
async def get_config(response: Response):
    data = mask_secrets(manager.get_config().model_dump(mode="json"))
    data["version"] = manager.config_version
    response.headers["ETag"] = f'"{manager.config_version}"'
    return data


@app.put("/api/config", dependencies=[Depends(require_admin)])
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
    payload = unmask_secrets(payload, manager.get_config())
    try:
        cfg = Config.model_validate(payload)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": _errors(exc)})
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


@app.get("/api/weather/area")
async def get_weather_area(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius_nm: float = Query(..., ge=1, le=500),
):
    return {"stations": await manager.get_area_weather(lat, lon, radius_nm)}


@app.get("/api/weather/bbox")
async def get_weather_bbox(
    min_lat: float = Query(..., ge=-90, le=90),
    min_lon: float = Query(..., ge=-180, le=180),
    max_lat: float = Query(..., ge=-90, le=90),
    max_lon: float = Query(..., ge=-180, le=180),
):
    if max_lat <= min_lat or max_lon <= min_lon:
        raise HTTPException(status_code=422, detail="max_lat/max_lon must exceed min_lat/min_lon")
    if (max_lat - min_lat) > 20 or (max_lon - min_lon) > 30:
        raise HTTPException(status_code=422, detail="bounding box too large (max 20 x 30 degrees)")
    return {"stations": await manager.get_bbox_weather(min_lat, min_lon, max_lat, max_lon)}


@app.get("/api/satellite")
async def get_satellite(
    sat: str = Query("G18", pattern=r"^(G16|G18|G19)$"),
    sector: str = Query("pnw", pattern=CODE_PATTERN),
    band: str = Query("GEOCOLOR", pattern=CODE_PATTERN),
    size: str = Query("1200x1200", pattern=r"^\d{3,4}x\d{3,4}$"),
    frames: int = Query(24, ge=1, le=60),
):
    return await manager.get_satellite(sat, sector, band, size, frames)


@app.get("/api/status")
async def get_status():
    return manager.status()


@app.get("/tiles/{z}/{x}/{y}.png")
async def get_tile(z: int, x: int, y: int):
    """Basemap tile from the on-disk cache (fetched from OpenStreetMap once)."""
    if not valid_tile(z, x, y):
        raise HTTPException(status_code=404, detail="no such tile")
    path = await manager.tiles.ensure(z, x, y)
    if path is None:
        raise HTTPException(status_code=502, detail="tile unavailable")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/test-source", dependencies=[Depends(require_admin)])
async def test_source(payload: dict):
    """Validate connectivity for a candidate data source without saving it."""
    payload = unmask_secrets({"data_source": dict(payload)}, manager.get_config())["data_source"]
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


@app.post("/api/display/blackout", dependencies=[Depends(require_admin)])
async def display_blackout(req: Optional[BlackoutRequest] = None):
    """Black out the kiosk picture for `seconds`, or until /api/display/restore."""
    req = req or BlackoutRequest()
    return manager.set_blackout(req.seconds, req.reason)


@app.post("/api/display/restore", dependencies=[Depends(require_admin)])
async def display_restore():
    return manager.restore_display()


def _sse(msg: dict) -> str:
    return f"data: {json.dumps(msg)}\n\n"


@app.get("/api/stream")
async def stream(request: Request):
    """SSE: config_changed / weather_updated / display so the kiosk hot-reloads
    and obeys remote blackouts. The `connected` event carries the backend boot
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
                    msg = await asyncio.wait_for(q.get(), timeout=20.0)
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
