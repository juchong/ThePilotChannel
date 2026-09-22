"""Backend tests. Run from the backend directory (or the image's /app):

    python -m pytest -q tests

Environment is prepared before the app is imported so the module-level
DataManager reads a throwaway config path and a stub static dir.
"""
import os
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="tpc-test-")
CFG = os.path.join(TMP, "config.yaml")
STATIC = os.path.join(TMP, "static")
os.makedirs(STATIC)
with open(os.path.join(STATIC, "admin.html"), "w") as fh:
    fh.write("<html>admin</html>")
with open(os.path.join(STATIC, "index.html"), "w") as fh:
    fh.write("<html>display</html>")
os.environ["HANGAR_CONFIG"] = CFG
os.environ["HANGAR_STATIC"] = STATIC
os.environ["HANGAR_ADMIN_PASSWORD"] = "hunter2"
os.environ["HANGAR_TILE_CACHE"] = os.path.join(TMP, "tiles")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_mod  # noqa: E402
from app.config import SECRET_MASK, Config, load_config, save_config  # noqa: E402
from app.views import build_views  # noqa: E402

AUTH = ("admin", "hunter2")


@pytest.fixture()
def client(monkeypatch):
    # never touch the network from tests
    async def fake_metar(icaos, stale):
        return {i.upper(): {"icao": i.upper(), "obs_time": time.time(), "wind": {}} for i in icaos}

    async def fake_traffic(lat, lon, nm):
        return [
            {"hex": "a1", "lat": lat, "lon": lon, "seen_pos": 1},
            {"hex": "a2", "lat": lat, "lon": lon, "seen_pos": 999},  # stale position, dropped
        ]

    monkeypatch.setattr(main_mod.manager.metar, "fetch", fake_metar)
    monkeypatch.setattr(main_mod.manager, "_fetch_traffic", fake_traffic)
    with TestClient(main_mod.app) as c:
        yield c


def base_cfg():
    return {
        "airports": [{"icao": "ksea", "name": "Seattle", "lat": 47.45, "lon": -122.31, "local_radius_mi": 5, "enabled": True}],
        "regions": [{"name": "Puget Sound", "center_lat": 47.44, "center_lon": -122.27, "radius_mi": 30, "enabled": True}],
    }


def put(client, body, **kw):
    return client.put("/api/config", json=body, auth=AUTH, **kw)


# ---- config validation ---------------------------------------------------------

def test_defaults_file_written():
    assert os.path.exists(CFG)


def test_put_normalizes_icao_and_returns_version(client):
    r = put(client, base_cfg())
    assert r.status_code == 200, r.text
    assert r.json()["version"]
    assert client.get("/api/config").json()["airports"][0]["icao"] == "KSEA"


@pytest.mark.parametrize(
    "mutate, loc_prefix",
    [
        (lambda c: c["airports"][0].update(icao="TOOLONG"), ["airports", "0", "icao"]),
        (lambda c: c["airports"][0].update(lat=99), ["airports", "0", "lat"]),
        (lambda c: c.update(cycle={"local_dwell_s": 0}), ["cycle", "local_dwell_s"]),
        (lambda c: c.update(radar={"frames": 12, "interval_min": 10}), ["radar"]),
        (lambda c: c.update(display={"timezone": "Mars/Olympus"}), ["display", "timezone"]),
        (lambda c: c.update(data_source={"local_url": "ftp://x/aircraft.json"}), ["data_source", "local_url"]),
        (lambda c: c.update(data_source={"local_url": "http://169.254.169.254/latest"}), ["data_source", "local_url"]),
        (lambda c: c.update(satellite={"sector": "../../etc"}), ["satellite", "sector"]),
        (lambda c: c.update(radar={"product": "n0z"}), ["radar", "product"]),
    ],
)
def test_put_rejects_invalid(client, mutate, loc_prefix):
    body = base_cfg()
    mutate(body)
    r = put(client, body)
    assert r.status_code == 422, r.text
    locs = [e["loc"][: len(loc_prefix)] for e in r.json()["detail"]]
    assert loc_prefix in locs, r.json()


