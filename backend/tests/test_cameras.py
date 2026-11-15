"""Camera view: tile validation, the view descriptor, the go2rtc relay, the
stored stream addresses and keeping the relay in step with them, and the relay
state in /api/status. Environment and the `client` fixture come from support.py
and conftest.py; HANGAR_GO2RTC points at a closed port, and tests that need a
relay install the stand-in below."""
import asyncio
import json
import logging

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect
from support import AUTH, base_cfg, put, sign_in, wait_until

from app import cameras
from app import main as main_mod
from app.config import STREAM_URL_MAX, stream_host, validate_stream_url
from app.store import MAX_STREAMS, STORE


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
    assert cams["unknown_streams"] == ["cam1", "cam3"] and cams["pending_streams"] == []   # no stream is stored


# ---- stream addresses -------------------------------------------------------------------------

# Every source scheme go2rtc 1.9.15 registers (its /api/schemes on the kiosk). Some
# of them run a program (exec, echo, expr, ffmpeg): an address from a web form may
# only be rtsp or rtsps.
GO2RTC_SCHEMES = [
    "alsa", "webtorrent", "dvrip", "vigi", "rtsp", "ffmpeg", "rtmpx", "rtmp", "rtmps", "kasa", "flussonic", "http",
    "tuya", "ivideon", "exec", "echo", "https", "httpx", "homekit", "doorbird", "eseecloud", "rtspx", "bubble",
    "multitrans", "tcp", "wyoming", "ring", "rtsps", "wyze", "hass", "v4l2", "yandex", "isapi", "nest", "gopro",
    "tapo", "expr", "onvif", "webrtc", "xiaomi", "roborock",
]
ALLOWED = {"rtsp", "rtsps"}


@pytest.mark.parametrize("scheme", GO2RTC_SCHEMES)
def test_only_rtsp_and_rtsps_addresses_are_accepted_of_all_the_relay_would_open(scheme):
    assert len(GO2RTC_SCHEMES) == len(set(GO2RTC_SCHEMES)) == 41
    forms = [f"{scheme}://192.168.1.1:7441/AbCdEf", f"{scheme.upper()}://192.168.1.1:7441/AbCdEf", f"{scheme}:192.168.1.1",
             f"{scheme}:rtsps://192.168.1.1:7441/AbCdEf", f"{scheme}:ffmpeg -i rtsps://192.168.1.1:7441/AbCdEf -f null -"]
    if scheme in ALLOWED:
        assert validate_stream_url(forms[0]) == forms[0]
        assert validate_stream_url(forms[1]) == forms[0]            # the scheme is lower-cased
        rest = forms[2:]
    else:
        rest = forms
    for url in rest:
        with pytest.raises(ValueError):
            validate_stream_url(url)


@pytest.mark.parametrize("given, kept", [
    ("rtsps://192.168.1.1:7441/AbCdEf?enableSrtp", "rtsps://192.168.1.1:7441/AbCdEf"),
    ("rtsps://192.168.1.1:7441/AbCdEf?enableSrtp=", "rtsps://192.168.1.1:7441/AbCdEf"),
    ("rtsps://192.168.1.1:7441/AbCdEf?enableSrtp=true", "rtsps://192.168.1.1:7441/AbCdEf"),
    ("rtsps://192.168.1.1:7441/AbCdEf?ENABLESRTP", "rtsps://192.168.1.1:7441/AbCdEf"),
    ("rtsps://192.168.1.1:7441/AbCdEf?enableSrtp&channel=1", "rtsps://192.168.1.1:7441/AbCdEf?channel=1"),
    ("rtsps://192.168.1.1:7441/AbCdEf?channel=1&enableSrtp", "rtsps://192.168.1.1:7441/AbCdEf?channel=1"),
    ("rtsps://192.168.1.1:7441/AbCdEf?a=1&enableSrtp&b=2", "rtsps://192.168.1.1:7441/AbCdEf?a=1&b=2"),
    ("  rtsps://192.168.1.1:7441/AbCdEf?enableSrtp \n", "rtsps://192.168.1.1:7441/AbCdEf"),
    ("rtsp://user:pass@cam.lan:554/live/main", "rtsp://user:pass@cam.lan:554/live/main"),
    ("rtsps://[fd00::1]:7441/AbCdEf", "rtsps://[fd00::1]:7441/AbCdEf"),
])
def test_the_srtp_ending_protect_adds_is_removed_and_nothing_else_changes(given, kept):
    assert validate_stream_url(given) == kept
    assert validate_stream_url(kept) == kept


