"""Camera view: tile validation, the view descriptor, the go2rtc relay, and the
relay state in /api/status. Environment and the `client` fixture come from
support.py and conftest.py; HANGAR_GO2RTC points at a closed port."""
import asyncio
import json

import pytest
from starlette.websockets import WebSocketDisconnect
from support import base_cfg, put

from app import cameras
from app import main as main_mod


def cams_cfg(enabled=True, tiles=None):
    body = base_cfg()
    body["cameras"] = {
        "enabled": enabled,
        "label": "Hangar cameras",
        "dwell_s": 40,
        "tiles": [{"name": "Gate", "stream": "cam1", "cols": 2}, {"name": "", "stream": "cam3", "cols": 4}]
        if tiles is None
        else tiles,
    }
    return body


@pytest.mark.parametrize(
    "tile, loc",
    [
        ({"stream": "cam 1", "cols": 2}, ["cameras", "tiles", "0", "stream"]),
        ({"stream": "../x", "cols": 2}, ["cameras", "tiles", "0", "stream"]),
        ({"stream": "cam1", "cols": 7}, ["cameras", "tiles", "0", "cols"]),
        ({"stream": "cam1", "cols": 0}, ["cameras", "tiles", "0", "cols"]),
    ],
)
def test_tile_with_unsafe_stream_name_or_bad_span_is_rejected(client, tile, loc):
    r = put(client, cams_cfg(tiles=[tile]))
    assert r.status_code == 422, r.text
    assert loc in [e["loc"][: len(loc)] for e in r.json()["detail"]], r.json()


def test_camera_view_is_listed_last_only_when_enabled_with_tiles(client):
    assert put(client, cams_cfg()).status_code == 200
    views = client.get("/api/views").json()["views"]
    cam = views[-1]
    assert cam["id"] == "cameras" and cam["type"] == "cameras"
    assert cam["label"] == "Hangar cameras" and cam["dwell_s"] == 40
    assert cam["tiles"] == [
        {"name": "Gate", "stream": "cam1", "cols": 2},
        {"name": "cam3", "stream": "cam3", "cols": 4},  # an unnamed tile is captioned by its stream
    ]
    assert put(client, cams_cfg(enabled=False)).status_code == 200
    assert "cameras" not in [v["id"] for v in client.get("/api/views").json()["views"]]
    assert put(client, cams_cfg(tiles=[])).status_code == 200
    assert "cameras" not in [v["id"] for v in client.get("/api/views").json()["views"]]


def test_relay_refuses_a_stream_that_is_not_a_tile(client):
    assert put(client, cams_cfg()).status_code == 200
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/cameras/ws?stream=cam9"):
            pass
    assert exc.value.code == 1008


def test_relay_closes_when_go2rtc_is_unreachable(client):
    assert put(client, cams_cfg()).status_code == 200
    with client.websocket_connect("/api/cameras/ws?stream=cam1") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 1011


class FakeUpstream:
    """A go2rtc stand-in: answers the codec offer with a MIME type, then streams
    one binary segment, and records what the browser sent and whether it was closed."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self.out = asyncio.Queue()

    async def send(self, data):
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data).get("type") == "mse":
            await self.out.put(json.dumps({"type": "mse", "value": 'video/mp4; codecs="hvc1.1.6.L153.B0"'}))
            await self.out.put(b"\x00\x00\x00\x18ftypiso5")

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.out.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self):
        self.closed = True
        await self.out.put(None)


def test_relay_pumps_text_and_binary_frames_both_ways(client, monkeypatch):
    assert put(client, cams_cfg()).status_code == 200
    made = []

    async def fake_open(stream):
        made.append((stream, FakeUpstream()))
        return made[-1][1]

    monkeypatch.setattr(cameras, "open_upstream", fake_open)
    with client.websocket_connect("/api/cameras/ws?stream=cam3") as ws:
        ws.send_text(json.dumps({"type": "mse", "value": "hvc1.1.6.L153.B0"}))
        assert json.loads(ws.receive_text())["value"].startswith("video/mp4")
        assert ws.receive_bytes() == b"\x00\x00\x00\x18ftypiso5"
    assert made[0][0] == "cam3"
    up = made[0][1]
    assert up.sent == [json.dumps({"type": "mse", "value": "hvc1.1.6.L153.B0"})]
    assert up.closed  # the browser leaving closes the go2rtc side too


def test_status_reports_the_relay_state(client):
    assert put(client, cams_cfg(enabled=False)).status_code == 200
    assert client.get("/api/status").json()["cameras"] == {"enabled": False}
    assert put(client, cams_cfg()).status_code == 200
    cameras._status_cache["value"] = None  # fresh probe, not the cached answer
    cams = client.get("/api/status").json()["cameras"]
    assert cams["enabled"] is True and cams["tiles"] == 2
    assert cams["relay"] == "down" and cams["error"]