def test_put_rejects_duplicate_icao(client):
    body = base_cfg()
    body["airports"].append(dict(body["airports"][0]))
    r = put(client, body)
    assert r.status_code == 422
    assert "duplicate" in r.text


def test_put_rejects_unknown_key(client):
    body = base_cfg()
    body["airports"][0]["radius_mi"] = 5  # typo for local_radius_mi
    body["display"] = {"units": "metric"}  # removed field
    r = put(client, body)
    assert r.status_code == 422
    locs = [e["loc"] for e in r.json()["detail"]]
    assert ["airports", "0", "radius_mi"] in locs
    assert ["display", "units"] in locs


def test_secret_is_masked_and_mask_keeps_value(client):
    body = base_cfg()
    body["data_source"] = {"mode": "aggregator", "aggregator": "airplaneslive", "api_key": "s3cr3t"}
    assert put(client, body).status_code == 200
    got = client.get("/api/config").json()
    assert got["data_source"]["api_key"] == SECRET_MASK
    got.pop("version")
    assert put(client, got).status_code == 200
    assert main_mod.manager.get_config().data_source.api_key == "s3cr3t"
    got["data_source"]["api_key"] = ""
    assert put(client, got).status_code == 200
    assert main_mod.manager.get_config().data_source.api_key == ""


def test_if_match_conflict(client):
    r = client.get("/api/config")
    etag = r.headers["etag"]
    body = r.json()
    body.pop("version")
    assert put(client, body, headers={"If-Match": '"stale-version"'}).status_code == 409
    assert put(client, body, headers={"If-Match": etag}).status_code == 200


# ---- auth ---------------------------------------------------------------------

def test_admin_requires_password(client):
    assert client.get("/admin").status_code == 401
    assert client.get("/admin", auth=("x", "wrong")).status_code == 401
    assert client.get("/admin", auth=AUTH).status_code == 200
    assert client.put("/api/config", json=base_cfg()).status_code == 401
    assert client.post("/api/test-source", json={}).status_code == 401


def test_admin_html_not_served_by_static(client):
    assert client.get("/admin.html").status_code == 404
    assert client.get("/").status_code == 200  # display stays open
    assert client.get("/api/config").status_code == 200


def test_security_headers(client):
    r = client.get("/api/status")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    # The OSM tile servers block requests without a Referer; the display page
    # must keep the browser's default referrer policy.
    page = client.get("/")
    assert "referrer-policy" not in page.headers
    # HTML must be revalidated on every load so a rebuilt bundle is picked up.
    assert page.headers["cache-control"] == "no-cache"
    assert client.get("/admin", auth=AUTH).headers["cache-control"] == "no-cache"


# ---- traffic / weather / satellite ---------------------------------------------

def test_traffic_unknown_view_is_404(client):
    assert client.get("/api/traffic", params={"view": "local:NOPE"}).status_code == 404


def test_first_poll_waits_for_a_fresh_snapshot(client):
    assert put(client, base_cfg()).status_code == 200
    # the fake source answers instantly, so the very first poll already has data
    r = client.get("/api/traffic", params={"view": "local:KSEA"}).json()
    assert not r.get("pending") and r["count"] == 1 and r["aircraft"][0]["hex"] == "a1"
    assert r["age_s"] >= 0 and r["stale"] is False and r["healthy"] is True


