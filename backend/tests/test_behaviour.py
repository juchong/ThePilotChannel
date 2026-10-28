"""Behaviour tests. Speed: polls never wait on an upstream fetch and a watched
view's snapshot stays fresher than the poll period. Reliability: outages are
backed off and logged once; caches and warm-up stay bounded. Quality: flight
categories, winds, and visibilities decode correctly.
"""
import asyncio
import io
import json
import logging
import os
import time

import httpx
import pytest
from support import AUTH, base_cfg, put

from app import main as main_mod
from app import manager as mg
from app.config import Config
from app.views import build_views
from app.weather import _ceiling_ft, _visibility_sm, derive_category, normalize_metar

TRAFFIC = {"view": "local:KSEA"}


def poll(client):
    return client.get("/api/traffic", params=TRAFFIC).json()


# ---- speed ------------------------------------------------------------------------------

def test_polls_of_an_active_view_never_wait_on_a_hanging_upstream(client, monkeypatch):
    """A slow receiver or aggregator must not stall the display's 1 Hz poll."""
    m = main_mod.manager

    async def hanging(lat, lon, nm):
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(m, "_fetch_traffic", hanging)
    assert put(client, base_cfg()).status_code == 200
    t0 = time.time()
    first = poll(client)
    assert time.time() - t0 < 2.0 and first["pending"] is True  # cold view: bounded wait, then "pending"
    for _ in range(5):
        t0 = time.time()
        poll(client)
        assert time.time() - t0 < 0.25  # the refresh is still hanging; polls do not care


def test_watched_view_snapshot_stays_fresher_than_the_poll_period(client, monkeypatch):
    """The background loop, not the poll, keeps a watched view fresh at 1 s; without
    it the refresh aliased with the poll and the display updated every 2 s."""
    m = main_mod.manager
    calls = []

    async def instant(lat, lon, nm):
        calls.append(time.time())
        return [{"hex": "a1", "lat": lat, "lon": lon, "seen_pos": 0.2}]

    monkeypatch.setattr(m, "_fetch_traffic", instant)
    assert put(client, base_cfg()).status_code == 200
    ages = []
    deadline = time.time() + 3.2
    while time.time() < deadline:
        r = poll(client)
        if not r.get("pending"):
            ages.append(r["age_s"])
        time.sleep(0.4)
    assert ages and max(ages) <= 1.3, ages
    assert len(calls) >= 3


def test_cached_data_is_served_in_milliseconds_while_upstream_is_slow(client, monkeypatch):
    m = main_mod.manager
    box = {"min_lat": 47.0, "min_lon": -123.0, "max_lat": 48.0, "max_lon": -121.0}

    async def fast(*a):
        return [{"icao": "KSEA", "lat": 47.45, "lon": -122.31, "obs_time": time.time(), "wind": {}}]

    async def slow(*a):
        await asyncio.sleep(3)
        return await fast()

    monkeypatch.setattr(m.metar, "fetch_bbox", fast)
    assert client.get("/api/weather/bbox", params=box).json()["stations"]
    monkeypatch.setattr(m.metar, "fetch_bbox", slow)
    key = next(k for k in m._area_cache if k.startswith("bbox:47.0:"))
    m._area_cache[key]["ts"] -= 10_000  # expired: refresh runs in the background
    t0 = time.time()
    r = client.get("/api/weather/bbox", params=box).json()
    assert time.time() - t0 < 0.3 and r["stations"] and r["stale"] is True


def test_basemap_tile_cache_hit_is_fast(client, monkeypatch):
    png = b"\x89PNG\r\n\x1a\n" + b"9" * 64

    async def upstream(z, x, y):
        await asyncio.sleep(0.4)
        return png

    monkeypatch.setattr(main_mod.manager.tiles, "_fetch_upstream", upstream)
    path = main_mod.manager.tiles.path(11, 327, 714)
    if os.path.exists(path):
        os.remove(path)  # an earlier warm-up may have cached it; measure a true cold fetch
    t0 = time.time()
    assert client.get("/tiles/11/327/714.png").status_code == 200
    cold = time.time() - t0
    t0 = time.time()
    assert client.get("/tiles/11/327/714.png").content == png
    hit = time.time() - t0
    assert cold >= 0.4 and hit < cold / 3, (cold, hit)


# ---- reliability --------------------------------------------------------------------------