@pytest.mark.parametrize("url", [
    "", "   ", "rtsps://", "rtsps:///AbCdEf", "rtsps://:7441/AbCdEf", "192.168.1.1:7441/AbCdEf", "//192.168.1.1/x",
    "rtsps://192.168.1.1:7441/Ab CdEf", "rtsps://192.168.1.1:7441/AbCdEf\tx", "rtsps://192.168.1.1:7441/AbCdEf\x00",
    "rtsps://192.168.1.1:7441/AbCdEf#video=copy", "rtsps://192.168.1.1:7441/AbCdEf#",
    "rtsps://169.254.1.1:7441/AbCdEf", "rtsps://[fe80::1]:7441/AbCdEf", "rtsps://224.0.0.1/x", "rtsps://0.0.0.0/x",
    "rtsps://192.168.1.1:7441/" + "a" * STREAM_URL_MAX,
])
def test_an_address_the_relay_must_not_be_given_is_refused(url):
    with pytest.raises(ValueError):
        validate_stream_url(url)


@pytest.mark.parametrize("url, host", [
    ("rtsps://192.168.1.1:7441/AbCdEf", "192.168.1.1:7441"),
    ("rtsp://user:pass@cam.lan/live?token=abc", "cam.lan"),
    ("rtsps://[fd00::1]:7441/AbCdEf", "[fd00::1]:7441"),
])
def test_only_the_host_of_an_address_is_shown_again(url, host):
    assert stream_host(url) == host