def test_leftover_snapshot_is_not_served_as_data(client, monkeypatch):
    import asyncio

    assert put(client, base_cfg()).status_code == 200
    assert client.get("/api/traffic", params={"view": "local:KSEA"}).json()["count"] == 1
    m = main_mod.manager
    m._traffic_cache["local:KSEA"]["ts"] -= 120          # pretend the view was last visited two minutes ago
    m._polled.pop("local:KSEA", None)

    async def slow(lat, lon, nm):                         # the refresh takes longer than the first-poll wait
        await asyncio.sleep(3)
        return [{"hex": "a1", "lat": lat, "lon": lon, "seen_pos": 1}]

    monkeypatch.setattr(m, "_fetch_traffic", slow)
    t0 = time.time()
    r = client.get("/api/traffic", params={"view": "local:KSEA"}).json()
    assert time.time() - t0 < 2.5                          # bounded wait
    assert r["pending"] is True and r["aircraft"] == [] and r["stale"] is True and r["age_s"] >= 120

    # but when refreshes are failing, the last known aircraft are shown (frozen, flagged)
    m._polled.pop("local:KSEA", None)                      # deactivate the view so the loop stops refreshing it
    time.sleep(3.5)                                        # and let the slow refresh above finish
    m._traffic_cache["local:KSEA"]["ts"] -= 120

    async def broken(lat, lon, nm):
        raise RuntimeError("receiver down")

    monkeypatch.setattr(m, "_fetch_traffic", broken)
    r = client.get("/api/traffic", params={"view": "local:KSEA"}).json()
    assert r["count"] == 1 and r["healthy"] is False and r["stale"] is True and r["error"] == "receiver down"


def test_weather_refreshes_on_config_change(client):
    assert put(client, base_cfg()).status_code == 200
    deadline = time.time() + 5
    while time.time() < deadline:
        m = client.get("/api/weather", params={"ids": "ksea,bad-id"}).json()["metars"]
        if "KSEA" in m:
            break
        time.sleep(0.1)
    assert "KSEA" in m and "age_s" in m["KSEA"]


def test_bbox_limits(client):
    assert client.get("/api/weather/bbox", params={"min_lat": -80, "min_lon": -170, "max_lat": 80, "max_lon": 170}).status_code == 422
    assert client.get("/api/weather/bbox", params={"min_lat": 48, "min_lon": -122, "max_lat": 47, "max_lon": -121}).status_code == 422


def test_satellite_param_validation(client):
    assert client.get("/api/satellite", params={"sector": "../x"}).status_code == 422
    assert client.get("/api/satellite", params={"frames": 500}).status_code == 422


# The SSE endpoint is an endless stream, which Starlette's TestClient cannot
# close cleanly, so it is exercised with curl instead (see README "Verify").
# The event payload itself is unit-tested here.
def test_sse_event_payload():
    from app.main import _sse

    line = _sse({"event": "connected", "boot_id": main_mod.manager.boot_id})
    assert line.startswith("data: ") and line.endswith("\n\n")
    assert main_mod.manager.boot_id in line


def test_healthz_reports_config_state(client):
    s = client.get("/healthz").json()
    assert s["ok"] and s["boot_id"] == main_mod.manager.boot_id
    assert s["status"]["config"]["source"] == "file"


# ---- views ------------------------------------------------------------------------

def test_interleave_keeps_extra_regions():
    cfg = Config.model_validate(
        {
            "airports": [{"icao": "KSEA", "lat": 47.45, "lon": -122.31}],
            "regions": [
                {"name": "A", "center_lat": 47, "center_lon": -122},
                {"name": "B", "center_lat": 46, "center_lon": -122},
            ],
            "satellite": {"enabled": False},
        }
    )
    ids = [v["id"] for v in build_views(cfg)]
    assert ids == ["local:KSEA", "region:A", "region:B"]


def test_local_view_fetch_radius_covers_the_screen():
    cfg = Config.model_validate({"airports": [{"icao": "KSEA", "lat": 47.45, "lon": -122.31, "local_radius_mi": 5}], "satellite": {"enabled": False}})
    v = build_views(cfg)[0]
    assert v["radius_nm"] == 4.34 and v["fetch_radius_nm"] == 9.78


def test_traffic_refresh_uses_fetch_radius(client, monkeypatch):
    seen = {}

    async def fake(lat, lon, nm):
        seen["nm"] = nm
        return []

    monkeypatch.setattr(main_mod.manager, "_fetch_traffic", fake)
    assert put(client, base_cfg()).status_code == 200
    client.get("/api/traffic", params={"view": "local:KSEA"})
    deadline = time.time() + 5
    while time.time() < deadline and "nm" not in seen:
        time.sleep(0.1)
    assert seen["nm"] == 9.78