def test_local_receiver_outage_backs_off_and_logs_once(client, monkeypatch, caplog):
    """In auto mode a dead receiver falls back to the aggregator, is retried
    only after LOCAL_RETRY_S, and is logged once."""
    from app.sources.aggregator import AggregatorSource
    from app.sources.local import LocalReadsbSource

    local_calls, agg_calls = [], []

    async def dead(self, lat, lon, nm):
        local_calls.append(time.time())
        raise httpx.ConnectError("All connection attempts failed")

    async def agg(self, lat, lon, nm):
        agg_calls.append(time.time())
        return [{"hex": "b1", "lat": lat, "lon": lon, "seen_pos": 0.5}]

    monkeypatch.setattr(LocalReadsbSource, "fetch", dead)
    monkeypatch.setattr(AggregatorSource, "fetch", agg)
    monkeypatch.setattr(mg, "AGGREGATOR_MIN_INTERVAL", 0.0)
    m = main_mod.manager
    cfg = base_cfg()
    cfg["data_source"] = {"mode": "auto", "local_url": "http://192.0.2.10/tar1090/data/aircraft.json"}
    caplog.set_level(logging.INFO, logger="hangar")
    assert put(client, cfg).status_code == 200
    # Real source selection only after the config is in place: the traffic loop
    # may already be refreshing this view, and a config change resets the breaker.
    monkeypatch.delattr(m, "_fetch_traffic")
    deadline = time.time() + 3.0
    while time.time() < deadline:
        poll(client)
        time.sleep(0.3)
    assert len(local_calls) == 1, "receiver retried before the back-off elapsed"
    assert len(agg_calls) >= 2
    warnings = [r for r in caplog.records if "local source unavailable" in r.getMessage()]
    assert len(warnings) == 1
    r = poll(client)
    assert r["source"].startswith("aggregator") and r["count"] == 1 and r["healthy"] is True


def test_repeated_upstream_failures_log_once_per_state_change(client, monkeypatch, caplog):
    m = main_mod.manager

    async def broken(lat, lon, nm):
        raise RuntimeError("receiver down")

    caplog.set_level(logging.INFO, logger="hangar")
    assert put(client, base_cfg()).status_code == 200  # resets the view's health
    monkeypatch.setattr(m, "_fetch_traffic", broken)  # then the failures start
    deadline = time.time() + 2.5
    while time.time() < deadline:
        r = poll(client)
        time.sleep(0.3)
    errors = [x for x in caplog.records if "traffic fetch failed" in x.getMessage()]
    assert len(errors) == 1, [e.getMessage() for e in errors]
    assert r["healthy"] is False and r["error"] == "receiver down"


def test_tile_warm_up_is_bounded_and_identifies_the_app(monkeypatch):
    """OpenStreetMap's usage policy: no bulk downloads, and requests must carry an
    identifying User-Agent. A mistaken 500-mile region must not become a crawl."""
    from app.tiles import MAX_TILES_PER_VIEW, TileCache, tiles_for_views

    huge = [{"id": "region:big", "center_lat": 47.4, "center_lon": -122.3, "radius_nm": 434.0}]
    assert 0 < len(tiles_for_views(huge)) <= MAX_TILES_PER_VIEW
    seen = {}

    class FakeClient:
        async def get(self, url, headers=None, timeout=None):
            seen["url"], seen["headers"] = url, headers

            class R:
                status_code = 200
                content = b"\x89PNG\r\n\x1a\n" + b"0" * 32
                headers = {"content-type": "image/png"}

                def raise_for_status(self):
                    pass

            return R()

    cache = TileCache(FakeClient(), root=main_mod.STATIC_DIR + "/tiles-ua")
    asyncio.run(cache._fetch_upstream(9, 81, 178))
    assert "tile.openstreetmap.org/9/81/178.png" in seen["url"]
    assert "ThePilotChannel" in seen["headers"]["User-Agent"] and "github.com" in seen["headers"]["User-Agent"]


def test_hashed_assets_are_immutable_and_html_always_revalidates(client):
    """Chromium once kept a months-old index.html: HTML must be no-cache, hashed
    bundles may be cached forever."""
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/assets/main-abc123.js").headers["cache-control"] == "public, max-age=31536000, immutable"


def test_metar_age_is_computed_when_read_not_when_fetched(client):
    """A report fetched fresh but read an hour later must show its true age."""
    m = main_mod.manager
    m._weather_cache = {"KSEA": {"icao": "KSEA", "obs_time": time.time() - 7200, "wind": {}, "age_s": 0, "stale": False}}
    r = client.get("/api/weather", params={"ids": "KSEA"}).json()["metars"]["KSEA"]
    assert r["age_s"] >= 7190 and r["stale"] is True


def test_manager_can_be_started_twice():
    """Each lifespan (and each test client) gets fresh loop-bound primitives."""
    async def run():
        m = mg.DataManager()
        await m.start()
        first_wake, first_loops = m._weather_wake, list(m._loops)
        await asyncio.sleep(0.1)
        await m.stop()
        assert all(t.done() for t in first_loops)
        m.client = httpx.AsyncClient()
        await m.start()
        await asyncio.sleep(0.1)
        try:
            assert m._weather_wake is not first_wake
            assert all(not t.done() for t in m._loops)
        finally:
            await m.stop()

    asyncio.run(run())


