"""FastAPI app: REST API, SSE event stream, and static frontend serving."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from .manager import DataManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

manager = DataManager()

STATIC_DIR = os.environ.get("HANGAR_STATIC", "/app/static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.start()
    yield
    await manager.stop()


app = FastAPI(title="The Pilot Channel", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "status": manager.status()}


@app.get("/admin")
async def admin_page():
    path = os.path.join(STATIC_DIR, "admin.html")
    if os.path.isfile(path):
        return FileResponse(path)
    raise HTTPException(status_code=404, detail="admin page not built")


@app.get("/api/config")
async def get_config():
    return manager.get_config().model_dump(mode="json")


@app.put("/api/config")
async def put_config(payload: dict):
    try:
        cfg = Config.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid config: {exc}")
    manager.update_config(cfg)
    return {"ok": True}


@app.get("/api/views")
async def get_views():
    return {"views": manager.views()}


@app.get("/api/traffic")
async def get_traffic(view: str = Query(...)):
    return await manager.get_traffic(view)


@app.get("/api/weather")
async def get_weather(ids: str = Query("")):
    icaos = [s for s in ids.split(",") if s] if ids else None
    return {"metars": manager.get_weather(icaos)}


@app.get("/api/weather/area")
async def get_weather_area(lat: float = Query(...), lon: float = Query(...), radius_nm: float = Query(...)):
    stations = await manager.get_area_weather(lat, lon, radius_nm)
    return {"stations": stations}


@app.get("/api/weather/bbox")
async def get_weather_bbox(
    min_lat: float = Query(...),
    min_lon: float = Query(...),
    max_lat: float = Query(...),
    max_lon: float = Query(...),
):
    stations = await manager.get_bbox_weather(min_lat, min_lon, max_lat, max_lon)
    return {"stations": stations}


@app.get("/api/status")
async def get_status():
    return manager.status()


@app.post("/api/test-source")
async def test_source(payload: dict):
    """Validate connectivity for a candidate data source without saving."""
    from .sources.aggregator import AggregatorSource
    from .sources.local import LocalReadsbSource

    mode = payload.get("mode", "auto")
    try:
        if mode == "local" or mode == "auto":
            src = LocalReadsbSource(manager.client, payload.get("local_url", ""))
            ac = await src.fetch(37.46, -122.12, 50)
            return {"ok": True, "source": "local", "count": len(ac)}
    except Exception as exc:  # noqa: BLE001
        if mode == "local":
            return {"ok": False, "source": "local", "error": str(exc)}
    try:
        src = AggregatorSource(manager.client, payload.get("aggregator", "adsbfi"), payload.get("api_key", ""))
        ac = await src.fetch(37.46, -122.12, 50)
        return {"ok": True, "source": f"aggregator:{payload.get('aggregator', 'adsbfi')}", "count": len(ac)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "source": "aggregator", "error": str(exc)}


@app.get("/api/stream")
async def stream(request: Request):
    """SSE: pushes config_changed / weather_updated so the kiosk hot-reloads."""
    q = manager.subscribe()

    async def gen():
        try:
            yield f"data: {json.dumps({'event': 'connected'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            manager.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


# Static frontend (built Vite assets). Mounted last so /api/* wins.
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
else:
    @app.get("/")
    async def no_frontend():
        return JSONResponse(
            {"message": "Frontend not built. Run the Vite build or use Docker.", "static_dir": STATIC_DIR}
        )