def test_radar_frames_never_exceed_iem_window():
    cfg = Config.model_validate({"regions": [{"name": "A", "center_lat": 47, "center_lon": -122}], "radar": {"frames": 12, "interval_min": 5}})
    frames = build_views(cfg)[0]["radar"]["frames"]
    assert max(f["age_min"] for f in frames) == 55 and len(frames) == 12


# ---- config file durability ----------------------------------------------------

def test_load_falls_back_to_backup_then_defaults(tmp_path):
    p = str(tmp_path / "config.yaml")
    good = Config.model_validate(base_cfg())
    save_config(good, p)                     # writes config.yaml
    save_config(good, p)                     # rotates: config.yaml.bak.1 now exists
    assert os.path.exists(p + ".bak.1")
    with open(p, "w") as fh:
        fh.write("airports: [\n")           # corrupt the primary file
    loaded = load_config(p)
    assert loaded.source.startswith("backup:") and loaded.error
    assert loaded.config.airports[0].icao == "KSEA"
    with open(p + ".bak.1", "w") as fh:
        fh.write("not: [valid\n")
    loaded = load_config(p)
    assert loaded.source == "defaults" and loaded.error
    with open(p) as fh:                      # the broken file is left for inspection
        assert fh.read().startswith("airports: [")


# ---- remote display control (blackout) ----------------------------------------------

def test_blackout_requires_auth(client):
    assert client.post("/api/display/blackout", json={"seconds": 5}).status_code == 401
    assert client.post("/api/display/restore").status_code == 401
    assert client.get("/api/display").status_code == 200  # state is readable without auth