def test_overlapping_weather_saves_serialise_and_keep_the_latest_state(monkeypatch):
    """Saves requested while the weather store is being written wait for the
    writer, are coalesced, and leave the latest caches on disk with no temp file."""
    path = os.path.join(os.path.dirname(mg.WEATHER_STORE), "weather-race.json")
    monkeypatch.setattr(mg, "WEATHER_STORE", path)
    dumps = []

    class SlowJson:
        def __getattr__(self, name):
            return getattr(json, name)

        def dump(self, obj, fh, **kw):
            dumps.append(sorted(obj["metars"]))
            time.sleep(0.2)
            json.dump(obj, fh, **kw)

    monkeypatch.setattr(mg, "json", SlowJson())

    async def run():
        m = mg.DataManager()
        try:
            m._weather_cache = {"KSEA": {"icao": "KSEA"}}
            m._save_weather_store()
            await asyncio.sleep(0.05)  # the first write is now in progress
            m._weather_cache = {"KSEA": {"icao": "KSEA"}, "KBFI": {"icao": "KBFI"}}
            m._save_weather_store()
            m._save_weather_store()
            await asyncio.gather(*[t for t in m._tasks if t.get_name() == "weather-store"])
        finally:
            await m.client.aclose()
        with open(path) as fh:
            return json.load(fh)["metars"]

    assert sorted(asyncio.run(run())) == ["KBFI", "KSEA"]
    assert dumps == [["KSEA"], ["KBFI", "KSEA"]]
    assert not os.path.exists(path + ".tmp")


# ---- quality -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ceiling, vis, expected",
    [
        (None, None, None),
        (5000, 10, "VFR"), (3100, 6, "VFR"),
        (3000, 10, "MVFR"), (1000, 5, "MVFR"), (2500, 10, "MVFR"), (10000, 5, "MVFR"),
        (900, 10, "IFR"), (5000, 2.5, "IFR"), (500, 3, "IFR"),
        (400, 10, "LIFR"), (5000, 0.5, "LIFR"), (200, 0.25, "LIFR"),
    ],
)
def test_flight_category_matches_faa_thresholds(ceiling, vis, expected):
    assert derive_category(ceiling, vis) == expected


def test_reported_category_wins_over_derived_and_colors_follow():
    m = normalize_metar({"icaoId": "KSEA", "fltCat": "IFR", "clouds": [{"cover": "OVC", "base": 5000}], "visib": "10+"}, 4500)
    assert m["category"] == "IFR" and m["category_color"] == "#ef4444"
    m = normalize_metar({"icaoId": "KSEA", "clouds": [{"cover": "OVC", "base": 400}], "visib": "3"}, 4500)
    assert m["category"] == "LIFR" and m["category_color"] == "#d946ef"


def test_visibility_and_ceiling_decoding():
    assert _visibility_sm("1 1/2") == 1.5 and _visibility_sm("1/4") == 0.25
    assert _visibility_sm("10+") == 10.0 and _visibility_sm(6) == 6.0 and _visibility_sm("junk") is None
    assert _ceiling_ft([{"cover": "FEW", "base": 500}, {"cover": "SCT", "base": 900}, {"cover": "BKN", "base": 2500}, {"cover": "OVC", "base": 4000}]) == 2500
    assert _ceiling_ft([{"cover": "FEW", "base": 500}]) is None
    assert _ceiling_ft([{"cover": "OVX", "base": 300}]) == 300


def test_wind_decoding_for_barbs():
    calm = normalize_metar({"icaoId": "KBFI", "wdir": 0, "wspd": 0}, 4500)["wind"]
    assert calm["calm"] is True
    vrb = normalize_metar({"icaoId": "KBFI", "wdir": "VRB", "wspd": 4}, 4500)["wind"]
    assert vrb["variable"] is True and vrb["dir"] is None and vrb["speed_kt"] == 4
    gust = normalize_metar({"icaoId": "KBFI", "wdir": 230, "wspd": 12, "wgst": 22}, 4500)["wind"]
    assert gust["dir"] == 230 and gust["gust_kt"] == 22 and gust["calm"] is False


def test_stale_flag_uses_the_configured_threshold():
    fresh = normalize_metar({"icaoId": "KSEA", "obsTime": time.time() - 600}, 4500)
    old = normalize_metar({"icaoId": "KSEA", "obsTime": time.time() - 5000}, 4500)
    assert fresh["stale"] is False and old["stale"] is True


