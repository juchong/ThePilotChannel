"""Live camera tiles: the browser's MSE WebSocket is relayed to go2rtc, which pulls
the camera streams and serves them as fragmented MP4 without re-encoding.

The stream addresses, with their access tokens, live in the secrets store. go2rtc
keeps streams in memory only and publishes no port, so this module gives it the
stored addresses through its API (at start, after a change, and again whenever it
has lost them) and relays only the streams configured as tiles. No address is
ever put into a response, a log line or an error.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from typing import Dict, List, Optional, Set
from urllib.parse import quote

import httpx
import websockets
from starlette.websockets import WebSocket, WebSocketState

from .store import STORE

log = logging.getLogger("hangar.cameras")

GO2RTC_URL = os.environ.get("HANGAR_GO2RTC", "http://go2rtc:1984").rstrip("/")
UPSTREAM_OPEN_TIMEOUT_S = 5.0
STATUS_TTL_S = 10.0
SYNC_INTERVAL_S = 30.0       # how often the relay's streams are compared with the store
API_TIMEOUT_S = 4.0
PROBE_TIMEOUT_S = 20.0       # a test waits this long for the camera to answer
TEST_PREFIX = "test-"
ADDRESS_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)

_status_cache: Dict[str, object] = {"ts": 0.0, "value": None}
_sockets: Dict[str, Set[WebSocket]] = {}     # stream id -> browser sockets being relayed
_testing: Set[str] = set()                   # temporary streams of tests in progress
_sync: Dict[str, object] = {"state": None, "error": None, "missing": []}
_wake: Optional[asyncio.Event] = None


def _client(timeout) -> httpx.AsyncClient:
    """An HTTP client for the relay's API."""
    return httpx.AsyncClient(timeout=timeout)


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
        log.warning("camera relay for %s unavailable: %s", stream, exc.__class__.__name__)
        request_sync()      # the relay may have restarted and lost its streams
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

    _sockets.setdefault(stream, set()).add(ws)
    tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        _sockets.get(stream, set()).discard(ws)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await upstream.close()
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass


async def close_stream(stream: str) -> None:
    """End the relays of one stream, so its tiles reconnect onto its new address."""
    for ws in list(_sockets.get(stream, ())):
        try:
            await ws.close(code=1012, reason="stream address changed")
        except Exception:  # noqa: BLE001
            pass


# ---- the relay's streams ------------------------------------------------------------
async def fetch_streams() -> Dict[str, dict]:
    """go2rtc's stream list, name -> {producers, consumers}. It carries the
    addresses: nothing of it but names may leave this module."""
    async with _client(API_TIMEOUT_S) as client:
        r = await client.get(f"{GO2RTC_URL}/api/streams")
        if r.status_code >= 400:
            raise RuntimeError(f"relay answered HTTP {r.status_code}")
        data = r.json()
        return data if isinstance(data, dict) else {}


def _sources(entry: Optional[dict]) -> List[str]:
    return [str(p.get("url")) for p in (entry or {}).get("producers") or [] if isinstance(p, dict)]


async def _set(client: httpx.AsyncClient, name: str, url: str) -> None:
    r = await client.patch(f"{GO2RTC_URL}/api/streams", params={"name": name, "src": url})
    if r.status_code >= 400:
        raise RuntimeError(f"relay refused stream {name} (HTTP {r.status_code})")


async def _drop(client: httpx.AsyncClient, name: str) -> None:
    # The relay removes the stream, then answers 400 because its config file is
    # read-only; whether it is gone is judged from its stream list.
    await client.delete(f"{GO2RTC_URL}/api/streams", params={"src": name})


async def sync() -> Dict[str, object]:
    """Make the relay's streams those of the store: set what is missing or has
    another address, drop what the store does not hold. -> the sync state."""
    wanted = {s["id"]: s["url"] for s in STORE.streams()}
    try:
        known = await fetch_streams()
        set_now = [name for name, url in wanted.items() if _sources(known.get(name)) != [url]]
        drop_now = [name for name in known if name not in wanted and name not in _testing]
        if set_now or drop_now:
            async with _client(API_TIMEOUT_S) as client:
                for name in set_now:
                    await _set(client, name, wanted[name])
                for name in drop_now:
                    await _drop(client, name)
            for name in set_now:
                if name in known:       # an address was replaced under running relays
                    await close_stream(name)
            known = await fetch_streams()
        missing = sorted(name for name, url in wanted.items() if _sources(known.get(name)) != [url])
        error = f"relay did not take: {', '.join(missing)}" if missing else None
        state = "ok"
    except Exception as exc:  # noqa: BLE001  (httpx errors carry the request URL: only the class is kept)
        state, missing = "down", sorted(wanted)
        error = str(exc) if isinstance(exc, RuntimeError) else exc.__class__.__name__
    if (state, error) != (_sync["state"], _sync["error"]):
        if error:
            log.error("camera relay: %s", error)
        elif _sync["state"] is not None:
            log.info("camera relay in step with the stored streams")
    _sync.update(state=state, error=error, missing=missing)
    _status_cache["value"] = None
    return dict(_sync)