def test_blackout_and_restore(client):
    r = client.post("/api/display/blackout", json={"seconds": 60, "reason": "test"}, auth=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["active"] is True and body["reason"] == "test" and body["remaining_s"] > 50
    assert client.get("/api/display").json()["active"] is True
    assert client.get("/healthz").json()["status"]["display"]["active"] is True
    r = client.post("/api/display/restore", auth=AUTH)
    assert r.status_code == 200 and r.json()["active"] is False
    assert client.get("/api/display").json()["active"] is False


def test_blackout_auto_restores(client):
    r = client.post("/api/display/blackout", json={"seconds": 0.3}, auth=AUTH)
    assert r.json()["active"] is True
    deadline = time.time() + 5
    while time.time() < deadline and client.get("/api/display").json()["active"]:
        time.sleep(0.1)
    assert client.get("/api/display").json()["active"] is False


def test_blackout_hold_until_restored_and_validation(client):
    r = client.post("/api/display/blackout", auth=AUTH)  # no body: hold until restored
    assert r.status_code == 200 and r.json()["active"] is True and r.json()["until"] is None
    assert client.post("/api/display/blackout", json={"seconds": -1}, auth=AUTH).status_code == 422
    assert client.post("/api/display/blackout", json={"seconds": 999999}, auth=AUTH).status_code == 422
    assert client.post("/api/display/restore", auth=AUTH).json()["active"] is False


def test_sse_connected_event_carries_display_state(client):
    from app.main import _sse

    client.post("/api/display/blackout", json={"seconds": 30}, auth=AUTH)
    line = _sse({"event": "connected", "display": main_mod.manager.display_state()})
    assert '"active": true' in line
    client.post("/api/display/restore", auth=AUTH)


# ---- aircraft type lookup (for tar1090-style icons) -------------------------------------

def test_normalize_aircraft_adds_type_description_and_wtc():
    from app.sources.base import AIRCRAFT_TYPES, normalize_aircraft, type_info

    assert len(AIRCRAFT_TYPES) > 2000
    assert type_info("A320") == ("L2J", "M")
    assert type_info("c172") == ("L1P", "L")
    assert type_info("ZZZZ") == (None, None) and type_info(None) == (None, None)
    ac = normalize_aircraft({"hex": "a", "lat": 1, "lon": 2, "t": "DH8D", "dbFlags": 1})
    assert (ac["type_desc"], ac["wtc"], ac["db_flags"]) == ("L2T", "M", 1)
    ac = normalize_aircraft({"hex": "b", "lat": 1, "lon": 2})
    assert ac["type_desc"] is None and ac["wtc"] is None and ac["db_flags"] is None


# ---- basemap tile cache ----------------------------------------------------------------

def test_tile_math_matches_display_framing():
    from app.tiles import LAYOUTS, tiles_for_view, tiles_for_views

    small = tiles_for_view(47.45, -122.31, 4.34, *LAYOUTS[0])   # a 5-mile local view at 1080p
    zooms = sorted({z for z, _, _ in small})
    assert len(zooms) == 2 and 11 <= zooms[0] <= 13          # ideal zoom plus its parent
    assert 10 <= len(small) <= 120
    both = tiles_for_views([{"id": "v", "center_lat": 47.45, "center_lon": -122.31, "radius_nm": 4.34}])
    assert len(both) > len(small)                            # the 4K layout adds a zoom level
    assert tiles_for_views([{"id": "sat", "type": "satellite"}]) == []


def test_tile_endpoint_caches_and_validates(client, monkeypatch):
    calls = []
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64

    async def fake_upstream(z, x, y):
        calls.append((z, x, y))
        return png

    monkeypatch.setattr(main_mod.manager.tiles, "_fetch_upstream", fake_upstream)
    r = client.get("/tiles/13/1310/2859.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == png
    assert r.headers["cache-control"].startswith("public")
    assert client.get("/tiles/13/1310/2859.png").status_code == 200
    assert calls == [(13, 1310, 2859)]                       # second hit served from disk
    assert client.get("/tiles/25/1/1.png").status_code == 404
    assert client.get("/tiles/3/9/1.png").status_code == 404   # x out of range for z=3


def test_tile_endpoint_serves_stale_on_upstream_failure(client, monkeypatch):
    png = b"\x89PNG\r\n\x1a\n" + b"1" * 64

    async def ok(z, x, y):
        return png

    async def broken(z, x, y):
        raise RuntimeError("osm down")

    monkeypatch.setattr(main_mod.manager.tiles, "_fetch_upstream", ok)
    assert client.get("/tiles/12/655/1429.png").status_code == 200
    path = main_mod.manager.tiles.path(12, 655, 1429)
    old = time.time() - 30 * 86400
    os.utime(path, (old, old))                               # make it stale
    monkeypatch.setattr(main_mod.manager.tiles, "_fetch_upstream", broken)
    r = client.get("/tiles/12/655/1429.png")
    assert r.status_code == 200 and r.content == png         # stale beats nothing
    assert client.get("/tiles/12/655/1430.png").status_code == 502


def test_warm_reports_counts(client, monkeypatch):
    async def fake_upstream(z, x, y):
        return b"\x89PNG\r\n\x1a\n" + b"2" * 16

    monkeypatch.setattr(main_mod.manager.tiles, "_fetch_upstream", fake_upstream)
    monkeypatch.setattr("app.tiles.WARM_SPACING_S", 0)
    import asyncio

    views = [{"id": "v", "center_lat": 47.45, "center_lon": -122.31, "radius_nm": 2.0}]
    result = asyncio.run(main_mod.manager.tiles.warm(views))
    assert result["state"] == "done" and result["total"] > 0
    assert result["fetched"] + result["cached"] == result["total"] and result["failed"] == 0
    assert client.get("/healthz").json()["status"]["tiles"]["state"] == "done"


# ---- fix timestamps for client-side dead reckoning ---------------------------------

def test_normalize_aircraft_fix_time():
    from app.sources.base import feed_now, normalize_aircraft

    ac = normalize_aircraft({"hex": "a", "lat": 1, "lon": 2, "seen_pos": 2.5, "gs": 100, "track": 90}, now_s=1000.0)
    assert ac["fix_ts"] == 997.5 and ac["seen_pos"] == 2.5
    assert normalize_aircraft({"hex": "b", "lat": 1, "lon": 2}, now_s=1000.0)["fix_ts"] is None
    assert normalize_aircraft({"hex": "c", "lat": 1, "lon": 2, "seen_pos": 1})["fix_ts"] is None
    assert feed_now({"now": 1790000000.5}) == 1790000000.5          # readsb: seconds
    assert feed_now({"now": 1790000000500}) == 1790000000.5         # aggregators: milliseconds
    assert feed_now({}) is None