def test_explicit_cycle_order_and_local_view_cap():
    cfg = Config.model_validate(
        {
            "airports": [{"icao": "KSEA", "lat": 47.45, "lon": -122.31}, {"icao": "KBFI", "lat": 47.55, "lon": -122.31}, {"icao": "KS50", "lat": 47.33, "lon": -122.23}],
            "regions": [{"name": "A", "center_lat": 47, "center_lon": -122}],
            "cycle": {"order": ["region:A", "local:KBFI", "local:NOPE"], "max_local_views": 2},
            "satellite": {"enabled": False},
        }
    )
    assert [v["id"] for v in build_views(cfg)] == ["region:A", "local:KBFI"]  # unknown ids ignored
    cfg.cycle.order = []
    cfg.cycle.interleave_regional = False
    assert [v["id"] for v in build_views(cfg)] == ["local:KSEA", "local:KBFI", "region:A"]  # cap applied


# ---- radar ----------------------------------------------------------------------------------

def _png(pixels):
    from PIL import Image

    im = Image.new("RGBA", (len(pixels), 1))
    im.putdata(pixels)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _alpha(png):
    from PIL import Image

    return list(Image.open(io.BytesIO(png)).convert("RGBA").getchannel("A").getdata())


def test_radar_floor_hides_weak_returns_and_keeps_rain():
    from app import radar

    pal = radar.PALETTES["n0q"]
    color = lambda dbz: tuple(pal[65 + int(dbz * 2)]) + (255,)  # noqa: E731
    png = _png([color(-5), color(5), color(9.5), color(10), color(20), (0, 0, 0, 0), (1, 2, 3, 255)])
    assert _alpha(radar.filter_tile(png, "n0q", 10)) == [0, 0, 0, 255, 255, 0, 255]
    assert radar.filter_tile(png, "n0q", -32) == png  # nothing below the floor: untouched
    n0r = radar.PALETTES["n0r"]
    png = _png([tuple(n0r[7]) + (255,), tuple(n0r[10]) + (255,)])  # 5 and 20 dBZ
    assert _alpha(radar.filter_tile(png, "n0r", 10)) == [0, 255]


def test_radar_legend_uses_the_product_palette_from_the_floor_up():
    from app import radar

    lg = radar.legend("n0q", 10)
    assert [d for d, _ in lg] == [15, 25, 35, 45, 55, 65, 75]
    r, g, b = radar.PALETTES["n0q"][65 + 30]
    assert lg[0][1] == f"#{r:02x}{g:02x}{b:02x}"
    assert [d for d, _ in radar.legend("n0r", 0)][0] == 5


def test_radar_tiles_are_proxied_filtered_and_cached_per_bucket(client, monkeypatch):
    from app import radar

    pal = radar.PALETTES["n0q"]
    calls = []

    async def upstream(product, lag, z, x, y):
        calls.append((product, lag, z, x, y))
        return _png([tuple(pal[75]) + (255,), tuple(pal[105]) + (255,)])  # 5 and 20 dBZ

    monkeypatch.setattr(main_mod.manager.radar, "_fetch_upstream", upstream)
    cfg = base_cfg()
    cfg["radar"] = {"min_dbz": 10}
    assert put(client, cfg).status_code == 200
    views = client.get("/api/views").json()
    view = next(v for v in (views["views"] if isinstance(views, dict) else views) if v["type"] == "regional")
    assert view["radar"]["tile_base"] == "/radar/n0q" and view["radar"]["legend"][0][0] == 15
    r = client.get("/radar/n0q-m05m/9/82/179.png", params={"v": 1})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert _alpha(r.content) == [0, 255]
    assert calls == [("n0q", "-m05m", 9, 82, 179)]
    client.get("/radar/n0q-m05m/9/82/179.png", params={"v": 1})
    assert len(calls) == 1  # served from memory
    client.get("/radar/n0q-m05m/9/82/179.png", params={"v": 2})
    assert len(calls) == 2  # a new bucket is a new fetch
    assert client.get("/radar/n0x/9/82/179.png").status_code == 404
    assert client.get("/radar/n0q/30/0/0.png").status_code == 404


def test_radar_tile_failures_answer_502_and_log_once(client, monkeypatch, caplog):
    async def broken(product, lag, z, x, y):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(main_mod.manager.radar, "_fetch_upstream", broken)
    main_mod.manager.radar._failing = False
    caplog.set_level(logging.INFO, logger="hangar")
    for x in (1, 2, 3):
        assert client.get(f"/radar/n0q/9/{x}/179.png", params={"v": 9}).status_code == 502
    assert len([r for r in caplog.records if "radar tiles unavailable" in r.getMessage()]) == 1