class Relay:
    """go2rtc 1.9.15 as it answered on the kiosk: PATCH sets a stream in memory
    (200) and refuses a scheme it cannot open (400); DELETE removes a stream and
    answers 400 all the same, because its config file is read-only; a restart
    forgets every stream; the probe query (src and video) names what the camera
    sends, answers 404 for a stream it does not have and 500 with the dial error
    for a camera that does not answer."""

    def __init__(self):
        self.streams = {}
        self.calls = []
        self.dead = set()      # host:port of cameras that do not answer
        self.down = False

    def restart(self):
        self.streams.clear()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        q = request.url.params
        self.calls.append((request.method, q.get("name") or q.get("src")))
        if request.method == "GET" and "src" not in q:
            return httpx.Response(200, json={n: {"producers": [{"url": u}], "consumers": None} for n, u in self.streams.items()})
        if request.method == "GET":
            url = self.streams.get(q["src"])
            if url is None:
                return httpx.Response(404, text="\n")
            if stream_host(url) in self.dead:
                return httpx.Response(500, text=f"streams: dial tcp {stream_host(url)}: i/o timeout\n")
            return httpx.Response(200, json={
                "producers": [{"id": 2, "format_name": "rtsp", "protocol": "rtsp+tcp", "remote_addr": stream_host(url), "url": url,
                               "sdp": f"v=0\r\ns=stream\r\nu={url}\r\n",
                               "medias": ["audio, recvonly, MPEG4-GENERIC/16000", "audio, recvonly, OPUS/48000/2", "video, recvonly, H265"]}],
                "consumers": [{"id": 1, "format_name": "probe"}],
            })
        if request.method == "PATCH":
            if q["src"].split(":", 1)[0] not in GO2RTC_SCHEMES or ":" not in q["src"]:
                return httpx.Response(400, text="streams: source not supported\n")
            self.streams[q["name"]] = q["src"]
            return httpx.Response(200)
        if request.method == "DELETE":
            self.streams.pop(q["src"], None)
            return httpx.Response(400, text="yaml: path not exist\n")
        return httpx.Response(405)

    def install(self, monkeypatch):
        monkeypatch.setattr(cameras, "_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))
        cameras._status_cache["value"] = None
        cameras._sync.update(state=None, error=None, missing=[])
        return self


URL_A, URL_B, URL_C = (f"rtsps://192.168.1.1:7441/Token{n}xyz" for n in "ABC")


@pytest.fixture()
def streams(monkeypatch):
    """Two stored streams, for one test."""
    monkeypatch.setitem(STORE.data, "streams", [
        {"id": "cam1", "name": "Gate", "url": URL_A, "changed": 1.0},
        {"id": "cam3", "name": "Runway", "url": URL_B, "changed": 1.0},
    ])


def test_the_relay_is_given_every_stored_stream_and_is_asked_nothing_more_once_it_has_them(monkeypatch, streams):
    relay = Relay().install(monkeypatch)
    state = asyncio.run(cameras.sync())
    assert relay.streams == {"cam1": URL_A, "cam3": URL_B}
    assert state == {"state": "ok", "error": None, "missing": []}
    relay.calls.clear()
    asyncio.run(cameras.sync())
    assert relay.calls == [("GET", None)]                      # in step: it only looks


def test_a_restarted_relay_gets_its_streams_again(monkeypatch, streams):
    relay = Relay().install(monkeypatch)
    asyncio.run(cameras.sync())
    relay.restart()
    assert relay.streams == {}
    asyncio.run(cameras.sync())
    assert relay.streams == {"cam1": URL_A, "cam3": URL_B}


class FakeSocket:
    def __init__(self):
        self.closed = None

    async def close(self, code=1000, reason=""):
        self.closed = code


def test_a_changed_address_replaces_the_stream_and_ends_the_relays_playing_the_old_one(monkeypatch, streams):
    relay = Relay().install(monkeypatch)
    asyncio.run(cameras.sync())
    playing, other = FakeSocket(), FakeSocket()
    monkeypatch.setitem(cameras._sockets, "cam1", {playing})
    monkeypatch.setitem(cameras._sockets, "cam3", {other})
    STORE.data["streams"][0]["url"] = URL_C
    asyncio.run(cameras.sync())
    assert relay.streams == {"cam1": URL_C, "cam3": URL_B}
    assert playing.closed == 1012 and other.closed is None     # only the changed stream's tiles reconnect


def test_a_stream_the_store_no_longer_holds_is_dropped_though_the_relay_answers_400(monkeypatch, streams):
    relay = Relay().install(monkeypatch)
    asyncio.run(cameras.sync())
    del STORE.data["streams"][0]
    state = asyncio.run(cameras.sync())
    assert relay.streams == {"cam3": URL_B} and ("DELETE", "cam1") in relay.calls and state["state"] == "ok"


def test_the_stream_of_a_test_in_progress_is_left_alone_and_a_leftover_one_is_dropped(monkeypatch, streams):
    relay = Relay().install(monkeypatch)
    relay.streams["test-1a2b3c4d"] = URL_C
    monkeypatch.setattr(cameras, "_testing", {"test-1a2b3c4d"})
    asyncio.run(cameras.sync())
    assert "test-1a2b3c4d" in relay.streams
    cameras._testing.clear()                                   # the test ended, or the app restarted during it
    asyncio.run(cameras.sync())
    assert sorted(relay.streams) == ["cam1", "cam3"]


def test_testing_a_stream_names_the_video_the_camera_sends_and_leaves_nothing_behind(monkeypatch, streams):
    relay = Relay().install(monkeypatch)
    asyncio.run(cameras.sync())
    assert asyncio.run(cameras.test(stream="cam1")) == {"ok": True, "video": "H265", "error": None}
    relay.calls.clear()
    assert asyncio.run(cameras.test(url=URL_C)) == {"ok": True, "video": "H265", "error": None}
    assert [m for m, _n in relay.calls] == ["PATCH", "GET", "DELETE"] and relay.calls[0][1].startswith("test-")
    assert sorted(relay.streams) == ["cam1", "cam3"] and cameras._testing == set()
    assert asyncio.run(cameras.test(stream="cam9")) == {
        "ok": False, "error": "the relay does not have this stream yet; try again in a moment"}


def test_a_camera_that_does_not_answer_and_a_relay_that_is_down_are_reported_without_the_address(monkeypatch, streams, caplog):
    caplog.set_level(logging.DEBUG)
    relay = Relay().install(monkeypatch)
    asyncio.run(cameras.sync())
    relay.dead.add("192.168.1.1:7441")
    results = [asyncio.run(cameras.test(stream="cam1")), asyncio.run(cameras.test(url=URL_C))]
    assert [r["ok"] for r in results] == [False, False] and all("i/o timeout" in r["error"] for r in results)
    relay.down = True
    results.append(asyncio.run(cameras.test(url=URL_C)))
    assert results[-1] == {"ok": False, "error": "camera relay unreachable (ConnectError)"}
    state = asyncio.run(cameras.sync())
    assert state == {"state": "down", "error": "ConnectError", "missing": ["cam1", "cam3"]}
    status = asyncio.run(cameras.status(["cam1", "cam3", "cam9"]))
    assert status["relay"] == "down" and status["unknown_streams"] == ["cam9"] and status["streams"] == 2
    said = json.dumps([results, state, status]) + caplog.text
    assert "Token" not in said and "rtsps://" not in said
    # the outage is logged when it starts, not at every attempt
    asyncio.run(cameras.sync())
    assert sum("camera relay: ConnectError" in r.getMessage() for r in caplog.records) == 1


def test_status_names_tiles_without_a_stored_stream_and_streams_the_relay_lacks(client, monkeypatch, streams):
    relay = Relay().install(monkeypatch)
    relay.streams["cam1"] = URL_A                              # the relay has cam1 and not cam3
    tiles = [{"name": "", "stream": s, "cols": 2} for s in ("cam1", "cam3", "cam9")]
    assert put(client, cams_cfg(tiles=tiles)).status_code == 200
    cameras._status_cache["value"] = None
    cams = client.get("/api/status").json()["cameras"]
    assert cams["relay"] == "ok" and cams["error"] is None and cams["tiles"] == 3 and cams["streams"] == 2
    assert cams["unknown_streams"] == ["cam9"] and cams["pending_streams"] == ["cam3"]
    assert wait_until(lambda: relay.streams.get("cam3") == URL_B, 3)     # noticing it has the loop give it to the relay


# ---- the stream API ----------------------------------------------------------------------------

def test_streams_are_managed_by_a_signed_in_browser_and_reach_the_relay_without_a_restart(client, monkeypatch):
    relay = Relay().install(monkeypatch)
    assert put(client, base_cfg()).status_code == 200          # no camera tiles
    assert client.get("/api/cameras/streams", auth=AUTH).status_code == 403
    sign_in(client)
    assert client.get("/api/cameras/streams").json() == {"streams": [], "relay": "ok", "error": None, "max": MAX_STREAMS}
    r = client.post("/api/cameras/streams", json={"name": "  Hangar   door ", "url": URL_A + "?enableSrtp"})
    assert r.status_code == 200
    made = r.json()
    assert made == {"id": "cam1", "name": "Hangar door", "host": "192.168.1.1:7441", "changed": made["changed"], "tiles": []}
    assert STORE.stream("cam1")["url"] == URL_A                 # stored without Protect's ending
    assert wait_until(lambda: relay.streams == {"cam1": URL_A}, 3)
    second = client.post("/api/cameras/streams", json={"name": "Runway", "url": URL_B}).json()
    assert second["id"] == "cam2"
    listing = client.get("/api/cameras/streams")
    assert [s["id"] for s in listing.json()["streams"]] == ["cam1", "cam2"] and "Token" not in listing.text
    # a new address is live at once; a new name alone asks nothing of the relay
    assert client.put("/api/cameras/streams/cam1", json={"url": URL_C}).status_code == 200
    assert wait_until(lambda: relay.streams.get("cam1") == URL_C, 3)
    relay.calls.clear()
    assert client.put("/api/cameras/streams/cam2", json={"name": "Runway north"}).json()["name"] == "Runway north"
    assert STORE.stream("cam2")["url"] == URL_B
    # a stream a tile plays cannot be removed from under it
    assert put(client, cams_cfg(tiles=[{"name": "Door", "stream": "cam1", "cols": 2}])).status_code == 200
    r = client.delete("/api/cameras/streams/cam1")
    assert r.status_code == 409 and "Door" in r.json()["detail"]
    assert client.get("/api/cameras/streams").json()["streams"][0]["tiles"] == ["Door"]
    assert client.delete("/api/cameras/streams/cam2").status_code == 200
    assert wait_until(lambda: sorted(relay.streams) == ["cam1"], 3)
    assert client.post("/api/cameras/streams", json={"name": "Again", "url": URL_B}).json()["id"] == "cam2"   # the lowest free id
    assert client.delete("/api/cameras/streams/cam7").status_code == 404
    assert client.put("/api/cameras/streams/cam7", json={"name": "x"}).status_code == 404


def test_a_bad_stream_is_refused_with_the_field_named_and_the_address_never_echoed(client, monkeypatch):
    Relay().install(monkeypatch)
    sign_in(client)
    for body, loc in (({"name": "", "url": URL_A}, ["name"]), ({"name": "Door", "url": "exec:rm -rf /data"}, ["url"]),
                      ({"name": "Door", "url": "http://192.168.1.1/TokenAxyz"}, ["url"]),
                      ({"name": "Door", "url": URL_A + "#backchannel=0"}, ["url"])):
        r = client.post("/api/cameras/streams", json=body)
        assert r.status_code == 422 and [e["loc"] for e in r.json()["detail"]] == [loc], r.text
        assert "Token" not in r.text and "rm -rf" not in r.text
    assert STORE.streams() == []
    assert client.post("/api/cameras/streams/test", json={}).status_code == 422
    assert client.post("/api/cameras/streams/test", json={"id": "cam1", "url": URL_A}).status_code == 422
    assert client.post("/api/cameras/streams/test", json={"id": "cam1"}).status_code == 404


def test_at_most_twelve_streams_are_stored(client, monkeypatch):
    Relay().install(monkeypatch)
    sign_in(client)
    for n in range(MAX_STREAMS):
        assert client.post("/api/cameras/streams", json={"name": f"Camera {n}", "url": f"{URL_A}{n}"}).status_code == 200
    assert [s["id"] for s in STORE.streams()] == [f"cam{n}" for n in range(1, MAX_STREAMS + 1)]
    r = client.post("/api/cameras/streams", json={"name": "One more", "url": URL_B})
    assert r.status_code == 409 and len(STORE.streams()) == MAX_STREAMS
