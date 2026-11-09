"""Live camera tiles: the browser's MSE WebSocket is relayed to go2rtc, which pulls
the camera streams and serves them as fragmented MP4 without re-encoding.

go2rtc publishes no port and takes the camera URLs, with their access tokens, from
its environment (.env), so the browser never talks to it directly: only the stream
names configured as tiles are reachable, through this relay.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, List
from urllib.parse import quote

import httpx
import websockets
from starlette.websockets import WebSocket, WebSocketState

log = logging.getLogger("hangar.cameras")

GO2RTC_URL = os.environ.get("HANGAR_GO2RTC", "http://go2rtc:1984").rstrip("/")
UPSTREAM_OPEN_TIMEOUT_S = 5.0
STATUS_TTL_S = 10.0

_status_cache: Dict[str, object] = {"ts": 0.0, "value": None}


def ws_url(stream: str) -> str:
    base = GO2RTC_URL.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    return f"{base}/api/ws?src={quote(stream, safe='')}"


async def open_upstream(stream: str):
    """Connect to go2rtc's MSE WebSocket for one stream (no frame size limit:
    a fragmented-MP4 segment can be large)."""
    return await websockets.connect(ws_url(stream), max_size=None, open_timeout=UPSTREAM_OPEN_TIMEOUT_S)


async def proxy(ws: WebSocket, stream: str) -> None:
    """Pump frames both ways between an accepted browser socket and go2rtc until
    either side closes. The browser side reconnects with its own backoff."""
    try:
        upstream = await open_upstream(stream)
    except Exception as exc:  # noqa: BLE001
        log.warning("camera relay for %s unavailable: %s", stream, exc)
        await ws.close(code=1011, reason="camera relay unavailable")
        return

    async def to_upstream():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])
            elif msg.get("text") is not None:
                await upstream.send(msg["text"])

    async def to_browser():
        async for frame in upstream:
            if isinstance(frame, (bytes, bytearray)):
                await ws.send_bytes(bytes(frame))
            else:
                await ws.send_text(frame)

    tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await upstream.close()
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass


async def fetch_streams() -> Dict[str, dict]:
    """go2rtc's stream list, name -> {producers, consumers}; it masks the URLs."""
    async with httpx.AsyncClient(timeout=2.0) as client:
        r = await client.get(f"{GO2RTC_URL}/api/streams")
        r.raise_for_status()
        return r.json()


async def status(streams: List[str]) -> Dict:
    """Whether go2rtc answers, which configured tile streams it does not know, and which
    it knows but has no camera URL for (an unset .env slot), cached for STATUS_TTL_S so
    status polls do not hammer it. Only stream names are returned."""
    now = time.monotonic()
    if _status_cache["value"] is None or now - float(_status_cache["ts"]) > STATUS_TTL_S:
        try:
            known = await fetch_streams()
            value = {
                "relay": "ok",
                "error": None,
                "unknown_streams": [s for s in streams if s not in known],
                "empty_streams": [s for s in streams if s in known and not (known[s] or {}).get("producers")],
            }
        except Exception as exc:  # noqa: BLE001
            value = {
                "relay": "down",
                "error": f"{type(exc).__name__}: {exc}"[:200],
                "unknown_streams": [],
                "empty_streams": [],
            }
        _status_cache["ts"] = now
        _status_cache["value"] = value
    return {"tiles": len(streams), **_status_cache["value"]}  # type: ignore[dict-item]