def request_sync() -> None:
    if _wake is not None:
        _wake.set()


async def sync_loop() -> None:
    """Keep the relay in step with the store. With no stored streams the relay
    is only contacted after a change."""
    global _wake
    _wake = asyncio.Event()
    if STORE.streams():
        _wake.set()
    while True:
        try:
            await asyncio.wait_for(_wake.wait(), timeout=SYNC_INTERVAL_S)
        except asyncio.TimeoutError:
            if not STORE.streams() and _sync["state"] != "down":
                continue
        _wake.clear()
        try:
            await sync()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001  (never let the loop die)
            log.error("camera relay sync failed: %s", exc.__class__.__name__)


def _plain(text: str, url: str = "") -> str:
    """An error text with every address taken out."""
    if url:
        text = text.replace(url, "the address")
    return ADDRESS_RE.sub("the address", " ".join(text.split()))[:200]


async def test(url: Optional[str] = None, stream: Optional[str] = None) -> Dict[str, object]:
    """Have the relay open a camera and report what it sends: a stored stream by
    id, or an address not stored yet (under a temporary name)."""
    name = stream
    async with _client(httpx.Timeout(PROBE_TIMEOUT_S, connect=API_TIMEOUT_S)) as client:
        try:
            if url is not None:
                name = TEST_PREFIX + secrets.token_hex(4)
                _testing.add(name)
                await _set(client, name, url)
            r = await client.get(f"{GO2RTC_URL}/api/streams", params={"src": name, "video": "all"})
            if r.status_code == 404:
                return {"ok": False, "error": "the relay does not have this stream yet; try again in a moment"}
            if r.status_code >= 400:
                return {"ok": False, "error": _plain(r.text, url or "") or f"relay answered HTTP {r.status_code}"}
            medias: List[str] = []
            for producer in (r.json() or {}).get("producers") or []:
                medias += [str(m) for m in producer.get("medias") or []]
            video = next((m.split(",")[-1].strip() for m in medias if m.lower().startswith("video")), None)
            if video is None:
                return {"ok": False, "error": "the camera answered but sends no video"}
            return {"ok": True, "video": video, "error": None}
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"camera relay unreachable ({exc.__class__.__name__})"}
        finally:
            if url is not None and name:
                try:
                    await _drop(client, name)
                except Exception:  # noqa: BLE001
                    pass
                _testing.discard(name)


async def status(tile_streams: List[str]) -> Dict:
    """Whether the relay answers and holds the stored streams, and which tiles
    name a stream that is not stored, cached for STATUS_TTL_S. Only stream ids
    are returned."""
    now = time.monotonic()
    if _status_cache["value"] is None or now - float(_status_cache["ts"]) > STATUS_TTL_S:
        stored = [s["id"] for s in STORE.streams()]
        try:
            known = await fetch_streams()
            wanted = {s["id"]: s["url"] for s in STORE.streams()}
            value = {
                "relay": "ok",
                "error": None,
                "pending_streams": sorted(n for n, url in wanted.items() if _sources(known.get(n)) != [url]),
            }
        except Exception as exc:  # noqa: BLE001
            value = {
                "relay": "down",
                "error": (str(exc) if isinstance(exc, RuntimeError) else exc.__class__.__name__)[:200],
                "pending_streams": [],
            }
        value["streams"] = len(stored)
        _status_cache["ts"] = now
        _status_cache["value"] = value
        if value["pending_streams"]:
            request_sync()
    stored_ids = {s["id"] for s in STORE.streams()}
    return {
        "tiles": len(tile_streams),
        "unknown_streams": sorted({s for s in tile_streams if s not in stored_ids}),
        **_status_cache["value"],  # type: ignore[dict-item]
    }
